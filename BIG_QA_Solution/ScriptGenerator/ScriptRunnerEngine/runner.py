import os
import sys
import json
import glob
import shutil
import subprocess
import signal
import time
import html
import re
from datetime import datetime
from urllib.parse import quote

active_processes = {} # pid -> process object


def _runtime_env_with_ca():
    """
    Base environment for test-run subprocesses.

    Reuses EnvironmentSetup._download_env() so running tests (and the runtime
    `npx playwright install` that the generated TypeScript hooks.ts triggers in its
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
                    return (
                        f"[Validation] Python syntax error in {rel_path}, line {exc.lineno}: "
                        f"{exc.msg}. Fix or regenerate this file before running the suite."
                    )
                except OSError as exc:
                    return f"[Validation] Could not read {source_path}: {exc}"
        return ""

    @classmethod
    def _write_runner_report(cls, full_path: str, success: bool, log_text: str) -> str:
        """Write a fresh fallback report when the test framework produced no usable report."""
        results_dir = os.path.join(full_path, 'Results')
        os.makedirs(results_dir, exist_ok=True)
        report_path = os.path.join(results_dir, 'runner_report.html')
        status = 'Passed' if success else 'Failed'
        color = '#22c55e' if success else '#ef4444'
        generated_at = datetime.now().astimezone().isoformat(timespec='seconds')
        content = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Script Runner - {status}</title></head>
<body style="font-family:system-ui;background:#0b1220;color:#e8eefc;padding:32px">
<main style="max-width:1100px;margin:auto"><h1>Execution {status}</h1>
<p style="color:{color};font-weight:700">Status: {status}</p>
<p>Generated: {html.escape(generated_at)}</p>
<pre style="white-space:pre-wrap;background:#131c2f;padding:20px;border-radius:10px;overflow:auto">{html.escape(log_text or 'No command output was captured.')}</pre>
</main></body></html>"""
        with open(report_path, 'w', encoding='utf-8') as report_file:
            report_file.write(content)
        return report_path

    @classmethod
    def _ensure_behave_html_report(cls, full_path: str) -> None:
        """Create a usable HTML report when Behave's HTML formatter leaves an empty file."""
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

        rows = []
        passed = failed = skipped = 0
        for feature in features if isinstance(features, list) else []:
            feature_name = feature.get('name') or 'Unnamed feature'
            for scenario in feature.get('elements') or []:
                statuses = [
                    ((step.get('result') or {}).get('status') or 'skipped').lower()
                    for step in scenario.get('steps') or []
                ]
                status = 'failed' if 'failed' in statuses else 'skipped' if statuses and all(s == 'skipped' for s in statuses) else 'passed'
                if status == 'failed':
                    failed += 1
                elif status == 'skipped':
                    skipped += 1
                else:
                    passed += 1
                rows.append(
                    f"<tr><td>{html.escape(feature_name)}</td><td>{html.escape(scenario.get('name') or 'Unnamed scenario')}</td>"
                    f"<td class=\"{status}\">{status.title()}</td></tr>"
                )

        generated_at = datetime.now().astimezone().isoformat(timespec='seconds')
        document = f"""<!doctype html><html><head><meta charset="utf-8"><title>Behave Test Report</title>
<style>body{{font-family:system-ui;margin:32px;background:#f5f7fb;color:#172033}}table{{width:100%;border-collapse:collapse;background:white}}th,td{{padding:12px;border:1px solid #dce3ef;text-align:left}}.passed{{color:#16803c}}.failed{{color:#c62828}}.skipped{{color:#8a6500}}</style>
</head><body><h1>Behave Test Report</h1><p>Generated: {html.escape(generated_at)}</p>
<p><strong>Passed:</strong> {passed} &nbsp; <strong>Failed:</strong> {failed} &nbsp; <strong>Skipped:</strong> {skipped}</p>
<table><thead><tr><th>Feature</th><th>Scenario</th><th>Status</th></tr></thead><tbody>{''.join(rows)}</tbody></table></body></html>"""
        try:
            with open(html_path, 'w', encoding='utf-8') as html_file:
                html_file.write(document)
        except OSError:
            pass

    @classmethod
    def _write_behave_execution_summary(cls, full_path: str, log_text: str) -> None:
        """Persist Behave's console totals, including scenarios excluded by tag filters."""
        summary = {}
        patterns = {
            'features': r'(\d+)\s+features?\s+passed,\s*(\d+)\s+failed,\s*(\d+)\s+skipped',
            'scenarios': r'(\d+)\s+scenarios?\s+passed,\s*(\d+)\s+failed,\s*(\d+)\s+skipped',
            'steps': r'(\d+)\s+steps?\s+passed,\s*(\d+)\s+failed,\s*(\d+)\s+skipped,\s*(\d+)\s+undefined',
        }
        for key, pattern in patterns.items():
            matches = re.findall(pattern, log_text or '', flags=re.IGNORECASE)
            if not matches:
                continue
            values = [int(value) for value in matches[-1]]
            summary[key] = {
                'passed': values[0],
                'failed': values[1],
                'skipped': values[2],
                'undefined': values[3] if len(values) > 3 else 0,
            }
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
        cache_key = int(time.time() * 1000)
        return f"/api/script-runner/report?path={quote(report_file, safe='')}&run={cache_key}"

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

    @classmethod
    def execute_with_streaming(cls, meta: dict, env: str, browser: str, tags: str, custom_commands: str = ""):
        project_path = meta.get('path', '') or meta.get('project_path', '')
        project_name = meta.get('name', '') or meta.get('project_name', '')
        full_path = os.path.join(project_path, project_name) if project_name not in project_path else project_path

        language = meta.get('language') or meta.get('lang') or meta.get('project_lang', '')
        framework = meta.get('framework') or meta.get('fw') or meta.get('project_fw', '')
        tool = meta.get('tool') or meta.get('project_tool', '')
        
        start_time = time.time()
        print(f"DEBUG: Starting streaming in {full_path}")
        
        # Import prompts for self-healing
        parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        from prompts.script_runner_prompts import (
            get_diagnose_locator_error_prompt,
            get_heal_locator_prompt
        )
        
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
                            
                            # Diagnose if it's a locator error
                            prompt_diag = get_diagnose_locator_error_prompt(cmd, cmd_output)
                            diag_resp = cls._call_ai_sync_json(prompt_diag)
                            
                            is_locator_error = diag_resp.get("is_locator_error", False)
                            file_to_fix = diag_resp.get("file_to_fix", "").strip()
                            failed_locator = diag_resp.get("failed_locator", "").strip()
                            reason = diag_resp.get("reason", "").strip()
                            
                            if is_locator_error and file_to_fix:
                                file_target = os.path.join(full_path, file_to_fix)
                                if os.path.exists(file_target):
                                    # Create backup
                                    backup_target = file_target + f".bak.healing.{attempt}"
                                    try:
                                        shutil.copy2(file_target, backup_target)
                                    except Exception as backup_err:
                                        print(f"Error creating backup: {backup_err}")
                                        
                                    log_msg = f"[Self-Healing] Diagnosed element locator failure in {file_to_fix}: '{failed_locator}' (Reason: {reason}). Backup created at {file_to_fix}.bak.healing.{attempt}"
                                    yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
                                    
                                    try:
                                        with open(file_target, 'r', encoding='utf-8') as f:
                                            file_content = f.read()
                                            
                                        # Call AI to heal locator
                                        prompt_fix = get_heal_locator_prompt(file_to_fix, file_content, failed_locator, cmd_output)
                                        fix_resp = cls._call_ai_sync_json(prompt_fix)
                                        healed_code = fix_resp.get("healed_content", "")
                                        explanation = fix_resp.get("explanation", "")
                                        
                                        if healed_code:
                                            with open(file_target, 'w', encoding='utf-8') as f:
                                                f.write(healed_code)
                                            log_msg = f"[Self-Healing] Corrected element locator: {explanation}. Retrying execution..."
                                            yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
                                            attempt += 1
                                            continue
                                        else:
                                            log_msg = "[Self-Healing] AI did not return a locator fix. Stopping retry loop."
                                            yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
                                            break
                                    except Exception as fix_err:
                                        log_msg = f"[Self-Healing] Error applying locator fix: {fix_err}"
                                        yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
                                        break
                                else:
                                    log_msg = f"[Self-Healing] Target file '{file_to_fix}' not found. Cannot perform healing."
                                    yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
                                    break
                            else:
                                log_msg = "[Self-Healing] Error does not appear to be element locator-related. Cannot self-heal."
                                yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
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
        from prompts.script_runner_prompts import get_execution_command_prompt, get_diagnose_error_prompt, get_fix_script_prompt, get_correct_command_prompt

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
                cmd = "npx playwright test"
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
                        # Execution failed. Let's ask AI to diagnose the error (COMMAND vs SCRIPT)
                        error_output = cmd_output
                        prompt_diag = get_diagnose_error_prompt(cmd, error_output)
                        diag_resp = cls._call_ai_sync_json(prompt_diag)
                        error_type = diag_resp.get("error_type", "COMMAND_ERROR")
                        file_to_fix = diag_resp.get("file_to_fix", "")
                        
                        if error_type == "SCRIPT_ERROR" and file_to_fix:
                            file_target = os.path.join(full_path, file_to_fix)
                            if os.path.exists(file_target):
                                backup_target = file_target + f".bak.{attempts}"
                                shutil.copy2(file_target, backup_target)
                                log_msg = f"[Self-Healing] Diagnosed SCRIPT_ERROR in {file_to_fix}. Backup created at {file_to_fix}.bak.{attempts}"
                                output_log += f"\n{log_msg}\n"
                                yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
                                
                                with open(file_target, 'r', encoding='utf-8') as f:
                                    file_content = f.read()
                                    
                                prompt_fix = get_fix_script_prompt(error_output, file_to_fix, file_content)
                                fix_resp = cls._call_ai_sync_json(prompt_fix)
                                fixed_code = fix_resp.get("fixed_content", "")
                                
                                if fixed_code:
                                    with open(file_target, 'w', encoding='utf-8') as f:
                                        f.write(fixed_code)
                                    log_msg = f"[Self-Healing] File {file_to_fix} rewritten successfully. Retrying execution..."
                                    output_log += f"\n{log_msg}\n"
                                    yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
                                    # Keep cmd the same, it will retry in the while loop
                                else:
                                    log_msg = "[Self-Healing] AI failed to provide a fix. Falling back to command retry."
                                    output_log += f"\n{log_msg}\n"
                                    yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
                                    error_type = "COMMAND_ERROR" # Fallback
                            else:
                                log_msg = f"[Self-Healing] Identified file {file_to_fix} but it does not exist. Falling back..."
                                output_log += f"\n{log_msg}\n"
                                yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
                                error_type = "COMMAND_ERROR" # Fallback
                                
                        if error_type == "COMMAND_ERROR" or not file_to_fix:
                            prompt2 = get_correct_command_prompt(
                                cmd=cmd,
                                error_output=error_output,
                                tool=tool,
                                framework=framework
                            )
                            ai_correction = cls._call_ai_sync_json(prompt2)
                            new_cmd = ai_correction.get("command", "")
                            if new_cmd:
                                cmd = new_cmd
                                log_msg = f"[Self-Healing] AI suggested corrected command: {cmd}"
                                output_log += f"\n{log_msg}\n"
                                yield f"event: progress\ndata: {json.dumps({'msg': log_msg, 'type': 'system'})}\n\n"
                            else:
                                break
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
