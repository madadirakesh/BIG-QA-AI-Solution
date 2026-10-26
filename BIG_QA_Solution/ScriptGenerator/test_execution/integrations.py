"""
Test sources and result publishers for Jira and Zephyr Scale.

Integration reality (checked, not assumed):
  * Jira: the application has no Jira MCP server; its existing Jira integration
    is the `jira` Python library configured through Configure Jira (.env). This
    module reuses that client (api.backend._get_jira_client) so credentials stay
    in one place.
  * Zephyr: no Zephyr MCP server is available in this environment. ZephyrScaleClient
    talks to the Zephyr Scale Cloud REST API v2 behind the same TestSource /
    ResultPublisher interface, so an MCP-backed client can replace it later
    without touching the engine. Zephyr Squad is not supported.

Publishers never touch descriptions, summaries or other fields; Jira gets a
comment plus attachments, Zephyr gets a new test execution record.
"""
from __future__ import annotations

import hashlib
import html
import io
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Callable

from .models import (
    TestCase, TestStep, TestCaseResult, PASSED, FAILED, BLOCKED, NOT_EXECUTED, IN_PROGRESS,
    PUB_PUBLISHED, PUB_FAILED, PUB_SKIPPED,
)

ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
ZEPHYR_TEST_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-T\d+$")
MAX_ATTACHMENTS_PER_TEST = 25
_TEST_TYPES = ("test", "test case", "testcase", "test-case")


class IntegrationError(RuntimeError):
    pass


# ── common interfaces ────────────────────────────────────────────────────────

class ResultPublisher(ABC):
    name: str = ""

    @abstractmethod
    def publish(self, tc: TestCase, result: TestCaseResult, evidence_reader: Callable, run_id: str) -> tuple[str, str, str]:
        """Returns (publication status, external reference, message)."""

    @staticmethod
    def fingerprint(result: TestCaseResult) -> str:
        """Same result -> same key, so a retried publication is detected as a duplicate."""
        raw = f"{result.run_id}|{result.test_key}|{result.status}|{result.ended_at}|{result.retry_count}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def idempotency_key(self, tc: TestCase, result: TestCaseResult) -> str:
        return f"{self.name}:{tc.key}:{self.fingerprint(result)}"


def _strip_markup(text: str) -> str:
    """Zephyr fields are HTML, Jira v2 descriptions are wiki markup: reduce both to plain text."""
    t = html.unescape(re.sub(r"<br\s*/?>|</p>|</li>", "\n", text or "", flags=re.I))
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\{(?:code|noformat|panel)[^}]*\}", "", t)
    t = re.sub(r"\*([^*\n]+)\*", r"\1", t)   # wiki bold; underscores are left alone (user_name)
    return t.strip()


# ── Jira ─────────────────────────────────────────────────────────────────────

_SECTION = re.compile(
    r"^\s*(?:h\d\.\s*|\*|#+\s*)?(pre-?conditions?|prerequisites?|test steps|steps(?: to reproduce)?|"
    r"expected results?|test data)\s*\*?\s*:?\s*$", re.I | re.M)


def parse_jira_test_description(description: str) -> dict:
    """
    Extracts preconditions / steps / expected / test data from a Jira Test issue
    description. Steps may be a wiki table (|| Step || Expected ||) or a
    numbered list ('# step' or '1. step', optionally 'step -> expected').
    """
    text = (description or "").replace("\r", "")
    out = {"preconditions": "", "steps": [], "expected": "", "test_data": ""}
    sections: dict[str, str] = {}
    matches = list(_SECTION.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        name = m.group(1).lower()
        key = ("preconditions" if name.startswith(("pre", "prereq")) else
               "expected" if name.startswith("expected") else
               "test_data" if name.startswith("test data") else "steps")
        sections[key] = (sections.get(key, "") + "\n" + text[m.end():end]).strip()
    steps_text = sections.get("steps", "" if matches else text)

    rows = [ln for ln in steps_text.splitlines() if ln.strip().startswith("|") and not ln.strip().startswith("||")]
    if rows:
        for ln in rows:
            cells = [_strip_markup(c) for c in ln.strip().strip("|").split("|")]
            if cells and re.fullmatch(r"\d+", cells[0] or ""):
                cells = cells[1:]
            if cells and cells[0]:
                out["steps"].append((cells[0], cells[1] if len(cells) > 1 else ""))
    else:
        for ln in steps_text.splitlines():
            m = re.match(r"^\s*(?:#+|\d+[.)])\s+(.+)$", ln)
            if not m:
                continue
            body = _strip_markup(m.group(1))
            parts = re.split(r"\s*(?:->|=>|→)\s*|\s+expected(?: result)?\s*:\s*", body, maxsplit=1, flags=re.I)
            out["steps"].append((parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""))
    out["preconditions"] = _strip_markup(sections.get("preconditions", ""))
    out["expected"] = _strip_markup(sections.get("expected", ""))
    out["test_data"] = _strip_markup(sections.get("test_data", ""))
    return out


class JiraAdapter(ResultPublisher):
    name = "jira"

    def __init__(self, client_factory: Callable):
        self._factory = client_factory
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                self._client = self._factory()
            except Exception as exc:
                raise IntegrationError(f"Jira is not available: {exc}") from exc
        return self._client

    def search(self, query: str, max_results: int = 25) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        if len(q) > 1000:
            raise IntegrationError("Query is too long.")
        jql = f'key = "{q.upper()}"' if ISSUE_KEY.match(q.upper()) else q
        try:
            issues = self.client.search_issues(jql, maxResults=max_results, fields="summary,issuetype,status")
        except Exception as exc:
            raise IntegrationError(f"Jira search failed: {exc}") from exc
        return [{"key": i.key, "summary": i.fields.summary, "type": str(i.fields.issuetype),
                 "status": str(i.fields.status)} for i in issues]

    def _issue_to_case(self, issue) -> TestCase:
        f = issue.fields
        parsed = parse_jira_test_description(getattr(f, "description", "") or "")
        tc = TestCase(
            key=issue.key, title=f.summary, source="jira",
            description=_strip_markup((getattr(f, "description", "") or ""))[:2000],
            preconditions=parsed["preconditions"], test_data=parsed["test_data"],
            expected_result=parsed["expected"],
            steps=[TestStep(number=i + 1, action_text=a, expected=e) for i, (a, e) in enumerate(parsed["steps"])],
            source_ref={"issue_key": issue.key},
        )
        if not tc.steps:
            tc.issues.append("No executable steps found in the issue description (expected a Steps section, "
                             "numbered list or Step/Expected table)")
        if tc.steps and tc.expected_result and not any(s.expected for s in tc.steps):
            tc.steps[-1].expected = tc.expected_result
        return tc

    def test_cases_for(self, key: str) -> tuple[list[TestCase], list[str]]:
        """Test cases for an issue: the issue itself if it is a Test, else linked/sub-task Tests."""
        notes: list[str] = []
        try:
            issue = self.client.issue(key)
        except Exception as exc:
            raise IntegrationError(f"Could not load {key}: {exc}") from exc
        if str(issue.fields.issuetype).lower() in _TEST_TYPES:
            return [self._issue_to_case(issue)], notes

        keys: list[str] = []
        for link in getattr(issue.fields, "issuelinks", []) or []:
            lt = link.type
            label = " ".join(str(getattr(lt, a, "")) for a in ("name", "inward", "outward")).lower()
            other = getattr(link, "outwardIssue", None) or getattr(link, "inwardIssue", None)
            if other is None:
                continue
            other_type = str(getattr(other.fields, "issuetype", "")).lower()
            if "test" in label or other_type in _TEST_TYPES:
                keys.append(other.key)
        for sub in getattr(issue.fields, "subtasks", []) or []:
            if "test" in str(getattr(sub.fields, "issuetype", "")).lower():
                keys.append(sub.key)
        cases = []
        for k in dict.fromkeys(keys):
            try:
                cases.append(self._issue_to_case(self.client.issue(k)))
            except Exception as exc:
                notes.append(f"Skipped {k}: {exc}")
        if not cases:
            notes.append(f"{key} has no linked Test issues or test sub-tasks. A story's own description is not "
                         "treated as an executable test; link Test issues or use Zephyr test cases.")
        return cases, notes

    def publish(self, tc, result, evidence_reader, run_id):
        issue_key = tc.source_ref.get("issue_key") or tc.source_ref.get("jira_issue")
        if not issue_key:
            return PUB_SKIPPED, "", "No Jira issue to publish to"
        marker = f"BIG-QA-RUN {run_id} {tc.key} {self.fingerprint(result)}"
        try:
            for c in self.client.comments(issue_key):
                if marker in (getattr(c, "body", "") or ""):
                    return PUB_PUBLISHED, f"comment:{c.id}", "Already published (found existing comment)"
        except Exception as exc:
            return PUB_FAILED, "", f"Could not read comments on {issue_key}: {exc}"

        attached, attach_errors = [], []
        for ev in result.evidence[:MAX_ATTACHMENTS_PER_TEST]:
            data = evidence_reader(ev)
            if not data:
                attach_errors.append(f"{ev.evidence_id}: file missing")
                continue
            fname = os.path.basename(ev.path)
            try:
                self.client.add_attachment(issue=issue_key, attachment=io.BytesIO(data), filename=fname)
                attached.append((fname, ev))
            except Exception as exc:
                attach_errors.append(f"{fname}: {exc}")
        try:
            comment = self.client.add_comment(issue_key, self._comment(tc, result, attached, marker))
        except Exception as exc:
            return PUB_FAILED, "", f"Adding the result comment failed: {exc}"
        msg = f"Comment added with {len(attached)} screenshot(s)"
        if len(result.evidence) > MAX_ATTACHMENTS_PER_TEST:
            msg += f"; {len(result.evidence) - MAX_ATTACHMENTS_PER_TEST} screenshot(s) over the attachment limit were not uploaded"
        if attach_errors:
            msg += "; attachment errors: " + "; ".join(attach_errors)[:400]
            return PUB_FAILED, f"comment:{comment.id}", msg
        return PUB_PUBLISHED, f"comment:{comment.id}", msg

    @staticmethod
    def _comment(tc: TestCase, r: TestCaseResult, attached, marker: str) -> str:
        icon = {PASSED: "(/)", FAILED: "(x)", BLOCKED: "(!)", NOT_EXECUTED: "(?)"}.get(r.status, "")
        lines = [f"h3. {icon} Automated execution: *{r.status}*",
                 f"Run {r.run_id} | {r.ended_at or r.started_at} | {round(r.duration_ms / 1000.0, 1)}s", ""]
        if r.failure_reason:
            lines.append(f"*Failure (step {r.failed_step}):* {r.failure_reason}")
        lines.append("||#||Step||Status||Actual||")
        for s in r.steps:
            cell = lambda x: (x or " ").replace("|", "/").replace("\n", " ")[:300]
            lines.append(f"|{s.number}|{cell(s.action_text)}|{s.status}|{cell(s.actual or s.error)}|")
        if attached:
            lines.append("")
            lines.append("*Evidence:*")
            for fname, ev in attached:
                where = "final state" if ev.step_number is None else f"step {ev.step_number}"
                lines.append(f"* {where} ({ev.status}): !{fname}|thumbnail!")
        lines.append("")
        lines.append(f"{{color:#999999}}{marker}{{color}}")
        return "\n".join(lines)


# ── Zephyr Scale Cloud (REST v2) ─────────────────────────────────────────────

ZEPHYR_STATUS = {PASSED: "Pass", FAILED: "Fail", BLOCKED: "Blocked", NOT_EXECUTED: "Not Executed", IN_PROGRESS: "In Progress"}


class ZephyrScaleClient:
    def __init__(self, token: str, base_url: str = "", session=None, max_retries: int = 3):
        import requests
        if not token:
            raise IntegrationError("Zephyr Scale is not configured (ZEPHYR_API_TOKEN).")
        self.base = (base_url or "https://api.zephyrscale.smartbear.com/v2").rstrip("/")
        self.http = session or requests.Session()
        self.http.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self.max_retries = max_retries

    def _req(self, method: str, path: str, **kw):
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.http.request(method, self.base + path, timeout=30, **kw)
            except Exception as exc:
                if attempt == self.max_retries:
                    raise IntegrationError(f"Zephyr request failed: {exc}") from exc
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429 and attempt < self.max_retries:
                time.sleep(min(float(resp.headers.get("Retry-After", 2 ** attempt)), 30))
                continue
            if resp.status_code >= 500 and attempt < self.max_retries:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise IntegrationError(f"Zephyr {method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else {}
        raise IntegrationError("Zephyr request failed after retries")

    def test_case(self, key):
        return self._req("GET", f"/testcases/{key}")

    def test_steps(self, key):
        return self._req("GET", f"/testcases/{key}/teststeps", params={"maxResults": 200}).get("values", [])

    def test_script(self, key):
        return self._req("GET", f"/testcases/{key}/testscript")

    def test_cases_for_issue(self, issue_key):
        return self._req("GET", f"/issuelinks/{issue_key}/testcases") or []

    def test_cases_for_project(self, project_key, max_results=50):
        return self._req("GET", "/testcases", params={"projectKey": project_key, "maxResults": max_results}).get("values", [])

    def test_cycles(self, project_key, max_results=50):
        return self._req("GET", "/testcycles", params={"projectKey": project_key, "maxResults": max_results}).get("values", [])

    def create_execution(self, payload: dict):
        return self._req("POST", "/testexecutions", json=payload)


class ZephyrAdapter(ResultPublisher):
    name = "zephyr"

    def __init__(self, client: ZephyrScaleClient, test_cycle: str = ""):
        self.client = client
        self.test_cycle = test_cycle

    def _to_case(self, key: str) -> TestCase:
        meta = self.client.test_case(key)
        steps: list[TestStep] = []
        issues = []
        try:
            for i, st in enumerate(self.client.test_steps(key), start=1):
                inline = st.get("inline") or {}
                if not inline and st.get("testCase"):
                    issues.append(f"Step {i} calls another test case, which is not expanded")
                    continue
                steps.append(TestStep(number=len(steps) + 1, action_text=_strip_markup(inline.get("description", "")),
                                      expected=_strip_markup(inline.get("expectedResult", "")),
                                      test_data=_strip_markup(inline.get("testData", ""))))
        except IntegrationError:
            pass
        if not steps:
            try:
                script = self.client.test_script(key)
                if script.get("text"):
                    for i, (_, line) in enumerate(_numbered(script["text"]), start=1):
                        steps.append(TestStep(number=i, action_text=line))
            except IntegrationError:
                pass
        project = (meta.get("project") or {}).get("key") or key.split("-T")[0]
        tc = TestCase(key=key, title=meta.get("name") or key, source="zephyr",
                      preconditions=_strip_markup(meta.get("precondition") or ""),
                      description=_strip_markup(meta.get("objective") or ""),
                      steps=steps, source_ref={"zephyr_key": key, "project_key": project})
        tc.issues.extend(issues)
        if not steps:
            tc.issues.append("Zephyr test case has no step-by-step or plain-text script")
        return tc

    def test_cases_for(self, ref: str) -> tuple[list[TestCase], list[str]]:
        ref = (ref or "").strip().upper()
        notes: list[str] = []
        if ZEPHYR_TEST_KEY.match(ref):
            keys = [ref]
        elif ISSUE_KEY.match(ref):
            keys = [t.get("key") for t in self.client.test_cases_for_issue(ref) if t.get("key")]
            if not keys:
                notes.append(f"No Zephyr test cases are linked to {ref}.")
        elif re.fullmatch(r"[A-Z][A-Z0-9_]+", ref):
            keys = [t.get("key") for t in self.client.test_cases_for_project(ref) if t.get("key")]
        else:
            raise IntegrationError("Enter a Zephyr test case key (PROJ-T1), a Jira issue key or a project key.")
        cases = []
        for k in keys:
            try:
                tc = self._to_case(k)
                if ISSUE_KEY.match(ref):
                    tc.source_ref["jira_issue"] = ref
                cases.append(tc)
            except IntegrationError as exc:
                notes.append(f"Skipped {k}: {exc}")
        return cases, notes

    def publish(self, tc, result, evidence_reader, run_id):
        key = tc.source_ref.get("zephyr_key")
        if not key:
            return PUB_SKIPPED, "", "Not a Zephyr test case"
        if not self.test_cycle:
            return PUB_SKIPPED, "", "No Zephyr test cycle selected; result not published"
        comment = f"Automated run {run_id}: {result.status}."
        if result.failure_reason:
            comment += f" Step {result.failed_step}: {result.failure_reason}"
        if result.evidence:
            comment += f" {len(result.evidence)} screenshot(s) are stored with the run in BIG QA (not uploaded to Zephyr)."
        payload = {
            "projectKey": tc.source_ref.get("project_key"),
            "testCaseKey": key,
            "testCycleKey": self.test_cycle,
            "statusName": ZEPHYR_STATUS.get(result.status, "Not Executed"),
            "actualEndDate": result.ended_at or None,
            "executionTime": result.duration_ms,
            "comment": html.escape(comment[:4000]),
        }
        if result.steps and len(result.steps) == len(tc.steps):
            payload["testScriptResults"] = [
                {"statusName": ZEPHYR_STATUS.get(s.status, "Not Executed"),
                 "actualEndDate": result.ended_at or None,
                 "actualResult": html.escape((s.actual or s.error or "")[:2000])} for s in result.steps]
        try:
            created = self.client.create_execution(payload)
        except IntegrationError as exc:
            return PUB_FAILED, "", str(exc)
        ref = str(created.get("key") or created.get("id") or "")
        status = PUB_PUBLISHED
        msg = f"Test execution {ref} created"
        if result.evidence:
            msg += "; screenshots not uploaded (this integration does not upload evidence to Zephyr)"
        return status, ref, msg


def _numbered(text: str):
    for ln in _strip_markup(text).splitlines():
        m = re.match(r"^\s*(?:\d+[.)]|[-*#])?\s*(.+)$", ln)
        if m and m.group(1).strip():
            yield None, m.group(1).strip()

