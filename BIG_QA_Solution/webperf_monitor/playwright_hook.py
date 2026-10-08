"""
Explicit Playwright integration.

Usage (sync API):
    from playwright.sync_api import sync_playwright
    from webperf_monitor import PlaywrightMonitor

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        monitor = PlaywrightMonitor(page, output_dir="reports")
        monitor.start()

        page.goto("https://example.com")
        ...
        browser.close()   # report.json / report.html are written automatically here

Only Chromium (browser.chromium) is supported, since Firefox and WebKit
don't expose the Chrome DevTools Protocol. This mirrors Lighthouse's own
Chromium-only constraint.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from .collector import PerformanceSession
from .report import write_reports


class _PlaywrightCDPAdapter:
    """
    Wraps a Playwright CDPSession so it satisfies the same tiny interface
    PerformanceSession expects from our own CDPClient:
    send / on / on_close / wait_closed / is_closed
    """

    def __init__(self, page):
        self._page = page
        context = page.context
        self._cdp = context.new_cdp_session(page)
        self._closed = threading.Event()
        self._close_handlers: list[Callable[[], None]] = []

        page.on("close", lambda *_: self._trigger_close())
        try:
            context.browser.on("disconnected", lambda *_: self._trigger_close())
        except Exception:
            pass  # some Playwright versions/contexts may not expose .browser

    def _trigger_close(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
            for handler in list(self._close_handlers):
                try:
                    handler()
                except Exception:
                    pass

    def send(self, method: str, params: Optional[dict] = None, timeout: float = None):
        if self._closed.is_set():
            raise RuntimeError(f"Cannot send '{method}': page/browser already closed")
        return self._cdp.send(method, params or {})

    def on(self, event_name: str, callback: Callable[[dict], None]) -> None:
        self._cdp.on(event_name, callback)

    def on_close(self, callback: Callable[[], None]) -> None:
        if self._closed.is_set():
            callback()
        else:
            self._close_handlers.append(callback)

    def wait_closed(self, timeout: Optional[float] = None) -> bool:
        return self._closed.wait(timeout)

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()


class PlaywrightMonitor:
    def __init__(self, page, output_dir: str = "webperf_reports",
                 label: Optional[str] = None, on_report: Optional[Callable[[dict], None]] = None):
        self.page = page
        self.output_dir = output_dir
        self.label = label or "playwright-session"
        self.on_report = on_report
        self.session: Optional[PerformanceSession] = None
        self._report_paths: Optional[dict] = None

    def start(self) -> "PlaywrightMonitor":
        adapter = _PlaywrightCDPAdapter(self.page)
        self.session = PerformanceSession(adapter, label=self.label)
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
        if it happens to be the last one open. With Playwright's sync API this
        must be called from the same thread that drives the page.
        """
        if self.session is None:
            raise RuntimeError("start() was not called")
        self.session.snapshot()

    def wait_for_report(self, timeout: Optional[float] = None) -> dict:
        """Block until the page/browser closes and the report has been written."""
        if self.session is None:
            raise RuntimeError("start() was not called")
        self.session.client.wait_closed(timeout)
        import time
        deadline = time.time() + 5
        while self._report_paths is None and time.time() < deadline:
            time.sleep(0.05)
        return self._report_paths or {}

    def stop_and_report(self) -> dict:
        """Finalize and write reports immediately, without waiting for the page to close."""
        if self.session is None:
            raise RuntimeError("start() was not called")
        result = self.session.stop_and_finalize()
        self._report_paths = write_reports(result, self.output_dir)
        return self._report_paths
