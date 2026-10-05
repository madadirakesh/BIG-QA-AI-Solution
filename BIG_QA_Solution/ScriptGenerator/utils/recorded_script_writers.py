"""
recorded_script_writers.py
--------------------------
Turn a recorded browser journey into the script the project's tool and the
chosen script type call for.

The recorder (ScriptRunnerEngine/performance_recorder.py) captures two streams:
the UI steps the tester performed (each with a Selenium locator) and the HTTP
traffic they caused. Which stream becomes the script depends on the choice made
in Create Test:

  Type         Locust project                      JMeter project
  ----------   ---------------------------------   ------------------------------------------
  CLI          HTTP traffic -> Locust HttpUser      HTTP traffic -> JMeter test plan
               locustfiles/<name>.py                TestScripts/cli/<name>.jmx
               (utils/locust_recorder_writer)
  Functional   UI steps -> Selenium pytest          UI steps -> Java Selenium journey, run by a
               functional/test_<name>.py            JMeter plan (one timed sample per step)
                                                    src/test/java/.../functional/<Name>.java
                                                    TestScripts/functional/<name>.jmx

Every script carries `Test Case:` and `Script Type:` markers, which is what the
Performance Test grid reads back for its Test Case and Type columns.
"""

import json
import os
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit

from utils.locust_recorder_writer import (
    MAX_BODY_CHARS,
    MAX_REQUESTS,
    PASSWORD_ENV_VAR,
    _call_target,
    _describe_step,
    _group_requests,
    _redact,
    _redact_tree,
    host_origin,
    recorded_script_name,
    recorded_script_title,
    sanitize_script_title,
    write_recorded_script,
)
from utils.api_script_generator import _header_manager, _prop, _x

LOCUST_TOOL = "Locust"
JMETER_TOOL = "Jmeter"
SCRIPT_TYPE_CLI = "CLI"
SCRIPT_TYPE_FUNCTIONAL = "Functional"

SELENIUM_VERSION = "4.49.0"
JAVA_PACKAGE = "com.perf.framework.functional"
JAVA_SOURCE_DIR = os.path.join("src", "test", "java", *JAVA_PACKAGE.split("."))
SUPPORT_CLASS = "SeleniumSupport"
# JMeter Java Request sampler that runs one journey step (no Groovy: the Groovy
# bundled with JMeter 5.6.3 cannot read Java 22+ class files).
SAMPLER_CLASS = "JourneySampler"
SUPPORT_FILES = (SUPPORT_CLASS, SAMPLER_CLASS)
FUNCTIONAL_DIRNAME = "functional"
LOCUSTFILES_DIRNAME = "locustfiles"
JMETER_SCRIPTS_DIRNAME = "TestScripts"
# Marker in a JMeter functional plan pointing at its Java journey, so deleting
# the plan can take the class with it.
JAVA_CLASS_MARKER = "Java journey"
# A recorded password in a JMeter plan is read from the same environment
# variable the Locust and Java scripts use; Maven passes its environment on.
# BeanShell rather than __groovy: JMeter 5.6.3's Groovy fails on Java 22+.
JMETER_PASSWORD_EXPRESSION = ('${__BeanShell(v = System.getenv("' + PASSWORD_ENV_VAR
                              + '"); v == null ? "" : v)}')

# Seconds after a click / Enter / submit within which a page navigation is
# treated as its result (replayed as a wait) rather than a typed-in URL.
_NAVIGATION_FOLLOWS_ACTION_SECONDS = 8

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_TEMPLATES_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "templates")


# ---------------------------------------------------------------------------
# Targets and naming
# ---------------------------------------------------------------------------

def normalize_script_type(value):
    return SCRIPT_TYPE_FUNCTIONAL if str(value or "").strip().lower() == "functional" else SCRIPT_TYPE_CLI


def naming_rules(tool, script_type):
    """Where a recording lands and how its file is named: {folder, prefix, extension}."""
    if tool == JMETER_TOOL:
        folder = "TestScripts/functional/" if script_type == SCRIPT_TYPE_FUNCTIONAL else "TestScripts/cli/"
        return {"folder": folder, "prefix": "", "extension": ".jmx"}
    if script_type == SCRIPT_TYPE_FUNCTIONAL:
        # pytest only discovers test_*.py files by default.
        return {"folder": f"{FUNCTIONAL_DIRNAME}/", "prefix": "test_", "extension": ".py"}
    return {"folder": f"{LOCUSTFILES_DIRNAME}/", "prefix": "", "extension": ".py"}


def recorded_file_name(raw, tool, script_type):
    """
    Turn a tester-supplied name into the file name the recording is saved under
    (`Checkout journey` -> `test_Checkout_journey.py`, `Checkout_journey.jmx`, ...).
    Only the base name survives, so a crafted value cannot escape the folder.
    Returns '' when nothing usable is left.
    """
    rules = naming_rules(tool, script_type)
    base = os.path.basename((raw or "").strip().replace("\\", "/"))
    base = re.sub(r"\.(py|jmx|java)$", "", base, flags=re.IGNORECASE)
    stem = _NON_ALNUM.sub("_", base).strip("_")
    if rules["prefix"] and stem.lower().startswith(rules["prefix"]):
        stem = stem[len(rules["prefix"]):].strip("_")
    if not stem:
        return ""
    if stem[0].isdigit() and not rules["prefix"]:
        stem = f"rec_{stem}"
    return f"{rules['prefix']}{stem}{rules['extension']}"


def target_directory(perf_dir, tool, script_type):
    return os.path.join(perf_dir, *naming_rules(tool, script_type)["folder"].strip("/").split("/"))


def _java_class_name(file_name):
    stem = os.path.splitext(file_name)[0]
    parts = [p for p in _NON_ALNUM.split(stem) if p]
    name = "".join(part[:1].upper() + part[1:] for part in parts) or "Recorded"
    if name[0].isdigit():
        name = f"Rec{name}"
    if not name.endswith("Journey"):
        name += "Journey"
    return name


def _name_taken(perf_dir, tool, script_type, file_name):
    if os.path.exists(os.path.join(target_directory(perf_dir, tool, script_type), file_name)):
        return True
    if tool == JMETER_TOOL:
        # JMeter plans are addressed by base name across every TestScripts folder.
        for _current, _dirs, files in os.walk(os.path.join(perf_dir, JMETER_SCRIPTS_DIRNAME)):
            if file_name in files:
                return True
        if script_type == SCRIPT_TYPE_FUNCTIONAL and os.path.exists(
                os.path.join(perf_dir, JAVA_SOURCE_DIR, _java_class_name(file_name) + ".java")):
            return True
    else:
        # Locust scripts are addressed by base name across locustfiles/ and functional/.
        other = SCRIPT_TYPE_CLI if script_type == SCRIPT_TYPE_FUNCTIONAL else SCRIPT_TYPE_FUNCTIONAL
        if os.path.exists(os.path.join(target_directory(perf_dir, tool, other), file_name)):
            return True
    return False


def name_in_use(perf_dir, tool, script_type, file_name):
    """True when `file_name` would collide with an existing script of this project."""
    return bool(file_name) and _name_taken(perf_dir, tool, script_type, file_name)


def _unique_name(perf_dir, tool, script_type, file_name):
    stem, extension = os.path.splitext(file_name)
    candidate, suffix = file_name, 2
    while _name_taken(perf_dir, tool, script_type, candidate):
        candidate = f"{stem}_{suffix}{extension}"
        suffix += 1
    return candidate


def _default_file_name(journey, tool, script_type):
    return recorded_file_name(
        recorded_script_name(journey.get("project_name"), journey.get("finished_at")), tool, script_type)


# ---------------------------------------------------------------------------
# UI steps -> replayable actions
# ---------------------------------------------------------------------------

_BY = {"id", "name", "css", "xpath"}


def _locator(step):
    locator = step.get("locator") if isinstance(step.get("locator"), dict) else {}
    by, value = str(locator.get("by") or ""), str(locator.get("value") or "")
    return (by, value) if by in _BY and value else None


def _same_locator(a, b):
    return _locator(a) is not None and _locator(a) == _locator(b)


def _url_target(url, origin):
    """(path relative to the base URL, or the absolute URL when cross-origin)."""
    parts = urlsplit(url)
    if origin and f"{parts.scheme}://{parts.netloc}".lower() == origin.lower():
        return (parts.path or "/") + (f"?{parts.query}" if parts.query else ""), True
    return url, False


def ui_actions(steps, origin):
    """
    Turn recorded UI steps into replayable actions.

    Clean-ups applied to the raw event stream:
      * the first navigation opens the page; a later one that follows a click /
        Enter / submit is that action's result and becomes a (soft) URL wait;
      * `Enter` recorded before the field's value (keydown fires before change)
        is moved after it, so the text is typed before it is submitted;
      * a submit straight after a click or Enter is the form's own implicit
        submission and is dropped - replaying it would submit twice.
    """
    steps = [dict(s) for s in steps or []]
    # Move a field's value before the Enter pressed in it.
    for index in range(len(steps) - 1):
        current, following = steps[index], steps[index + 1]
        if current.get("type") == "press" and following.get("type") == "input" and _same_locator(current, following):
            steps[index], steps[index + 1] = following, current

    actions = []
    last_action_at = None
    previous_type = ""
    opened = False
    for step in steps:
        kind = (step.get("type") or "").lower()
        label = _describe_step(step)
        at = step.get("at") or 0

        if kind == "navigate":
            url = step.get("url") or step.get("target") or ""
            if not url:
                continue
            target, same_origin = _url_target(url, origin)
            follows_action = (opened and last_action_at is not None
                              and at - last_action_at <= _NAVIGATION_FOLLOWS_ACTION_SECONDS)
            if follows_action:
                actions.append({"kind": "wait_url", "fragment": urlsplit(url).path or "/",
                                "label": f"wait for {urlsplit(url).path or url}"})
            else:
                actions.append({"kind": "open", "target": target, "relative": same_origin, "label": label})
                opened = True
            previous_type = kind
            continue

        locator = _locator(step)
        if kind == "submit" and previous_type in ("click", "press"):
            previous_type = kind
            continue
        if locator is None:
            actions.append({"kind": "note", "label": f"{label} (no locator was recorded - replay by hand)"})
            previous_type = kind
            continue

        value = step.get("value") or ""
        if kind == "click" and value in ("checked", "unchecked"):
            actions.append({"kind": "check", "locator": locator, "checked": value == "checked", "label": label})
        elif kind == "click":
            actions.append({"kind": "click", "locator": locator, "label": label})
        elif kind == "input":
            actions.append({"kind": "type", "locator": locator, "value": value,
                            "secret": bool(step.get("secret")), "label": label})
        elif kind == "select":
            actions.append({"kind": "select", "locator": locator, "value": value, "label": label})
        elif kind == "press":
            actions.append({"kind": "enter", "locator": locator, "label": label})
        elif kind == "submit":
            actions.append({"kind": "submit", "locator": locator, "label": label})
        else:
            actions.append({"kind": "note", "label": label})
        last_action_at = at
        previous_type = kind

    if not any(a["kind"] == "open" for a in actions):
        actions.insert(0, {"kind": "open", "target": "/", "relative": True, "label": "open the application"})
    return actions


def _header(journey, file_name, title, script_type, counts, notes, run_lines):
    finished_at = journey.get("finished_at") or datetime.now()
    lines = [
        file_name,
        "-" * len(file_name),
        f"Test Case: {title}",
        f"Script Type: {script_type}",
        "",
        f"Recorded from : {journey.get('application_url') or ''}",
        f"Recorded at   : {finished_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Journey       : {counts}",
        "",
        "Generated by the BIG QA performance recorder. Review before load testing:",
    ]
    for note in notes:
        lines.append(f"  * {note}")
    lines += [""] + run_lines
    return lines


# ---------------------------------------------------------------------------
# Locust project, Functional: Selenium pytest
# ---------------------------------------------------------------------------

_PY_BY = {"id": "By.ID", "name": "By.NAME", "css": "By.CSS_SELECTOR", "xpath": "By.XPATH"}
_REQUIREMENTS = ("selenium>=4.20", "pytest>=8.0")


def _py_locator(locator):
    return f"{_PY_BY[locator[0]]}, {locator[1]!r}"


def build_selenium_pytest(journey, file_name, title, actions):
    origin = host_origin(journey.get("application_url"))
    uses_password = any(a.get("secret") for a in actions)
    test_name = "test_" + (_NON_ALNUM.sub("_", os.path.splitext(file_name)[0]).strip("_").lower()
                           .removeprefix("test_") or "recorded_journey")

    notes = ["Each step waits for its element, so the printed step timings are what a user waits for.",
             "Locators prefer test ids, ids and names; review CSS/XPath fallbacks after page changes."]
    if uses_password:
        notes.append(f"Passwords are read from the {PASSWORD_ENV_VAR} environment variable.")
    if any(a["kind"] == "note" for a in actions):
        notes.append("Some actions had no locator and are left as comments to complete by hand.")

    header = _header(
        journey, file_name, title, SCRIPT_TYPE_FUNCTIONAL, f"{len(actions)} replayed step(s)", notes,
        ["Run (from the perf project root, after installing requirements.txt):",
         f"    pytest {FUNCTIONAL_DIRNAME}/{file_name} -s            # headless",
         f"    pytest {FUNCTIONAL_DIRNAME}/{file_name} -s --headed   # watch the browser"])
    lines = ['"""'] + header + ['"""', "", "import os", "", "from selenium.webdriver.common.by import By", ""]
    lines.append(f'BASE_URL = os.getenv("PERF_BASE_URL", {origin!r})')
    if uses_password:
        lines.append(f'PASSWORD = os.getenv("{PASSWORD_ENV_VAR}", "")')
    lines += ["", "", f"def {test_name}(ui):"]

    for number, action in enumerate(actions, start=1):
        label = f"{number}. {action['label']}"
        kind = action["kind"]
        if kind == "note":
            lines += [f"    # {label}", ""]
            continue
        if kind == "open":
            call = (f"ui.open(BASE_URL + {action['target']!r})" if action["relative"]
                    else f"ui.open({action['target']!r})")
        elif kind == "wait_url":
            call = f"ui.wait_for_url({action['fragment']!r})"
        elif kind == "click":
            call = f"ui.click({_py_locator(action['locator'])})"
        elif kind == "check":
            call = f"ui.set_checked({_py_locator(action['locator'])}, {action['checked']})"
        elif kind == "type":
            text = "PASSWORD" if action["secret"] else repr(action["value"])
            call = f"ui.type({_py_locator(action['locator'])}, {text})"
        elif kind == "select":
            call = f"ui.select({_py_locator(action['locator'])}, {action['value']!r})"
        elif kind == "enter":
            call = f"ui.press_enter({_py_locator(action['locator'])})"
        else:
            call = f"ui.submit({_py_locator(action['locator'])})"
        lines += [f"    with ui.step({label!r}):", f"        {call}", ""]
    return "\n".join(lines).rstrip() + "\n"


def _ensure_python_functional_support(perf_dir):
    """Copy functional/conftest.py and add Selenium + pytest to requirements.txt when missing."""
    notes = []
    functional_dir = os.path.join(perf_dir, FUNCTIONAL_DIRNAME)
    os.makedirs(functional_dir, exist_ok=True)
    conftest = os.path.join(functional_dir, "conftest.py")
    if not os.path.isfile(conftest):
        source = os.path.join(_TEMPLATES_ROOT, "Locust_framework", FUNCTIONAL_DIRNAME, "conftest.py")
        with open(source, "r", encoding="utf-8") as handle:
            content = handle.read()
        with open(conftest, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

    requirements = os.path.join(perf_dir, "requirements.txt")
    existing = ""
    if os.path.isfile(requirements):
        with open(requirements, "r", encoding="utf-8") as handle:
            existing = handle.read()
    missing = [req for req in _REQUIREMENTS
               if not re.search(rf"^\s*{re.split(r'[<>=]', req)[0]}\b", existing, re.IGNORECASE | re.MULTILINE)]
    if missing:
        with open(requirements, "a", encoding="utf-8", newline="\n") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write("# Functional (Selenium) journeys recorded from the Performance Test page\n")
            handle.write("\n".join(missing) + "\n")
        notes.append(f"Added {', '.join(r.split('>')[0] for r in missing)} to requirements.txt - install them "
                     f"into .venv (pip install -r requirements.txt) before running the test.")
    return notes


# ---------------------------------------------------------------------------
# JMeter project, Functional: Java Selenium journey + JMeter plan
# ---------------------------------------------------------------------------

_JAVA_BY = {"id": "By.id", "name": "By.name", "css": "By.cssSelector", "xpath": "By.xpath"}


def _java_string(value):
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return '"' + "".join(ch if 32 <= ord(ch) < 127 else f"\\u{ord(ch):04x}" for ch in text) + '"'


def _java_comment(value):
    return str(value).replace("*/", "* /").replace("\\u", "\\\\u")


def _java_locator(locator):
    return f"{_JAVA_BY[locator[0]]}({_java_string(locator[1])})"


def build_java_journey(journey, class_name, file_name, title, actions):
    origin = host_origin(journey.get("application_url"))
    uses_password = any(a.get("secret") for a in actions)
    method_lines, names = [], []
    for number, action in enumerate(actions, start=1):
        kind = action["kind"]
        names.append(f"{number:02d} - {action['label']}")
        if kind == "note":
            body = f"        // {_java_comment(action['label'])} - complete by hand"
        elif kind == "open":
            body = f"        open({_java_string(action['target'])});"
        elif kind == "wait_url":
            body = f"        waitForUrl({_java_string(action['fragment'])});"
        elif kind == "click":
            body = f"        click({_java_locator(action['locator'])});"
        elif kind == "check":
            body = f"        setChecked({_java_locator(action['locator'])}, {str(action['checked']).lower()});"
        elif kind == "type":
            text = f'env("{PASSWORD_ENV_VAR}")' if action["secret"] else _java_string(action["value"])
            body = f"        type({_java_locator(action['locator'])}, {text});"
        elif kind == "select":
            body = f"        select({_java_locator(action['locator'])}, {_java_string(action['value'])});"
        elif kind == "enter":
            body = f"        pressEnter({_java_locator(action['locator'])});"
        else:
            body = f"        submit({_java_locator(action['locator'])});"
        method_lines += [
            f"    /** {number}. {_java_comment(action['label'])} */",
            f"    public void step{number:02d}() {{",
            body,
            "    }",
            "",
        ]

    finished_at = journey.get("finished_at") or datetime.now()
    doc = [
        "/**",
        f" * {class_name} - recorded functional journey.",
        " *",
        f" * Test Case: {_java_comment(title)}",
        f" * Script Type: {SCRIPT_TYPE_FUNCTIONAL}",
        f" * Recorded from : {_java_comment(journey.get('application_url') or '')}",
        f" * Recorded at   : {finished_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f" * JMeter plan   : TestScripts/functional/{file_name}",
        " *",
        " * Each stepNN() is one JMeter sample in the plan, so the reports time every user action.",
    ]
    if uses_password:
        doc.append(f" * Passwords are read from the {PASSWORD_ENV_VAR} environment variable.")
    doc += [
        " *",
        " * Run it standalone from the project root:",
        f" *   mvn -q test-compile exec:java -Dexec.mainClass={JAVA_PACKAGE}.{class_name} "
        "-Dexec.classpathScope=test -Dselenium.headless=false",
        " */",
    ]
    steps_array = ",\n".join(f"        {_java_string(name)}" for name in names)
    run_all = "\n".join(f"        step{n:02d}();" for n in range(1, len(actions) + 1))
    return "\n".join([
        f"package {JAVA_PACKAGE};",
        "",
        "import org.openqa.selenium.By;",
        "import org.openqa.selenium.WebDriver;",
        "",
    ] + doc + [
        f"public class {class_name} extends {SUPPORT_CLASS} {{",
        "",
        f"    public static final String DEFAULT_BASE_URL = {_java_string(origin)};",
        "",
        "    /** Sample names, in order - the JMeter plan uses the same labels. */",
        "    public static final String[] STEPS = {",
        steps_array,
        "    };",
        "",
        f"    public {class_name}(WebDriver driver, String baseUrl) {{",
        "        super(driver, baseUrl);",
        "    }",
        "",
    ] + method_lines + [
        "    /** The whole journey, for standalone runs. */",
        "    public void runAll() {",
        run_all,
        "    }",
        "",
        "    public static void main(String[] args) {",
        '        boolean headless = Boolean.parseBoolean(System.getProperty("selenium.headless", "true"));',
        '        String baseUrl = System.getProperty("selenium.baseUrl", DEFAULT_BASE_URL);',
        f"        {class_name} journey = new {class_name}(createDriver(headless), baseUrl);",
        "        try {",
        "            journey.runAll();",
        "        } finally {",
        "            journey.quit();",
        "        }",
        "    }",
        "}",
        "",
    ])


def _java_request(name, parameters, indent):
    """A JMeter Java Request sampler running JourneySampler with `parameters`."""
    pad = " " * indent
    arguments = "".join(
        f'\n{pad}      <elementProp name="{_x(key)}" elementType="Argument">'
        f'{_prop("Argument.name", key)}{_prop("Argument.value", value)}{_prop("Argument.metadata", "=")}'
        f'</elementProp>'
        for key, value in parameters.items()
    )
    return (
        f'{pad}<JavaSampler guiclass="JavaTestSamplerGui" testclass="JavaSampler" testname="{_x(name[:150])}" enabled="true">\n'
        f'{pad}  <elementProp name="arguments" elementType="Arguments" guiclass="ArgumentsPanel" testclass="Arguments" enabled="true">\n'
        f'{pad}    <collectionProp name="Arguments.arguments">{arguments}\n{pad}    </collectionProp>\n'
        f'{pad}  </elementProp>\n'
        f'{pad}  {_prop("classname", f"{JAVA_PACKAGE}.{SAMPLER_CLASS}")}\n'
        f'{pad}</JavaSampler>\n{pad}<hashTree/>\n'
    )


def _thread_group(name, threads, loops, children, duration=False):
    scheduler = (
        '        <boolProp name="ThreadGroup.scheduler">true</boolProp>\n'
        f'        {_prop("ThreadGroup.duration", "${__P(duration,30)}")}\n'
        if duration else
        '        <boolProp name="ThreadGroup.scheduler">false</boolProp>\n'
        f'        {_prop("ThreadGroup.duration", "")}\n'
    )
    return f"""      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="{_x(name)}" enabled="true">
        {_prop("ThreadGroup.on_sample_error", "continue")}
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          {_prop("LoopController.loops", loops)}
        </elementProp>
        {_prop("ThreadGroup.num_threads", threads)}
        {_prop("ThreadGroup.ramp_time", "${__P(rampup,5)}")}
{scheduler}        {_prop("ThreadGroup.delay", "")}
        <boolProp name="ThreadGroup.same_user_on_next_iteration">true</boolProp>
      </ThreadGroup>
      <hashTree>
{children}      </hashTree>
"""


def _test_plan(title, comments, body):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="{_x(title)}" enabled="true">
      {_prop("TestPlan.comments", comments)}
      <boolProp name="TestPlan.functional_mode">false</boolProp>
      <boolProp name="TestPlan.serialize_threadgroups">false</boolProp>
      <elementProp name="TestPlan.user_defined_variables" elementType="Arguments" guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">
        <collectionProp name="Arguments.arguments"/>
      </elementProp>
    </TestPlan>
    <hashTree>
{body}    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""


def build_java_journey_plan(journey, class_name, file_name, title, actions):
    origin = host_origin(journey.get("application_url"))
    qualified = f"{JAVA_PACKAGE}.{class_name}"
    comments = "\n".join(_header(
        journey, file_name, title, SCRIPT_TYPE_FUNCTIONAL, f"{len(actions)} replayed step(s)",
        ["Every step is one sample, timed around the Selenium action and its waits.",
         f"Base URL: -Jselenium.baseUrl (default {origin}); browser: pw.headless; users: -Jselenium.threads."],
        [f"{JAVA_CLASS_MARKER}: {JAVA_SOURCE_DIR.replace(os.sep, '/')}/{class_name}.java",
         f"Run: mvn clean verify -Pfunctional   (or -Dperf.tests=functional/{file_name})"]))

    # Each virtual user launches its own Chrome, runs one sample per recorded
    # action (JourneySampler calls journey.stepNN()), then closes the browser.
    samplers = [_java_request("00 - Launch browser", {"journey": qualified, "action": "launch", "step": ""}, 8)]
    for number, action in enumerate(actions, start=1):
        if action["kind"] == "note":
            continue
        samplers.append(_java_request(f"{number:02d} - {action['label']}",
                                      {"journey": qualified, "action": "step", "step": f"step{number:02d}"}, 8))
    samplers.append(_java_request("99 - Close browser", {"journey": qualified, "action": "close", "step": ""}, 8))

    body = _thread_group("Browser Users", "${__P(selenium.threads,1)}", "${__P(selenium.loops,1)}",
                         "".join(samplers))
    return _test_plan(title, comments, body)


def _insert_before(text, anchor, snippet, after=None):
    start = text.find(after) if after else 0
    if start == -1:
        return text, False
    index = text.find(anchor, start)
    if index == -1:
        return text, False
    return text[:index] + snippet + text[index:], True


def ensure_selenium_support(perf_dir):
    """
    Make a JMeter project able to compile and run Java Selenium journeys:
    the Selenium dependency, Selenium on JMeter's classpath (testPlanLibraries),
    the compiled test classes on JMeter's classpath (user.classpath), and the
    shared SeleniumSupport base class. Idempotent - only what is missing is added.

    Returns a list of human-readable notes about anything that could not be done.
    """
    notes = []
    for class_name in SUPPORT_FILES:
        target = os.path.join(perf_dir, JAVA_SOURCE_DIR, f"{class_name}.java")
        source = os.path.join(_TEMPLATES_ROOT, "jmeter-framework", JAVA_SOURCE_DIR, f"{class_name}.java")
        if os.path.isfile(target) or os.path.normcase(os.path.abspath(target)) == os.path.normcase(os.path.abspath(source)):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(source, "r", encoding="utf-8") as handle:
            content = handle.read()
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

    pom_path = os.path.join(perf_dir, "pom.xml")
    if not os.path.isfile(pom_path):
        return ["No pom.xml found - add Selenium to the build by hand to run this journey."]
    with open(pom_path, "r", encoding="utf-8") as handle:
        pom = original = handle.read()
    if "jmeter-maven-plugin" not in pom:
        return ["pom.xml does not use the jmeter-maven-plugin - add Selenium to JMeter's classpath by hand."]

    # The run goals look up the configure execution by the plugin's default id.
    pom = pom.replace("<id>configure</id>", "<id>configuration</id>")
    if "<selenium.version>" not in pom:
        pom, _ = _insert_before(pom, "</properties>", f"  <selenium.version>{SELENIUM_VERSION}</selenium.version>\n  ")
    if "<artifactId>selenium-java</artifactId>" not in pom:
        pom, ok = _insert_before(pom, "</dependencies>", (
            "  <!-- Selenium for recorded functional journeys (compiled with the tests, run by JMeter) -->\n"
            "    <dependency>\n"
            "      <groupId>org.seleniumhq.selenium</groupId>\n"
            "      <artifactId>selenium-java</artifactId>\n"
            "      <version>${selenium.version}</version>\n"
            "      <scope>test</scope>\n"
            "    </dependency>\n  "))
        if not ok:
            notes.append("Could not add the selenium-java dependency to pom.xml.")
    if "<artifactId>ApacheJMeter_java</artifactId>" not in pom:
        pom, ok = _insert_before(pom, "</dependencies>", (
            "  <!-- JMeter's Java Request API, for JourneySampler (JMeter itself provides it at run time) -->\n"
            "    <dependency>\n"
            "      <groupId>org.apache.jmeter</groupId>\n"
            "      <artifactId>ApacheJMeter_java</artifactId>\n"
            "      <version>${jmeter.version}</version>\n"
            "      <scope>provided</scope>\n"
            "    </dependency>\n  "))
        if not ok:
            notes.append("Could not add the ApacheJMeter_java dependency to pom.xml.")
    if "org.seleniumhq.selenium:selenium-java" not in pom:
        if "</testPlanLibraries>" in pom:
            pom, _ = _insert_before(pom, "</testPlanLibraries>",
                                    "  <artifact>org.seleniumhq.selenium:selenium-java:${selenium.version}</artifact>\n          ")
        else:
            pom, ok = _insert_before(pom, "</configuration>", (
                "  <testPlanLibraries>\n"
                "            <artifact>org.seleniumhq.selenium:selenium-java:${selenium.version}</artifact>\n"
                "          </testPlanLibraries>\n        "), after="jmeter-maven-plugin")
            if not ok:
                notes.append("Could not add Selenium to the JMeter test plan libraries in pom.xml.")
    if "<user.classpath>" not in pom:
        snippet = "  <user.classpath>${project.build.testOutputDirectory}</user.classpath>\n          "
        if "</propertiesUser>" in pom:
            pom, _ = _insert_before(pom, "</propertiesUser>", snippet)
        else:
            pom, ok = _insert_before(pom, "</configuration>", (
                "  <propertiesUser>\n"
                "            <user.classpath>${project.build.testOutputDirectory}</user.classpath>\n"
                "          </propertiesUser>\n        "), after="jmeter-maven-plugin")
            if not ok:
                notes.append("Could not put the compiled journeys on JMeter's classpath (user.classpath).")
    if pom != original:
        with open(pom_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(pom)
    return notes


# ---------------------------------------------------------------------------
# JMeter project, CLI: recorded HTTP traffic -> JMeter test plan
# ---------------------------------------------------------------------------

def _recorded_body(request, secrets):
    """Return (kind, content, content_type) for a recorded request body."""
    body = request.get("post_data") or ""
    if not body:
        return "", None, ""
    body = body[:MAX_BODY_CHARS]
    content_type = (request.get("content_type") or "").lower()
    if "x-www-form-urlencoded" in content_type or (not content_type and "=" in body and "\n" not in body
                                                     and body.lstrip()[:1] not in ("{", "[")):
        pairs = parse_qsl(body, keep_blank_values=True)
        if pairs:
            return "form", [(k, _redact(v, secrets)) for k, v in pairs], "application/x-www-form-urlencoded"
    if "json" in content_type or body.lstrip()[:1] in ("{", "["):
        try:
            parsed = _redact_tree(json.loads(body), secrets)
            return "raw", json.dumps(parsed, indent=2), content_type or "application/json"
        except ValueError:
            pass
    return "raw", _redact(body, secrets), content_type or "text/plain"


def _password_placeholder(text):
    from utils.locust_recorder_writer import _PASSWORD_SENTINEL
    return str(text).replace(_PASSWORD_SENTINEL, JMETER_PASSWORD_EXPRESSION)


def _recorded_sampler(request, origin, secrets, indent):
    pad = " " * indent
    target, stats_name = _call_target(request.get("url", ""), origin)
    method = (request.get("method") or "GET").upper()
    domain = protocol = port = ""
    path = target
    if target.lower().startswith(("http://", "https://")):
        parts = urlsplit(target)
        domain, protocol, port = parts.hostname or "", parts.scheme, str(parts.port or "")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

    kind, content, content_type = _recorded_body(request, secrets)
    arguments = []
    if kind == "raw":
        arguments.append(
            f'{pad}      <elementProp name="" elementType="HTTPArgument">\n'
            f'{pad}        <boolProp name="HTTPArgument.always_encode">false</boolProp>\n'
            f'{pad}        {_prop("Argument.value", _password_placeholder(content))}\n'
            f'{pad}        {_prop("Argument.metadata", "=")}\n'
            f'{pad}      </elementProp>')
    elif kind == "form":
        for key, value in content:
            arguments.append(
                f'{pad}      <elementProp name="{_x(key)}" elementType="HTTPArgument">\n'
                f'{pad}        <boolProp name="HTTPArgument.always_encode">true</boolProp>\n'
                f'{pad}        {_prop("Argument.name", key)}\n'
                f'{pad}        {_prop("Argument.value", _password_placeholder(value))}\n'
                f'{pad}        {_prop("Argument.metadata", "=")}\n'
                f'{pad}        <boolProp name="HTTPArgument.use_equals">true</boolProp>\n'
                f'{pad}      </elementProp>')
    args_xml = ("\n" + "\n".join(arguments) + f"\n{pad}    ") if arguments else ""
    xml = (
        f'{pad}<HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="{_x(method + " " + stats_name)}" enabled="true">\n'
        f'{pad}  <boolProp name="HTTPSampler.postBodyRaw">{"true" if kind == "raw" else "false"}</boolProp>\n'
        f'{pad}  <elementProp name="HTTPsampler.Arguments" elementType="Arguments">\n'
        f'{pad}    <collectionProp name="Arguments.arguments">{args_xml}</collectionProp>\n'
        f'{pad}  </elementProp>\n'
        f'{pad}  {_prop("HTTPSampler.domain", domain)}\n'
        f'{pad}  {_prop("HTTPSampler.port", port)}\n'
        f'{pad}  {_prop("HTTPSampler.protocol", protocol)}\n'
        f'{pad}  {_prop("HTTPSampler.path", path)}\n'
        f'{pad}  {_prop("HTTPSampler.method", method)}\n'
        f'{pad}  <boolProp name="HTTPSampler.follow_redirects">true</boolProp>\n'
        f'{pad}  <boolProp name="HTTPSampler.auto_redirects">false</boolProp>\n'
        f'{pad}  <boolProp name="HTTPSampler.use_keepalive">true</boolProp>\n'
        f'{pad}  <boolProp name="HTTPSampler.DO_MULTIPART_POST">false</boolProp>\n'
        f'{pad}</HTTPSamplerProxy>\n'
    )
    if content_type and kind:
        xml += (f"{pad}<hashTree>\n" + _header_manager("Content-Type", {"Content-Type": content_type}, indent + 2)
                + f"{pad}</hashTree>\n")
    else:
        xml += f"{pad}<hashTree/>\n"
    return xml


def _transaction(name, children, indent):
    pad = " " * indent
    return (
        f'{pad}<TransactionController guiclass="TransactionControllerGui" testclass="TransactionController" testname="{_x(name[:150])}" enabled="true">\n'
        f'{pad}  <boolProp name="TransactionController.includeTimers">false</boolProp>\n'
        f'{pad}  <boolProp name="TransactionController.parent">false</boolProp>\n'
        f'{pad}</TransactionController>\n{pad}<hashTree>\n{children}{pad}</hashTree>\n'
    )


def build_recorded_jmeter_plan(journey, file_name, title):
    application_url = journey.get("application_url") or ""
    origin = host_origin(application_url)
    steps = list(journey.get("steps") or [])
    requests = list(journey.get("requests") or [])
    secrets = [s for s in (journey.get("secrets") or []) if s]
    dropped = max(0, len(requests) - MAX_REQUESTS)
    requests = requests[:MAX_REQUESTS]

    buckets = _group_requests(steps, requests)
    transactions = []
    if buckets.get(-1):
        transactions.append(_transaction(
            "00 - Initial page load",
            "".join(_recorded_sampler(r, origin, secrets, 10) for r in buckets[-1]), 8))
    for step in steps:
        entries = buckets.get(step.get("index")) or []
        if not entries:
            continue  # a UI-only action produced no traffic to replay
        transactions.append(_transaction(
            f"{step.get('index', 0) + 1:02d} - {_describe_step(step)}",
            "".join(_recorded_sampler(r, origin, secrets, 10) for r in entries), 8))

    notes = ["Each recorded step is a Transaction Controller, so reports time the step and its requests.",
             "Headers (cookies, Authorization, CSRF) were not recorded; the Cookie Manager keeps each user's session.",
             "Static assets (images, CSS, JS, fonts, media) were skipped."]
    if dropped:
        notes.append(f"{dropped} request(s) beyond the first {MAX_REQUESTS} were not included.")
    if secrets:
        notes.append(f"Passwords are read from the {PASSWORD_ENV_VAR} environment variable.")
    comments = "\n".join(_header(
        journey, file_name, title, SCRIPT_TYPE_CLI,
        f"{len(steps)} user action(s), {len(requests)} request(s)", notes,
        [f"Run: mvn clean verify -Pcli   (or -Dperf.tests=cli/{file_name})"]))

    parts = urlsplit(application_url)
    defaults = f"""      <ConfigTestElement guiclass="HttpDefaultsGui" testclass="ConfigTestElement" testname="HTTP Request Defaults" enabled="true">
        <elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">
          <collectionProp name="Arguments.arguments"/>
        </elementProp>
        {_prop("HTTPSampler.domain", "${__P(api.host," + (parts.hostname or "localhost") + ")}")}
        {_prop("HTTPSampler.port", "${__P(api.port," + str(parts.port) + ")}" if parts.port else "")}
        {_prop("HTTPSampler.protocol", "${__P(api.protocol," + (parts.scheme or "https") + ")}")}
        {_prop("HTTPSampler.contentEncoding", "UTF-8")}
        {_prop("HTTPSampler.path", "")}
        {_prop("HTTPSampler.connect_timeout", "10000")}
        {_prop("HTTPSampler.response_timeout", "30000")}
      </ConfigTestElement>
      <hashTree/>
      <CookieManager guiclass="CookiePanel" testclass="CookieManager" testname="HTTP Cookie Manager" enabled="true">
        <collectionProp name="CookieManager.cookies"/>
        <boolProp name="CookieManager.clearEachIteration">true</boolProp>
        <boolProp name="CookieManager.controlledByThreadGroup">false</boolProp>
      </CookieManager>
      <hashTree/>
"""
    if not transactions:
        transactions.append(_transaction("01 - Open the application", _recorded_sampler(
            {"method": "GET", "url": origin + "/" if origin else "/"}, origin, secrets, 10), 8))
    body = defaults + _thread_group("Recorded Users", "${__P(threads,5)}", "-1", "".join(transactions), duration=True)
    return _test_plan(title, comments, body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def write_recording(perf_dir, tool, script_type, journey):
    """
    Write the recording as the script `tool` + `script_type` call for.

    Returns {"path", "file_name", "relative_path", "notes": [...], "extra_files": [...]}.
    A name already taken gets a numeric suffix rather than overwriting.
    """
    tool = JMETER_TOOL if tool == JMETER_TOOL else LOCUST_TOOL
    script_type = normalize_script_type(script_type)

    if tool == LOCUST_TOOL and script_type == SCRIPT_TYPE_CLI:
        path, file_name = write_recorded_script(
            os.path.join(perf_dir, LOCUSTFILES_DIRNAME), dict(journey, script_type=SCRIPT_TYPE_CLI))
        return {"path": path, "file_name": file_name,
                "relative_path": f"{LOCUSTFILES_DIRNAME}/{file_name}", "notes": [], "extra_files": []}

    file_name = (recorded_file_name(journey.get("file_name"), tool, script_type)
                 or _default_file_name(journey, tool, script_type))
    file_name = _unique_name(perf_dir, tool, script_type, file_name)
    title = (sanitize_script_title(journey.get("title"))
             or recorded_script_title(journey.get("project_name"), journey.get("finished_at")))
    origin = host_origin(journey.get("application_url"))
    directory = target_directory(perf_dir, tool, script_type)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, file_name)
    notes, extra_files = [], []

    if tool == LOCUST_TOOL:
        notes += _ensure_python_functional_support(perf_dir)
        source = build_selenium_pytest(journey, file_name, title, ui_actions(journey.get("steps"), origin))
        compile(source, file_name, "exec")  # never write a test that cannot even be imported
    elif script_type == SCRIPT_TYPE_CLI:
        source = build_recorded_jmeter_plan(journey, file_name, title)
    else:
        actions = ui_actions(journey.get("steps"), origin)
        class_name = _java_class_name(file_name)
        notes += ensure_selenium_support(perf_dir)
        java_path = os.path.join(perf_dir, JAVA_SOURCE_DIR, f"{class_name}.java")
        os.makedirs(os.path.dirname(java_path), exist_ok=True)
        with open(java_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(build_java_journey(journey, class_name, file_name, title, actions))
        extra_files.append(os.path.relpath(java_path, perf_dir).replace("\\", "/"))
        source = build_java_journey_plan(journey, class_name, file_name, title, actions)

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(source)
    return {"path": path, "file_name": file_name,
            "relative_path": os.path.relpath(path, perf_dir).replace("\\", "/"),
            "notes": notes, "extra_files": extra_files}


def companion_files(perf_dir, script_path):
    """Files generated alongside a script that should go when it is deleted (a plan's Java journey)."""
    if not script_path.lower().endswith(".jmx"):
        return []
    try:
        with open(script_path, "r", encoding="utf-8", errors="ignore") as handle:
            head = handle.read(8000)
    except OSError:
        return []
    match = re.search(rf"{JAVA_CLASS_MARKER}: ([^\n&<]+\.java)", head)
    if not match:
        return []
    root = os.path.realpath(perf_dir)
    candidate = os.path.realpath(os.path.join(perf_dir, match.group(1).strip()))
    if not candidate.startswith(os.path.join(root, "src") + os.sep) or not os.path.isfile(candidate):
        return []
    return [candidate]
