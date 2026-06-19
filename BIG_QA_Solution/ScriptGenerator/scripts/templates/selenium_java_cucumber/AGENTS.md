# AI Agent Guide — {{PROJECT_NAME}}

> **What this file is.** A tool-agnostic set of rules and "skills" for any AI coding assistant
> (OpenAI Codex, Claude, Cursor, Antigravity, Gemini, Copilot, etc.) working in this project.
> `AGENTS.md` is an open, cross-tool convention, so this one file keeps every assistant aligned.
> The `CLAUDE.md`, `GEMINI.md`, and `.github/copilot-instructions.md` files just point back here.
>
> **Read this before writing any code.** This project was scaffolded from the BIG-QA standardized
> **Selenium + Java + Cucumber** template. Stay inside the conventions below so generated tests
> build and run out of the box and match the rest of the suite.

---

## 1. Stack & ground truth

| Aspect | Value |
|--------|-------|
| Automation tool | **Selenium 4** (WebDriver) |
| Language | **Java** (Maven project) |
| BDD framework | **Cucumber** on **JUnit 5 (JUnit Platform)** |
| Design pattern | **Page Object Model (POM)** |
| Driver management | **WebDriverManager** (drivers auto-resolved — never hard-code driver paths) |
| Config source | **`.env`** read via `dotenv-java` through `utils.ConfigReader` |
| Build/run | **`mvn test`** |

The application under test lives at **`{{BASE_URL}}`** (overridable via `APP_URL` in `.env`).

---

## 2. Where things go (project layout)

Put new code in the right package — this is the highest-value rule. Do not invent new packages.

| Path | What belongs here |
|------|-------------------|
| `src/test/resources/features/*.feature` | Gherkin scenarios (one feature file per feature/page area) |
| `src/test/java/stepDefinitions/*.java` | Step definitions — glue between Gherkin and Page Objects |
| `src/main/java/pageObjects/*.java` | Page Objects — one class per page/component |
| `src/main/java/hooks/Hooks.java` | `@Before`/`@After` driver lifecycle. **Owns the driver — rarely edit.** |
| `src/main/java/utils/DriverFactory.java` | Creates/holds the per-scenario `WebDriver`. Fetch via `DriverFactory.getDriver()`. |
| `src/main/java/utils/ConfigReader.java` | Centralised `.env` access (`getAppUrl()`, `getOrDefault(...)`) |
| `src/test/java/runners/TestRunner.java` | Cucumber↔JUnit Platform runner + glue config. Rarely edit. |
| `Results/` | Reports (generated at run time — never commit/edit by hand) |
| `pom.xml`, `src/test/resources/junit-platform.properties` | Dependencies / Cucumber glue config |

The glue package `stepDefinitions` is registered via `junit-platform.properties` / `TestRunner.java`
— new step classes **must** live in that package or Cucumber reports steps as "undefined".

---

## 3. How to add a new test (canonical workflow)

1. **Write the scenario** in `src/test/resources/features/*.feature` using Gherkin. Use only
   `Given` / `When` / `Then` keywords (see §4). Tag smoke-critical scenarios with `@smoke`.
2. **Add/extend the Page Object** in `src/main/java/pageObjects/`. Put every locator here as a
   `private static final By` constant — never inline a selector inside a step.
3. **Write the step definitions** in `src/test/java/stepDefinitions/`. Fetch the driver with
   `DriverFactory.getDriver()` (do not create your own), instantiate the Page Object, call its
   methods, and assert with JUnit 5 `Assertions`.
4. **Read inputs from config**: URL via `ConfigReader.getAppUrl()`, credentials via
   `ConfigReader.getOrDefault("USER", ...)` / `getOrDefault("PASSWORD", ...)`. Never hard-code.
5. **Run it**: `mvn test`.

### Reference patterns (copy this style)

Page Object — locators as `By` constants, driver injected via constructor:

```java
package pageObjects;

import org.openqa.selenium.By;
import org.openqa.selenium.WebDriver;

public class LoginPage {
    private final WebDriver driver;
    private static final By USERNAME = By.id("txtUserID");
    private static final By LOGIN_BUTTON = By.id("sub");

    public LoginPage(WebDriver driver) { this.driver = driver; }

    public void open(String appUrl) { driver.get(appUrl); }
}
```

Step definition — thin glue, driver from `DriverFactory`, config from `ConfigReader`:

```java
package stepDefinitions;

import io.cucumber.java.en.When;
import utils.ConfigReader;
import utils.DriverFactory;
import pageObjects.LoginPage;

public class LoginSteps {
    private final LoginPage loginPage = new LoginPage(DriverFactory.getDriver());

    @When("I enter valid Username and Password")
    public void i_enter_valid_username_and_password() {
        loginPage.login(ConfigReader.getOrDefault("USER", "standard_user"),
                        ConfigReader.getOrDefault("PASSWORD", "secret_sauce"));
    }
}
```

---

## 4. Coding rules (do / don't)

**Locators & waits**
- Use **only** Selenium 4 `By` locators: `By.id`, `By.name`, `By.cssSelector`, `By.xpath`,
  `By.className`, `By.tagName`, `By.linkText`, `By.partialLinkText`.
- **Never** use deprecated Selenium 3 `findElementBy*` style. Prefer explicit waits with
  `WebDriverWait` + `Duration` over `Thread.sleep()`.
- Use **real, unique** locators for the actual app — do not hallucinate IDs/URLs. The shipped login
  sample uses lenient comma-separated fallback selectors; replace them with the real ones.

**Cucumber / Gherkin**
- Step annotations must be `@Given`, `@When`, `@Then` (from `io.cucumber.java.en`). In `.feature`
  files use `Given` / `When` / `Then`.
- **Never** use `@And` / `@But`. Cucumber matches on step **text**, not the keyword, so a Gherkin
  `And ...` line is matched by the `@When`/`@Then` annotation that logically precedes it.
- New step classes go in the `stepDefinitions` glue package (see §2).

**Structure**
- One Page Object per page/component. The driver is owned by `Hooks` via `DriverFactory` — fetch it,
  never instantiate a `WebDriver` in steps or pages.
- Read all config through `ConfigReader` — never `System.getenv` or hard-coded literals.

---

## 5. Run & verify

```bash
mvn test                 # compile + run the Cucumber suite
```

Reports land in `Results/`. After any change, run `mvn test` and confirm it's green before
considering the task done.

---

## 6. Credentials & secrets (do not "fix" this)

In `.env`:
- `APP_URL`, `USER` — plaintext, safe to edit.
- `PASSWORD` — stored as **`ENC:<token>`** (AES-256-GCM). `ConfigReader` decrypts it at run time
  using `CRED_KEY`, so `ConfigReader.getOrDefault("PASSWORD", ...)` returns plaintext. **Do not**
  replace the `ENC:` value with a plaintext password, and **do not** delete or regenerate
  `CRED_KEY` — either breaks decryption.
- Never hard-code credentials in feature, step, or page files. Never commit real secrets (`.env`
  is gitignored).

---

## 7. Quick "don't" checklist

- ❌ Selectors inside step files → ✅ `By` constants in the Page Object.
- ❌ `new ChromeDriver()` in a step → ✅ `DriverFactory.getDriver()`.
- ❌ `@And` / `@But` annotation → ✅ `@Given` / `@When` / `@Then`.
- ❌ `Thread.sleep()` → ✅ `WebDriverWait` + `Duration`.
- ❌ Plaintext password in `.env` → ✅ keep the `ENC:` token + `CRED_KEY`.
- ❌ Step class outside the `stepDefinitions` package → ✅ keep it in the glue package.
