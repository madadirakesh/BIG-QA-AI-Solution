# Selenium + Python + Behave BDD Template

A BDD test project: **Behave** scenarios driving **Selenium 4** with a Page Object Model layout.
Drivers are resolved automatically by WebDriverManager — nothing to download by hand.

## Prerequisites

- **Python 3.12+** (`python --version`)
- A local browser (Chrome by default)
- Internet on first `pip install` (and first run, for the WebDriver binaries). Behind a corporate
  proxy, see `../../SETUP-PROXY.md`.

## Quick start

```bash
python -m venv venv
venv\Scripts\activate           # Windows  (source venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
behave                          # run all scenarios
behave --tags=@smoke            # run only @smoke
```

Reports are written under `Results/` (see `behave.ini`).

## Layout

| Path | Purpose |
|------|---------|
| `features/*.feature` | Gherkin scenarios |
| `features/steps/` | Step definitions |
| `features/environment.py` | Per-scenario driver setup/teardown + screenshot on failure |
| `pages/` | Page Objects (`base_page`, `login_page`, `home_page`) |
| `utils/config_reader.py` | Reads `.env` |
| `behave.ini`, `requirements.txt` | Config / dependencies |

## Configuration

Edit `.env`: `APP_URL`, `BROWSER` (chrome\|firefox\|edge), `HEADLESS` (true\|false), `USER`,
`PASSWORD`. The shipped login sample is lenient so `behave` is green out of the box — replace the
locators in `pages/login_page.py` with real ones.
