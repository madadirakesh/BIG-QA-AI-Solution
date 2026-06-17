import os
import json
import time
from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal, QUrl, QTimer, QFile, QIODevice, QTextStream
from PyQt6.QtWidgets import QTabWidget, QVBoxLayout, QWidget
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineScript, QWebEngineProfile, QWebEngineNewWindowRequest
from PyQt6.QtWebChannel import QWebChannel

class PyBridge(QObject):
    locatorsReceived = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.seen_cids = set()
        self.last_capture_time = 0

    @pyqtSlot(str)
    def receive_payload(self, json_payload):
        try:
            data = json.loads(json_payload)
            if not data: return

            cid = data[0].get("cid", "")
            if cid and cid in self.seen_cids:
                return
            if cid: self.seen_cids.add(cid)

            now = time.time()
            if now - self.last_capture_time < 1.0:
                return
            self.last_capture_time = now

            with open("debug_poll.log", "a") as f: f.write(f"RECEIVED FROM NATIVE BRIDGE: {len(data)} locators\n")
            self.locatorsReceived.emit(data)
        except Exception as e:
            print(f"Failed to parse payload from JS: {e}")


class SafeWebEnginePage(QWebEnginePage):
    """A page that silently accepts SSL errors."""
    def certificateError(self, error):
        return True


class BrowserController(QObject):
    # Emitted (url, title) when an action opens a new window/tab so the studio
    # can offer a "switch to window" step.
    windowOpened = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # The main UI container is now a Tab Widget
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        
        # Styles for the tab bar to blend with the studio
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #1e293b; border-radius: 4px; }
            QTabBar::tab { background: #0f172a; color: #94a3b8; padding: 8px 16px; border: 1px solid #1e293b; border-bottom: none; border-top-left-radius: 4px; border-top-right-radius: 4px; }
            QTabBar::tab:selected { background: #1e293b; color: #ffffff; font-weight: bold; }
            QTabBar::tab:hover:!selected { background: #1e293b; }
            QTabBar::close-button { subcontrol-position: right; padding: 2px; }
            QTabBar::close-button:hover { background: rgba(239,68,68,0.25); border-radius: 3px; }
        """)

        # Shared Python bridge
        self.pybridge = PyBridge()
        self.browser_instances = []
        self.is_capturing = False

        # Dashboard QWebEngineView reference — set by LocatorStudio after UI setup
        # so we can push URL-change notifications back to the dashboard panel.
        self._dashboard_view = None

        self._inspector_js = self._get_inspector_js()
        self._qwebchannel_js = self._get_qwebchannel_js()

        # Create the initial default tab
        self._create_new_tab("about:blank", "Main Page")

        # Polling fallback to drain buffers and heal broken states
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_capture_buffer)
        self.poll_timer.start(1000)

    def _create_new_tab(self, url: str = "", title: str = "New Tab") -> QWebEngineView:
        view = QWebEngineView()
        page = SafeWebEnginePage(view)
        view.setPage(page)

        # Isolated channel per tab, but shared pybridge QObject
        channel = QWebChannel(view)
        channel.registerObject("pybridge", self.pybridge)
        page.setWebChannel(channel)

        # Inject scripts into this tab's profile (only once per default profile)
        profile = page.profile()
        if not getattr(self, "_scripts_registered", False):
            scripts = profile.scripts()
            script = QWebEngineScript()
            code = self._qwebchannel_js + "\n" + self._inspector_js
            script.setSourceCode(code)
            script.setName("desktop_inspector")
            script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
            script.setRunsOnSubFrames(True)
            scripts.insert(script)
            self._scripts_registered = True

        # Connect signals
        view.loadFinished.connect(lambda ok, v=view: self._on_load_finished(ok, v))
        page.newWindowRequested.connect(self._on_new_window_requested)
        # Fix #7: propagate URL changes (back/forward/redirect) to the dashboard URL bar
        view.urlChanged.connect(lambda url, v=view: self._on_url_changed(url, v))

        self.browser_instances.append({
            'view': view,
            'channel': channel,
            'page': page
        })

        idx = self.tabs.addTab(view, title)
        self.tabs.setCurrentIndex(idx)

        if url and url != "about:blank":
            view.setUrl(QUrl(url))

        return view

    def _close_tab(self, index):
        if self.tabs.count() <= 1:
            return  # Prevent closing the last tab

        view = self.tabs.widget(index)
        self.tabs.removeTab(index)

        # Cleanup memory
        for inst in self.browser_instances:
            if inst['view'] == view:
                self.browser_instances.remove(inst)
                view.deleteLater()
                break

    def _on_new_window_requested(self, request: QWebEngineNewWindowRequest):
        """Intercept target=_blank and open cleanly in a new tab inside our QTabWidget."""
        url = request.requestedUrl().toString()
        if not url or url == "about:blank":
            return
        
        # Create a new tab immediately, but don't set its URL (Chromium will do it)
        new_view = self._create_new_tab(url="", title="Loading...")

        # Tell Chromium to fulfill the popup request by routing it into our new tab's page
        request.openIn(new_view.page())

        # Notify the studio that a new window opened (so it can add a
        # "switch to window" step). Fire once the new tab has finished loading
        # so we can report its final URL/title.
        def _announce_window(ok, v=new_view):
            try:
                v.loadFinished.disconnect(_announce_window)
            except Exception:
                pass
            try:
                self.windowOpened.emit(v.url().toString(), v.title() or "")
            except Exception:
                pass
        new_view.loadFinished.connect(_announce_window)

    def _on_url_changed(self, url, view):
        """Push the new URL to the dashboard URL bar whenever the active tab navigates."""
        # Only push for the currently visible tab to avoid confusing the user
        if view != self.tabs.currentWidget():
            return
        url_str = url.toString()
        if url_str in ('', 'about:blank'):
            return
        if self._dashboard_view:
            import json
            safe_url = json.dumps(url_str)
            self._dashboard_view.page().runJavaScript(
                f"if (typeof window.onBrowserUrlChanged === 'function') window.onBrowserUrlChanged({safe_url});"
            )

    def _on_load_finished(self, ok, view):
        if not ok: return
        
        # Update tab title
        idx = self.tabs.indexOf(view)
        if idx >= 0:
            title = view.title() or "New Tab"
            self.tabs.setTabText(idx, title[:20])

        # Force script injection on load to guarantee it didn't miss
        js_force = f"""
        if (typeof window.activateDesktopInspector === 'undefined') {{
            {self._qwebchannel_js}
            {self._inspector_js}
        }}
        """
        view.page().runJavaScript(js_force)

        # Re-activate inspector if we are in capturing mode
        if self.is_capturing:
            view.page().runJavaScript("if (typeof window.activateDesktopInspector === 'function') { window.activateDesktopInspector(); }")

    def _poll_capture_buffer(self):
        # Only poll the currently active tab to optimize performance and prevent background tab overhead
        active_view = self.tabs.currentWidget()
        if active_view:
            active_view.page().runJavaScript("if (typeof window._drainCaptureBuffer === 'function') { window._drainCaptureBuffer(); }", self._on_drain_result)
            if self.is_capturing:
                active_view.page().runJavaScript("if (typeof window.activateDesktopInspector === 'function' && !window.desktopInspectorActive) { window.activateDesktopInspector(); }")

    def _on_drain_result(self, result):
        if result and result != "[]":
            try:
                batches = json.loads(result)
                for payload in batches:
                    if not payload: continue
                    cid = payload[0].get("cid", "")
                    if cid and cid in self.pybridge.seen_cids: continue
                    if cid: self.pybridge.seen_cids.add(cid)
                    now = time.time()
                    if now - self.pybridge.last_capture_time < 1.0: continue
                    self.pybridge.last_capture_time = now
                    self.pybridge.locatorsReceived.emit(payload)
            except Exception as e:
                pass

    @property
    def view(self):
        """Proxy property to maintain backward compatibility with locator_studio.py."""
        return self.tabs.currentWidget()

    def get_ui_component(self):
        return self.tabs

    def load_url(self, url_str: str):
        current_view = self.tabs.currentWidget()
        if not current_view and self.browser_instances:
            current_view = self.browser_instances[0]['view']
            
        if current_view:
            url_str = url_str.strip()
            # Replace localhost with 127.0.0.1 to avoid macOS loopback IPv6 DNS resolution delays
            if "localhost" in url_str:
                url_str = url_str.replace("localhost", "127.0.0.1")

            if not url_str.startswith("http"):
                # Detect local addresses to prevent hanging on HTTPS handshake
                is_local = (
                    "127.0.0.1" in url_str 
                    or "::1" in url_str 
                    or url_str.startswith("192.168.") 
                    or url_str.startswith("10.")
                )
                if is_local:
                    url_str = "http://" + url_str
                else:
                    url_str = "https://" + url_str
            current_view.setUrl(QUrl(url_str))

    def start_capturing(self):
        self.is_capturing = True
        for inst in self.browser_instances:
            inst['view'].page().runJavaScript("if (typeof window.activateDesktopInspector === 'function') { window.activateDesktopInspector(); }")

    def stop_capturing(self):
        self.is_capturing = False
        for inst in self.browser_instances:
            inst['view'].page().runJavaScript("if (typeof window.deactivateDesktopInspector === 'function') { window.deactivateDesktopInspector(); }")

    def freeze_page(self, duration_ms: int):
        current_view = self.tabs.currentWidget()
        if current_view:
            current_view.page().runJavaScript(f"if (typeof window.freezePage === 'function') window.freezePage({duration_ms});")

    def highlight_element(self, selector_type: str, selector_value: str):
        safe_val = json.dumps(selector_value)
        current_view = self.tabs.currentWidget()
        if current_view:
            current_view.page().runJavaScript(f"if (typeof window.highlightElementByLocator === 'function') window.highlightElementByLocator('{selector_type}', {safe_val});")

    def _get_inspector_js(self) -> str:
        script_path = os.path.join(os.path.dirname(__file__), "desktop_inspector.js")
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                return f.read()
        except:
            return ""

    def _get_qwebchannel_js(self) -> str:
        file = QFile(":/qtwebchannel/qwebchannel.js")
        if file.open(QIODevice.OpenModeFlag.ReadOnly):
            stream = QTextStream(file)
            content = stream.readAll()
            file.close()
            return content
        return ""

