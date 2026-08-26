"""
performance_runner.py
---------------------
Discovers and executes the Locust scripts of a scaffolded performance project
(`<project path>/<project name>_perf`, created from the Performance
Configuration screen).

Two execution modes are supported:

* **Script check** - a headed, single-user validation run. Locust starts with
  its web UI (so the run is watchable live), auto-starts, and auto-quits once
  the short run window closes, leaving an HTML report behind.
* **Performance test** - a headless run driven by the concurrent users / spawn
  rate / duration chosen in the run-configuration dialog.

Both modes stream their output as Server-Sent Events and finish by pointing at
the generated Locust HTML report.
"""

import csv
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LOCUSTFILES_DIRNAME = "locustfiles"
RESULTS_DIRNAME = "results"

# A script check is a validation run, not a load run: one user, one spawn per
# second, and a short window so the user gets a verdict quickly.
SCRIPT_CHECK_USERS = 1
SCRIPT_CHECK_SPAWN_RATE = 1
SCRIPT_CHECK_RUN_TIME = "30s"
# Seconds Locust stays alive after the run so the web UI can be read before it exits.
SCRIPT_CHECK_AUTOQUIT_SECONDS = 5

DEFAULT_SPAWN_RATE = 1
DEFAULT_RUN_DURATION_MINUTES = 1

# pid -> Popen, so an in-flight run can be aborted from the UI.
active_performance_processes = {}

# Words that should keep their conventional casing in a generated script title.
_TITLE_ACRONYMS = {
    "http": "HTTP", "https": "HTTPS", "api": "API", "xml": "XML", "json": "JSON",
    "csv": "CSV", "grpc": "gRPC", "websocket": "WebSocket", "ws": "WS",
    "db": "DB", "ui": "UI", "url": "URL", "id": "ID", "sql": "SQL",
}


def _sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def script_title(filename):
    """Turn `http_api_test.py` into `HTTP API Test`."""
    stem = Path(filename).stem
    words = [w for w in stem.replace("-", "_").split("_") if w]
    return " ".join(_TITLE_ACRONYMS.get(w.lower(), w.capitalize()) for w in words) or stem


# A recorded script's filename is a timestamped slug (rec_Shop_20260821_140301.py),
# which makes a poor grid label. The recorder writes a `Test Case:` line into the
# module docstring; when one is present it wins over the filename.
_TEST_CASE_MARKER = re.compile(r"^\s*Test Case:\s*(.+?)\s*$", re.MULTILINE)
_TITLE_SCAN_BYTES = 2000


def declared_title(script_file):
    """Return the script's declared `Test Case:` title, or '' when it has none."""
    try:
        with open(script_file, "r", encoding="utf-8", errors="ignore") as handle:
            head = handle.read(_TITLE_SCAN_BYTES)
    except OSError:
        return ""
    match = _TEST_CASE_MARKER.search(head)
    return match.group(1).strip() if match else ""


def _latest_report(results_dir, stem):
    """Return the newest HTML report produced for `stem`, or '' when there is none."""
    if not results_dir.is_dir():
        return ""
    candidates = []
    for run_dir in results_dir.glob(f"run_{stem}_*"):
        if not run_dir.is_dir():
            continue
        candidates.extend(run_dir.glob("*.html"))
    if not candidates:
        return ""
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return str(newest)


def list_scripts(perf_dir):
    """
    List the performance scripts of a scaffolded project.

    Returns a list of dicts: {title, file_name, relative_path, last_report,
    last_run_at}. Private/`__init__` modules are skipped - they are helpers,
    not runnable Locust scripts.
    """
    perf_path = Path(perf_dir)
    locust_dir = perf_path / LOCUSTFILES_DIRNAME
    results_dir = perf_path / RESULTS_DIRNAME
    if not locust_dir.is_dir():
        return []

    scripts = []
    for script_file in sorted(locust_dir.glob("*.py")):
        if script_file.name.startswith("_"):
            continue
        last_report = _latest_report(results_dir, script_file.stem)
        scripts.append({
            "title": declared_title(script_file) or script_title(script_file.name),
            "file_name": script_file.name,
            "relative_path": f"{LOCUSTFILES_DIRNAME}/{script_file.name}",
            "last_report": last_report,
            "last_run_at": (
                datetime.fromtimestamp(os.path.getmtime(last_report)).strftime("%Y-%m-%d %H:%M")
                if last_report else ""
            ),
        })
    return scripts


def resolve_script_path(perf_dir, file_name):
    """
    Resolve a script name to an absolute path inside the project's locustfiles
    folder. Returns '' when the name escapes that folder or does not exist, so a
    crafted request can never execute an arbitrary file.
    """
    locust_dir = Path(perf_dir) / LOCUSTFILES_DIRNAME
    candidate = (locust_dir / os.path.basename(file_name or "")).resolve()
    try:
        candidate.relative_to(locust_dir.resolve())
    except ValueError:
        return ""
    return str(candidate) if candidate.is_file() else ""


def resolve_python(perf_dir):
    """Prefer the performance project's own venv interpreter; fall back to this app's."""
    venv_python = Path(perf_dir) / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    return str(venv_python) if venv_python.is_file() else sys.executable


def _find_free_port(preferred=8089):
    for port in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
        except OSError:
            continue
    return preferred


def normalize_run_time(run_duration):
    """Accept 5, '5', '5m', '30s' or '4h' and return a Locust --run-time value in that form."""
    text = str(run_duration or "").strip().lower()
    if not text:
        return f"{DEFAULT_RUN_DURATION_MINUTES}m"
    if text.isdigit():
        return f"{int(text)}m"
    return text


def build_locust_command(python_exe, script_path, host, users, spawn_rate, run_time,
                         html_out, csv_prefix, headed=False, web_port=None):
    """
    Build the Locust command line for a run.

    Headless runs exit as soon as `--run-time` elapses. Headed runs keep the
    Locust web UI up: `--autostart` begins the run without a click and
    `--autoquit` shuts the process down afterwards, so the HTML report is still
    written and the UI is still watchable while it happens.
    """
    cmd = [
        python_exe, "-m", "locust",
        "-f", script_path,
        "--host", host,
        "--users", str(users),
        "--spawn-rate", str(spawn_rate),
        "--run-time", run_time,
        "--html", html_out,
        "--csv", csv_prefix,
        "--only-summary",
    ]
    if headed:
        cmd += ["--autostart", "--autoquit", str(SCRIPT_CHECK_AUTOQUIT_SECONDS),
                "--web-host", "127.0.0.1", "--web-port", str(web_port)]
    else:
        cmd.append("--headless")
    return cmd


def _runtime_env():
    """Subprocess env with the app's proxy/TLS settings and unbuffered UTF-8 output."""
    try:
        from ScriptRunnerEngine.runner import _runtime_env_with_ca
        env = _runtime_env_with_ca()
    except Exception:
        env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _locust_available(python_exe, env):
    try:
        probe = subprocess.run(
            [python_exe, "-c", "import locust"],
            capture_output=True, env=env, timeout=60,
        )
        return probe.returncode == 0
    except Exception:
        return False


def _read_summary(csv_prefix):
    """Pull the aggregated totals out of Locust's `<prefix>_stats.csv`."""
    stats_file = f"{csv_prefix}_stats.csv"
    if not os.path.isfile(stats_file):
        return {}
    try:
        with open(stats_file, "r", encoding="utf-8", errors="ignore", newline="") as handle:
            for row in csv.DictReader(handle):
                if (row.get("Name") or "").strip() == "Aggregated":
                    return {
                        "requests": row.get("Request Count", "0"),
                        "failures": row.get("Failure Count", "0"),
                        "avg_response_ms": row.get("Average Response Time", ""),
                        "p95_ms": row.get("95%", ""),
                        "requests_per_sec": row.get("Requests/s", ""),
                    }
    except Exception:
        pass
    return {}


def stream_run(perf_dir, script_file, host, mode="check", users=None, spawn_rate=None,
               run_duration=None, report_url_builder=None):
    """
    Execute one performance script and yield Server-Sent Events.

    Events: `started` (command + optional live web UI url), `log` (one output
    line), and a final `result` (status, report url, aggregated summary).
    """
    def report_url(path):
        if not path or not os.path.exists(path):
            return ""
        return report_url_builder(path) if report_url_builder else ""

    script_path = resolve_script_path(perf_dir, script_file)
    if not script_path:
        yield _sse("result", {"status": "error", "message": f"Script '{script_file}' was not found in this performance project."})
        return
    if not host:
        yield _sse("result", {"status": "error", "message": "This performance project has no Application URL configured."})
        return

    headed = mode == "check"
    if headed:
        users, spawn_rate, run_time = SCRIPT_CHECK_USERS, SCRIPT_CHECK_SPAWN_RATE, SCRIPT_CHECK_RUN_TIME
    else:
        users = int(users)
        spawn_rate = int(spawn_rate) if spawn_rate else DEFAULT_SPAWN_RATE
        run_time = normalize_run_time(run_duration)

    stem = Path(script_path).stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_dir = Path(perf_dir) / RESULTS_DIRNAME / f"run_{stem}_{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=True)
    html_out = str(result_dir / f"{stem}_report.html")
    csv_prefix = str(result_dir / stem)

    python_exe = resolve_python(perf_dir)
    env = _runtime_env()
    if not _locust_available(python_exe, env):
        yield _sse("result", {
            "status": "error",
            "message": ("Locust is not installed for this performance project. Run "
                        f"\"{python_exe} -m pip install -r requirements.txt\" inside {perf_dir} and try again."),
        })
        return

    web_port = _find_free_port() if headed else None
    cmd = build_locust_command(python_exe, script_path, host, users, spawn_rate,
                               run_time, html_out, csv_prefix, headed=headed, web_port=web_port)

    yield _sse("started", {
        "mode": mode,
        "command": subprocess.list2cmdline(cmd),
        "web_url": f"http://127.0.0.1:{web_port}" if headed else "",
        "users": users,
        "spawn_rate": spawn_rate,
        "run_time": run_time,
        "result_dir": str(result_dir),
    })

    process = None
    try:
        process = subprocess.Popen(
            cmd, cwd=perf_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
        active_performance_processes[process.pid] = process
        for line in iter(process.stdout.readline, ''):
            if line:
                yield _sse("log", {"msg": line.rstrip()})
        process.stdout.close()
        return_code = process.wait()
    except Exception as e:
        yield _sse("result", {"status": "error", "message": f"Failed to start the performance run: {e}"})
        return
    finally:
        if process and process.pid in active_performance_processes:
            del active_performance_processes[process.pid]

    summary = _read_summary(csv_prefix)
    # Locust exits non-zero when requests failed or thresholds were not met, so a
    # non-zero code still produces a report worth showing.
    yield _sse("result", {
        "status": "success" if return_code == 0 else "failed",
        "return_code": return_code,
        "report_url": report_url(html_out),
        "report_path": html_out if os.path.exists(html_out) else "",
        "summary": summary,
    })


def stop_active_runs():
    """Terminate every in-flight performance run. Returns the number stopped."""
    stopped = 0
    for pid, process in list(active_performance_processes.items()):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=30)
            else:
                process.terminate()
            stopped += 1
        except Exception:
            pass
        active_performance_processes.pop(pid, None)
    return stopped
