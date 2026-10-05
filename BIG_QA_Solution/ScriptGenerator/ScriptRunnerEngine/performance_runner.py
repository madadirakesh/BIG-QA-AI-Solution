"""
performance_runner.py
---------------------
Discovers the scripts of a performance project (Locust locustfiles, or the
JMeter framework's TestScripts plans) and executes the Locust ones
(`<project path>/<project name>_perf`, created from the Performance
Configuration screen).

Two execution modes are supported:

* **Script check** - a headed, single-user validation run of exactly one pass
  of the journey. Locust starts with its web UI (so the run is watchable live),
  auto-starts, and auto-quits once that pass finishes, leaving an HTML report
  behind. The single pass comes from `locust_check_hook.py`, loaded as a second
  locustfile; Locust has no flag for it.
* **Performance test** - a headless run driven by the concurrent users / spawn
  rate / duration chosen in the run-configuration dialog.

Both modes stream their output as Server-Sent Events and finish by pointing at
the generated Locust HTML report.
"""

import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LOCUSTFILES_DIRNAME = "locustfiles"
RESULTS_DIRNAME = "results"

# Recorded Functional journeys of a Locust project are Selenium pytests.
FUNCTIONAL_DIRNAME = "functional"

# JMeter projects keep their test plans in TestScripts/<type>/*.jmx.
JMETER_TOOL = "Jmeter"
JMETER_SCRIPTS_DIRNAME = "TestScripts"

# Script types shown in the grid's Type column.
SCRIPT_TYPE_API = "API"
SCRIPT_TYPE_CLI = "CLI"
SCRIPT_TYPE_FUNCTIONAL = "Functional"
_SCRIPT_TYPES = {t.lower(): t for t in (SCRIPT_TYPE_API, SCRIPT_TYPE_CLI, SCRIPT_TYPE_FUNCTIONAL)}
# The JMeter framework's folder per script type.
_JMETER_TYPE_FOLDERS = {"api": SCRIPT_TYPE_API, "cli": SCRIPT_TYPE_CLI, "functional": SCRIPT_TYPE_FUNCTIONAL}

# A script check is a validation run, not a load run: one user making one pass
# of the journey. The single pass is enforced by SCRIPT_CHECK_HOOK, because
# Locust itself has no "run once" switch - so SCRIPT_CHECK_RUN_TIME is only a
# ceiling for a journey that hangs, not the length of the check.
SCRIPT_CHECK_USERS = 1
SCRIPT_CHECK_SPAWN_RATE = 1
SCRIPT_CHECK_RUN_TIME = "30s"
SCRIPT_CHECK_HOOK = "locust_check_hook.py"
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
# Generated scripts also declare their type (`Script Type: API`).
_SCRIPT_TYPE_MARKER = re.compile(r"^\s*Script Type:\s*(.+?)\s*$", re.MULTILINE)
# The recorder's docstring header, for recordings made before the type marker existed.
_RECORDED_MARKER = re.compile(r"^\s*Recorded from\s*:", re.MULTILINE)
_JMX_TEST_PLAN_NAME = re.compile(r'<TestPlan\b[^>]*\btestname="([^"]*)"')


def declared_title(script_file):
    """Return the script's declared `Test Case:` title, or '' when it has none."""
    try:
        with open(script_file, "r", encoding="utf-8", errors="ignore") as handle:
            head = handle.read(_TITLE_SCAN_BYTES)
    except OSError:
        return ""
    match = _TEST_CASE_MARKER.search(head)
    return match.group(1).strip() if match else ""


def _read_head(script_file, limit=_TITLE_SCAN_BYTES):
    try:
        with open(script_file, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def declared_script_type(head):
    """The `Script Type:` a script declares, normalised to API / CLI / Functional; '' when none."""
    match = _SCRIPT_TYPE_MARKER.search(head or "")
    return _SCRIPT_TYPES.get(match.group(1).strip().lower(), "") if match else ""


def locust_script_type(script_file):
    """
    Type of a Locust script: its declared marker, else CLI for a browser
    recording (its HTTP traffic replayed - the CLI choice in Create Test), else
    API - the framework's own sample locustfiles are all protocol-level API tests.
    """
    head = _read_head(script_file)
    return declared_script_type(head) or (
        SCRIPT_TYPE_CLI if _RECORDED_MARKER.search(head) else SCRIPT_TYPE_API)


def _unescape_xml(text):
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        text = text.replace(entity, char)
    return text


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


def _list_jmeter_scripts(perf_path):
    """
    The `*.jmx` test plans under TestScripts/. The type is the plan's folder
    (api / cli / functional), else its declared marker, else API; the title is
    the plan's declared `Test Case:`, else its TestPlan name, else the file name.
    """
    scripts_dir = perf_path / JMETER_SCRIPTS_DIRNAME
    if not scripts_dir.is_dir():
        return []
    scripts = []
    for script_file in sorted(scripts_dir.rglob("*.jmx"), key=lambda p: str(p).lower()):
        relative = script_file.relative_to(scripts_dir)
        head = _unescape_xml(_read_head(script_file, limit=6000))
        folder = relative.parts[0].lower() if len(relative.parts) > 1 else ""
        plan_name = _JMX_TEST_PLAN_NAME.search(head)
        declared = _TEST_CASE_MARKER.search(head)
        scripts.append({
            "title": (declared.group(1).strip() if declared else "")
                     or (plan_name.group(1).strip() if plan_name else "")
                     or script_title(script_file.name),
            "file_name": script_file.name,
            "relative_path": f"{JMETER_SCRIPTS_DIRNAME}/{relative.as_posix()}",
            "script_type": _JMETER_TYPE_FOLDERS.get(folder) or declared_script_type(head) or SCRIPT_TYPE_API,
            # JMeter runs are not driven from this screen yet, so there is no report to link.
            "last_report": "",
            "last_run_at": "",
        })
    return scripts


def list_scripts(perf_dir, tool=""):
    """
    List the performance scripts of a project.

    Returns a list of dicts: {title, file_name, relative_path, script_type,
    last_report, last_run_at}. For Locust, private/`__init__` modules are
    skipped - they are helpers, not runnable Locust scripts. JMeter projects
    list their TestScripts/**/*.jmx plans.
    """
    perf_path = Path(perf_dir)
    if tool == JMETER_TOOL:
        return _list_jmeter_scripts(perf_path)
    locust_dir = perf_path / LOCUSTFILES_DIRNAME
    results_dir = perf_path / RESULTS_DIRNAME

    scripts = []
    for script_file in sorted(locust_dir.glob("*.py")) if locust_dir.is_dir() else []:
        if script_file.name.startswith("_"):
            continue
        last_report = _latest_report(results_dir, script_file.stem)
        scripts.append({
            "title": declared_title(script_file) or script_title(script_file.name),
            "file_name": script_file.name,
            "relative_path": f"{LOCUSTFILES_DIRNAME}/{script_file.name}",
            "script_type": locust_script_type(script_file),
            "last_report": last_report,
            "last_run_at": (
                datetime.fromtimestamp(os.path.getmtime(last_report)).strftime("%Y-%m-%d %H:%M")
                if last_report else ""
            ),
        })

    # Recorded Functional journeys: Selenium pytests, run with pytest rather
    # than Locust, so there is no Locust report to link.
    functional_dir = perf_path / FUNCTIONAL_DIRNAME
    for script_file in sorted(functional_dir.glob("test_*.py")) if functional_dir.is_dir() else []:
        scripts.append({
            "title": declared_title(script_file) or script_title(script_file.name),
            "file_name": script_file.name,
            "relative_path": f"{FUNCTIONAL_DIRNAME}/{script_file.name}",
            "script_type": declared_script_type(_read_head(script_file)) or SCRIPT_TYPE_FUNCTIONAL,
            "last_report": "",
            "last_run_at": "",
        })
    return scripts


def resolve_script_path(perf_dir, file_name, tool="", include_functional=False):
    """
    Resolve a script name to an absolute path inside the project's locustfiles
    folder (JMeter: anywhere under TestScripts/, matched by base name). Returns
    '' when the name escapes that folder or does not exist, so a crafted request
    can never execute an arbitrary file.

    `include_functional` also looks in functional/ (the Selenium pytests). It is
    off by default because every caller but delete hands the path to Locust.
    """
    if tool == JMETER_TOOL:
        scripts_dir = (Path(perf_dir) / JMETER_SCRIPTS_DIRNAME).resolve()
        name = os.path.basename(file_name or "")
        if not name.lower().endswith(".jmx") or not scripts_dir.is_dir():
            return ""
        for candidate in scripts_dir.rglob(name):
            if candidate.is_file() and candidate.name == name:
                return str(candidate)
        return ""

    locust_dir = Path(perf_dir) / LOCUSTFILES_DIRNAME
    folders = [locust_dir] + ([Path(perf_dir) / FUNCTIONAL_DIRNAME] if include_functional else [])
    for folder in folders:
        candidate = (folder / os.path.basename(file_name or "")).resolve()
        try:
            candidate.relative_to(folder.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return str(candidate)
    return ""


def count_run_artifacts(perf_dir, file_name):
    """Number of results folders a script's past runs left behind."""
    return len(_run_artifact_dirs(perf_dir, file_name))


def remove_run_artifacts(perf_dir, file_name):
    """
    Delete the results folders of a script's past runs - the HTML reports and
    Locust CSVs. Returns how many were removed.
    """
    removed = 0
    for run_dir in _run_artifact_dirs(perf_dir, file_name):
        shutil.rmtree(run_dir, ignore_errors=True)
        if not run_dir.exists():
            removed += 1
    return removed


def _run_artifact_dirs(perf_dir, file_name):
    """
    The `results/run_<stem>_<stamp>` folders belonging to one script.

    Matched by prefix rather than `glob`, because a stem is free to contain the
    characters glob treats as patterns (`[`, `*`, `?`).
    """
    stem = Path(os.path.basename(file_name or "")).stem
    results_dir = Path(perf_dir) / RESULTS_DIRNAME
    if not stem or not results_dir.is_dir():
        return []
    prefix = f"run_{stem}_"
    return [d for d in results_dir.iterdir() if d.is_dir() and d.name.startswith(prefix)]


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


def script_check_locustfiles(script_path):
    """
    The `-f` value for a script check: the script plus the single-pass hook.

    Returns the script alone when the hook cannot be passed - Locust splits
    `-f` on commas, so a path containing one would be read as two locustfiles.
    Such a check falls back to being bounded by `--run-time`.
    """
    hook = str(Path(__file__).resolve().parent / SCRIPT_CHECK_HOOK)
    if "," in script_path or "," in hook or not os.path.isfile(hook):
        return script_path
    return f"{script_path},{hook}"


def build_locust_command(python_exe, script_path, host, users, spawn_rate, run_time,
                         html_out, csv_prefix, headed=False, web_port=None):
    """
    Build the Locust command line for a run.

    Headless runs exit as soon as `--run-time` elapses. Headed runs keep the
    Locust web UI up: `--autostart` begins the run without a click and
    `--autoquit` shuts the process down afterwards, so the HTML report is still
    written and the UI is still watchable while it happens.

    A script check additionally loads `locust_check_hook.py`, which ends the run
    after one pass of the journey. Locust has no flag for that, and without it a
    one-user check repeats the journey until `--run-time` expires.
    """
    cmd = [
        python_exe, "-m", "locust",
        "-f", script_check_locustfiles(script_path) if headed else script_path,
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


def _read_csv_rows(path):
    """Read a Locust CSV as a list of header -> value dicts; [] when unreadable."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except Exception:
        return []


def read_stats_rows(csv_prefix):
    """
    Read every row of Locust's `<prefix>_stats.csv` - one per request plus the
    trailing "Aggregated" total - as a list of header -> value dicts.
    """
    return _read_csv_rows(f"{csv_prefix}_stats.csv")


def read_stats_history_rows(csv_prefix):
    """
    Read Locust's `<prefix>_stats_history.csv` - one sample per interval for the
    duration of the run - as a list of header -> value dicts.

    This is the run's timeline. `_stats.csv` only has the totals a run ended at,
    so without these samples there is nothing to plot against elapsed time.
    """
    return _read_csv_rows(f"{csv_prefix}_stats_history.csv")


def _read_summary(csv_prefix, rows):
    """Pull the aggregated totals out of the parsed `_stats.csv` rows."""
    for row in rows:
        if (row.get("Name") or "").strip() == "Aggregated":
            summary = {
                "requests": row.get("Request Count", "0"),
                "failures": row.get("Failure Count", "0"),
                "avg_response_ms": row.get("Average Response Time", ""),
                "p95_ms": row.get("95%", ""),
                "requests_per_sec": row.get("Requests/s", ""),
            }
            breaches = _count_threshold_breaches(csv_prefix)
            if breaches:
                summary["threshold_failures"] = breaches
            return summary
    return {}


# A response-time threshold fails its request with this wording (generated by
# utils/payload_parameterizer._threshold_preamble). Locust files it as the
# failure's error text, which is what tells a breach apart from an HTTP error.
_THRESHOLD_ERROR_MARKER = "ms threshold"


def _count_threshold_breaches(csv_prefix):
    """How many requests failed for coming back slower than their threshold."""
    failures_file = f"{csv_prefix}_failures.csv"
    if not os.path.isfile(failures_file):
        return 0
    total = 0
    try:
        with open(failures_file, "r", encoding="utf-8", errors="ignore", newline="") as handle:
            for row in csv.DictReader(handle):
                if _THRESHOLD_ERROR_MARKER in (row.get("Error") or ""):
                    total += int(row.get("Occurrences") or 0)
    except Exception:
        return 0
    return total


def stream_run(perf_dir, script_file, host, mode="check", users=None, spawn_rate=None,
               run_duration=None, report_url_builder=None, result_stem=None, stats_sink=None):
    """
    Execute one performance script and yield Server-Sent Events.

    Events: `started` (command + optional live web UI url), `log` (one output
    line), and a final `result` (status, report url, aggregated summary).

    `result_stem` names the results folder and report files when it differs from
    the executed file: a payload-driven run executes a generated copy of the
    script, but its report belongs to the script the tester recorded.

    `stats_sink`, when given, is called once with `(rows, run_info, history)`
    after the run: `rows` are the parsed `_stats.csv` totals, `run_info` the
    settings they were measured under, and `history` the parsed
    `_stats_history.csv` samples that make up the run's timeline. It is how the
    caller persists a run's numbers - the runner itself owns no database.
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

    stem = result_stem or Path(script_path).stem
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

    stats_rows = read_stats_rows(csv_prefix)
    if stats_sink:
        # Recording the run must never mask its result, so a failing sink is
        # reported as a log line and the run still reports normally.
        try:
            stats_sink(stats_rows, {
                "mode": mode,
                "script_file": Path(script_file).name,
                "result_stem": stem,
                "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "users": users,
                "spawn_rate": spawn_rate,
                "run_time": run_time,
                "result_dir": str(result_dir),
            }, read_stats_history_rows(csv_prefix))
        except Exception as e:
            yield _sse("log", {"msg": f"[Stats] The run statistics could not be saved: {e}"})

    summary = _read_summary(csv_prefix, stats_rows)
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
