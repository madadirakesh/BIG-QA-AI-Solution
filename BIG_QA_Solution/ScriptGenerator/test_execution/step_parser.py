"""
Deterministic natural-language -> action translation.

The parser only handles phrasings it can read unambiguously; anything else is
left `unresolved` for the AI planner (or reported as Blocked when AI is off).
It never guesses an assertion: an expected result it cannot turn into a check
keeps the step from passing.
"""
from __future__ import annotations

import re
from typing import Optional

from .models import Action, Target, TestCase, TestStep, ACTION_SCHEMA

# Secret names handed to Playwright MCP via --secrets. The browser receives the
# real value; responses, logs and AI prompts only ever see the name.
SECRET_PASSWORD = "TE_APP_PASSWORD"
PLACEHOLDER_USERNAME = "{{username}}"

_Q = r"[\"'“”‘’]"        # straight and curly quotes
_QUOTED = re.compile(rf"{_Q}([^\"'“”‘’]+){_Q}")
_URL = re.compile(r"(https?://[^\s\"'<>]+|(?<![\w/])/[\w\-./?=&%#]*)")
_KEYS = {"enter": "Enter", "return": "Enter", "tab": "Tab", "escape": "Escape", "esc": "Escape",
         "space": " ", "backspace": "Backspace", "delete": "Delete", "arrow down": "ArrowDown",
         "arrow up": "ArrowUp", "down arrow": "ArrowDown", "up arrow": "ArrowUp", "page down": "PageDown"}
_ELEMENT_NOUNS = {
    "button": "button", "btn": "button", "link": "link", "tab": "tab", "menu": "menuitem", "menu item": "menuitem",
    "checkbox": "checkbox", "check box": "checkbox", "radio": "radio", "radio button": "radio", "icon": "",
    "option": "option", "dropdown": "combobox", "drop down": "combobox", "field": "textbox", "textbox": "textbox",
    "text box": "textbox", "input": "textbox", "box": "textbox", "heading": "heading", "image": "img",
}
_NOUN_RE = r"(?:radio button|check box|text box|menu item|drop down|button|btn|link|tab|menu|checkbox|radio|icon|option|dropdown|field|textbox|input|box|heading|image)"
_APP_PLACES = re.compile(
    r"^(?:the\s+)?(?:application|app|website|web site|site|portal|home ?page|landing page|login page|"
    r"log ?in page|sign ?in page|base url|application url|app url|url)\b", re.I)
_VERIFY_PREFIX = re.compile(r"^(?:verify|validate|check|ensure|confirm|assert|observe|see)\s+(?:that\s+)?", re.I)
_SHOWN = r"(?:is |are |should be |must be |gets |get |will be )?(?:displayed|shown|visible|present|appears?|seen|rendered)"


def _clean(text: str) -> str:
    t = " ".join((text or "").split()).strip()
    return t.rstrip(".;")


def _strip_article(s: str) -> str:
    return re.sub(r"^(?:the|a|an|on|in|into)\s+", "", s.strip(), flags=re.I).strip()


def _target_from_phrase(phrase: str, default_role: str = "") -> Optional[Target]:
    """'the "Login" button' -> role button Login; '#submit' -> css; 'Username field' -> label Username."""
    p = _strip_article(_clean(phrase))
    if not p:
        return None
    if re.match(r"^(?:css=|xpath=|//|#[\w-]+$|\.[\w-]+$|\[[^\]]+\]$)", p):
        if p.startswith("xpath=") or p.startswith("//"):
            return Target(kind="xpath", value=p[6:] if p.startswith("xpath=") else p)
        return Target(kind="css", value=p[4:] if p.startswith("css=") else p)
    role = default_role
    m = re.match(rf"^(.*?)\s+{_NOUN_RE}$", p, re.I)
    if m and m.group(1).strip():
        noun = p[len(m.group(1)):].strip().lower()
        role = _ELEMENT_NOUNS.get(noun, role)
        p = m.group(1).strip()
    q = _QUOTED.search(p)
    name = q.group(1).strip() if q else p.strip(" \"'")
    if not name or len(name) > 80:
        return None
    if role in ("textbox", "combobox"):
        return Target(kind="label", value=name, role=role)
    if role:
        return Target(kind="role", value=name, role=role)
    return Target(kind="text", value=name)


def _parse_test_data(text: str) -> dict[str, str]:
    """'username: alice, password = x' -> {'username': 'alice', ...} (keys lower-cased)."""
    out = {}
    for part in re.split(r"[\n;,|]", text or ""):
        m = re.match(r"^\s*([A-Za-z][\w .-]{0,40}?)\s*[:=]\s*(.+?)\s*$", part)
        if m:
            out[m.group(1).strip().lower()] = m.group(2).strip().strip("\"'")
    return out


def _value_for_field(field: str, adjective: str, data: dict[str, str], has_credentials: bool) -> Optional[tuple[str, bool]]:
    """Value for 'enter <adjective> <field>' with no literal value: (value, is_secret) or None."""
    f = field.lower().strip()
    for k, v in data.items():
        if k == f or k.replace(" ", "") == f.replace(" ", ""):
            if "password" in f and v.lower() in ("{{password}}", "<password>", "valid password"):
                return SECRET_PASSWORD, True
            return v, False
    if has_credentials and adjective.lower() in ("", "valid", "correct", "registered"):
        if "password" in f:
            return SECRET_PASSWORD, True
        if any(w in f for w in ("username", "user name", "user id", "userid", "email", "login id")):
            return PLACEHOLDER_USERNAME, False
    return None


def _resolve_literal(value: str) -> tuple[str, bool]:
    v = value.strip()
    if v.lower() in ("{{password}}", "<password>", "${password}"):
        return SECRET_PASSWORD, True
    if v.lower() in ("{{username}}", "<username>", "${username}"):
        return PLACEHOLDER_USERNAME, False
    return v, False


def parse_action_text(text: str, test_data: str = "", has_base_url: bool = True,
                      has_credentials: bool = False) -> Optional[list[Action]]:
    """Actions for a step's action text, [] for a purely informational step, None if not understood."""
    t = _clean(text)
    if not t:
        return []
    data = _parse_test_data(test_data)
    low = t.lower()

    # Verification written in the action column ("Verify 'Welcome' is displayed").
    if _VERIFY_PREFIX.match(t):
        checks = parse_expected_text(t)
        if checks is not None:
            return checks
        if not re.match(r"^(?:check|tick)\b", t, re.I):
            return None   # "Verify ..." that we can't check; "Check the X checkbox" falls through

    # navigate
    m = re.match(r"^(?:navigate|go|browse|redirect)\s+(?:to\s+)?(.+)$|^(?:open|launch|visit|load|access)\s+(.+)$", t, re.I)
    if m:
        rest = (m.group(1) or m.group(2)).strip()
        url = _URL.search(rest)
        if url:
            return [Action("navigate", value=url.group(1).rstrip(".,)"))]
        q = _QUOTED.search(rest)
        if q and ("." in q.group(1) or q.group(1).startswith("/")):
            return [Action("navigate", value=q.group(1))]
        rest_core = re.sub(r"^(?:the\s+)?(?:browser\s+and\s+(?:navigate|go)\s+to\s+)?", "", rest, flags=re.I)
        if _APP_PLACES.match(rest_core) or re.match(r"^(?:the\s+)?browser$", rest, re.I):
            return [Action("navigate", value="/")] if has_base_url else None
        return None

    # press key
    m = re.match(r"^(?:press|hit|tap)\s+(?:the\s+)?(.+?)(?:\s+key)?(?:\s+on\s+(?:the\s+)?keyboard)?$", t, re.I)
    if m and m.group(1).lower().strip(" \"'") in _KEYS:
        return [Action("press", value=_KEYS[m.group(1).lower().strip(" \"'")])]

    # wait N seconds / wait for "text"
    m = re.match(r"^wait\s+(?:for\s+)?(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?)\b", t, re.I)
    if m:
        return [Action("wait", value=str(min(float(m.group(1)), 30)))]
    m = re.match(rf"^wait\s+(?:for|until)\s+(?:the\s+)?(?:text\s+)?{_Q}(.+?){_Q}", t, re.I)
    if m:
        return [Action("wait_for_text", value=m.group(1))]

    # select "X" from Y
    m = re.match(rf"^(?:select|choose|pick)\s+{_Q}(.+?){_Q}\s+(?:from|in|on)\s+(?:the\s+)?(.+?)$", t, re.I)
    if m:
        target = _target_from_phrase(m.group(2), default_role="combobox")
        if target:
            if target.kind == "role":
                target = Target(kind="label", value=target.value, role="combobox")
            return [Action("select", target=target, value=m.group(1))]

    # check / uncheck
    m = re.match(r"^(un ?check|untick|deselect|check|tick)\s+(?:the\s+)?(.+?)$", t, re.I)
    if m and ("checkbox" in low or "check box" in low or _QUOTED.search(m.group(2))):
        target = _target_from_phrase(m.group(2), default_role="checkbox")
        if target:
            kind = "uncheck" if m.group(1).lower().replace(" ", "") in ("uncheck", "untick", "deselect") else "check"
            return [Action(kind, target=target)]

    # type: Enter "value" in/into <field>
    m = re.match(rf"^(?:enter|type|input|fill(?:\s+in)?|provide|key\s+in|insert)\s+{_Q}(.*?){_Q}\s+(?:in|into|on|for|as)\s+(?:the\s+)?(.+?)$", t, re.I)
    if m:
        target = _target_from_phrase(m.group(2), default_role="textbox")
        if target:
            value, secret = _resolve_literal(m.group(1))
            return [Action("type", target=_as_field(target), value=value, secret=secret)]

    # type: Enter <field> as/with/: "value"
    m = re.match(r"^(?:enter|type|input|fill(?:\s+in)?|provide|set)\s+(?:the\s+)?(.+?)\s+(?:as|with|=|:|to)\s+(.+)$", t, re.I)
    if m and not re.search(r"\b(?:and|then)\b", m.group(1), re.I):
        target = _target_from_phrase(m.group(1), default_role="textbox")
        raw_val = m.group(2).strip()
        q = _QUOTED.fullmatch(raw_val) or _QUOTED.match(raw_val)
        val = q.group(1) if q else raw_val
        if target and val:
            value, secret = _resolve_literal(val)
            return [Action("type", target=_as_field(target), value=value, secret=secret)]

    # type: Enter (a) valid username / Enter password  (value from Test Data or project credentials)
    m = re.match(r"^(?:enter|type|input|fill(?:\s+in)?|provide)\s+(?:a\s+|an\s+|the\s+)?(valid\s+|correct\s+|registered\s+|invalid\s+|wrong\s+|incorrect\s+)?(.+?)(?:\s+(?:field|textbox|text box|box|input))?$", t, re.I)
    if m:
        adjective = (m.group(1) or "").strip()
        field = re.sub(r"\s+(?:in|into)\s+.*$", "", m.group(2)).strip()
        resolved = _value_for_field(field, adjective, data, has_credentials)
        if resolved and len(field) <= 40:
            return [Action("type", target=Target(kind="label", value=field, role="textbox"),
                           value=resolved[0], secret=resolved[1])]
        return None

    # hover
    m = re.match(r"^(?:hover|mouse ?over|move\s+(?:the\s+)?mouse)\s+(?:over|on|to)?\s*(.+)$", t, re.I)
    if m:
        target = _target_from_phrase(m.group(1))
        return [Action("hover", target=target)] if target else None

    # click (last: "select"/"press" fall through to here when they name an element)
    m = re.match(r"^(double[\s-]?click|click|tap|press|hit|select|choose)\s+(?:on\s+)?(.+?)$", t, re.I)
    if m:
        phrase = re.sub(r"\s+(?:to\s+\w+.*|in order to.*)$", "", m.group(2), flags=re.I)
        if m.group(1).lower() in ("select", "choose") and not (_QUOTED.search(phrase) or re.search(rf"\b{_NOUN_RE}$", phrase, re.I)):
            return None   # "Select a product" is too vague to click deterministically
        target = _target_from_phrase(phrase)
        if target:
            kind = "double_click" if m.group(1).lower().startswith("double") else "click"
            return [Action(kind, target=target)]
    return None


def _as_field(target: Target) -> Target:
    if target.kind in ("role", "text"):
        return Target(kind="label", value=target.value, role="textbox")
    return target


def parse_expected_text(text: str) -> Optional[list[Action]]:
    """Assertions for an expected-result sentence, None if it can't be checked deterministically."""
    t = _clean(text)
    if not t:
        return []
    body = _VERIFY_PREFIX.sub("", t)
    low = body.lower()
    actions: list[Action] = []

    m = re.search(rf"(?:page\s+)?title\s+(?:is|should be|should contain|contains|equals|=)\s+{_Q}(.+?){_Q}", body, re.I)
    if m:
        actions.append(Action("assert_title", value=m.group(1)))
    m = re.search(rf"url\s+(?:is|should be|should contain|contains|includes|ends with)\s+{_Q}?([^\s\"']+){_Q}?", body, re.I)
    if m:
        actions.append(Action("assert_url", value=m.group(1).rstrip(".,")))
    m = re.search(rf"(?:redirected|navigated|taken|routed)\s+to\s+(?:the\s+)?{_Q}?((?:https?://|/)[^\s\"']+){_Q}?", body, re.I)
    if m:
        actions.append(Action("assert_url", value=m.group(1).rstrip(".,")))
    m = re.search(rf"^{_Q}?(.+?){_Q}?\s+(?:field|textbox|input|box)\s+(?:should\s+)?(?:contain|contains|has value|have value|shows|displays|value is)\s+{_Q}(.+?){_Q}", body, re.I)
    if m:
        actions.append(Action("assert_value", target=Target(kind="label", value=m.group(1), role="textbox"), value=m.group(2)))
    if actions:
        return actions

    negative = bool(re.search(r"\b(?:not|no longer|isn't|is not|should not|shouldn't|never)\b", low))
    quoted = [q.strip() for q in _QUOTED.findall(body) if q.strip()]
    if quoted and re.search(rf"{_SHOWN}|\b(?:message|text|error|label|heading|banner|toast|alert|contain|contains|see|displays?|shows?|reads?)\b", low):
        kind = "assert_not_text" if negative else "assert_text"
        return [Action(kind, value=q) for q in quoted]

    # "Login button is displayed" (unquoted element + visibility)
    m = re.match(rf"^(?:the\s+)?(.+?)\s+{_NOUN_RE}\s+{_SHOWN}$", body, re.I)
    if m and not negative and len(m.group(1)) <= 40:
        noun = re.search(rf"{_NOUN_RE}(?=\s+{_SHOWN}$)", body, re.I).group(0).lower()
        role = _ELEMENT_NOUNS.get(noun, "")
        target = Target(kind="role", value=m.group(1).strip(), role=role) if role and role != "textbox" \
            else Target(kind="label" if role == "textbox" else "text", value=m.group(1).strip(), role=role)
        return [Action("assert_visible", target=target)]
    return None


def validate_action(a: Action) -> Optional[str]:
    """Error message when an action (typically from the AI) violates the schema, else None."""
    spec = ACTION_SCHEMA.get(a.type)
    if spec is None:
        return f"unsupported action '{a.type}'"
    for req in spec["required"]:
        if req == "target" and (a.target is None or not a.target.value):
            return f"'{a.type}' needs a target"
        if req == "value" and not str(a.value).strip():
            return f"'{a.type}' needs a value"
    if a.target is not None:
        if a.target.kind not in ("css", "xpath", "text", "label", "role", "placeholder", "testid", "ref"):
            return f"unsupported target kind '{a.target.kind}'"
        if len(a.target.value) > 300:
            return "target too long"
    if a.type == "wait":
        try:
            if not 0 <= float(a.value) <= 30:
                return "wait must be 0-30 seconds"
        except ValueError:
            return "wait value must be a number"
    if a.secret and a.value != SECRET_PASSWORD:
        return "unknown secret reference"
    return None


def plan_step(step: TestStep, test_data: str, has_base_url: bool, has_credentials: bool) -> TestStep:
    """Fills step.actions deterministically, or marks the step unresolved for the AI planner."""
    data = "\n".join(x for x in (test_data, step.test_data) if x)
    acts = parse_action_text(step.action_text, data, has_base_url, has_credentials)
    exp = parse_expected_text(step.expected) if step.expected else []
    if acts is None or exp is None:
        step.actions = []
        step.plan_source = "unresolved"
        missing = []
        if acts is None:
            missing.append("action")
        if exp is None:
            missing.append("expected result")
        step.plan_error = "Could not interpret the " + " and ".join(missing) + " deterministically"
        return step
    step.actions = (acts or []) + (exp or [])
    if not step.actions:
        step.actions = [Action("note", value=step.action_text[:200])]
    step.plan_source = "parser"
    step.plan_error = ""
    return step


def plan_test_case(tc: TestCase, has_base_url: bool, has_credentials: bool) -> TestCase:
    for s in tc.steps:
        if s.plan_source in ("ai", "cache") and s.actions:
            continue
        plan_step(s, tc.test_data, has_base_url, has_credentials)
    return tc
