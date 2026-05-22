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
    def _get_current_branch(project_path):
        # Try symbolic-ref first
        res = GitService._run_cmd("git symbolic-ref --short HEAD", project_path)
        if res["success"] and res["stdout"].strip():
            return res["stdout"].strip()
        # Fallback to rev-parse
        res = GitService._run_cmd("git rev-parse --abbrev-ref HEAD", project_path)
        if res["success"] and res["stdout"].strip() and res["stdout"].strip() != "HEAD":
            return res["stdout"].strip()
        
        # Check .git/HEAD file if HEAD is unborn
        try:
            head_file = os.path.join(project_path, ".git", "HEAD")
            if os.path.exists(head_file):
                with open(head_file, "r") as f:
                    content = f.read().strip()
                    if content.startswith("ref: refs/heads/"):
                        return content.replace("ref: refs/heads/", "").strip()
        except Exception:
            pass
        return "main"

    @staticmethod
    def sync_git_config(project_path, auth_config):
        """
        Synchronizes the local repository's .git/config with the database Git credentials.
        Also configures local user name and email to avoid commit configuration issues.
        """
        if not os.path.exists(project_path):
            return {"success": False, "message": "Project path does not exist."}

        # Initialize git if not present
        if not os.path.exists(os.path.join(project_path, '.git')):
            init_res = GitService._run_cmd("git init", project_path)
            if not init_res["success"]:
                return {"success": False, "message": f"Failed to initialize git: {init_res['stderr']}"}

        # Setup remote origin with credentials
        if auth_config and auth_config.get('repo_url'):
            url = auth_config['repo_url']
            if "://" in url and auth_config.get('access_token'):
                parts = url.split("://")
                username = auth_config.get('username') or 'git'
                remote_url = f"{parts[0]}://{username}:{auth_config['access_token']}@{parts[1]}"
            else:
                remote_url = url
            
            check_remote = GitService._run_cmd("git remote -v", project_path)
            if "origin" not in check_remote["stdout"]:
                GitService._run_cmd(f"git remote add origin {remote_url}", project_path)
            else:
                GitService._run_cmd(f"git remote set-url origin {remote_url}", project_path)

        # Setup local user identity
        if auth_config and auth_config.get('username'):
            username = auth_config['username']
            GitService._run_cmd(f'git config user.name "{username}"', project_path)
            email = f"{username}@users.noreply.github.com"
            GitService._run_cmd(f'git config user.email "{email}"', project_path)
            
            # Bypass system/global Git Credential Manager (GCM) for this local repository.
            # This forces Git to strictly use the inline username/token in the remote URL instead of cached Windows credentials.
            GitService._run_cmd('git config credential.helper ""', project_path)
            
        return {"success": True, "message": "Git configuration successfully synchronized locally."}

    @staticmethod
    def execute_native_action(action, project_path, auth_config, commit_message=None):
        """
        Executes a standard deterministic Git command.
        auth_config is a dict containing repo_url, username, access_token
        """
        if not os.path.exists(project_path):
            return {"success": False, "message": "Project path does not exist."}

        # Synchronize git local config & user identity
        sync_res = GitService.sync_git_config(project_path, auth_config)
        if not sync_res["success"]:
            return sync_res

        if action == "status":
            res = GitService._run_cmd("git status -s", project_path)
            if not res["success"]:
                return {"success": False, "message": res["stderr"]}
            return {"success": True, "message": "Status retrieved", "output": res["stdout"] or "No changes. Working tree clean."}
            
        elif action == "commit":
            # Add all and commit
            msg = commit_message or "chore: Update project scripts via BIG-QA"
            escaped_msg = msg.replace('"', '\\"')
            GitService._run_cmd("git add .", project_path)
            res = GitService._run_cmd(f'git commit -m "{escaped_msg}"', project_path)
            if not res["success"]:
                if "nothing to commit" in res["stdout"].lower() or "nothing to commit" in res["stderr"].lower():
                    return {"success": True, "message": "Nothing to commit.", "output": "Working tree is clean."}
                return {"success": False, "message": f"Commit failed: {res['stderr'] or res['stdout']}"}
            return {"success": True, "message": "Committed successfully", "output": res["stdout"]}

        elif action == "push":
            if not auth_config or not auth_config.get('repo_url') or not auth_config.get('access_token'):
                return {"success": False, "message": "Repository URL and Token must be configured to push."}
            
            # Check if there are commits
            commit_check = GitService._run_cmd("git log -1", project_path)
            if not commit_check["success"]:
                return {"success": False, "message": "Push failed: No commits found. Please make an initial commit first before pushing."}

            branch = GitService._get_current_branch(project_path)
            
            # Align local master to main if remote prefers main or has no master
            if branch == "master":
                remote_master = GitService._run_cmd("git ls-remote --heads origin master", project_path)
                remote_main = GitService._run_cmd("git ls-remote --heads origin main", project_path)
                
                has_remote_master = remote_master["success"] and remote_master["stdout"].strip()
                has_remote_main = remote_main["success"] and remote_main["stdout"].strip()
                
                if has_remote_main or not has_remote_master:
                    rename_res = GitService._run_cmd("git branch -M main", project_path)
                    if rename_res["success"]:
                        branch = "main"

            res = GitService._run_cmd(f"git push -u origin {branch}", project_path)
            if not res["success"]:
                return {"success": False, "message": f"Push failed: {res['stderr'] or res['stdout']}"}
            return {"success": True, "message": f"Pushed to remote branch '{branch}' successfully", "output": (res["stderr"] + "\n" + res["stdout"]).strip()}
            
        elif action == "pull":
            if not auth_config or not auth_config.get('repo_url') or not auth_config.get('access_token'):
                return {"success": False, "message": "Repository URL and Token must be configured to pull."}
            
            # Fetch remote updates first to keep remote tracking branches up to date
            GitService._run_cmd("git fetch origin", project_path)

            # Find current local branch
            local_branch = GitService._get_current_branch(project_path)

            # Check if local branch has an upstream tracking branch configured
            upstream_res = GitService._run_cmd("git rev-parse --abbrev-ref --symbolic-full-name @{u}", project_path)
            
            pull_branch = None
            if upstream_res["success"] and upstream_res["stdout"].strip():
                upstream = upstream_res["stdout"].strip()
                if "/" in upstream:
                    pull_branch = upstream.split("/", 1)[1]
            
            if not pull_branch:
                # Check if local_branch exists on remote
                remote_check = GitService._run_cmd(f"git ls-remote --heads origin {local_branch}", project_path)
                if remote_check["success"] and remote_check["stdout"].strip():
                    pull_branch = local_branch
                else:
                    # Find default branch from remote using ls-remote
                    default_branch_res = GitService._run_cmd("git ls-remote --symref origin HEAD", project_path)
                    if default_branch_res["success"] and default_branch_res["stdout"].strip():
                        for line in default_branch_res["stdout"].splitlines():
                            if "refs/heads/" in line and "HEAD" in line:
                                parts = line.split("refs/heads/")
                                if len(parts) > 1:
                                    pull_branch = parts[1].split()[0].strip()
                                    break
                    
                    if not pull_branch:
                        # Check if main exists
                        main_check = GitService._run_cmd("git ls-remote --heads origin main", project_path)
                        if main_check["success"] and main_check["stdout"].strip():
                            pull_branch = "main"
                        else:
                            # Check if master exists
                            master_check = GitService._run_cmd("git ls-remote --heads origin master", project_path)
                            if master_check["success"] and master_check["stdout"].strip():
                                pull_branch = "master"
                            else:
                                pull_branch = "main"

            res = GitService._run_cmd(f"git pull origin {pull_branch} --rebase", project_path)
            if not res["success"]:
                # Try allowing unrelated histories
                if "unrelated histories" in res["stderr"].lower() or "unrelated histories" in res["stdout"].lower():
                    res = GitService._run_cmd(f"git pull origin {pull_branch} --rebase --allow-unrelated-histories", project_path)
                
                if not res["success"]:
                    return {"success": False, "message": f"Pull failed: {res['stderr'] or res['stdout']}"}
            
            # If pull branch was different from local branch and we succeeded, set tracking
            if pull_branch != local_branch:
                GitService._run_cmd(f"git branch --set-upstream-to=origin/{pull_branch} {local_branch}", project_path)

            return {"success": True, "message": f"Pulled branch '{pull_branch}' from remote successfully", "output": res["stdout"] or "Already up to date."}
            
        else:
            return {"success": False, "message": f"Unknown action: {action}"}
