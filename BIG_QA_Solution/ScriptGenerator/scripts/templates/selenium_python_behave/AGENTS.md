# AI Agent Guide — {{PROJECT_NAME}}

> **What this file is.** A tool-agnostic set of rules and "skills" for any AI coding assistant
> (Codex, Claude, Cursor, Copilot, Gemini, etc.) working in this project. `AGENTS.md` is an open,
> cross-tool convention, so a single file here keeps every assistant aligned. If your tool looks for
> a different filename, see the pointer files in this repo (`CLAUDE.md`) — they all defer to this one.
>
> **Read this before writing any code.** This project was scaffolded from the BIG-QA standardized
> **Selenium + Python + Behave** template. Stay inside the conventions below so generated tests run
> out of the box and match the rest of the suite.

---

## 1. Stack & ground truth

| Aspect | Value |
|--------|-------|
| Automation tool | **Selenium 4** (WebDriver) |
| Language | **Python 3.12+** |
| BDD framework | **Behave** (Gherkin) |
| Design pattern | **Page Object Model (POM)** |
| Driver management | **webdriver-manager** (drivers auto-resolved — never hard-code driver paths) |
| Config source | **`.env`** read via `python-dotenv` |

The application under test lives at **`{{BASE_URL}}`** (overridable via `APP_URL` in `.env`).

---

## 2. Where things go (project layout)

Put new code in the right place — this is the highest-value rule. Do not invent new top-level folders.

| Path | What belongs here |
|------|-------------------|
| `features/*.feature` | Gherkin scenarios (one feature file per feature/page area) |
| `features/steps/*_steps.py` | Step definitions — glue between Gherkin and Page Objects |
| `features/environment.py` | Driver lifecycle hooks (setup/teardown, screenshot on failure). **Rarely edit.** |
| `pages/*.py` | Page Objects — one class per page/component, all extend `BasePage` |
| `pages/base_page.py` | Shared element helpers (`find`, `click`, `type_text`, `is_visible`). Extend, don't duplicate. |
| `utils/config_reader.py` | Centralised `.env` access |
| `Results/` | Reports + failure screenshots (generated at run time — never commit/edit by hand) |
| `behave.ini`, `requirements.txt` | Config / dependencies |

---

## 3. How to add a new test (canonical workflow)

Follow these steps in order — this mirrors how the BIG-QA generator itself produces code:

1. **Write the scenario** in a `features/*.feature` file using Gherkin. Use only `Given` / `When` /
   `Then` as step keywords (see §4). Tag smoke-critical scenarios with `@smoke`.
2. **Add/extend the Page Object** in `pages/`. Put every locator here as a class constant — never
   inline a selector inside a step. The class must extend `BasePage` and use its helpers.
3. **Write the step definitions** in `features/steps/`. Steps stay thin: they call Page Object
   methods and make assertions. No Selenium calls or raw locators in step files.
4. **Read inputs from config**, not literals: URL from `context.base_url` / `APP_URL`, credentials
   from `os.getenv("USER")` / `os.getenv("PASSWORD")` (see §6).
5. **Run it**: `behave` (all) or `behave --tags=@smoke` (smoke only).

### Reference patterns (copy this style)

Page Object — locators as constants, behaviour via `BasePage` helpers:

```python
from selenium.webdriver.common.by import By
from pages.base_page import BasePage


class LoginPage(BasePage):
    """Page Object for the Login page."""

    USERNAME_INPUT = (By.ID, "txtUserID")
    PASSWORD_INPUT = (By.ID, "txtPassword")
    LOGIN_BUTTON   = (By.ID, "sub")

    def enter_username(self, username: str):
        self.type_text(*self.USERNAME_INPUT, username)

    def click_login(self):
        self.click(*self.LOGIN_BUTTON)
```

Step definition — thin glue, no locators, reads creds from env:

```python
import os
from behave import given, when, then
from pages.login_page import LoginPage


@when("I enter valid Username and Password")
def step_enter_credentials(context):
    username = os.getenv("USER", "standard_user")
    password = os.getenv("PASSWORD", "secret_sauce")
    context.login_page.enter_username(username)
    context.login_page.enter_password(password)
```

---

## 4. Coding rules (do / don't)

These follow the team's Selenium-4 Python standards. Violating them produces code that breaks or
diverges from the suite.

**Locators & waits**
- Use **only** Selenium 4 `By` locators: `driver.find_element(By.ID, "...")`,
  `By.NAME`, `By.XPATH`, `By.CSS_SELECTOR`, `By.CLASS_NAME`, `By.TAG_NAME`, `By.LINK_TEXT`,
  `By.PARTIAL_LINK_TEXT`.
- **Never** use deprecated Selenium 3 methods: `find_element_by_id()`, `find_element_by_xpath()`,
  `find_elements_by_*()`, etc.
- Always go through `BasePage` helpers (which wrap `WebDriverWait` + `expected_conditions`). Do not
  use `time.sleep()` for synchronisation, and avoid raw `driver.find_element` inside steps.
- Use **real, unique** locators for the actual app. Do **not** hallucinate IDs/URLs. The shipped
  login sample is intentionally lenient — replace its locators with the real ones for `{{BASE_URL}}`.

**Behave / Gherkin**
- Step decorators must be exactly `@given`, `@when`, `@then` (lowercase). In `.feature` files use
  `Given` / `When` / `Then`.
- **Never** use `And` / `But` (or `@and` / `@but`) as a step keyword — substitute the logically
  correct `Given` / `When` / `Then` based on the preceding step.
- The Behave context object carries state between steps: `context.driver`, `context.base_url`,
  and the page objects you attach (`context.login_page`, `context.home_page`, …).

**Structure**
- One Page Object per page/component; reuse `BasePage`. Match the existing snake_case method
  naming and the import/style conventions already present in `pages/` and `features/steps/`.
- Before adding a step, check `features/steps/` for an existing matching step — extend/reuse rather
  than duplicating.

---

## 5. Run & verify

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
behave                          # run all scenarios
behave --tags=@smoke            # run only @smoke
```

Reports and failure screenshots land in `Results/` (configured in `behave.ini` and
`features/environment.py`). After any code change, run the relevant scenarios and confirm they pass
before considering the task done.

---

## 6. Credentials & secrets (do not "fix" this)

This project encrypts its password at rest. In `.env`:

- `APP_URL`, `USER` — plaintext, safe to edit.
- `PASSWORD` — stored as **`ENC:<token>`** (AES-GCM). It is decrypted automatically at run time by
  `features/environment.py` using `CRED_KEY`, so `os.getenv("PASSWORD")` returns plaintext inside
  tests. **Do not** replace the `ENC:` value with a plaintext password, and **do not** delete or
  regenerate `CRED_KEY` — doing either breaks decryption and the login sample.
- Never hard-code credentials in `.feature`, step, or page files. Always read them from the
  environment via `os.getenv(...)` or `utils/config_reader.py`.
- Never commit real secrets. `.env` is gitignored; keep it that way.

---

## 7. Quick "don't" checklist

- ❌ Selectors inside step files → ✅ in the Page Object as constants.
- ❌ `time.sleep()` → ✅ `BasePage` wait helpers.
- ❌ `@and` / `And` step keyword → ✅ `Given` / `When` / `Then`.
- ❌ Selenium 3 `find_element_by_*` → ✅ `By.*` locators.
- ❌ Plaintext password in `.env` → ✅ keep the `ENC:` token + `CRED_KEY`.
- ❌ New top-level folders → ✅ use the layout in §2.
