import os
import sys
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
            import os
            for i, cmd in enumerate(commands):
                print(f"DEBUG: Running step {i+1}: {cmd}")
                yield f"event: progress\ndata: {json.dumps({'msg': f'[Step {i+1}/{len(commands)}]: {cmd}', 'type': 'step_start', 'step': i+1})}\n\n"
                
                # Automatically detect and use venv if present
                env_vars = os.environ.copy()
                venv_bin = os.path.join(full_path, "venv", "Scripts" if os.name == 'nt' else "bin")
                if os.path.exists(venv_bin):
                    env_vars["PATH"] = venv_bin + os.pathsep + env_vars.get("PATH", "")
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
            tool=meta.get('tool'),
            language=meta.get('language'),
            framework=meta.get('framework'),
            env=env,
            browser=browser,
            tags=tags
        )
        
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
                import shlex
                result = subprocess.run(shlex.split(cmd), cwd=full_path, shell=False, capture_output=True, text=True, timeout=120, encoding='utf-8', errors='replace')
                output_log += f"\\n\\n[Attempt {attempts+1} Command]: {cmd}\\n"
                output_log += result.stdout + "\\n" + result.stderr
                if result.returncode == 0:
                    success = True
                else:
                    # Execution failed. Let's ask AI to diagnose the error (COMMAND vs SCRIPT)
                    error_output = result.stderr or result.stdout
                    prompt_diag = get_diagnose_error_prompt(cmd, error_output)
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
                                
                            prompt_fix = get_fix_script_prompt(error_output, file_to_fix, file_content)
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
                        prompt2 = get_correct_command_prompt(
                            cmd=cmd,
                            error_output=error_output,
                            tool=meta.get('tool'),
                            framework=meta.get('framework')
                        )
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
            import shlex
            result = subprocess.run(shlex.split(cmd), cwd=project_path, shell=False, capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
            output = result.stdout + result.stderr
            return {"output": output}
        except Exception as e:
            return {"output": str(e)}
