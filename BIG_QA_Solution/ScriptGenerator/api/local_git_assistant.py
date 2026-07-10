# Shared local Git helpers used by the MCP Git assistant.

import os
import re

from api.git_service import GitService

DEFAULT_COMMIT_LIMIT = 5
MAX_COMMIT_LIMIT = 20


def _project_path() -> str:
    """Resolve the current repository work tree from the active request context."""
    work_tree = (os.environ.get("GIT_WORK_TREE") or "").strip()
    return work_tree or os.getcwd()


def _run_git(*args: str) -> str:
    """Run a git command against the selected project and sanitize the output."""
    # Reuse the shared GitService execution path so native Git actions and MCP
    # tools behave consistently without copying subprocess handling logic here.
    result = GitService._run_cmd(["git", *args], _project_path())
    if not result["success"]:
        error_text = result["stderr"].strip() or result["stdout"].strip() or "unknown git error"
        raise RuntimeError(_sanitize_git_output(error_text))
    return _sanitize_git_output(result["stdout"].strip())


def _sanitize_git_output(text: str) -> str:
    """Mask credentials if a remote URL is echoed back by Git."""
    if not text:
        return text
    return re.sub(r"https://([^:/\s]+):([^@\s]+)@", r"https://\1:***@", text)


def _normalize_file_path(file_path: str) -> str:
    return (file_path or "").strip().strip("\"'`")


def _empty_diff_message(staged: bool, file_path: str = "") -> str:
    if file_path:
        return f"No diff found for '{file_path}'."
    return "No staged changes." if staged else "No unstaged diff."


def repository_overview() -> str:
    """Return a compact repo snapshot for broad assistant questions."""
    # This is the default high-level answer for general prompts such as
    # "status", "review the code status", or other broad repository questions.
    branch = _run_git("branch", "--show-current") or "(unknown)"
    status = _run_git("status", "--short", "--branch") or "No status output."
    staged = _run_git("diff", "--cached", "--stat") or "No staged changes."
    unstaged = _run_git("diff", "--stat") or "No unstaged diff."
    commits = _run_git("log", "--oneline", "-5") or "No commit history found."
    remotes = _run_git("remote", "-v") or "No remotes configured."
    return (
        f"Current branch:\n{branch}\n\n"
        f"Status:\n{status}\n\n"
        f"Staged changes:\n{staged}\n\n"
        f"Unstaged changes:\n{unstaged}\n\n"
        f"Recent commits:\n{commits}\n\n"
        f"Remotes:\n{remotes}"
    )


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
