"""Turn a rough page-object dump into feature + page object + step-definition files.

Supports two flavors:
  - playwright_ts     Playwright TypeScript + Cucumber.js
  - selenium_python   Selenium 4 + Python + Behave
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


PLAYWRIGHT_TS = "playwright_ts"
SELENIUM_PYTHON = "selenium_python"

_BY_ALIASES = {
    "id": "ID",
    "name": "NAME",
    "xpath": "XPATH",
    "css": "CSS_SELECTOR",
    "css_selector": "CSS_SELECTOR",
    "cssselector": "CSS_SELECTOR",
    "class": "CLASS_NAME",
    "class_name": "CLASS_NAME",
    "classname": "CLASS_NAME",
    "tag": "TAG_NAME",
    "tag_name": "TAG_NAME",
    "link": "LINK_TEXT",
    "link_text": "LINK_TEXT",
    "linktext": "LINK_TEXT",
    "partial_link": "PARTIAL_LINK_TEXT",
    "partial_link_text": "PARTIAL_LINK_TEXT",
}


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", text or "")


def pascal(text: str) -> str:
    parts = words(text)
    return "".join(w[:1].upper() + w[1:] for w in parts) or "Page"


def camel(text: str) -> str:
    p = pascal(text)
    return p[:1].lower() + p[1:]


def snake(text: str) -> str:
    parts = [w.lower() for w in words(text)]
    return "_".join(parts) or "page"


def humanize(text: str) -> str:
    return " ".join(w.lower() for w in words(text)) or "element"


def ts_str(value: str) -> str:
    return "'" + (value or "").replace("\\", "\\\\").replace("'", "\\'") + "'"


def py_str(value: str) -> str:
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def strip_page_suffix(name: str) -> str:
    cleaned = re.sub(r"(page|pom|object)$", "", name or "", flags=re.I).strip()
    return cleaned or name or "Page"


def guess_by(selector: str) -> str:
    s = (selector or "").strip()
    if s.startswith(("//", ".//", "(")):
        return "XPATH"
    if s.startswith("#") and re.match(r"^#[A-Za-z][\w-]*$", s):
        return "ID"
    if s.startswith("id="):
        return "ID"
    if s.startswith("name="):
        return "NAME"
    if s.startswith(("xpath=", "text=")):
        return "XPATH"
    return "CSS_SELECTOR"


def selector_value(selector: str, by_type: str) -> str:
    s = (selector or "").strip()
    for prefix in ("id=", "name=", "css=", "xpath=", "text="):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    if by_type == "ID" and s.startswith("#"):
        s = s[1:]
    return s


def infer_action(name: str, selector: str = "") -> str:
    blob = f"{name} {selector}".lower()
    if re.search(r"\b(goto|open|launch|navigate|visit)\b", blob):
        return "navigate"
    if re.search(r"\b(verify|assert|should|visible|displayed|expect)\b", blob):
        return "verify"
    if re.search(r"\b(select|choose|option|dropdown|combo)\b", blob):
        return "select"
    if re.search(r"\b(fill|enter|type|input|set|send)\b", blob):
        return "fill"
    if re.search(r"\b(search)\b", blob):
        return "fill"
    if re.search(r"\b(click|press|submit|save|button|link)\b", blob):
        return "click"
    if re.search(r"(textbox|input|field|name|email|phone|password|username)", blob):
        return "fill"
    return "click"


def field_label(name: str) -> str:
    cleaned = re.sub(
        r"(selector|locator|loc|element|btn|button|link|input|field|txt)$",
        "",
        name,
        flags=re.I,
    )
    return humanize(cleaned or name)


@dataclass
class Locator:
    name: str
    selector: str
    expr: str = ""
    by_type: str = "CSS_SELECTOR"
    action: str = "click"


@dataclass
class ParsedPO:
    class_name: str
    flavor: str
    locators: list[Locator] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class GeneratedFile:
    filename: str
    relative_path: str
    kind: str
    content: str


def _unquote(raw: str) -> str:
    raw = (raw or "").strip()
    if len(raw) >= 2 and raw[0] in "'\"`" and raw[-1] == raw[0]:
        inner = raw[1:-1]
        return re.sub(r"\\([\\'\"`])", r"\1", inner)
    return raw


def _balanced_call(text: str, start: int) -> str:
    """Return the call starting at `start` (on an identifier) through its closing paren."""
    i = text.find("(", start)
    if i < 0:
        return ""
    depth = 0
    quote = None
    j = i
    while j < len(text):
        ch = text[j]
        if quote:
            if ch == "\\" :
                j += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
        j += 1
    return text[start:]


def _unique_name(base: str, used: set[str]) -> str:
    name, n = base, 2
    while name in used:
        name = f"{base}{n}"
        n += 1
    used.add(name)
    return name


def _add_locator(locators: list[Locator], used: set[str], name: str, selector: str, expr: str = "") -> None:
    selector = (selector or "").strip()
    expr = (expr or "").strip().rstrip(";")
    if not selector and not expr:
        return
    key = (selector, expr)
    if any((loc.selector, loc.expr) == key for loc in locators):
        return
    field = _unique_name(camel(name) if name[0:1].islower() else camel(name), used)
    if field in {"constructor", "page", "driver", "self"}:
        field = _unique_name(f"{field}Loc", used)
    locators.append(
        Locator(
            name=field,
            selector=selector,
            expr=expr,
            by_type=guess_by(selector or expr),
            action=infer_action(name, selector or expr),
        )
    )


def parse_playwright_ts(source: str) -> ParsedPO:
    notes: list[str] = []
    class_m = re.search(r"(?:export\s+)?class\s+(\w+)", source)
    class_name = class_m.group(1) if class_m else "GeneratedPage"
    locators: list[Locator] = []
    used: set[str] = set()

    field_re = re.compile(
        r"(?:(?:private|public|protected|readonly)\s+)*(?:readonly\s+)?"
        r"(?:this\.)?(\w+)\s*(?::\s*[^=;{]+)?\s*=\s*([^;=\n]+)",
    )
    for m in field_re.finditer(source):
        name, raw = m.group(1), m.group(2).strip().rstrip(",")
        if name in {"constructor", "page", "browser", "context"}:
            continue
        if re.search(r"\b(getByRole|getByText|getByLabel|getByPlaceholder|getByTestId|locator)\s*\(", raw):
            expr = re.sub(r"^(?:this\.)?page\.", "", raw)
            expr = re.sub(r"^await\s+", "", expr).strip()
            _add_locator(locators, used, name, "", expr)
        elif raw[:1] in "'\"`":
            _add_locator(locators, used, name, _unquote(raw), "")
        elif "By." in raw:
            by_m = re.search(r"By\.(\w+)\s*,\s*(['\"`])(.*?)\2", raw, re.S)
            if by_m:
                selector = by_m.group(3)
                loc = Locator(
                    name=_unique_name(camel(name), used),
                    selector=selector,
                    by_type=_BY_ALIASES.get(by_m.group(1).lower(), "CSS_SELECTOR"),
                    action=infer_action(name, selector),
                )
                locators.append(loc)

    call_re = re.compile(
        r"(?:(?:this\.)?page\.)?(getByRole|getByText|getByLabel|getByPlaceholder|getByTestId|locator)\s*\("
    )
    for m in call_re.finditer(source):
        call = _balanced_call(source, m.start())
        if not call:
            continue
        expr = re.sub(r"^(?:this\.)?page\.", "", call).strip()
        label = "element"
        name_m = re.search(r"name\s*:\s*(['\"`])(.*?)\1", expr, re.S)
        if name_m:
            label = name_m.group(2)
        else:
            lit = re.search(r"(['\"`])(.*?)\1", expr, re.S)
            if lit:
                label = lit.group(2)[:40]
        _add_locator(locators, used, label, "", expr)

    css_calls = re.compile(
        r"""(?:click|fill|type|press)\(\s*(['"`])([^'"`]+)\1"""
    )
    for m in css_calls.finditer(source):
        _add_locator(locators, used, m.group(2), m.group(2), "")

    if not locators:
        notes.append("No locators were recognized in the Playwright TypeScript page object.")
    return ParsedPO(class_name=class_name, flavor=PLAYWRIGHT_TS, locators=locators, notes=notes)


def parse_selenium_python(source: str) -> ParsedPO:
    notes: list[str] = []
    class_m = re.search(r"class\s+(\w+)", source)
    class_name = class_m.group(1) if class_m else "GeneratedPage"
    locators: list[Locator] = []
    used: set[str] = set()

    tuple_re = re.compile(
        r"(\w+)\s*=\s*\(\s*By\.(\w+)\s*,\s*(['\"])(.*?)\3\s*\)",
        re.S,
    )
    for m in tuple_re.finditer(source):
        name, by_name, selector = m.group(1), m.group(2), m.group(4)
        locators.append(
            Locator(
                name=_unique_name(snake(name).upper(), used),
                selector=selector,
                by_type=_BY_ALIASES.get(by_name.lower(), "CSS_SELECTOR"),
                action=infer_action(name, selector),
            )
        )

    str_re = re.compile(r"""(\w+)\s*=\s*(['"])([^'"]+)\2""")
    for m in str_re.finditer(source):
        name, selector = m.group(1), m.group(3)
        if name.isupper() and name.endswith(("URL", "TIMEOUT")):
            continue
        if any(loc.selector == selector for loc in locators):
            continue
        if name in {"self", "driver"}:
            continue
        by_type = guess_by(selector)
        locators.append(
            Locator(
                name=_unique_name(snake(name).upper(), used),
                selector=selector_value(selector, by_type),
                by_type=by_type,
                action=infer_action(name, selector),
            )
        )

    find_re = re.compile(
        r"find_element(?:_by_(\w+))?\(\s*(?:By\.(\w+)\s*,\s*)?(['\"])(.*?)\3",
        re.S | re.I,
    )
    for m in find_re.finditer(source):
        legacy, by_name, selector = m.group(1), m.group(2), m.group(4)
        by_type = _BY_ALIASES.get((by_name or legacy or "css").lower(), "CSS_SELECTOR")
        _add_locator(locators, used, selector, selector_value(selector, by_type), "")
        locators[-1].name = _unique_name(snake(locators[-1].name).upper(), used)
        locators[-1].by_type = by_type
        locators[-1].action = infer_action(locators[-1].name, selector)

    if not locators:
        notes.append("No locators were recognized in the Selenium Python page object.")
    return ParsedPO(class_name=class_name, flavor=SELENIUM_PYTHON, locators=locators, notes=notes)


def parse_po(source: str, flavor: str) -> ParsedPO:
    if flavor == SELENIUM_PYTHON:
        return parse_selenium_python(source)
    return parse_playwright_ts(source)


def _entity_names(parsed: ParsedPO, task_name: str | None) -> tuple[str, str, str]:
    raw = (task_name or "").strip() or strip_page_suffix(parsed.class_name)
    pascal_name = pascal(raw)
    if not pascal_name.lower().endswith("page"):
        class_name = f"{pascal_name}Page"
    else:
        class_name = pascal_name
        pascal_name = strip_page_suffix(class_name)
        pascal_name = pascal(pascal_name)
    return class_name, pascal_name, humanize(pascal_name)


def _step_for(loc: Locator, entity: str) -> tuple[str, str, str]:
    """Return (gherkin_keyword, feature_text, method_hint)."""
    label = field_label(loc.name)
    action = loc.action
    if action == "navigate":
        return "Given", "I launch the application", "open"
    if action == "fill":
        return "When", f'I enter "{{string}}" into the {label} field', f"enter{pascal(label)}"
    if action == "select":
        return "When", f'I select "{{string}}" in the {label} field', f"select{pascal(label)}"
    if action == "verify":
        return "Then", f"the {label} should be visible on the {entity} page", f"verify{pascal(label)}"
    return "When", f"I click the {label}", f"click{pascal(label)}"


def _python_method_name(loc: Locator) -> str:
    label = field_label(loc.name)
    if loc.action == "navigate":
        return "open"
    if loc.action == "fill":
        return f"enter_{snake(label)}"
    if loc.action == "select":
        return f"select_{snake(label)}"
    if loc.action == "verify":
        return f"is_{snake(label)}_visible"
    return f"click_{snake(label)}"


def _ts_method_name(loc: Locator) -> str:
    label = field_label(loc.name)
    if loc.action == "navigate":
        return "open"
    if loc.action == "fill":
        return f"enter{pascal(label)}"
    if loc.action == "select":
        return f"select{pascal(label)}"
    if loc.action == "verify":
        return f"verify{pascal(label)}Visible"
    return f"click{pascal(label)}"


def _ts_target(loc: Locator) -> str:
    if loc.expr:
        expr = loc.expr
        if not expr.startswith("this.page.") and not expr.startswith("page."):
            return f"this.page.{expr}"
        return expr.replace("page.", "this.page.", 1) if expr.startswith("page.") else expr
    return f"this.page.locator(this.{loc.name}Selector)"


def render_playwright_ts(parsed: ParsedPO, class_name: str, entity: str, entity_human: str) -> list[GeneratedFile]:
    locators = [loc for loc in parsed.locators if loc.action != "navigate"]
    inst = camel(class_name)
    feature_lines = [
        f"Feature: {entity_human.title()} Functionality",
        "",
        "  @smoke",
        f"  Scenario: Verify {entity_human} workflow",
        "    Given I launch the application",
    ]
    prev = "Given"
    used_methods: set[str] = set()
    used_steps: set[str] = set()
    ops: list[tuple[Locator, str, str, str]] = []

    for loc in locators:
        kw, feature_tmpl, _hint = _step_for(loc, entity_human)
        method = _unique_name(_ts_method_name(loc), used_methods)
        if loc.action == "fill":
            feature_text = feature_tmpl.replace("{string}", "sample value")
            step_pattern = feature_tmpl.replace("{string}", "{string}")
        elif loc.action == "select":
            feature_text = feature_tmpl.replace("{string}", "sample option")
            step_pattern = feature_tmpl.replace("{string}", "{string}")
        else:
            feature_text = feature_tmpl
            step_pattern = feature_tmpl
        if step_pattern in used_steps:
            continue
        used_steps.add(step_pattern)
        gherkin_kw = kw if kw != prev else "And"
        if kw == "Then" and prev != "Then":
            gherkin_kw = "Then"
        feature_lines.append(f"    {gherkin_kw} {feature_text}")
        prev = kw
        ops.append((loc, method, kw, step_pattern))

    if not any(op[2] == "Then" for op in ops):
        feature_lines.append(f"    Then the {entity_human} page should be displayed")

    feature = "\n".join(feature_lines) + "\n"

    fields = []
    methods = []
    for loc in locators:
        if not loc.expr:
            fields.append(f"  private readonly {loc.name}Selector = {ts_str(loc.selector)};")

    methods.append(
        "  async open(appUrl: string) {\n"
        "    await this.page.goto(appUrl);\n"
        "  }"
    )
    emitted_methods: set[str] = set()
    for loc, method, _kw, _step in ops:
        if method in emitted_methods:
            continue
        emitted_methods.add(method)
        target = _ts_target(loc)
        if loc.action == "fill":
            body = f"    await {target}.fill(value);"
            sig = f"  async {method}(value: string)"
        elif loc.action == "select":
            body = f"    await {target}.selectOption(value);"
            sig = f"  async {method}(value: string)"
        elif loc.action == "verify":
            body = f"    await expect({target}).toBeVisible();"
            sig = f"  async {method}()"
        else:
            body = (
                f"    const element = {target};\n"
                "    await element.waitFor({ state: 'visible' });\n"
                "    await element.click();"
            )
            sig = f"  async {method}()"
        methods.append(f"{sig} {{\n{body}\n  }}")

    if not any(op[2] == "Then" for op in ops):
        methods.append(
            "  async verifyPageDisplayed() {\n"
            "    await expect(this.page.locator('body')).toBeVisible();\n"
            "  }"
        )

    field_block = ("\n".join(fields) + "\n\n") if fields else ""
    page_src = (
        "import { expect, Page } from '@playwright/test';\n\n"
        f"export class {class_name} {{\n"
        f"{field_block}"
        "  constructor(private page: Page) {}\n\n"
        + "\n\n".join(methods)
        + "\n}\n"
    )

    keywords = sorted({("Given" if op[2] == "Given" else op[2]) for op in ops} | {"Given", "Then"})
    keyword_import = ", ".join(k for k in ("Given", "When", "Then") if k in keywords or k == "Given" or k == "Then")
    if "When" not in keyword_import and any(op[2] == "When" for op in ops):
        keyword_import = "Given, When, Then"
    else:
        keyword_import = "Given, When, Then"

    step_blocks = [
        "Given('I launch the application', async function (this: ICustomWorld) {",
        f"  this.{inst} = new {class_name}(this.page!);",
        f"  await this.{inst}.open(ConfigReader.getEnvUrl());",
        "});",
        "",
    ]
    for loc, method, kw, step_pattern in ops:
        decorator_kw = "Then" if kw == "Then" else "When" if kw != "Given" else "Given"
        if loc.action in ("fill", "select"):
            step_blocks.append(
                f"{decorator_kw}('{step_pattern}', async function (this: ICustomWorld, value: string) {{\n"
                f"  await this.{inst}.{method}(value);\n"
                "});"
            )
        else:
            step_blocks.append(
                f"{decorator_kw}('{step_pattern}', async function (this: ICustomWorld) {{\n"
                f"  await this.{inst}.{method}();\n"
                "});"
            )
        step_blocks.append("")

    if not any(op[2] == "Then" for op in ops):
        step_blocks.append(
            f"Then('the {entity_human} page should be displayed', async function (this: ICustomWorld) {{\n"
            f"  await this.{inst}.verifyPageDisplayed();\n"
            "});"
        )

    steps_src = (
        f"import {{ {keyword_import} }} from '@cucumber/cucumber';\n"
        "import { ICustomWorld } from '../hooks/hooks';\n"
        "import { ConfigReader } from '../utils/configReader';\n"
        f"import {{ {class_name} }} from '../pageObjects/{class_name}';\n\n"
        "declare module '../hooks/hooks' {\n"
        "  interface ICustomWorld {\n"
        f"    {inst}?: {class_name};\n"
        "  }\n"
        "}\n\n"
        + "\n".join(step_blocks).rstrip()
        + "\n"
    )

    stem = pascal(entity)
    return [
        GeneratedFile(f"{stem}.feature", f"test/features/{stem}.feature", "feature", feature),
        GeneratedFile(f"{class_name}.ts", f"test/pageObjects/{class_name}.ts", "page_object", page_src),
        GeneratedFile(f"{stem}Steps.ts", f"test/stepDefinitions/{stem}Steps.ts", "steps", steps_src),
    ]


def render_selenium_python(parsed: ParsedPO, class_name: str, entity: str, entity_human: str) -> list[GeneratedFile]:
    locators = [loc for loc in parsed.locators if loc.action != "navigate"]
    module_name = snake(class_name)
    inst = module_name
    feature_lines = [
        f"Feature: {entity_human.title()} Functionality",
        "",
        "  @smoke",
        f"  Scenario: Verify {entity_human} workflow",
        "    Given I launch the application",
    ]
    prev = "Given"
    used_methods: set[str] = set()
    used_steps: set[str] = set()
    ops: list[tuple[Locator, str, str, str]] = []

    for loc in locators:
        kw, feature_tmpl, _hint = _step_for(loc, entity_human)
        method = _unique_name(_python_method_name(loc), used_methods)
        if loc.action == "fill":
            feature_text = feature_tmpl.replace("{string}", "sample value")
            step_pattern = 'I enter "{value}" into the ' + field_label(loc.name) + " field"
        elif loc.action == "select":
            feature_text = feature_tmpl.replace("{string}", "sample option")
            step_pattern = 'I select "{value}" in the ' + field_label(loc.name) + " field"
        else:
            feature_text = feature_tmpl
            step_pattern = feature_tmpl
        if step_pattern in used_steps:
            continue
        used_steps.add(step_pattern)
        gherkin_kw = kw if kw != prev else "And"
        if kw == "Then" and prev != "Then":
            gherkin_kw = "Then"
        feature_lines.append(f"    {gherkin_kw} {feature_text}")
        prev = kw
        ops.append((loc, method, kw, step_pattern))

    if not any(op[2] == "Then" for op in ops):
        feature_lines.append(f"    Then the {entity_human} page should be displayed")

    feature = "\n".join(feature_lines) + "\n"

    const_lines = []
    used_consts: set[str] = set()
    const_map: dict[str, str] = {}
    for loc in locators:
        const = _unique_name(snake(loc.name).upper() or "ELEMENT", used_consts)
        const_map[loc.name] = const
        value = selector_value(loc.selector, loc.by_type)
        const_lines.append(f"    {const} = (By.{loc.by_type}, {py_str(value)})")

    methods = [
        "    def open(self, app_url: str):\n"
        "        self.driver.get(app_url)"
    ]
    emitted: set[str] = set()
    for loc, method, _kw, _step in ops:
        if method in emitted:
            continue
        emitted.add(method)
        const = const_map[loc.name]
        if loc.action == "fill":
            body = f"        self.type_text(*self.{const}, value)"
            sig = f"    def {method}(self, value: str):"
        elif loc.action == "select":
            body = (
                f"        element = self.find(*self.{const})\n"
                "        element.send_keys(value)"
            )
            sig = f"    def {method}(self, value: str):"
        elif loc.action == "verify":
            body = f"        return self.is_visible(*self.{const})"
            sig = f"    def {method}(self) -> bool:"
        else:
            body = f"        self.click(*self.{const})"
            sig = f"    def {method}(self):"
        methods.append(f"{sig}\n{body}")

    if not any(op[2] == "Then" for op in ops):
        methods.append(
            "    def is_loaded(self) -> bool:\n"
            "        return self.driver.title is not None"
        )

    page_src = (
        "from selenium.webdriver.common.by import By\n"
        "from pages.base_page import BasePage\n\n\n"
        f"class {class_name}(BasePage):\n"
        f'    """Page Object for the {entity_human} page."""\n\n'
        + "\n".join(const_lines)
        + "\n\n"
        + "\n\n".join(methods)
        + "\n"
    )

    step_blocks = [
        "@given(\"I launch the application\")",
        f"def step_launch_application(context):",
        "    context.driver.get(context.base_url)",
        f"    context.{inst} = {class_name}(context.driver)",
        "",
    ]
    used_fn: set[str] = {"step_launch_application"}
    for loc, method, kw, step_pattern in ops:
        deco = "then" if kw == "Then" else "when" if kw != "Given" else "given"
        fn = _unique_name(f"step_{snake(method)}", used_fn)
        if loc.action in ("fill", "select"):
            step_blocks.append(f"@{deco}({py_str(step_pattern)})")
            step_blocks.append(f"def {fn}(context, value):")
            step_blocks.append(f"    context.{inst}.{method}(value)")
        elif loc.action == "verify":
            step_blocks.append(f"@{deco}({py_str(step_pattern)})")
            step_blocks.append(f"def {fn}(context):")
            step_blocks.append(f"    assert context.{inst}.{method}(), {py_str(f'Expected {field_label(loc.name)} to be visible')}")
        else:
            step_blocks.append(f"@{deco}({py_str(step_pattern)})")
            step_blocks.append(f"def {fn}(context):")
            step_blocks.append(f"    context.{inst}.{method}()")
        step_blocks.append("")

    if not any(op[2] == "Then" for op in ops):
        step_blocks.append(f"@then({py_str(f'the {entity_human} page should be displayed')})")
        step_blocks.append("def step_verify_page_displayed(context):")
        step_blocks.append(f"    assert context.{inst}.is_loaded()")
        step_blocks.append("")

    steps_src = (
        "from behave import given, when, then\n"
        f"from pages.{module_name} import {class_name}\n\n\n"
        + "\n".join(step_blocks).rstrip()
        + "\n"
    )

    stem = snake(entity)
    return [
        GeneratedFile(f"{stem}.feature", f"features/{stem}.feature", "feature", feature),
        GeneratedFile(f"{module_name}.py", f"pages/{module_name}.py", "page_object", page_src),
        GeneratedFile(f"{stem}_steps.py", f"features/steps/{stem}_steps.py", "steps", steps_src),
    ]


def generate_files(source: str, flavor: str = PLAYWRIGHT_TS, task_name: str | None = None) -> dict:
    flavor = (flavor or PLAYWRIGHT_TS).strip().lower()
    if flavor in ("selenium", "python", "selenium_python", "selenium-python"):
        flavor = SELENIUM_PYTHON
    else:
        flavor = PLAYWRIGHT_TS

    parsed = parse_po(source or "", flavor)
    class_name, entity, entity_human = _entity_names(parsed, task_name)
    if flavor == SELENIUM_PYTHON:
        files = render_selenium_python(parsed, class_name, entity, entity_human)
    else:
        files = render_playwright_ts(parsed, class_name, entity, entity_human)

    return {
        "status": "success",
        "flavor": flavor,
        "class_name": class_name,
        "notes": parsed.notes,
        "locator_count": len(parsed.locators),
        "files": [
            {
                "filename": f.filename,
                "relative_path": f.relative_path,
                "kind": f.kind,
                "content": f.content,
            }
            for f in files
        ],
    }
