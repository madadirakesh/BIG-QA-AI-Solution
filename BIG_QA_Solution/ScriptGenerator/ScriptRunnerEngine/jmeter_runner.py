"""
jmeter_runner.py
----------------
Runs a JMeter test plan from the Performance Test page - the Script Check and
the Performance Test - and reports it the way a Locust run is reported, so the
grid, the run console and the performance dashboard work the same for both
tools.

* **Script check** - one thread, one iteration (`-Jthreads=1 -Jloops=1`), with
  a browser journey shown on screen (`-Jselenium.headless=false`).
* **Performance test** - the users / ramp-up / duration chosen in the
  run-configuration dialog.

JMeter itself is found in this order: the copy the jmeter-maven-plugin unpacked
under `target/` (which also carries the plan libraries from the pom),
`JMETER_HOME`, then `jmeter` on PATH. A Maven project that has not been built
yet is provisioned first (`mvn test-compile jmeter:configure`), and a plan that
drives a compiled Java Selenium journey is always compiled before it runs.

Plans read their settings as JMeter properties (`${__P(threads,5)}`); the pom's
<propertiesUser> are passed through, then the run's own values on top:
threads, rampup, duration, loops, the Selenium/Playwright switches and the
application URL as api.host / api.protocol / api.port.

The samples (`.jtl`) are summarised into Locust-format `<stem>_stats.csv`,
`_stats_history.csv` and `_failures.csv`, and JMeter's own HTML dashboard is
the run's report.
"""

import csv
import glob
import math
import os
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from ScriptRunnerEngine.performance_runner import (
    _runtime_env,
    _sse,
    active_performance_processes,
    read_stats_history_rows,
    read_stats_rows,
    _read_summary,
)

SCRIPT_CHECK_DURATION_SECONDS = 300
DEFAULT_DURATION_SECONDS = 60
JAVA_JOURNEY_MARKER = "JourneySampler"
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}
_PERCENTILES = (("50%", 50), ("66%", 66), ("75%", 75), ("80%", 80), ("90%", 90), ("95%", 95),
                ("98%", 98), ("99%", 99), ("99.9%", 99.9), ("99.99%", 99.99), ("100%", 100))
_TRANSACTION_SAMPLE = re.compile(r"^Number of samples in transaction", re.IGNORECASE)
_DURATION_FAILURE = re.compile(r"lasted too long.*?longer than ([\d,.]+) milliseconds", re.IGNORECASE)
_POM_PROPERTIES = re.compile(r"<properties>(.*?)</properties>", re.DOTALL)
_POM_USER_PROPERTIES = re.compile(r"<propertiesUser>(.*?)</propertiesUser>", re.DOTALL)
_XML_ENTRY = re.compile(r"<([A-Za-z_][\w.\-]*)>([^<]*)</\1>")
_MAVEN_REF = re.compile(r"\$\{([^}]+)\}")


# ─────────────────────────────────────────────────────────────────────────────
# Finding JMeter
# ─────────────────────────────────────────────────────────────────────────────

def _read(path, limit=400_000):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _uses_maven_plugin(perf_dir):
    return "jmeter-maven-plugin" in _read(os.path.join(perf_dir, "pom.xml"))


def _provisioned_jar(perf_dir):
    """The ApacheJMeter jar the jmeter-maven-plugin unpacked under target/, newest first."""
    target = os.path.join(perf_dir, "target")
    jars = glob.glob(os.path.join(target, "jmeter", "bin", "ApacheJMeter*.jar")) + \
        glob.glob(os.path.join(target, "*", "jmeter", "bin", "ApacheJMeter*.jar"))
    return max(jars, key=os.path.getmtime) if jars else ""


def find_jmeter(perf_dir):
    """('jar', path) or ('exe', path) for the JMeter to run, or ('', '') when there is none."""
    jar = _provisioned_jar(perf_dir)
    if jar:
        return "jar", jar
    home = os.environ.get("JMETER_HOME", "").strip()
    if home:
        jars = glob.glob(os.path.join(home, "bin", "ApacheJMeter*.jar"))
        if jars:
            return "jar", jars[0]
    exe = shutil.which("jmeter")
    if exe:
        return "exe", exe
    return "", ""


def _jmeter_env():
    """
    Subprocess env for Maven / Java: the app's proxy and TLS settings, plus
    PATH, JAVA_HOME and MAVEN_HOME repaired the way every other Maven run of
    this application is (a stale JAVA_HOME stops `mvn` from starting at all).
    """
    try:
        from ScriptRunnerEngine.runner import _runtime_env_for_command
        env = _runtime_env_for_command("mvn -version")
    except Exception:
        env = _runtime_env()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _java_executable(env):
    home = (env.get("JAVA_HOME") or "").strip()
    if home:
        candidate = os.path.join(home, "bin", "java.exe" if os.name == "nt" else "java")
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("java", path=env.get("PATH")) or "java"


def _maven_executable(env):
    path = env.get("PATH")
    return shutil.which("mvn", path=path) or shutil.which("mvn.cmd", path=path) or ""


def _stream_process(cmd, cwd, env, prefix=""):
    """Run a command, yielding its output lines; the last item is the exit code (int)."""
    try:
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1)
    except Exception as error:
        yield f"{prefix}Could not start {cmd[0]}: {error}"
        yield -1
        return
    active_performance_processes[process.pid] = process
    try:
        for line in iter(process.stdout.readline, ""):
            if line.strip():
                yield prefix + line.rstrip()
        process.stdout.close()
        yield process.wait()
    finally:
        active_performance_processes.pop(process.pid, None)


def _configure_goal(perf_dir):
    """
    The plugin's configure goal, bound to the pom's own configure execution so
    its configuration (plan libraries, properties) is the one that applies.
    """
    pom = _read(os.path.join(perf_dir, "pom.xml"))
    execution_id = "default"
    for block in re.findall(r"<execution>(.*?)</execution>", pom, re.DOTALL):
        if re.search(r"<goal>\s*configure\s*</goal>", block):
            match = re.search(r"<id>\s*([^<]+?)\s*</id>", block)
            execution_id = match.group(1) if match else "default"
            break
    return f"com.lazerycode.jmeter:jmeter-maven-plugin:configure@{execution_id}"


def _needs_compile(perf_dir, plan_head):
    return JAVA_JOURNEY_MARKER in plan_head and os.path.isdir(os.path.join(perf_dir, "src", "test", "java"))


def _prepare(perf_dir, plan_head, env):
    """
    Build what the run needs with Maven: JMeter itself the first time, and the
    Java Selenium journeys whenever the plan uses them. Yields log lines; the
    last item is (ok, message).
    """
    maven_project = _uses_maven_plugin(perf_dir)
    goals = []
    if maven_project and _needs_compile(perf_dir, plan_head):
        goals.append("test-compile")
    if maven_project and not _provisioned_jar(perf_dir):
        goals.append(_configure_goal(perf_dir))
    if goals:
        mvn = _maven_executable(env)
        if not mvn:
            yield (False, "Apache Maven was not found on PATH, so this JMeter project cannot be built. "
                          "Install Maven 3.8+ and try again.")
            return
        if "test-compile" not in goals:
            goals.insert(0, "test-compile")
        yield "[Maven] Preparing the project: mvn " + " ".join(goals)
        code = None
        # No -DskipTests: the jmeter-maven-plugin honours it and would skip
        # configure too. test-compile never runs the tests anyway.
        for item in _stream_process([mvn, "-B"] + goals, perf_dir, env, prefix="[Maven] "):
            if isinstance(item, int):
                code = item
            else:
                yield item
        if code != 0:
            yield (False, "Maven could not prepare the JMeter project (see the log above).")
            return
    kind, _ = find_jmeter(perf_dir)
    if not kind:
        yield (False, "JMeter was not found. Add the jmeter-maven-plugin to pom.xml, set JMETER_HOME, "
                      "or put jmeter on PATH, then try again.")
        return
    yield (True, "")


# ─────────────────────────────────────────────────────────────────────────────
# Properties
# ─────────────────────────────────────────────────────────────────────────────

def _pom_properties(perf_dir):
    """The pom's <propertiesUser>, with Maven ${...} references resolved where possible."""
    pom = _read(os.path.join(perf_dir, "pom.xml"))
    if not pom:
        return {}
    maven = {"project.basedir": perf_dir, "basedir": perf_dir,
             "project.build.directory": os.path.join(perf_dir, "target"),
             "project.build.testOutputDirectory": os.path.join(perf_dir, "target", "test-classes")}
    block = _POM_PROPERTIES.search(pom)
    if block:
        maven.update({k: v.strip() for k, v in _XML_ENTRY.findall(block.group(1))})
    user = _POM_USER_PROPERTIES.search(pom)
    if not user:
        return {}

    def resolve(value, depth=0):
        if depth > 5:
            return value
        return _MAVEN_REF.sub(lambda m: resolve(maven[m.group(1)], depth + 1) if m.group(1) in maven
                              else m.group(0), value)

    return {k: resolve(v.strip()) for k, v in _XML_ENTRY.findall(user.group(1))}


def run_properties(perf_dir, host, mode, users, spawn_rate, run_duration):
    """The JMeter properties for one run (str -> str)."""
    props = {k: v for k, v in _pom_properties(perf_dir).items() if "${" not in v}
    check = mode == "check"
    if check:
        threads, rampup, loops, duration = 1, 1, 1, SCRIPT_CHECK_DURATION_SECONDS
    else:
        threads = max(1, int(users or 1))
        rate = max(1, int(spawn_rate or 1))
        rampup = max(1, math.ceil(threads / rate))
        loops = -1
        duration = int(run_duration) * 60 if str(run_duration or "").strip().isdigit() else DEFAULT_DURATION_SECONDS
    props.update({
        "threads": threads, "rampup": rampup, "loops": loops, "duration": duration,
        "selenium.threads": threads, "selenium.loops": loops,
        "selenium.headless": str(not check).lower(), "pw.headless": str(not check).lower(),
    })
    url = (host or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.hostname:
        props.update({"api.host": parts.hostname, "api.protocol": parts.scheme or "https",
                      "selenium.baseUrl": url, "pw.baseUrl": url})
        if parts.port:
            props["api.port"] = parts.port
    classes = os.path.join(perf_dir, "target", "test-classes")
    if os.path.isdir(classes):
        existing = props.get("user.classpath", "")
        if os.path.normcase(classes) not in os.path.normcase(existing):
            props["user.classpath"] = classes + (os.pathsep + existing if existing else "")
    props.update({
        "jmeter.save.saveservice.output_format": "csv",
        "jmeter.save.saveservice.print_field_names": "true",
        "jmeter.save.saveservice.thread_counts": "true",
        "jmeter.save.saveservice.url": "true",
        "jmeter.reportgenerator.overall_granularity": "1000",
        # Single-pass checks finish quickly; keep the console summariser useful.
        "summariser.interval": "10",
    })
    return {k: str(v) for k, v in props.items()}


def _write_properties(path, props):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for key, value in props.items():
            # .properties escaping: backslashes (Windows paths) and separators.
            escaped = value.replace("\\", "\\\\").replace("\n", "\\n")
            handle.write(f"{key}={escaped}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Results: .jtl -> Locust-format CSVs
# ─────────────────────────────────────────────────────────────────────────────

def _percentile(sorted_values, pct):
    if not sorted_values:
        return 0
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def _histogram_percentile(histogram, count, pct):
    rank = max(1, math.ceil(pct / 100 * count))
    running = 0
    for value in sorted(histogram):
        running += histogram[value]
        if running >= rank:
            return value
    return 0


def _request_type(label):
    first = (label or "").split(" ", 1)[0].upper()
    return first if first in _HTTP_METHODS else "JMeter"


def _failure_text(row):
    message = (row.get("failureMessage") or "").strip()
    match = _DURATION_FAILURE.search(message)
    if match:
        # Same wording as a Locust threshold, so a breach is counted as one.
        limit = match.group(1).replace(",", "")
        return f"Response time exceeded the {limit} ms threshold"
    if message:
        return message
    code = (row.get("responseCode") or "").strip()
    text = (row.get("responseMessage") or "").strip()
    return f"{code} {text}".strip() or "Sample failed"


def read_jtl(jtl_path):
    if not os.path.isfile(jtl_path):
        return []
    try:
        with open(jtl_path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception:
        return []


def _stats_row(request_type, name, samples, span_seconds):
    elapsed = sorted(s["elapsed"] for s in samples)
    count = len(samples)
    failures = sum(1 for s in samples if not s["success"])
    span = max(span_seconds, 1e-9)
    row = {
        "Type": request_type, "Name": name,
        "Request Count": count, "Failure Count": failures,
        "Median Response Time": _percentile(elapsed, 50),
        "Average Response Time": round(sum(elapsed) / count, 2) if count else 0,
        "Min Response Time": elapsed[0] if elapsed else 0,
        "Max Response Time": elapsed[-1] if elapsed else 0,
        "Average Content Size": round(sum(s["bytes"] for s in samples) / count, 2) if count else 0,
        "Requests/s": round(count / span, 4),
        "Failures/s": round(failures / span, 4),
    }
    for header, pct in _PERCENTILES:
        row[header] = _percentile(elapsed, pct)
    return row


def summarise_jtl(rows):
    """Return (stats rows, history rows, failure rows) in Locust's CSV shapes."""
    samples = []
    for row in rows:
        try:
            samples.append({
                "ts": int(float(row.get("timeStamp") or 0)),
                "elapsed": int(float(row.get("elapsed") or 0)),
                "label": row.get("label") or "",
                "success": str(row.get("success", "true")).strip().lower() == "true",
                "bytes": int(float(row.get("bytes") or 0)),
                "threads": int(float(row.get("allThreads") or 0)),
                "transaction": bool(_TRANSACTION_SAMPLE.match(row.get("responseMessage") or "")),
                "row": row,
            })
        except ValueError:
            continue
    if not samples:
        return [], [], []

    start = min(s["ts"] for s in samples)
    end = max(s["ts"] + s["elapsed"] for s in samples)
    span = max((end - start) / 1000.0, 1.0)

    by_label = defaultdict(list)
    for sample in samples:
        by_label[sample["label"]].append(sample)
    stats = [_stats_row("TX" if entries[0]["transaction"] else _request_type(label), label, entries, span)
             for label, entries in by_label.items()]
    # A transaction sample wraps the requests it contains; counting both in the
    # totals would double the request count (JMeter's own summary skips them).
    requests = [s for s in samples if not s["transaction"]] or samples
    stats.append(_stats_row("", "Aggregated", requests, span))

    # One history row per second, like Locust's _stats_history.csv. Totals are
    # kept as a histogram so a long run is not re-sorted every second.
    buckets = defaultdict(list)
    for sample in requests:
        buckets[(sample["ts"] + sample["elapsed"]) // 1000].append(sample)
    history = []
    histogram, total, failed, elapsed_sum, bytes_sum = Counter(), 0, 0, 0, 0
    for second in sorted(buckets):
        current = buckets[second]
        for sample in current:
            histogram[sample["elapsed"]] += 1
            elapsed_sum += sample["elapsed"]
            bytes_sum += sample["bytes"]
            failed += not sample["success"]
        total += len(current)
        window = sorted(s["elapsed"] for s in current)
        row = {
            "Timestamp": second, "User Count": max(s["threads"] for s in current),
            "Type": "", "Name": "Aggregated",
            "Requests/s": len(current), "Failures/s": sum(1 for s in current if not s["success"]),
            "Total Request Count": total,
            "Total Failure Count": failed,
            "Total Median Response Time": _histogram_percentile(histogram, total, 50),
            "Total Average Response Time": round(elapsed_sum / total, 2),
            "Total Min Response Time": min(histogram), "Total Max Response Time": max(histogram),
            "Total Average Content Size": round(bytes_sum / total, 2),
        }
        for header, pct in _PERCENTILES:
            row[header] = _percentile(window, pct)
        history.append(row)

    failures = Counter()
    for sample in samples:
        if not sample["success"]:
            failures[(_request_type(sample["label"]), sample["label"], _failure_text(sample["row"]))] += 1
    failure_rows = [{"Method": m, "Name": n, "Error": e, "Occurrences": c} for (m, n, e), c in failures.items()]
    return stats, history, failure_rows


def _write_csv(path, rows):
    if not rows:
        return
    headers = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────────────────────

def stream_jmeter_run(perf_dir, script_path, host, mode="check", users=None, spawn_rate=None,
                      run_duration=None, report_url=None, result_stem=None, stats_sink=None,
                      results_root=None):
    """Execute one JMeter plan and yield the same Server-Sent Events as a Locust run."""
    check = mode == "check"
    plan_head = _read(script_path, 200_000)
    env = _jmeter_env()

    outcome = (False, "")
    for item in _prepare(perf_dir, plan_head, env):
        if isinstance(item, tuple):
            outcome = item
        else:
            yield _sse("log", {"msg": item})
    if not outcome[0]:
        yield _sse("result", {"status": "error", "message": outcome[1]})
        return

    stem = result_stem or Path(script_path).stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_dir = Path(results_root or os.path.join(perf_dir, "Results")) / f"run_{stem}_{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=True)
    jtl = str(result_dir / f"{stem}.jtl")
    html_dir = result_dir / "html"
    props_file = str(result_dir / "bigqa.properties")
    props = run_properties(perf_dir, host, mode, users, spawn_rate, run_duration)
    _write_properties(props_file, props)

    kind, jmeter = find_jmeter(perf_dir)
    base = [_java_executable(env), "-jar", jmeter] if kind == "jar" else [jmeter]
    cmd = base + ["-n", "-t", script_path, "-l", jtl, "-j", str(result_dir / "jmeter.log"),
                  "-q", props_file, "-e", "-o", str(html_dir)]

    threads, loops = props["threads"], props["loops"]
    yield _sse("started", {
        "mode": mode,
        "command": subprocess.list2cmdline(cmd),
        "web_url": "",
        "users": int(threads),
        "spawn_rate": spawn_rate if not check else 1,
        "run_time": f"{props['duration']}s" + (" (1 iteration)" if loops == "1" else ""),
        "result_dir": str(result_dir),
    })
    yield _sse("log", {"msg": f"[JMeter] threads={threads} rampup={props['rampup']}s "
                              f"duration={props['duration']}s loops={'infinite' if loops == '-1' else loops}"})

    return_code = None
    for item in _stream_process(cmd, os.path.dirname(script_path) or perf_dir, env):
        if isinstance(item, int):
            return_code = item
        else:
            yield _sse("log", {"msg": item})

    stats, history, failure_rows = summarise_jtl(read_jtl(jtl))
    csv_prefix = str(result_dir / stem)
    _write_csv(f"{csv_prefix}_stats.csv", stats)
    _write_csv(f"{csv_prefix}_stats_history.csv", history)
    _write_csv(f"{csv_prefix}_failures.csv", failure_rows)
    stats_rows = read_stats_rows(csv_prefix)

    if stats_sink:
        try:
            stats_sink(stats_rows, {
                "mode": mode,
                "script_file": Path(script_path).name,
                "result_stem": stem,
                "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "users": int(threads),
                "spawn_rate": spawn_rate,
                "run_time": f"{props['duration']}s",
                "result_dir": str(result_dir),
            }, read_stats_history_rows(csv_prefix))
        except Exception as error:
            yield _sse("log", {"msg": f"[Stats] The run statistics could not be saved: {error}"})

    summary = _read_summary(csv_prefix, stats_rows)
    report = html_dir / "index.html"
    report_path = str(report) if report.is_file() else ""
    if not stats:
        yield _sse("log", {"msg": "[JMeter] The plan produced no samples - check the log above and "
                                  f"{result_dir / 'jmeter.log'}."})
    failed = return_code != 0 or not stats or int(summary.get("failures") or 0) > 0
    yield _sse("result", {
        "status": "failed" if failed else "success",
        "return_code": return_code,
        "report_url": report_url(report_path) if report_url and report_path else "",
        "report_path": report_path,
        "summary": summary,
    })
