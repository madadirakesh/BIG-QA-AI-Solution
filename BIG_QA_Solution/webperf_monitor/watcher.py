"""
Background watcher: polls running processes, detects Chromium-family
browsers that were launched by automation tooling (Selenium/Playwright),
attaches to them over CDP, and writes a report once each one closes -
with zero changes required to the automation script itself.

HOW DETECTION WORKS (please read - this is a heuristic, not a guarantee):
  There is no OS-level flag that marks "this browser was launched by a
  script". What we actually detect is the command-line switches that
  automation tooling adds when it launches Chrome:
    - `--remote-debugging-port=<port>` (opens a CDP TCP endpoint)
    - `--enable-automation` (added by chromedriver for every Selenium session)
  Selenium (via chromedriver) always adds both, so it is reliably detected.
  Playwright, by default, talks to Chromium over a debugging *pipe*, not a
  TCP port, so a plain `page.goto(...)` script is invisible to an external
  watcher. To make a Playwright session visible to this watcher, launch
  Chromium with an explicit port, e.g.:
      browser = p.chromium.launch(args=["--remote-debugging-port=9222"])
  Otherwise, use `PlaywrightMonitor` (the explicit hook) instead - it
  doesn't need the watcher at all.

Requires: psutil
"""

from __future__ import annotations

import os
import re
import time
import threading
from typing import Callable, Optional

try:
    import psutil
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The watcher requires the 'psutil' package. Install it with: pip install psutil"
    ) from exc

from .cdp_client import CDPClient, find_page_ws_url, wait_for_debugger_address
from .collector import PerformanceSession
from .report import write_consolidated_report

BROWSER_NAME_PATTERN = re.compile(r"(chrome|chromium|msedge)", re.IGNORECASE)
PORT_PATTERN = re.compile(r"--remote-debugging-port=(\d+)")
USER_DATA_DIR_PATTERN = re.compile(r"--user-data-dir=([^\s]+)")
AUTOMATION_FLAG = "--enable-automation"


def _is_automation_browser(proc: "psutil.Process"):
    """Return (declared_port, cmdline_string) if this process looks like the
    TOP-LEVEL automated browser process, else None. declared_port may be 0,
    meaning "OS-assigned" - the real port has to be resolved separately.

    Important: Chrome's multi-process architecture spawns many child
    processes (renderer, GPU, network service, crashpad handler, utility)
    that are also named chrome.exe/chromium/msedge and often inherit most
    of the parent's command line, INCLUDING --remote-debugging-port and
    --enable-automation. Only the actual browser process is missing a
    `--type=...` switch, so that's what distinguishes it - this is the
    same technique tools like Puppeteer/chrome-launcher use to find the
    root process. Without this check, every renderer/GPU/utility process
    gets misdetected as a separate "browser", spawning many duplicate
    connection attempts to the same devtools socket and starving out the
    one that would have succeeded.
    """
    try:
        name = proc.name() or ""
        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None

    if not BROWSER_NAME_PATTERN.search(name) and not any(BROWSER_NAME_PATTERN.search(c) for c in cmdline[:1]):
        return None

    joined = " ".join(cmdline)

    if "--type=" in joined:
        return None  # child process (renderer/gpu/utility/crashpad/...), not the browser itself

    port_match = PORT_PATTERN.search(joined)
    if not port_match:
        return None  # no CDP port requested -> nothing we can attach to

    # `--remote-debugging-pipe` is Playwright's default transport and is only
    # ever present when a parent process handed the browser pipe handles at
    # launch - a human-launched Chrome never has it. Treating it as an
    # automation marker is what makes a HEADED Playwright run detectable:
    # Playwright deliberately omits --enable-automation (to avoid anti-bot
    # fingerprinting), so without this the headed case fails the check even
    # when the user has added an explicit --remote-debugging-port.
    looks_automated = (AUTOMATION_FLAG in joined
                       or "--headless" in joined
                       or "--test-type" in joined
                       or "--remote-debugging-pipe" in joined)
    if not looks_automated:
        return None

    return int(port_match.group(1)), joined


def _resolve_actual_port(declared_port: int, cmdline: str, wait_seconds: float = 8.0) -> Optional[int]:
    """
    chromedriver (Selenium) almost always launches Chrome with
    `--remote-debugging-port=0`, meaning "let the OS pick a free port".
    When that happens, Chrome writes the real port it bound to into
    `<user-data-dir>/DevToolsActivePort` shortly after startup. This reads
    that file. If a non-zero port was declared on the command line, it's
    used as-is.
    """
    if declared_port != 0:
        return declared_port

    match = USER_DATA_DIR_PATTERN.search(cmdline)
    if not match:
        return None
    user_data_dir = match.group(1).strip('"')
    port_file = os.path.join(user_data_dir, "DevToolsActivePort")

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            with open(port_file, "r") as f:
                first_line = f.readline().strip()
            if first_line.isdigit():
                return int(first_line)
        except (FileNotFoundError, PermissionError):
            pass
        time.sleep(0.2)
    return None


class Watcher:
    def __init__(self, output_dir: str = "webperf_reports", poll_interval: float = 1.0,
                 on_report: Optional[Callable[[dict], None]] = None,
                 label_fn: Optional[Callable[[int], str]] = None,
                 verbose: bool = False):
        self.output_dir = output_dir
        self.poll_interval = poll_interval
        self.on_report = on_report
        self.label_fn = label_fn or (lambda pid: f"auto-detected-pid-{pid}")
        self.verbose = verbose
        self._seen_pids: set[int] = set()
        self._seen_addresses: set[str] = set()
        self._addresses_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._monitor_threads: list[threading.Thread] = []
        self._threads_lock = threading.Lock()
        # Finalized per-browser session results, consolidated into ONE report
        # when the watcher is stopped (instead of each session overwriting a
        # single report.json/report.html).
        self._results: list[dict] = []
        self._results_lock = threading.Lock()

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[webperf_monitor] {msg}")

    def start(self) -> "Watcher":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 20.0) -> "Optional[dict]":
        """
        Signal the poll loop to stop, wait for any in-flight browser sessions
        to finalize, then write ONE consolidated report covering every session
        seen during the run. Call this from your test framework's teardown -
        this is the "end of the test" that the watcher waits for. Returns the
        paths of the consolidated report (or None if no sessions were seen).
        """
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

        with self._threads_lock:
            pending = list(self._monitor_threads)
        if pending:
            self._log(f"waiting up to {timeout}s for {len(pending)} active session(s) to finalize...")
        deadline = time.time() + timeout
        for t in pending:
            remaining = max(0.0, deadline - time.time())
            t.join(timeout=remaining)
            if t.is_alive():
                self._log(f"warning: session thread {t.name} did not finish within timeout; "
                           f"its metrics may be missing from the consolidated report")

        return self._write_consolidated_report()

    def _write_consolidated_report(self) -> "Optional[dict]":
        with self._results_lock:
            results = list(self._results)
        if not results:
            self._log("no browser sessions were captured - nothing to report")
            return None
        paths = write_consolidated_report(results, self.output_dir)
        self._log(f"consolidated report written for {len(results)} session(s) "
                  f"-> {paths['html']}")
        if self.on_report:
            self.on_report(paths.get("result", {}))
        return paths

    def _run(self) -> None:
        self._log(f"scanning for automation-launched browsers every {self.poll_interval}s...")
        while not self._stop.is_set():
            try:
                for proc in psutil.process_iter(["pid", "name"]):
                    if proc.pid in self._seen_pids:
                        continue
                    detection = _is_automation_browser(proc)
                    if detection is not None:
                        declared_port, cmdline = detection
                        self._seen_pids.add(proc.pid)
                        self._log(f"detected automated browser: pid={proc.pid} "
                                  f"declared_port={declared_port}")
                        t = threading.Thread(
                            target=self._attach_and_monitor,
                            args=(proc.pid, declared_port, cmdline),
                            daemon=True,
                            name=f"webperf-monitor-{proc.pid}",
                        )
                        with self._threads_lock:
                            self._monitor_threads.append(t)
                        t.start()
            except Exception as e:  # noqa: BLE001
                self._log(f"error while scanning processes: {e}")
            self._stop.wait(self.poll_interval)

    def _connect(self, address: str, pid: int, quiet: bool = False) -> Optional[CDPClient]:
        """Attach a fresh CDP client to `address`, or return None on failure."""
        try:
            wait_for_debugger_address(address, timeout=10)
            ws_url = find_page_ws_url(address)
            return CDPClient(ws_url)
        except Exception as e:  # noqa: BLE001
            if not quiet:
                self._log(f"pid={pid}: failed to attach over CDP at {address}: {e}")
            return None

    def _attach_and_monitor(self, pid: int, declared_port: int, cmdline: str) -> None:
        port = _resolve_actual_port(declared_port, cmdline)
        if port is None:
            self._log(f"pid={pid}: could not resolve a real debugging port "
                       f"(declared port was {declared_port}) - giving up on this session")
            return

        address = f"127.0.0.1:{port}"

        # Second line of defense against duplicate detections (e.g. two
        # processes that both legitimately reference the same profile/port):
        # only ever attach once per resolved address.
        with self._addresses_lock:
            if address in self._seen_addresses:
                self._log(f"pid={pid}: address {address} is already being monitored "
                           f"by another detection - skipping duplicate")
                return
            self._seen_addresses.add(address)

        client = self._connect(address, pid)
        if client is None:
            return

        self._log(f"pid={pid}: attached over CDP at {address}")
        session = PerformanceSession(client, label=self.label_fn(pid))

        def finalize(result: dict) -> None:
            # Collect the finalized session instead of writing a report per
            # browser (which would overwrite the previous one). All sessions
            # are consolidated into a single report when the watcher stops.
            with self._results_lock:
                self._results.append(result)
            self._log(f"pid={pid}: session captured "
                      f"(score: {result.get('performance_score')}, "
                      f"urls: {len(result.get('page_loads', []))})")

        session.on_finalized(finalize)
        # We manage finalization ourselves so a tab closing mid-session doesn't
        # prematurely end capture. We keep monitoring this browser until it
        # actually exits, or until the whole test ends (stop()).
        session.start(finalize_on_disconnect=False)

        # While the browser is alive we snapshot metrics every poll so nothing
        # is lost if it exits abruptly. If a tab closes but the browser (test)
        # is still running, we re-attach to another page target and keep going.
        # We only stop monitoring THIS browser once its process exits (that
        # session is then captured) - the watcher itself keeps scanning for
        # further browsers until the test ends.
        while not self._stop.is_set() and psutil.pid_exists(pid):
            if client.is_closed:
                new_client = self._connect(address, pid, quiet=True)
                if new_client is not None:
                    session.rebind(new_client)
                    client = new_client
                    self._log(f"pid={pid}: re-attached over CDP after target closed")
            else:
                session.snapshot()
            self._stop.wait(self.poll_interval)

        # Browser exited or the test ended -> finalize this session, using live
        # data if the browser is still up, otherwise the last snapshot taken
        # before it closed. finalize() appends it to the consolidated results.
        session.stop_and_finalize()
        if not client.is_closed:
            client.close()


def watch(output_dir: str = "webperf_reports", poll_interval: float = 1.0,
          on_report: Optional[Callable[[dict], None]] = None, verbose: bool = False) -> Watcher:
    """Convenience function: create and start a Watcher."""
    return Watcher(output_dir=output_dir, poll_interval=poll_interval,
                    on_report=on_report, verbose=verbose).start()
