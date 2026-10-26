"""
AI fallback for what the deterministic parser cannot read.

Token discipline:
  * one call per test case, carrying only that case's unresolved steps
  * no workbook, story, page or log context in planning calls
  * results validated against ACTION_SCHEMA and cached by step-text hash,
    so the same wording is never sent twice (across runs too)
  * locator recovery sends a trimmed snapshot, once per step, never repeated
    for the same failure
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Awaitable, Callable, Optional

from .models import ACTION_SCHEMA, Action, TestCase, UsageMetrics
from .step_parser import validate_action

AICall = Callable[[str], Awaitable[str]]
SNAPSHOT_EXCERPT_CHARS = 6000
PLAN_VERSION = "v1"   # bump to invalidate cached plans when the schema changes

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # offline or not installed: fall back to a chars/4 estimate
    _ENC = None


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)


def _schema_line() -> str:
    return "; ".join(f"{k}: {','.join(v['required']) or '-'}" for k, v in ACTION_SCHEMA.items())


def _extract_json(raw: str) -> dict:
    text = re.sub(r"```[a-z]*", "", raw or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def plan_hash(action_text: str, expected: str, test_data: str) -> str:
    norm = "\x1f".join(" ".join((x or "").lower().split()) for x in (PLAN_VERSION, action_text, expected, test_data))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


class AIPlanner:
    def __init__(self, ai_call: Optional[AICall], store, usage: UsageMetrics, redact=lambda s: s):
        self.ai_call = ai_call
        self.store = store
        self.usage = usage
        self.redact = redact

    async def _call(self, prompt: str) -> str:
        prompt = self.redact(prompt)
        self.usage.model_calls += 1
        self.usage.est_input_tokens += estimate_tokens(prompt)
        out = await self.ai_call(prompt)
        self.usage.est_output_tokens += estimate_tokens(out)
        return out

    async def plan_unresolved(self, tc: TestCase) -> None:
        """Fills actions for tc's unresolved steps from cache, then (one call) from the AI."""
        pending = []
        for s in tc.steps:
            if s.plan_source != "unresolved":
                continue
            h = plan_hash(s.action_text, s.expected, s.test_data or tc.test_data)
            cached = self.store.get_plan(h) if self.store else None
            if cached is not None:
                s.actions = [Action.from_dict({**a, "source": "cache"}) for a in cached]
                s.plan_source, s.plan_error = "cache", ""
                self.usage.plan_cache_hits += 1
            else:
                pending.append((s, h))
        if not pending:
            return
        if self.ai_call is None:
            for s, _ in pending:
                s.plan_error += " (AI interpretation is turned off)"
            return

        steps_payload = [{"number": s.number, "action": s.action_text, "expected": s.expected,
                          **({"data": s.test_data} if s.test_data else {})} for s, _ in pending]
        from prompts.test_execution_prompts import get_step_interpretation_prompt
        prompt = get_step_interpretation_prompt(
            tc.title, tc.preconditions[:500], json.dumps(steps_payload, ensure_ascii=False), _schema_line())
        try:
            parsed = _extract_json(await self._call(prompt))
        except Exception as exc:
            for s, _ in pending:
                s.plan_error = f"AI interpretation failed: {self.redact(str(exc))[:300]}"
            return

        by_number = {}
        for item in parsed.get("steps", []) if isinstance(parsed, dict) else []:
            if isinstance(item, dict) and "number" in item:
                try:
                    by_number[int(item["number"])] = item
                except (TypeError, ValueError):
                    continue
        for s, h in pending:
            item = by_number.get(s.number)
            if item is None:
                s.plan_error = "AI returned no plan for this step"
                continue
            if item.get("unsupported"):
                s.plan_error = f"Not automatable: {str(item['unsupported'])[:200]}"
                continue
            actions, errors = [], []
            for raw in item.get("actions") or []:
                try:
                    a = Action.from_dict({**raw, "source": "ai"})
                except Exception as exc:
                    errors.append(f"malformed action ({exc.__class__.__name__})")
                    continue
                err = validate_action(a)
                if err:
                    errors.append(err)
                else:
                    actions.append(a)
            if errors or not actions:
                # Reject the whole step rather than run half a plan.
                s.plan_error = "AI plan rejected: " + ("; ".join(errors) if errors else "no actions")
                continue
            s.actions, s.plan_source, s.plan_error = actions, "ai", ""
            self.usage.steps_planned_by_ai += 1
            if self.store:
                self.store.put_plan(h, [a.to_dict() for a in actions])

    async def recover_locator(self, step_text: str, target_desc: str, snapshot: str) -> Optional[str]:
        """Snapshot ref for an element the deterministic resolver couldn't find, or None."""
        if self.ai_call is None or not snapshot:
            return None
        from prompts.test_execution_prompts import get_locator_recovery_prompt
        excerpt = snapshot[:SNAPSHOT_EXCERPT_CHARS]
        try:
            parsed = _extract_json(await self._call(get_locator_recovery_prompt(step_text, target_desc, excerpt)))
        except Exception:
            return None
        ref = parsed.get("ref") if isinstance(parsed, dict) else None
        # Only accept a ref that really exists in what we sent.
        if isinstance(ref, str) and re.fullmatch(r"[a-z]?\d+|e\d+|f\d+e\d+", ref) and f"[ref={ref}]" in excerpt:
            self.usage.locator_recoveries += 1
            return ref
        return None
