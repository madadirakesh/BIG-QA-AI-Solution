import argparse

from mcp.server.fastmcp import FastMCP

from api import local_git_assistant as git_tools


def create_server(host: str, port: int) -> FastMCP:
    # This file exposes Git capabilities as MCP tools. The actual Git command
    # implementation lives in local_git_assistant.py so the MCP layer stays thin.
    mcp = FastMCP(
        "Git MCP Server",
        host=host,
        port=port,
        log_level="ERROR",
    )

    @mcp.tool(description="Get a concise repository overview including branch, status, staged changes, unstaged changes, commits, and remotes.")
    def repository_overview() -> str:
        return git_tools.repository_overview()

    @mcp.tool(description="Get the selected local project path and local repository status, including branch, staged and unstaged changes, local commits, and remotes.")
    def local_repository_overview() -> str:
        return git_tools.local_repository_overview()

    @mcp.tool(description="Get the selected project path and live remote-tracking repository details, including remotes, upstream branch, ahead/behind status, latest local commit, and latest upstream commit. By default it refreshes remote refs with git fetch.")
    def remote_repository_overview(refresh: bool = True) -> str:
        return git_tools.remote_repository_overview(refresh=refresh)

    @mcp.tool(description="Get a combined local and live repository context for the selected project path. Use this for broad questions about the repo, project path, local changes, push/pull status, or local-vs-remote comparisons.")
    def repository_context(refresh_remote: bool = True) -> str:
        return git_tools.repository_context(refresh_remote=refresh_remote)

    @mcp.tool(description="Show the current git status including branch tracking information and changed files.")
    def git_status() -> str:
        return git_tools.git_status()

    @mcp.tool(description="Show a summary of changed files for staged or unstaged changes.")
    def git_diff_summary(staged: bool = False) -> str:
        return git_tools.git_diff(staged=staged)

    @mcp.tool(description="Show the actual git diff patch. Optionally provide a file_path to narrow the patch to one file.")
    def git_diff_content(file_path: str = "", staged: bool = False) -> str:
        return git_tools.git_diff_content(staged=staged, file_path=file_path or None)

    @mcp.tool(description="Show recent commits from the current repository.")
    def recent_commits(limit: int = 5) -> str:
        return git_tools.recent_commits(limit)

    @mcp.tool(description="Show local and remote branches for the current repository.")
    def branch_info() -> str:
        return git_tools.branch_info()

    @mcp.tool(description="Show configured git remotes and their fetch/push URLs.")
    def remote_info() -> str:
        return git_tools.remote_info()

    @mcp.tool(description="Commit all local changes using the provided commit message.")
    def commit_all(commit_message: str) -> str:
        return git_tools.commit_all(commit_message)

    @mcp.tool(description="Merge a source branch into a target branch for the selected project path. Use only when the user explicitly asks to merge branches.")
    def merge_branch(source_branch: str, target_branch: str = "") -> str:
        return git_tools.merge_branch(source_branch=source_branch, target_branch=target_branch)

    @mcp.tool(description="Pull the current branch from origin with rebase.")
    def pull_current_branch() -> str:
        return git_tools.pull_current_branch()

    @mcp.tool(description="Push the current branch to origin and set upstream if needed.")
    def push_current_branch() -> str:
        return git_tools.push_current_branch()

    return mcp


def main() -> None:
    # The MCP client launches this module as a short-lived localhost server for
    # each assistant request, using streamable HTTP transport.
    parser = argparse.ArgumentParser(description="Git MCP Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = create_server(args.host, args.port)
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
