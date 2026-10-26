# Test Execution module

Runs manual test cases from an **Excel workbook**, **Jira** or **Zephyr Scale** in a real browser through
**Playwright MCP**, captures screenshot evidence, writes results back to the source, and shows an
execution dashboard. Sidebar: *Functional Testing → Test Execution* (directly after Test Case Generator).
Available to the QA and Admin roles.

## Setup

| Requirement | Why |
|---|---|
| Node.js 18+ (`npx` on PATH) | Starts the Playwright MCP server (`@playwright/mcp@0.0.83`, pinned). First use downloads the package. |
| Google Chrome | The browser Playwright MCP drives (already required by the app). |
| `pip install -r requirements.txt` | Adds **Pillow** (needed to embed screenshots in Excel). Everything else was already a dependency. |
| A project with a base URL (Configurations) | Base URL and app username/password come from the selected project. A base URL can also be typed per run. |
| Configure AI (optional) | Only used for steps the deterministic parser can't read. Without it those steps are **Blocked**, never guessed. |
| Configure Jira (optional) | Reuses the existing Jira settings (`JIRA_SERVER`, `JIRA_EMAIL`, `JIRA_API_TOKEN`). |
| Zephyr token (optional) | *Test Execution → Zephyr Scale → Configure Zephyr*, or `ZEPHYR_API_TOKEN` in `ScriptGenerator/.env`. |

Environment variables (all optional, see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `TEST_EXECUTION_ALLOWED_HOSTS` | – | Extra hosts the browser may open (comma separated, `*` wildcards), e.g. an SSO host |
| `TEST_EXECUTION_PRODUCTION_HOSTS` | – | Hosts that require ticking "I confirm … production" before a run starts |
| `ZEPHYR_API_TOKEN` / `ZEPHYR_BASE_URL` | – / `https://api.zephyrscale.smartbear.com/v2` | Zephyr Scale Cloud API |
| `PLAYWRIGHT_MCP_PACKAGE` | `@playwright/mcp@0.0.83` | Pinned MCP server package |
| `TEST_EXECUTION_DATA_DIR` | `workspace/test_execution` | Uploads, evidence, result workbooks |
| `TEST_EXECUTION_PUBLIC_BASE_URL` | – | Absolute app URL used in screenshot links inside workbooks |
| `TEST_EXECUTION_NO_SANDBOX` | – | `true` inside containers (set in the Dockerfile) |
| `TEST_EXECUTION_MAX_UPLOAD_MB` / `_MAX_CASES` / `_MAX_ACTIVE_RUNS` / `_MAX_EMBEDDED_IMAGES` | 15 / 500 / 2 / 150 | Limits |

## Workflow

1. **Source** – upload `.xlsx`/`.xls` (pick worksheet, adjust the auto-detected column mapping), search Jira by key or JQL,
   or load Zephyr test cases by test key (`PROJ-T1`), linked Jira issue (`PROJ-123`) or project key.
2. **Review** – every row/issue is shown with validation problems (missing ID, duplicate ID, no steps) and which steps
   the parser understood vs. which need AI. Invalid cases are listed and can't be selected; nothing is dropped silently.
3. **Configure** – project/base URL, allowed hosts, screenshot mode (with evidence estimate), isolation, AI switches,
   publishing, Zephyr test cycle, timeouts, retries, parallel browsers (1–4), CSS selectors to blur in screenshots.
4. **Execute** – live progress and log; cancel stops after the current step.
5. **Dashboard** – totals, pass rate, duration, screenshots, publication counts, cost/efficiency counters; filter/search
   results, expand step details with screenshot previews; retry failed tests, retry failed publications, download the
   updated workbook, export CSV/JSON (JSON includes the audit trail and publication ledger). Recent runs can be reopened.

Supported Excel layouts: one row per test case with numbered steps in one cell (numbered expected results are aligned
per step), and the Test Case Generator's *separate rows* layout (metadata row followed by `Test Step | Step Action |
Step Expected` rows). The header row may be below title rows.

## How execution works

```
source adapter → parse/validate → normalize (TestCase/TestStep) → plan → Playwright MCP → evidence → aggregate → publish → dashboard
```

* **Plan** – `step_parser.py` turns common phrasings into actions from a fixed schema (`navigate, click, type, select,
  check, press, wait, assert_text, assert_url, assert_title, assert_visible, assert_value, …`). Only steps it cannot read go to
  the AI, **one call per test case containing only those steps**; the answer is validated against the schema (anything
  else rejects the step) and cached in `TestExecutionPlanCache` by wording, so it is never asked twice.
* **Execute** – each worker owns one Playwright MCP process (= one isolated browser). Elements described in words
  ("the Login button") are resolved deterministically with `browser_find` against the accessibility snapshot and acted on
  by ref — no LLM, no waiting on wrong selectors. If that fails and AI recovery is on, one call with a trimmed snapshot
  may pick a ref (it must exist in the snapshot). `browser_close` between tests gives fresh storage.
* **Credentials** – the project password is passed to Playwright MCP's `--secrets` file and typed by *name*
  (`TE_APP_PASSWORD`); it never appears in results, logs, Jira comments or AI prompts.

### Status model

| Status | When |
|---|---|
| Passed | every step ran and every derived assertion held |
| Failed | an expected result did not hold, or an element could not be found |
| Blocked | a step could not be interpreted, a URL was not allowed, the test case is invalid, or infrastructure failed after retries |
| Not Executed | cancelled, not reached, or after the first failed/blocked step |
| In Progress | currently running |

**Pass rate = Passed ÷ (Passed + Failed).** Blocked and Not Executed are excluded: they say nothing about the application.
Publication status (Pending/Published/Failed/Skipped/Unsupported) is tracked per destination, separately from test status.

Retries: only infrastructure failures (MCP/browser crash, timeouts) are retried, on a fresh browser, up to the configured
limit — and **never** after the test already clicked/typed/submitted something, to avoid repeating destructive actions.

### Screenshots

| Mode | Captured |
|---|---|
| No screenshots | none |
| After every step | after each executed step (byte-identical consecutive shots are skipped) |
| Final result only (default) | end state of each test, including failed/blocked |
| Failure evidence only | when a step fails or is blocked (not for policy/interpretation blocks, where nothing loaded) |

Files are named `<test>_<step|final>_<status>_<timestamp>_<hash>.png` and stored through the `EvidenceStore` interface
(`evidence.py`; `LocalEvidenceStore` by default — plug an object-storage implementation into `service.build_evidence_store`).
Screenshot bytes are never sent to the AI.

## Where results go

| Source | Destination |
|---|---|
| Excel | A copy of the original workbook: results in the mapped Status/Actual/Evidence columns (or appended *Execution Status, Actual Result, Executed At, Duration, Failure Details, Evidence, Execution Run ID* columns), per-step status on step rows, an **Execution Evidence** sheet (embedded images, or links), and an **Execution Summary** sheet. `.xls` input is written as `.xlsx` (values kept, formatting not). |
| Jira | One comment per test result (status, step table, failure, inline thumbnails, run marker) plus screenshot attachments. Descriptions/fields/status are never changed. |
| Zephyr Scale | A new test execution in the selected test cycle with status, comment and per-step results. |

Duplicate publication is prevented by an idempotency ledger (`TestExecutionPublications`) keyed by the result's
fingerprint, plus (Jira) a run marker checked in existing comments before posting.

## Integrations actually used — and what is not available

* **Playwright MCP** (`@playwright/mcp@0.0.83`, stdio, via the app's `mcp` Python SDK). Tools used:
  `browser_navigate, browser_click, browser_type, browser_select_option, browser_hover, browser_press_key, browser_find,
  browser_evaluate` (fixed, app-authored functions only), `browser_snapshot, browser_take_screenshot, browser_close`.
  Verified against a live server, and covered by a live test.
* **Jira** – this app has **no Jira MCP server**; its Jira integration is the `jira` Python library. The module reuses that
  client (`api.backend._get_jira_client`). A story's own description is **not** treated as an executable test: tests are
  Jira issues of type *Test/Test Case* (steps parsed from a Steps section, numbered list, or `||Step||Expected||` table),
  linked via a "test" link type or as test sub-tasks.
* **Zephyr** – **no Zephyr MCP server is available** in this environment. `ZephyrScaleClient` uses the Zephyr Scale Cloud
  REST API v2 behind the same source/publisher interface, so an MCP-backed client can replace it. Zephyr Squad is not
  supported. This integration does not upload screenshots to Zephyr (no execution-attachment endpoint was verified),
  so screenshots stay in BIG QA and the execution comment and publication message say so — it never claims an upload.
  The endpoints used (`/testcases/{key}`, `/testcases/{key}/teststeps`, `/testcases/{key}/testscript`,
  `/issuelinks/{issueKey}/testcases`, `/testcases`, `/testcycles`, `POST /testexecutions`) follow the public v2 API
  documentation but have **not been exercised against a live Zephyr instance** from this environment.

## Security

* Login and CSRF from the app; QA/Admin role check on every route.
* Navigation allow-list (base URL host + allowed hosts), only `http(s)`; cloud metadata / link-local always blocked
  (also passed to Playwright MCP `--blocked-origins`); the landing URL after every navigation/click is re-checked to catch redirects.
* Production hosts require explicit confirmation.
* Uploads: extension + magic-number check, size limit, sanitized names, upload/run IDs validated, all paths joined with traversal checks.
* Step text, Excel cells and Jira/Zephyr content are untrusted: they can only become schema actions, never code or shell
  commands; they are HTML-escaped in the UI. The `.env` writer rejects quotes/newlines.
* Secrets are redacted from results, logs and AI prompts; screenshot masking via configured CSS selectors.
* Audit trail per run (`TestExecutionAudit`): start, cancel, retries, each publication, completion.
* Runs active when the app stops are marked *interrupted*; completed test results are kept and can be retried.

## Tests

```
cd BIG_QA_Solution/ScriptGenerator
../.venv/Scripts/python -m pytest tests -q        # Windows; use ../.venv/bin/python on Linux/macOS
TE_SKIP_LIVE=1 ...                                 # skip the live Playwright MCP test
```

* `test_te_excel.py`, `test_te_parser_security.py`, `test_te_engine.py`, `test_te_service.py`, `test_te_routes.py` –
  use a mocked Playwright MCP session (`conftest.FakeMCPSession`) and a temporary database.
* `test_te_integrations.py` – Jira and Zephyr against **mocked** clients. There is no live Jira/Zephyr test: no test
  instance is configured here. Validate against a non-production Jira/Zephyr project before relying on publication.
* `test_te_live_playwright_mcp.py` – **live**: real Playwright MCP + headless Chrome against a local demo page.

## Troubleshooting

| Symptom | Fix |
|---|---|
| All tests Blocked: "Playwright MCP unavailable … npx" | Install Node.js 18+ and make sure `npx` is on the PATH of the process running the app. |
| First run is slow | `npx` downloads `@playwright/mcp` once; later runs reuse the cache. |
| "Navigation to X is not allowed" | Add the host to *Additional allowed hosts* or `TEST_EXECUTION_ALLOWED_HOSTS`. |
| Steps Blocked "Could not interpret" | Rephrase (e.g. `Click on 'Save' button`, `Enter 'x' in Email field`, `'Saved' is displayed`) or enable AI. |
| "needs the application password" | Set username/password on the project in Configurations. |
| Element not found | Increase the element timeout, or name the element as it appears on screen; a CSS selector (`#id`) also works. |
| Zephyr not published | Pick a test cycle; check the token and EU/US base URL. |
| Chrome fails in Docker | `TEST_EXECUTION_NO_SANDBOX=true` (set in the Dockerfile). |
