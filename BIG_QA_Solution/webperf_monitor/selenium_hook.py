"""
Explicit Selenium integration.

Usage:
    from selenium import webdriver
    from webperf_monitor import SeleniumMonitor

    driver = webdriver.Chrome()
    monitor = SeleniumMonitor(driver, output_dir="reports")
    monitor.start()

    driver.get("https://example.com")
    ...
    driver.quit()   # report.json / report.html are written automatically here

Only Chromium-family browsers (Chrome, Edge, Chromium) are supported, since
they're the ones that expose a CDP debugger address - the same constraint
Lighthouse itself has.
"""

from __future__ import annotations

from typing import Callable, Optional

from .cdp_client import CDPClient, find_page_ws_url
from .collector import PerformanceSession
from .report import write_reports


class SeleniumMonitor:
    def __init__(self, driver, output_dir: str = "webperf_reports",
                 label: Optional[str] = None, on_report: Optional[Callable[[dict], None]] = None):
        self.driver = driver
        self.output_dir = output_dir
        self.label = label or "selenium-session"
        self.on_report = on_report
        self.session: Optional[PerformanceSession] = None
        self._report_paths: Optional[dict] = None

    def _debugger_address(self) -> str:
        caps = self.driver.capabilities
        chrome_opts = caps.get("goog:chromeOptions") or caps.get("ms:edgeOptions") or {}
        address = chrome_opts.get("debuggerAddress")
        if not address:
            raise RuntimeError(
                "Could not find a CDP debuggerAddress on this driver. "
                "SeleniumMonitor only supports Chromium-family browsers "
                "(Chrome, Edge, Chromium) - the same limitation Lighthouse has."
            )
        return address

    def start(self) -> "SeleniumMonitor":
        address = self._debugger_address()
        ws_url = find_page_ws_url(address, url_hint=None)
        client = CDPClient(ws_url)
        self.session = PerformanceSession(client, label=self.label)
        self.session.on_finalized(self._handle_finalized)
        self.session.start()
        return self

    def _handle_finalized(self, result: dict) -> None:
        self._report_paths = write_reports(result, self.output_dir)
        if self.on_report:
            self.on_report(result)

    def capture_page(self) -> None:
        """
        Record the metrics of the page that is open RIGHT NOW as its own page
        load, so it gets its own entry in the report's per-page section (repeated
        loads of the same URL are averaged there).

        Call this just before navigating away - unlike the background watcher,
        this hook doesn't poll, so a page that is never captured is only measured
        if it happens to be the last one open. Must be called from the thread
        that drives the browser.
        """
        if self.session is None:
            raise RuntimeError("start() was not called")
        self.session.snapshot()

    def wait_for_report(self, timeout: Optional[float] = None) -> dict:
        """
        Block until the browser closes and the report has been written.
        Call this after driver.quit() if you didn't pass on_report.
        """
        if self.session is None:
            raise RuntimeError("start() was not called")
        self.session.client.wait_closed(timeout)
        # give the finalize callback a brief moment to finish writing files
        import time
        deadline = time.time() + 5
        while self._report_paths is None and time.time() < deadline:
            time.sleep(0.05)
        return self._report_paths or {}

    def stop_and_report(self) -> dict:
        """Finalize and write reports immediately, without waiting for the browser to close."""
        if self.session is None:
            raise RuntimeError("start() was not called")
        result = self.session.stop_and_finalize()
        self._report_paths = write_reports(result, self.output_dir)
        return self._report_paths
