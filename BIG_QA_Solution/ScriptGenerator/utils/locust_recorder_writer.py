"""
locust_recorder_writer.py
-------------------------
Turn a recorded browser journey into a runnable Locust script.

The recorder (ScriptRunnerEngine/performance_recorder.py) drives a real browser
and hands this module two parallel streams:

  * steps    - the functional UI actions the user performed (clicks, typing,
               submits, navigations), in order
  * requests - the document / XHR / fetch traffic the browser made, each tagged
               with the step that was active when it fired

This module weaves them back together: every step becomes a comment, and the
requests it triggered become `self.client.*` calls underneath it. The result is
a plain HttpUser that reads like the journey the tester actually performed,
which matters because someone has to maintain it afterwards.

Two things are deliberately NOT copied into the generated script:

  * Request headers (cookies, Authorization, CSRF tokens). Locust's client is a
    requests.Session, so it maintains its own cookies across the journey, and a
    recorded bearer token is both a secret and stale by the time the test runs.
  * Anything the user typed into a password field. Those values are swapped for
    an environment-variable lookup so a recording can be committed safely.
"""

import json
import os
import re
from datetime import datetime
from pprint import pformat
from urllib.parse import parse_qsl, urlsplit, urlunsplit

# Bound the generated file. A long exploratory session can produce thousands of
# requests; past a few hundred the script stops being reviewable and the point
# of recording (a readable, editable starting script) is lost. When the cap
# bites, the script says so rather than silently dropping the tail.
MAX_REQUESTS = 300
MAX_BODY_CHARS = 4000

FILENAME_PREFIX = "rec"
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

# Emitted instead of a recorded password. Read from the environment at runtime
# so the same script works across environments without holding a credential.
PASSWORD_ENV_VAR = "PERF_RECORDED_PASSWORD"
_PASSWORD_SENTINEL = "__BIGQA_RECORDED_PASSWORD__"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


# ── naming ──────────────────────────────────────────────────────────────────

def _sanitize_project_name(project_name):
    cleaned = _NON_ALNUM.sub("_", (project_name or "").strip()).strip("_")
    return cleaned or "performance"


def recorded_script_name(project_name, when=None):
    """`rec_{ProjectName}_{YYYYmmdd_HHMMSS}.py` - the recording naming convention."""
    when = when or datetime.now()
    return f"{FILENAME_PREFIX}_{_sanitize_project_name(project_name)}_{when.strftime(TIMESTAMP_FORMAT)}.py"


def recorded_script_title(project_name, when=None):
    """Human label for the Performance Test grid's 'Test Case' column."""
    when = when or datetime.now()
    return f"Recorded — {(project_name or 'performance').strip()} ({when.strftime('%d %b %Y %H:%M')})"


def _class_name(project_name):
    parts = [p for p in _NON_ALNUM.split(project_name or "") if p]
    name = "".join(part[:1].upper() + part[1:] for part in parts) or "Recorded"
    if name[0].isdigit():
        name = f"Rec{name}"
    return f"{name}User"


# ── url handling ────────────────────────────────────────────────────────────

def host_origin(application_url):
    """`https://shop.example.com/login?x=1` -> `https://shop.example.com`."""
    parts = urlsplit((application_url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _call_target(url, origin):
    """
    Return (target, stats_name) for a recorded URL.

    Same-origin requests become a relative path so the script honours the
    HttpUser's `host` and can be repointed at another environment by changing
    one line. Cross-origin calls (a separate API host, say) keep their absolute
    URL - requests accepts those unchanged.

    `stats_name` drops the query string so Locust aggregates `/search?q=a` and
    `/search?q=b` into one row instead of one row per value typed.
    """
    parts = urlsplit(url)
    request_origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path or "/"
    relative = urlunsplit(("", "", path, parts.query, "")) or "/"
    if origin and request_origin.lower() == origin.lower():
        return relative, path
    return url, f"{request_origin}{path}"


# ── payloads ────────────────────────────────────────────────────────────────

def _redact(value, secrets):
    """Swap any recorded secret for the sentinel the renderer turns into PASSWORD."""
    if not isinstance(value, str) or not secrets:
        return value
    for secret in secrets:
        if secret and secret in value:
            value = value.replace(secret, _PASSWORD_SENTINEL)
    return value


def _redact_tree(node, secrets):
    if isinstance(node, dict):
        return {key: _redact_tree(val, secrets) for key, val in node.items()}
    if isinstance(node, list):
        return [_redact_tree(item, secrets) for item in node]
    return _redact(node, secrets)


def _literal(value, indent):
    """Render a Python literal, wrapped and indented to sit inside a call."""
    text = pformat(value, width=88, sort_dicts=False)
    pad = " " * indent
    return text.replace("\n", "\n" + pad)


def _render_payload(request, secrets, indent):
    """
    Return (kwarg_source, uses_password) for a request body.

    JSON and form bodies are re-emitted as Python literals so they can be edited
    or parameterised later; anything else is passed through as a raw string.
    """
    body = request.get("post_data") or ""
    if not body:
        return "", False

    truncated = len(body) > MAX_BODY_CHARS
    if truncated:
        body = body[:MAX_BODY_CHARS]

    content_type = (request.get("content_type") or "").lower()

    if "json" in content_type or body.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if parsed is not None:
            rendered = _literal(_redact_tree(parsed, secrets), indent)
            return f"json={rendered}", _PASSWORD_SENTINEL in rendered

    if "x-www-form-urlencoded" in content_type or (not content_type and "=" in body and "\n" not in body):
        pairs = parse_qsl(body, keep_blank_values=True)
        if pairs:
            form = {key: _redact(value, secrets) for key, value in pairs}
            rendered = _literal(form, indent)
            return f"data={rendered}", _PASSWORD_SENTINEL in rendered

    raw = _redact(body, secrets)
    rendered = _literal(raw, indent)
    suffix = "  # body truncated by the recorder" if truncated else ""
    return f"data={rendered}{suffix}", _PASSWORD_SENTINEL in rendered


# ── script assembly ─────────────────────────────────────────────────────────

def _describe_step(step):
    action = (step.get("type") or "action").lower()
    target = (step.get("target") or "").strip()
    value = (step.get("value") or "").strip()

    if action == "navigate":
        return f"navigate to {step.get('url') or target}"
    if action == "click":
        return f"click {target}" if target else "click"
    if action == "input":
        shown = "***" if step.get("secret") else value
        return f"type {shown!r} into {target}" if target else f"type {shown!r}"
    if action == "select":
        return f"select {value!r} in {target}"
    if action == "submit":
        return f"submit {target}" if target else "submit form"
    if action == "press":
        return f"press {value or 'key'}" + (f" in {target}" if target else "")
    return " ".join(filter(None, (action, target, value)))


def _group_requests(steps, requests):
    """
    Bucket requests under the step that was active when they fired.

    Requests recorded before any UI action (the initial page load, mostly) land
    in a leading bucket keyed by -1 so they still make it into the script.
    """
    buckets = {-1: []}
    for step in steps:
        buckets[step.get("index")] = []
    for request in requests:
        buckets.setdefault(request.get("step_index", -1), []).append(request)
    return buckets


def build_locust_script(journey):
    """
    Render the recorded journey as Locust source.

    `journey` keys: project_name, application_url, steps, requests, secrets,
    started_at, finished_at (both datetimes).
    """
    project_name = journey.get("project_name") or "performance"
    application_url = journey.get("application_url") or ""
    origin = host_origin(application_url)
    steps = list(journey.get("steps") or [])
    requests = list(journey.get("requests") or [])
    secrets = [s for s in (journey.get("secrets") or []) if s]
    finished_at = journey.get("finished_at") or datetime.now()

    dropped = max(0, len(requests) - MAX_REQUESTS)
    if dropped:
        requests = requests[:MAX_REQUESTS]

    buckets = _group_requests(steps, requests)
    uses_password = False
    body_lines = []

    def emit_requests(entries):
        nonlocal uses_password
        for entry in entries:
            method = (entry.get("method") or "GET").lower()
            target, stats_name = _call_target(entry.get("url", ""), origin)
            payload, had_secret = _render_payload(entry, secrets, indent=12)
            uses_password = uses_password or had_secret

            args = [repr(target)]
            if payload:
                args.append(payload)
            args.append(f"name={stats_name!r}")

            single_line = f"        self.client.{method}({', '.join(args)})"
            if len(single_line) <= 110 and "\n" not in single_line:
                body_lines.append(single_line)
            else:
                body_lines.append(f"        self.client.{method}(")
                for arg in args:
                    body_lines.append(f"            {arg},")
                body_lines.append("        )")

    preamble = buckets.get(-1) or []
    if preamble:
        body_lines.append("        # Initial page load")
        emit_requests(preamble)

    for step in steps:
        entries = buckets.get(step.get("index")) or []
        comment = f"        # Step {step.get('index', 0) + 1} - {_describe_step(step)}"
        if body_lines:
            body_lines.append("")
        body_lines.append(comment)
        if entries:
            emit_requests(entries)
        else:
            # A click that only changed local state still belongs in the script:
            # it documents the journey and marks where to add a check later.
            body_lines.append("        # (no network traffic was recorded for this action)")

    if not body_lines:
        body_lines = [
            "        # The recording captured no requests. Browse the application",
            "        # after pressing Start so the recorder has traffic to convert.",
            "        self.client.get(\"/\", name=\"/\")",
        ]

    notes = [
        ["Headers (cookies, Authorization, CSRF) were not recorded. Locust's client",
         "keeps its own session, and a captured token is both a secret and stale by",
         "the time the test runs."],
        ["Static assets (images, CSS, JS, fonts, media) were skipped so the script",
         "measures the application rather than the CDN."],
    ]
    if dropped:
        notes.append([f"{dropped} request(s) beyond the first {MAX_REQUESTS} were not included."])
    if uses_password:
        notes.append([f"Passwords were replaced with the {PASSWORD_ENV_VAR} environment",
                      "variable, so this file is safe to commit."])

    file_name = recorded_script_name(project_name, finished_at)
    header = [
        '"""',
        file_name,
        "-" * len(file_name),
        f"Test Case: {recorded_script_title(project_name, finished_at)}",
        "",
        f"Recorded from : {application_url}",
        f"Recorded at   : {finished_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Journey       : {len(steps)} user action(s), {len(requests)} request(s)",
        "",
        "Generated by the BIG QA performance recorder. Review before load testing:",
    ]
    for note in notes:
        header.append(f"  * {note[0]}")
        header += [f"    {line}" for line in note[1:]]
    header += [
        "",
        "Run standalone (from the perf project root):",
        f"    locust -f locustfiles/{file_name}" + (f" --host {origin}" if origin else ""),
        '"""',
        "",
    ]

    imports = []
    if uses_password:
        imports += ["import os", ""]
    imports += ["from locust import HttpUser, task, between", ""]
    if uses_password:
        imports += [
            f'PASSWORD = os.getenv("{PASSWORD_ENV_VAR}", "")',
            "",
        ]

    class_lines = [
        "",
        f"class {_class_name(project_name)}(HttpUser):",
        f'    host = "{origin}"' if origin else "    # host comes from --host / the suite config",
        "    wait_time = between(1, 3)",
        "",
        "    @task",
        "    def recorded_journey(self):",
    ]

    source = "\n".join(header + imports + class_lines + body_lines) + "\n"
    # The renderer works with a sentinel so redaction survives pformat/repr;
    # swap it for the module-level constant now that the text is final.
    return source.replace(f"'{_PASSWORD_SENTINEL}'", "PASSWORD").replace(
        f'"{_PASSWORD_SENTINEL}"', "PASSWORD")


def write_recorded_script(locustfiles_dir, journey):
    """
    Write the generated script into the project's locustfiles folder.

    Returns (absolute_path, file_name). A name collision (two recordings inside
    the same second) gets a numeric suffix rather than overwriting.
    """
    finished_at = journey.get("finished_at") or datetime.now()
    project_name = journey.get("project_name") or "performance"

    os.makedirs(locustfiles_dir, exist_ok=True)
    file_name = recorded_script_name(project_name, finished_at)
    path = os.path.join(locustfiles_dir, file_name)
    suffix = 2
    while os.path.exists(path):
        stem = recorded_script_name(project_name, finished_at)[:-3]
        file_name = f"{stem}_{suffix}.py"
        path = os.path.join(locustfiles_dir, file_name)
        suffix += 1

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(build_locust_script(journey))
    return path, file_name
