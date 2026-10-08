"""
performance_runner.py
---------------------
Discovers the scripts of a performance project and executes them. Where the
scripts live is read from the project itself (utils/perf_project_layout), so a
framework onboarded in place works as well as one scaffolded by this
application:

  * Locust   - every module that declares a Locust User, wherever it sits, plus
               Selenium pytest journeys recorded by older versions (run through
               a generated Locust wrapper, so they behave like any other script)
  * JMeter   - every *.jmx plan; runs are delegated to jmeter_runner.py

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

from utils import perf_project_layout as layout

JMETER_TOOL = "Jmeter"

# Generated next to a Selenium pytest journey so Locust can run it.
UI_WRAPPER_PREFIX = "_bigqa_ui_"
UI_SUPPORT_IMPORT = "core.ui_journey"
UI_REQUIREMENT = "selenium>=4.20"
# A browser journey needs far longer than an HTTP one for its single checked pass.
SCRIPT_CHECK_UI_RUN_TIME = "5m"

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
    prefix = f"run_{stem}_"
    for run_dir in results_dir.iterdir():
        if not run_dir.is_dir() or not run_dir.name.startswith(prefix):
            continue
        candidates.extend(run_dir.glob("*.html"))
        # JMeter writes its dashboard as html/index.html.
        dashboard = run_dir / "html" / "index.html"
        if dashboard.is_file():
            candidates.append(dashboard)
    if not candidates:
        return ""
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return str(newest)


def _results_path(perf_dir, tool=""):
    return Path(layout.results_dir(perf_dir, JMETER_TOOL if tool == JMETER_TOOL else layout.LOCUST_TOOL))


def _last_run(results_dir, stem):
    last_report = _latest_report(results_dir, stem)
    return last_report, (
        datetime.fromtimestamp(os.path.getmtime(last_report)).strftime("%Y-%m-%d %H:%M")
        if last_report else "")


def _relative_path(perf_dir, path):
    return os.path.relpath(path, perf_dir).replace("\\", "/")


def _jmeter_script_type(plans_root, script_file, head):
    """The plan's type folder (api / cli / functional) anywhere under the root, else its marker, else API."""
    try:
        parts = Path(script_file).resolve().relative_to(Path(plans_root).resolve()).parts[:-1]
    except ValueError:
        parts = Path(script_file).parts[:-1]
    for part in parts:
        if part.lower() in _JMETER_TYPE_FOLDERS:
            return _JMETER_TYPE_FOLDERS[part.lower()]
    return declared_script_type(head) or SCRIPT_TYPE_API


def _list_jmeter_scripts(perf_dir):
    """
    Every `*.jmx` plan of the project. The title is the plan's declared
    `Test Case:`, else its TestPlan name, else the file name.
    """
    plans_root = layout.jmeter_plans_root(perf_dir)
    results_dir = _results_path(perf_dir, JMETER_TOOL)
    scripts = []
    for path in sorted(layout.detect_layout(perf_dir, JMETER_TOOL)["plans"], key=lambda p: p.lower()):
        script_file = Path(path)
        head = _unescape_xml(_read_head(script_file, limit=6000))
        plan_name = _JMX_TEST_PLAN_NAME.search(head)
        declared = _TEST_CASE_MARKER.search(head)
        last_report, last_run_at = _last_run(results_dir, script_file.stem)
        scripts.append({
            "title": (declared.group(1).strip() if declared else "")
                     or (plan_name.group(1).strip() if plan_name else "")
                     or script_title(script_file.name),
            "file_name": script_file.name,
            "relative_path": _relative_path(perf_dir, script_file),
            "script_type": _jmeter_script_type(plans_root, script_file, head),
            "last_report": last_report,
            "last_run_at": last_run_at,
        })
    return scripts


def list_scripts(perf_dir, tool=""):
    """
    List the performance scripts of a project, wherever they live.

    Returns a list of dicts: {title, file_name, relative_path, script_type,
    last_report, last_run_at}. Private / generated modules (leading `_`) are
    never listed - they are helpers, not scripts. JMeter projects list their
    *.jmx plans.
    """
    if tool == JMETER_TOOL:
        return _list_jmeter_scripts(perf_dir)
    detected = layout.detect_layout(perf_dir, layout.LOCUST_TOOL)
    results_dir = _results_path(perf_dir)

    scripts = []
    entries = [(p, False) for p in detected["scripts"]] + [(p, True) for p in detected["journeys"]]
    for path, journey in sorted(entries, key=lambda e: (_relative_path(perf_dir, e[0]).lower())):
        script_file = Path(path)
        last_report, last_run_at = _last_run(results_dir, script_file.stem)
        scripts.append({
            "title": declared_title(script_file) or script_title(script_file.name),
            "file_name": script_file.name,
            "relative_path": _relative_path(perf_dir, script_file),
            "script_type": (declared_script_type(_read_head(script_file)) or SCRIPT_TYPE_FUNCTIONAL)
                           if journey else locust_script_type(script_file),
            "last_report": last_report,
            "last_run_at": last_run_at,
        })
    return scripts


def resolve_script_path(perf_dir, file_name, tool="", include_functional=True):
    """
    Resolve a script name to the absolute path of a script of this project.

    Scripts are addressed by base name. Only files the project discovery found
    (or a generated `_bigqa_*` sibling of one) are returned, so a crafted
    request can never execute or delete an arbitrary file. Returns '' when
    nothing matches. `include_functional` is kept for older callers: Functional
    journeys are always runnable now.
    """
    name = os.path.basename(file_name or "")
    if not name or not perf_dir or not os.path.isdir(perf_dir):
        return ""
    tool = JMETER_TOOL if tool == JMETER_TOOL else layout.LOCUST_TOOL
    for fresh in (False, True):
        detected = layout.detect_layout(perf_dir, tool, fresh=fresh)
        candidates = layout.all_script_paths(perf_dir, tool)
        # The folder new scripts go to wins a base-name clash.
        preferred = layout.abs_dir(perf_dir, detected.get("script_dir") or detected.get("plans_root") or "")
        candidates.sort(key=lambda p: os.path.dirname(p) != preferred)
        for candidate in candidates:
            if os.path.basename(candidate) == name and os.path.isfile(candidate):
                return candidate
        if name.startswith(layout.GENERATED_PREFIX):
            folders = {os.path.dirname(p) for p in candidates} | {preferred}
            for folder in folders:
                candidate = os.path.join(folder, name)
                if os.path.isfile(candidate):
                    return candidate
    return ""


def count_run_artifacts(perf_dir, file_name, tool=""):
    """Number of results folders a script's past runs left behind."""
    return len(_run_artifact_dirs(perf_dir, file_name, tool))


def remove_run_artifacts(perf_dir, file_name, tool=""):
    """
    Delete the results folders of a script's past runs - the HTML reports and
    CSVs. Returns how many were removed.
    """
    removed = 0
    for run_dir in _run_artifact_dirs(perf_dir, file_name, tool):
        shutil.rmtree(run_dir, ignore_errors=True)
        if not run_dir.exists():
            removed += 1
    return removed


def _run_artifact_dirs(perf_dir, file_name, tool=""):
    """
    The `results/run_<stem>_<stamp>` folders belonging to one script.

    Matched by prefix rather than `glob`, because a stem is free to contain the
    characters glob treats as patterns (`[`, `*`, `?`).
    """
    stem = Path(os.path.basename(file_name or "")).stem
    results_dir = _results_path(perf_dir, tool)
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


def is_ui_script(script_path):
    """True for a Functional journey: a Locust Selenium script or a Selenium pytest."""
    head = _read_head(script_path, limit=20_000)
    return UI_SUPPORT_IMPORT in head or layout.is_ui_journey_source(head)


def ui_wrapper_source(target_name, levels):
    """A Locust script that runs the `test_*` functions of a Selenium pytest journey."""
    stem = Path(target_name).stem
    root = "Path(__file__).resolve()" + ".parent" * (levels + 1)
    return "\n".join([
        f'"""Generated by BIG QA - runs {target_name} (a Selenium pytest journey) as a Locust user."""',
        "",
        "import importlib.util",
        "import sys",
        "from pathlib import Path",
        "",
        "from locust import User, between, task",
        "",
        f"sys.path.insert(0, str({root}))",
        "",
        "from core.ui_journey import UiSession  # noqa: E402",
        "",
        f"_SPEC = importlib.util.spec_from_file_location({('_bigqa_journey_' + stem)!r},",
        f"                                               Path(__file__).resolve().parent / {target_name!r})",
        "_MODULE = importlib.util.module_from_spec(_SPEC)",
        "_SPEC.loader.exec_module(_MODULE)",
        "_TESTS = [value for name, value in vars(_MODULE).items() if name.startswith('test_') and callable(value)]",
        "",
        "",
        "class JourneyUser(User):",
        "    wait_time = between(1, 3)",
        "",
        "    def on_start(self):",
        f"        self.ui = UiSession(self.environment, base_url=self.host, name={stem!r})",
        "",
        "    def on_stop(self):",
        "        self.ui.quit()",
        "",
        "    @task",
        "    def journey(self):",
        "        for test in _TESTS:",
        "            test(self.ui)",
        "",
    ])


def _locust_entry_point(perf_dir, script_path):
    """
    The file to hand Locust. A Selenium pytest journey gets a generated Locust
    wrapper beside it; every other script runs as it is.
    """
    if not layout.is_ui_journey_source(_read_head(script_path, limit=20_000)):
        return script_path
    folder = os.path.dirname(script_path)
    wrapper = os.path.join(folder, f"{UI_WRAPPER_PREFIX}{Path(script_path).stem}.py")
    with open(wrapper, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(ui_wrapper_source(os.path.basename(script_path), layout.depth_below_root(perf_dir, folder)))
    return wrapper


def _module_available(python_exe, module, env):
    try:
        probe = subprocess.run([python_exe, "-c", f"import {module}"], capture_output=True, env=env, timeout=60)
        return probe.returncode == 0
    except Exception:
        return False


def _install_ui_requirements(perf_dir, python_exe, env):
    """
    Make the project ready for a browser journey: core/ui_journey.py and
    Selenium in its interpreter. Yields log lines; the last item is
    (ok, message).
    """
    from utils.recorded_script_writers import ensure_ui_support
    for note in ensure_ui_support(perf_dir):
        yield f"[Setup] {note}"
    if _module_available(python_exe, "selenium", env):
        yield (True, "")
        return
    yield "[Setup] Installing Selenium into the project's environment (first browser run only)..."
    try:
        process = subprocess.Popen(
            [python_exe, "-m", "pip", "install", UI_REQUIREMENT], cwd=perf_dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        for line in iter(process.stdout.readline, ""):
            if line.strip():
                yield f"[pip] {line.rstrip()}"
        process.stdout.close()
        ok = process.wait() == 0
    except Exception as error:
        yield (False, f"Selenium could not be installed: {error}")
        return
    yield (ok, "" if ok else (f"Selenium could not be installed. Run \"{python_exe} -m pip install "
                              f"{UI_REQUIREMENT}\" inside {perf_dir} and try again."))


def stream_run(perf_dir, script_file, host, mode="check", users=None, spawn_rate=None,
               run_duration=None, report_url_builder=None, result_stem=None, stats_sink=None,
               tool=""):
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

    script_path = resolve_script_path(perf_dir, script_file, tool)
    if not script_path:
        yield _sse("result", {"status": "error", "message": f"Script '{script_file}' was not found in this performance project."})
        return
    if not host:
        yield _sse("result", {"status": "error", "message": "This performance project has no Application URL configured."})
        return

    if tool == JMETER_TOOL:
        from ScriptRunnerEngine.jmeter_runner import stream_jmeter_run
        yield from stream_jmeter_run(
            perf_dir, script_path, host, mode=mode, users=users, spawn_rate=spawn_rate,
            run_duration=run_duration, report_url=report_url, result_stem=result_stem,
            stats_sink=stats_sink, results_root=str(_results_path(perf_dir, JMETER_TOOL)))
        return

    ui_journey = is_ui_script(script_path)
    headed = mode == "check"
    if headed:
        users, spawn_rate = SCRIPT_CHECK_USERS, SCRIPT_CHECK_SPAWN_RATE
        run_time = SCRIPT_CHECK_UI_RUN_TIME if ui_journey else SCRIPT_CHECK_RUN_TIME
    else:
        users = int(users)
        spawn_rate = int(spawn_rate) if spawn_rate else DEFAULT_SPAWN_RATE
        run_time = normalize_run_time(run_duration)

    stem = result_stem or Path(script_path).stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_dir = _results_path(perf_dir) / f"run_{stem}_{timestamp}"
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

    if ui_journey:
        # Browser journeys: one Chrome per user, visible during a script check.
        env["PERF_BASE_URL"] = host
        if headed:
            env["PERF_UI_HEADED"] = "1"
        outcome = (False, "")
        for item in _install_ui_requirements(perf_dir, python_exe, env):
            if isinstance(item, tuple):
                outcome = item
            else:
                yield _sse("log", {"msg": item})
        if not outcome[0]:
            yield _sse("result", {"status": "error", "message": outcome[1]})
            return
        script_path = _locust_entry_point(perf_dir, script_path)

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
