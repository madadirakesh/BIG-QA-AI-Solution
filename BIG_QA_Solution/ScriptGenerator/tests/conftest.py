"""
Shared fixtures for the Test Execution tests.

FakeMCPSession mimics @playwright/mcp 0.0.83 responses (formats captured from
the real server). Tests using it are unit/integration tests of OUR code with a
mocked MCP; tests/test_execution/test_live_playwright_mcp.py is the only one
that talks to a real Playwright MCP server.
"""
import hashlib
import io
import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_GENERATOR = os.path.dirname(HERE)
ROOT = os.path.dirname(SCRIPT_GENERATOR)
for p in (SCRIPT_GENERATOR, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from test_execution.playwright_mcp import MCPInfraError, ToolResult  # noqa: E402


def png_bytes(seed: str) -> bytes:
    from PIL import Image
    h = hashlib.sha256(seed.encode()).digest()
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (h[0], h[1], h[2])).save(buf, "PNG")
    return buf.getvalue()


class FakeApp:
    """A tiny scripted 'application': pages with accessible elements and click behaviours."""

    def __init__(self, base="http://app.test/login"):
        self.base = base
        self.pages = {
            "http://app.test/login": {
                "title": "Demo Login",
                "elements": [
                    {"role": "textbox", "name": "Username", "ref": "e2"},
                    {"role": "textbox", "name": "Password", "ref": "e3"},
                    {"role": "combobox", "name": "Role", "ref": "e4"},
                    {"role": "checkbox", "name": "Remember me", "ref": "e5"},
                    {"role": "button", "name": "Login", "ref": "e6", "on_click": "login"},
                ],
                "texts": ["Sign in"],
            },
            "http://app.test/dashboard": {"title": "Dashboard", "elements": [
                {"role": "link", "name": "Logout", "ref": "e9", "on_click": "goto:http://app.test/login"}],
                "texts": ["Welcome back, alice"]},
        }


class FakeMCPSession:
    instances = []

    def __init__(self, app: FakeApp = None, password="s3cret!", fail_plan=None, slow_tools=None):
        self.app = app or FakeApp()
        self.password = password
        self.url = "about:blank"
        self.values = {}
        self.checked = {}
        self.extra_texts = []
        self.calls = []
        self.resets = 0
        self.fail_plan = list(fail_plan or [])   # [(tool, nth_call)] -> raise MCPInfraError
        self.slow_tools = slow_tools or {}
        self.tool_names = {"browser_navigate", "browser_click", "browser_type", "browser_find", "browser_evaluate",
                           "browser_take_screenshot", "browser_snapshot", "browser_close", "browser_select_option",
                           "browser_press_key", "browser_hover"}
        self.entered = False
        FakeMCPSession.instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.entered = False

    @property
    def page(self):
        return self.app.pages.get(self.url, {"title": "", "elements": [], "texts": []})

    def _el(self, target):
        for e in self.page["elements"]:
            if e["ref"] == target:
                return e
        return None

    async def call(self, tool, args, timeout=90):
        import asyncio
        await asyncio.sleep(0)   # real MCP calls are I/O and yield to other workers
        self.calls.append((tool, args))
        n = sum(1 for t, _ in self.calls if t == tool)
        for t, nth in list(self.fail_plan):
            if t == tool and nth == n:
                self.fail_plan.remove((t, nth))
                raise MCPInfraError(f"{tool}: Target page, context or browser has been closed")
        if tool in self.slow_tools:
            await asyncio.sleep(self.slow_tools[tool])
        if tool == "browser_navigate":
            if args["url"] not in self.app.pages:
                return ToolResult(False, "### Error\nError: net::ERR_NAME_NOT_RESOLVED")
            self.url = args["url"]
            self.extra_texts = []
            return ToolResult(True, f"### Page\n- Page URL: {self.url}", page_url=self.url)
        if tool == "browser_find":
            want = args["text"].lower()
            lines = []
            for e in self.page["elements"]:
                if want in e["name"].lower() or want in str(self.values.get(e["ref"], "")).lower():
                    lines.append(f'- {e["role"]} "{e["name"]}" [ref={e["ref"]}]')
            for i, t in enumerate(self.page["texts"] + self.extra_texts):
                if want in t.lower():
                    lines.append(f"- paragraph [ref=p{i}]: {t}")
            if not lines:
                return ToolResult(True, f'### Result\nNo matches found for "{args["text"]}".')
            return ToolResult(True, f'### Result\nFound {len(lines)} matches for "{args["text"]}":\n\n' + "\n".join(lines))
        if tool in ("browser_click", "browser_hover"):
            el = self._el(args["target"])
            if el is None:
                return ToolResult(False, f'### Error\nError: "{args["target"]}" does not match any elements.')
            if tool == "browser_click":
                if el["role"] == "checkbox":
                    self.checked[el["ref"]] = not self.checked.get(el["ref"], False)
                beh = el.get("on_click", "")
                if beh == "login":
                    if self.values.get("e2") == "alice" and self.values.get("e3") == self.password:
                        self.url = "http://app.test/dashboard"
                    else:
                        self.extra_texts.append("Invalid credentials")
                elif beh.startswith("goto:"):
                    self.url = beh[5:]
            return ToolResult(True, f"### Page\n- Page URL: {self.url}", page_url=self.url)
        if tool == "browser_type":
            el = self._el(args["target"])
            if el is None:
                return ToolResult(False, f'### Error\nError: "{args["target"]}" does not match any elements.')
            text = args["text"]
            self.values[el["ref"]] = self.password if text == "TE_APP_PASSWORD" else text
            return ToolResult(True, "### Ran Playwright code")
        if tool == "browser_select_option":
            el = self._el(args["target"])
            if el is None:
                return ToolResult(False, "### Error\nError: no element")
            self.values[el["ref"]] = args["values"][0]
            return ToolResult(True, "ok")
        if tool == "browser_press_key":
            return ToolResult(True, "ok")
        if tool == "browser_evaluate":
            fn = args["function"]
            if "location.href" in fn:
                res = {"url": self.url, "title": self.page["title"]}
            elif "el.checked" in fn:
                res = self.checked.get(args.get("target"), False)
            elif "el.value" in fn:
                res = self.values.get(args.get("target"), "")
            else:
                res = None
            return ToolResult(True, "### Result\n" + json.dumps(res))
        if tool == "browser_take_screenshot":
            state = f"{self.url}|{self.extra_texts}|{sorted(self.values.items())}"
            return ToolResult(True, "### Result\n- [Screenshot of viewport]", image=png_bytes(state))
        if tool == "browser_snapshot":
            body = "\n".join(f'- {e["role"]} "{e["name"]}" [ref={e["ref"]}]' for e in self.page["elements"])
            return ToolResult(True, f"### Snapshot\n```yaml\n{body}\n```")
        if tool == "browser_close":
            self.resets += 1
            self.url, self.values, self.checked, self.extra_texts = "about:blank", {}, {}, []
            return ToolResult(True, "closed")
        return ToolResult(False, "### Error\nunknown tool")

    async def page_state(self):
        return {"url": self.url, "title": self.page["title"]}

    async def evaluate_json(self, function, target=""):
        r = await self.call("browser_evaluate", {"function": function, **({"target": target} if target else {})})
        m = re.search(r"### Result\s*\n(.*)", r.text, re.S)
        return json.loads(m.group(1)) if m else None, ""

    async def screenshot(self):
        r = await self.call("browser_take_screenshot", {"type": "png", "scale": "css"})
        return r.image

    async def reset(self):
        await self.call("browser_close", {})


@pytest.fixture
def fake_app():
    return FakeApp()


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeMCPSession.instances.clear()
    yield
