import os
from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal, QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage

class PyBridge(QObject):
    # Signal emitted when locators are received from JS
    locatorsReceived = pyqtSignal(list)

    @pyqtSlot(str)
    def receive_payload(self, json_payload):
        import json
        try:
            data = json.loads(json_payload)
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
        from PyQt6.QtWebChannel import QWebChannel
        self.channel = QWebChannel()
        self.pybridge = PyBridge()
        self.channel.registerObject("pybridge", self.pybridge)
        self.view.page().setWebChannel(self.channel)
        
        self.is_capturing = False

        self.view.loadFinished.connect(self._on_load_finished)

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

        # Inject qwebchannel.js
        self.view.page().runJavaScript(self._get_qwebchannel_js())

        # Inject our custom inspector script
        self.view.page().runJavaScript(self._get_inspector_js())

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
        safe_val = selector_value.replace("'", "\\'")
        self.view.page().runJavaScript(f"if (typeof window.highlightElementByLocator === 'function') window.highlightElementByLocator('{selector_type}', '{safe_val}');")

    def _get_inspector_js(self) -> str:
        script_path = os.path.join(os.path.dirname(__file__), "desktop_inspector.js")
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                return f.read()
        except:
            return ""

    def _get_qwebchannel_js(self) -> str:
        from PyQt6.QtCore import QFile, QIODevice, QTextStream
        file = QFile(":/qtwebchannel/qwebchannel.js")
        if file.open(QIODevice.OpenModeFlag.ReadOnly):
            stream = QTextStream(file)
            content = stream.readAll()
            file.close()
            return content
        return ""
