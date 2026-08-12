"""
Minimal Chrome DevTools Protocol (CDP) client.

This talks directly to a Chromium-family browser's debugging websocket -
the same protocol Lighthouse itself is built on - so no Node.js or
Lighthouse CLI is required.

Requires: websocket-client, requests
"""

from __future__ import annotations

import json
import threading
import time
import itertools
from typing import Any, Callable, Optional

import requests

try:
    import websocket  # from websocket-client
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "webperf_monitor requires the 'websocket-client' package. "
        "Install it with: pip install websocket-client"
    ) from exc


class CDPError(RuntimeError):
    """Raised when the browser returns a CDP error response."""


class CDPClient:
    """
    A synchronous CDP client that connects to a single target's
    (page-level) debugger websocket and exposes:

      - send(method, params) -> blocking call, returns the 'result' dict
      - on(event_name, callback) -> subscribe to CDP events
      - on_close(callback) -> subscribe to the websocket disconnecting
        (this fires when the tab/browser is closed)
    """

    def __init__(self, ws_url: str, connect_timeout: float = 10.0):
        self.ws_url = ws_url
        self._id_counter = itertools.count(1)
        self._pending: dict[int, dict] = {}
        self._pending_lock = threading.Lock()
        self._event_handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._close_handlers: list[Callable[[], None]] = []
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._closed = threading.Event()
        self._last_error: Optional[str] = None
        self._connect(connect_timeout)

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    def _connect(self, timeout: float) -> None:
        def _on_error(ws, err):
            self._last_error = str(err) or err.__class__.__name__

        self._ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=lambda ws: self._connected.set(),
            on_message=self._on_message,
            on_close=lambda ws, *a: self._on_close(),
            on_error=_on_error,
        )

        # Two things bite loopback DevTools connections on Windows:
        #
        #  1. Origin check (Chrome/Edge 111+): the DevTools endpoint returns
        #     HTTP 403 for any websocket whose Origin header is not in the
        #     browser's --remote-allow-origins allowlist, and the handshake
        #     just dies (surfacing here as a connect timeout). websocket-client
        #     sends "Origin: http://127.0.0.1:<port>" by default; native CDP
        #     clients send no Origin header at all, which makes Chrome skip the
        #     check entirely. suppress_origin=True reproduces that behaviour.
        #
        #  2. Proxy: if an HTTP/HTTPS proxy is configured in the environment,
        #     websocket-client would try to tunnel this localhost connection
        #     through the proxy and hang, even though plain `requests` calls to
        #     the same host succeed (they bypass the proxy for localhost).
        #     Bypass the proxy for loopback hosts.
        def _run() -> None:
            self._ws.run_forever(
                suppress_origin=True,
                http_no_proxy=["127.0.0.1", "localhost", "::1"],
                skip_utf8_validation=True,
            )

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout):
            # Surface the underlying reason (proxy tunnel failure, 403 origin
            # rejection, connection refused, ...) instead of a bare timeout.
            detail = self._last_error
            if not detail and self._closed.is_set():
                detail = "connection closed during handshake"
            suffix = f" ({detail})" if detail else ""
            raise TimeoutError(
                f"Timed out connecting to CDP endpoint: {self.ws_url}{suffix}"
            )

    def _on_close(self) -> None:
        self._closed.set()
        for handler in list(self._close_handlers):
            try:
                handler()
            except Exception:
                pass

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        if "id" in msg:
            with self._pending_lock:
                waiter = self._pending.get(msg["id"])
            if waiter is not None:
                waiter["response"] = msg
                waiter["event"].set()
        elif "method" in msg:
            handlers = self._event_handlers.get(msg["method"], [])
            for handler in handlers:
                try:
                    handler(msg.get("params", {}))
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def send(self, method: str, params: Optional[dict] = None, timeout: float = 15.0) -> dict:
        if self._closed.is_set():
            raise CDPError(f"Cannot send '{method}': CDP connection is closed")

        msg_id = next(self._id_counter)
        event = threading.Event()
        with self._pending_lock:
            self._pending[msg_id] = {"event": event, "response": None}

        payload = {"id": msg_id, "method": method, "params": params or {}}
        self._ws.send(json.dumps(payload))

        if not event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP call '{method}' timed out after {timeout}s")

        with self._pending_lock:
            response = self._pending.pop(msg_id, {}).get("response") or {}

        if "error" in response:
            raise CDPError(f"{method} failed: {response['error']}")
        return response.get("result", {})

    def send_async(self, method: str, params: Optional[dict] = None) -> None:
        """
        Fire-and-forget send that does NOT wait for a response. This is safe to
        call from inside an event handler (which runs on the websocket read
        thread): a blocking send() there would dead-lock, because the response
        can only be processed by that same thread once the handler returns.
        Used e.g. to ack screencast frames.
        """
        if self._closed.is_set():
            return
        msg_id = next(self._id_counter)
        payload = {"id": msg_id, "method": method, "params": params or {}}
        try:
            self._ws.send(json.dumps(payload))
        except Exception:
            pass

    def on(self, event_name: str, callback: Callable[[dict], None]) -> None:
        self._event_handlers.setdefault(event_name, []).append(callback)

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

    def close(self) -> None:
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass


def list_targets(debugger_address: str) -> list[dict]:
    """GET http://{host:port}/json - list of debuggable targets (tabs)."""
    resp = requests.get(f"http://{debugger_address}/json", timeout=5)
    resp.raise_for_status()
    return resp.json()


def find_page_ws_url(debugger_address: str, url_hint: Optional[str] = None) -> str:
    """
    Find the websocket debugger URL for the current top-level page target.
    If url_hint is given, prefer a target whose URL matches it.
    """
    targets = [t for t in list_targets(debugger_address) if t.get("type") == "page"]
    if not targets:
        raise RuntimeError(f"No page targets found at {debugger_address}")

    if url_hint:
        for t in targets:
            if t.get("url", "").startswith(url_hint):
                return t["webSocketDebuggerUrl"]

    return targets[0]["webSocketDebuggerUrl"]


def wait_for_debugger_address(debugger_address: str, timeout: float = 10.0) -> None:
    """Block until the /json endpoint responds (browser has finished booting)."""
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            requests.get(f"http://{debugger_address}/json/version", timeout=1)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.2)
    raise TimeoutError(f"Debugger address {debugger_address} never became ready: {last_err}")
