# AI Agent Guide — {{PROJECT_NAME}}

> **What this file is.** A tool-agnostic set of rules and "skills" for any AI coding assistant
> (OpenAI Codex, Claude, Cursor, Antigravity, Gemini, Copilot, etc.) working in this project.
> `AGENTS.md` is an open, cross-tool convention, so this one file keeps every assistant aligned.
> The `CLAUDE.md`, `GEMINI.md`, and `.github/copilot-instructions.md` files just point back here.
>
> **Read this before writing any code.** This project was scaffolded from the BIG-QA standardized
> **Selenium + C# + Reqnroll** template. Stay inside the conventions below so generated tests
> build and run out of the box and match the rest of the suite.

---

## 1. Stack & ground truth

| Aspect | Value |
|--------|-------|
| Automation tool | **Selenium 4** (with built-in **Selenium Manager** — no WebDriverManager/driver downloads) |
| Language | **C#** (.NET) |
| BDD framework | **Reqnroll** (the maintained SpecFlow successor) on **NUnit** |
| Design pattern | **Page Object Model (POM)** |
| Config source | **`.env`** read via `DotNetEnv` through `Utils/ConfigReader` |
| Run | **`dotnet test`** |

The application under test lives at **`{{BASE_URL}}`** (overridable via `APP_URL` in `.env`).

Root namespace: **`SeleniumReqnrollTests`** (e.g. `SeleniumReqnrollTests.PageObjects`).

---

## 2. Where things go (project layout)

Put new code in the right folder/namespace — this is the highest-value rule. Do not invent new folders.

| Path | What belongs here |
|------|-------------------|
| `Features/*.feature` | Gherkin scenarios (one feature file per feature/page area) |
| `StepDefinitions/*.cs` | Step definitions (`[Binding]` classes) — glue between Gherkin and Page Objects |
| `PageObjects/*.cs` | Page Objects — one class per page/component, each takes an `IWebDriver` |
| `Hooks/Hooks.cs` | `[BeforeScenario]`/`[AfterScenario]` driver lifecycle. **Owns the driver — rarely edit.** |
| `Utils/DriverFactory.cs` | Creates the `IWebDriver` (via Selenium Manager) |
| `Utils/ConfigReader.cs` | Centralised `.env` access (`GetAppUrl()`, `GetOrDefault(...)`) |
| `reqnroll.json`, `*.csproj` | Reqnroll + project config / dependencies |

The `IWebDriver` is created in `Hooks` and provided to step classes through **Reqnroll context
injection** — declare it as a constructor parameter; do not new-up a driver yourself.

---

## 3. How to add a new test (canonical workflow)

1. **Write the scenario** in `Features/*.feature` using Gherkin. Use only `Given` / `When` / `Then`
   step keywords (see §4). Tag smoke-critical scenarios with `@smoke`.
2. **Add/extend the Page Object** in `PageObjects/`. Put every locator here as a
   `private static readonly By` field — never inline a selector inside a step. Each Page Object
   takes an `IWebDriver` in its constructor.
3. **Write the step definitions** in `StepDefinitions/` as a `[Binding]` class. Receive `IWebDriver`
   via the constructor (context injection), instantiate the Page Object, call its methods, and
   assert with NUnit `Assert`.
4. **Read inputs from config**: URL via `ConfigReader.GetAppUrl()`, credentials via
   `ConfigReader.GetOrDefault("USER", ...)` / `GetOrDefault("PASSWORD", ...)`. Never hard-code.
5. **Run it**: `dotnet test`.

### Reference patterns (copy this style)

Page Object — locators as `By` fields, `IWebDriver` injected via constructor:

```csharp
using OpenQA.Selenium;

namespace SeleniumReqnrollTests.PageObjects;

public class LoginPage
{
    private readonly IWebDriver _driver;
    private static readonly By Username = By.Id("txtUserID");
    private static readonly By LoginButton = By.Id("sub");

    public LoginPage(IWebDriver driver) => _driver = driver;

    public void Open(string appUrl) => _driver.Navigate().GoToUrl(appUrl);
}
```

Step definition — `[Binding]` class, driver via Reqnroll context injection:

```csharp
using NUnit.Framework;
using OpenQA.Selenium;
using Reqnroll;
using SeleniumReqnrollTests.PageObjects;
using SeleniumReqnrollTests.Utils;

namespace SeleniumReqnrollTests.StepDefinitions;

[Binding]
public class LoginSteps
{
    private readonly LoginPage _loginPage;

    public LoginSteps(IWebDriver driver) => _loginPage = new LoginPage(driver);

    [Given("I launch the application")]
    public void GivenILaunchTheApplication() => _loginPage.Open(ConfigReader.GetAppUrl());
}
```

---

## 4. Coding rules (do / don't)

**Locators & waits**
- Use **only** Selenium 4 `By` locators: `By.Id`, `By.Name`, `By.CssSelector`, `By.XPath`,
  `By.ClassName`, `By.TagName`, `By.LinkText`, `By.PartialLinkText`.
- Prefer `WebDriverWait` over `Thread.Sleep()`. Drivers are resolved by **Selenium Manager** — do
  not add WebDriverManager or hard-code driver paths.
- Use **real, unique** locators for the actual app — do not hallucinate IDs/URLs. The shipped login
  sample uses lenient comma-separated fallback selectors; replace with the real ones.

**Reqnroll / Gherkin**
- Step attributes are `[Given]`, `[When]`, `[Then]` (from `Reqnroll`). In `.feature` files use
  `Given` / `When` / `Then`.
- **Never** use `[And]` / `[But]`. Reqnroll matches on step **text**, so a Gherkin `And ...` line is
  matched by the preceding `[Given]`/`[When]`/`[Then]` binding.
- Step classes must be annotated `[Binding]`, and the driver comes in through the constructor
  (context injection) — never instantiate a driver in a step.

**Structure**
- One Page Object per page/component, each taking an `IWebDriver`. Keep everything under the
  `SeleniumReqnrollTests.*` namespaces matching the folder.
- Read all config through `ConfigReader` — never `Environment.GetEnvironmentVariable` directly or
  hard-coded literals.

---

## 5. Run & verify

```bash
dotnet test              # restore, build, and run the Reqnroll/NUnit suite
```

After any change, run `dotnet test` and confirm it's green before considering the task done.

---

## 6. Credentials & secrets (do not "fix" this)

In `.env`:
- `APP_URL`, `USER` — plaintext, safe to edit.
- `PASSWORD` — stored as **`ENC:<token>`** (AES-256-GCM). `ConfigReader` decrypts it at run time
  using `CRED_KEY`, so `ConfigReader.GetOrDefault("PASSWORD", ...)` returns plaintext. **Do not**
  replace the `ENC:` value with a plaintext password, and **do not** delete or regenerate
  `CRED_KEY` — either breaks decryption.
- Never hard-code credentials in feature, step, or page files. Never commit real secrets (`.env`
  is gitignored).

---

## 7. Quick "don't" checklist

- ❌ Selectors inside step files → ✅ `By` fields in the Page Object.
- ❌ `new ChromeDriver()` in a step → ✅ receive `IWebDriver` via the constructor (context injection).
- ❌ `[And]` / `[But]` attribute → ✅ `[Given]` / `[When]` / `[Then]`.
- ❌ `Thread.Sleep()` → ✅ `WebDriverWait`.
- ❌ Plaintext password in `.env` → ✅ keep the `ENC:` token + `CRED_KEY`.
- ❌ Adding WebDriverManager → ✅ rely on Selenium Manager.
