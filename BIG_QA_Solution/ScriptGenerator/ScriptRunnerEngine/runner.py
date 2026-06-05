import os
import sys
import json
import glob
import shutil
import subprocess
import signal
import time

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


class ScriptRunnerService:
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
        
        # Mode A: Custom Sequential Commands
        if custom_commands.strip():
            commands = [c.strip() for c in custom_commands.split('\n') if c.strip()]
            print(f"DEBUG: Found {len(commands)} custom commands")
            yield f"event: progress\ndata: {json.dumps({'msg': f'[Sequential Mode] Starting {len(commands)} commands...', 'type': 'system'})}\n\n"
            
            success = True
            for i, cmd in enumerate(commands):
                print(f"DEBUG: Running step {i+1}: {cmd}")
                yield f"event: progress\ndata: {json.dumps({'msg': f'[Step {i+1}/{len(commands)}]: {cmd}', 'type': 'step_start', 'step': i+1})}\n\n"
                
                # Automatically detect and use venv if present.
                # Start from _runtime_env_with_ca() (not a bare os.environ copy) so a test run
                # behind a corporate proxy inherits the same TLS/CA + proxy settings as install.
                env_vars = _runtime_env_with_ca()
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
                
                for line in process.stdout:
                    yield f"event: log\ndata: {json.dumps({'msg': line.rstrip()})}\n\n"
                
                process.stdout.close()
                return_code = process.wait()
                del active_processes[process.pid]
                
                if return_code != 0:
                    yield f"event: progress\ndata: {json.dumps({'msg': f'[Error] Command failed with exit code {return_code}', 'type': 'step_fail', 'step': i+1})}\n\n"
                    success = False
                    break
                else:
                    yield f"event: progress\ndata: {json.dumps({'msg': f'[Success] Step {i+1} completed', 'type': 'step_pass', 'step': i+1})}\n\n"

            # Finalize
            if success and language and language.lower() == 'java':
                yield f"event: progress\ndata: {json.dumps({'msg': '[System] Execution successful. Opening latest report file...', 'type': 'system'})}\n\n"
                latest_report = cls._open_latest_report(full_path, start_time)
                if latest_report:
                    yield f"event: progress\ndata: {json.dumps({'msg': f'[System] Opened report: {os.path.basename(latest_report)}', 'type': 'system'})}\n\n"
                else:
                    yield f"event: progress\ndata: {json.dumps({'msg': '[System] No recently generated report files (.html, .pdf, etc.) found.', 'type': 'system'})}\n\n"

            report_url = cls._get_latest_report(full_path)
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
                env_vars = _runtime_env_with_ca()
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
                        env_vars = _runtime_env_with_ca()
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

        # Open latest result file if execution was successful and it is a Java project
        if success and language and language.lower() == 'java':
            yield f"event: progress\ndata: {json.dumps({'msg': '[System] Execution successful. Opening latest report file...', 'type': 'system'})}\n\n"
            latest_report = cls._open_latest_report(full_path, start_time)
            if latest_report:
                yield f"event: progress\ndata: {json.dumps({'msg': f'[System] Opened report: {os.path.basename(latest_report)}', 'type': 'system'})}\n\n"
            else:
                yield f"event: progress\ndata: {json.dumps({'msg': '[System] No recently generated report files (.html, .pdf, etc.) found.', 'type': 'system'})}\n\n"

        # Step 3: Finalize
        report_url = cls._get_latest_report(full_path)
        yield f"event: result\ndata: {json.dumps({'status': 'success' if success else 'error', 'report_url': report_url})}\n\n"
        return

    @classmethod
    def _open_latest_report(cls, full_path, start_time):
        report_extensions = ['.html', '.htm', '.pdf', '.json', '.xml', '.png']
        candidates = []

        # 1. Target Directory Identification
        possible_dirs = []
        try:
            for item in os.listdir(full_path):
                item_path = os.path.join(full_path, item)
                if os.path.isdir(item_path):
                    item_lower = item.lower()
                    if any(variation in item_lower for variation in ["results", "outputs", "reports", "target", "build"]):
                        if not any(skip in item_lower for skip in [".git", ".idea", ".venv", "node_modules"]):
                            possible_dirs.append(item_path)
        except Exception:
            pass

        # Sort multiple matching directories by latest modification timestamp
        if possible_dirs:
            possible_dirs.sort(key=os.path.getmtime, reverse=True)
            
            # 2. Directory Traversal & Deep Search
            target_dir = possible_dirs[0]
            for root, dirs, files in os.walk(target_dir):
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in report_extensions:
                        file_path = os.path.join(root, file)
                        try:
                            mtime = os.path.getmtime(file_path)
                            if mtime >= start_time - 10:
                                candidates.append((file_path, mtime, ext))
                        except Exception:
                            pass

        # 3. Fallback (Else Condition)
        if not candidates:
            for root, dirs, files in os.walk(full_path):
                if any(p in root.replace('\\', '/').split('/') for p in ['.git', '.idea', '.venv', 'node_modules', 'classes', 'test-classes', 'src']):
                    continue
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in report_extensions:
                        file_path = os.path.join(root, file)
                        try:
                            mtime = os.path.getmtime(file_path)
                            if mtime >= start_time - 10:
                                candidates.append((file_path, mtime, ext))
                        except Exception:
                            pass

        if not candidates:
            return None

        def sort_key(item):
            path, mtime, ext = item
            priority = 0
            if ext in ['.html', '.htm', '.pdf']:
                priority = 2
            elif ext in ['.xml', '.json']:
                priority = 1
            return (priority, mtime)

        candidates.sort(key=sort_key, reverse=True)
        latest_report = candidates[0][0]
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
            html_files = glob.glob(os.path.join(full_path, '**', '*.html'), recursive=True)
            if html_files:
                html_files.sort(key=os.path.getmtime, reverse=True)
                report_file = html_files[0]
                report_url = f"/api/script-runner/report?path={report_file}"
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
            is_windows = os.name == 'nt'
            if is_windows:
                result = subprocess.run(cmd, cwd=project_path, shell=True, capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
            else:
                import shlex
                result = subprocess.run(shlex.split(cmd), cwd=project_path, shell=False, capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
            output = result.stdout + result.stderr
            return {"output": output}
        except Exception as e:
            return {"output": str(e)}
