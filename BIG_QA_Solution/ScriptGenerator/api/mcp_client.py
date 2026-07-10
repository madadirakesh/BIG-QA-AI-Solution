import asyncio
from contextlib import suppress
import json
import os
import socket
import subprocess
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from api import backend

MCP_HOST = "127.0.0.1"
MCP_STARTUP_TIMEOUT_SECONDS = 12


def _find_available_port() -> int:
    # Start each MCP session on a fresh localhost port so concurrent requests
    # do not collide with each other or with a previous crashed session.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((MCP_HOST, 0))
        return int(sock.getsockname()[1])


def _extract_json_block(text: str) -> dict:
    raw = (text or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
        raise


def _extract_text_from_tool_result(result) -> str:
    text_parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            text_parts.append(text)
    return "\n".join(text_parts).strip()


def _build_tool_choice_prompt(prompt: str, tools: list[dict]) -> str:
    # First AI pass: ask the configured model to choose exactly one MCP tool
    # and arguments based on the user's natural-language request.
    return (
        "You are an AI Git Assistant using MCP tools.\n"
        "Choose exactly one MCP tool that best answers the user's request.\n"
        "Return ONLY raw JSON in one of these shapes:\n"
        '{"mode":"tool","tool":"tool_name","arguments":{"key":"value"}}\n'
        '{"mode":"answer","message":"direct answer"}\n\n'
        "Rules:\n"
        "- Prefer repository_overview for broad repo questions.\n"
        "- Use git_diff_content when the user asks what changed in a specific file or asks for actual code changes.\n"
        "- Use git_diff_summary for high-level change summaries.\n"
        "- Use commit_all, pull_current_branch, or push_current_branch only when the user explicitly asks for those actions.\n"
        "- If the user asks about GitHub pull requests, PR reviews, or remote review metadata, do not choose a tool. Return mode=answer and explain that this MCP server only has local Git tools.\n\n"
        f"Available Tools:\n{json.dumps(tools, indent=2)}\n\n"
        f"User Request:\n{prompt}"
    )


def _build_summary_prompt(user_prompt: str, tool_name: str, tool_args: dict, tool_output: str) -> str:
    # Second AI pass: convert raw MCP tool output into a direct user-facing
    # explanation, while preserving full diff output when requested.
    return (
        "You are an AI Git Assistant.\n"
        "Answer the user based only on the MCP tool output below.\n"
        "Be concise, practical, and accurate.\n"
        "If the output is already a patch/diff that the user explicitly asked to see, preserve it verbatim.\n"
        "If the tool output indicates a failure, explain the likely cause and next step.\n\n"
        f"User Request:\n{user_prompt}\n\n"
        f"Tool Used: {tool_name}\n"
        f"Tool Arguments: {json.dumps(tool_args, ensure_ascii=True)}\n\n"
        f"Tool Output:\n{tool_output}"
    )


async def _start_mcp_server(project_path: str) -> tuple[subprocess.Popen, str]:
    # Launch an isolated MCP server process for the selected repository and
    # inject the repo context through environment variables.
    # Earlier MCP attempts relied on a less explicit transport path. We now
    # start the Git MCP server inside the project for each request so the MCP
    # lifecycle is controlled by this app and easier to debug in production.
    port = _find_available_port()
    script_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["GIT_DIR"] = os.path.join(project_path, ".git")
    env["GIT_WORK_TREE"] = project_path

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "api.git_mcp_server",
            "--host",
            MCP_HOST,
            "--port",
            str(port),
        ],
        cwd=script_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process, f"http://{MCP_HOST}:{port}/mcp"


def _read_process_stderr(process: subprocess.Popen) -> str:
    if not process.stderr:
        return ""
    with suppress(Exception):
        return process.stderr.read().strip()
    return ""


async def _wait_for_server(server_url: str, process: subprocess.Popen) -> None:
    # Avoid the earlier stdio transport issue by waiting for the HTTP-based MCP
    # server to become reachable before opening the client session.
    deadline = time.monotonic() + MCP_STARTUP_TIMEOUT_SECONDS
    last_error: Exception | None = None
    port = int(server_url.rsplit(":", 1)[1].split("/", 1)[0])

    while time.monotonic() < deadline:
        if process.poll() is not None:
            details = _read_process_stderr(process) or "No server logs captured."
            raise RuntimeError(f"MCP server exited unexpectedly. {details}")

        try:
            reader, writer = await asyncio.open_connection(MCP_HOST, port)
            writer.close()
            await writer.wait_closed()
            return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.5)

    raise RuntimeError(f"MCP server startup timed out. {last_error}")


def _stop_process(process: subprocess.Popen | None) -> None:
    # Every request starts a short-lived MCP server, so we always terminate it
    # explicitly to avoid leaving orphaned localhost processes behind.
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def run_mcp_git_prompt(prompt: str, project_path: str):
    # Main MCP assistant flow:
    # 1. start the Git MCP server for the selected repo
    # 2. let the global AI model choose the best MCP tool
    # 3. execute that MCP tool
    # 4. optionally summarize the tool result back to the user
    if not os.path.isdir(project_path):
        return {"success": False, "message": f"MCP Error: Project path not found: {project_path}"}

    process = None
    try:
        process, server_url = await _start_mcp_server(project_path)
        await _wait_for_server(server_url, process)

        async with streamablehttp_client(server_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_response = await session.list_tools()
                available_tools = [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.inputSchema,
                    }
                    for tool in tools_response.tools
                ]

                selector_prompt = _build_tool_choice_prompt(prompt, available_tools)
                selection_raw = await backend.call_ai(selector_prompt, expect_json=True)
                try:
                    selection = _extract_json_block(selection_raw)
                except Exception as e:
                    return {"success": False, "message": f"MCP Error: Failed to parse AI tool selection: {e}"}

                if selection.get("mode") == "answer":
                    return {
                        "success": True,
                        "message": "AI Responded",
                        "output": (selection.get("message") or "").strip() or "No response generated.",
                    }

                tool_name = (selection.get("tool") or "").strip()
                tool_args = selection.get("arguments") or {}
                allowed_tools = {tool["name"] for tool in available_tools}
                if tool_name not in allowed_tools:
                    return {"success": False, "message": f"MCP Error: AI selected an unavailable tool: {tool_name or '(empty)'}"}

                result = await session.call_tool(tool_name, tool_args)
                tool_output = _extract_text_from_tool_result(result)

                if not tool_output:
                    tool_output = "Tool executed successfully with no text output."

                if tool_name == "git_diff_content":
                    final_output = tool_output
                else:
                    summary_prompt = _build_summary_prompt(prompt, tool_name, tool_args, tool_output)
                    try:
                        final_output = await backend.call_ai(summary_prompt, expect_json=False)
                    except Exception:
                        final_output = tool_output

                return {
                    "success": True,
                    "message": f"Executed '{tool_name}'",
                    "output": final_output,
                }
    except Exception as e:
        return {"success": False, "message": f"MCP Error: {str(e)}"}
    finally:
        _stop_process(process)
