"""
Playwright codegen  ->  Cucumber feature + Page Object + Step Definitions generator.

Run:
    pip install streamlit
    streamlit run app.py

Paste (or upload) a rough Playwright script recorded with `npx playwright codegen`
and the app generates three files that follow the project's conventions:

    <Task>.feature        (features folder)
    <Task>Page.ts         (page objects folder)
    <Task>Steps.ts        (step definitions folder)

plus the .env additions the generated page object reads its test data from.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# ----------------------------------------------------------------------------------------------
# Sample input (the rough codegen script)
# ----------------------------------------------------------------------------------------------
SAMPLE = """import { test, expect } from '@playwright/test';

test('test', async ({ page }) => {
  await page.goto('https://11013783-demo.creatio.com/0/Shell/#Section/Contacts_ListPage');
  await page.getByRole('link', { name: 'Accounts' }).click();
  await page.locator('#AddButton').getByRole('button', { name: 'New' }).click();
  await page.getByRole('textbox', { name: 'Name' }).click();
  await page.getByRole('textbox', { name: 'Name' }).fill('NeerajAccount');
  await page.getByRole('combobox', { name: 'Type' }).click();
  await page.getByRole('option', { name: 'Competitor' }).click();
  await page.getByRole('combobox', { name: 'Industry' }).click();
  await page.getByRole('option', { name: 'Advertising' }).click();
  await page.getByRole('combobox', { name: 'Primary contact' }).click();
  await page.getByRole('combobox', { name: 'Primary contact' }).fill('neeraj');
  await page.getByRole('option', { name: 'Neeraj', exact: true }).click();
  await page.getByRole('textbox', { name: 'Web' }).click();
  await page.getByRole('textbox', { name: 'Web' }).fill('[www.test.com](https://www.test.com)');
  await page.getByRole('textbox', { name: 'Primary phone' }).click();
  await page.getByRole('textbox', { name: 'Primary phone' }).fill('1234554321');
  await page.getByRole('button', { name: 'Save' }).click();
  await page.getByRole('textbox', { name: 'Search', exact: true }).fill('neerajAccount');
  await page.getByRole('textbox', { name: 'Search', exact: true }).press('Enter');
  await page.getByRole('link', { name: 'NeerajAccount' }).click();
});
"""

# ----------------------------------------------------------------------------------------------
# Small text helpers
# ----------------------------------------------------------------------------------------------


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", text or "")


def pascal(text: str) -> str:
    return "".join(w[:1].upper() + w[1:] for w in words(text)) or "Element"


def camel(text: str) -> str:
    p = pascal(text)
    return p[:1].lower() + p[1:]


def upper_snake(text: str) -> str:
    return "_".join(w.upper() for w in words(text)) or "VALUE"


def lower_words(text: str) -> str:
    return " ".join(w.lower() for w in words(text)) or "element"


def spaced(text: str) -> str:
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", text)
    return " ".join(parts) if parts else text


def singularize(word: str) -> str:
    w = word.strip()
    low = w.lower()
    if low.endswith("ies") and len(w) > 3:
        return w[:-3] + "y"
    if low.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if low.endswith("s") and not low.endswith("ss") and len(w) > 1:
        return w[:-1]
    return w


def ts(value: str) -> str:
    """TypeScript single-quoted string literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def unescape(text: str) -> str:
    return re.sub(r"\\([\\'\"`])", r"\1", text)


MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")

# ----------------------------------------------------------------------------------------------
# Parsing the codegen script
# ----------------------------------------------------------------------------------------------
STR_RE = re.compile(r"""^\s*(['"`])((?:\\.|(?!\1).)*)\1""", re.S)
NAME_RE = re.compile(r"""name:\s*(['"`])((?:\\.|(?!\1).)*)\1""", re.S)
ROLE_RE = re.compile(r"""getByRole\(\s*(['"])(\w+)\1\s*(?:,\s*\{(.*?)\})?\s*\)""", re.S)
TEXT_RE = re.compile(
    r"""(?:getByText|getByLabel|getByPlaceholder|getByTestId|getByAltText|getByTitle|locator)"""
    r"""\(\s*(['"`])((?:\\.|(?!\1).)*)\1""",
    re.S,
)
GOTO_RE = re.compile(r"^page\.goto\((.*)\)$", re.S)
EXPECT_RE = re.compile(r"^expect\((page\..+)\)\.toBeVisible\(\s*\)$", re.S)
ACTION_RE = re.compile(
    r"^(page\..+)\.(click|dblclick|fill|press|check|uncheck|selectOption|hover)\((.*)\)$", re.S
)


@dataclass
class Act:
    action: str
    loc: str = ""
    arg: str = ""
    key: str = ""
    src: str = ""


@dataclass
class Loc:
    code: str  # locator expression without the leading "page."
    role: str | None
    name: str | None
    exact: bool
    simple: bool  # whole locator is one getByRole('role', { name: '...' })
    label: str


def split_statements(source: str) -> list[str]:
    lines = [ln for ln in source.splitlines() if not ln.strip().startswith("//")]
    text = "\n".join(lines)
    start_re = re.compile(r"await\s+(?=page\s*\.|expect\s*\()")
    out: list[str] = []
    pos = 0
    while True:
        m = start_re.search(text, pos)
        if not m:
            break
        i = m.end()
        quote = None
        while i < len(text):
            ch = text[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in "'\"`":
                quote = ch
            elif ch == ";":
                break
            i += 1
        raw = text[m.end():i]
        raw = re.sub(r"\s*\n\s*(?=\.)", "", raw)
        raw = re.sub(r"\s*\n\s*", " ", raw).strip()
        out.append(raw)
        pos = i + 1
    return out


def clean_literal(raw: str, notes: list[str]) -> str:
    value = unescape(raw)
    cleaned = MD_LINK_RE.sub(r"\1", value)
    if cleaned != value:
        notes.append(f"Removed markdown link formatting: '{value}' -> '{cleaned}'")
    return cleaned


def parse_script(source: str) -> tuple[list[Act], str, list[str]]:
    acts: list[Act] = []
    notes: list[str] = []
    url = ""
    for stmt in split_statements(source):
        m = GOTO_RE.match(stmt)
        if m:
            sm = STR_RE.match(m.group(1))
            if sm and not url:
                url = clean_literal(sm.group(2), notes)
            continue
        m = EXPECT_RE.match(stmt)
        if m:
            acts.append(Act("expect_visible", m.group(1), src=stmt))
            continue
        m = ACTION_RE.match(stmt)
        if m:
            action, args = m.group(2), m.group(3)
            sm = STR_RE.match(args)
            arg = clean_literal(sm.group(2), notes) if sm else ""
            if action in ("fill", "press", "selectOption") and not sm:
                notes.append(f"Skipped (no plain string argument): {stmt[:100]}")
                continue
            acts.append(Act(action, m.group(1), arg, "", stmt))
            continue
        notes.append(f"Skipped unsupported statement: {stmt[:100]}")
    return acts, url, notes


def role_of(loc: str) -> str | None:
    ms = list(ROLE_RE.finditer(loc))
    return ms[-1].group(2) if ms else None


def merge(acts: list[Act]) -> list[Act]:
    """Collapse the redundant steps codegen records (click+fill, fill+press, double clicks)."""
    out: list[Act] = []
    i, n = 0, len(acts)
    while i < n:
        a = acts[i]
        if (
            a.action == "click"
            and i + 1 < n
            and acts[i + 1].action == "fill"
            and acts[i + 1].loc == a.loc
            and role_of(a.loc) != "combobox"
        ):
            i += 1
            a = acts[i]
        if a.action == "fill" and i + 1 < n and acts[i + 1].action == "press" and acts[i + 1].loc == a.loc:
            p = acts[i + 1]
            out.append(Act("fill_press", a.loc, a.arg, p.arg, f"{a.src}  +  press('{p.arg}')"))
            i += 2
            continue
        if out and a.action == "click" and out[-1].action == "click" and out[-1].loc == a.loc:
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def add_exact(expr: str, roles: set[str]) -> str:
    def repl(m: re.Match) -> str:
        quote, role, opts = m.group(1), m.group(2), m.group(3)
        if role not in roles or opts is None or "exact" in opts or not re.search(r"name:\s*['\"`]", opts):
            return m.group(0)
        return f"getByRole({quote}{role}{quote}, {{ {opts.strip()}, exact: true }})"

    return ROLE_RE.sub(repl, expr)


def parse_loc(expr_full: str, exact_roles: set[str]) -> Loc:
    expr = expr_full[len("page."):] if expr_full.startswith("page.") else expr_full
    expr = add_exact(expr, exact_roles)
    role = name = None
    simple = exact = False
    roles = list(ROLE_RE.finditer(expr))
    if roles:
        m = roles[-1]
        role = m.group(2)
        opts = m.group(3) or ""
        nm = NAME_RE.search(opts)
        if nm:
            name = unescape(nm.group(2))
        exact = bool(re.search(r"exact:\s*true", opts))
        simple = len(roles) == 1 and m.group(0) == expr and name is not None
    label = name
    if not label:
        ts_matches = list(TEXT_RE.finditer(expr))
        if ts_matches:
            label = unescape(ts_matches[-1].group(2))
    label = (label or role or "Element").strip()
    return Loc(expr, role, name, exact, simple, label)


def detect(acts: list[Act]) -> tuple[str, str]:
    """Return (section, entity) guessed from the first left-navigation click."""
    if acts and acts[0].action == "click":
        loc = parse_loc(acts[0].loc, set())
        if loc.role == "link" and loc.name:
            return loc.name, singularize(loc.name)
    return "", ""


# ----------------------------------------------------------------------------------------------
# Generation
# ----------------------------------------------------------------------------------------------
@dataclass
class Settings:
    task: str
    entity: str
    section: str
    prefix: str
    app_name: str
    feature_title: str
    scenario_title: str
    exact_roles: set = field(default_factory=set)
    add_verify: bool = True
    login_import: str = "./loginPage"
    login_class: str = "LoginPage"
    user_env: str = "APP_USERNAME"
    pass_env: str = "APP_PASSWORD"
    features_dir: str = "test/features"
    steps_dir: str = "test/stepDefinitions"
    pages_dir: str = "test/pageObjects"


@dataclass
class OpDef:
    key: tuple
    kind: str
    phase: str  # Given | When | Then
    method: str
    step: str  # step text used in the step definition
    feature: str  # step text used in the feature file
    code: str  # page-object method source
    param: str = ""


class Env:
    def __init__(self) -> None:
        self.vars: dict[str, str] = {}

    def add(self, name: str, value: str) -> str:
        base, n = name, 2
        while name in self.vars and self.vars[name] != value:
            name = f"{base}_{n}"
            n += 1
        self.vars[name] = value
        return name


def method_code(name: str, lines: list[str]) -> str:
    body = "\n".join(("    " + ln) if ln else "" for ln in lines)
    return f"  async {name}(): Promise<void> {{\n{body}\n  }}"


def stmt(loc_code: str, call: str) -> list[str]:
    one = f"await this.page.{loc_code}.{call};"
    if len(one) <= 88:
        return [one]
    return ["await this.page", f"  .{loc_code}", f"  .{call};"]


class Builder:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.E = pascal(s.entity)
        self.e = lower_words(s.entity)
        self.S = lower_words(s.section)
        self.P = s.prefix
        self.env = Env()
        self.registry: dict[tuple, OpDef] = {}
        self.defs: list[OpDef] = []
        self.timeline: list[OpDef] = []
        self.rows: list[dict] = []
        self.used_methods: set[str] = set()
        self.used_steps: set[str] = set()
        self.fill_helper = False
        self.option_helper = False
        self.name_env: tuple[str, str] | None = None

    def strip_entity(self, label: str) -> str:
        """'Lead name' -> 'name' when the entity is 'Lead' (keeps step text readable)."""
        pat = re.compile(rf"^{re.escape(self.e)}\s+(?=\S)", re.I)
        return pat.sub("", label.strip()) if len(words(label)) > 1 else label

    # -- registry -------------------------------------------------------------------------
    def _uniq_method(self, base: str) -> str:
        name, n = base, 2
        while name in self.used_methods:
            name = f"{base}{n}"
            n += 1
        self.used_methods.add(name)
        return name

    def _uniq_step(self, text: str) -> str:
        out, n = text, 2
        while out in self.used_steps:
            out = f"{text} {n}"
            n += 1
        self.used_steps.add(out)
        return out

    def define(self, key, kind, phase, base, step, code_fn, src, param="", feature=None) -> OpDef:
        if key in self.registry:
            op = self.registry[key]
        else:
            method = self._uniq_method(base)
            if kind not in ("nav", "button"):
                step = self._uniq_step(step)
                feature = None
            op = OpDef(key, kind, phase, method, step, feature or step, code_fn(method), param)
            self.registry[key] = op
            self.defs.append(op)
        self.timeline.append(op)
        self.rows.append(
            {
                "Recorded action": src[:110],
                "Generated step": op.feature,
                "Page object method": f"{op.method}()",
            }
        )
        return op

    # -- operations -----------------------------------------------------------------------
    def add_login(self) -> None:
        s = self.s

        def code(m: str) -> str:
            return method_code(
                m,
                [
                    "await this.loginPage.launchApplication();",
                    f"await this.loginPage.enterUsername(this.getEnv({ts(s.user_env)}));",
                    f"await this.loginPage.enterPassword(this.getEnv({ts(s.pass_env)}));",
                    "await this.loginPage.clickLoginButton();",
                    f"await expect(this.page).toHaveTitle({ts(s.app_name)});",
                ],
            )

        app = " ".join(words(s.app_name)) or "application"
        self.define(
            ("login",), "login", "Given", "login",
            f"the user is logged in to the {app} application to manage {self.S}",
            code, "(login through the existing LoginPage)",
        )

    def nav(self, loc: Loc, a: Act) -> None:
        label = loc.name or loc.label
        var = f"{camel(label)}Link"

        def code(m: str) -> str:
            return method_code(m, [
                f"const {var} = this.page.{loc.code};",
                f"await {var}.waitFor({{ state: 'visible' }});",
                f"await {var}.click();",
            ])

        self.define(
            ("nav", loc.code), "nav", "When", f"click{pascal(label)}",
            f"the user clicks on {{string}} from the left navigation to manage {self.S}",
            code, a.src, param=label,
            feature=f'the user clicks on "{label}" from the left navigation to manage {self.S}',
        )

    def button(self, loc: Loc, a: Act) -> None:
        label = loc.name or loc.label
        base = f"save{self.E}" if label.lower() == "save" else f"click{pascal(label)}"
        var = f"{camel(label)}Button"

        def code(m: str) -> str:
            return method_code(m, [
                f"const {var} = this.page.{loc.code};",
                f"await {var}.waitFor({{ state: 'visible' }});",
                f"await {var}.click();",
            ])

        self.define(
            ("button", loc.code), "button", "When", base,
            f"the user clicks on the {{string}} button on the {self.e} page",
            code, a.src, param=label,
            feature=f'the user clicks on the "{label}" button on the {self.e} page',
        )

    def combo_click(self, loc: Loc, a: Act) -> None:
        label = loc.label
        self.define(
            ("combo_click", loc.code), "combo_click", "When", f"click{pascal(label)}Field",
            f"the user clicks on the {self.E} {self.strip_entity(label)} field",
            lambda m: method_code(m, stmt(loc.code, "click()")), a.src,
        )

    def combo_search(self, loc: Loc, a: Act) -> None:
        label = loc.label
        env = self.env.add(f"{self.P}_{upper_snake(label)}_SEARCH", a.arg)
        self.define(
            ("combo_search", loc.code, a.arg), "combo_search", "When", f"search{pascal(label)}",
            f"the user searches for the configured {self.e} {lower_words(self.strip_entity(label))}",
            lambda m: method_code(m, stmt(loc.code, f"fill(this.getEnv({ts(env)}))")), a.src,
        )

    def option(self, loc: Loc, a: Act, field_label: str | None) -> None:
        fld = field_label or loc.name or loc.label
        if loc.simple and loc.role == "option":
            env = self.env.add(f"{self.P}_{upper_snake(fld)}", loc.name or "")
            self.option_helper = True
            lines = [f"await this.selectOption({ts(env)});"]
        else:
            lines = stmt(loc.code, "click()")
        self.define(
            ("option", loc.code), "option", "When", f"select{pascal(fld)}",
            f"the user selects the configured {self.e} {lower_words(self.strip_entity(fld))}",
            lambda m: method_code(m, lines), a.src,
        )

    def fill(self, loc: Loc, a: Act) -> None:
        label = loc.label
        if a.arg == "":
            lines = stmt(loc.code, "fill('')")
        else:
            env = self.env.add(f"{self.P}_{upper_snake(label)}", a.arg)
            if label.lower() == "name" and self.name_env is None:
                self.name_env = (env, a.arg)
            if loc.simple and loc.role == "textbox":
                self.fill_helper = True
                args = f"{ts(loc.name or label)}, {ts(env)}" + (", true" if loc.exact else "")
                lines = [f"await this.fillTextbox({args});"]
            else:
                lines = stmt(loc.code, f"fill(this.getEnv({ts(env)}))")
        self.define(
            ("fill", loc.code, a.arg), "fill", "When", f"enter{pascal(label)}",
            f"the user enters the configured {self.e} {lower_words(self.strip_entity(label))}",
            lambda m: method_code(m, lines), a.src,
        )

    def fill_press(self, loc: Loc, a: Act) -> None:
        label = loc.label
        var = f"{camel(label)}Field"
        if "search" in label.lower():
            env = self.env.add(f"{self.P}_SEARCH_TERM", a.arg)
            base = f"search{self.E}"
            step = f"the user searches for the configured {self.e} name"
        else:
            env = self.env.add(f"{self.P}_{upper_snake(label)}", a.arg)
            base = f"submit{pascal(label)}"
            step = f"the user submits the configured {self.e} {lower_words(label)}"

        def code(m: str) -> str:
            return method_code(m, [
                f"const {var} = this.page.{loc.code};",
                f"await {var}.fill(this.getEnv({ts(env)}));",
                f"await {var}.press({ts(a.key)});",
            ])

        self.define(("fill_press", loc.code, a.arg, a.key), "fill_press", "When", base, step, code, a.src)

    def open_record(self, loc: Loc, a: Act) -> None:
        name = loc.name or loc.label
        if self.name_env and name.lower() == self.name_env[1].lower():
            env = self.name_env[0]
        else:
            env = self.env.add(f"{self.P}_RECORD_NAME", name)
        role = loc.role or "link"

        if self.s.add_verify:
            def vcode(m: str) -> str:
                return method_code(m, [
                    f"const recordName = this.getEnv({ts(env)});",
                    f"await expect(this.page.getByRole({ts(role)}, {{ name: recordName }}).first()).toBeVisible();",
                ])

            self.define(
                ("verify_record", env), "verify", "Then", f"verify{self.E}Created",
                f"the newly created {self.e} should be displayed in the search results",
                vcode, a.src,
            )

        def ocode(m: str) -> str:
            return method_code(m, [
                f"const recordLink = this.page.getByRole({ts(role)}, {{ name: this.getEnv({ts(env)}) }}).first();",
                "await recordLink.waitFor({ state: 'visible' });",
                "await recordLink.click();",
            ])

        self.define(
            ("open_record", env), "open", "When", f"open{self.E}",
            f"the user opens the newly created {self.e}", ocode, a.src,
        )

    def generic_click(self, loc: Loc, a: Act, wait: bool = False) -> None:
        label = loc.label
        lw = lower_words(label)
        if wait:
            var = f"{camel(label)}Link"
            lines = [
                f"const {var} = this.page.{loc.code};",
                f"await {var}.waitFor({{ state: 'visible' }});",
                f"await {var}.click();",
            ]
            step = f"the user clicks on the {lw} link on the {self.e} page"
        else:
            lines = stmt(loc.code, "click()")
            step = f"the user clicks on the {lw} on the {self.e} page"
        self.define(
            ("click", loc.code), "click", "When", f"click{pascal(label)}", step,
            lambda m: method_code(m, lines), a.src,
        )

    def verify_visible(self, loc: Loc, a: Act) -> None:
        label = loc.label
        self.define(
            ("expect_visible", loc.code), "verify_visible", "Then", f"verify{pascal(label)}Visible",
            f"the {lower_words(label)} should be visible on the {self.e} page",
            lambda m: method_code(m, [f"await expect(this.page.{loc.code}).toBeVisible();"]), a.src,
        )

    def generic(self, loc: Loc, a: Act) -> None:
        label, act = loc.label, a.action
        lw, pl = lower_words(label), pascal(label)
        if act in ("check", "uncheck"):
            base, call = f"{act}{pl}", f"{act}()"
            step = f"the user {act}s the {lw} on the {self.e} page"
        elif act == "dblclick":
            base, call = f"doubleClick{pl}", "dblclick()"
            step = f"the user double clicks on the {lw} on the {self.e} page"
        elif act == "hover":
            base, call = f"hover{pl}", "hover()"
            step = f"the user hovers over the {lw} on the {self.e} page"
        elif act == "selectOption":
            env = self.env.add(f"{self.P}_{upper_snake(label)}", a.arg)
            base, call = f"select{pl}Option", f"selectOption(this.getEnv({ts(env)}))"
            step = f"the user selects the configured {self.e} {lw} option"
        else:  # standalone press
            base, call = f"press{pascal(a.arg)}On{pl}", f"press({ts(a.arg)})"
            step = f"the user presses {lower_words(a.arg)} on the {lw} on the {self.e} page"
        self.define(
            (act, loc.code, a.arg), act, "When", base, step,
            lambda m: method_code(m, stmt(loc.code, call)), a.src,
        )


def build(acts: list[Act], s: Settings) -> Builder:
    b = Builder(s)
    b.add_login()
    merged = merge(acts)
    parsed = [(a, parse_loc(a.loc, s.exact_roles)) for a in merged]
    current_field: str | None = None
    n = len(parsed)
    for idx, (a, loc) in enumerate(parsed):
        last = idx == n - 1
        if a.action == "click":
            if loc.role == "link" and loc.name and idx == 0:
                b.nav(loc, a)
            elif loc.role == "link" and loc.name and last:
                b.open_record(loc, a)
            elif loc.role == "button" and loc.name:
                b.button(loc, a)
            elif loc.role == "combobox":
                current_field = loc.label
                b.combo_click(loc, a)
            elif loc.role == "option":
                b.option(loc, a, current_field)
                current_field = None
            elif loc.role == "link":
                b.generic_click(loc, a, wait=True)
            else:
                b.generic_click(loc, a)
        elif a.action == "fill":
            if loc.role == "combobox":
                current_field = loc.label
                b.combo_search(loc, a)
            else:
                b.fill(loc, a)
        elif a.action == "fill_press":
            b.fill_press(loc, a)
        elif a.action == "expect_visible":
            b.verify_visible(loc, a)
        else:
            b.generic(loc, a)
    return b


# ----------------------------------------------------------------------------------------------
# Rendering the three files
# ----------------------------------------------------------------------------------------------
FILL_HELPER = """  private async fillTextbox(label: string, envName: string, exact = false): Promise<void> {
    await this.page.getByRole('textbox', { name: label, exact }).fill(this.getEnv(envName));
  }"""

OPTION_HELPER = """  private async selectOption(envName: string): Promise<void> {
    await this.page
      .getByRole('option', { name: this.getEnv(envName), exact: true })
      .click();
  }"""

GET_ENV = """  private getEnv(name: string): string {
    const value = process.env[name];
    if (!value) {
      throw new Error(`Missing or empty environment variable: ${name}`);
    }
    return value;
  }"""


def render_page(b: Builder, s: Settings) -> str:
    helpers = []
    if b.fill_helper:
        helpers.append(FILL_HELPER)
    if b.option_helper:
        helpers.append(OPTION_HELPER)
    helpers.append(GET_ENV)
    head = (
        "import { expect, Page } from '@playwright/test';\n"
        f"import {{ {s.login_class} }} from {ts(s.login_import)};\n\n"
        f"export class {s.task}Page {{\n"
        f"  private readonly loginPage: {s.login_class};\n\n"
        "  constructor(private readonly page: Page) {\n"
        f"    this.loginPage = new {s.login_class}(page);\n"
        "  }\n\n"
    )
    body = "\n\n".join(op.code for op in b.defs)
    tail = "\n\n  // ---------- Helpers ----------\n\n" + "\n\n".join(helpers) + "\n}\n"
    return head + body + tail


def _param_block(defs: list[OpDef], text: str, param: str, label: str, inst: str) -> str:
    if len(defs) == 1:
        d = defs[0]
        body = (
            f"    if ({param} !== {ts(d.param)}) {{\n"
            f"      throw new Error(`Unsupported {label}: ${{{param}}}`);\n"
            "    }\n\n"
            f"    await this.{inst}.{d.method}();\n"
        )
    else:
        parts = []
        for i, d in enumerate(defs):
            head = "if" if i == 0 else "} else if"
            parts.append(f"    {head} ({param} === {ts(d.param)}) {{\n      await this.{inst}.{d.method}();\n")
        body = "".join(parts) + (
            "    } else {\n"
            f"      throw new Error(`Unsupported {label}: ${{{param}}}`);\n"
            "    }\n"
        )
    return (
        "When(\n"
        f"  {ts(text)},\n"
        f"  async function (this: CustomWorld, {param}: string) {{\n"
        f"{body}"
        "  }\n"
        ");\n"
    )


def render_steps(b: Builder, s: Settings) -> str:
    page_class = f"{s.task}Page"
    inst = camel(page_class)
    keywords = []
    for kw in ("Given", "When", "Then"):
        if any(d.phase == kw for d in b.defs):
            keywords.append(kw)

    head = (
        "import 'dotenv/config';\n"
        f"import {{ {', '.join(keywords)} }} from '@cucumber/cucumber';\n"
        "import { Page } from '@playwright/test';\n"
        f"import {{ {page_class} }} from '../pageObjects/{page_class}';\n\n"
        "interface CustomWorld {\n"
        "  page: Page;\n"
        f"  {inst}: {page_class};\n"
        "}\n"
    )

    nav_defs = [d for d in b.defs if d.kind == "nav"]
    btn_defs = [d for d in b.defs if d.kind == "button"]
    blocks: list[str] = []
    nav_done = btn_done = False
    for d in b.defs:
        if d.kind == "nav":
            if not nav_done:
                blocks.append(_param_block(nav_defs, d.step, "navigationItem", "navigation item", inst))
                nav_done = True
            continue
        if d.kind == "button":
            if not btn_done:
                blocks.append(_param_block(btn_defs, d.step, "buttonName", "button", inst))
                btn_done = True
            continue
        if d.kind == "login":
            pre = (
                "Given(\n"
                f"  {ts(d.step)},\n"
                "  async function (this: CustomWorld) {\n"
                f"    this.{inst} = new {page_class}(this.page);\n"
                f"    await this.{inst}.{d.method}();\n"
                "  }\n"
                ");\n"
            )
            blocks.append(pre)
            continue
        kw = "Then" if d.phase == "Then" else "When"
        blocks.append(
            f"{kw}(\n"
            f"  {ts(d.step)},\n"
            "  async function (this: CustomWorld) {\n"
            f"    await this.{inst}.{d.method}();\n"
            "  }\n"
            ");\n"
        )
    return head + "\n" + "\n".join(blocks)


def render_feature(b: Builder, s: Settings) -> str:
    lines = [f"Feature: {s.feature_title}", "", f"  Scenario: {s.scenario_title}"]
    prev = None
    for op in b.timeline:
        if op.phase == "Given":
            kw = "Given"
        elif prev == op.phase:
            kw = "And"
        else:
            kw = op.phase
        prev = op.phase
        lines.append(f"    {kw} {op.feature}")
    return "\n".join(lines) + "\n"


def render_env(b: Builder, s: Settings, url: str) -> str:
    lines = [f"# ---- {s.task} test data (add to your .env) ----"]
    for k, v in b.env.vars.items():
        needs_quotes = ("#" in v) or (v != v.strip())
        lines.append(f'{k}="{v}"' if needs_quotes else f"{k}={v}")
    if url:
        lines += ["", f"# Recorded start URL (handled by your existing LoginPage / APP_URL): {url}"]
    return "\n".join(lines) + "\n"


@dataclass
class Result:
    feature: str
    page: str
    steps: str
    env: str
    rows: list
    files: dict
    run_cmd: str


def generate(acts: list[Act], url: str, s: Settings) -> Result:
    b = build(acts, s)
    feature = render_feature(b, s)
    page = render_page(b, s)
    steps = render_steps(b, s)
    env = render_env(b, s, url)
    files = {
        f"{s.features_dir}/{s.task}.feature": feature,
        f"{s.pages_dir}/{s.task}Page.ts": page,
        f"{s.steps_dir}/{s.task}Steps.ts": steps,
        f"env_additions_{s.task}.txt": env,
    }
    run_cmd = f"npx cucumber-js --profile single {s.features_dir}/{s.task}.feature"
    return Result(feature, page, steps, env, b.rows, files, run_cmd)


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(path, content)
    return buf.getvalue()


# ----------------------------------------------------------------------------------------------
# Streamlit UI
# ----------------------------------------------------------------------------------------------
def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Playwright to Cucumber generator", page_icon="🧪", layout="wide")
    st.title("🧪 Playwright → Cucumber generator")
    st.caption(
        "Paste a rough script recorded with `npx playwright codegen` and get a feature file, "
        "a Page Object and a Step Definition file in your project's style."
    )

    # ---- input -----------------------------------------------------------------------------
    st.session_state.setdefault("script", SAMPLE)
    mode = st.radio("Input", ["Paste script", "Upload file"], horizontal=True, label_visibility="collapsed")
    if mode == "Paste script":
        st.text_area("Rough Playwright script", key="script", height=300)
        c1, c2, _ = st.columns([1, 1, 6])
        c1.button("Load sample", on_click=lambda: st.session_state.update(script=SAMPLE))
        c2.button("Clear", on_click=lambda: st.session_state.update(script=""))
        source = st.session_state.script
    else:
        up = st.file_uploader("Upload a .ts / .js / .txt codegen file", type=["ts", "js", "txt"])
        source = up.getvalue().decode("utf-8", errors="replace") if up else ""

    if not source.strip():
        st.info("Paste or upload a Playwright codegen script to begin.")
        st.stop()

    acts, url, notes = parse_script(source)
    if not acts:
        st.error("No Playwright actions found. The script needs lines such as `await page.getByRole(...).click();`")
        st.stop()

    det_section, det_entity = detect(acts)

    # ---- settings --------------------------------------------------------------------------
    with st.sidebar:
        st.header("Settings")
        app_name = st.text_input("Application name", "Creatio")
        entity = st.text_input(
            "Entity (singular)", value=det_entity or "Record", key=f"entity_{det_entity}",
            help="Used in method names, step wording and env variable names, e.g. Account.",
        )
        section = st.text_input(
            "Section (left navigation item)", value=det_section or f"{entity}s",
            key=f"section_{det_section}_{entity}",
        )
        task = st.text_input(
            "Task name (PascalCase)", value=f"Create{pascal(entity)}", key=f"task_{entity}",
            help="Names the files: <Task>.feature, <Task>Page.ts, <Task>Steps.ts and the class <Task>Page.",
        )
        prefix = st.text_input("Env variable prefix", value=upper_snake(entity), key=f"prefix_{entity}")
        feature_title = st.text_input(
            "Feature title", value=f"{pascal(entity)} management in {app_name}", key=f"ft_{entity}_{app_name}"
        )
        scenario_title = st.text_input("Scenario title", value=spaced(task), key=f"sc_{task}")
        exact_roles = st.multiselect(
            "Add `exact: true` to these roles",
            ["link", "textbox", "button", "combobox", "checkbox", "tab", "menuitem"],
            default=["link", "textbox", "button"],
            help="Avoids strict-mode violations when a name like 'Name' also matches 'Full name'.",
        )
        add_verify = st.checkbox("Add a verification step before opening a record", value=True)
        with st.expander("Login conventions"):
            login_class = st.text_input("Login page class", "LoginPage")
            login_import = st.text_input("Login page import path", "./loginPage")
            user_env = st.text_input("Username env variable", "APP_USERNAME")
            pass_env = st.text_input("Password env variable", "APP_PASSWORD")
        with st.expander("Project folders"):
            features_dir = st.text_input("Features folder", "test/features")
            steps_dir = st.text_input("Step definitions folder", "test/stepDefinitions")
            pages_dir = st.text_input("Page objects folder", "test/pageObjects")

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", task or ""):
        st.error("Task name must start with a letter and contain only letters and digits (e.g. CreateAccount).")
        st.stop()
    if not words(entity):
        st.error("Entity cannot be empty.")
        st.stop()

    settings = Settings(
        task=task, entity=entity, section=section or f"{entity}s", prefix=prefix or upper_snake(entity),
        app_name=app_name, feature_title=feature_title, scenario_title=scenario_title,
        exact_roles=set(exact_roles), add_verify=add_verify, login_import=login_import,
        login_class=login_class, user_env=user_env, pass_env=pass_env,
        features_dir=features_dir.strip("/"), steps_dir=steps_dir.strip("/"), pages_dir=pages_dir.strip("/"),
    )
    result = generate(acts, url, settings)

    # ---- output ----------------------------------------------------------------------------
    st.success(f"Parsed {len(acts)} recorded actions and generated {len(result.rows)} steps for **{task}**.")
    for n in notes:
        st.warning(n)

    st.download_button(
        "⬇️ Download all files (.zip)", make_zip(result.files), file_name=f"{task}_generated.zip",
        mime="application/zip", key="zip",
    )

    t_feat, t_page, t_steps, t_env, t_map = st.tabs(
        ["Feature", "Page Object", "Step Definitions", ".env & run", "Mapping"]
    )
    with t_feat:
        st.caption(f"{settings.features_dir}/{task}.feature")
        st.code(result.feature, language="gherkin")
        st.download_button("Download", result.feature, file_name=f"{task}.feature", key="d_feat")
    with t_page:
        st.caption(f"{settings.pages_dir}/{task}Page.ts")
        st.code(result.page, language="typescript")
        st.download_button("Download", result.page, file_name=f"{task}Page.ts", key="d_page")
    with t_steps:
        st.caption(f"{settings.steps_dir}/{task}Steps.ts")
        st.code(result.steps, language="typescript")
        st.download_button("Download", result.steps, file_name=f"{task}Steps.ts", key="d_steps")
    with t_env:
        st.markdown("**Add these test-data variables to your `.env`** (the page object reads them with `getEnv`):")
        st.code(result.env, language="bash")
        st.markdown("**Run only this feature** (uses the `single` profile from your `cucumber.js`):")
        st.code(result.run_cmd, language="bash")
        st.download_button("Download env additions", result.env, file_name=f"env_additions_{task}.txt", key="d_env")
    with t_map:
        st.caption("How each recorded action was turned into a step and a page-object method.")
        st.dataframe(result.rows, hide_index=True)

    with st.expander("Write the files straight into my project"):
        root = st.text_input("Project root folder", value=".")
        overwrite = st.checkbox("Overwrite files that already exist", value=False)
        if st.button("Write files"):
            base = Path(root).expanduser().resolve()
            for rel, content in result.files.items():
                if rel.startswith("env_additions_"):
                    continue
                target = base / rel
                if target.exists() and not overwrite:
                    st.warning(f"Skipped (already exists): {target}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                st.success(f"Wrote {target}")

    with st.expander("Conventions & limitations"):
        st.markdown(
            """
- Step wording includes the entity (for example *"the configured account name"*), so steps from different
  tasks do not clash. Two tasks about the **same entity** can still produce identical step text, so give one
  of them a different entity wording.
- The **first link click** is treated as left navigation, **buttons** are grouped into one parameterised step,
  a **combobox + option** becomes click-field and select steps, and a **final link click** becomes
  verify + open steps.
- `click` + `fill` on a textbox collapses into a single fill, and `fill` + `press` becomes a search step.
- Markdown links that codegen sometimes records (`[x](https://x)`) are cleaned to plain text.
- Test data is never hard-coded: every value goes to an env variable. Do not commit real credentials.
            """
        )


if __name__ == "__main__":
    main()