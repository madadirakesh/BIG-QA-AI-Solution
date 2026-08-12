# Auto-watcher mode (no code changes to your automation script)

This mode satisfies "automatically start running if any browser launches
with an automation script rather than manually." It runs as a separate
background process that watches for automation-launched browsers and
reports on them without touching your existing scripts.

## 1. Start the watcher

In one terminal:

```bash
python -m webperf_monitor watch --output-dir webperf_reports
```

Leave it running. It polls the local process list every second looking for
Chromium-family browsers launched with automation flags.

## 2. Run your existing automation script, unchanged

In another terminal, run your normal Selenium script exactly as before -
no `SeleniumMonitor` import needed:

```python
from selenium import webdriver

driver = webdriver.Chrome()
driver.get("https://example.com")
driver.quit()
```

## 3. Get your report

When `driver.quit()` runs (or the browser process otherwise exits), the
watcher writes `report.json` and `report.html` into `webperf_reports/`,
named by process id, and prints the performance score to its own terminal.

## Detection limits - please read

- **Selenium**: reliably detected. chromedriver always launches Chrome with
  `--remote-debugging-port` (usually `=0`, meaning "OS picks a free port")
  and `--enable-automation`. The watcher resolves the real port from
  `<user-data-dir>/DevToolsActivePort`, which Chrome writes shortly after
  launch.
- **Playwright (default)**: **not** detected by the watcher. Playwright
  normally talks to Chromium over a debugging *pipe*, not a TCP port, so
  there's nothing for an external process to see. Either:
  - launch Chromium with an explicit port so the watcher can see it:
    ```python
    browser = p.chromium.launch(args=["--remote-debugging-port=9222"])
    ```
  - or just use `PlaywrightMonitor` (the explicit hook) instead, which
    doesn't rely on the watcher at all.
- **Firefox / WebKit**: not supported by either mode - CDP is a
  Chromium-only protocol (the same constraint Lighthouse has).
- Manually-opened browser windows (you double-clicking the Chrome icon)
  won't have these automation flags and are correctly ignored.
