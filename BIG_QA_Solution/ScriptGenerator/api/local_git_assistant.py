"""Shared Git helpers used by the MCP Git assistant."""

import os
import re

from api.git_service import GitService

DEFAULT_COMMIT_LIMIT = 5
MAX_COMMIT_LIMIT = 20


def _project_path() -> str:
    """Resolve the current repository work tree from the active request context."""
    work_tree = (os.environ.get("GIT_WORK_TREE") or "").strip()
    return work_tree or os.getcwd()


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

    result = GitService.execute_native_action(
        "merge",
        _project_path(),
        auth_config={},
        merge_source_branch=normalized_source,
        merge_target_branch=normalized_target,
    )
    if result.get("success"):
        return result.get("output") or result.get("message") or "Merge completed successfully."
    raise RuntimeError(_sanitize_git_output(result.get("message") or "Merge failed."))


def pull_current_branch() -> str:
    # Try the local tracking configuration first, then fall back to origin/<branch>
    # so repositories with incomplete upstream setup still have a useful path.
    branch = current_branch()
    try:
        return _run_git("pull", "--rebase")
    except RuntimeError:
        return _run_git("pull", "origin", branch, "--rebase")


def push_current_branch() -> str:
    # Always push the currently checked-out branch and set upstream so the first
    # successful push also establishes tracking for future native Git actions.
    branch = current_branch()
    return _run_git("push", "-u", "origin", branch)
