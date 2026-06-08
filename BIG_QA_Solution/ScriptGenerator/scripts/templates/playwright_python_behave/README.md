# Playwright + Python + Behave BDD Template

A BDD test project: **Behave** scenarios driving **Playwright** (sync API) with a Page Object
Model layout that mirrors the Selenium/Python template — same folders, same step wording.

## Prerequisites

- **Python 3.12+** (`python --version`)
- Internet on first `pip install` and first `playwright install` (browser binaries). Behind a
  corporate proxy, see `../../SETUP-PROXY.md`.

## Quick start

```bash
python -m venv venv
venv\Scripts\activate           # Windows  (source venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
python -m playwright install    # one-time browser download
behave                          # run all scenarios
behave --tags=@smoke            # run only @smoke
```

Reports are written under `Results/` (see `behave.ini`).

## Layout

| Path | Purpose |
|------|---------|
| `features/*.feature` | Gherkin scenarios |
| `features/steps/` | Step definitions |
| `features/environment.py` | Per-scenario browser context setup/teardown + screenshot on failure |
| `pages/` | Page Objects (`base_page`, `login_page`, `home_page`) |
| `utils/config_reader.py` | Reads `.env` |
| `behave.ini`, `requirements.txt` | Config / dependencies |

## Configuration

Edit `.env`: `APP_URL`, `BROWSER` (chromium\|firefox\|webkit), `HEADLESS` (true\|false), `USER`,
`PASSWORD`. The shipped login sample is lenient so `behave` is green out of the box — replace the
locators in `pages/login_page.py` with real ones.
