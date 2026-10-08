"""
PerformanceSession - attaches to a single browser tab over CDP, records
console/network/paint/layout-shift/long-task activity for the lifetime of
that tab, and produces a Lighthouse-style metrics dict once told to
finalize (normally triggered automatically when the tab/browser closes).
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from typing import Callable, Optional

from .cdp_client import CDPClient, find_page_ws_url, CDPError
from .injected_js import (
    COLLECTOR_SCRIPT, COLLECT_RESULTS_EXPR, COLLECT_NAV_TIMING_EXPR,
    COLLECT_DIAGNOSTICS_EXPR,
)
from .scoring import score_metric, rating_for_score, compute_performance_score
from .diagnostics import (
    build_opportunities, build_diagnostic_items, compute_speed_index, analyze_minification,
)

RESOURCE_TYPES = (
    "Document", "Stylesheet", "Script", "Image", "Font",
    "XHR", "Fetch", "Media", "WebSocket", "Manifest", "Other",
)

# Filmstrip capture limits (keep memory / report size bounded).
_MAX_FILMSTRIP_FRAMES = 60
_FILMSTRIP_WINDOW_S = 8.0
# Minification sampling limits (best-effort, capped to stay cheap).
_MAX_MINIFY_SAMPLES = 15
_MAX_MINIFY_BODY_BYTES = 700_000
# Per-page-load Speed Index: frames stashed per load (down-sampled to keep the
# in-memory cost bounded across a long test) and the number of loads we bother
# keeping frames for at all.
_MAX_RUN_SI_FRAMES = 20
_MAX_RUNS_WITH_FRAMES = 40
# Browser-internal pages are never the page under test (Chrome opens about:blank
# on startup, chromedriver navigates to data:, etc.), so they are not measured.
_INTERNAL_URL_PREFIXES = (
    "about:", "chrome:", "chrome-error:", "chrome-extension:", "chrome-search:",
    "chrome-untrusted:", "devtools:", "edge:", "view-source:",
)


def _downsample(frames: list, max_frames: int) -> list:
    """Evenly-spaced subset of `frames`, at most `max_frames` long."""
    if not frames:
        return []
    if len(frames) <= max_frames or max_frames < 2:
        return list(frames)
    step = (len(frames) - 1) / (max_frames - 1)
    idxs = sorted({round(i * step) for i in range(max_frames)})
    return [frames[i] for i in idxs]


class PerformanceSession:
    """
    `client` must implement: send(method, params=None), on(event, callback),
    on_close(callback), wait_closed(timeout). This is either a raw
    `CDPClient` (Selenium / watcher) or an adapter around Playwright's own
    CDP session (see playwright_hook.py) - both satisfy the same protocol.
    """

    def __init__(self, client, label: Optional[str] = None):
        self.label = label or "session"
        self.client = client

        self.console_messages: list[dict] = []
        self.failed_requests: list[dict] = []
        self.resource_summary: dict[str, dict] = defaultdict(lambda: {"count": 0, "bytes": 0})
        self._request_meta: dict[str, dict] = {}
        self._request_encoded_bytes: dict[str, int] = {}

        self.navigations: list[dict] = []
        self._start_time = None
        self._finalized = False
        self._finalize_lock = threading.Lock()
        self.result: Optional[dict] = None
        self._on_finalized_callbacks: list[Callable[[dict], None]] = []

        # Latest metrics snapshot captured while the browser was still alive.
        # These let us produce a full report even if the browser has already
        # exited by the time we finalize (see snapshot() / _finalize()).
        self._last_vitals: dict = {}
        self._last_perf: dict = {}
        self._finalize_on_disconnect = True

        # Load time of each distinct URL visited during the session, keyed by
        # URL. Populated from the Navigation Timing API on each snapshot and at
        # finalize (latest non-null reading wins).
        self.page_load_times: dict[str, dict] = {}

        # Root-cause diagnostics + category audits (latest good read).
        self._last_diagnostics: dict = {}
        # Per-resource network metadata used to build opportunities, keyed by
        # url: {transfer_size, mime, content_encoding, type, unminified, minify_savings}.
        self.resource_details: dict[str, dict] = {}
        self._minify_samples = 0
        self._minify_checked: set = set()

        # Filmstrip screencast frames + final screenshot (visual analysis).
        self.filmstrip: list[dict] = []
        self._filmstrip_lock = threading.Lock()
        self._filmstrip_nav_start: Optional[float] = None
        self._screencast_on = False
        self.final_screenshot: Optional[str] = None

        # One entry per PAGE LOAD seen during the session (not just the last
        # one), so the report can average repeated loads of the same URL instead
        # of showing whatever page happened to be open when we finalized.
        # Each entry: {epoch, url, vitals, diagnostics, nav timings, frames}.
        self.page_runs: list[dict] = []
        self._runs_lock = threading.Lock()
        self._current_run: Optional[dict] = None
        self._nav_epoch = 0
        self._runs_with_frames = 0

    # ------------------------------------------------------------------ #
    def start(self, finalize_on_disconnect: bool = True) -> "PerformanceSession":
        """
        Begin collecting. By default the session finalizes automatically when
        the browser/tab closes (used by the Selenium/Playwright hooks). Pass
        finalize_on_disconnect=False to keep the accumulated data alive across
        tab/browser close events and finalize explicitly instead - this is
        what the watcher uses to keep monitoring until the whole test ends.
        """
        self._finalize_on_disconnect = finalize_on_disconnect
        self._subscribe(self.client)
        self._start_time = time.time()
        return self

    def _subscribe(self, c) -> None:
        """Enable the required CDP domains on `c` and wire up event handlers.
        Safe to call again on a fresh client (see rebind())."""
        c.send("Page.enable")
        c.send("Network.enable")
        c.send("Log.enable")
        c.send("Runtime.enable")
        c.send("Performance.enable")
        c.send("Page.addScriptToEvaluateOnNewDocument", {"source": COLLECTOR_SCRIPT})

        c.on("Log.entryAdded", self._on_log_entry)
        c.on("Runtime.consoleAPICalled", self._on_console_api)
        c.on("Runtime.exceptionThrown", self._on_exception)
        c.on("Network.responseReceived", self._on_response_received)
        c.on("Network.loadingFinished", self._on_loading_finished)
        c.on("Network.loadingFailed", self._on_loading_failed)
        c.on("Page.frameNavigated", self._on_frame_navigated)

        # Filmstrip: capture screencast frames for the visual loading sequence.
        # The ack MUST be fire-and-forget (send_async) because it happens inside
        # the websocket read thread; a blocking send would dead-lock. Only the
        # raw CDPClient exposes send_async, so the filmstrip is captured for the
        # watcher/Selenium path; the Playwright hook simply skips it.
        if hasattr(c, "send_async"):
            c.on("Page.screencastFrame", self._on_screencast_frame)
            try:
                c.send("Page.startScreencast", {
                    "format": "jpeg", "quality": 55,
                    "maxWidth": 720, "maxHeight": 720, "everyNthFrame": 1,
                })
                self._screencast_on = True
                if self._filmstrip_nav_start is None:
                    self._filmstrip_nav_start = time.time()
            except (CDPError, TimeoutError):
                pass

        if self._finalize_on_disconnect:
            c.on_close(self._handle_disconnect)

    def rebind(self, client) -> None:
        """
        Point this session at a NEW CDP client (e.g. after the previously
        attached tab closed but the browser/test is still running) without
        discarding already-accumulated console/network/navigation data.
        """
        self.client = client
        self._subscribe(client)

    def snapshot(self) -> None:
        """
        Capture the current vitals / performance metrics from the live page.
        Called periodically by the watcher so that if the browser exits
        abruptly at the end of the test we can still report the last values we
        saw. Keeps the most recent non-empty read.

        The read is also filed against the page load it came from (see
        _record_page_run) so per-page averages can be computed later.
        """
        epoch = self._nav_epoch
        vitals = self._collect_vitals()
        if vitals:
            self._last_vitals = vitals
        perf = self._collect_performance_metrics()
        if perf:
            self._last_perf = perf
        nav = self._collect_nav_timing()
        self._record_nav_timing(nav)
        diag = self._collect_diagnostics()
        if diag:
            self._last_diagnostics = diag
        # Only file the reading if no navigation happened while we were reading
        # it - otherwise the values could straddle two different pages.
        if epoch == self._nav_epoch:
            self._record_page_run(epoch, nav, vitals, diag)
        self._sample_minification()

    def _record_nav_timing(self, nav: Optional[dict] = None) -> None:
        """Read the current document's load timing and remember it per-URL."""
        if nav is None:
            nav = self._collect_nav_timing()
        url = nav.get("url") if nav else None
        if not url:
            return
        existing = self.page_load_times.get(url, {})
        # Keep the most complete reading: a later snapshot may finally have a
        # non-null load_time once the load event has fired.
        merged = {**existing, **{k: v for k, v in nav.items() if v is not None}}
        self.page_load_times[url] = merged

    # ------------------------------------------------------------------ #
    # Per-page-load tracking
    # ------------------------------------------------------------------ #
    def _record_page_run(self, epoch: int, nav: Optional[dict],
                         vitals: dict, diagnostics: dict) -> None:
        """
        File a metrics reading against the page load it belongs to.

        A new run starts whenever the navigation epoch changes (a real
        navigation / reload) or the URL changes within the same epoch (an
        in-document SPA route change). Within one run the latest non-empty
        reading wins, because the vitals of a load only get more complete over
        time (LCP settles, CLS accumulates, the load event fires).
        """
        url = (nav or {}).get("url")
        if not url or url.lower().startswith(_INTERNAL_URL_PREFIXES):
            return
        with self._runs_lock:
            run = self._current_run
            if run is None or run.get("epoch") != epoch or run.get("url") != url:
                run = {
                    "epoch": epoch,
                    "url": url,
                    # Only the first page of an epoch owns that epoch's
                    # filmstrip, so only it can get a visual Speed Index.
                    "owns_filmstrip": run is None or run.get("epoch") != epoch,
                    "started_at": time.time(),
                }
                self.page_runs.append(run)
                self._current_run = run
            if vitals:
                run["vitals"] = vitals
            if diagnostics:
                run["diagnostics"] = diagnostics
            for key in ("load_time_ms", "dom_content_loaded_ms", "response_end_ms"):
                value = (nav or {}).get(key)
                if value is not None:
                    run[key] = value

    def _close_current_run(self, frames: list) -> None:
        """
        Called when the page being measured is navigated away from (or at
        finalize): hand it the filmstrip frames captured while it was loading so
        its own Speed Index can be computed, and stop accumulating into it.
        """
        with self._runs_lock:
            run = self._current_run
            self._current_run = None
            if run is None or not frames or not run.get("owns_filmstrip"):
                return
            if self._runs_with_frames >= _MAX_RUNS_WITH_FRAMES:
                return
            self._runs_with_frames += 1
            run["frames"] = _downsample(frames, _MAX_RUN_SI_FRAMES)

    def _build_page_result(self, run: dict) -> Optional[dict]:
        """Turn one recorded page load into a scored, report-shaped dict."""
        vitals = run.get("vitals") or {}
        diagnostics = run.get("diagnostics") or {}
        if not vitals and not diagnostics and run.get("load_time_ms") is None:
            return None  # nothing was ever read for this load

        fcp = vitals.get("fcp")
        lcp = vitals.get("lcp")
        cls = vitals.get("cls")
        tbt = self._compute_tbt(vitals.get("longTasks", []))
        inp = vitals.get("inp") or diagnostics.get("inp_ms")
        speed_index = compute_speed_index(run.get("frames") or [])

        metric_scores = {
            "fcp": score_metric("fcp", fcp),
            "si": score_metric("si", speed_index),
            "lcp": score_metric("lcp", lcp),
            "tbt": score_metric("tbt", tbt),
            "cls": score_metric("cls", cls),
            "inp": score_metric("inp", inp),
        }
        return {
            "url": run.get("url"),
            "session": self.label,
            "performance_score": compute_performance_score(metric_scores),
            "metrics": {
                "first_contentful_paint_ms": fcp,
                "largest_contentful_paint_ms": lcp,
                "total_blocking_time_ms": tbt,
                "cumulative_layout_shift": cls,
                "speed_index_ms": speed_index,
                "interaction_to_next_paint_ms": inp,
            },
            "metric_scores": {
                k: {"score": v, "rating": rating_for_score(v)}
                for k, v in metric_scores.items()
            },
            "categories": self._build_category_scores(diagnostics),
            "ttfb_ms": diagnostics.get("ttfb_ms"),
            "dom_nodes": (diagnostics.get("dom") or {}).get("nodes"),
            "render_blocking_count": len(diagnostics.get("render_blocking") or []),
            "load_time_ms": run.get("load_time_ms"),
            "dom_content_loaded_ms": run.get("dom_content_loaded_ms"),
            "response_end_ms": run.get("response_end_ms"),
        }

    def _build_page_results(self) -> list:
        with self._runs_lock:
            runs = list(self.page_runs)
        results = [self._build_page_result(r) for r in runs]
        return [r for r in results if r]

    def on_finalized(self, callback: Callable[[dict], None]) -> None:
        """Register a callback invoked with the report dict once finalized."""
        if self._finalized and self.result is not None:
            callback(self.result)
        else:
            self._on_finalized_callbacks.append(callback)

    # ------------------------------------------------------------------ #
    # Event handlers
    # ------------------------------------------------------------------ #
    def _on_log_entry(self, params: dict) -> None:
        entry = params.get("entry", {})
        if entry.get("level") in ("error", "warning"):
            self.console_messages.append({
                "level": entry.get("level"),
                "source": entry.get("source"),
                "text": entry.get("text"),
                "url": entry.get("url"),
                "line": entry.get("lineNumber"),
                "timestamp": entry.get("timestamp"),
            })

    def _on_console_api(self, params: dict) -> None:
        if params.get("type") not in ("error", "warning"):
            return
        args = params.get("args", [])
        text = " ".join(
            str(a.get("value", a.get("description", "")))
            for a in args
        )
        frame = (params.get("stackTrace", {}).get("callFrames") or [{}])[0]
        self.console_messages.append({
            "level": "error" if params["type"] == "error" else "warning",
            "source": "console-api",
            "text": text,
            "url": frame.get("url"),
            "line": frame.get("lineNumber"),
            "timestamp": params.get("timestamp"),
        })

    def _on_exception(self, params: dict) -> None:
        details = params.get("exceptionDetails", {})
        self.console_messages.append({
            "level": "error",
            "source": "uncaught-exception",
            "text": details.get("text") or (details.get("exception") or {}).get("description"),
            "url": details.get("url"),
            "line": details.get("lineNumber"),
            "timestamp": params.get("timestamp"),
        })

    def _on_response_received(self, params: dict) -> None:
        resp = params.get("response", {})
        req_id = params.get("requestId")
        headers = {k.lower(): v for k, v in (resp.get("headers") or {}).items()}
        self._request_meta[req_id] = {
            "url": resp.get("url"),
            "status": resp.get("status"),
            "mimeType": resp.get("mimeType"),
            "type": params.get("type", "Other"),
            "content_encoding": headers.get("content-encoding"),
        }
        if resp.get("status", 0) >= 400:
            self.failed_requests.append({
                "url": resp.get("url"),
                "status": resp.get("status"),
                "type": params.get("type", "Other"),
            })

    def _on_loading_finished(self, params: dict) -> None:
        req_id = params.get("requestId")
        size = params.get("encodedDataLength", 0)
        meta = self._request_meta.get(req_id, {})
        rtype = meta.get("type", "Other")
        if rtype not in RESOURCE_TYPES:
            rtype = "Other"
        self.resource_summary[rtype]["count"] += 1
        self.resource_summary[rtype]["bytes"] += size

        # Track per-resource details for the opportunities analysis.
        url = meta.get("url")
        if url:
            detail = self.resource_details.get(url, {})
            detail.update({
                "url": url,
                "type": meta.get("type", "Other"),
                "mime": meta.get("mimeType"),
                "content_encoding": meta.get("content_encoding"),
                "transfer_size": (detail.get("transfer_size") or 0) + size,
                "request_id": req_id,
            })
            self.resource_details[url] = detail

    def _on_loading_failed(self, params: dict) -> None:
        req_id = params.get("requestId")
        meta = self._request_meta.get(req_id, {})
        self.failed_requests.append({
            "url": meta.get("url"),
            "status": "failed",
            "error": params.get("errorText"),
            "type": params.get("type", "Other"),
        })

    def _on_frame_navigated(self, params: dict) -> None:
        frame = params.get("frame", {})
        if frame.get("parentId") is None:  # top-level navigation only
            self.navigations.append({"url": frame.get("url"), "time": time.time()})
            # Start a fresh filmstrip for the newly-loading page so the frames
            # captured line up with the metrics for that navigation. The frames
            # of the page we're leaving go to its own page-load record, so that
            # load keeps its own Speed Index.
            with self._filmstrip_lock:
                previous_frames = self.filmstrip
                self.filmstrip = []
                self._filmstrip_nav_start = time.time()
            self._close_current_run(previous_frames)
            self._nav_epoch += 1

    def _on_screencast_frame(self, params: dict) -> None:
        # Runs on the websocket read thread: ack with send_async (never a
        # blocking send, which would dead-lock this thread).
        sid = params.get("sessionId")
        if sid is not None:
            try:
                self.client.send_async("Page.screencastFrameAck", {"sessionId": sid})
            except Exception:
                pass
        start = self._filmstrip_nav_start or time.time()
        within_window = (time.time() - start) <= _FILMSTRIP_WINDOW_S
        if len(self.filmstrip) < _MAX_FILMSTRIP_FRAMES and within_window and params.get("data"):
            meta = params.get("metadata", {})
            self.filmstrip.append({
                "data": params["data"],
                "ts": time.time(),
                "offset_ms": max(0, round((time.time() - start) * 1000)),
            })

    # ------------------------------------------------------------------ #
    # Finalization
    # ------------------------------------------------------------------ #
    def _handle_disconnect(self) -> None:
        self._finalize()

    def stop_and_finalize(self) -> dict:
        """Manually finalize while the browser tab is still open."""
        return self._finalize()

    def _finalize(self) -> dict:
        with self._finalize_lock:
            if self._finalized:
                return self.result  # type: ignore[return-value]
            self._finalized = True

            # Capture the final visuals while the page is (hopefully) still up.
            self._capture_screenshot()
            self._stop_screencast()

            # Prefer a fresh read from the live page; fall back to the last
            # snapshot captured while the browser was still alive (the browser
            # may already have closed by the time the test ends).
            vitals = self._collect_vitals() or self._last_vitals
            perf_metrics = self._collect_performance_metrics() or self._last_perf
            final_nav = self._collect_nav_timing()
            self._record_nav_timing(final_nav)  # capture the final URL's load time too
            diagnostics = self._collect_diagnostics() or self._last_diagnostics

            # File the final reading against the page that is still open, then
            # close it out so it owns the frames captured for it.
            self._record_page_run(self._nav_epoch, final_nav, vitals, diagnostics)
            with self._filmstrip_lock:
                trailing_frames = list(self.filmstrip)
            self._close_current_run(trailing_frames)
            page_results = self._build_page_results()

            fcp = vitals.get("fcp")
            lcp = vitals.get("lcp")
            cls = vitals.get("cls")
            tbt = self._compute_tbt(vitals.get("longTasks", []))
            inp = vitals.get("inp") or diagnostics.get("inp_ms")
            speed_index = compute_speed_index(self.filmstrip)

            metric_scores = {
                "fcp": score_metric("fcp", fcp),
                "si": score_metric("si", speed_index),
                "lcp": score_metric("lcp", lcp),
                "tbt": score_metric("tbt", tbt),
                "cls": score_metric("cls", cls),
                "inp": score_metric("inp", inp),
            }
            performance_score = compute_performance_score(metric_scores)

            opportunities = build_opportunities(diagnostics, self.resource_details)
            diagnostic_items = build_diagnostic_items(diagnostics)
            categories = self._build_category_scores(diagnostics)

            notes = [
                "Total Blocking Time is approximated from the Long Tasks API "
                "over the whole navigation, not the strict FCP-to-TTI window "
                "Lighthouse uses.",
                "Opportunity savings are heuristic estimates from network "
                "metadata and the DOM, not Lighthouse's simulated throttling model.",
                "Runs against the native (unthrottled) test browser - unlike "
                "Lighthouse's emulated mobile/4G environment.",
            ]
            if speed_index is None:
                notes.append("Speed Index was not computed (install 'Pillow' to "
                             "enable filmstrip-based Speed Index).")
            if page_results:
                notes.append(f"Session-level metrics above describe the LAST page open "
                             f"({len(page_results)} page load(s) were measured in total). "
                             f"See the per-page section for values averaged over every "
                             f"load of the same URL.")

            self.result = {
                "label": self.label,
                "generated_at": time.time(),
                "duration_seconds": round(time.time() - self._start_time, 2) if self._start_time else None,
                "navigations": self.navigations,
                # Every individual page load, each with its own metrics/scores.
                # The report groups these by URL and averages them.
                "page_results": page_results,
                "page_loads": sorted(
                    self.page_load_times.values(),
                    key=lambda p: (p.get("load_time_ms") is None, p.get("load_time_ms") or 0),
                    reverse=False,
                ),
                "performance_score": performance_score,
                "metrics": {
                    "first_contentful_paint_ms": fcp,
                    "largest_contentful_paint_ms": lcp,
                    "total_blocking_time_ms": tbt,
                    "cumulative_layout_shift": cls,
                    "speed_index_ms": speed_index,
                    "interaction_to_next_paint_ms": inp,
                },
                "metric_scores": {
                    k: {"score": v, "rating": rating_for_score(v)}
                    for k, v in metric_scores.items()
                },
                "categories": categories,
                "opportunities": opportunities,
                "diagnostics": diagnostic_items,
                "lcp_element": diagnostics.get("lcp_element"),
                "viewport": diagnostics.get("viewport"),
                "dom_stats": diagnostics.get("dom"),
                "render_blocking": diagnostics.get("render_blocking", []),
                "filmstrip": self._select_filmstrip_frames(),
                "screenshot": self.final_screenshot,
                "timing": perf_metrics,
                "console_messages": self.console_messages,
                "failed_requests": self.failed_requests,
                "resource_summary": {
                    k: v for k, v in self.resource_summary.items()
                },
                "notes": notes,
            }

            for cb in self._on_finalized_callbacks:
                try:
                    cb(self.result)
                except Exception:
                    pass

            return self.result

    def _collect_vitals(self) -> dict:
        try:
            res = self.client.send("Runtime.evaluate", {
                "expression": COLLECT_RESULTS_EXPR,
                "returnByValue": True,
            }, timeout=5)
            value = res.get("result", {}).get("value")
            return json.loads(value) if value else {}
        except (CDPError, TimeoutError, json.JSONDecodeError):
            return {}

    def _collect_performance_metrics(self) -> dict:
        try:
            res = self.client.send("Performance.getMetrics", timeout=5)
            return {m["name"]: m["value"] for m in res.get("metrics", [])}
        except (CDPError, TimeoutError):
            return {}

    def _collect_nav_timing(self) -> dict:
        try:
            res = self.client.send("Runtime.evaluate", {
                "expression": COLLECT_NAV_TIMING_EXPR,
                "returnByValue": True,
            }, timeout=5)
            value = res.get("result", {}).get("value")
            if not value or value == "null":
                return {}
            return json.loads(value)
        except (CDPError, TimeoutError, json.JSONDecodeError):
            return {}

    def _collect_diagnostics(self) -> dict:
        try:
            res = self.client.send("Runtime.evaluate", {
                "expression": COLLECT_DIAGNOSTICS_EXPR,
                "returnByValue": True,
            }, timeout=8)
            value = res.get("result", {}).get("value")
            return json.loads(value) if value else {}
        except (CDPError, TimeoutError, json.JSONDecodeError):
            return {}

    def _sample_minification(self) -> None:
        """
        Best-effort: fetch a handful of CSS/JS response bodies and flag ones
        that look unminified, recording an estimated saving. Each resource is
        checked at most once and the total is capped so this stays cheap.
        """
        if self._minify_samples >= _MAX_MINIFY_SAMPLES:
            return
        for url, detail in list(self.resource_details.items()):
            if self._minify_samples >= _MAX_MINIFY_SAMPLES:
                break
            req_id = detail.get("request_id")
            if not req_id or req_id in self._minify_checked:
                continue
            if detail.get("type") not in ("Script", "Stylesheet"):
                continue
            size = detail.get("transfer_size") or 0
            if size < 2048 or size > _MAX_MINIFY_BODY_BYTES:
                self._minify_checked.add(req_id)
                continue
            self._minify_checked.add(req_id)
            self._minify_samples += 1
            try:
                res = self.client.send("Network.getResponseBody",
                                       {"requestId": req_id}, timeout=5)
            except (CDPError, TimeoutError):
                continue
            if res.get("base64Encoded"):
                continue
            body = res.get("body") or ""
            unminified, fraction = analyze_minification(body)
            if unminified:
                detail["unminified"] = True
                detail["minify_savings"] = int(size * min(0.5, fraction))
                self.resource_details[url] = detail

    def _build_category_scores(self, diagnostics: dict) -> dict:
        """Convert the in-page audit results into 0-100 category scores plus
        the passed/failed audit lists, alongside the Performance category."""
        audits = diagnostics.get("audits", {}) or {}
        categories: dict[str, dict] = {}
        for key in ("accessibility", "best-practices", "seo", "pwa"):
            data = audits.get(key)
            if not data:
                continue
            score = data.get("score")
            categories[key] = {
                "score": round(score * 100) if score is not None else None,
                "passed": data.get("passed", []),
                "failed": data.get("failed", []),
            }
        return categories

    def _capture_screenshot(self) -> None:
        if self.final_screenshot is not None:
            return
        try:
            res = self.client.send("Page.captureScreenshot",
                                   {"format": "jpeg", "quality": 60}, timeout=8)
            data = res.get("data")
            if data:
                self.final_screenshot = data
        except (CDPError, TimeoutError):
            pass

    def _stop_screencast(self) -> None:
        if not self._screencast_on:
            return
        self._screencast_on = False
        try:
            self.client.send("Page.stopScreencast", timeout=3)
        except (CDPError, TimeoutError):
            pass

    def _select_filmstrip_frames(self, max_frames: int = 8) -> list:
        """Down-sample the captured frames to an evenly-spaced handful for the
        report (keeps embedded base64 payloads reasonable)."""
        with self._filmstrip_lock:
            frames = list(self.filmstrip)
        return [{"data": f["data"], "offset_ms": f.get("offset_ms", 0)}
                for f in _downsample(frames, max_frames)]

    @staticmethod
    def _compute_tbt(long_tasks: list) -> float:
        total = 0.0
        for task in long_tasks:
            duration = task.get("duration", 0)
            if duration > 50:
                total += duration - 50
        return round(total, 1)
