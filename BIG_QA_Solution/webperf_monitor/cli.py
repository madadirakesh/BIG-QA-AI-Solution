"""
CLI entry point.

    python -m webperf_monitor watch [--output-dir DIR] [--interval SECONDS] [--verbose] [--pid-file PATH]
    python -m webperf_monitor stop [--pid-file PATH] [--timeout SECONDS]

`watch` starts the auto-detect watcher IN THE FOREGROUND and blocks until
it's told to stop (Ctrl+C, or an external `stop` command). This is
designed to run as an independent, standalone process - it does not need
to live inside your test automation framework's source tree, and it isn't
imported by your test code. It just needs to run as a separate process on
the SAME machine as your browser automation (it inspects local OS
processes and connects to localhost debugging ports), for the whole
duration your tests are running.

Typical CI / test-framework integration:

    # setup / before-suite
    webperf-monitor watch --output-dir webperf_reports --pid-file /tmp/webperf.pid --verbose &

    # run your existing Selenium/Playwright suite, unchanged
    pytest tests/

    # teardown / after-suite - stops the watcher AND waits for any
    # still-open browser sessions to finalize and write their reports
    # before returning.
    webperf-monitor stop --pid-file /tmp/webperf.pid

HOW STOPPING WORKS (and why it isn't just a signal):
  The report is only written during shutdown, so the shutdown MUST be
  graceful or the whole run is lost. Signals can't deliver that portably:
  on Windows `os.kill(pid, SIGTERM)` maps to `TerminateProcess`, which
  kills the process outright - the SIGTERM handler never runs, the
  watcher never finalizes, and no report is written. The same is true of
  `taskkill`, closing the console window, and most IDE "stop" buttons.

  So `stop` primarily works by writing a sentinel file next to the pid
  file (`<pid-file>.stop`), which the running watcher polls for. That is
  portable, needs no signal delivery, and always lets the watcher finish
  writing. SIGTERM is still sent on POSIX for immediate pickup, and
  SIGINT/SIGTERM/SIGBREAK handlers are still installed so Ctrl+C keeps
  working. `stop` then waits for the watcher to actually exit before
  returning, so a CI teardown step can rely on the report existing once
  the command comes back.

  `kill -9` (POSIX) and `taskkill /F` (Windows) still skip all of this
  and should be avoided.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time


def _write_pid_file(path: str) -> None:
    with open(path, "w") as f:
        f.write(str(os.getpid()))


def _stop_file_for(pid_file: str) -> str:
    """Sentinel path a `stop` command touches to request graceful shutdown."""
    return pid_file + ".stop"


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:  # pragma: no cover - psutil is a hard dependency
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True


def _install_stop_handlers(set_flag) -> None:
    """
    Install handlers for every shutdown signal this platform can actually
    deliver. SIGINT covers Ctrl+C everywhere; SIGTERM is what POSIX process
    managers send; SIGBREAK is Ctrl+Break on Windows (which some IDE stop
    buttons and `CTRL_BREAK_EVENT` senders use). Unsupported names are
    simply skipped.
    """
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda signum, frame: set_flag())
        except (ValueError, OSError, RuntimeError):
            # e.g. not on the main thread, or unsupported on this platform
            pass


def _cmd_watch(args) -> int:
    from .watcher import Watcher

    stop_file = _stop_file_for(args.pid_file) if args.pid_file else None
    if stop_file:
        # A sentinel left behind by a previous run would stop this one
        # instantly - clear it before we start.
        _unlink(stop_file)

    if args.pid_file:
        _write_pid_file(args.pid_file)

    def on_report(result):
        score = result.get("performance_score")
        print(f"[webperf_monitor] consolidated report written "
              f"({result.get('session_count', 0)} session(s), "
              f"{result.get('page_count', 0)} distinct page(s) over "
              f"{result.get('measured_page_loads', 0)} measured load(s)) "
              f"- avg performance score: {score}")

    stop_hint = (f"run 'webperf-monitor stop --pid-file {args.pid_file}'"
                 if args.pid_file else "press Ctrl+C in this window")
    print(f"[webperf_monitor] watching for automated browser sessions "
          f"(reports -> {os.path.abspath(args.output_dir)}). PID={os.getpid()}. "
          f"To stop and write the report, {stop_hint}.")
    if not args.verbose:
        print("[webperf_monitor] tip: re-run with --verbose to see which browsers "
              "are detected and attached to.")

    w = Watcher(output_dir=args.output_dir, poll_interval=args.interval,
                on_report=on_report, verbose=args.verbose).start()

    stop_requested = {"flag": False}

    def _request_stop() -> None:
        stop_requested["flag"] = True

    _install_stop_handlers(_request_stop)

    try:
        while not stop_requested["flag"]:
            # The sentinel file is the portable stop channel - see the module
            # docstring for why we can't rely on signals alone (Windows).
            if stop_file and os.path.exists(stop_file):
                print(f"\n[webperf_monitor] stop requested via {stop_file}")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        # Only reachable if the SIGINT handler could not be installed.
        pass
    finally:
        print("[webperf_monitor] stopping watcher and flushing pending reports...")
        paths = w.stop(timeout=args.shutdown_timeout)
        if paths is None:
            # The watcher writes nothing when it captured no sessions, and it
            # only says so under --verbose. Say it unconditionally here: a
            # silent exit is indistinguishable from success and is the most
            # common "it ran but produced no report" complaint.
            print("[webperf_monitor] WARNING: no browser sessions were captured, "
                  "so no report was written.")
            print("[webperf_monitor]   - the browser must start AND be running/exit "
                  "while this watcher is up")
            print("[webperf_monitor]   - it must be launched with "
                  "--remote-debugging-port plus one of --enable-automation / "
                  "--headless / --test-type (Selenium does this; default "
                  "Playwright does NOT - it uses a pipe)")
            print("[webperf_monitor]   - re-run with --verbose to see detection "
                  "and CDP attach diagnostics")
        if stop_file:
            _unlink(stop_file)
        if args.pid_file:
            _unlink(args.pid_file)
        print("[webperf_monitor] stopped.")
    return 0


def _cmd_stop(args) -> int:
    if not os.path.exists(args.pid_file):
        print(f"[webperf_monitor] no pid file at {args.pid_file} - is the watcher running?")
        return 1

    try:
        with open(args.pid_file) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        print(f"[webperf_monitor] pid file {args.pid_file} is missing or malformed.")
        return 1

    stop_file = _stop_file_for(args.pid_file)

    if not _pid_alive(pid):
        print(f"[webperf_monitor] no live process with pid {pid} (stale pid file); removing it.")
        _unlink(args.pid_file)
        _unlink(stop_file)
        return 1

    try:
        with open(stop_file, "w") as f:
            f.write(str(pid))
    except OSError as exc:
        print(f"[webperf_monitor] could not write stop file {stop_file}: {exc}")
        return 1

    # POSIX only: SIGTERM is picked up instantly, so the watcher doesn't have
    # to wait for its next sentinel poll. On Windows os.kill() is
    # TerminateProcess - it would kill the watcher mid-run and destroy the
    # report we're trying to collect, so it is never sent there.
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    print(f"[webperf_monitor] stop requested for pid {pid}; waiting up to "
          f"{args.timeout}s for it to finalize and write pending reports...")

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            print("[webperf_monitor] watcher exited cleanly - reports have been written.")
            _unlink(stop_file)
            _unlink(args.pid_file)
            return 0
        time.sleep(0.25)

    print(f"[webperf_monitor] WARNING: pid {pid} is still running after {args.timeout}s. "
          f"It may still be finalizing a slow session; raise --timeout, or check the "
          f"watcher's own output. Not force-killing it, because that would discard "
          f"the report.")
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="webperf_monitor")
    sub = parser.add_subparsers(dest="command", required=True)

    watch_p = sub.add_parser("watch", help="Auto-detect and monitor automated browser sessions")
    watch_p.add_argument("--output-dir", default="webperf_reports",
                          help="Directory to write report.json/report.html into (default: webperf_reports)")
    watch_p.add_argument("--interval", type=float, default=1.0,
                          help="Process-scan interval in seconds (default: 1.0)")
    watch_p.add_argument("--pid-file", default=None,
                          help="Write the watcher's PID here so it can be stopped later with "
                               "'webperf-monitor stop --pid-file PATH'")
    watch_p.add_argument("--shutdown-timeout", type=float, default=20.0,
                          help="Max seconds to wait for in-flight sessions to finalize on stop (default: 20)")
    watch_p.add_argument("--verbose", action="store_true",
                          help="Print diagnostic info about detection/attachment (recommended if reports "
                               "aren't appearing, so you can see why)")
    watch_p.set_defaults(func=_cmd_watch)

    stop_p = sub.add_parser("stop", help="Gracefully stop a watcher started with --pid-file")
    stop_p.add_argument("--pid-file", required=True, help="Path passed to 'watch --pid-file'")
    stop_p.add_argument("--timeout", type=float, default=45.0,
                        help="Max seconds to wait for the watcher to finish writing its report "
                             "before returning (default: 45). Should exceed the watcher's "
                             "--shutdown-timeout.")
    stop_p.set_defaults(func=_cmd_stop)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
