import pytest

from test_execution.models import Action, Target, TestStep
from test_execution.security import (
    NavigationPolicy, PolicyError, Redactor, is_production_url, safe_join, sanitize_filename,
)
from test_execution.step_parser import (
    SECRET_PASSWORD, PLACEHOLDER_USERNAME, parse_action_text, parse_expected_text, plan_step, validate_action,
)


def types(actions):
    return [a.type for a in actions]


@pytest.mark.parametrize("text,expected_type,target", [
    ("Navigate to https://app.test/login", "navigate", None),
    ("Open the application", "navigate", None),
    ("Go to /settings/profile", "navigate", None),
    ("Click on Login button", "click", ("role", "Login", "button")),
    ("Click 'Save changes'", "click", ("text", "Save changes", "")),
    ("Tap the Home link", "click", ("role", "Home", "link")),
    ("Double-click the 'Report' row", "double_click", None),
    ("Enter 'alice' in Username field", "type", ("label", "Username", "textbox")),
    ("Type “bob” into the search box", "type", ("label", "search", "textbox")),
    ("Enter email as alice@example.com", "type", ("label", "email", "textbox")),
    ("Select 'Admin' from Role dropdown", "select", ("label", "Role", "combobox")),
    ("Check the Remember me checkbox", "check", ("role", "Remember me", "checkbox")),
    ("Uncheck the 'Subscribe' checkbox", "uncheck", None),
    ("Press Enter", "press", None),
    ("Wait for 2 seconds", "wait", None),
    ("Hover over the Profile menu", "hover", None),
    ("Click #submit-btn", "click", ("css", "#submit-btn", "")),
])
def test_action_phrases(text, expected_type, target):
    acts = parse_action_text(text)
    assert acts and acts[0].type == expected_type, text
    if target:
        t = acts[0].target
        assert (t.kind, t.value, t.role) == target


def test_credentials_resolve_to_secret_names_not_values():
    acts = parse_action_text("Enter valid password", has_credentials=True)
    assert acts[0].value == SECRET_PASSWORD and acts[0].secret
    acts = parse_action_text("Enter a valid username", has_credentials=True)
    assert acts[0].value == PLACEHOLDER_USERNAME and not acts[0].secret
    assert parse_action_text("Enter valid password", has_credentials=False) is None
    acts = parse_action_text("Enter password", test_data="password: {{password}}")
    assert acts[0].secret


def test_test_data_supplies_values():
    acts = parse_action_text("Enter username", test_data="username: carol; city = Paris")
    assert acts[0].value == "carol"


def test_vague_steps_are_not_guessed():
    assert parse_action_text("Do something clever with the widget") is None
    assert parse_action_text("Select a product") is None
    assert parse_action_text("Verify the dashboard looks correct") is None
    assert parse_action_text("Enter valid credentials and click login", has_credentials=True) is None


@pytest.mark.parametrize("text,kinds", [
    ("'Welcome back' message is displayed", ["assert_text"]),
    ("Verify that \"Invalid credentials\" error is shown", ["assert_text"]),
    ("'Error' should not be displayed", ["assert_not_text"]),
    ("Page title should be 'Dashboard'", ["assert_title"]),
    ("URL contains '/dashboard'", ["assert_url"]),
    ("User is redirected to /home", ["assert_url"]),
    ("Login button is displayed", ["assert_visible"]),
    ("'Email' field should contain 'a@b.c'", ["assert_value"]),
    ("Both 'Name' and 'Age' columns are shown", ["assert_text", "assert_text"]),
])
def test_expected_phrases(text, kinds):
    assert types(parse_expected_text(text)) == kinds


def test_unverifiable_expectation_keeps_step_unresolved():
    step = plan_step(TestStep(1, "Click on Login button", "User has a great experience"), "", True, False)
    assert step.plan_source == "unresolved" and "expected result" in step.plan_error
    step = plan_step(TestStep(1, "Click on Login button", ""), "", True, False)
    assert step.plan_source == "parser" and types(step.actions) == ["click"]


def test_validate_action_rejects_out_of_schema():
    assert validate_action(Action("run_shell", value="rm -rf /")) == "unsupported action 'run_shell'"
    assert validate_action(Action("click")) == "'click' needs a target"
    assert validate_action(Action("click", target=Target("javascript", "x"))).startswith("unsupported target")
    assert validate_action(Action("wait", value="999")) == "wait must be 0-30 seconds"
    assert validate_action(Action("type", target=Target("label", "x"), value="OTHER_SECRET", secret=True)) == "unknown secret reference"
    assert validate_action(Action("assert_text", value="ok")) is None


# ── security ────────────────────────────────────────────────────────────────

def test_navigation_policy():
    p = NavigationPolicy("https://app.test/login", ["sso.test", "*.cdn.test"], resolve_dns=False)
    assert p.resolve("/") == "https://app.test/login"
    assert p.resolve("/dashboard") == "https://app.test/dashboard"
    assert p.resolve("https://sso.test/auth") == "https://sso.test/auth"
    assert p.resolve("https://img.cdn.test/x") == "https://img.cdn.test/x"
    for bad in ("https://evil.example/", "file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi",
                "http://169.254.169.254/latest/meta-data", "http://metadata.google.internal/"):
        with pytest.raises(PolicyError):
            p.resolve(bad)


def test_metadata_blocked_even_if_allowlisted():
    p = NavigationPolicy("https://app.test/", ["169.254.169.254", "metadata.google.internal"], resolve_dns=False)
    with pytest.raises(PolicyError):
        p.check("http://169.254.169.254/")
    with pytest.raises(PolicyError):
        p.check("http://metadata.google.internal/")


def test_base_url_must_be_http():
    with pytest.raises(PolicyError):
        NavigationPolicy("ftp://x.test/")


def test_production_guard(monkeypatch):
    monkeypatch.setenv("TEST_EXECUTION_PRODUCTION_HOSTS", "app.example.com,*.prod.example.com")
    assert is_production_url("https://app.example.com/login")
    assert is_production_url("https://eu.prod.example.com/")
    assert not is_production_url("https://staging.example.com/")


def test_filenames_and_traversal(tmp_path):
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("TC 01/<script>.png") == "script_.png"
    assert safe_join(str(tmp_path), "a", "b.png").startswith(str(tmp_path))
    with pytest.raises(PolicyError):
        safe_join(str(tmp_path), "..", "outside.txt")


def test_redactor():
    r = Redactor(["s3cret!"])
    assert r("typed s3cret! into field") == "typed ****** into field"
    assert "abc123" not in r("Authorization: Bearer abc123")
