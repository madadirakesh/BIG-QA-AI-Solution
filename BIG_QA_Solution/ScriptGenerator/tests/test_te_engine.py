import asyncio
import json
import threading

import pytest

from conftest import FakeMCPSession
from test_execution.ai_planner import AIPlanner, estimate_tokens
from test_execution.engine import ExecutionEngine, parse_find_candidates, pick_candidate
from test_execution.evidence import LocalEvidenceStore
from test_execution.models import (
    TestCase, TestStep, RunConfig, UsageMetrics, Target,
    PASSED, FAILED, BLOCKED, NOT_EXECUTED,
    FAIL_ASSERTION, FAIL_ELEMENT, FAIL_INTERPRET, FAIL_INFRA, FAIL_POLICY,
)
from test_execution.security import NavigationPolicy, Redactor
from test_execution.step_parser import plan_test_case
from test_execution.store import ExecutionStore

BASE = "http://app.test/login"


def case(key, steps):
    tc = TestCase(key=key, title=key, source="excel",
                  steps=[TestStep(i + 1, a, e) for i, (a, e) in enumerate(steps)])
    return plan_test_case(tc, True, True)


LOGIN_OK = [("Open the application", "Page title is 'Demo Login'"), ("Enter valid username", ""),
            ("Enter valid password", ""), ("Check the Remember me checkbox", ""),
            ("Click on Login button", "'Welcome back' is displayed")]


def make_engine(tmp_path, cfg=None, factory=None, planner=None, cancel=None, events=None):
    usage = UsageMetrics()
    cfg = cfg or RunConfig.from_dict({"screenshot_mode": "none", "action_timeout_ms": 1000})
    store = LocalEvidenceStore(str(tmp_path / "ev"))
    eng = ExecutionEngine(
        "TE-20261023-abcdef12", cfg, NavigationPolicy(BASE, resolve_dns=False), store, usage, Redactor(["s3cret!"]),
        planner=planner, credentials={"username": "alice", "password": "s3cret!"},
        session_factory=factory or (lambda i: FakeMCPSession()), cancel_event=cancel,
        on_event=(events.append if events is not None else None), work_root=str(tmp_path))
    return eng, usage, store


def run(eng, cases):
    return asyncio.run(eng.run(cases))


def test_passing_case_uses_no_ai_and_hides_password(tmp_path):
    eng, usage, _ = make_engine(tmp_path)
    [r] = run(eng, [case("TC1", LOGIN_OK)])
    assert r.status == PASSED, r.failure_reason
    assert all(s.status == PASSED for s in r.steps)
    assert usage.model_calls == 0
    dump = json.dumps(r.to_dict())
    assert "s3cret!" not in dump and "******" in dump


def test_assertion_failure_marks_rest_not_executed(tmp_path):
    eng, _, _ = make_engine(tmp_path)
    [r] = run(eng, [case("TC2", [("Open the application", ""), ("Enter 'bob' in Username field", ""),
                                 ("Click on Login button", "'Welcome back' is displayed"), ("Click Logout", "")])])
    assert r.status == FAILED and r.failure_category == FAIL_ASSERTION and r.failed_step == 3
    assert [s.status for s in r.steps] == [PASSED, PASSED, FAILED, NOT_EXECUTED]


def test_missing_element_is_failed_not_passed(tmp_path):
    eng, _, _ = make_engine(tmp_path, cfg=RunConfig.from_dict({"screenshot_mode": "none", "action_timeout_ms": 1000,
                                                              "use_ai_locator_recovery": False}))
    [r] = run(eng, [case("TC3", [("Open the application", ""), ("Click the 'Nope' button", "")])])
    assert r.status == FAILED and r.failure_category == FAIL_ELEMENT


def test_uninterpretable_and_invalid_cases_are_blocked(tmp_path):
    eng, _, _ = make_engine(tmp_path)
    vague = case("TC4", [("Open the application", ""), ("Do something clever", "It works")])
    invalid = case("TC5", [])
    invalid.issues.append("No test steps")
    r1, r2 = run(eng, [vague, invalid])
    assert r1.status == BLOCKED and r1.failure_category == FAIL_INTERPRET
    assert r1.steps[0].status == PASSED and r1.steps[1].status == BLOCKED
    assert r2.status == BLOCKED and "No test steps" in r2.failure_reason


def test_disallowed_navigation_is_blocked(tmp_path):
    eng, _, _ = make_engine(tmp_path)
    [r] = run(eng, [case("TC6", [("Navigate to https://evil.example/", "")])])
    assert r.status == BLOCKED and r.failure_category == FAIL_POLICY


def test_infra_failure_is_retried_on_a_fresh_browser(tmp_path):
    sessions = []

    def factory(i):
        s = FakeMCPSession(fail_plan=[("browser_navigate", 1)] if not sessions else [])
        sessions.append(s)
        return s
    eng, usage, _ = make_engine(tmp_path, factory=factory)
    [r] = run(eng, [case("TC7", LOGIN_OK)])
    assert r.status == PASSED and r.retry_count == 1 and usage.retries == 1
    assert len(sessions) == 2


def test_infra_failure_after_state_change_is_not_retried(tmp_path):
    def factory(i):
        return FakeMCPSession(fail_plan=[("browser_click", 1)])
    eng, usage, _ = make_engine(tmp_path, factory=factory)
    [r] = run(eng, [case("TC8", LOGIN_OK)])
    assert r.status == BLOCKED and r.failure_category == FAIL_INFRA
    assert "not retried automatically" in r.failure_reason
    assert usage.retries == 0
    assert r.steps[3].status == BLOCKED and r.steps[4].status == NOT_EXECUTED


def test_unavailable_mcp_blocks_everything(tmp_path):
    from test_execution.playwright_mcp import MCPInfraError

    class Broken(FakeMCPSession):
        async def __aenter__(self):
            raise MCPInfraError("npx not found")
    eng, _, _ = make_engine(tmp_path, factory=lambda i: Broken())
    results = run(eng, [case("A", LOGIN_OK), case("B", LOGIN_OK)])
    assert [r.status for r in results] == [BLOCKED, BLOCKED]
    assert "npx not found" in results[0].failure_reason


def test_isolation_resets_browser_between_tests(tmp_path):
    eng, _, _ = make_engine(tmp_path)
    run(eng, [case("A", LOGIN_OK), case("B", LOGIN_OK)])
    assert FakeMCPSession.instances[0].resets == 2


@pytest.mark.parametrize("mode,expected_shots", [("none", 0), ("final", 2), ("every_step", 6), ("failure", 1)])
def test_screenshot_modes(tmp_path, mode, expected_shots):
    cfg = RunConfig.from_dict({"screenshot_mode": mode, "action_timeout_ms": 1000})
    eng, usage, store = make_engine(tmp_path, cfg=cfg)
    ok = case("OK", LOGIN_OK)
    bad = case("BAD", [("Open the application", ""), ("Click on Login button", "'Welcome back' is displayed")])
    r_ok, r_bad = run(eng, [ok, bad])
    assert usage.screenshots == expected_shots
    for ev in r_ok.evidence + r_bad.evidence:
        assert store.get(eng.run_id, ev.path)               # stored and retrievable
        assert ev.path.endswith(".png") and ev.test_key in ev.path
    if mode == "failure":
        assert r_bad.evidence[0].step_number == 2 and r_bad.evidence[0].status == FAILED


def test_duplicate_screenshots_are_skipped(tmp_path):
    cfg = RunConfig.from_dict({"screenshot_mode": "every_step", "action_timeout_ms": 1000})
    eng, usage, _ = make_engine(tmp_path, cfg=cfg)
    # three steps that don't change the page -> one screenshot
    [r] = run(eng, [case("D", [("Open the application", ""), ("Wait for 0 seconds", ""), ("Wait for 0 seconds", "")])])
    assert r.status == PASSED and usage.screenshots == 1


def test_cancel_before_start_marks_not_executed(tmp_path):
    cancel = threading.Event()
    cancel.set()
    eng, _, _ = make_engine(tmp_path, cancel=cancel)
    results = run(eng, [case("A", LOGIN_OK)])
    assert results[0].status == NOT_EXECUTED


def test_test_timeout_blocks(tmp_path):
    cfg = RunConfig.from_dict({"screenshot_mode": "none", "test_timeout_s": 30})
    eng, _, _ = make_engine(tmp_path, cfg=cfg, factory=lambda i: FakeMCPSession(slow_tools={"browser_navigate": 0.5}))
    eng.cfg.test_timeout_s = 0.2   # bypass the 30s floor for the test
    [r] = run(eng, [case("T", LOGIN_OK)])
    assert r.status == BLOCKED and "time limit" in r.failure_reason


def test_concurrency_uses_separate_browsers(tmp_path):
    cfg = RunConfig.from_dict({"screenshot_mode": "none", "concurrency": 2, "action_timeout_ms": 1000})
    eng, _, _ = make_engine(tmp_path, cfg=cfg)
    results = run(eng, [case(f"C{i}", LOGIN_OK) for i in range(4)])
    assert all(r.status == PASSED for r in results)
    assert len(FakeMCPSession.instances) == 2


def test_ai_locator_recovery_used_once_with_trimmed_snapshot(tmp_path):
    prompts = []

    async def ai(prompt):
        prompts.append(prompt)
        return '{"ref": "e6"}'
    usage = UsageMetrics()
    planner = AIPlanner(ai, None, usage)
    eng, _, _ = make_engine(tmp_path, planner=planner)
    eng.usage = usage
    [r] = run(eng, [case("R", [("Open the application", ""), ("Click the 'Sign me in' button", "")])])
    assert r.status == PASSED
    assert len(prompts) == 1 and usage.locator_recoveries == 1
    assert "[ref=e6]" in prompts[0] and "Sign me in" in prompts[0]


def test_ai_recovery_rejects_refs_not_in_snapshot(tmp_path):
    async def ai(prompt):
        return '{"ref": "e999"}'
    usage = UsageMetrics()
    eng, _, _ = make_engine(tmp_path, planner=AIPlanner(ai, None, usage))
    [r] = run(eng, [case("R", [("Open the application", ""), ("Click the 'Nope' button", "")])])
    assert r.status == FAILED and r.failure_category == FAIL_ELEMENT


def test_find_parsing_prefers_role_and_exact_name():
    text = ('### Result\nFound 3 matches:\n\n- generic [ref=e1]:\n  - link "Login help" [ref=e7]\n'
            '  - button "Login" [active] [ref=e4]\n  - paragraph [ref=e5]: Login to continue')
    cands = parse_find_candidates(text)
    assert [c["ref"] for c in cands] == ["e1", "e7", "e4", "e5"]
    assert pick_candidate(cands, Target("role", "Login", "button"), "click") == "e4"
    assert pick_candidate(cands, Target("text", "Login to continue"), "click") == "e5"
    assert pick_candidate(cands, Target("label", "Login", "textbox"), "type") is None


# ── AI planner ──────────────────────────────────────────────────────────────

def test_planner_batches_unresolved_steps_and_caches(tmp_path):
    store = ExecutionStore(str(tmp_path / "db.sqlite"))
    prompts = []

    async def ai(prompt):
        prompts.append(prompt)
        return json.dumps({"steps": [
            {"number": 2, "actions": [{"type": "click", "target": {"kind": "role", "value": "Widget", "role": "button"}}]},
            {"number": 3, "unsupported": "needs a human to judge aesthetics"}]})
    usage = UsageMetrics()
    planner = AIPlanner(ai, store, usage)
    tc = case("P", [("Open the application", ""), ("Poke the widget thing", ""), ("Admire the design", "Looks nice")])
    asyncio.run(planner.plan_unresolved(tc))
    assert len(prompts) == 1 and usage.model_calls == 1
    assert "Poke the widget" in prompts[0] and "Open the application" not in prompts[0]   # minimal context
    assert tc.steps[1].plan_source == "ai" and tc.steps[1].actions[0].type == "click"
    assert tc.steps[2].plan_source == "unresolved" and "Not automatable" in tc.steps[2].plan_error
    assert usage.est_input_tokens == estimate_tokens(prompts[0]) and usage.est_output_tokens > 0

    tc2 = case("P2", [("Poke the widget thing", "")])
    asyncio.run(AIPlanner(ai, store, usage).plan_unresolved(tc2))
    assert len(prompts) == 1 and usage.plan_cache_hits == 1 and tc2.steps[0].plan_source == "cache"


def test_planner_rejects_out_of_schema_actions(tmp_path):
    async def ai(prompt):
        return '{"steps": [{"number": 1, "actions": [{"type": "execute_js", "value": "fetch(\'/x\')"}]}]}'
    tc = case("X", [("Poke the widget thing", "")])
    asyncio.run(AIPlanner(ai, None, UsageMetrics()).plan_unresolved(tc))
    assert tc.steps[0].plan_source == "unresolved" and "rejected" in tc.steps[0].plan_error


def test_planner_off_means_blocked_not_guessed(tmp_path):
    tc = case("X", [("Poke the widget thing", "")])
    asyncio.run(AIPlanner(None, None, UsageMetrics()).plan_unresolved(tc))
    assert tc.steps[0].plan_source == "unresolved" and "turned off" in tc.steps[0].plan_error


def test_planner_redacts_secrets_from_prompts(tmp_path):
    seen = []

    async def ai(prompt):
        seen.append(prompt)
        return '{"steps": []}'
    tc = case("X", [("Poke the widget with s3cret!", "")])
    asyncio.run(AIPlanner(ai, None, UsageMetrics(), Redactor(["s3cret!"])).plan_unresolved(tc))
    assert "s3cret!" not in seen[0]
