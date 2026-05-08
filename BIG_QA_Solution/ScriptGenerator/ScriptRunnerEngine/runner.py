import os
import json
import glob
import shutil
import subprocess
import signal
import time

active_processes = {} # pid -> process object


class ScriptRunnerService:
    @staticmethod
    def _call_ai_sync_json(prompt: str) -> dict:
        try:
            import asyncio
            import json
            from BIG_QA_Solution.ScriptGenerator.api.backend import call_ai
            #from api.backend import call_ai
            
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
        project_path = meta.get('path', '')
        project_name = meta.get('name', '')
        full_path = os.path.join(project_path, project_name) if project_name not in project_path else project_path
        
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
                
                # Automatically detect and use venv if present
                env_vars = os.environ.copy()
                venv_bin = os.path.join(full_path, "venv", "Scripts" if os.name == 'nt' else "bin")
                if os.path.exists(venv_bin):
                    env_vars["PATH"] = venv_bin + os.pathsep + env_vars.get("PATH", "")
                
                process = subprocess.Popen(cmd, cwd=full_path, shell=True, env=env_vars, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, universal_newlines=True, encoding='utf-8', errors='replace')
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
            report_url = cls._get_latest_report(full_path)
            yield f"event: result\ndata: {json.dumps({'status': 'success' if success else 'error', 'report_url': report_url})}\n\n"
            return

        # Mode B: AI Generation (Simplified for streaming)
        yield f"event: progress\ndata: {json.dumps({'msg': '[AI Mode] Analyzing & generating execution command...', 'type': 'system'})}\n\n"
        # ... existing AI logic simplified or wrapped ...
        # (For now, let's just implement sequential streaming as it's the primary request)

        # Mode B: AI Generation with Self-Healing (Existing logic)
        # Step 1: AI generates command
        prompt = f"""
        The user wants to run an automated test.
        Tool: {meta.get('tool')}
        Language: {meta.get('language')}
        Framework: {meta.get('framework')}
        Environment: {env}
        Browser: {browser}
        Tags: {tags}
        
        Generate the terminal command to execute this test suite locally. Wait, for Python it's usually `pytest` or `behave`, for Node it's `npm run test` or `npx playwright test`, for Java it's `mvn test`. Include any tags/browser parameters as standard arguments for the given framework.
        Return only JSON like: {{"command": "the command string"}}
        """
        
        ai_resp = cls._call_ai_sync_json(prompt)
        cmd = ai_resp.get("command", "")
        
        if not cmd:
            # Fallback if AI fails
            if meta.get('language') == 'Python':
                cmd = "pytest tests/ --html=Results/report.html"
            elif meta.get('language') in ['Typescript', 'JavaScript', 'TypeScript']:
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
                result = subprocess.run(cmd, cwd=full_path, shell=True, capture_output=True, text=True, timeout=120, encoding='utf-8', errors='replace')
                output_log += f"\\n\\n[Attempt {attempts+1} Command]: {cmd}\\n"
                output_log += result.stdout + "\\n" + result.stderr
                if result.returncode == 0:
                    success = True
                else:
                    # Execution failed. Let's ask AI to diagnose the error (COMMAND vs SCRIPT)
                    error_output = result.stderr or result.stdout
                    prompt_diag = f"""
                    The test execution failed.
                    Command: {cmd}
                    Output: {error_output}
                    
                    Analyze the error. Return ONLY JSON exactly matching this format:
                    {{
                      "error_type": "COMMAND_ERROR" or "SCRIPT_ERROR",
                      "file_to_fix": "relative/path/to/failed_script.py", 
                      "reason": "short description"
                    }}
                    If the error is related to element not found, syntax error in test script, assertion failure, etc., classify as SCRIPT_ERROR and provide the correct relative path to the failing script file.
                    If it's an issue with the command itself (e.g. pytest not found), classify as COMMAND_ERROR.
                    If no specific file is found, use empty string for file_to_fix.
                    """
                    diag_resp = cls._call_ai_sync_json(prompt_diag)
                    error_type = diag_resp.get("error_type", "COMMAND_ERROR")
                    file_to_fix = diag_resp.get("file_to_fix", "")
                    
                    if error_type == "SCRIPT_ERROR" and file_to_fix:
                        file_target = os.path.join(full_path, file_to_fix)
                        if os.path.exists(file_target):
                            backup_target = file_target + f".bak.{attempts}"
                            shutil.copy2(file_target, backup_target)
                            output_log += f"\\n[Self-Healing] Diagnosed SCRIPT_ERROR in {file_to_fix}. Backup created at {file_to_fix}.bak.{attempts}\\n"
                            
                            with open(file_target, 'r', encoding='utf-8') as f:
                                file_content = f.read()
                                
                            prompt_fix = f"""
                            The test failed with this error:
                            {error_output}
                            
                            Here is the current content of {file_to_fix}:
                            ```
                            {file_content}
                            ```
                            
                            Fix the source code (e.g., self-heal the locator or assertion) so the test will pass. Keep imports and class structures intact.
                            Return ONLY JSON:
                            {{
                              "fixed_content": "the completely rewritten file content here"
                            }}
                            """
                            fix_resp = cls._call_ai_sync_json(prompt_fix)
                            fixed_code = fix_resp.get("fixed_content", "")
                            
                            if fixed_code:
                                with open(file_target, 'w', encoding='utf-8') as f:
                                    f.write(fixed_code)
                                output_log += f"[Self-Healing] File {file_to_fix} rewritten successfully. Retrying execution...\\n"
                                # Keep cmd the same, it will retry in the while loop
                            else:
                                output_log += "[Self-Healing] AI failed to provide a fix. Falling back to command retry.\\n"
                                error_type = "COMMAND_ERROR" # Fallback
                        else:
                            output_log += f"[Self-Healing] Identified file {file_to_fix} but it does not exist. Falling back...\\n"
                            error_type = "COMMAND_ERROR" # Fallback
                            
                    if error_type == "COMMAND_ERROR" or not file_to_fix:
                        prompt2 = f"""
                        The command `{cmd}` failed with this error: {error_output}
                        For a project using {meta.get('tool')}/{meta.get('framework')}.
                        Provide a corrected command in JSON like {{"command": "new command string"}}.
                        """
                        ai_correction = cls._call_ai_sync_json(prompt2)
                        new_cmd = ai_correction.get("command", "")
                        if new_cmd:
                            cmd = new_cmd
                        else:
                            break
            except Exception as e:
                output_log += f"\\nError executing: {e}"
                break
            attempts += 1

        # Step 3: Finalize
        return cls._finalize_execution(success, output_log, full_path)

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
            result = subprocess.run(cmd, cwd=project_path, shell=True, capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
            output = result.stdout + result.stderr
            return {"output": output}
        except Exception as e:
            return {"output": str(e)}
