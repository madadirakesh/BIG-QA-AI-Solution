"""
performance_recorder.py
-----------------------
Record a user journey in a real browser and turn it into a Locust script.

Flow, from the Performance Test page's "Create Test" button:

  1. A Chrome window opens on the project's Application URL, driven by Selenium.
  2. A floating recorder panel is injected into every page the user visits. It
     carries Start / Stop / Save & Close, and shows what has been captured so far.
  3. After Start, the session captures both halves of the journey:
       * functional UI actions (clicks, typing, selects, submits, navigations),
         reported by the injected script through a CDP binding
       * document / XHR / fetch traffic, read straight off CDP's Network domain
  4. Closing the panel finalizes the session: the journey is converted to a
     Locust script, written into the project's locustfiles/ folder, and the
     browser closes.

Why CDP rather than Playwright: the app already ships Selenium and its own
CDPClient (webperf_monitor), so a recorder built on those adds no dependency and
no browser download. `Page.addScriptToEvaluateOnNewDocument` is what makes the
panel survive navigations - it re-runs the injection on every document, so a
full page load or an SPA route change cannot lose the controls.

Threading model: one worker thread per session drives the browser and owns all
CDP *calls*. CDP *events* arrive on the websocket reader thread, so their
handlers only ever append to a lock-guarded list or a queue - never send, which
would dead-lock the reader.
"""

import json
import os
import queue
import threading
import time
from datetime import datetime

from utils.locust_recorder_writer import write_recorded_script

LOCUSTFILES_DIRNAME = "locustfiles"

# CDP binding the injected panel calls to reach Python.
BINDING_NAME = "__bigqaRecorderSend"

# Only these resource types become Locust calls. Images/CSS/fonts/media are the
# CDN's problem, not the application's, and including them would triple the
# script length while measuring the wrong thing.
CAPTURED_RESOURCE_TYPES = {"Document", "XHR", "Fetch"}

# A recording left open forever would hold a browser and a thread. Two hours is
# far beyond any real journey but still bounded.
MAX_SESSION_SECONDS = 2 * 60 * 60

# Guardrails against a runaway page (an app that fires a request every 100ms).
MAX_CAPTURED_REQUESTS = 2000
MAX_CAPTURED_STEPS = 1000

_OVERLAY_JS = r"""
(() => {
  if (window.__bigqaRecorderInstalled) { return; }
  window.__bigqaRecorderInstalled = true;

  const HOST_ID = '__bigqa_recorder_host';

  const send = (payload) => {
    try {
      if (typeof window.__BINDING__ === 'function') {
        window.__BINDING__(JSON.stringify(payload));
      }
    } catch (err) { /* the binding is gone; nothing useful to do here */ }
  };

  // The panel lives in a shadow root, so events from it are retargeted to the
  // host element before they reach document listeners. Without this guard,
  // pressing Start would itself be recorded as the first user action.
  const isRecorderUi = (el) =>
    !!el && ((el.id === HOST_ID) || (el.closest && !!el.closest('#' + HOST_ID)));

  /* ---------------------------------------------------------------- *
   * Action capture - runs in every frame, including iframes.
   * ---------------------------------------------------------------- */

  const clean = (text) => (text || '').replace(/\s+/g, ' ').trim().slice(0, 60);

  const describe = (el) => {
    if (!el || !el.tagName) { return ''; }
    const tag = el.tagName.toLowerCase();
    const attr = (name) => (el.getAttribute && el.getAttribute(name)) || '';
    const label = clean(
      attr('aria-label') || attr('placeholder') || attr('name') || attr('id') ||
      attr('title') || attr('alt') || (tag === 'input' ? attr('value') : '') ||
      clean(el.innerText || el.textContent || '')
    );
    let kind = tag;
    if (tag === 'input') { kind = (attr('type') || 'text') + ' field'; }
    else if (tag === 'a') { kind = 'link'; }
    else if (tag === 'select') { kind = 'dropdown'; }
    else if (tag === 'textarea') { kind = 'text area'; }
    return label ? kind + " '" + label + "'" : kind;
  };

  const INTERACTIVE = 'a,button,input,select,textarea,label,[role=button],[role=link],[role=tab],[onclick]';

  document.addEventListener('click', (event) => {
    const raw = event.target;
    if (!raw || !raw.tagName || isRecorderUi(raw)) { return; }
    const el = (raw.closest && raw.closest(INTERACTIVE)) || raw;
    const tag = el.tagName.toLowerCase();
    if (tag === 'html' || tag === 'body') { return; }
    send({ kind: 'action', type: 'click', target: describe(el) });
  }, true);

  document.addEventListener('change', (event) => {
    const el = event.target;
    if (!el || !el.tagName || isRecorderUi(el)) { return; }
    const tag = el.tagName.toLowerCase();
    const type = ((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();

    if (tag === 'select') {
      const option = el.options && el.options[el.selectedIndex];
      send({ kind: 'action', type: 'select', target: describe(el),
             value: clean(option ? option.text : el.value) });
      return;
    }
    if (type === 'checkbox' || type === 'radio') {
      send({ kind: 'action', type: 'click', target: describe(el),
             value: el.checked ? 'checked' : 'unchecked' });
      return;
    }
    if (tag === 'input' || tag === 'textarea') {
      // The real value of a password field is sent so the backend can find and
      // redact it from recorded request bodies. It is never written to the
      // generated script - see locust_recorder_writer.
      send({ kind: 'action', type: 'input', target: describe(el),
             value: el.value == null ? '' : String(el.value).slice(0, 200),
             secret: type === 'password' });
    }
  }, true);

  document.addEventListener('submit', (event) => {
    if (isRecorderUi(event.target)) { return; }
    send({ kind: 'action', type: 'submit', target: describe(event.target) });
  }, true);

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || isRecorderUi(event.target)) { return; }
    send({ kind: 'action', type: 'press', target: describe(event.target), value: 'Enter' });
  }, true);

  /* ---------------------------------------------------------------- *
   * Control panel - top frame only.
   * ---------------------------------------------------------------- */
  if (window.top !== window) { return; }

  let ui = null;

  const build = () => {
    const host = document.createElement('div');
    host.id = HOST_ID;
    host.style.cssText = 'position:fixed;top:16px;right:16px;z-index:2147483647;';
    // A shadow root keeps the page's CSS out of the panel and the panel's CSS
    // out of the page - important when recording an app with aggressive resets.
    const root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>
        * { box-sizing: border-box; }
        .panel {
          width: 268px; border-radius: 12px; overflow: hidden;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          background: #11162a; color: #e8ecf8;
          border: 1px solid rgba(255,255,255,0.14);
          box-shadow: 0 18px 44px rgba(0,0,0,0.45);
        }
        .head {
          display: flex; align-items: center; gap: 8px; cursor: move;
          padding: 10px 12px; background: rgba(255,255,255,0.06);
          border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        .dot { width: 9px; height: 9px; border-radius: 50%; background: #64748b; flex: none; }
        .dot.rec { background: #ef4444; animation: pulse 1.2s ease-in-out infinite; }
        .dot.paused { background: #fbbf24; }
        .dot.done { background: #22c55e; }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.25; } }
        .title { font-size: 12px; font-weight: 700; letter-spacing: 0.3px; flex: 1; }
        .close {
          background: none; border: none; color: #94a3b8; cursor: pointer;
          font-size: 17px; line-height: 1; padding: 0 2px;
        }
        .close:hover { color: #fff; }
        .body { padding: 12px; }
        .state { font-size: 12px; margin-bottom: 10px; color: #cbd5f5; min-height: 32px; }
        .counts { display: flex; gap: 8px; margin-bottom: 10px; }
        .count {
          flex: 1; text-align: center; padding: 6px 4px; border-radius: 8px;
          background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08);
        }
        .count b { display: block; font-size: 15px; }
        .count span { font-size: 10px; color: #94a3b8; text-transform: uppercase; }
        .row { display: flex; gap: 8px; }
        button.act {
          flex: 1; padding: 8px 6px; border-radius: 8px; cursor: pointer;
          font-size: 12px; font-weight: 600; border: 1px solid transparent;
          background: rgba(255,255,255,0.08); color: #e8ecf8;
        }
        button.act:hover:not(:disabled) { background: rgba(255,255,255,0.16); }
        button.act:disabled { opacity: 0.4; cursor: not-allowed; }
        button.start { background: #dc2626; }
        button.save  { background: #2563eb; margin-top: 8px; width: 100%; }
      </style>
      <div class="panel">
        <div class="head" part="head">
          <span class="dot" id="dot"></span>
          <span class="title">BIG QA RECORDER</span>
          <button class="close" id="close" title="Save &amp; close">&times;</button>
        </div>
        <div class="body">
          <div class="state" id="state">Connecting…</div>
          <div class="counts">
            <div class="count"><b id="acts">0</b><span>Actions</span></div>
            <div class="count"><b id="reqs">0</b><span>Requests</span></div>
          </div>
          <div class="row">
            <button class="act start" id="start">Start</button>
            <button class="act" id="stop" disabled>Stop</button>
          </div>
          <button class="act save" id="save">Save &amp; close</button>
        </div>
      </div>`;
    (document.documentElement || document.body).appendChild(host);

    const $ = (id) => root.getElementById(id);
    $('start').addEventListener('click', () => send({ kind: 'control', action: 'start' }));
    $('stop').addEventListener('click', () => send({ kind: 'control', action: 'stop' }));
    $('save').addEventListener('click', () => send({ kind: 'control', action: 'finish' }));
    $('close').addEventListener('click', () => send({ kind: 'control', action: 'finish' }));

    // Drag by the header so the panel can be moved off whatever it covers.
    let drag = null;
    $('close').addEventListener('mousedown', (e) => e.stopPropagation());
    root.querySelector('.head').addEventListener('mousedown', (event) => {
      const box = host.getBoundingClientRect();
      drag = { dx: event.clientX - box.left, dy: event.clientY - box.top };
      event.preventDefault();
    });
    window.addEventListener('mousemove', (event) => {
      if (!drag) { return; }
      host.style.left = Math.max(0, event.clientX - drag.dx) + 'px';
      host.style.top = Math.max(0, event.clientY - drag.dy) + 'px';
      host.style.right = 'auto';
    }, true);
    window.addEventListener('mouseup', () => { drag = null; }, true);

    return { host, root, $ };
  };

  const mount = () => {
    if (!document.documentElement) { return; }
    if (document.getElementById(HOST_ID)) { return; }
    ui = build();
    if (window.__bigqaRecorderLastState) {
      window.__bigqaRecorderSetState(window.__bigqaRecorderLastState);
    }
  };

  window.__bigqaRecorderSetState = (raw) => {
    window.__bigqaRecorderLastState = raw;
    if (!ui) { return; }
    let data;
    try { data = JSON.parse(raw); } catch (err) { return; }
    ui.$('state').textContent = data.message || '';
    ui.$('acts').textContent = data.steps == null ? 0 : data.steps;
    ui.$('reqs').textContent = data.requests == null ? 0 : data.requests;
    ui.$('dot').className = 'dot' + (data.dot ? ' ' + data.dot : '');
    ui.$('start').disabled = !data.can_start;
    ui.$('stop').disabled = !data.can_stop;
    ui.$('save').disabled = !data.can_finish;
    ui.$('start').textContent = data.start_label || 'Start';
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
  // Single-page apps routinely replace the whole DOM on a route change; a cheap
  // re-mount check is more reliable here than a MutationObserver on <html>.
  setInterval(mount, 1000);
})();
""".replace("__BINDING__", BINDING_NAME)


class RecordingSession:
    """One browser recording. Owns a Selenium driver, a CDP client and a thread."""

    def __init__(self, session_id, perf_dir, project_name, application_url):
        self.id = session_id
        self.perf_dir = perf_dir
        self.project_name = project_name
        self.application_url = application_url

        self.state = "launching"
        self.message = "Opening the browser…"
        self.error = ""
        self.script_file = ""
        self.script_path = ""
        self.created_at = time.time()
        self.finished_at = None

        self._steps = []
        self._requests = []
        self._by_request_id = {}
        self._secrets = []
        self._last_navigation = ""
        self._lock = threading.Lock()

        self._commands = queue.Queue()
        self._finish_requested = threading.Event()
        self._cancel_requested = threading.Event()
        self._done = threading.Event()

        self._driver = None
        self._client = None
        self._thread = None

    # ── public API ──────────────────────────────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._run, name=f"perf-recorder-{self.id}", daemon=True)
        self._thread.start()

    def request_finish(self):
        self._finish_requested.set()

    def request_cancel(self):
        self._cancel_requested.set()
        self._finish_requested.set()

    @property
    def is_active(self):
        return not self._done.is_set()

    def snapshot(self):
        """State for the polling endpoint."""
        with self._lock:
            return {
                "session_id": self.id,
                "state": self.state,
                "message": self.message,
                "error": self.error,
                "steps": len(self._steps),
                "requests": len(self._requests),
                "script_file": self.script_file,
                "script_path": self.script_path,
                "project_name": self.project_name,
                "application_url": self.application_url,
                "active": self.is_active,
                "last_action": self._steps[-1].get("target", "") if self._steps else "",
            }

    # ── state helpers ───────────────────────────────────────────────────

    def _set(self, state, message):
        with self._lock:
            self.state = state
            self.message = message

    def _fail(self, message):
        with self._lock:
            self.state = "error"
            self.error = message
            self.message = message

    def _overlay_payload(self):
        with self._lock:
            state, steps, requests = self.state, len(self._steps), len(self._requests)
            script_file = self.script_file
        presets = {
            "launching": ("Starting up…", "", False, False, False),
            "ready": ("Ready. Press Start, then use the application as a user would.",
                      "", True, False, True),
            "recording": ("Recording. Every click, entry and request is being captured.",
                          "rec", False, True, True),
            "paused": ("Paused. Press Resume to continue, or Save & close to finish.",
                       "paused", True, False, True),
            "saving": ("Generating the Locust script…", "done", False, False, False),
            "completed": (f"Saved {script_file}. This window can be closed.", "done", False, False, False),
            "error": ("The recording failed. See the Performance Test page.", "", False, False, False),
        }
        message, dot, can_start, can_stop, can_finish = presets.get(
            state, ("", "", False, False, True))
        return {
            "message": message,
            "dot": dot,
            "steps": steps,
            "requests": requests,
            "can_start": can_start,
            "can_stop": can_stop,
            "can_finish": can_finish,
            "start_label": "Resume" if state == "paused" else "Start",
        }

    def _push_overlay(self):
        if self._client is None or self._client.is_closed:
            return
        payload = json.dumps(json.dumps(self._overlay_payload()))  # JS string literal
        try:
            self._client.send("Runtime.evaluate", {
                "expression": f"window.__bigqaRecorderSetState && window.__bigqaRecorderSetState({payload})",
                "returnByValue": True,
            }, timeout=5)
        except Exception:
            # A navigation can land between the check and the call; the next
            # tick re-pushes, and the panel re-applies its last state on mount.
            pass

    # ── CDP event handlers (websocket reader thread - never call send here) ──

    def _on_binding(self, params):
        if params.get("name") != BINDING_NAME:
            return
        try:
            payload = json.loads(params.get("payload") or "{}")
        except ValueError:
            return

        # UI actions are recorded here, on the reader thread, rather than being
        # queued for the worker. Network events are handled on this same thread,
        # so recording both inline is what keeps them in true chronological
        # order - queueing the actions let a request be filed under the previous
        # step, because the click that caused it had not been drained yet.
        # Safe because _record_action only takes a lock and appends; it issues
        # no CDP call, which is the thing that would dead-lock this thread.
        if payload.get("kind") == "action":
            self._record_action(payload)
        else:
            self._commands.put(payload)

    def _on_request(self, params):
        with self._lock:
            if self.state != "recording" or len(self._requests) >= MAX_CAPTURED_REQUESTS:
                return
            if params.get("type") not in CAPTURED_RESOURCE_TYPES:
                return
            request = params.get("request") or {}
            url = request.get("url") or ""
            if not url.lower().startswith(("http://", "https://")):
                return
            headers = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
            entry = {
                "method": (request.get("method") or "GET").upper(),
                "url": url,
                "resource_type": params.get("type"),
                "post_data": request.get("postData") or "",
                "content_type": headers.get("content-type", ""),
                "status": None,
                "step_index": self._steps[-1]["index"] if self._steps else -1,
                "at": time.time(),
            }
            self._requests.append(entry)
            request_id = params.get("requestId")
            if request_id:
                self._by_request_id[request_id] = entry

    def _on_response(self, params):
        with self._lock:
            entry = self._by_request_id.get(params.get("requestId"))
            if entry is not None:
                entry["status"] = (params.get("response") or {}).get("status")

    def _on_navigated(self, params):
        frame = params.get("frame") or {}
        if frame.get("parentId"):
            return  # sub-frame: not a user-visible navigation
        url = frame.get("url") or ""
        if not url.lower().startswith(("http://", "https://")):
            return
        with self._lock:
            if self.state != "recording" or url == self._last_navigation:
                return
            self._last_navigation = url
            self._append_step_locked({"type": "navigate", "target": url, "url": url})

    # ── journey building ────────────────────────────────────────────────

    def _append_step_locked(self, step):
        if len(self._steps) >= MAX_CAPTURED_STEPS:
            return
        step["index"] = len(self._steps)
        step["at"] = time.time()
        self._steps.append(step)

    def _record_action(self, payload):
        with self._lock:
            if self.state != "recording":
                return
            secret = bool(payload.get("secret"))
            value = payload.get("value") or ""
            if secret and value and value not in self._secrets:
                # Kept in memory only, to redact this value out of request bodies.
                self._secrets.append(value)
            self._append_step_locked({
                "type": payload.get("type") or "action",
                "target": payload.get("target") or "",
                "value": value,
                "secret": secret,
            })

    def _current_url(self):
        try:
            result = self._client.send(
                "Runtime.evaluate",
                {"expression": "location.href", "returnByValue": True}, timeout=5)
            url = ((result.get("result") or {}).get("value") or "").strip()
        except Exception:
            url = ""
        return url if url.lower().startswith(("http://", "https://")) else self.application_url

    def _seed_entry_point(self):
        """
        Open the journey with a GET of whatever page Start was pressed on.

        Without this the script skips the page the tester was already looking
        at, and a Locust user - which starts with no cookies and no history -
        would begin mid-journey against an application that expects the entry
        page to have been fetched first.
        """
        url = self._current_url()
        if not url:
            return
        with self._lock:
            self._last_navigation = url
            self._append_step_locked({"type": "navigate", "target": url, "url": url})
            self._requests.append({
                "method": "GET",
                "url": url,
                "resource_type": "Document",
                "post_data": "",
                "content_type": "",
                "status": None,
                "step_index": self._steps[-1]["index"] if self._steps else -1,
                "at": time.time(),
            })

    def _handle_control(self, action):
        if action == "start":
            with self._lock:
                self._last_navigation = ""
            self._set("recording", "Recording…")
            self._seed_entry_point()
            self._push_overlay()
        elif action == "stop":
            self._set("paused", "Paused.")
            self._push_overlay()
        elif action == "finish":
            self._finish_requested.set()
        elif action == "cancel":
            self.request_cancel()

    def _drain_commands(self):
        while True:
            try:
                payload = self._commands.get_nowait()
            except queue.Empty:
                return
            if payload.get("kind") == "control":
                self._handle_control(payload.get("action"))

    # ── lifecycle ───────────────────────────────────────────────────────

    def _launch(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:
            raise RuntimeError(
                "Selenium is required to record a performance test. "
                "Install it with: pip install selenium"
            ) from exc
        from webperf_monitor.cdp_client import CDPClient, find_page_ws_url

        options = Options()
        options.add_argument("--start-maximized")
        # Drop the "controlled by automated test software" infobar: the tester is
        # driving this window by hand and the banner only gets in the way.
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        self._driver = webdriver.Chrome(options=options)

        chrome_caps = (self._driver.capabilities.get("goog:chromeOptions") or {})
        address = chrome_caps.get("debuggerAddress")
        if not address:
            raise RuntimeError(
                "Chrome did not expose a CDP debugger address, so the recorder "
                "cannot capture network traffic."
            )

        self._client = CDPClient(find_page_ws_url(address))
        self._client.send("Page.enable")
        self._client.send("Network.enable")
        self._client.send("Runtime.enable")
        self._client.send("Runtime.addBinding", {"name": BINDING_NAME})
        # Registered before the first navigation so the very first document the
        # tester sees already carries the panel.
        self._client.send("Page.addScriptToEvaluateOnNewDocument", {"source": _OVERLAY_JS})

        self._client.on("Runtime.bindingCalled", self._on_binding)
        self._client.on("Network.requestWillBeSent", self._on_request)
        self._client.on("Network.responseReceived", self._on_response)
        self._client.on("Page.frameNavigated", self._on_navigated)

        self._driver.get(self.application_url)

    def _pump(self):
        deadline = time.time() + MAX_SESSION_SECONDS
        last_push = 0.0
        while not self._finish_requested.is_set():
            if self._client.is_closed:
                # The tester closed the tab or the browser. Treat it as "finish"
                # rather than "discard" - losing a recorded journey to a stray
                # click on the window's X would be infuriating.
                self._set(self.state, "Browser closed; saving the recording…")
                break
            if time.time() > deadline:
                self._set(self.state, "Recording time limit reached; saving…")
                break
            self._drain_commands()
            now = time.time()
            if now - last_push >= 0.7:
                self._push_overlay()
                last_push = now
            time.sleep(0.15)
        self._drain_commands()

    def _finalize(self):
        if self._cancel_requested.is_set():
            self._set("cancelled", "Recording discarded.")
            return

        self._set("saving", "Generating the Locust script…")
        self._push_overlay()

        with self._lock:
            journey = {
                "project_name": self.project_name,
                "application_url": self.application_url,
                "steps": list(self._steps),
                "requests": list(self._requests),
                "secrets": list(self._secrets),
                "started_at": datetime.fromtimestamp(self.created_at),
                "finished_at": datetime.now(),
            }

        path, file_name = write_recorded_script(
            os.path.join(self.perf_dir, LOCUSTFILES_DIRNAME), journey)

        with self._lock:
            self.script_path = path
            self.script_file = file_name
            self.state = "completed"
            self.message = (
                f"Saved {file_name} — {len(journey['steps'])} action(s), "
                f"{len(journey['requests'])} request(s)."
            )
        # Let the panel show the confirmation before the window disappears.
        self._push_overlay()
        time.sleep(1.2)

    def _teardown(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
        self.finished_at = time.time()
        self._done.set()

    def _run(self):
        try:
            self._launch()
            self._set("ready", "Press Start in the recorder panel to begin capturing.")
            self._push_overlay()
            self._pump()
            self._finalize()
        except Exception as exc:
            self._fail(str(exc) or exc.__class__.__name__)
        finally:
            self._teardown()


class RecorderRegistry:
    """Tracks live recordings so the HTTP layer can poll and control them."""

    # Completed sessions are kept briefly so the page can pick up the result
    # after its next poll, then dropped so the dict cannot grow unbounded.
    RETENTION_SECONDS = 30 * 60

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def active_for_project(self, perf_dir):
        with self._lock:
            for session in self._sessions.values():
                if session.perf_dir == perf_dir and session.is_active:
                    return session
        return None

    def create(self, session_id, perf_dir, project_name, application_url):
        session = RecordingSession(session_id, perf_dir, project_name, application_url)
        with self._lock:
            self._sessions[session_id] = session
        self._purge()
        session.start()
        return session

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def _purge(self):
        cutoff = time.time() - self.RETENTION_SECONDS
        with self._lock:
            stale = [
                key for key, session in self._sessions.items()
                if not session.is_active and (session.finished_at or 0) < cutoff
            ]
            for key in stale:
                self._sessions.pop(key, None)


registry = RecorderRegistry()
