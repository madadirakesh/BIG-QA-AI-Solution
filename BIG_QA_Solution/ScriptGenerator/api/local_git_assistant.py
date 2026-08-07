"""Shared Git helpers used by the MCP Git assistant."""

import os
import re
import shlex

from api.git_service import GitService
from db.app_db import fetch_data

DEFAULT_COMMIT_LIMIT = 5
MAX_COMMIT_LIMIT = 20
REMOTE_GIT_COMMANDS = {"fetch", "pull", "push"}
ALLOWED_GIT_COMMANDS = {
    "status",
    "diff",
    "log",
    "show",
    "branch",
    "checkout",
    "switch",
    "fetch",
    "pull",
    "push",
    "merge",
    "rebase",
    "stash",
    "add",
    "restore",
    "commit",
    "cherry-pick",
    "rev-parse",
    "remote",
}
REJECTED_GIT_COMMANDS = {"reset", "clean", "config", "credential"}
REJECTED_GIT_TOKENS = {";", "&&", "||", "|", ">", ">>", "<", "$(", "`"}


def _project_path() -> str:
    """Resolve the current repository work tree from the active request context."""
    work_tree = (os.environ.get("GIT_WORK_TREE") or "").strip()
    return work_tree or os.getcwd()


def _project_git_auth() -> dict:
    """Load the saved Git auth config for the selected project, if any."""
    project_path = _project_path()
    existing = fetch_data("SELECT id FROM ProjectDetails WHERE project_path = ?", (project_path,))
    if not existing:
        return {}

    project_id = existing[0].get("id")
    if not project_id:
        return {}

    git_config = fetch_data(
        "SELECT repo_url, username, access_token FROM ProjectGitConfig WHERE project_details_id = ?",
        (project_id,),
    )
    return dict(git_config[0]) if git_config else {}


def _sync_project_git_auth() -> dict:
    """Ensure the selected repo uses the saved remote URL and token before Git operations."""
    auth_config = _project_git_auth()
    if auth_config:
        GitService.sync_git_config(_project_path(), auth_config)
    return auth_config


def _sanitize_git_output(text: str) -> str:
    """Mask credentials if a remote URL is echoed back by Git."""
    if not text:
        return text
    return re.sub(r"https://([^:/\s]+):([^@\s]+)@", r"https://\1:***@", text)


def _run_git_result(*args: str) -> dict:
    """Run a git command and return a sanitized result payload."""
    result = GitService._run_cmd(["git", *args], _project_path())
    return {
        "success": bool(result.get("success")),
        "stdout": _sanitize_git_output((result.get("stdout") or "").strip()),
        "stderr": _sanitize_git_output((result.get("stderr") or "").strip()),
    }


def _run_git(*args: str) -> str:
    """Run a git command against the selected project and raise on failure."""
    result = _run_git_result(*args)
    if not result["success"]:
        error_text = result["stderr"] or result["stdout"] or "unknown git error"
        raise RuntimeError(error_text)
    return result["stdout"]


def _normalize_file_path(file_path: str) -> str:
    return (file_path or "").strip().strip("\"'`")


def _empty_diff_message(staged: bool, file_path: str = "") -> str:
    if file_path:
        return f"No diff found for '{file_path}'."
    return "No staged changes." if staged else "No unstaged diff."


def _readable_output(result: dict, fallback: str) -> str:
    if result["success"] and result["stdout"]:
        return result["stdout"]
    if result["stderr"]:
        return f"{fallback} ({result['stderr']})"
    return fallback


def _resolve_current_branch() -> str:
    branch = _run_git_result("branch", "--show-current")
    if branch["success"] and branch["stdout"]:
        return branch["stdout"]
    return GitService._get_current_branch(_project_path()) or "(unknown)"


def _upstream_branch() -> str:
    upstream = _run_git_result("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream["success"] and upstream["stdout"]:
        return upstream["stdout"]
    return ""


def _remote_head_branch() -> str:
    remote_head = _run_git_result("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if remote_head["success"] and remote_head["stdout"]:
        return remote_head["stdout"]
    return ""


def _format_overview(title: str, body: str) -> str:
    return f"{title}:\n{body.strip() if body else '(none)'}"


def _format_native_result(result: dict, fallback_success: str) -> str:
    """Convert native Git action results into MCP-friendly text."""
    if result.get("success"):
        return result.get("output") or result.get("message") or fallback_success

    message = _sanitize_git_output(result.get("message") or "Git action failed.")
    hints = result.get("hints") or []
    if hints:
        message = f"{message}\nSuggested next steps:\n" + "\n".join(f"- {hint}" for hint in hints)
    raise RuntimeError(message)


def _validate_git_command(tokens: list[str]) -> None:
    if not tokens:
        raise RuntimeError("Please provide a git command to execute.")

    for token in tokens:
        if token in REJECTED_GIT_TOKENS:
            raise RuntimeError("Shell operators are not supported in MCP Git commands. Please provide only the git command itself.")

    command = tokens[0]
    if command in REJECTED_GIT_COMMANDS:
        raise RuntimeError(f"`git {command}` is not allowed through the MCP assistant.")
    if command not in ALLOWED_GIT_COMMANDS:
        raise RuntimeError(f"`git {command}` is not supported by the MCP assistant yet.")

    if command == "branch" and any(flag in tokens[1:] for flag in ("-d", "-D", "--delete")):
        raise RuntimeError("Branch deletion is not allowed through the MCP assistant.")

    if command == "remote":
        if len(tokens) > 1 and tokens[1] not in ("-v", "show", "get-url", "prune"):
            raise RuntimeError("Only read-only remote commands are allowed through the MCP assistant.")

    if command == "checkout" and any(flag in tokens[1:] for flag in ("--orphan",)):
        raise RuntimeError("`git checkout --orphan` is not allowed through the MCP assistant.")

    if command == "restore" and any(flag in tokens[1:] for flag in ("--source",)):
        raise RuntimeError("Restoring from an arbitrary source is not allowed through the MCP assistant.")


def _is_plain_push_command(tokens: list[str]) -> bool:
    return tokens in (["push"], ["push", "origin"], ["push", "-u", "origin", current_branch()])


def _is_plain_pull_command(tokens: list[str]) -> bool:
    return tokens in (["pull"], ["pull", "origin"], ["pull", "origin", current_branch()])


def local_repository_overview() -> str:
    """Return a compact snapshot of the selected local working tree."""
    project_path = _project_path()
    branch = _resolve_current_branch()
    status = _readable_output(_run_git_result("status", "--short", "--branch"), "No status output.")
    staged = _readable_output(_run_git_result("diff", "--cached", "--stat"), "No staged changes.")
    unstaged = _readable_output(_run_git_result("diff", "--stat"), "No unstaged diff.")
    commits = _readable_output(_run_git_result("log", "--oneline", f"-{DEFAULT_COMMIT_LIMIT}"), "No commit history found yet.")
    remotes = _readable_output(_run_git_result("remote", "-v"), "No remotes configured.")
    return (
        f"Project path:\n{project_path}\n\n"
        f"{_format_overview('Current branch', branch)}\n\n"
        f"{_format_overview('Local status', status)}\n\n"
        f"{_format_overview('Staged changes', staged)}\n\n"
        f"{_format_overview('Unstaged changes', unstaged)}\n\n"
        f"{_format_overview('Recent local commits', commits)}\n\n"
        f"{_format_overview('Configured remotes', remotes)}"
    )


def remote_repository_overview(refresh: bool = True) -> str:
    """Return the live/cached remote-tracking view for the selected repository."""
    project_path = _project_path()
    _sync_project_git_auth()
    branch = _resolve_current_branch()
    remotes = _readable_output(_run_git_result("remote", "-v"), "No remotes configured.")
    upstream = _upstream_branch()
    fetch_result = None

    if refresh:
        fetch_result = _run_git_result("fetch", "origin", "--prune")

    fetch_summary = "Refresh skipped. Using current local remote-tracking refs."
    if refresh:
        if fetch_result and fetch_result["success"]:
            fetch_summary = fetch_result["stdout"] or "Fetch completed successfully."
        elif fetch_result:
            fetch_summary = fetch_result["stderr"] or fetch_result["stdout"] or "Fetch failed."

    if not upstream:
        remote_head = _remote_head_branch() or "(unknown)"
        return (
            f"Project path:\n{project_path}\n\n"
            f"{_format_overview('Current branch', branch)}\n\n"
            f"{_format_overview('Configured remotes', remotes)}\n\n"
            f"{_format_overview('Remote refresh', fetch_summary)}\n\n"
            f"{_format_overview('Upstream tracking', 'No upstream branch is configured for the current local branch.')}\n\n"
            f"{_format_overview('Remote default branch', remote_head)}"
        )

    ahead_behind = _run_git_result("rev-list", "--left-right", "--count", f"{upstream}...HEAD")
    divergence = "Unable to calculate ahead/behind status."
    if ahead_behind["success"] and ahead_behind["stdout"]:
        parts = ahead_behind["stdout"].split()
        if len(parts) >= 2:
            behind, ahead = parts[0], parts[1]
            divergence = f"Local branch is ahead by {ahead} commit(s) and behind by {behind} commit(s) relative to {upstream}."

    local_head = _readable_output(_run_git_result("log", "--oneline", "-1", "HEAD"), "No local commits found yet.")
    upstream_head = _readable_output(_run_git_result("log", "--oneline", "-1", upstream), "No remote-tracking commit found.")
    remote_head = _remote_head_branch() or "(unknown)"

    return (
        f"Project path:\n{project_path}\n\n"
        f"{_format_overview('Current branch', branch)}\n\n"
        f"{_format_overview('Configured remotes', remotes)}\n\n"
        f"{_format_overview('Remote refresh', fetch_summary)}\n\n"
        f"{_format_overview('Tracked upstream branch', upstream)}\n\n"
        f"{_format_overview('Ahead/behind summary', divergence)}\n\n"
        f"{_format_overview('Latest local commit', local_head)}\n\n"
        f"{_format_overview('Latest upstream-tracking commit', upstream_head)}\n\n"
        f"{_format_overview('Remote default branch', remote_head)}"
    )


def repository_context(refresh_remote: bool = True) -> str:
    """Return both local and remote repository context for broad assistant prompts."""
    return (
        "Local repository context\n"
        "------------------------\n"
        f"{local_repository_overview()}\n\n"
        "Remote repository context\n"
        "-------------------------\n"
        f"{remote_repository_overview(refresh=refresh_remote)}"
    )


def repository_overview() -> str:
    """Backward-compatible broad overview for legacy callers."""
    return repository_context(refresh_remote=True)


def git_status() -> str:
    return _run_git("status", "--short", "--branch") or "No status output."


def current_branch() -> str:
    return GitService._get_current_branch(_project_path()) or "(unknown)"


def git_diff(staged: bool = False) -> str:
    """Return a summary view of changed files."""
    args = ["diff", "--stat"]
    if staged:
        args.insert(1, "--cached")
    return _run_git(*args) or _empty_diff_message(staged)


def git_diff_content(staged: bool = False, file_path: str | None = None) -> str:
    """Return the actual patch content, optionally narrowed to one file."""
    # Use the full patch view when the user asks what actually changed, or when
    # a specific file is mentioned in the prompt.
    args = ["diff"]
    if staged:
        args.append("--cached")

    normalized_path = _normalize_file_path(file_path) if file_path else ""
    if normalized_path:
        args.extend(["--", normalized_path])

    output = _run_git(*args)
    if output:
        return output

    return _empty_diff_message(staged, normalized_path)


def recent_commits(limit: int = 5) -> str:
    safe_limit = min(max(int(limit or DEFAULT_COMMIT_LIMIT), 1), MAX_COMMIT_LIMIT)
    return _run_git("log", "--oneline", f"-{safe_limit}") or "No commit history found."


def branch_info() -> str:
    return _run_git("branch", "-a") or "No branches found."


def remote_info() -> str:
    return _run_git("remote", "-v") or "No remotes configured."


def commit_all(commit_message: str) -> str:
    # The MCP commit tool uses one safe path for staging and committing so the
    # behavior is consistent whether the action is triggered by UI or AI flow.
    normalized_message = (commit_message or "").strip() or "chore: Update project scripts via BIG-QA"
    _sync_project_git_auth()
    GitService._run_cmd(["git", "add", "."], _project_path())
    result = GitService._run_cmd(["git", "commit", "-m", normalized_message], _project_path())
    if result["success"]:
        return _sanitize_git_output(result["stdout"].strip())
    combined_output = (result["stderr"].strip() or result["stdout"].strip() or "unknown git error")
    if "nothing to commit" in combined_output.lower():
        return "Nothing to commit. Working tree is clean."
    raise RuntimeError(_sanitize_git_output(combined_output))


def merge_branch(source_branch: str, target_branch: str = "") -> str:
    """Safely merge one branch into another for the selected project."""
    normalized_source = (source_branch or "").strip()
    normalized_target = (target_branch or "").strip() or current_branch()
    if not normalized_source:
        raise RuntimeError("Source branch is required for merge.")

    auth_config = _sync_project_git_auth()
    result = GitService.execute_native_action(
        "merge",
        _project_path(),
        auth_config=auth_config,
        merge_source_branch=normalized_source,
        merge_target_branch=normalized_target,
    )
    return _format_native_result(result, "Merge completed successfully.")


def pull_current_branch() -> str:
    auth_config = _sync_project_git_auth()
    result = GitService.execute_native_action("pull", _project_path(), auth_config=auth_config)
    return _format_native_result(result, "Pull completed successfully.")


def push_current_branch() -> str:
    auth_config = _sync_project_git_auth()
    result = GitService.execute_native_action("push", _project_path(), auth_config=auth_config)
    return _format_native_result(result, "Push completed successfully.")


def execute_git_command(command: str) -> str:
    """Run a guarded git command for common operational workflows."""
    raw_command = (command or "").strip()
    if not raw_command:
        raise RuntimeError("Please provide the git command you want to run.")

    try:
        tokens = shlex.split(raw_command)
    except ValueError as exc:
        raise RuntimeError(f"Unable to parse git command: {exc}") from exc

    if tokens and tokens[0].lower() == "git":
        tokens = tokens[1:]

    _validate_git_command(tokens)
    command_name = tokens[0]
    auth_config = _sync_project_git_auth()

    if command_name == "push" and _is_plain_push_command(tokens):
        result = GitService.execute_native_action("push", _project_path(), auth_config=auth_config)
        return _format_native_result(result, "Push completed successfully.")

    if command_name == "pull" and _is_plain_pull_command(tokens):
        result = GitService.execute_native_action("pull", _project_path(), auth_config=auth_config)
        return _format_native_result(result, "Pull completed successfully.")

    if command_name == "merge":
        if len(tokens) == 2 and not tokens[1].startswith("-"):
            return merge_branch(tokens[1], "")
        if len(tokens) == 3 and not tokens[1].startswith("-") and not tokens[2].startswith("-"):
            return merge_branch(tokens[1], tokens[2])

    result = GitService._run_cmd(["git", *tokens], _project_path())
    stdout = _sanitize_git_output((result.get("stdout") or "").strip())
    stderr = _sanitize_git_output((result.get("stderr") or "").strip())
    if result.get("success"):
        return stdout or stderr or f"`git {' '.join(tokens)}` completed successfully."

    error_text = stderr or stdout or "unknown git error"
    if command_name in REMOTE_GIT_COMMANDS:
        hints = GitService._build_git_hints(command_name, current_branch(), error_text)
        if hints:
            error_text = f"{error_text}\nSuggested next steps:\n" + "\n".join(f"- {hint}" for hint in hints)
    raise RuntimeError(error_text)
