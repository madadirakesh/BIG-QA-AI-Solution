# Selenium + C# + Reqnroll BDD Template

A ready-to-run BDD test project: **Reqnroll** (the maintained SpecFlow successor) on **NUnit**,
driving **Selenium 4** with a Page Object + hooks layout that mirrors the Java/Python templates in
this repo.

## Prerequisites

- **.NET SDK 8.0+** (`dotnet --version`)
- A local browser (Chrome by default). **No driver download needed** — Selenium 4.6+ includes
  Selenium Manager, which fetches the matching driver automatically on first run.
- Internet access on first `dotnet restore` (NuGet packages) and first test run (Selenium Manager
  driver download). Behind a corporate proxy, see `../../SETUP-PROXY.md`.

## Quick start

```bash
dotnet restore
dotnet test
```

`dotnet test` builds the project (Reqnroll generates the `.feature` code-behind automatically),
launches the browser, runs the sample login scenario, and writes results under `Results/`.

## Layout

| Path | Purpose |
|------|---------|
| `Features/LoginFeature.feature` | Gherkin scenarios |
| `StepDefinitions/LoginSteps.cs` | Step bindings (receives the driver by injection) |
| `PageObjects/LoginPage.cs` | Page Object for the login screen |
| `Hooks/Hooks.cs` | Per-scenario driver setup/teardown + screenshot on failure |
| `Utils/DriverFactory.cs` | Browser launch (chrome/firefox/edge, headless toggle) |
| `Utils/ConfigReader.cs` | Reads `.env` via DotNetEnv |
| `.env` | `APP_URL`, `BROWSER`, `HEADLESS`, `USER`, `PASSWORD` |

## Configuration

Edit `.env`:

```
APP_URL=https://your-app.example.com
BROWSER=chrome        # chrome | firefox | edge
HEADLESS=false        # true | false
USER=your_user
PASSWORD=your_password
```

The shipped sample is intentionally lenient (it navigates to `APP_URL` and verifies the page
loaded) so the project is green out of the box. Replace the locators in `LoginPage.cs` and the
assertions in `LoginSteps.cs` with real ones, or let the Script Developer wizard generate them.
