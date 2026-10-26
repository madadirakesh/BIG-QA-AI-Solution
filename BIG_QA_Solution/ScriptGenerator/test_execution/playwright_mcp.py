"""
Thin client for the official Playwright MCP server (@playwright/mcp), started
over stdio with the app's MCP Python SDK.

Verified tool contract (@playwright/mcp 0.0.83): browser_navigate{url},
browser_click{target}, browser_type{target,text,submit?}, browser_select_option
{target,values}, browser_hover{target}, browser_press_key{key}, browser_find
{text}, browser_snapshot{depth?}, browser_evaluate{function,target?},
browser_take_screenshot{type,scale}, browser_close{}. `target` takes either a
snapshot ref or a unique selector. Failures come back with isError=True.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Optional

PLAYWRIGHT_MCP_PACKAGE = os.environ.get("PLAYWRIGHT_MCP_PACKAGE") or "@playwright/mcp@0.0.83"
MCP_CALL_TIMEOUT_S = 90
MCP_START_TIMEOUT_S = 180   # first run downloads the package and browser


class MCPInfraError(RuntimeError):
    """The MCP server/browser itself failed (not the application under test)."""


@dataclass
class ToolResult:
    ok: bool
    text: str
    image: Optional[bytes] = None
    page_url: str = ""


_PAGE_URL = re.compile(r"^- Page URL:\s*(\S+)", re.M)
_INFRA_MARKERS = ("browser has been closed", "target closed", "target page, context or browser has been closed",
                  "browser is not installed", "executable doesn't exist", "connection closed", "econnrefused",
                  "protocol error", "session closed")


def _attr(obj, *names):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return None


def is_infra_error(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _INFRA_MARKERS)


def build_server_args(work_dir: str, headless: bool, action_timeout_ms: int, navigation_timeout_ms: int,
                      secrets_file: Optional[str], blocked_origins: str) -> list[str]:
    args = ["-y", PLAYWRIGHT_MCP_PACKAGE, "--isolated", "--output-dir", work_dir,
            "--timeout-action", str(action_timeout_ms), "--timeout-navigation", str(navigation_timeout_ms),
            # Response snapshots are never needed by the deterministic engine; the
            # resolver asks for them explicitly (browser_find / browser_snapshot).
            "--snapshot-mode", "none", "--codegen", "none", "--viewport-size", "1366x768"]
    if headless:
        args.append("--headless")
    if os.environ.get("TEST_EXECUTION_NO_SANDBOX", "").lower() in ("1", "true", "yes"):
        args.append("--no-sandbox")   # containers without user namespaces (see DOCKER.md)
    if secrets_file:
        args += ["--secrets", secrets_file]
    if blocked_origins:
        args += ["--blocked-origins", blocked_origins]
    return args


class PlaywrightMCPSession:
    """One MCP server process == one isolated browser. Use as an async context manager."""

    def __init__(self, work_dir: str, headless: bool = True, action_timeout_ms: int = 8000,
                 navigation_timeout_ms: int = 30000, secrets: Optional[dict[str, str]] = None,
                 blocked_origins: str = "", on_operation=None):
        self.work_dir = work_dir
        self.headless = headless
        self.action_timeout_ms = action_timeout_ms
        self.navigation_timeout_ms = navigation_timeout_ms
        self.secrets = {k: v for k, v in (secrets or {}).items() if v}
        self.blocked_origins = blocked_origins
        self.on_operation = on_operation or (lambda: None)
        self._stack: Optional[AsyncExitStack] = None
        self._session = None
        self._secrets_file: Optional[str] = None
        self.tool_names: set[str] = set()

    async def __aenter__(self):
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client, StdioServerParameters

        os.makedirs(self.work_dir, exist_ok=True)
        if self.secrets:
            self._secrets_file = os.path.join(self.work_dir, ".secrets.env")
            with open(self._secrets_file, "w", encoding="utf-8") as f:
                for k, v in self.secrets.items():
                    f.write(f"{k}={v}\n")
        npx = shutil.which("npx") or shutil.which("npx.cmd")
        if not npx:
            raise MCPInfraError("Node.js/npx was not found. Install Node.js 18+ to run Playwright MCP.")
        params = StdioServerParameters(
            command="npx",
            args=build_server_args(self.work_dir, self.headless, self.action_timeout_ms,
                                   self.navigation_timeout_ms, self._secrets_file, self.blocked_origins),
            cwd=self.work_dir,
        )
        self._stack = AsyncExitStack()
        try:
            errlog = open(os.path.join(self.work_dir, "mcp-server.log"), "a", encoding="utf-8")
            self._stack.callback(errlog.close)
            streams = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
            self._session = await self._stack.enter_async_context(ClientSession(*streams[:2]))
            await asyncio.wait_for(self._session.initialize(), timeout=MCP_START_TIMEOUT_S)
            tools = await asyncio.wait_for(self._session.list_tools(), timeout=MCP_START_TIMEOUT_S)
            self.tool_names = {t.name for t in tools.tools}
        except BaseException as exc:
            await self._close()
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            raise MCPInfraError(f"Could not start Playwright MCP: {exc}") from exc
        missing = {"browser_navigate", "browser_click", "browser_type", "browser_find",
                   "browser_evaluate", "browser_take_screenshot"} - self.tool_names
        if missing:
            await self._close()
            raise MCPInfraError(f"Playwright MCP is missing required tools: {', '.join(sorted(missing))}")
        return self

    async def __aexit__(self, *exc):
        await self._close()

    async def _close(self):
        try:
            if self._stack is not None:
                await self._stack.aclose()
        except BaseException:
            pass
        finally:
            self._stack = None
            self._session = None
            if self._secrets_file and os.path.exists(self._secrets_file):
                try:
                    os.remove(self._secrets_file)
                except OSError:
                    pass

    async def call(self, tool: str, args: dict, timeout: float = MCP_CALL_TIMEOUT_S) -> ToolResult:
        if self._session is None:
            raise MCPInfraError("Playwright MCP session is not running")
        if tool not in self.tool_names:
            raise MCPInfraError(f"Playwright MCP tool '{tool}' is not available")
        self.on_operation()
        try:
            res = await asyncio.wait_for(self._session.call_tool(tool, args), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise MCPInfraError(f"{tool} timed out after {int(timeout)}s") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MCPInfraError(f"{tool} failed: {exc}") from exc
        texts, image = [], None
        for item in _attr(res, "content") or []:
            t = getattr(item, "text", None)
            if t:
                texts.append(t)
            data = getattr(item, "data", None)
            mime = _attr(item, "mime_type", "mimeType") or ""
            if data and mime.startswith("image/"):
                image = base64.b64decode(data)
        text = "\n".join(texts)
        is_error = bool(_attr(res, "is_error", "isError"))
        if is_error and is_infra_error(text):
            raise MCPInfraError(text[:500])
        m = _PAGE_URL.search(text)
        return ToolResult(ok=not is_error, text=text, image=image, page_url=m.group(1) if m else "")

    # Convenience wrappers ------------------------------------------------
    async def page_state(self) -> dict:
        r = await self.call("browser_evaluate", {"function": "() => ({url: location.href, title: document.title})"})
        m = re.search(r"### Result\s*\n(.*?)(?:\n###|\Z)", r.text, re.S)
        try:
            return json.loads(m.group(1).strip()) if m else {}
        except ValueError:
            return {}

    async def evaluate_json(self, function: str, target: str = ""):
        args = {"function": function}
        if target:
            args["target"] = target
        r = await self.call("browser_evaluate", args)
        if not r.ok:
            return None, r.text
        m = re.search(r"### Result\s*\n(.*?)(?:\n###|\Z)", r.text, re.S)
        try:
            return (json.loads(m.group(1).strip()) if m else None), ""
        except ValueError:
            return (m.group(1).strip() if m else None), ""

    async def screenshot(self) -> Optional[bytes]:
        r = await self.call("browser_take_screenshot", {"type": "png", "scale": "css"})
        return r.image if r.ok else None

    async def reset(self):
        """Fresh browser storage for the next test (verified: browser_close discards the context)."""
        if "browser_close" in self.tool_names:
            await self.call("browser_close", {})
