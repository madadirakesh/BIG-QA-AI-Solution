# {{PROJECT_NAME}}

QA automation framework — **Selenium 4 for Java**, **Cucumber BDD**, **JUnit 5**, **Maven**.
Driver binaries are managed automatically by **WebDriverManager** — no manual installs.
Scaffolded by the BIG-QA Script Generator.

---

## Prerequisites

- **Java 11+** (`java -version`)
- **Maven 3.6+** (`mvn -version`)
- A browser installed locally — Chrome (default), Firefox, or Edge.
- Internet access on first run (WebDriverManager downloads the driver binary).

## Quick start

```bash
# 1. Configure environment (one time)
cp .env .env
#    then edit .env with your application URL and credentials

# 2. Install Maven dependencies (one time)
mvn install -DskipTests

# 3. Run all features
mvn test
```

Reports land in [`Results/`](Results/):
- `Results/cucumber.html` — browseable HTML report
- `Results/cucumber.json` — JSON for CI tooling and the BIG-QA dashboard
- `Results/screenshots/` — auto-captured on every failed scenario

## Project layout

```
.
├── pom.xml                                    Maven build file
├── .env                                       Local config (gitignored, keep your creds here)
├── .env.example                               Template copy, safe to commit
├── src/
│   ├── main/java/
│   │   ├── hooks/Hooks.java                   Cucumber @Before / @After (open & close browser)
│   │   ├── pageObjects/                       Page Object classes (you add these)
│   │   └── utils/
│   │       ├── ConfigReader.java              Reads .env via dotenv-java
│   │       └── DriverFactory.java             Per-thread WebDriver lifecycle (WebDriverManager)
│   └── test/
│       ├── java/
│       │   ├── runners/TestRunner.java        JUnit 5 + Cucumber entry point
│       │   └── stepDefinitions/               Step definitions (you add these)
│       └── resources/
│           ├── features/                      .feature files (BDD scenarios)
│           └── junit-platform.properties      Cucumber glue + plugin config
└── Results/                                   Test reports (generated, gitignored)
```

## Configuration

All runtime settings live in `.env`. The same file is read by the test code via
`ConfigReader.getProperty("KEY")`.

| Variable   | Required | Default     | Description                              |
|------------|----------|-------------|------------------------------------------|
| `APP_URL`  | yes      | —           | Base URL of the application under test   |
| `BROWSER`  | no       | `chrome`    | `chrome` \| `firefox` \| `edge`          |
| `HEADLESS` | no       | `false`     | `true` to run without a visible browser  |
| `USER`     | no       | —           | Username for login flows                 |
| `PASSWORD` | no       | —           | Password for login flows                 |

## Writing your first test

1. **Add a feature file** under `src/test/resources/features/`, e.g. `login.feature`:
   ```gherkin
   Feature: Login

     Scenario: User logs in successfully
       Given I launch the application
       When I enter valid Username and Password
       And I click the login button
       Then I should be redirected to the homepage
   ```
2. **Add a page object** under `src/main/java/pageObjects/LoginPage.java`.
3. **Add step definitions** under `src/test/java/stepDefinitions/LoginSteps.java`, using
   `DriverFactory.getDriver()` to access the WebDriver instance.
4. Run `mvn test`.

Or use the **Script Developer** wizard in BIG-QA to auto-generate step + page files from the
feature, then refine the locators.

## Common commands

| Command                                                                    | What it does                              |
|----------------------------------------------------------------------------|-------------------------------------------|
| `mvn test`                                                                  | Run every feature                         |
| `mvn test -Dcucumber.filter.tags="@smoke"`                                  | Run only `@smoke`-tagged scenarios        |
| `mvn test -Dcucumber.filter.tags="not @wip"`                                | Skip work-in-progress scenarios           |
| `BROWSER=firefox mvn test`                                                  | Override .env at the command line         |
| `HEADLESS=true mvn test`                                                    | Run headless (good for CI)                |

## Troubleshooting

- **`'artifactId' with value 'X' does not match a valid id pattern`** — project name contained
  characters Maven disallows. Recreate via the BIG-QA modal; names are sanitised into the
  artifactId now (e.g. `First Java App` → `first-java-app`).
- **`Property 'APP_URL' not found in .env file`** — you forgot to `cp .env.example .env` and
  edit it.
- **`SessionNotCreatedException: ... browser version X / driver version Y`** — usually means
  WebDriverManager couldn't reach its cache server. Delete `~/.cache/selenium/` and retry.
- **Chrome won't launch on Linux** — install missing deps:
  `sudo apt install libnss3 libgbm1 libasound2`.
- **Tests pass locally but fail in CI** — set `HEADLESS=true` and add the Chrome flags already
  configured in `DriverFactory.java` (`--no-sandbox`, `--disable-dev-shm-usage`).
