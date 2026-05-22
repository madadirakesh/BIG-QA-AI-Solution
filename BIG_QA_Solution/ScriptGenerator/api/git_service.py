import os
import subprocess
import json

class GitService:
    @staticmethod
    def _run_cmd(cmd, cwd, env=None):
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                shell=True,
                capture_output=True,
                text=True,
                env=env or os.environ
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip()
            }
        except Exception as e:
            return {"success": False, "stdout": "", "stderr": str(e)}

    @staticmethod
    def execute_native_action(action, project_path, auth_config):
        """
        Executes a standard deterministic Git command.
        auth_config is a dict containing repo_url, username, access_token
        """
        if not os.path.exists(project_path):
            return {"success": False, "message": "Project path does not exist."}

        # Initialize git if not present
        if not os.path.exists(os.path.join(project_path, '.git')):
            init_res = GitService._run_cmd("git init", project_path)
            if not init_res["success"]:
                return {"success": False, "message": f"Failed to initialize git: {init_res['stderr']}"}

        # Ensure credentials are used if needed for push/pull
        remote_url = ""
        if auth_config.get('repo_url'):
            url = auth_config['repo_url']
            if "://" in url and auth_config.get('access_token'):
                parts = url.split("://")
                # Format: https://token@github.com/user/repo.git
                # Or https://user:token@github.com/... depending on provider. Assuming token is enough for most modern platforms.
                remote_url = f"{parts[0]}://{auth_config['username']}:{auth_config['access_token']}@{parts[1]}"
            else:
                remote_url = url
            
            # Setup remote origin
            check_remote = GitService._run_cmd("git remote -v", project_path)
            if "origin" not in check_remote["stdout"]:
                GitService._run_cmd(f"git remote add origin {remote_url}", project_path)
            else:
                GitService._run_cmd(f"git remote set-url origin {remote_url}", project_path)

        if action == "status":
            res = GitService._run_cmd("git status -s", project_path)
            if not res["success"]:
                return {"success": False, "message": res["stderr"]}
            return {"success": True, "message": "Status retrieved", "output": res["stdout"] or "No changes. Working tree clean."}
            
        elif action == "commit":
            # Add all and commit
            GitService._run_cmd("git add .", project_path)
            res = GitService._run_cmd('git commit -m "chore: Update project scripts via BIG-QA"', project_path)
            if not res["success"]:
                if "nothing to commit" in res["stdout"].lower() or "nothing to commit" in res["stderr"].lower():
                    return {"success": True, "message": "Nothing to commit.", "output": "Working tree is clean."}
                return {"success": False, "message": f"Commit failed: {res['stderr']}"}
            return {"success": True, "message": "Committed successfully", "output": res["stdout"]}

        elif action == "push":
            if not remote_url:
                return {"success": False, "message": "Repository URL and Token must be configured to push."}
            # Try to push to current branch (assume main/master)
            branch_res = GitService._run_cmd("git rev-parse --abbrev-ref HEAD", project_path)
            branch = branch_res["stdout"] if branch_res["success"] else "main"
            
            res = GitService._run_cmd(f"git push -u origin {branch}", project_path)
            if not res["success"]:
                return {"success": False, "message": f"Push failed: {res['stderr']}"}
            return {"success": True, "message": "Pushed to remote successfully", "output": res["stderr"] + "\n" + res["stdout"]}
            
        elif action == "pull":
            if not remote_url:
                return {"success": False, "message": "Repository URL and Token must be configured to pull."}
            branch_res = GitService._run_cmd("git rev-parse --abbrev-ref HEAD", project_path)
            branch = branch_res["stdout"] if branch_res["success"] else "main"
            
            res = GitService._run_cmd(f"git pull origin {branch} --rebase", project_path)
            if not res["success"]:
                return {"success": False, "message": f"Pull failed: {res['stderr']}"}
            return {"success": True, "message": "Pulled from remote successfully", "output": res["stdout"]}
            
        else:
            return {"success": False, "message": f"Unknown action: {action}"}
