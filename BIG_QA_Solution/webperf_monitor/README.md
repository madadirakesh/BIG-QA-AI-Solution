# webperf-monitor

A Lighthouse-style web performance monitor, in pure Python, for Selenium
and Playwright automation. It talks directly to Chrome DevTools Protocol
(CDP) - the same protocol Lighthouse itself is built on - so it works
standalone, without Node.js or the Lighthouse CLI.

It supports two modes:

1. **Explicit hook** (recommended): wrap your existing `webdriver` / `page`
   object in one line.
2. **Auto-watcher**: a background process that detects any Chromium
   browser launched by automation tooling on the machine and monitors it
   automatically, with zero changes to your automation script. See
   [`examples/watcher_usage.md`](examples/watcher_usage.md) for exactly
   how detection works and its limits.

In both modes, `report.json` and `report.html` are generated automatically
once the browser (or tab) closes.

## Install

```bash
pip install -e .
# plus whichever framework(s) you use:
pip install -e ".[selenium]"
pip install -e ".[playwright]"
```

(Or just `pip install websocket-client requests psutil` directly if you'd
rather not install this as a package.)

## Quick start - Selenium

```python
from selenium import webdriver
from webperf_monitor import SeleniumMonitor

driver = webdriver.Chrome()
monitor = SeleniumMonitor(driver, output_dir="webperf_reports")
monitor.start()

driver.get("https://example.com")
driver.quit()

paths = monitor.wait_for_report(timeout=15)
print(paths)  # {'json': 'webperf_reports/report.json', 'html': 'webperf_reports/report.html'}
```

## Quick start - Playwright

```python
from playwright.sync_api import sync_playwright
from webperf_monitor import PlaywrightMonitor

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()

    monitor = PlaywrightMonitor(page, output_dir="webperf_reports")
    monitor.start()

    page.goto("https://example.com")
    browser.close()

    paths = monitor.wait_for_report(timeout=15)
```

## Quick start - auto-watcher (no script changes)

The watcher is an **independent, standalone process** - it does not need to
be placed inside your test automation framework's codebase or imported by
your test code. It just needs to run as a separate process on the **same
machine** as your browser automation (it inspects local OS processes and
connects to `localhost` debugging ports), for the duration your tests run.

```bash
# start it (e.g. in your CI job's "before tests" step)
webperf-monitor watch --output-dir webperf_reports --pid-file /tmp/webperf.pid --verbose &

# run your existing Selenium/Playwright suite, completely unchanged
pytest tests/

# stop it (e.g. in your "after tests" / teardown step) - this waits for
# any browser sessions still open at that moment to finish and write
# their reports before it exits
webperf-monitor stop --pid-file /tmp/webperf.pid
```

An external `kill <pid>` (SIGTERM) triggers the same graceful shutdown as
`webperf-monitor stop`. Avoid `kill -9`, which skips it. `--verbose` is
recommended the first time you wire this up - it prints exactly which
processes were detected, what port they were attached on, and why a
session was skipped if one was, which is the fastest way to debug a
"report never showed up" situation.

See [`examples/watcher_usage.md`](examples/watcher_usage.md) for the
detection heuristic and its limits (short version: Selenium is always
detected; Playwright needs an explicit `--remote-debugging-port`).

## What's in the report

| Category | Details |
|---|---|
| Performance score | 0-100, weighted log-normal score across FCP/LCP/TBT/CLS, using Lighthouse's own scoring curve constants |
| Core Web Vitals | First Contentful Paint, Largest Contentful Paint, Cumulative Layout Shift (captured via native `PerformanceObserver`, same primitives Lighthouse uses) |
| Total Blocking Time | Approximated from the Long Tasks API |
| Console errors/warnings | `console.error`/`console.warn` calls, uncaught exceptions, and browser-level log entries, with source file + line number |
| Failed network requests | Any response with status >= 400, plus requests that failed outright (DNS, connection reset, etc.) |
| Resource breakdown | Request count and transferred bytes, grouped by type (script, image, stylesheet, font, XHR, ...) |

## Honest limitations

- **Speed Index is not measured.** Real Speed Index requires frame-by-frame
  video analysis of the page render, which needs full trace capture and a
  headless rendering pipeline - out of scope for a lightweight CDP-only
  tool. It's reported as `null` rather than faked, and excluded from the
  performance score (weights are renormalized across the remaining four
  metrics).
- **TBT is an approximation**, summed over the whole page load rather than
  strictly the FCP-to-Time-to-Interactive window Lighthouse uses.
- **Chromium-only.** Chrome, Edge, and Chromium are supported. Firefox and
  WebKit don't expose CDP - the same constraint real Lighthouse has.
- **The auto-watcher is a heuristic**, not a guarantee - see
  [`examples/watcher_usage.md`](examples/watcher_usage.md).
- Scores use Lighthouse's *desktop/general* control-point constants as a
  reasonable general-purpose default; they aren't pulled live from
  Lighthouse and may drift from future Lighthouse releases.

## Package layout

```
webperf_monitor/
  cdp_client.py       # low-level CDP websocket client
  injected_js.py       # PerformanceObserver script injected into pages
  scoring.py           # Lighthouse-style log-normal scoring
  collector.py         # PerformanceSession - event capture & finalization
  report.py             # JSON + HTML report generation
  selenium_hook.py     # SeleniumMonitor
  playwright_hook.py   # PlaywrightMonitor
  watcher.py            # auto-detect background daemon
  cli.py                 # `python -m webperf_monitor watch`
examples/
  selenium_example.py
  playwright_example.py
  watcher_usage.md
```

## Integrating with your own solution

Every entry point (`SeleniumMonitor`, `PlaywrightMonitor`, `Watcher`)
accepts an `on_report(result: dict)` callback fired the moment a report is
finalized, so you can push metrics into your own system (a database, CI
pipeline, alerting, dashboards, etc.) instead of - or in addition to -
reading the JSON file:

```python
def on_report(result):
    my_system.record_metric("performance_score", result["performance_score"])

monitor = SeleniumMonitor(driver, on_report=on_report)
```
