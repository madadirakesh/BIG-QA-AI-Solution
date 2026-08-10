# AI Agent Guide — {{PROJECT_NAME}}

> **What this file is.** A tool-agnostic set of rules and "skills" for any AI coding assistant
> (OpenAI Codex, Claude, Cursor, Antigravity, Gemini, Copilot, etc.) working in this project.
> `AGENTS.md` is an open, cross-tool convention, so this one file keeps every assistant aligned.
> The `CLAUDE.md`, `GEMINI.md`, and `.github/copilot-instructions.md` files just point back here.
>
> **Read this before writing any code.** This project was scaffolded from the BIG-QA standardized
> **Playwright + Python + Behave** template. Stay inside the conventions below so generated tests
> run out of the box and match the rest of the suite.

---

## 1. Stack & ground truth

| Aspect | Value |
|--------|-------|
| Automation tool | **Playwright for Python** (`playwright.sync_api`) |
| Language | **Python 3.10+** |
| BDD framework | **Behave** (Gherkin) |
| Design pattern | **Page Object Model (POM)** |
| Browser management | Playwright-managed (`playwright install` for browser binaries) |
| Config source | **`.env`** read via `python-dotenv` |
| Run | **`behave`** |

The application under test lives at **`{{BASE_URL}}`** (overridable via `APP_URL` in `.env`).

> **Note:** This is the *Playwright* Python template — Page Objects take a Playwright `Page` and
> use Playwright selectors. It deliberately mirrors the Selenium/Python template's method names
> (`open` / `click` / `type_text` / `is_visible`) so steps read the same, but the underlying API is
> Playwright, **not** Selenium. Do not introduce `selenium`/`webdriver` imports here.

---

## 2. Where things go (project layout)

Put new code in the right place — this is the highest-value rule. Do not invent new top-level folders.

| Path | What belongs here |
|------|-------------------|
| `features/*.feature` | Gherkin scenarios (one feature file per feature/page area) |
| `features/steps/*_steps.py` | Step definitions — glue between Gherkin and Page Objects |
| `features/environment.py` | Playwright + browser lifecycle hooks, screenshot on failure. **Rarely edit.** |
| `pages/*.py` | Page Objects — one class per page/component, all extend `BasePage` |
| `pages/base_page.py` | Shared Playwright helpers (`open`, `click`, `type_text`, `is_visible`, `count`) |
| `utils/config_reader.py` | Centralised `.env` access |
| `Results/` | Reports + failure screenshots (generated at run time — never commit/edit by hand) |
| `behave.ini`, `requirements.txt` | Config / dependencies |

---

## 3. How to add a new test (canonical workflow)

1. **Write the scenario** in a `features/*.feature` file using Gherkin. Use only `Given` / `When` /
   `Then` step keywords (see §4). Tag smoke-critical scenarios with `@smoke`.
2. **Add/extend the Page Object** in `pages/`. Put every selector here as a class constant — never
   inline a selector inside a step. The class must extend `BasePage` and use its helpers.
3. **Write the step definitions** in `features/steps/`. Steps stay thin: they get the page from
   `context.page`, call Page Object methods, and assert. No raw Playwright locator calls in steps.
4. **Read inputs from config**: URL from `context.base_url` / `APP_URL`, credentials from
   `os.getenv("USER")` / `os.getenv("PASSWORD")` (see §6).
5. **Run it**: `behave` (all) or `behave --tags=@smoke` (smoke only).

### Reference patterns (copy this style)

Page Object — selectors as constants, behaviour via `BasePage` helpers:

```python
from pages.base_page import BasePage


class LoginPage(BasePage):
    USERNAME_INPUT = "#txtUserID"
    PASSWORD_INPUT = "#txtPassword"
    LOGIN_BUTTON   = "#sub"

    def enter_username(self, username: str):
        self.type_text(self.USERNAME_INPUT, username)

    def click_login(self):
        self.click(self.LOGIN_BUTTON)
```

Step definition — thin glue, page from `context.page`, creds from env:

```python
import os
from behave import given, when, then
from pages.login_page import LoginPage


@given("I launch the application")
def step_launch_application(context):
    context.page.goto(context.base_url)
    context.login_page = LoginPage(context.page)
```

---

## 4. Coding rules (do / don't)

**Locators & waits**
- Selectors are Playwright strings (CSS, `text=`, role). Playwright **auto-waits** for
  actionability — **do not** use `time.sleep()`. Use `BasePage` helpers; for explicit waits use
  `page.wait_for_load_state()` / `locator.wait_for()`.
- Use **real, unique** selectors for the actual app — do not hallucinate selectors/URLs. The
  shipped login sample uses lenient comma-separated fallback selectors; replace with the real ones.
- **Never** import `selenium` / `webdriver` — this is a Playwright project.

**Behave / Gherkin**
- Step decorators must be exactly `@given`, `@when`, `@then` (lowercase). In `.feature` files use
  `Given` / `When` / `Then`.
- **Never** use `And` / `But` (or `@and` / `@but`) — substitute the logically correct
  `Given` / `When` / `Then`.
- The Behave context carries state between steps: `context.page`, `context.base_url`, and the page
  objects you attach (`context.login_page`, `context.home_page`, …).

**Structure**
- One Page Object per page/component; reuse `BasePage`. Match the existing snake_case method naming
  and import/style conventions already present in `pages/` and `features/steps/`.
- Before adding a step, check `features/steps/` for an existing matching step — extend/reuse rather
  than duplicating.

---

## 5. Run & verify

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
python -m pip install -r requirements.txt
python -m playwright install chromium  # one-time: default browser via this venv's Python
behave                          # run all scenarios
behave --tags=@smoke            # run only @smoke
```

Reports and failure screenshots land in `Results/`. After any change, run the relevant scenarios
and confirm they pass before considering the task done.

---

## 6. Credentials & secrets (do not "fix" this)

In `.env`:
- `APP_URL`, `USER` — plaintext, safe to edit.
- `PASSWORD` — stored as **`ENC:<token>`** (AES-GCM). It's decrypted automatically at run time by
  `features/environment.py` using `CRED_KEY`, so `os.getenv("PASSWORD")` returns plaintext inside
  tests. **Do not** replace the `ENC:` value with a plaintext password, and **do not** delete or
  regenerate `CRED_KEY` — either breaks decryption.
- Never hard-code credentials in `.feature`, step, or page files — read them via `os.getenv(...)`
  or `utils/config_reader.py`. Never commit real secrets (`.env` is gitignored).

---

## 7. Quick "don't" checklist

- ❌ Selectors inside step files → ✅ in the Page Object as constants.
- ❌ `time.sleep()` → ✅ Playwright auto-wait / `BasePage` helpers.
- ❌ `selenium` / `webdriver` imports → ✅ `playwright.sync_api` only.
- ❌ `@and` / `And` step keyword → ✅ `Given` / `When` / `Then`.
- ❌ Plaintext password in `.env` → ✅ keep the `ENC:` token + `CRED_KEY`.
- ❌ New top-level folders → ✅ use the layout in §2.
