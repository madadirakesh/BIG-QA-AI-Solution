import os
import json
import time
from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal, QUrl, QTimer, QFile, QIODevice, QTextStream
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineScript, QWebEngineProfile
from PyQt6.QtWebChannel import QWebChannel

class PyBridge(QObject):
    # Signal emitted when locators are received from JS
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
            
            # Catch ghost clicks from different iframes/listeners within 1 second
            now = time.time()
            if now - self.last_capture_time < 1.0:
                return
            self.last_capture_time = now

            with open("debug_poll.log", "a") as f: f.write(f"RECEIVED FROM NATIVE BRIDGE: {len(data)} locators\\n")
            self.locatorsReceived.emit(data)
        except Exception as e:
            print(f"Failed to parse payload from JS: {e}")

class CustomWebEnginePage(QWebEnginePage):
    def certificateError(self, error):
        # Ignore SSL errors which frequently block local staging sites in QA tools
        return True

class BrowserController(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = QWebEngineView()
        
        # Override the page with our custom page that ignores SSL errors
        self.custom_page = CustomWebEnginePage(self.view)
        self.view.setPage(self.custom_page)
        self.view.setUrl(QUrl("about:blank"))
        
        # Setup channel
        self.channel = QWebChannel()
        self.pybridge = PyBridge()
        self.channel.registerObject("pybridge", self.pybridge)
        self.view.page().setWebChannel(self.channel)
        
        self.is_capturing = False

        # Inject scripts into all frames
        profile = self.view.page().profile()
        scripts = profile.scripts()
        script = QWebEngineScript()
        code = self._get_qwebchannel_js() + "\n" + self._get_inspector_js()
        script.setSourceCode(code)
        script.setName("inspector")
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setRunsOnSubFrames(True)
        scripts.insert(script)

        self.view.loadFinished.connect(self._on_load_finished)

        # Polling fallback to drain the unbreakable capture buffer
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_capture_buffer)
        self.poll_timer.start(1000)

    def _poll_capture_buffer(self):
        # We periodically ask the main window to drain its _captureBuffer
        self.view.page().runJavaScript("if (typeof window._drainCaptureBuffer === 'function') { window._drainCaptureBuffer(); }", self._on_drain_result)

    def _on_drain_result(self, result):
        if result and result != "[]":
            try:
                batches = json.loads(result)
                with open("debug_poll.log", "a") as f: f.write(f"RECEIVED FROM TIMER: {len(batches)} batches\\n")
                for payload in batches:
                    if not payload: continue
                    cid = payload[0].get("cid", "")
                    if cid and cid in self.pybridge.seen_cids:
                        continue
                    if cid: self.pybridge.seen_cids.add(cid)
                    
                    now = time.time()
                    if now - self.pybridge.last_capture_time < 1.0:
                        continue
                    self.pybridge.last_capture_time = now
                    
                    self.pybridge.locatorsReceived.emit(payload)
            except Exception as e:
                with open("debug_poll.log", "a") as f:
                    f.write(f"Error parsing drained buffer: {e}\\nRaw result: {result}\\n")

    def get_ui_component(self):
        return self.view

    def load_url(self, url_str: str):
        url_str = url_str.strip()
        if not url_str.startswith("http"):
            url_str = "https://" + url_str
        self.view.setUrl(QUrl(url_str))

    def _on_load_finished(self, ok):
        if not ok:
            return

        # If currently capturing, re-activate
        if self.is_capturing:
            self.view.page().runJavaScript("if (typeof window.activateDesktopInspector === 'function') { window.activateDesktopInspector(); }")

    def start_capturing(self):
        self.is_capturing = True
        self.view.page().runJavaScript("if (typeof window.activateDesktopInspector === 'function') { window.activateDesktopInspector(); }")

    def stop_capturing(self):
        self.is_capturing = False
        self.view.page().runJavaScript("if (typeof window.deactivateDesktopInspector === 'function') { window.deactivateDesktopInspector(); }")

    def freeze_page(self, duration_ms: int):
        self.view.page().runJavaScript(f"if (typeof window.freezePage === 'function') window.freezePage({duration_ms});")

    def highlight_element(self, selector_type: str, selector_value: str):
        safe_val = json.dumps(selector_value)
        self.view.page().runJavaScript(f"if (typeof window.highlightElementByLocator === 'function') window.highlightElementByLocator('{selector_type}', {safe_val});")

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
