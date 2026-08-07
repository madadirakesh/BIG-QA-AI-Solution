import os
import subprocess
import json

class GitService:
    @staticmethod
    def _run_cmd(cmd, cwd, env=None):
        import shlex
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                shell=False,
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
    def _has_uncommitted_changes(project_path):
        res = GitService._run_cmd("git status --porcelain", project_path)
        return bool(res["success"] and res["stdout"].strip())

    @staticmethod
    def _default_branch(project_path):
        default_branch = "main"
        ls_res = GitService._run_cmd("git ls-remote --symref origin HEAD", project_path)
        if ls_res["success"] and ls_res["stdout"]:
            for line in ls_res["stdout"].splitlines():
                if "refs/heads/" in line and "HEAD" in line:
                    parts = line.split("refs/heads/")
                    if len(parts) > 1:
                        detected = parts[1].split()[0].strip()
                        if detected:
                            return detected

        heads_res = GitService._run_cmd("git ls-remote --heads origin", project_path)
        if heads_res["success"] and heads_res["stdout"]:
            lines = heads_res["stdout"]
            if "refs/heads/main" in lines:
                default_branch = "main"
            elif "refs/heads/master" in lines:
                default_branch = "master"
        return default_branch

    @staticmethod
    def _build_git_hints(action, branch, stderr_text):
        text = (stderr_text or "").lower()
        hints = []

        if action == "push":
            if (
                "fetch first" in text
                or "non-fast-forward" in text
                or "failed to push some refs" in text
                or "remote contains work that you do not have locally" in text
            ):
                hints.append("The remote repository already has commits. This usually happens when GitHub created the repo with a README.md or .gitignore.")
                hints.append(f"Run `git pull origin {branch} --allow-unrelated-histories` to bring the remote starter commit into your local branch.")
                hints.append("If Git reports conflicts in README.md or .gitignore, resolve them, then run `git add .` and `git commit -m \"Merge remote initial commit\"`.")
                hints.append(f"After that, run `git push -u origin {branch}` again.")
                hints.append("If you want your local codebase to fully replace the remote one, run `git fetch origin` first so Git has the latest remote state.")
                hints.append(f"Then run `git push -u origin {branch} --force-with-lease` to overwrite the remote branch with your local history.")
                hints.append("Use the force option only when you are sure the GitHub repository should be overwritten, because it rewrites remote history.")
            elif "no upstream branch" in text:
                hints.append(f"Run `git push -u origin {branch}` to create the upstream tracking branch.")

        elif action == "pull":
            if "unrelated histories" in text:
                hints.append(f"Your local branch and `origin/{branch}` do not share history yet.")
                hints.append(f"Run `git pull origin {branch} --allow-unrelated-histories` and resolve any conflicts before pushing again.")
            elif "please commit your changes or stash them" in text:
                hints.append("Your working tree has local changes that would be overwritten by pull.")
                hints.append("Run `git status` to inspect them, then either commit them with `git add .` and `git commit -m \"...\"`, or stash them with `git stash`.")
                hints.append(f"After that, run `git pull origin {branch}` again.")

        elif action == "merge":
            if "merge conflict" in text or "automatic merge failed" in text:
                hints.append("Git found conflicts while merging branches.")
                hints.append("Run `git status` to see the conflicted files, resolve them manually, then run `git add .` and `git commit` to finish the merge.")
            elif "working tree has uncommitted changes" in text:
                hints.append("Commit or stash your local changes before merging branches.")
                hints.append("Use `git add . && git commit -m \"...\"` or `git stash`, then retry the merge.")

        return hints

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
    def download_project_from_git(project_path, auth_config):
        """
        Clones/downloads the project from the git repository into the specified directory,
        initializing git and configuring remote and authentication locally.
        """
        repo_url = auth_config.get('repo_url')
        username = auth_config.get('username')
        access_token = auth_config.get('access_token')

        if not repo_url:
            return {"success": False, "message": "Repository URL is required."}

        # Make sure the directory exists
        try:
            os.makedirs(project_path, exist_ok=True)
        except Exception as e:
            return {"success": False, "message": f"Failed to create directory '{project_path}': {str(e)}"}

        # Construct authenticated URL
        if "://" in repo_url and access_token:
            parts = repo_url.split("://", 1)
            user = username or 'git'
            import urllib.parse
            escaped_token = urllib.parse.quote_plus(access_token)
            escaped_user = urllib.parse.quote_plus(user)
            authed_url = f"{parts[0]}://{escaped_user}:{escaped_token}@{parts[1]}"
        else:
            authed_url = repo_url

        # Check if .git folder already exists.
        git_dir = os.path.join(project_path, '.git')
        if not os.path.exists(git_dir):
            # If the directory is empty, we can run git clone directly.
            # Otherwise, we initialize git, set remote, fetch, and checkout/reset.
            try:
                is_empty = len(os.listdir(project_path)) == 0
            except Exception:
                is_empty = True

            if is_empty:
                clone_res = GitService._run_cmd(f"git clone {authed_url} .", project_path)
                if not clone_res["success"]:
                    return {"success": False, "message": f"Git clone failed: {clone_res['stderr'] or clone_res['stdout']}"}
            else:
                init_res = GitService._run_cmd("git init", project_path)
                if not init_res["success"]:
                    return {"success": False, "message": f"Failed to initialize git: {init_res['stderr']}"}
                
                remote_res = GitService._run_cmd(f"git remote add origin {authed_url}", project_path)
                if not remote_res["success"]:
                    GitService._run_cmd(f"git remote set-url origin {authed_url}", project_path)
        else:
            GitService._run_cmd(f"git remote set-url origin {authed_url}", project_path)

        # Set user name/email local configs
        if username:
            GitService._run_cmd(f'git config user.name "{username}"', project_path)
            email = f"{username}@users.noreply.github.com"
            GitService._run_cmd(f'git config user.email "{email}"', project_path)
            GitService._run_cmd('git config credential.helper ""', project_path)

        # Fetch remote branches
        fetch_res = GitService._run_cmd("git fetch origin", project_path)
        if not fetch_res["success"]:
            return {"success": False, "message": f"Failed to fetch from remote origin: {fetch_res['stderr'] or fetch_res['stdout']}"}

        # Determine default branch
        default_branch = GitService._default_branch(project_path)

        # Checkout default branch
        checkout_res = GitService._run_cmd(f"git checkout -f {default_branch}", project_path)
        if not checkout_res["success"]:
            checkout_res = GitService._run_cmd(f"git checkout -b {default_branch} origin/{default_branch}", project_path)
            if not checkout_res["success"]:
                reset_res = GitService._run_cmd(f"git reset --hard origin/{default_branch}", project_path)
                if not reset_res["success"]:
                    return {"success": False, "message": f"Failed to checkout default branch '{default_branch}': {checkout_res['stderr'] or checkout_res['stdout']}"}

        # Set upstream/tracking branch
        GitService._run_cmd(f"git branch --set-upstream-to=origin/{default_branch} {default_branch}", project_path)

        # Pull/rebase to be up to date
        GitService._run_cmd(f"git pull origin {default_branch} --rebase", project_path)

        return {"success": True, "message": f"Successfully configured and downloaded project branch '{default_branch}'"}

    @staticmethod
    def execute_native_action(action, project_path, auth_config, commit_message=None, merge_source_branch=None, merge_target_branch=None):
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
                error_text = res["stderr"] or res["stdout"]
                return {
                    "success": False,
                    "message": f"Push failed: {error_text}",
                    "hints": GitService._build_git_hints("push", branch, error_text),
                }
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
                    error_text = res["stderr"] or res["stdout"]
                    return {
                        "success": False,
                        "message": f"Pull failed: {error_text}",
                        "hints": GitService._build_git_hints("pull", pull_branch, error_text),
                    }
            
            # If pull branch was different from local branch and we succeeded, set tracking
            if pull_branch != local_branch:
                GitService._run_cmd(f"git branch --set-upstream-to=origin/{pull_branch} {local_branch}", project_path)

            return {"success": True, "message": f"Pulled branch '{pull_branch}' from remote successfully", "output": res["stdout"] or "Already up to date."}

        elif action == "merge":
            source_branch = (merge_source_branch or "").strip()
            target_branch = (merge_target_branch or "").strip() or GitService._get_current_branch(project_path)

            if not source_branch:
                return {"success": False, "message": "Merge failed: source branch is required."}

            if source_branch == target_branch:
                return {"success": False, "message": "Merge failed: source and target branches must be different."}

            if GitService._has_uncommitted_changes(project_path):
                return {
                    "success": False,
                    "message": "Merge blocked: working tree has uncommitted changes. Commit or stash them before merging."
                }

            fetch_res = GitService._run_cmd("git fetch origin --prune", project_path)
            if not fetch_res["success"] and auth_config and auth_config.get('repo_url'):
                return {"success": False, "message": f"Merge failed during remote refresh: {fetch_res['stderr'] or fetch_res['stdout']}"}

            current_branch = GitService._get_current_branch(project_path)
            if current_branch != target_branch:
                checkout_target = GitService._run_cmd(f"git checkout {target_branch}", project_path)
                if not checkout_target["success"]:
                    return {"success": False, "message": f"Merge failed: could not checkout target branch '{target_branch}': {checkout_target['stderr'] or checkout_target['stdout']}"}

            branch_check = GitService._run_cmd(f"git rev-parse --verify {source_branch}", project_path)
            merge_ref = source_branch
            if not branch_check["success"]:
                remote_branch_check = GitService._run_cmd(f"git rev-parse --verify origin/{source_branch}", project_path)
                if remote_branch_check["success"]:
                    merge_ref = f"origin/{source_branch}"
                else:
                    return {"success": False, "message": f"Merge failed: source branch '{source_branch}' was not found locally or on origin."}

            merge_res = GitService._run_cmd(f"git merge --no-ff --no-edit {merge_ref}", project_path)
            if not merge_res["success"]:
                abort_res = GitService._run_cmd("git merge --abort", project_path)
                abort_hint = ""
                if not abort_res["success"]:
                    abort_hint = " Automatic merge abort may require manual cleanup."
                return {
                    "success": False,
                    "message": f"Merge failed: {merge_res['stderr'] or merge_res['stdout']}.{abort_hint}",
                    "hints": GitService._build_git_hints("merge", target_branch, merge_res["stderr"] or merge_res["stdout"]),
                }

            return {
                "success": True,
                "message": f"Merged '{source_branch}' into '{target_branch}' successfully",
                "output": merge_res["stdout"] or merge_res["stderr"] or "Merge completed successfully."
            }
            
        else:
            return {"success": False, "message": f"Unknown action: {action}"}
