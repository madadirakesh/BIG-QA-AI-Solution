"""
Normalized intermediate representation shared by every source, the engine and
every publisher. Nothing in here knows about Excel, Jira or Playwright.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


# ── Execution status ──────────────────────────────────────────────────────────
PASSED = "Passed"
FAILED = "Failed"
BLOCKED = "Blocked"
NOT_EXECUTED = "Not Executed"
IN_PROGRESS = "In Progress"
EXECUTION_STATUSES = (PASSED, FAILED, BLOCKED, NOT_EXECUTED, IN_PROGRESS)

# ── Publication status (kept separate: a test can pass while Jira is down) ─────
PUB_PENDING = "Pending"
PUB_PUBLISHED = "Published"
PUB_FAILED = "Failed"
PUB_SKIPPED = "Skipped"          # nothing to publish to (e.g. publishing turned off)
PUB_UNSUPPORTED = "Unsupported"  # destination cannot accept this kind of update

# ── Failure categories: lets the dashboard and retry logic tell an application
#    defect from a broken test instruction from flaky infrastructure. ─────────
FAIL_ASSERTION = "assertion"         # app behaved differently than expected -> Failed
FAIL_ELEMENT = "element_not_found"   # locator didn't resolve -> Failed (may be app or test)
FAIL_INTERPRET = "uninterpretable"   # step text couldn't be turned into an action -> Blocked
FAIL_INFRA = "infrastructure"        # MCP/browser/timeout problems -> Blocked, retryable
FAIL_POLICY = "policy"               # URL not allowed, production not confirmed -> Blocked
FAIL_CANCELLED = "cancelled"

# Screenshot modes
SHOT_NONE = "none"
SHOT_EVERY_STEP = "every_step"
SHOT_FINAL = "final"
SHOT_FAILURE = "failure"
SCREENSHOT_MODES = (SHOT_NONE, SHOT_EVERY_STEP, SHOT_FINAL, SHOT_FAILURE)

# Supported actions. The AI planner is validated against exactly this set.
ACTION_SCHEMA: dict[str, dict[str, Any]] = {
    "navigate":        {"required": ["value"]},          # value = URL or path
    "click":           {"required": ["target"]},
    "double_click":    {"required": ["target"]},
    "hover":           {"required": ["target"]},
    "type":            {"required": ["target", "value"]},
    "select":          {"required": ["target", "value"]},
    "check":           {"required": ["target"]},
    "uncheck":         {"required": ["target"]},
    "press":           {"required": ["value"]},          # value = key name
    "wait":            {"required": ["value"]},          # value = seconds
    "wait_for_text":   {"required": ["value"]},
    "assert_text":     {"required": ["value"]},
    "assert_not_text": {"required": ["value"]},
    "assert_url":      {"required": ["value"]},          # substring of URL
    "assert_title":    {"required": ["value"]},          # substring of title
    "assert_visible":  {"required": ["target"]},
    "assert_value":    {"required": ["target", "value"]},
    "note":            {"required": []},                 # informational step, nothing to do
}
ASSERTION_ACTIONS = {"assert_text", "assert_not_text", "assert_url", "assert_title",
                     "assert_visible", "assert_value", "wait_for_text"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Target:
    """
    How to find an element. `kind` decides how the engine builds selector
    candidates: css/xpath/ref are used verbatim; text/label/role/placeholder
    are semantic descriptions resolved deterministically at run time.
    """
    kind: str            # css | xpath | text | label | role | placeholder | testid | ref
    value: str
    role: str = ""       # for kind == "role": button, link, textbox, ...

    def describe(self) -> str:
        if self.kind == "role":
            return f'{self.role or "element"} "{self.value}"'
        return f"{self.kind}={self.value}"


@dataclass
class Action:
    type: str
    target: Optional[Target] = None
    value: str = ""
    secret: bool = False      # value is a secret NAME for Playwright MCP --secrets, never a literal
    source: str = "parser"    # parser | ai | cache

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.target is None:
            d["target"] = None
        return d

    @staticmethod
    def from_dict(d: dict) -> "Action":
        t = d.get("target")
        return Action(
            type=d["type"],
            target=Target(**t) if isinstance(t, dict) else None,
            value=str(d.get("value", "") or ""),
            secret=bool(d.get("secret", False)),
            source=d.get("source", "parser"),
        )


@dataclass
class TestStep:
    __test__ = False   # not a pytest test class
    number: int
    action_text: str
    expected: str = ""
    test_data: str = ""
    source_row: Optional[int] = None    # Excel row the step text came from (1-based)
    actions: list[Action] = field(default_factory=list)
    plan_source: str = ""               # parser | ai | cache | unresolved
    plan_error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["actions"] = [a.to_dict() for a in self.actions]
        return d

    @staticmethod
    def from_dict(d: dict) -> "TestStep":
        d = dict(d)
        d["actions"] = [Action.from_dict(a) for a in d.get("actions", [])]
        return TestStep(**d)


@dataclass
class TestCase:
    __test__ = False   # not a pytest test class
    key: str                         # unique within a run (Excel ID / Jira key / Zephyr key)
    title: str
    source: str                      # excel | jira | zephyr
    description: str = ""
    preconditions: str = ""
    test_data: str = ""
    expected_result: str = ""
    steps: list[TestStep] = field(default_factory=list)
    source_ref: dict = field(default_factory=dict)   # where to write results back
    issues: list[str] = field(default_factory=list)  # validation problems (never silently dropped)
    selected: bool = True

    @property
    def valid(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [s.to_dict() for s in self.steps]
        d["valid"] = self.valid
        return d

    @staticmethod
    def from_dict(d: dict) -> "TestCase":
        d = {k: v for k, v in d.items() if k != "valid"}
        d["steps"] = [TestStep.from_dict(s) for s in d.get("steps", [])]
        return TestCase(**d)


@dataclass
class Evidence:
    evidence_id: str
    test_key: str
    step_number: Optional[int]       # None = final screenshot
    status: str
    captured_at: str
    path: str                        # storage-relative path
    size_bytes: int = 0
    label: str = ""


@dataclass
class StepResult:
    number: int
    action_text: str
    expected: str
    status: str = NOT_EXECUTED
    actual: str = ""
    error: str = ""
    failure_category: str = ""
    started_at: str = ""
    duration_ms: int = 0
    evidence_ids: list[str] = field(default_factory=list)
    operations: int = 0


@dataclass
class TestCaseResult:
    __test__ = False   # not a pytest test class
    run_id: str
    test_key: str
    title: str
    source: str
    status: str = NOT_EXECUTED
    started_at: str = ""
    ended_at: str = ""
    duration_ms: int = 0
    steps: list[StepResult] = field(default_factory=list)
    expected: str = ""
    actual: str = ""
    failed_step: Optional[int] = None
    failure_reason: str = ""
    failure_category: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    retry_count: int = 0
    publication: dict = field(default_factory=dict)   # destination -> {status, message, ref}

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "TestCaseResult":
        d = dict(d)
        d["steps"] = [StepResult(**s) for s in d.get("steps", [])]
        d["evidence"] = [Evidence(**e) for e in d.get("evidence", [])]
        return TestCaseResult(**d)


@dataclass
class RunConfig:
    base_url: str = ""
    screenshot_mode: str = SHOT_FINAL
    concurrency: int = 1
    isolate_tests: bool = True          # fresh browser storage per test case
    headless: bool = True
    action_timeout_ms: int = 8000
    navigation_timeout_ms: int = 30000
    test_timeout_s: int = 300
    max_infra_retries: int = 1          # retries for infrastructure failures only
    use_ai_planner: bool = True
    use_ai_locator_recovery: bool = True
    publish_results: bool = True
    embed_screenshots: bool = True      # Excel only: embed vs hyperlink
    mask_selectors: list[str] = field(default_factory=list)
    allowed_hosts: list[str] = field(default_factory=list)
    confirm_production: bool = False
    zephyr_test_cycle: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "RunConfig":
        known = {k: v for k, v in (d or {}).items() if k in RunConfig.__dataclass_fields__}
        cfg = RunConfig(**known)
        cfg.concurrency = max(1, min(int(cfg.concurrency or 1), 4))
        cfg.max_infra_retries = max(0, min(int(cfg.max_infra_retries or 0), 3))
        cfg.action_timeout_ms = max(1000, min(int(cfg.action_timeout_ms or 8000), 60000))
        cfg.navigation_timeout_ms = max(5000, min(int(cfg.navigation_timeout_ms or 30000), 120000))
        cfg.test_timeout_s = max(30, min(int(cfg.test_timeout_s or 300), 3600))
        if cfg.screenshot_mode not in SCREENSHOT_MODES:
            cfg.screenshot_mode = SHOT_FINAL
        cfg.mask_selectors = [s for s in (cfg.mask_selectors or []) if isinstance(s, str) and s.strip()][:20]
        cfg.allowed_hosts = [h.strip().lower() for h in (cfg.allowed_hosts or []) if isinstance(h, str) and h.strip()][:20]
        return cfg


@dataclass
class UsageMetrics:
    """Cost/performance counters. Token numbers are estimates and labelled as such."""
    model_calls: int = 0
    est_input_tokens: int = 0
    est_output_tokens: int = 0
    plan_cache_hits: int = 0
    steps_parsed_deterministically: int = 0
    steps_planned_by_ai: int = 0
    locator_recoveries: int = 0
    browser_operations: int = 0
    screenshots: int = 0
    evidence_bytes: int = 0
    retries: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def compute_summary(results: list[TestCaseResult], usage: Optional[UsageMetrics] = None) -> dict:
    """
    Pass % = Passed / (Passed + Failed). Blocked and Not Executed are excluded
    from the denominator: they say nothing about whether the application works.
    """
    counts = {s: 0 for s in EXECUTION_STATUSES}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    decided = counts[PASSED] + counts[FAILED]
    published = pending = pub_failed = 0
    for r in results:
        for p in (r.publication or {}).values():
            st = p.get("status")
            if st == PUB_PUBLISHED:
                published += 1
            elif st == PUB_FAILED:
                pub_failed += 1
            elif st == PUB_PENDING:
                pending += 1
    return {
        "total": len(results),
        "passed": counts[PASSED],
        "failed": counts[FAILED],
        "blocked": counts[BLOCKED],
        "not_executed": counts[NOT_EXECUTED],
        "in_progress": counts[IN_PROGRESS],
        "pass_percentage": round(100.0 * counts[PASSED] / decided, 1) if decided else None,
        "pass_percentage_formula": "Passed / (Passed + Failed); Blocked and Not Executed excluded",
        "total_duration_ms": sum(r.duration_ms for r in results),
        "screenshot_count": sum(len(r.evidence) for r in results),
        "published": published,
        "publication_pending": pending,
        "publication_failed": pub_failed,
        "usage": usage.to_dict() if usage else None,
    }
