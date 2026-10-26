"""
Execution engine: runs planned TestCases through Playwright MCP.

Status rules (never optimistic):
  * an assertion that does not hold, or an element that cannot be found -> Failed
  * a step that could not be interpreted, a blocked URL, or infrastructure
    trouble that survives the retry budget -> Blocked
  * steps after the first Failed/Blocked step, and tests never started or
    cancelled -> Not Executed
  * Passed only when every step ran and every derived assertion held
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from typing import Callable, Optional

from .models import (
    Action, Target, TestCase, TestCaseResult, StepResult, RunConfig, UsageMetrics, Evidence,
    PASSED, FAILED, BLOCKED, NOT_EXECUTED, IN_PROGRESS,
    FAIL_ASSERTION, FAIL_ELEMENT, FAIL_INTERPRET, FAIL_INFRA, FAIL_POLICY, FAIL_CANCELLED,
    SHOT_EVERY_STEP, SHOT_FINAL, SHOT_FAILURE, utcnow_iso,
)
from .playwright_mcp import MCPInfraError, PlaywrightMCPSession
from .security import NavigationPolicy, PolicyError, Redactor
from .step_parser import PLACEHOLDER_USERNAME

STATE_CHANGING = {"click", "double_click", "type", "select", "check", "uncheck", "press"}
POLL_INTERVAL_S = 0.75

_SNAP_LINE = re.compile(r'^\s*-\s+(?P<role>[\w-]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')
_REF = re.compile(r"\[ref=([^\]]+)\]")
_ROLE_PREFS = {
    "click": {"button", "link", "menuitem", "tab", "checkbox", "radio", "option", "switch", "treeitem", "cell", "row"},
    "double_click": {"button", "link", "cell", "row", "treeitem"},
    "type": {"textbox", "searchbox", "combobox", "spinbutton"},
    "assert_value": {"textbox", "searchbox", "combobox", "spinbutton"},
    "select": {"combobox", "listbox"},
    "check": {"checkbox", "radio", "switch"},
    "uncheck": {"checkbox", "switch"},
}
_INPUT_ACTIONS = {"type", "select", "check", "uncheck", "assert_value"}
_SKIP_ROLES = {"document", "main", "navigation", "banner", "contentinfo", "region", "list", "group", "form", "rowgroup", "table"}


class StepFailure(Exception):
    def __init__(self, status: str, category: str, message: str):
        super().__init__(message)
        self.status, self.category, self.message = status, category, message


class TestCancelled(Exception):
    pass


def parse_find_candidates(text: str) -> list[dict]:
    """Snapshot lines from browser_find/browser_snapshot -> [{role, name, text, ref}] in document order."""
    out = []
    for line in (text or "").splitlines():
        m = _SNAP_LINE.match(line)
        if not m:
            continue
        rest = m.group("rest") or ""
        ref = _REF.search(rest)
        if not ref:
            continue
        inline = ""
        if "]:" in rest:
            inline = rest.split("]:", 1)[1].strip()
        name = (m.group("name") or "").replace('\\"', '"')
        out.append({"role": m.group("role").lower(), "name": name, "text": inline, "ref": ref.group(1)})
    return out


def pick_candidate(cands: list[dict], target: Target, action_type: str) -> Optional[str]:
    want = " ".join(target.value.lower().split())
    prefs = _ROLE_PREFS.get(action_type, set())
    best, best_score = None, 0
    for c in cands:
        if c["role"] in _SKIP_ROLES:
            continue
        if action_type in _INPUT_ACTIONS and c["role"] not in prefs and c["role"] != target.role:
            continue   # never type into / select on a button or paragraph that happens to share the name
        name, inline = c["name"].lower(), c["text"].lower()
        score = 0
        if name == want:
            score = 6
        elif inline == want:
            score = 5
        elif want and name.startswith(want):
            score = 4
        elif want and want in name:
            score = 3
        elif want and want in inline:
            score = 2
        if not score:
            continue
        if target.role and c["role"] == target.role:
            score += 3
        elif c["role"] in prefs:
            score += 2
        if score > best_score:
            best, best_score = c["ref"], score
    return best if best_score > 0 else None


_CSS_FALLBACK_JS = """() => {
  const want = %s, roleHint = %s;
  const esc = (s) => s.replace(/["\\\\]/g, '\\\\$&');
  const variants = [want, want.replace(/\\s+/g, ''), want.replace(/\\s+/g, '_'), want.replace(/\\s+/g, '-')];
  const tags = roleHint === 'textbox' ? 'input,textarea' : (roleHint === 'combobox' ? 'select,input' : '*');
  for (const v of variants) {
    for (const attr of ['placeholder', 'name', 'id', 'aria-label', 'data-testid', 'title', 'value']) {
      const sel = tags.split(',').map(t => `${t}[${attr}="${esc(v)}" i]`).join(',');
      const hits = Array.from(document.querySelectorAll(sel)).filter(e => e.offsetParent !== null || e.getClientRects().length);
      if (hits.length === 1) return sel;
    }
  }
  return null;
}"""

_MASK_JS = """() => {
  const sels = %s; let n = 0;
  for (const s of sels) { try { document.querySelectorAll(s).forEach(e => { e.dataset.teMask = e.style.filter || ''; e.style.filter = 'blur(10px)'; n++; }); } catch (e) {} }
  return n;
}"""
_UNMASK_JS = """() => { document.querySelectorAll('[data-te-mask]').forEach(e => { e.style.filter = e.dataset.teMask; delete e.dataset.teMask; }); return true; }"""


SessionFactory = Callable[[int], "PlaywrightMCPSession"]


class ExecutionEngine:
    def __init__(self, run_id: str, config: RunConfig, policy: NavigationPolicy, evidence_store,
                 usage: UsageMetrics, redactor: Redactor, planner=None,
                 credentials: Optional[dict] = None, session_factory: Optional[SessionFactory] = None,
                 on_event: Optional[Callable[[dict], None]] = None,
                 on_result: Optional[Callable[[TestCaseResult], None]] = None,
                 cancel_event: Optional[threading.Event] = None, work_root: str = "."):
        self.run_id = run_id
        self.cfg = config
        self.policy = policy
        self.evidence = evidence_store
        self.usage = usage
        self.redact = redactor
        self.planner = planner
        self.credentials = credentials or {}
        self.on_event = on_event or (lambda e: None)
        self.on_result = on_result or (lambda r: None)
        self.cancel_event = cancel_event or threading.Event()
        self.work_root = work_root
        self.session_factory = session_factory or self._default_session
        self._worker_errors: list[str] = []

    # ── session ─────────────────────────────────────────────────────────
    def _default_session(self, worker: int) -> PlaywrightMCPSession:
        secrets = {}
        if self.credentials.get("password"):
            from .step_parser import SECRET_PASSWORD
            secrets[SECRET_PASSWORD] = self.credentials["password"]
        return PlaywrightMCPSession(
            work_dir=os.path.join(self.work_root, f"worker{worker}"),
            headless=self.cfg.headless,
            action_timeout_ms=self.cfg.action_timeout_ms,
            navigation_timeout_ms=self.cfg.navigation_timeout_ms,
            secrets=secrets,
            blocked_origins=self.policy.blocked_origins_arg(),
            on_operation=self._count_op,
        )

    def _count_op(self):
        self.usage.browser_operations += 1

    # ── orchestration ───────────────────────────────────────────────────
    async def run(self, cases: list[TestCase]) -> list[TestCaseResult]:
        results: dict[str, TestCaseResult] = {}
        queue: asyncio.Queue = asyncio.Queue()
        for tc in cases:
            queue.put_nowait(tc)
        workers = max(1, min(self.cfg.concurrency, len(cases) or 1))
        await asyncio.gather(*(self._worker(i, queue, results) for i in range(workers)))
        # Anything a dead worker pool never reached.
        reason = "; ".join(sorted(set(self._worker_errors)))[:500]
        for tc in cases:
            if tc.key not in results:
                cancelled = self.cancel_event.is_set()
                r = self._new_result(tc)
                r.status = NOT_EXECUTED if cancelled else BLOCKED
                r.failure_category = FAIL_CANCELLED if cancelled else FAIL_INFRA
                r.failure_reason = "Run cancelled before this test started" if cancelled else \
                    self.redact(f"Playwright MCP unavailable: {reason or 'unknown error'}")
                results[tc.key] = r
                self.on_result(r)
        return [results[tc.key] for tc in cases]

    async def _worker(self, idx: int, queue: asyncio.Queue, results: dict):
        session = None
        try:
            while not self.cancel_event.is_set():
                try:
                    tc: TestCase = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if session is None:
                    session = self.session_factory(idx)
                    try:
                        await session.__aenter__()
                    except MCPInfraError as exc:
                        self._worker_errors.append(str(exc))
                        session = None
                        return   # the claimed test (and any left) are reported Blocked by run()
                result, session = await self._run_with_retries(idx, session, tc)
                results[tc.key] = result
                self.on_result(result)
        finally:
            if session is not None:
                await session.__aexit__(None, None, None)

    async def _run_with_retries(self, idx, session, tc: TestCase):
        attempt = 0
        while True:
            result = self._new_result(tc)
            result.retry_count = attempt
            state = {"mutated": False}
            try:
                await asyncio.wait_for(self._execute_case(session, tc, result, state), timeout=self.cfg.test_timeout_s)
                return result, session
            except asyncio.TimeoutError:
                self._finish_blocked(result, FAIL_INFRA, f"Test exceeded the {self.cfg.test_timeout_s}s time limit")
                await self._safe_reset(session)
                return result, session
            except MCPInfraError as exc:
                msg = self.redact(str(exc))[:400]
                # Restart the browser: the old process may be gone.
                await session.__aexit__(None, None, None)
                new_session = self.session_factory(idx)
                try:
                    await new_session.__aenter__()
                    session = new_session
                except MCPInfraError as start_exc:
                    self._finish_blocked(result, FAIL_INFRA, f"{msg}; browser restart failed: {start_exc}")
                    self._worker_errors.append(str(start_exc))
                    return result, _DeadSession()
                if attempt < self.cfg.max_infra_retries and not state["mutated"]:
                    attempt += 1
                    self.usage.retries += 1
                    self.on_event({"type": "retry", "test_key": tc.key, "attempt": attempt, "reason": msg})
                    continue
                note = "" if not state["mutated"] else \
                    " (not retried automatically: the test had already changed application state)"
                self._finish_blocked(result, FAIL_INFRA, msg + note)
                return result, session

    def _new_result(self, tc: TestCase) -> TestCaseResult:
        return TestCaseResult(
            run_id=self.run_id, test_key=tc.key, title=tc.title, source=tc.source,
            expected=tc.expected_result or (tc.steps[-1].expected if tc.steps else ""),
            steps=[StepResult(number=s.number, action_text=s.action_text, expected=s.expected) for s in tc.steps],
        )

    def _finish_blocked(self, result: TestCaseResult, category: str, reason: str):
        current = next((s for s in result.steps if s.status == IN_PROGRESS), None)
        if current:
            current.status, current.error, current.failure_category = BLOCKED, reason, category
            result.failed_step = current.number
        for s in result.steps:
            if s.status == IN_PROGRESS:
                s.status = NOT_EXECUTED
        result.status, result.failure_category, result.failure_reason = BLOCKED, category, reason
        result.actual = result.actual or reason
        self._close_timing(result)

    def _close_timing(self, result: TestCaseResult):
        result.ended_at = utcnow_iso()
        if result.started_at and not result.duration_ms:
            result.duration_ms = int((time.monotonic() - getattr(result, "_t0", time.monotonic())) * 1000)

    async def _safe_reset(self, session):
        try:
            await session.reset()
        except Exception:
            pass

    # ── one test case ───────────────────────────────────────────────────
    async def _execute_case(self, session, tc: TestCase, result: TestCaseResult, state: dict):
        t0 = time.monotonic()
        result._t0 = t0  # type: ignore[attr-defined]
        result.started_at = utcnow_iso()
        result.status = IN_PROGRESS
        self.on_event({"type": "test_started", "test_key": tc.key, "title": tc.title})

        if not tc.valid:
            reason = "Invalid test case: " + "; ".join(tc.issues)
            for s in result.steps:
                s.status = NOT_EXECUTED
            result.status, result.failure_category, result.failure_reason = BLOCKED, FAIL_INTERPRET, reason
            result.actual = reason
            result.ended_at = utcnow_iso()
            result.duration_ms = int((time.monotonic() - t0) * 1000)
            return

        stop: Optional[StepFailure] = None
        for step, sres in zip(tc.steps, result.steps):
            if stop is not None:
                sres.status = NOT_EXECUTED
                continue
            if self.cancel_event.is_set():
                stop = StepFailure(NOT_EXECUTED, FAIL_CANCELLED, f"Cancelled before step {step.number}")
                sres.status = NOT_EXECUTED
                continue
            sres.status = IN_PROGRESS
            sres.started_at = utcnow_iso()
            ts = time.monotonic()
            ops_before = self.usage.browser_operations
            try:
                if not step.actions:
                    raise StepFailure(BLOCKED, FAIL_INTERPRET, step.plan_error or "Step could not be interpreted")
                outcomes = []
                for action in step.actions:
                    outcomes.append(await self._perform(session, action, step.action_text))
                    if action.type in STATE_CHANGING:
                        state["mutated"] = True
                sres.status = PASSED
                sres.actual = self.redact("; ".join(o for o in outcomes if o))[:1000]
            except StepFailure as sf:
                sres.status, sres.failure_category = sf.status, sf.category
                sres.error = self.redact(sf.message)[:1000]
                sres.actual = sres.error
                stop = sf
                result.failed_step = step.number
            finally:
                sres.duration_ms = int((time.monotonic() - ts) * 1000)
                sres.operations = self.usage.browser_operations - ops_before
            if sres.status == IN_PROGRESS:      # MCPInfraError propagates; leave the marker for _finish_blocked
                continue
            mode = self.cfg.screenshot_mode
            if mode == SHOT_EVERY_STEP or (mode == SHOT_FAILURE and sres.status in (FAILED, BLOCKED)
                                           and sres.failure_category not in (FAIL_INTERPRET, FAIL_POLICY)):
                await self._capture(session, result, sres, step.number, sres.status)
            self.on_event({"type": "step_finished", "test_key": tc.key, "step": step.number, "status": sres.status})

        if stop is None:
            result.status = PASSED
            last = next((s.actual for s in reversed(result.steps) if s.actual), "")
            result.actual = f"All {len(result.steps)} steps completed. {last}".strip()
        else:
            result.status = stop.status
            result.failure_category = stop.category
            result.failure_reason = self.redact(stop.message)[:1000]
            result.actual = result.failure_reason

        if self.cfg.screenshot_mode == SHOT_FINAL and result.status != NOT_EXECUTED:
            await self._capture(session, result, None, None, result.status)
        result.ended_at = utcnow_iso()
        result.duration_ms = int((time.monotonic() - t0) * 1000)
        if self.cfg.isolate_tests:
            await self._safe_reset(session)
        self.on_event({"type": "test_finished", "test_key": tc.key, "status": result.status})

    # ── actions ─────────────────────────────────────────────────────────
    async def _perform(self, session, a: Action, step_text: str) -> str:
        t = a.type
        if t == "note":
            return "Informational step (no browser action)"
        if t == "navigate":
            try:
                url = self.policy.resolve(a.value)
            except PolicyError as exc:
                raise StepFailure(BLOCKED, FAIL_POLICY, str(exc))
            r = await session.call("browser_navigate", {"url": url})
            if not r.ok:
                raise StepFailure(FAILED, FAIL_ASSERTION, f"Navigation to {url} failed: {_first_error(r.text)}")
            self._check_landing(r.page_url)
            return f"Opened {r.page_url or url}"
        if t == "wait":
            await asyncio.sleep(min(float(a.value), 30))
            return f"Waited {a.value}s"
        if t == "press":
            r = await session.call("browser_press_key", {"key": a.value})
            if not r.ok:
                raise StepFailure(FAILED, FAIL_ASSERTION, f"Pressing {a.value} failed: {_first_error(r.text)}")
            return f"Pressed {a.value}"
        if t in ("assert_text", "wait_for_text"):
            if await self._poll_text(session, a.value, present=True):
                return f'"{a.value}" is displayed'
            raise StepFailure(FAILED, FAIL_ASSERTION, f'Expected text "{a.value}" was not found on the page')
        if t == "assert_not_text":
            await asyncio.sleep(0.5)
            if await self._poll_text(session, a.value, present=False):
                return f'"{a.value}" is not displayed'
            raise StepFailure(FAILED, FAIL_ASSERTION, f'Text "{a.value}" is displayed but should not be')
        if t in ("assert_url", "assert_title"):
            key = "url" if t == "assert_url" else "title"
            got = await self._poll_state(session, key, a.value)
            if got[0]:
                return f'Page {key} contains "{a.value}"'
            raise StepFailure(FAILED, FAIL_ASSERTION, f'Expected page {key} to contain "{a.value}" but it was "{got[1]}"')

        target = await self._resolve(session, a.target, t, step_text)
        desc = a.target.describe() if a.target else ""
        if t == "assert_visible":
            return f"{desc} is visible"
        if t == "assert_value":
            val, err = await session.evaluate_json("(el) => (el.value !== undefined ? el.value : el.textContent)", target)
            if err:
                raise StepFailure(FAILED, FAIL_ASSERTION, f"Could not read value of {desc}: {_first_error(err)}")
            if a.value.lower() in str(val or "").lower():
                return f'{desc} contains "{a.value}"'
            raise StepFailure(FAILED, FAIL_ASSERTION, f'{desc} has value "{val}", expected "{a.value}"')
        if t in ("click", "double_click", "hover"):
            tool = "browser_hover" if t == "hover" else "browser_click"
            args = {"target": target, "element": desc}
            if t == "double_click":
                args["doubleClick"] = True
            r = await session.call(tool, args)
            if not r.ok:
                raise StepFailure(FAILED, FAIL_ELEMENT, f"Could not {t.replace('_', ' ')} {desc}: {_first_error(r.text)}")
            self._check_landing(r.page_url)
            return f"{'Hovered' if t == 'hover' else 'Clicked'} {desc}"
        if t == "type":
            value = a.value
            if PLACEHOLDER_USERNAME in value:
                if not self.credentials.get("username"):
                    raise StepFailure(BLOCKED, FAIL_INTERPRET, "Step needs the application username but none is configured for the project")
                value = value.replace(PLACEHOLDER_USERNAME, self.credentials["username"])
            if a.secret and not self.credentials.get("password"):
                raise StepFailure(BLOCKED, FAIL_INTERPRET, "Step needs the application password but none is configured for the project")
            r = await session.call("browser_type", {"target": target, "text": value, "element": desc})
            if not r.ok:
                raise StepFailure(FAILED, FAIL_ELEMENT, f"Could not type into {desc}: {_first_error(r.text)}")
            shown = "******" if a.secret else value
            return f'Entered "{shown}" in {desc}'
        if t == "select":
            r = await session.call("browser_select_option", {"target": target, "values": [a.value], "element": desc})
            if not r.ok:
                raise StepFailure(FAILED, FAIL_ELEMENT, f'Could not select "{a.value}" in {desc}: {_first_error(r.text)}')
            return f'Selected "{a.value}" in {desc}'
        if t in ("check", "uncheck"):
            checked, err = await session.evaluate_json("(el) => !!el.checked", target)
            if err:
                raise StepFailure(FAILED, FAIL_ELEMENT, f"Could not read {desc}: {_first_error(err)}")
            if bool(checked) != (t == "check"):
                r = await session.call("browser_click", {"target": target, "element": desc})
                if not r.ok:
                    raise StepFailure(FAILED, FAIL_ELEMENT, f"Could not {t} {desc}: {_first_error(r.text)}")
            return f"{'Checked' if t == 'check' else 'Unchecked'} {desc}"
        raise StepFailure(BLOCKED, FAIL_INTERPRET, f"Unsupported action '{t}'")

    def _check_landing(self, page_url: str):
        """Redirects can't be stopped by --allowed-origins, so verify where we ended up."""
        if page_url and page_url.startswith(("http://", "https://")):
            try:
                self.policy.check(page_url)
            except PolicyError as exc:
                raise StepFailure(BLOCKED, FAIL_POLICY, f"Browser was redirected to a disallowed location. {exc}")

    async def _poll_text(self, session, text: str, present: bool) -> bool:
        deadline = time.monotonic() + self.cfg.action_timeout_ms / 1000.0
        while True:
            r = await session.call("browser_find", {"text": text})
            found = r.ok and "No matches found" not in r.text
            if found == present:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _poll_state(self, session, key: str, want: str) -> tuple[bool, str]:
        deadline = time.monotonic() + self.cfg.action_timeout_ms / 1000.0
        got = ""
        while True:
            state = await session.page_state()
            got = str(state.get(key, ""))
            if want.lower() in got.lower():
                return True, got
            if time.monotonic() >= deadline:
                return False, got
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _resolve(self, session, target: Optional[Target], action_type: str, step_text: str) -> str:
        if target is None:
            raise StepFailure(BLOCKED, FAIL_INTERPRET, "Action has no target element")
        if target.kind == "css":
            return target.value
        if target.kind == "xpath":
            return "xpath=" + target.value
        if target.kind == "testid":
            return f'[data-testid="{target.value}"]'
        if target.kind == "ref":
            return target.value

        # Semantic target: look it up in the accessibility snapshot (no waiting on bad selectors).
        deadline = time.monotonic() + self.cfg.action_timeout_ms / 1000.0
        queries = [target.value]
        compact = target.value.replace(" ", "")
        if compact != target.value:
            queries.append(compact)
        while True:
            for q in queries:
                r = await session.call("browser_find", {"text": q})
                if r.ok:
                    ref = pick_candidate(parse_find_candidates(r.text), target, action_type)
                    if ref:
                        return ref
            if action_type in ("type", "select", "assert_value", "check", "uncheck"):
                sel, _ = await session.evaluate_json(_CSS_FALLBACK_JS % (json.dumps(target.value), json.dumps(target.role or "")))
                if isinstance(sel, str) and sel:
                    return sel
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_INTERVAL_S)

        if self.cfg.use_ai_locator_recovery and self.planner is not None:
            snap = await session.call("browser_snapshot", {})
            if snap.ok:
                ref = await self.planner.recover_locator(step_text, f"{target.describe()} (for {action_type})", _snapshot_body(snap.text))
                if ref:
                    return ref
        raise StepFailure(FAILED, FAIL_ELEMENT, f"Element not found: {target.describe()}")

    # ── evidence ────────────────────────────────────────────────────────
    async def _capture(self, session, result: TestCaseResult, sres: Optional[StepResult],
                       step_number: Optional[int], status: str):
        try:
            if self.cfg.mask_selectors:
                await session.evaluate_json(_MASK_JS % json.dumps(self.cfg.mask_selectors))
            data = await session.screenshot()
            if self.cfg.mask_selectors:
                await session.evaluate_json(_UNMASK_JS)
        except MCPInfraError:
            raise
        except Exception as exc:
            result.actual = (result.actual + f" [screenshot failed: {exc}]").strip()
            return
        if not data:
            if sres is not None:
                sres.actual = (sres.actual + " [screenshot unavailable]").strip()
            return
        if self.evidence.is_duplicate(self.run_id, result.test_key, data):
            return
        label = f"{result.test_key} - {'final' if step_number is None else 'step ' + str(step_number)} - {status}"
        ev: Evidence = self.evidence.put(self.run_id, result.test_key, step_number, status, data, label)
        result.evidence.append(ev)
        if sres is not None:
            sres.evidence_ids.append(ev.evidence_id)
        self.usage.screenshots += 1
        self.usage.evidence_bytes += ev.size_bytes


class _DeadSession:
    """Placeholder after an unrecoverable browser failure: every call is an infra error."""
    tool_names: set = set()

    async def __aenter__(self):
        raise MCPInfraError("Playwright MCP is not running")

    async def __aexit__(self, *exc):
        return None

    async def call(self, *a, **k):
        raise MCPInfraError("Playwright MCP is not running")

    async def reset(self):
        return None


def _first_error(text: str) -> str:
    m = re.search(r"### Error\s*\n(.+)", text or "")
    line = (m.group(1) if m else (text or "").strip().splitlines()[0] if (text or "").strip() else "unknown error")
    return line.strip()[:300]


def _snapshot_body(text: str) -> str:
    m = re.search(r"```yaml\s*\n(.*?)```", text or "", re.S)
    return (m.group(1) if m else text or "").strip()
