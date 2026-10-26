"""Jira / Zephyr adapters against MOCKED clients. These are not live integration tests."""
from types import SimpleNamespace as NS

import pytest

from test_execution.integrations import (
    JiraAdapter, ZephyrAdapter, ZephyrScaleClient, IntegrationError, parse_jira_test_description,
)
from test_execution.models import (
    TestCaseResult, StepResult, Evidence, PASSED, FAILED,
    PUB_PUBLISHED, PUB_FAILED, PUB_SKIPPED,
)


# ── Jira ────────────────────────────────────────────────────────────────────

def test_parse_wiki_table_steps():
    d = parse_jira_test_description(
        "h3. Preconditions\nUser exists\n\nh3. Test Steps\n||#||Step||Expected||\n"
        "|1|Open the application|Login page shown|\n|2|Click *Login* button|'Welcome' is displayed|\n"
        "h3. Test Data\nusername: alice")
    assert d["preconditions"] == "User exists"
    assert d["steps"] == [("Open the application", "Login page shown"), ("Click Login button", "'Welcome' is displayed")]
    assert d["test_data"] == "username: alice"


def test_parse_numbered_steps_with_arrows():
    d = parse_jira_test_description("Steps:\n# Open the application\n# Click Login -> 'Welcome' is displayed\n"
                                    "Expected Result:\nUser is logged in")
    assert d["steps"] == [("Open the application", ""), ("Click Login", "'Welcome' is displayed")]
    assert d["expected"] == "User is logged in"


def _issue(key, type_, summary="s", description="", links=(), subtasks=()):
    return NS(key=key, fields=NS(issuetype=type_, summary=summary, description=description,
                                 issuelinks=list(links), subtasks=list(subtasks), status="Open"))


class FakeJira:
    def __init__(self):
        self.issues = {
            "PROJ-1": _issue("PROJ-1", "Story", links=[
                NS(type=NS(name="Test", inward="is tested by", outward="tests"), inwardIssue=NS(key="PROJ-2", fields=NS(issuetype="Test"))),
                NS(type=NS(name="Relates", inward="relates to", outward="relates to"), outwardIssue=NS(key="PROJ-9", fields=NS(issuetype="Bug"))),
            ]),
            "PROJ-2": _issue("PROJ-2", "Test", "Login", "Steps:\n# Open the application\n# Click Login button"),
            "PROJ-3": _issue("PROJ-3", "Story"),
        }
        self.comments_by_issue = {}
        self.attachments = []
        self.fail_attach = False
        self.fail_comment = False

    def issue(self, key):
        return self.issues[key]

    def search_issues(self, jql, maxResults=25, fields=None):
        self.last_jql = jql
        return [self.issues["PROJ-2"]]

    def comments(self, key):
        return self.comments_by_issue.get(key, [])

    def add_comment(self, key, body):
        if self.fail_comment:
            raise RuntimeError("503 Service Unavailable")
        c = NS(id=str(len(self.comments_by_issue.get(key, [])) + 100), body=body)
        self.comments_by_issue.setdefault(key, []).append(c)
        return c

    def add_attachment(self, issue, attachment, filename):
        if self.fail_attach:
            raise RuntimeError("413 too large")
        self.attachments.append((issue, filename, attachment.read()))


def _result(tc, status=PASSED, evidence=0):
    r = TestCaseResult(run_id="TE-20261023-aaaaaaaa", test_key=tc.key, title=tc.title, source=tc.source, status=status,
                       ended_at="2026-10-23T10:00:00+00:00", duration_ms=1200,
                       steps=[StepResult(s.number, s.action_text, s.expected, status, "ok") for s in tc.steps])
    r.evidence = [Evidence(f"ev{i}", tc.key, None, status, "t", f"TE/x{i}.png", 3) for i in range(evidence)]
    return r


def test_jira_story_resolves_linked_tests_only():
    fake = FakeJira()
    cases, notes = JiraAdapter(lambda: fake).test_cases_for("PROJ-1")
    assert [c.key for c in cases] == ["PROJ-2"]
    assert [s.action_text for s in cases[0].steps] == ["Open the application", "Click Login button"]
    cases, notes = JiraAdapter(lambda: fake).test_cases_for("PROJ-3")
    assert cases == [] and "not treated as an executable test" in notes[0]


def test_jira_search_by_key_uses_key_jql():
    fake = FakeJira()
    JiraAdapter(lambda: fake).search("proj-2")
    assert fake.last_jql == 'key = "PROJ-2"'


def test_jira_publish_comment_attachments_and_duplicate_prevention():
    fake = FakeJira()
    adapter = JiraAdapter(lambda: fake)
    tc = JiraAdapter(lambda: fake).test_cases_for("PROJ-2")[0][0]
    r = _result(tc, FAILED, evidence=2)
    status, ref, msg = adapter.publish(tc, r, lambda ev: b"png", r.run_id)
    assert status == PUB_PUBLISHED and ref.startswith("comment:")
    assert len(fake.attachments) == 2
    body = fake.comments_by_issue["PROJ-2"][0].body
    assert "Failed" in body and "thumbnail" in body and "BIG-QA-RUN" in body
    # same result again (e.g. retry after a lost response): no second comment
    status, _, msg = adapter.publish(tc, r, lambda ev: b"png", r.run_id)
    assert status == PUB_PUBLISHED and "Already published" in msg
    assert len(fake.comments_by_issue["PROJ-2"]) == 1 and len(fake.attachments) == 2


def test_jira_publication_failures_are_reported_not_hidden():
    fake = FakeJira()
    adapter = JiraAdapter(lambda: fake)
    tc = adapter.test_cases_for("PROJ-2")[0][0]
    fake.fail_comment = True
    assert adapter.publish(tc, _result(tc), lambda ev: b"x", "R")[0] == PUB_FAILED
    fake.fail_comment, fake.fail_attach = False, True
    status, _, msg = adapter.publish(tc, _result(tc, evidence=1), lambda ev: b"x", "R")
    assert status == PUB_FAILED and "attachment errors" in msg


def test_jira_unavailable_raises_integration_error():
    def boom():
        raise RuntimeError("Jira credentials not found in .env")
    with pytest.raises(IntegrationError):
        JiraAdapter(boom).search("PROJ-1")


# ── Zephyr ──────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, code, payload=None, headers=None):
        self.status_code, self._p, self.headers = code, payload, headers or {}
        self.content = b"x" if payload is not None else b""
        self.text = str(payload)

    def json(self):
        return self._p


class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.sent = []

    def request(self, method, url, timeout=None, **kw):
        self.sent.append((method, url, kw))
        handler = self.routes[(method, url.split("/v2", 1)[1].split("?")[0])]
        return handler(kw) if callable(handler) else handler


def _zephyr(routes):
    http = FakeHTTP(routes)
    return ZephyrScaleClient("tok", session=http, max_retries=2), http


ROUTES = {
    ("GET", "/testcases/PROJ-T1"): FakeResp(200, {"key": "PROJ-T1", "name": "Login", "project": {"key": "PROJ"},
                                                  "precondition": "<p>User exists</p>"}),
    ("GET", "/testcases/PROJ-T1/teststeps"): FakeResp(200, {"values": [
        {"inline": {"description": "Open the application", "testData": "", "expectedResult": "<b>Login</b> page shown"}},
        {"inline": {"description": "Click Login button", "testData": "", "expectedResult": ""}}]}),
    ("GET", "/issuelinks/PROJ-5/testcases"): FakeResp(200, [{"key": "PROJ-T1"}]),
}


def test_zephyr_retrieval_by_key_and_issue():
    client, http = _zephyr(ROUTES)
    assert http.headers["Authorization"] == "Bearer tok"
    adapter = ZephyrAdapter(client)
    [tc], _ = adapter.test_cases_for("PROJ-T1")
    assert tc.source == "zephyr" and tc.preconditions == "User exists"
    assert [s.expected for s in tc.steps] == ["Login page shown", ""]
    [tc2], _ = adapter.test_cases_for("PROJ-5")
    assert tc2.source_ref["jira_issue"] == "PROJ-5"


def test_zephyr_publish_maps_status_and_requires_cycle():
    created = {}

    def create(kw):
        created.update(kw["json"])
        return FakeResp(201, {"id": 77, "key": "PROJ-E77"})
    client, _ = _zephyr({**ROUTES, ("POST", "/testexecutions"): create})
    tc = ZephyrAdapter(client).test_cases_for("PROJ-T1")[0][0]
    r = _result(tc, FAILED, evidence=1)
    assert ZephyrAdapter(client).publish(tc, r, None, "R")[0] == PUB_SKIPPED      # no cycle chosen
    status, ref, msg = ZephyrAdapter(client, "PROJ-R1").publish(tc, r, None, "R")
    assert status == PUB_PUBLISHED and ref == "PROJ-E77"
    assert created["statusName"] == "Fail" and created["testCycleKey"] == "PROJ-R1" and created["projectKey"] == "PROJ"
    assert len(created["testScriptResults"]) == 2
    assert "not uploaded" in msg       # evidence upload is unsupported, and we say so


def test_zephyr_rate_limit_is_retried_then_errors(monkeypatch):
    monkeypatch.setattr("test_execution.integrations.time.sleep", lambda s: None)
    calls = []

    def limited(kw):
        calls.append(1)
        return FakeResp(429, {}, {"Retry-After": "1"}) if len(calls) < 2 else FakeResp(200, {"key": "PROJ-T1", "name": "x"})
    client, _ = _zephyr({("GET", "/testcases/PROJ-T1"): limited})
    assert client.test_case("PROJ-T1")["name"] == "x" and len(calls) == 2
    client, _ = _zephyr({("GET", "/testcases/PROJ-T1"): FakeResp(404, {"message": "not found"})})
    with pytest.raises(IntegrationError):
        client.test_case("PROJ-T1")


def test_zephyr_requires_token():
    with pytest.raises(IntegrationError):
        ZephyrScaleClient("")
