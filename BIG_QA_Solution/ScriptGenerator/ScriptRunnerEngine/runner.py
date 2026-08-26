import os
import sys
import json
import glob
import shutil
import subprocess
import signal
import threading
import time
import html
import re
from collections import deque
from datetime import datetime
from urllib.parse import quote

active_processes = {} # pid -> process object

# Browser-performance monitors that are currently running, one per streaming
# execution. The abort endpoint finalizes these directly so a force-killed run
# still writes its report instead of losing it.
active_perf_sessions = []
_perf_sessions_lock = threading.Lock()


class WebPerfSession:
    """
    Runs webperf_monitor's auto-detect Watcher for the lifetime of one test
    execution, so clicking "Launch Execution" also profiles whatever browser
    the tests drive - with no change to the project under test.

    The watcher polls the local process list for Chromium browsers carrying
    automation switches (which is exactly what Selenium/chromedriver
    produces), attaches over CDP, and writes ONE consolidated
    Lighthouse-style report when stopped.

    Everything here is best-effort by design: monitoring must never break the
    run it observes, so each entry point swallows its own errors and surfaces
    them as a console line instead of raising.
    """

    ENV_FLAG = "BIGQA_WEBPERF"

    def __init__(self, full_path: str, start_time: float):
        stamp = datetime.fromtimestamp(start_time).strftime("%Y%m%d_%H%M%S")
        # Written inside the project folder, which serve_report() already
        # treats as an allowed base - so the HTML report is viewable in-app.
        self.output_dir = os.path.join(full_path, "webperf_reports", stamp)
        self._watcher = None
        self._lock = threading.Lock()
        # Set once finalization has completed (or once we know there is
        # nothing to finalize). Lets a second caller wait for the first
        # caller's result instead of racing past an empty one - see stop().
        self._done = threading.Event()
        # Diagnostics from the watcher's own threads, drained into the run's
        # console by the streaming generator. Bounded so a pathological run
        # can't grow it without limit.
        self._logs = deque(maxlen=500)
        self.report_path = None
        self.summary = None

    @classmethod
    def is_enabled(cls) -> bool:
        """Monitoring is on by default; set BIGQA_WEBPERF=0 to turn it off."""
        return os.environ.get(cls.ENV_FLAG, "1").strip().lower() not in ("0", "false", "no", "off")

    def start(self):
        """Start monitoring. Returns a console line to show, or None if off."""
        if not self.is_enabled():
            self._done.set()
            return None
        try:
            parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if parent_dir not in sys.path:
                sys.path.insert(0, parent_dir)
            from webperf_monitor.watcher import Watcher
        except Exception as e:
            self._done.set()
            return (f"[WebPerf] Performance monitoring unavailable ({e}). "
                    f"Install its dependency with: pip install psutil")
        try:
            # 0.5s so a short-lived browser isn't missed between scans.
            self._watcher = self._build_watcher(Watcher).start()
        except Exception as e:
            self._watcher = None
            self._done.set()
            return f"[WebPerf] Could not start performance monitoring: {e}"
        with _perf_sessions_lock:
            active_perf_sessions.append(self)
        return ("[WebPerf] Browser performance monitoring started - any automated "
                "browser this run launches will be profiled.")

    def _build_watcher(self, watcher_cls):
        """
        Watcher subclass whose diagnostics land in this run's console instead
        of the Flask server's stdout. Detection is a heuristic - when it does
        not fire, or a CDP attach fails, the person who clicked "Launch
        Execution" is the one who needs to see why.
        """
        sink = self._logs

        class _SinkWatcher(watcher_cls):
            def _log(self, msg):
                sink.append(f"[WebPerf] {msg}")

        return _SinkWatcher(output_dir=self.output_dir, poll_interval=0.5, verbose=True)

    def drain_logs(self) -> list:
        """Pop everything the watcher threads have logged since the last call."""
        lines = []
        while True:
            try:
                lines.append(self._logs.popleft())
            except IndexError:
                return lines

    def stop(self, timeout: float = 25.0):
        """
        Finalize monitoring and write the consolidated report.

        Idempotent and thread-safe: the streaming generator and the abort
        endpoint may both call this, from different threads, in either order.
        The first caller does the work and returns the summary; a second
        caller waits for that work to finish (so it sees the finished report
        path rather than racing past an empty one) and returns None.
        """
        with self._lock:
            watcher, self._watcher = self._watcher, None
        if watcher is None:
            # Either monitoring never started - in which case _done is already
            # set and this returns immediately - or another thread is
            # finalizing right now and we wait for it.
            self._done.wait(timeout=timeout)
            return None
        with _perf_sessions_lock:
            if self in active_perf_sessions:
                active_perf_sessions.remove(self)
        try:
            try:
                paths = watcher.stop(timeout=timeout)
            except Exception as e:
                self.summary = f"[WebPerf] Error while finalizing the performance report: {e}"
                return self.summary
            if not paths:
                self.summary = ("[WebPerf] No automated browser session was detected during this run, "
                                "so no performance report was written. (Expected for API/non-UI tests. "
                                "Playwright needs --remote-debugging-port to be visible to the watcher.)")
                return self.summary
            self.report_path = paths.get("html")
            result = paths.get("result") or {}
            self.summary = (f"[WebPerf] Performance report ready - {result.get('session_count', 0)} browser "
                            f"session(s), {result.get('total_urls', 0)} URL(s), average score "
                            f"{result.get('performance_score')}.")
            return self.summary
        finally:
            self._done.set()

    @property
    def report_url(self) -> str:
        if not self.report_path:
            return ""
        return (f"/api/script-runner/report?path={quote(self.report_path, safe='')}"
                f"&run={int(time.time() * 1000)}")


def stop_all_perf_sessions() -> int:
    """
    Finalize every in-flight performance monitor. Called by the abort endpoint:
    force-killing the test process tree also kills the browser being profiled,
    so the report has to be written here rather than waiting for the stream to
    unwind. Returns how many sessions were finalized.
    """
    with _perf_sessions_lock:
        sessions = list(active_perf_sessions)
    stopped = 0
    for session in sessions:
        try:
            # Shorter than the streaming path's timeout: this one blocks an
            # interactive "Stop" click, so it must not hang the UI.
            session.stop(timeout=15.0)
            # Count sessions that ended up finalized, whether this call did the
            # work or the unwinding stream beat us to it.
            if session.summary:
                stopped += 1
        except Exception:
            pass
    return stopped


def _runtime_env_with_ca():
    """
    Base environment for test-run subprocesses.

    Reuses EnvironmentSetup._download_env() so running tests (and the runtime
    project-local `npx --no-install playwright install` that generated TypeScript hooks trigger in
    BeforeAll) gets the SAME TLS/CA + proxy settings as project creation does. Without this,
    creation would succeed behind a corporate proxy but the first test run would fail on the
    same certificate error we already solved once — see
    ProjectBootstrapper/environment_setup.py for the single source of that config.

    Falls back to a plain os.environ copy if that module can't be imported for any reason, so
    test execution never hard-fails on an import hiccup.
    """
    try:
        from ProjectBootstrapper.environment_setup import EnvironmentSetup
        return EnvironmentSetup._download_env()
    except Exception:
        return os.environ.copy()


def _runtime_env_for_command(cmd: str):
    """Build the subprocess env for a specific command, preserving proxy/TLS settings.

    We start from the CA/proxy-aware base env, then let EnvironmentSetup normalise PATH plus
    tool-home variables (notably JAVA_HOME for Maven runs) so execution matches the preflight
    dependency checks.
    """
    base_env = _runtime_env_with_ca()
    try:
        try:
            from ProjectBootstrapper.environment_setup import EnvironmentSetup
        except ImportError:
            try:
                from ScriptGenerator.ProjectBootstrapper.environment_setup import EnvironmentSetup
            except ImportError:
                from BIG_QA_Solution.ScriptGenerator.ProjectBootstrapper.environment_setup import EnvironmentSetup
        return EnvironmentSetup.prepare_runtime_env(cmd, base_env=base_env)
    except Exception:
        return base_env


class ScriptRunnerService:
    MAX_HEALING_RETRIES = 3

    # Self-healing is deliberately narrow: it repairs element locators, and it
    # only ever rewrites Page Object files. Step definitions, feature files,
    # hooks, config and test data are the human-authored contract of the suite -
    # editing them would silently change what the test asserts, so any failure
    # that is not a locator failure in a Page Object fails the run instead.
    PAGE_OBJECT_DIR_NAMES = {
        'pages', 'page', 'pageobjects', 'page_objects', 'pageobject', 'page_object'
    }
    PAGE_OBJECT_EXTENSIONS = {'.py', '.java', '.ts', '.js', '.cs'}
    NON_HEALABLE_DIR_NAMES = {
        'steps', 'step', 'stepdefinitions', 'step_definitions', 'stepdefs',
        'features', 'feature', 'hooks', 'support', 'utils', 'util', 'utilities',
        'helpers', 'helper', 'runners', 'runner', 'config', 'configs', 'configuration',
        'testdata', 'test_data', 'data', 'resources', 'reports', 'results'
    }
    # Applied to the file name too, so a step definition parked in `pages/` is
    # still rejected.
    NON_HEALABLE_FILE_MARKERS = (
        'step', 'hook', 'runner', 'config', 'environment', 'conftest', 'fixture'
    )

    REPORT_EXTENSIONS = {'.html', '.htm', '.pdf', '.json', '.xml', '.png', '.txt'}
    REPORT_DIR_HINTS = ("results", "reports", "outputs", "allure-results", "allure-report", "playwright-report")
    REPORT_SKIP_DIRS = {
        '.git', '.idea', '.venv', 'venv', 'env', 'node_modules', '__pycache__',
        'site-packages', 'dist-packages', 'classes', 'test-classes', 'src',
        'target', 'bin', 'obj'
    }

    @staticmethod
    def _inspect_project_files(full_path: str) -> dict:
        """Inspect key build/runtime files so commands can be validated before execution."""
        manifests = {
            "package_json": os.path.exists(os.path.join(full_path, "package.json")),
            "requirements_txt": os.path.exists(os.path.join(full_path, "requirements.txt")),
            "pyproject_toml": os.path.exists(os.path.join(full_path, "pyproject.toml")),
            "pom_xml": os.path.exists(os.path.join(full_path, "pom.xml")),
            "build_gradle": os.path.exists(os.path.join(full_path, "build.gradle")),
            "dotnet_project": False,
            "has_python_files": False,
            "has_feature_files": False,
        }

        try:
            for root, dirs, files in os.walk(full_path):
                dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', 'venv', '.venv', 'target', 'bin', 'obj', '__pycache__'}]
                if not manifests["dotnet_project"] and any(name.endswith('.csproj') for name in files):
                    manifests["dotnet_project"] = True
                if not manifests["has_python_files"] and any(name.endswith('.py') for name in files):
                    manifests["has_python_files"] = True
                if not manifests["has_feature_files"] and any(name.endswith('.feature') for name in files):
                    manifests["has_feature_files"] = True
                if manifests["dotnet_project"] and manifests["has_python_files"] and manifests["has_feature_files"]:
                    break
        except Exception:
            pass

        return manifests

    @classmethod
    def _iter_report_candidates(cls, full_path: str, start_time: float | None = None):
        candidates = []
        report_extensions = cls.REPORT_EXTENSIONS
        skip_dirs = cls.REPORT_SKIP_DIRS

        def should_skip_dir(root_path: str) -> bool:
            parts = {part.lower() for part in root_path.replace('\\', '/').split('/') if part}
            return any(part in skip_dirs for part in parts)

        def path_priority(file_path: str, ext: str) -> tuple:
            normalized = file_path.replace('\\', '/').lower()
            filename = os.path.basename(normalized)
            priority = 0
            in_report_dir = any(segment in normalized for segment in ('/results/', '/reports/', '/allure-report/', '/playwright-report/'))
            named_report_file = (
                'report' in filename or
                'cucumber' in filename or
                'behave' in filename or
                'allure' in filename or
                filename == 'index.html'
            )

            if not in_report_dir and not named_report_file:
                return (-1, os.path.getmtime(file_path))

            if in_report_dir:
                priority += 50
            if filename in {'report.html', 'cucumber.html', 'cucumber_report.html', 'behave_report.html', 'index.html'}:
                priority += 25
            elif 'behave_report' in filename:
                priority += 20
            if ext in {'.html', '.htm', '.pdf'}:
                priority += 10
            elif ext in {'.txt', '.json', '.xml'}:
                priority += 5

            return (priority, os.path.getmtime(file_path))

        report_roots = []
        try:
            for item in os.listdir(full_path):
                item_path = os.path.join(full_path, item)
                if not os.path.isdir(item_path):
                    continue
                item_lower = item.lower()
                if any(hint in item_lower for hint in cls.REPORT_DIR_HINTS) and item_lower not in skip_dirs:
                    report_roots.append(item_path)
        except Exception:
            pass

        search_roots = report_roots or [full_path]
        seen_paths = set()
        for search_root in search_roots:
            for root, dirs, files in os.walk(search_root):
                dirs[:] = [d for d in dirs if d.lower() not in skip_dirs]
                if should_skip_dir(root):
                    dirs[:] = []
                    continue
                for file in files:
                    if file.lower() == 'runner_summary.json':
                        continue
                    ext = os.path.splitext(file)[1].lower()
                    if ext not in report_extensions:
                        continue
                    file_path = os.path.join(root, file)
                    if file_path in seen_paths:
                        continue
                    seen_paths.add(file_path)
                    try:
                        mtime = os.path.getmtime(file_path)
                        if start_time is not None and mtime < start_time:
                            continue
                        if os.path.getsize(file_path) == 0:
                            continue
                        candidates.append((file_path, *path_priority(file_path, ext)))
                    except Exception:
                        continue

        candidates.sort(key=lambda item: (item[1], item[2]), reverse=True)
        return [item[0] for item in candidates]

    @classmethod
    def _suggest_commands(cls, language: str, framework: str, tool: str, manifests: dict) -> list:
        language = (language or "").lower()
        framework = (framework or "").lower()
        tool = (tool or "").lower()

        if manifests.get("package_json"):
            if tool == "playwright":
                return ['npm test', 'npm run report']
            return ['npm test']
        if manifests.get("pom_xml"):
            return ['mvn test']
        if manifests.get("build_gradle"):
            return ['gradle test']
        if manifests.get("dotnet_project"):
            return ['dotnet test --results-directory Results --logger "html;LogFileName=report.html"']
        if language == "python" or manifests.get("requirements_txt") or manifests.get("pyproject_toml") or manifests.get("has_python_files"):
            if framework in {"behave", "cucumber"} or manifests.get("has_feature_files"):
                return ['behave -f json -o Results/behave_report.json -f html -o Results/report.html -f pretty']
            return ['pytest']
        return []

    @staticmethod
    def _split_commands(commands_text: str) -> list:
        return [line.strip() for line in (commands_text or "").splitlines() if line.strip()]

    @staticmethod
    def _command_family(cmd: str) -> str:
        cmd_lower = (cmd or "").strip().lower()
        if not cmd_lower:
            return ""
        if cmd_lower.startswith(("npm ", "npx ", "yarn ", "pnpm ")):
            return "node"
        if cmd_lower.startswith("mvn "):
            return "maven"
        if cmd_lower.startswith(("gradle ", "./gradlew", "gradlew ")):
            return "gradle"
        if cmd_lower.startswith("dotnet "):
            return "dotnet"
        if cmd_lower == "behave" or cmd_lower.startswith("behave "):
            return "behave"
        if cmd_lower == "pytest" or cmd_lower.startswith("pytest "):
            return "pytest"
        if cmd_lower.startswith("python -m pytest") or cmd_lower.startswith("python3 -m pytest"):
            return "pytest"
        return "generic"

    @classmethod
    def _normalize_command_for_execution(cls, cmd: str) -> str:
        """Normalize commands so reports are generated consistently without changing suite scope."""
        cmd = (cmd or "").strip()
        family = cls._command_family(cmd)
        cmd_lower = cmd.lower()

        if family == "behave":
            has_formatter = any(token in cmd_lower for token in (" -f ", " --format ", " -o ", " --outfile "))
            if not has_formatter:
                cmd = f"{cmd} -f json -o Results/behave_report.json -f html -o Results/report.html -f pretty"
            return cmd

        if family == "dotnet":
            if '--logger' not in cmd_lower:
                cmd = f'{cmd} --logger "html;LogFileName=report.html"'
                cmd_lower = cmd.lower()
            if '--results-directory' not in cmd_lower:
                cmd = f'{cmd} --results-directory Results'
            return cmd

        return cmd

    @classmethod
    def _matches_legacy_default_commands(cls, commands: list, suggested_commands: list, tool: str) -> bool:
        normalized = [' '.join((cmd or '').split()).strip().lower() for cmd in commands if (cmd or '').strip()]
        suggested = [' '.join((cmd or '').split()).strip().lower() for cmd in suggested_commands if (cmd or '').strip()]
        tool = (tool or '').strip().lower()

        legacy_sets = [
            ['behave'],
            ['behave --tags=@smoke'],
            ['behave', 'behave --tags=@smoke'],
            ['behave -f html -o results/report.html -f pretty'],
            ['behave --tags=@smoke -f html -o results/report.html -f pretty'],
            ['behave -f pretty -o results/behave_report.txt -f html -o results/report.html'],
            ['behave --tags=@smoke -f pretty -o results/behave_report.txt -f html -o results/report.html'],
            ['behave -f json -o results/behave_report.json -f html -o results/report.html -f pretty'],
            ['behave --stop -f pretty -o results/behave_report.txt -f html -o results/report.html',
             'behave --tags=@smoke --stop -f pretty -o results/behave_report.txt -f html -o results/report.html'],
        ]

        if tool == 'playwright':
            legacy_sets.extend([
                ['npm test -- --tags "@smoke"', 'npm run report'],
                ["npm test -- --tags '@smoke'", 'npm run report'],
            ])
        if tool == 'selenium':
            legacy_sets.extend([
                ['dotnet test'],
            ])

        normalized_legacy_sets = [
            [' '.join(item.split()).strip().lower() for item in legacy]
            for legacy in legacy_sets
        ]
        return normalized == suggested or normalized in normalized_legacy_sets

    @classmethod
    def resolve_project_commands(cls, full_path: str, language: str, framework: str, tool: str, saved_commands: str = "") -> dict:
        manifests = cls._inspect_project_files(full_path)
        suggested_commands = cls._suggest_commands(language, framework, tool, manifests)
        saved_list = cls._split_commands(saved_commands)

        if saved_list:
            if cls._matches_legacy_default_commands(saved_list, suggested_commands, tool):
                return {
                    "commands": "\n".join(suggested_commands),
                    "source": "inferred",
                    "warning": "",
                    "suggestions": suggested_commands,
                }
            is_valid, validation_msg = cls._validate_custom_commands(saved_list, full_path, language, framework, tool)
            if is_valid:
                return {
                    "commands": "\n".join(saved_list),
                    "source": "saved",
                    "warning": "",
                    "suggestions": suggested_commands,
                }
            return {
                "commands": "\n".join(suggested_commands),
                "source": "inferred",
                "warning": validation_msg,
                "suggestions": suggested_commands,
            }

        return {
            "commands": "\n".join(suggested_commands),
            "source": "inferred",
            "warning": "",
            "suggestions": suggested_commands,
        }

    @classmethod
    def _validate_custom_commands(cls, commands: list, full_path: str, language: str, framework: str, tool: str):
        if not os.path.isdir(full_path):
            return False, f"[Validation] Project path does not exist: {full_path}"

        manifests = cls._inspect_project_files(full_path)
        suggestions = cls._suggest_commands(language, framework, tool, manifests)

        for cmd in commands:
            family = cls._command_family(cmd)
            if not family:
                continue
            if family == "node" and not manifests.get("package_json"):
                msg = f"[Validation] '{cmd}' requires a package.json in {full_path}, but this project does not have one."
            elif family == "maven" and not manifests.get("pom_xml"):
                msg = f"[Validation] '{cmd}' requires a pom.xml in {full_path}, but this project does not have one."
            elif family == "gradle" and not manifests.get("build_gradle"):
                msg = f"[Validation] '{cmd}' requires a build.gradle in {full_path}, but this project does not have one."
            elif family == "dotnet" and not manifests.get("dotnet_project"):
                msg = f"[Validation] '{cmd}' requires a .csproj project in {full_path}, but none was found."
            elif family == "behave" and not (manifests.get("has_feature_files") or manifests.get("has_python_files")):
                msg = f"[Validation] '{cmd}' looks like a Behave command, but this project does not look like a Python BDD project."
            elif family == "pytest" and not (manifests.get("has_python_files") or manifests.get("requirements_txt") or manifests.get("pyproject_toml")):
                msg = f"[Validation] '{cmd}' looks like a pytest command, but this project does not look like a Python test project."
            else:
                msg = ""

            if msg:
                if suggestions:
                    msg += f" Try: {' ; '.join(suggestions)}"
                return False, msg

        if any(cls._command_family(cmd) in {"behave", "pytest"} for cmd in commands):
            syntax_issue = cls._validate_python_project(full_path)
            if syntax_issue:
                return False, syntax_issue

        return True, ""

    @classmethod
    def _validate_python_project(cls, full_path: str) -> str:
        """Compile project Python sources before launching a test framework."""
        import ast
        for root, dirs, files in os.walk(full_path):
            dirs[:] = [d for d in dirs if d.lower() not in cls.REPORT_SKIP_DIRS]
            for filename in files:
                if not filename.endswith('.py'):
                    continue
                source_path = os.path.join(root, filename)
                try:
                    with open(source_path, 'r', encoding='utf-8') as source_file:
                        ast.parse(source_file.read(), filename=source_path)
                except SyntaxError as exc:
                    rel_path = os.path.relpath(source_path, full_path)
                    return f"[Validation] Python syntax error in {rel_path}, line {exc.lineno}: {exc.msg}. Fix or regenerate this file before running the suite."
                except OSError as exc:
                    return f"[Validation] Could not read {source_path}: {exc}"
        return ""

    @classmethod
    def _write_runner_report(cls, full_path: str, success: bool, log_text: str) -> str:
        """Write a fresh diagnostic report when the framework produced no usable report."""
        results_dir = os.path.join(full_path, 'Results')
        os.makedirs(results_dir, exist_ok=True)
        report_path = os.path.join(results_dir, 'runner_report.html')
        status = 'Passed' if success else 'Failed'
        color = '#22c55e' if success else '#ef4444'
        generated_at = datetime.now().astimezone().isoformat(timespec='seconds')
        content = f'''<!doctype html><html><head><meta charset="utf-8"><title>Script Runner - {status}</title></head>
<body style="font-family:system-ui;background:#0b1220;color:#e8eefc;padding:32px"><main style="max-width:1100px;margin:auto"><h1>Execution {status}</h1><p style="color:{color};font-weight:700">Status: {status}</p><p>Generated: {html.escape(generated_at)}</p><pre style="white-space:pre-wrap;background:#131c2f;padding:20px;border-radius:10px;overflow:auto">{html.escape(log_text or 'No command output was captured.')}</pre></main></body></html>'''
        with open(report_path, 'w', encoding='utf-8') as report_file:
            report_file.write(content)
        return report_path

    @classmethod
    def _ensure_behave_html_report(cls, full_path: str) -> None:
        """Create usable HTML when Behave's HTML formatter leaves an empty file."""
        results_dir = os.path.join(full_path, 'Results')
        json_path = os.path.join(results_dir, 'behave_report.json')
        html_path = os.path.join(results_dir, 'report.html')
        if not os.path.isfile(json_path) or os.path.getsize(json_path) == 0:
            return
        if os.path.isfile(html_path) and os.path.getsize(html_path) > 0:
            return
        try:
            with open(json_path, 'r', encoding='utf-8') as json_file:
                features = json.load(json_file)
        except (OSError, ValueError):
            return
        rows, passed, failed, skipped = [], 0, 0, 0
        for feature in features if isinstance(features, list) else []:
            feature_name = feature.get('name') or 'Unnamed feature'
            for scenario in feature.get('elements') or []:
                statuses = [((step.get('result') or {}).get('status') or 'skipped').lower() for step in scenario.get('steps') or []]
                status = 'failed' if 'failed' in statuses else 'skipped' if statuses and all(s == 'skipped' for s in statuses) else 'passed'
                if status == 'failed': failed += 1
                elif status == 'skipped': skipped += 1
                else: passed += 1
                rows.append(f'<tr><td>{html.escape(feature_name)}</td><td>{html.escape(scenario.get("name") or "Unnamed scenario")}</td><td class="{status}">{status.title()}</td></tr>')
        generated_at = datetime.now().astimezone().isoformat(timespec='seconds')
        document = f'''<!doctype html><html><head><meta charset="utf-8"><title>Behave Test Report</title><style>body{{font-family:system-ui;margin:32px;background:#f5f7fb;color:#172033}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{padding:12px;border:1px solid #dce3ef;text-align:left}}.passed{{color:#16803c}}.failed{{color:#c62828}}.skipped{{color:#8a6500}}</style></head><body><h1>Behave Test Report</h1><p>Generated: {html.escape(generated_at)}</p><p><strong>Passed:</strong> {passed} &nbsp; <strong>Failed:</strong> {failed} &nbsp; <strong>Skipped:</strong> {skipped}</p><table><thead><tr><th>Feature</th><th>Scenario</th><th>Status</th></tr></thead><tbody>{''.join(rows)}</tbody></table></body></html>'''
        try:
            with open(html_path, 'w', encoding='utf-8') as html_file:
                html_file.write(document)
        except OSError:
            pass

    @classmethod
    def _write_behave_execution_summary(cls, full_path: str, log_text: str) -> None:
        """Persist Behave console totals, including scenarios excluded by tag filters."""
        summary = {}
        patterns = {
            'features': r'(\d+)\s+features?\s+passed,\s*(\d+)\s+failed,\s*(\d+)\s+skipped',
            'scenarios': r'(\d+)\s+scenarios?\s+passed,\s*(\d+)\s+failed,\s*(\d+)\s+skipped',
            'steps': r'(\d+)\s+steps?\s+passed,\s*(\d+)\s+failed,\s*(\d+)\s+skipped,\s*(\d+)\s+undefined',
        }
        for key, pattern in patterns.items():
            matches = re.findall(pattern, log_text or '', flags=re.IGNORECASE)
            if matches:
                values = [int(value) for value in matches[-1]]
                summary[key] = {'passed': values[0], 'failed': values[1], 'skipped': values[2], 'undefined': values[3] if len(values) > 3 else 0}
        if not summary:
            return
        summary['generated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
        results_dir = os.path.join(full_path, 'Results')
        os.makedirs(results_dir, exist_ok=True)
        try:
            with open(os.path.join(results_dir, 'runner_summary.json'), 'w', encoding='utf-8') as summary_file:
                json.dump(summary, summary_file, indent=2)
        except OSError:
            pass

    @classmethod
    def _current_report_url(cls, full_path: str, start_time: float, success: bool, log_text: str) -> str:
        cls._write_behave_execution_summary(full_path, log_text)
        cls._ensure_behave_html_report(full_path)
        candidates = cls._iter_report_candidates(full_path, start_time=start_time)
        report_file = candidates[0] if candidates else cls._write_runner_report(full_path, success, log_text)
        return f"/api/script-runner/report?path={quote(report_file, safe='')}&run={int(time.time() * 1000)}"

    @staticmethod
    def _call_ai_sync_json(prompt: str) -> dict:
        try:
            import asyncio
            import json
            try:
                from api.backend import call_ai
            except ImportError:
                try:
                    from ScriptGenerator.api.backend import call_ai
                except ImportError:
                    from BIG_QA_Solution.ScriptGenerator.api.backend import call_ai
            
            # call_ai respects the global AI_TOOL environment variable and routes logic correctly
            result_str = asyncio.run(call_ai(prompt, expect_json=True))
            if not result_str:
                return {}
                
            text = result_str.replace('```json', '').replace('```', '').strip()
            return json.loads(text)
        except Exception as e:
            print(f"AI call failed: {e}")
            return {}

    @staticmethod
    def _load_healing_prompts():
        """Import the healing prompts, adding BIG_QA_Solution to sys.path if needed."""
        parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        from prompts.script_runner_prompts import (
            get_diagnose_locator_error_prompt,
            get_heal_locator_prompt,
        )
        return get_diagnose_locator_error_prompt, get_heal_locator_prompt

    @classmethod
    def _resolve_page_object_target(cls, full_path: str, file_to_fix: str):
        """
        Resolve the file the AI nominated, accepting it only if it is a Page Object.

        The diagnosis prompt is already told to return Page Objects only, but the
        answer is model output - this is the guard that actually keeps step
        definitions, feature files, hooks and config out of the healing path.

        Returns (absolute_path, "") when healing may proceed, otherwise
        ("", reason) explaining why the file is off-limits.
        """
        rel_path = (file_to_fix or '').strip().strip('"').strip("'").replace('\\', '/')
        if not rel_path:
            return "", "no Page Object file was identified"

        project_root = os.path.abspath(full_path)
        candidate = os.path.abspath(rel_path if os.path.isabs(rel_path) else os.path.join(project_root, rel_path))
        try:
            # Never let a hallucinated path escape the project under test.
            if os.path.commonpath([candidate, project_root]) != project_root:
                raise ValueError
        except ValueError:
            return "", f"'{rel_path}' is outside the project directory"

        display = os.path.relpath(candidate, project_root).replace('\\', '/')
        segments = display.lower().split('/')
        directories, filename = segments[:-1], segments[-1]
        stem, ext = os.path.splitext(filename)

        if ext not in cls.PAGE_OBJECT_EXTENSIONS:
            return "", f"'{display}' is not a Page Object source file"
        if any(directory in cls.NON_HEALABLE_DIR_NAMES for directory in directories):
            return "", f"'{display}' is a step definition/feature/support file, not a Page Object"
        if any(marker in stem for marker in cls.NON_HEALABLE_FILE_MARKERS):
            return "", f"'{display}' is a step definition/hook/config file, not a Page Object"
        if not (any(directory in cls.PAGE_OBJECT_DIR_NAMES for directory in directories) or 'page' in stem):
            return "", f"'{display}' does not look like a Page Object file"
        if not os.path.isfile(candidate):
            return "", f"'{display}' does not exist in the project"
        return candidate, ""

    @staticmethod
    def _python_syntax_error(display: str, source: str) -> str:
        """Return a message if healed Python source would not compile, else ''."""
        if not display.endswith('.py'):
            return ""
        import ast
        try:
            ast.parse(source, filename=display)
        except SyntaxError as exc:
            return f"line {exc.lineno}: {exc.msg}"
        return ""

    @classmethod
    def _attempt_locator_heal(cls, full_path: str, cmd: str, cmd_output: str, attempt: int):
        """
        Decide whether a failed command can be self-healed, and heal it if so.

        Healing is limited to element locator failures, and the only file ever
        modified is the Page Object that declares the failing locator. Every
        other failure - assertion, syntax, import, undefined step, application
        or data error - is reported back as unhealable so the caller fails the
        test rather than rewriting the suite.

        Returns (outcome, messages):
          "HEALED"        - a Page Object locator was rewritten; re-run the command
          "COMMAND_ERROR" - the command/environment is wrong; no file was touched
          "FAILED"        - not healable; fail the test
        `messages` are console lines for the caller to stream.
        """
        get_diagnose_locator_error_prompt, get_heal_locator_prompt = cls._load_healing_prompts()
        messages = []

        diagnosis = cls._call_ai_sync_json(get_diagnose_locator_error_prompt(cmd, cmd_output))
        category = (diagnosis.get("error_category") or "OTHER").strip().upper()
        reason = (diagnosis.get("reason") or "").strip()
        reason_suffix = f" ({reason})" if reason else ""

        if category == "COMMAND":
            messages.append(f"[Self-Healing] Failure is a command/environment problem, not an element "
                            f"locator{reason_suffix}. No project file will be modified.")
            return "COMMAND_ERROR", messages

        if category != "LOCATOR":
            messages.append(f"[Self-Healing] Failure is not an element locator issue{reason_suffix}. "
                            f"Self-healing only repairs locators in Page Object files - failing the test.")
            return "FAILED", messages

        file_target, rejection = cls._resolve_page_object_target(full_path, diagnosis.get("file_to_fix", ""))
        if rejection:
            messages.append(f"[Self-Healing] Cannot heal: {rejection}. Only Page Object files are "
                            f"modified - failing the test.")
            return "FAILED", messages

        display = os.path.relpath(file_target, os.path.abspath(full_path)).replace('\\', '/')
        failed_locator = (diagnosis.get("failed_locator") or "").strip()
        messages.append(f"[Self-Healing] Locator failure in Page Object {display}: "
                        f"'{failed_locator}'{reason_suffix}.")

        try:
            with open(file_target, 'r', encoding='utf-8') as page_object_file:
                original_content = page_object_file.read()
        except OSError as read_err:
            messages.append(f"[Self-Healing] Could not read {display}: {read_err} - failing the test.")
            return "FAILED", messages

        fix_response = cls._call_ai_sync_json(
            get_heal_locator_prompt(display, original_content, failed_locator, cmd_output)
        )
        healed_content = fix_response.get("healed_content", "")
        if not healed_content or healed_content.strip() == original_content.strip():
            messages.append("[Self-Healing] AI did not return a usable locator fix - failing the test.")
            return "FAILED", messages

        # Validate before writing: a half-rewritten Page Object would break every
        # scenario that uses it, including ones that were passing.
        syntax_error = cls._python_syntax_error(display, healed_content)
        if syntax_error:
            messages.append(f"[Self-Healing] Healed content for {display} is not valid Python "
                            f"({syntax_error}). Left the file untouched and failing the test.")
            return "FAILED", messages

        backup_target = f"{file_target}.bak.healing.{attempt}"
        try:
            shutil.copy2(file_target, backup_target)
            with open(file_target, 'w', encoding='utf-8') as page_object_file:
                page_object_file.write(healed_content)
        except OSError as write_err:
            messages.append(f"[Self-Healing] Could not apply the locator fix to {display}: "
                            f"{write_err} - failing the test.")
            return "FAILED", messages

        explanation = (fix_response.get("explanation") or "").strip()
        messages.append(f"[Self-Healing] Updated locator in {display} (backup: "
                        f"{display}.bak.healing.{attempt}). {explanation} Retrying execution...")
        return "HEALED", messages

    @staticmethod
    def _resolve_full_path(meta: dict) -> str:
        project_path = meta.get('path', '') or meta.get('project_path', '')
        project_name = meta.get('name', '') or meta.get('project_name', '')
        return os.path.join(project_path, project_name) if project_name not in project_path else project_path

    @classmethod
    def execute_with_streaming(cls, meta: dict, env: str, browser: str, tags: str, custom_commands: str = ""):
        """
        Public streaming entry point for "Launch Execution".

        Wraps the actual run so browser performance monitoring starts before
        the first command and is ALWAYS finalized afterwards:
          - normal completion  -> stopped just before the final result event,
                                  so the report exists by the time the UI is
                                  told the run is over
          - user force-stop    -> the /stop endpoint finalizes it directly
                                  (stop() is idempotent, so the unwinding
                                  stream calling it again is harmless)
          - client disconnect  -> Flask closes this generator and the finally
                                  block finalizes it

        The finally block deliberately does not yield: a generator being
        closed early receives GeneratorExit and must not produce more output.
        """
        perf = WebPerfSession(cls._resolve_full_path(meta), time.time())
        start_msg = perf.start()
        if start_msg:
            yield f"event: progress\ndata: {json.dumps({'msg': start_msg, 'type': 'system'})}\n\n"

        try:
            for chunk in cls._stream_execution(meta, env, browser, tags, custom_commands):
                # Interleave the monitor's own diagnostics (browser detected,
                # CDP attached, attach failed, ...) with the test output.
                yield from cls._perf_log_events(perf)
                if chunk.startswith("event: result"):
                    yield from cls._finalize_perf_and_result(perf, chunk)
                else:
                    yield chunk
        finally:
            perf.stop(timeout=10.0)

    @staticmethod
    def _perf_log_events(perf: "WebPerfSession"):
        for line in perf.drain_logs():
            yield f"event: progress\ndata: {json.dumps({'msg': line, 'type': 'system'})}\n\n"

    @classmethod
    def _finalize_perf_and_result(cls, perf: "WebPerfSession", result_chunk: str):
        """Stop monitoring, then re-emit the run's result event carrying the
        performance report URL alongside the functional report URL."""
        # `or perf.summary` covers the abort path: the /stop endpoint already
        # finalized this same session object, so stop() is a no-op here but the
        # summary and report path it produced are still worth reporting.
        summary = perf.stop() or perf.summary
        yield from cls._perf_log_events(perf)  # anything logged during finalization
        if summary:
            yield f"event: progress\ndata: {json.dumps({'msg': summary, 'type': 'system'})}\n\n"
        try:
            payload = json.loads(result_chunk.split("data: ", 1)[1].strip())
        except (IndexError, ValueError):
            yield result_chunk  # malformed - pass through untouched
            return
        payload["perf_report_url"] = perf.report_url
        yield f"event: result\ndata: {json.dumps(payload)}\n\n"

    @classmethod
    def _stream_execution(cls, meta: dict, env: str, browser: str, tags: str, custom_commands: str = ""):
        full_path = cls._resolve_full_path(meta)

        language = meta.get('language') or meta.get('lang') or meta.get('project_lang', '')
        framework = meta.get('framework') or meta.get('fw') or meta.get('project_fw', '')
        tool = meta.get('tool') or meta.get('project_tool', '')
        
        start_time = time.time()
        print(f"DEBUG: Starting streaming in {full_path}")
        
        # Mode A: Custom Sequential Commands
        if custom_commands.strip():
            commands = [cls._normalize_command_for_execution(c) for c in custom_commands.split('\n') if c.strip()]
            execution_log = ""
            print(f"DEBUG: Found {len(commands)} custom commands")
            is_valid, validation_msg = cls._validate_custom_commands(commands, full_path, language, framework, tool)
            if not is_valid:
                yield f"event: progress\ndata: {json.dumps({'msg': validation_msg, 'type': 'step_fail', 'step': 1})}\n\n"
                report_url = cls._current_report_url(full_path, start_time, False, validation_msg)
                yield f"event: result\ndata: {json.dumps({'status': 'error', 'report_url': report_url})}\n\n"
                return
            yield f"event: progress\ndata: {json.dumps({'msg': f'[Sequential Mode] Starting {len(commands)} commands...', 'type': 'system'})}\n\n"
            
            success = True
            for i, cmd in enumerate(commands):
                attempt = 0
                max_retries = cls.MAX_HEALING_RETRIES
                cmd_success = False
                
                while attempt <= max_retries and not cmd_success:
                    if attempt > 0:
                        print(f"DEBUG: Re-running step {i+1} (Healing Attempt {attempt}/{max_retries}): {cmd}")
                        yield f"event: progress\ndata: {json.dumps({'msg': f'[Self-Healing Attempt {attempt}/{max_retries}] Re-running: {cmd}', 'type': 'step_start', 'step': i+1})}\n\n"
                    else:
                        print(f"DEBUG: Running step {i+1}: {cmd}")
                        yield f"event: progress\ndata: {json.dumps({'msg': f'[Step {i+1}/{len(commands)}]: {cmd}', 'type': 'step_start', 'step': i+1})}\n\n"
                    
                    # Automatically detect and use venv if present.
                    # Start from _runtime_env_with_ca() so proxy CA certs are preserved.
                    env_vars = _runtime_env_for_command(cmd)
                    venv_bin = os.path.join(full_path, "venv", "Scripts" if os.name == 'nt' else "bin")
                    if os.path.exists(venv_bin):
                        env_vars["PATH"] = venv_bin + os.pathsep + env_vars.get("PATH", "")
                    
                    is_windows = os.name == 'nt'
                    if is_windows:
                        process = subprocess.Popen(cmd, cwd=full_path, shell=True, env=env_vars, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True, encoding='utf-8', errors='replace')
                    else:
                        import shlex
                        process = subprocess.Popen(shlex.split(cmd), cwd=full_path, shell=False, env=env_vars, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True, encoding='utf-8', errors='replace')
                    active_processes[process.pid] = process
                    
                    cmd_output = ""
                    for line in process.stdout:
                        cmd_output += line
                        yield f"event: log\ndata: {json.dumps({'msg': line.rstrip()})}\n\n"
                    
                    process.stdout.close()
                    return_code = process.wait()
                    execution_log += f"\n[Command] {cmd}\n{cmd_output}\n[Exit Code] {return_code}\n"
                    
                    if process.pid in active_processes:
                        del active_processes[process.pid]
                    
                    if return_code == 0:
                        cmd_success = True
                        yield f"event: progress\ndata: {json.dumps({'msg': f'[Success] Step {i+1} completed', 'type': 'step_pass', 'step': i+1})}\n\n"
                    else:
                        # Check for self-healing possibilities if we haven't exhausted retries
                        if attempt < max_retries:
                            yield f"event: progress\ndata: {json.dumps({'msg': '[Self-Healing] Command failed. Diagnosing logs for element locator issues...', 'type': 'system'})}\n\n"

                            outcome, heal_messages = cls._attempt_locator_heal(full_path, cmd, cmd_output, attempt)
                            for heal_msg in heal_messages:
                                execution_log += f"\n{heal_msg}\n"
                                yield f"event: progress\ndata: {json.dumps({'msg': heal_msg, 'type': 'system'})}\n\n"

                            if outcome == "HEALED":
                                attempt += 1
                                continue

                            # Anything other than a healed Page Object locator is a real
                            # failure: report it as such instead of retrying blindly.
                            yield f"event: progress\ndata: {json.dumps({'msg': f'[Error] Command failed with exit code {return_code}', 'type': 'step_fail', 'step': i+1})}\n\n"
                            success = False
                            break
                        else:
                            yield f"event: progress\ndata: {json.dumps({'msg': f'[Error] Command failed with exit code {return_code} after all retries.', 'type': 'step_fail', 'step': i+1})}\n\n"
                            success = False
                            break
                
                if not cmd_success:
                    success = False
                    break

            # Finalize
            report_url = cls._current_report_url(full_path, start_time, success, execution_log)
            yield f"event: result\ndata: {json.dumps({'status': 'success' if success else 'error', 'report_url': report_url})}\n\n"
            return

        # Mode B: AI Generation (Simplified for streaming)
        yield f"event: progress\ndata: {json.dumps({'msg': '[AI Mode] Analyzing & generating execution command...', 'type': 'system'})}\n\n"
        # ... existing AI logic simplified or wrapped ...
        # (For now, let's just implement sequential streaming as it's the primary request)

        # Mode B: AI Generation with Self-Healing (Existing logic)
        # Step 1: AI generates command
        parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        from prompts.script_runner_prompts import get_execution_command_prompt, get_correct_command_prompt

        prompt = get_execution_command_prompt(
            tool=tool,
            language=language,
            framework=framework,
            env=env,
            browser=browser,
            tags=tags
        )
        
        ai_resp = cls._call_ai_sync_json(prompt)
        cmd = ai_resp.get("command", "")
        
        if not cmd:
            # Fallback if AI fails
            if language == 'Python':
                cmd = "pytest tests/ --html=Results/report.html"
            elif language in ['Typescript', 'JavaScript', 'TypeScript']:
                # Run the package.json script so npm resolves the project-local executable.
                cmd = "npm test"
            else:
                cmd = "echo 'Unsupported'"

        # Step 2: Execution Loop
        max_retries = 2
        attempts = 0
        success = False
        output_log = ""
        
        while attempts <= max_retries and not success:
            try:
                print(f"Running command: {cmd} in {full_path}")
                yield f"event: progress\ndata: {json.dumps({'msg': f'[Attempt {attempts+1}/{max_retries+1}] Running: {cmd}', 'type': 'step_start', 'step': attempts+1})}\n\n"
                
                # Automatically detect and use venv if present.
                # Start from _runtime_env_with_ca() (not a bare os.environ copy) so a test run
                # behind a corporate proxy inherits the same TLS/CA + proxy settings as install.
                env_vars = _runtime_env_for_command(cmd)
                venv_bin = os.path.join(full_path, "venv", "Scripts" if os.name == 'nt' else "bin")
                if os.path.exists(venv_bin):
                    env_vars["PATH"] = venv_bin + os.pathsep + env_vars.get("PATH", "")
                
                is_windows = os.name == 'nt'
                if is_windows:
                    process = subprocess.Popen(cmd, cwd=full_path, shell=True, env=env_vars, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True, encoding='utf-8', errors='replace')
                else:
                    import shlex
                    process = subprocess.Popen(shlex.split(cmd), cwd=full_path, shell=False, env=env_vars, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True, encoding='utf-8', errors='replace')
                
                active_processes[process.pid] = process
                
                cmd_output = ""
                for line in process.stdout:
                    cmd_output += line
                    yield f"event: log\ndata: {json.dumps({'msg': line.rstrip()})}\n\n"
                
                process.stdout.close()
                return_code = process.wait()
                if process.pid in active_processes:
                    del active_processes[process.pid]
                
                output_log += f"\n\n[Attempt {attempts+1} Command]: {cmd}\n"
                output_log += cmd_output
                
                if return_code == 0:
                    success = True
                    yield f"event: progress\ndata: {json.dumps({'msg': f'[Success] Attempt {attempts+1} completed successfully', 'type': 'step_pass', 'step': attempts+1})}\n\n"
                else:
                    yield f"event: progress\ndata: {json.dumps({'msg': f'[Fail] Attempt {attempts+1} failed with exit code {return_code}', 'type': 'step_fail', 'step': attempts+1})}\n\n"
                    
                    if attempts < max_retries:
                        yield f"event: progress\ndata: {json.dumps({'msg': '[Self-Healing] Diagnosing error...', 'type': 'system'})}\n\n"
                        error_output = cmd_output
                        outcome, heal_messages = cls._attempt_locator_heal(full_path, cmd, error_output, attempts)
                        for heal_msg in heal_messages:
                            output_log += f"\n{heal_msg}\n"
                            yield f"event: progress\ndata: {json.dumps({'msg': heal_msg, 'type': 'system'})}\n\n"

                        if outcome == "FAILED":
                            # Not a Page Object locator failure - do not touch the suite.
                            break

                        if outcome == "COMMAND_ERROR":
                            # The command this mode generated may simply be wrong. Correcting
                            # it changes no project file, so it stays allowed.
                            prompt2 = get_correct_command_prompt(
                                cmd=cmd,
                                error_output=error_output,
                                tool=tool,
                                framework=framework
                            )
                            ai_correction = cls._call_ai_sync_json(prompt2)
                            new_cmd = ai_correction.get("command", "")
                            if not new_cmd:
                                break
                            cmd = new_cmd
                            log_msg = f"[Self-Healing] AI suggested corrected command: {cmd}"
                            output_log += f"\n{log_msg}\n"
                            yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
                        # "HEALED" keeps the same command and re-runs it.
                    else:
                        break
            except Exception as e:
                err_msg = f"Error executing: {e}"
                output_log += f"\n{err_msg}"
                yield f"event: progress\ndata: {json.dumps({'msg': err_msg, 'type': 'system'})}\n\n"
                break
            attempts += 1

        # Step 2.5: Java Fallback logic if the primary execution failed
        if not success and language and language.lower() == 'java':
            yield f"event: progress\ndata: {json.dumps({'msg': '[Fallback] Execution failed. Scanning for Java Test Runners...', 'type': 'system'})}\n\n"
            
            import re
            java_files = []
            for root, dirs, files in os.walk(full_path):
                # Skip build, target, .git, etc.
                if any(p in root.replace('\\', '/').split('/') for p in ['.git', '.idea', '.venv', 'node_modules', 'target', 'bin', 'obj']):
                    continue
                for file in files:
                    if file.endswith('.java'):
                        java_files.append(os.path.join(root, file))
            
            runners = []
            for jf in java_files:
                try:
                    with open(jf, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    
                    content_clean = re.sub(r'//.*|/\*.*?\*/', '', content, flags=re.DOTALL)
                    has_main = re.search(r'public\s+static\s+void\s+main\b', content_clean) is not None
                    
                    package_match = re.search(r'package\s+([\w\.]+)\s*;', content_clean)
                    package_name = package_match.group(1).strip() if package_match else None
                    class_name = os.path.splitext(os.path.basename(jf))[0]
                    full_class_name = f"{package_name}.{class_name}" if package_name else class_name
                    
                    is_runnable_junit = any(annot in content_clean for annot in ['@Suite', '@RunWith', '@Test', '@org.junit'])
                    is_runner_by_name = 'runner' in class_name.lower() or 'test' in class_name.lower() or 'suite' in class_name.lower()
                    
                    if has_main or is_runnable_junit or is_runner_by_name:
                        runners.append({
                            "filepath": jf,
                            "has_main": has_main,
                            "class_name": class_name,
                            "full_class_name": full_class_name,
                            "is_runnable_junit": is_runnable_junit,
                            "is_runner_by_name": is_runner_by_name
                        })
                except Exception as e:
                    print(f"Error parsing Java file {jf}: {e}")
            
            # Prioritize:
            # 1. Has main method
            # 2. Runnable JUnit suite/test
            # 3. Named Runner/Test
            runners.sort(key=lambda r: (r['has_main'], r['is_runnable_junit'], r['is_runner_by_name']), reverse=True)
            
            if runners:
                chosen = runners[0]
                has_maven = os.path.exists(os.path.join(full_path, "pom.xml"))
                has_gradle = os.path.exists(os.path.join(full_path, "build.gradle"))
                
                fallback_cmd = ""
                if chosen['has_main']:
                    msg_text = f"[Fallback] Found main method in runner class: {chosen['full_class_name']}"
                    yield f"event: progress\ndata: {json.dumps({'msg': msg_text, 'type': 'system'})}\n\n"
                    if has_gradle:
                        fallback_cmd = f"gradle execute -PmainClass={chosen['full_class_name']}"
                    else:
                        fallback_cmd = f"mvn test-compile exec:java -Dexec.classpathScope=\"test\" -Dexec.mainClass=\"{chosen['full_class_name']}\""
                else:
                    msg_text = f"[Fallback] Found runnable runner class: {chosen['full_class_name']}"
                    yield f"event: progress\ndata: {json.dumps({'msg': msg_text, 'type': 'system'})}\n\n"
                    if has_gradle:
                        fallback_cmd = f"gradle test --tests {chosen['full_class_name']}"
                    else:
                        fallback_cmd = f"mvn test -Dtest={chosen['class_name']}"
                
                if fallback_cmd:
                    yield f"event: progress\ndata: {json.dumps({'msg': f'[Fallback] Running command: {fallback_cmd}', 'type': 'step_start', 'step': attempts+1})}\n\n"
                    try:
                        # Same TLS/CA + proxy-aware base env as the primary run path above.
                        env_vars = _runtime_env_for_command(fallback_cmd)
                        is_windows = os.name == 'nt'
                        if is_windows:
                            process = subprocess.Popen(fallback_cmd, cwd=full_path, shell=True, env=env_vars, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True, encoding='utf-8', errors='replace')
                        else:
                            import shlex
                            process = subprocess.Popen(shlex.split(fallback_cmd), cwd=full_path, shell=False, env=env_vars, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True, encoding='utf-8', errors='replace')
                        
                        active_processes[process.pid] = process
                        
                        cmd_output = ""
                        for line in process.stdout:
                            cmd_output += line
                            yield f"event: log\ndata: {json.dumps({'msg': line.rstrip()})}\n\n"
                        
                        process.stdout.close()
                        return_code = process.wait()
                        if process.pid in active_processes:
                            del active_processes[process.pid]
                        
                        output_log += f"\n\n[Fallback Command]: {fallback_cmd}\n"
                        output_log += cmd_output
                        
                        if return_code == 0:
                            success = True
                            yield f"event: progress\ndata: {json.dumps({'msg': '[Success] Fallback execution completed successfully', 'type': 'step_pass', 'step': attempts+1})}\n\n"
                        else:
                            yield f"event: progress\ndata: {json.dumps({'msg': f'[Fail] Fallback execution failed with exit code {return_code}', 'type': 'step_fail', 'step': attempts+1})}\n\n"
                    except Exception as e:
                        err_msg = f"Error executing fallback: {e}"
                        output_log += f"\n{err_msg}"
                        yield f"event: progress\ndata: {json.dumps({'msg': err_msg, 'type': 'system'})}\n\n"
            else:
                yield f"event: progress\ndata: {json.dumps({'msg': '[Fallback] No runner classes found in the project.', 'type': 'system'})}\n\n"

        # Step 3: Finalize
        report_url = cls._current_report_url(full_path, start_time, success, output_log)
        yield f"event: result\ndata: {json.dumps({'status': 'success' if success else 'error', 'report_url': report_url})}\n\n"
        return

    @classmethod
    def _open_latest_report(cls, full_path, start_time):
        candidates = cls._iter_report_candidates(full_path, start_time=start_time)
        if not candidates:
            return None
        latest_report = candidates[0]
        try:
            is_windows = os.name == 'nt'
            if is_windows:
                os.startfile(latest_report)
            else:
                import subprocess
                if sys.platform == 'darwin':
                    subprocess.Popen(['open', latest_report])
                else:
                    subprocess.Popen(['xdg-open', latest_report])
            return latest_report
        except Exception as e:
            print(f"Failed to open report file {latest_report}: {e}")
            return None

    @classmethod
    def _get_latest_report(cls, full_path):
        report_url = ""
        try:
            report_files = cls._iter_report_candidates(full_path)
            if report_files:
                report_file = report_files[0]
                report_url = f"/api/script-runner/report?path={quote(report_file, safe='')}"
        except Exception: pass
        return report_url

    @classmethod
    def _finalize_execution(cls, success, output_log, full_path):
        report_url = cls._get_latest_report(full_path)

        return {
            "status": "success" if success else "error",
            "log": output_log,
            "report_url": report_url
        }

    @staticmethod
    def execute_cmd(cmd: str, project_path: str) -> dict:
        if not cmd:
            return {"output": "No command provided."}
            
        try:
            env_vars = _runtime_env_for_command(cmd)
            venv_bin = os.path.join(project_path, "venv", "Scripts" if os.name == 'nt' else "bin")
            if os.path.exists(venv_bin):
                env_vars["PATH"] = venv_bin + os.pathsep + env_vars.get("PATH", "")
            is_windows = os.name == 'nt'
            if is_windows:
                result = subprocess.run(cmd, cwd=project_path, shell=True, env=env_vars, capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
            else:
                import shlex
                result = subprocess.run(shlex.split(cmd), cwd=project_path, shell=False, env=env_vars, capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
            output = result.stdout + result.stderr
            return {"output": output}
        except Exception as e:
            return {"output": str(e)}
