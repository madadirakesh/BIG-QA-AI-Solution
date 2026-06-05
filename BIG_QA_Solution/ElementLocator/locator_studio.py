import sys
import os
import platform
import json

# Bypass GPU driver negotiation on Windows (safe — software rasterizer takes over as renderer)
if platform.system() == "Windows":
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --disable-gpu-compositing"
import logging
import sqlite3
import threading
from pathlib import Path
from datetime import datetime

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QSplitter, QFileDialog, QMessageBox)
from PyQt6.QtCore import Qt, QUrl, pyqtSlot, QObject, pyqtSignal, QTimer, QFile, QIODevice, QTextStream
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel

# Path setup
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

from PyQt6.QtWebEngineCore import QWebEnginePage

class WebPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        msg = f"JS Console: {message} (Line: {lineNumber}, Source: {sourceID})"
        print(msg)
        # Force writing to studio_error.log
        try:
            with open(os.path.join(os.path.dirname(__file__), 'studio_error.log'), 'a', encoding='utf-8') as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - JS_CONSOLE - {msg}\n")
        except Exception as e:
            pass


from browser_controller import BrowserController
from ai_service import AIService
from code_generator import CodeGenerator
from excel_exporter import ExcelExporter
from merge_engine import MergeEngine
from dotenv import load_dotenv

# Setup error logging
logging.basicConfig(
    filename='studio_error.log',
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def _ensure_qwebchannel_js():
    """Materialise Qt's bundled qwebchannel.js to ui/libs/qwebchannel.js.

    dashboard.html includes <script src="libs/qwebchannel.js"> synchronously
    during HTML parsing. If the file is missing, the dashboard's initBridge()
    throws "ReferenceError: QWebChannel is not defined" and pyBridge stays
    null — which breaks every JS->Python action (capture toggle, AI requests,
    code preview, smart merge, etc.).

    BrowserController also injects qwebchannel.js into the default profile,
    but at InjectionPoint.DocumentReady — too late for inline scripts that
    run during HTML parse. So we drop the file on disk instead.
    """
    libs_dir = BASE_DIR / "ui" / "libs"
    target = libs_dir / "qwebchannel.js"
    if target.exists():
        return
    libs_dir.mkdir(parents=True, exist_ok=True)
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        print(f"[Studio] WARNING: could not read :/qtwebchannel/qwebchannel.js from Qt resources; dashboard bridge will be broken", flush=True)
        return
    try:
        stream = QTextStream(f)
        content = stream.readAll()
    finally:
        f.close()
    target.write_text(content, encoding="utf-8")
    print(f"[Studio] Wrote qwebchannel.js to {target}", flush=True)


def find_existing_page_objects(project_path, language):
    if not project_path or not os.path.exists(project_path):
        return []
        
    ext_map = {
        "java": ".java",
        "python": ".py",
        "c#": ".cs",
        "javascript": ".js",
        "typescript": ".ts"
    }
    target_ext = ext_map.get(language.lower(), "")
    if not target_ext:
        return []
        
    page_objects = []
    # Recursively scan files
    for root, dirs, files in os.walk(project_path):
        # Exclude directories we shouldn't scan
        dirs[:] = [d for d in dirs if d not in ('.venv', '.git', '.idea', '__pycache__', 'node_modules', 'target', 'bin', 'obj')]
        
        for file in files:
            file_lower = file.lower()
            if not file.endswith(target_ext):
                continue
            is_page_object = False
            if "page" in file_lower or "pom" in file_lower:
                is_page_object = True
            elif "page" in root.lower() or "pom" in root.lower():
                is_page_object = True
                
            if is_page_object:
                page_objects.append(os.path.join(root, file))
                if len(page_objects) >= 5: # Limit to 5 files to avoid scanning too many
                    return page_objects
                    
    return page_objects

def find_matching_page_object(existing_pos, page_name):
    if not existing_pos or not page_name:
        return None
    page_name_lower = page_name.lower()
    clean_name = page_name_lower.replace("page", "").replace("_", "").replace("-", "")
    for path in existing_pos:
        filename = os.path.basename(path).lower()
        clean_file = filename.split('.')[0].replace("page", "").replace("_", "").replace("-", "")
        if clean_file == clean_name:
            return path
            
    for path in existing_pos:
        filename = os.path.basename(path).lower()
        if clean_name in filename:
            return path
            
    return None

class StudioBridge(QObject):
    """Bridge for communication between Python and the Dashboard UI"""
    locatorsReceived = pyqtSignal(str) # Send to JS
    aiLocatorsReceived = pyqtSignal(str) # Send AI locators to JS
    mergePreviewReady = pyqtSignal(str) # Send Merge preview to JS
    codePreviewReady = pyqtSignal(str) # Send Generated Code to JS
    liveVerificationResult = pyqtSignal(str) # Send verification result to JS
    commandReceived = pyqtSignal(str, str) # From JS: action, payload

    @pyqtSlot(str, str)
    def callPython(self, action, payload):
        self.commandReceived.emit(action, payload)

class MergeBridge(QObject):
    """
    Dedicated, isolated QObject bridge for the Smart Merge popup window.
    QWebChannel REQUIRES a pure QObject (not QMainWindow) to reliably expose
    slots and signals to JavaScript. This is the canonical Qt approach.
    """
    mergePreviewReady = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._window = None   # Back-pointer to the MergeWindow

    @pyqtSlot(str, str)
    def callPython(self, action, payload_str):
        if self._window is not None:
            self._window._handle_command(action, payload_str)


class GridBridge(QObject):
    """
    Dedicated, isolated QObject bridge for the spacious Captured Locators Grid View.
    Allows reliable communication between JS in grid.html and python.
    """
    gridActionReceived = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self._window = None

    @pyqtSlot(str, str)
    def callPython(self, action, payload_str):
        if self._window is not None:
            self._window._handle_command(action, payload_str)


class GridWindow(QMainWindow):
    def __init__(self, parent, locators, tool, lang):
        super().__init__(parent)
        self.setWindowTitle("Captured Locators Grid View")
        self.locators = locators
        self.tool = tool
        self.lang = lang
        self._parent_studio = parent

        # Spacious size suitable for large screen editing, centered
        screen = QApplication.primaryScreen().availableGeometry()
        win_w = min(1180, int(screen.width() * 0.75))
        win_h = int(screen.height() * 0.82)
        win_x = screen.x() + (screen.width() - win_w) // 2
        win_y = screen.y() + (screen.height() - win_h) // 2
        self.setGeometry(win_x, win_y, win_w, win_h)
        self.setMinimumSize(950, 600)

        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)

        self.view = QWebEngineView()
        self.page_obj = WebPage(self.view)
        self.view.setPage(self.page_obj)

        self._bridge = GridBridge()
        self._bridge._window = self
        self.channel = QWebChannel()
        self.channel.registerObject("pyBridge", self._bridge)
        self.view.page().setWebChannel(self.channel)

        self.view.loadFinished.connect(self._on_load_finished)

        grid_path = BASE_DIR / "ui" / "grid.html"
        self.view.setUrl(QUrl.fromLocalFile(str(grid_path)))

        layout.addWidget(self.view)
        self.setCentralWidget(central_widget)

        # Polling action queue timer
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_js_action)

        QTimer.singleShot(150, self.force_foreground)

    def _on_load_finished(self, ok):
        print(f"[GridWindow] loadFinished ok={ok}, locators={len(self.locators)}")
        if ok:
            QTimer.singleShot(500, self._push_data_to_js)
            self._poll_timer.start(150)

    def _push_data_to_js(self):
        try:
            payload = {
                "elements": self.locators,
                "tool":     self.tool,
                "lang":     self.lang
            }
            payload_json = json.dumps(payload)
            self.view.page().runJavaScript(
                f"(function(){{ try{{ loadInitialGridData({payload_json}); return 'ok'; }}"
                f" catch(e){{ return 'ERR:'+e.toString(); }} }})()"
            )
        except Exception as e:
            print(f"[GridWindow] _push_data_to_js ERROR: {e}")
            logging.error(f"Error in _push_data_to_js: {e}")

    def _poll_js_action(self):
        try:
            self.view.page().runJavaScript(
                "typeof getPendingAction === 'function' ? getPendingAction() : null",
                self._on_js_action
            )
        except Exception:
            pass

    def _on_js_action(self, result):
        if not result:
            return
        try:
            data = json.loads(result)
            action  = data.get("action", "")
            payload = data.get("payload", "")
            if action:
                self._handle_command(action, payload)
        except Exception as e:
            print(f"[GridWindow] _on_js_action parse error: {e}")

    def force_foreground(self):
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        self._poll_timer.stop()
        super().closeEvent(event)
        # Notify the parent window dashboard to refresh the sidebar queue view
        self._parent_studio.dashboard_view.page().runJavaScript("renderQueue();")
        if hasattr(self._parent_studio, "grid_window"):
            self._parent_studio.grid_window = None

    def update_data(self, locators, tool, lang):
        self.locators = locators
        self.tool = tool
        self.lang = lang
        self._push_data_to_js()

    def _handle_command(self, action, payload):
        try:
            print(f"[GridWindow] Command: {action}")
            if action == "grid_window_ready":
                self._push_data_to_js()

            elif action == "close_grid_window":
                self.close()

            elif action == "grid_edit":
                idx = payload.get("idx")
                field = payload.get("field")
                value = payload.get("value")
                # Keep Python-side locators in sync so highlight/verify/AI use current data
                if isinstance(idx, int) and 0 <= idx < len(self.locators):
                    self.locators[idx][field] = value
                    if field == 'nameHint':
                        self.locators[idx]['name'] = value
                # Forward to main dashboard
                safe_val = json.dumps(value)
                js = f"updateElementFromGrid({idx}, '{field}', {safe_val});"
                self._parent_studio.dashboard_view.page().runJavaScript(js)

            elif action == "grid_highlight":
                idx = payload.get("idx")
                if idx >= 0 and idx < len(self.locators):
                    loc = self.locators[idx]
                    self._parent_studio.browser_ctrl.highlight_element(loc.get("type"), loc.get("value"))

            elif action == "grid_verify":
                idx = payload.get("idx")
                if idx >= 0 and idx < len(self.locators):
                    loc = self.locators[idx]
                    l_type = loc.get("type")
                    l_val = loc.get("value")
                    
                    def on_res(res):
                        count = res if isinstance(res, int) else 0
                        # Return verification results back to both Grid Window and main Dashboard
                        self.view.page().runJavaScript(f"updateGridMatchBadge({idx}, {count});")
                        self._parent_studio.dashboard_view.page().runJavaScript(f"updateItemMatchBadge({idx}, {count});")
                    
                    safe_val = json.dumps(l_val)
                    js = f"window.verifyLiveLocator('{l_type}', {safe_val});"
                    self._parent_studio.browser_ctrl.view.page().runJavaScript(js, on_res)

            elif action == "grid_delete":
                idx = payload.get("idx")
                # Mirror deletion in Python-side locators
                if isinstance(idx, int) and 0 <= idx < len(self.locators):
                    self.locators.pop(idx)
                js = f"deleteElementFromGrid({idx});"
                self._parent_studio.dashboard_view.page().runJavaScript(js)

            elif action == "grid_swap_alt":
                elem_idx = payload.get("elemIdx")
                alt_idx = payload.get("altIdx")
                # Mirror swap in Python-side locators
                if isinstance(elem_idx, int) and 0 <= elem_idx < len(self.locators):
                    el = self.locators[elem_idx]
                    alts = el.get("alternatives", [])
                    if isinstance(alt_idx, int) and 0 <= alt_idx < len(alts):
                        chosen = alts[alt_idx]
                        current = {"type": el.get("type"), "value": el.get("value"), "count": el.get("count")}
                        el["type"] = chosen.get("type")
                        el["value"] = chosen.get("value")
                        el["count"] = chosen.get("count")
                        alts[alt_idx] = current
                js = f"swapAltFromGrid({elem_idx}, {alt_idx});"
                self._parent_studio.dashboard_view.page().runJavaScript(js)

            elif action == "grid_bulk_delete":
                idxs = payload.get("idxs", [])
                # Mirror bulk deletion in Python-side locators (descending to avoid index shift)
                for i in sorted(idxs, reverse=True):
                    if isinstance(i, int) and 0 <= i < len(self.locators):
                        self.locators.pop(i)
                safe_idxs = json.dumps(idxs)
                js = f"bulkDeleteFromGrid({safe_idxs});"
                self._parent_studio.dashboard_view.page().runJavaScript(js)

            elif action == "grid_bulk_set_action":
                idxs = payload.get("idxs", [])
                val_action = payload.get("action")
                safe_idxs = json.dumps(idxs)
                js = f"bulkSetActionFromGrid({safe_idxs}, '{val_action}');"
                self._parent_studio.dashboard_view.page().runJavaScript(js)

            elif action == "grid_ai":
                idx = payload.get("idx")
                if idx >= 0 and idx < len(self.locators):
                    loc = self.locators[idx]
                    
                    # Call Python AI generation in thread
                    def runner():
                        try:
                            ai_locators = self._parent_studio.ai_service.generate_unique_xpath(
                                loc.get("nameHint", "element"),
                                loc.get("outerHtml", ""),
                                self.tool
                            )
                            # Return result back to Grid and main window
                            result = {"elemIdx": idx, "locators": ai_locators}
                            res_json = json.dumps(result)
                            self.view.page().runJavaScript(f"applyAIGridResult({idx}, {res_json});")
                            self._parent_studio.dashboard_view.page().runJavaScript(f"applyAIResult({idx}, {json.dumps(ai_locators)});")
                        except Exception as e:
                            logging.error(f"AI XPath generation failed: {e}")
                            err = json.dumps({"elemIdx": idx, "locators": [], "error": str(e)})
                            self.view.page().runJavaScript(f"applyAIGridError({idx}, {err});")
                            
                    threading.Thread(target=runner, daemon=True).start()

            elif action == "copy_locator":
                idx = payload.get("idx")
                if isinstance(idx, int) and 0 <= idx < len(self.locators):
                    text = self.locators[idx].get("value", "")
                    QApplication.clipboard().setText(text)

            elif action == "grid_export_excel":
                self._parent_studio._handle_js_command("export_to_excel", json.dumps({"locators": self.locators}))

            elif action == "grid_store_db":
                self._parent_studio.dashboard_view.page().runJavaScript("showDbModal();")

        except Exception as e:
            print(f"[GridWindow] _handle_command ERROR: {e}")
            logging.error(f"Error handling GridWindow command {action}: {e}")

class POBridge(QObject):
    """
    Dedicated, isolated QObject bridge for the Gen PO popup window.
    Same pattern as MergeBridge — button events are polled, not signalled.
    """
    def __init__(self):
        super().__init__()
        self._window = None

    @pyqtSlot(str, str)
    def callPython(self, action, payload_str):
        if self._window is not None:
            self._window._handle_command(action, payload_str)


class POWindow(QMainWindow):
    """Dedicated full-screen Page Object code editor popup window."""

    def __init__(self, parent, code, tool, lang, filename, elem_count):
        super().__init__(parent)
        self.setWindowTitle("Gen PO — Page Object Editor")
        self._code       = code
        self._tool       = tool
        self._lang       = lang
        self._filename   = filename
        self._elem_count = elem_count
        self._parent_studio = parent

        # Size: centred, slightly narrower than merge window
        screen = QApplication.primaryScreen().availableGeometry()
        win_w = min(1200, int(screen.width() * 0.72))
        win_h = int(screen.height() * 0.86)
        win_x = screen.x() + (screen.width()  - win_w) // 2
        win_y = screen.y() + (screen.height() - win_h) // 2
        self.setGeometry(win_x, win_y, win_w, win_h)
        self.setMinimumSize(800, 580)

        central = QWidget()
        layout  = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        self.view     = QWebEngineView()
        self.page_obj = WebPage(self.view)
        self.view.setPage(self.page_obj)

        self._bridge         = POBridge()
        self._bridge._window = self
        self.channel         = QWebChannel()
        self.channel.registerObject("pyBridge", self._bridge)
        self.view.page().setWebChannel(self.channel)

        self.view.loadFinished.connect(self._on_load_finished)

        po_path = BASE_DIR / "ui" / "po.html"
        self.view.setUrl(QUrl.fromLocalFile(str(po_path)))

        layout.addWidget(self.view)
        self.setCentralWidget(central)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_js_action)

        QTimer.singleShot(150, self.force_foreground)

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def _on_load_finished(self, ok):
        print(f"[POWindow] loadFinished ok={ok}")
        if ok:
            QTimer.singleShot(500, self._push_data_to_js)
            self._poll_timer.start(200)

    def _push_data_to_js(self):
        try:
            payload = {
                "code":       self._code,
                "tool":       self._tool,
                "lang":       self._lang,
                "filename":   self._filename,
                "elemCount":  self._elem_count
            }
            payload_json = json.dumps(payload)
            self.view.page().runJavaScript(
                f"(function(){{ try{{ loadInitialPOData({payload_json}); return 'ok'; }}"
                f" catch(e){{ return 'ERR:'+e.toString(); }} }})()"
            )
        except Exception as e:
            print(f"[POWindow] _push_data_to_js ERROR: {e}")
            logging.error(f"Error in POWindow._push_data_to_js: {e}")

    # ── Polling ────────────────────────────────────────────────────────────
    def _poll_js_action(self):
        try:
            self.view.page().runJavaScript(
                "typeof getPendingAction === 'function' ? getPendingAction() : null",
                self._on_js_action
            )
        except Exception:
            pass

    def _on_js_action(self, result):
        if not result:
            return
        try:
            data    = json.loads(result)
            action  = data.get("action", "")
            payload = data.get("payload", "")
            if action:
                self._handle_command(action, payload)
        except Exception as e:
            print(f"[POWindow] _on_js_action parse error: {e}")

    # ── Window helpers ─────────────────────────────────────────────────────
    def force_foreground(self):
        self.raise_()
        self.activateWindow()
        if platform.system() == "Windows":
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            self.show()
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint)
            self.show()

    def closeEvent(self, event):
        self._poll_timer.stop()
        super().closeEvent(event)
        if hasattr(self._parent_studio, "po_window"):
            self._parent_studio.po_window = None

    def update_data(self, code, tool, lang, filename, elem_count):
        """Refresh the window with new generated code (re-use existing window)."""
        self._code       = code
        self._tool       = tool
        self._lang       = lang
        self._filename   = filename
        self._elem_count = elem_count
        self._push_data_to_js()

    # ── Command handler ────────────────────────────────────────────────────
    def _handle_command(self, action, payload_str):
        try:
            payload = json.loads(payload_str) if payload_str else {}
            print(f"[POWindow] Command: {action}")

            if action == "po_window_ready":
                self._push_data_to_js()

            elif action == "po_copy_code":
                code = payload.get("code", "")
                QApplication.clipboard().setText(code)

            elif action == "po_save_code":
                content  = payload.get("code", "")
                filename = payload.get("filename", self._filename or "MyPage")
                lang     = payload.get("lang",     self._lang     or "TypeScript")

                ext_map = {"Java": ".java", "Python": ".py", "C#": ".cs",
                           "JavaScript": ".js", "TypeScript": ".ts"}
                ext = ext_map.get(lang, ".txt")

                fname, _ = QFileDialog.getSaveFileName(
                    self, "Save Page Object",
                    f"{filename}{ext}",
                    f"{lang} File (*{ext});;All Files (*)"
                )
                if fname:
                    try:
                        with open(fname, "w", encoding="utf-8") as f:
                            f.write(content)
                        QMessageBox.information(self, "Saved", f"Page Object saved to:\n{fname}")
                    except Exception as e:
                        QMessageBox.critical(self, "Error", f"Could not save file: {e}")

            elif action == "close_po_window":
                self.close()

        except Exception as e:
            print(f"[POWindow] _handle_command ERROR ({action}): {e}")
            logging.error(f"Error handling POWindow command {action}: {e}")



class MergeWindow(QMainWindow):
    def __init__(self, parent, locators, tool, lang, bypass_style=False):
        super().__init__(parent)
        self.setWindowTitle("Smart Merge")
        self.locators = locators
        self.tool = tool
        self.lang = lang
        self.bypass_style = bypass_style
        self._preview_sent = False
        self._parent_studio = parent   # explicit reference for cross-window calls

        # Size the window to match the right browser pane of Locator Studio:
        # screen width minus the left control panel (~320 px), 88% of screen height.
        screen = QApplication.primaryScreen().availableGeometry()
        LEFT_PANEL_W = 320
        win_w = max(900, screen.width() - LEFT_PANEL_W)
        win_h = int(screen.height() * 0.88)
        win_x = screen.x() + LEFT_PANEL_W
        win_y = screen.y() + (screen.height() - win_h) // 2
        self.setGeometry(win_x, win_y, win_w, win_h)
        self.setMinimumSize(860, 620)

        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)

        self.view = QWebEngineView()
        self.page_obj = WebPage(self.view)
        self.view.setPage(self.page_obj)

        # NOTE: QWebChannel is NOT used for button actions (it fails to connect reliably).
        # Buttons use a Python-side polling timer instead (see _start_action_poll).
        # We still keep the channel so JS initBridge() doesn't crash, but buttons
        # go via _pendingAction / getPendingAction() polling.
        self._bridge = MergeBridge()
        self._bridge._window = self
        self.channel = QWebChannel()
        self.channel.registerObject("pyBridge", self._bridge)
        self.view.page().setWebChannel(self.channel)

        # When page finishes loading, send data and start action polling
        self.view.loadFinished.connect(self._on_load_finished)

        merge_path = BASE_DIR / "ui" / "merge.html"
        self.view.setUrl(QUrl.fromLocalFile(str(merge_path)))

        layout.addWidget(self.view)
        self.setCentralWidget(central_widget)

        # Action polling timer — polls JS every 250 ms for button presses
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_js_action)

        # Force foreground on Windows
        QTimer.singleShot(150, self.force_foreground)

    # ------------------------------------------------------------------ #
    #  Data loading                                                         #
    # ------------------------------------------------------------------ #
    def _on_load_finished(self, ok):
        """Called when merge.html finishes loading. Push data and start polling."""
        print(f"[MergeWindow] loadFinished ok={ok}, locators={len(self.locators)}")
        if ok:
            # Give the page 600 ms to initialise, then push data via runJavaScript
            QTimer.singleShot(600, self._push_data_to_js)
            # Start polling for button actions (250 ms interval)
            self._poll_timer.start(250)

    def _push_data_to_js(self):
        """Directly inject all locator data into the JS page via runJavaScript."""
        try:
            title_cl = "".join(
                c for c in self._parent_studio.browser_ctrl.view.page().title()
                if c.isalnum()
            )
            if not title_cl:
                title_cl = "MyPage"
                
            new_code = None
            target_file = ""
            merged_code = ""
            original_code = ""
            
            # Scenario A: Style inheritance
            if not self.bypass_style and self._parent_studio.project_path:
                existing_pos = find_existing_page_objects(self._parent_studio.project_path, self.lang)
                if existing_pos:
                    sample_codes = []
                    for path in existing_pos[:2]:
                        try:
                            with open(path, "r", encoding="utf-8") as f:
                                sample_codes.append(f.read())
                        except Exception:
                            pass
                    if sample_codes and self._parent_studio.ai_api_key:
                        combined_samples = "\n\n--- NEXT SAMPLE ---\n\n".join(sample_codes)
                        new_code = self._parent_studio.ai_service.generate_styled_page_object(
                            self.tool, self.lang, title_cl, self.locators, combined_samples
                        )
                        
                    # Auto-match existing page object file if name matches
                    matching_file = find_matching_page_object(existing_pos, title_cl)
                    if matching_file:
                        try:
                            with open(matching_file, "r", encoding="utf-8") as f:
                                original_code = f.read()
                            target_file = matching_file
                            if self._parent_studio.ai_api_key:
                                merged_code = self._parent_studio.ai_service.merge_locators_with_style(
                                    self.tool, self.lang, original_code, self.locators
                                )
                            else:
                                merged_code = MergeEngine.merge_locators(original_code, self.locators, self.tool, self.lang)
                        except Exception as e:
                            print(f"Error auto-reading matching PO: {e}")

            # Scenario B: Fallbacks
            if not new_code:
                new_code = CodeGenerator.generate_class_content(
                    self.tool, self.lang, title_cl, self.locators
                )

            payload = {
                "elements": self.locators,
                "tool":     self.tool,
                "lang":     self.lang,
                "new_code": new_code,
                "target_file": target_file,
                "merged_code": merged_code,
                "original_code": original_code
            }
            payload_json = json.dumps(payload)
            print(f"[MergeWindow] Pushing {len(self.locators)} elements to JS via runJavaScript")

            def _cb(result):
                print(f"[MergeWindow] loadInitialMergeData JS result: {result}")

            self.view.page().runJavaScript(
                f"(function(){{ try{{ loadInitialMergeData({payload_json}); return 'ok'; }}"
                f" catch(e){{ return 'ERR:'+e.toString(); }} }})()",
                _cb
            )
            self._preview_sent = True
        except Exception as e:
            print(f"[MergeWindow] _push_data_to_js ERROR: {e}")
            logging.error(f"Error in _push_data_to_js: {e}")

    # ------------------------------------------------------------------ #
    #  Action polling (replaces QWebChannel for button events)             #
    # ------------------------------------------------------------------ #
    def _poll_js_action(self):
        """Poll JS every 250 ms for any pending button action."""
        try:
            self.view.page().runJavaScript(
                "typeof getPendingAction === 'function' ? getPendingAction() : null",
                self._on_js_action
            )
        except Exception:
            pass

    def _on_js_action(self, result):
        """Called with the result of getPendingAction() — handle if non-null."""
        if not result:
            return
        try:
            data = json.loads(result)
            action  = data.get("action", "")
            payload = data.get("payload", "")
            if action:
                print(f"[MergeWindow] Polled action: {action}")
                self._handle_command(action, payload)
        except Exception as e:
            print(f"[MergeWindow] _on_js_action parse error: {e}")

    # ------------------------------------------------------------------ #
    #  Window helpers                                                       #
    # ------------------------------------------------------------------ #
    def force_foreground(self):
        self.raise_()
        self.activateWindow()
        if platform.system() == "Windows":
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            self.show()
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint)
            self.show()

    def closeEvent(self, event):
        """Stop the polling timer when the window closes."""
        self._poll_timer.stop()
        super().closeEvent(event)

    def update_data(self, locators, tool, lang, bypass_style=False):
        self.locators = locators
        self.tool = tool
        self.lang = lang
        self.bypass_style = bypass_style
        self._preview_sent = False
        self._push_data_to_js()

    # ------------------------------------------------------------------ #
    #  Command handler                                                      #
    # ------------------------------------------------------------------ #
    def _handle_command(self, action, payload_str):
        """Handle all JS → Python commands (polled or via QWebChannel)."""
        try:
            payload = json.loads(payload_str) if payload_str else {}
            print(f"[MergeWindow] Received command: {action}")

            if action == "merge_window_ready":
                # QWebChannel did connect — push data immediately too
                self._push_data_to_js()

            elif action == "close_merge_window":
                self.close()
                if hasattr(self._parent_studio, "merge_window"):
                    self._parent_studio.merge_window = None

            elif action == "browse_merge_file":
                locators = payload.get("locators", [])
                tool     = payload.get("tool", self.tool)
                lang     = payload.get("lang", self.lang)

                fname, _ = QFileDialog.getOpenFileName(
                    self, "Select existing Page Object file to merge with", "", "All Files (*)"
                )
                if fname:
                    try:
                        with open(fname, "r", encoding="utf-8") as f:
                            current_code = f.read()
                        title_cl = "".join(
                            c for c in self._parent_studio.browser_ctrl.view.page().title()
                            if c.isalnum()
                        )
                        if not title_cl:
                            title_cl = "MyPage"
                            
                        new_code = None
                        merged_code = None
                        
                        # Scenario A: Style inheritance
                        if not self.bypass_style and self._parent_studio.project_path:
                            existing_pos = find_existing_page_objects(self._parent_studio.project_path, lang)
                            if existing_pos:
                                sample_codes = []
                                for path in existing_pos[:2]:
                                    try:
                                        with open(path, "r", encoding="utf-8") as f:
                                            sample_codes.append(f.read())
                                    except Exception:
                                        pass
                                if sample_codes and self._parent_studio.ai_api_key:
                                    combined_samples = "\n\n--- NEXT SAMPLE ---\n\n".join(sample_codes)
                                    new_code = self._parent_studio.ai_service.generate_styled_page_object(
                                        tool, lang, title_cl, locators, combined_samples
                                    )
                                    merged_code = self._parent_studio.ai_service.merge_locators_with_style(
                                        tool, lang, current_code, locators
                                    )
                        
                        # Scenario B: Fallbacks
                        if not new_code:
                            new_code = CodeGenerator.generate_class_content(tool, lang, title_cl, locators)
                        if not merged_code:
                            merged_code = MergeEngine.merge_locators(current_code, locators, tool, lang)

                        result_json = json.dumps({
                            "target_file":   fname,
                            "new_code":      new_code,
                            "merged_code":   merged_code,
                            "original_code": current_code
                        })
                        self.view.page().runJavaScript(
                            f"(function(){{ try{{ loadMergedFile({result_json}); }}"
                            f" catch(e){{ console.error('loadMergedFile error:',e); }} }})()"
                        )
                    except Exception as e:
                        QMessageBox.critical(self, "Merge Error", f"Failed to read file: {e}")

            elif action == "smart_merge_confirm":
                file_path   = payload.get("file_path")
                merged_code = payload.get("merged_code")
                if file_path and merged_code:
                    try:
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(merged_code)
                        QMessageBox.information(self, "Success", "File merged and saved successfully!")
                        QTimer.singleShot(100, self.close)
                        if hasattr(self._parent_studio, "merge_window"):
                            self._parent_studio.merge_window = None
                    except Exception as e:
                        QMessageBox.critical(self, "Error", f"Failed to save merged file: {e}")

        except Exception as e:
            print(f"[MergeWindow] _handle_command ERROR ({action}): {e}")
            logging.error(f"Error handling MergeWindow command {action}: {e}")


class LocatorStudio(QMainWindow):
    def __init__(self, project_path=None, language=None, framework=None, tool=None, app_url=None):
        super().__init__()
        self.setWindowTitle("Locator Studio")
        # Minimum size ensures all panels are usable; app always starts maximized
        self.setMinimumSize(1200, 700)
        self.resize(1600, 900)   # fallback size

        # Make sure ui/libs/qwebchannel.js exists on disk before the dashboard
        # loads — otherwise the dashboard's <script src> 404s and pyBridge is
        # never connected, breaking the capture button (and every other
        # JS->Python action).
        _ensure_qwebchannel_js()
        
        # Cache project context with normalization
        def clean_val(v):
            if not v or v.strip().lower() in ("none", "null", "undefined", "n/a"):
                return None
            return v.strip()

        self.project_path = clean_val(project_path)
        self.project_lang = clean_val(language)
        self.project_fw = clean_val(framework)
        self.project_tool = clean_val(tool)
        self.app_url = clean_val(app_url)
        self.project_name = os.path.basename(self.project_path) if self.project_path else None

        print(f"[Studio] Normalised parameters - path: {self.project_path}, lang: {self.project_lang}, fw: {self.project_fw}, tool: {self.project_tool}, url: {self.app_url}, name: {self.project_name}", flush=True)
        
        # Load environment
        load_dotenv(BASE_DIR.parent / "ScriptGenerator" / ".env")
        
        # Initialize Backend Services
        self.browser_ctrl = BrowserController()
        self.bridge = StudioBridge()
        self.merge_window = None
        self.grid_window  = None
        self.po_window    = None
        
        # Setup AI Service
        self.ai_tool = os.getenv("AI_TOOL", "GEMINI").strip().upper()
        self.ai_model = os.getenv("AI_MODEL", "gemini-1.5-flash").strip().strip('"')
        self.ai_api_key = os.getenv("API_KEY", "").strip().strip('"')
        
        if self.ai_tool in ["OPENAI", "COPILOT"]:
            self.ai_api_url = "https://api.openai.com/v1/chat/completions"
        elif self.ai_tool in ["CLAUDE", "ANTHROPIC"]:
            self.ai_api_url = "https://api.anthropic.com/v1/messages"
        else:
            # Default to Gemini
            self.ai_tool = "GEMINI"
            self.ai_api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.ai_model}:generateContent?key="
            
        self.ai_service = AIService(self.ai_tool, self.ai_model, self.ai_api_key, self.ai_api_url)
        
        # Track whether the project context has been pushed to the dashboard
        # so we don't push it twice (once via loadFinished, once via
        # dashboard_ready). MUST be initialised BEFORE _setup_ui() — that's
        # the call that wires loadFinished, which can fire and read this attr.
        self._context_pushed = False

        # UI Setup
        # Order matters: connect the bridge signal FIRST so any early
        # dashboard_ready callback from JS is not lost, then build the UI
        # (which loads dashboard.html asynchronously).
        self._setup_bridge()
        self._setup_ui()

        # Connect Browser Signals
        self.browser_ctrl.pybridge.locatorsReceived.connect(self._on_elements_captured)
        
        # Force foreground on Windows/OS after launch
        QTimer.singleShot(150, self.force_foreground)

    def force_foreground(self):
        self.raise_()
        self.activateWindow()
        if platform.system() == "Windows":
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            self.show()
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint)
            self.show()

    def _setup_ui(self):
        central_widget = QWidget()
        layout = QHBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setOpaqueResize(False)
        
        # Left Panel: Dashboard UI (HTML/JS)
        self.dashboard_view = QWebEngineView()
        self.dashboard_page = WebPage(self.dashboard_view)
        self.dashboard_view.setPage(self.dashboard_page)
        
        # IMPORTANT: Set up the channel BEFORE loading the URL to avoid race conditions
        self.channel = QWebChannel()
        self.channel.registerObject("pyBridge", self.bridge)
        self.dashboard_view.page().setWebChannel(self.channel)
        
        # Push project context once the dashboard page is fully loaded.
        # This is more reliable than waiting for a JS->Python dashboard_ready
        # callback (which can race with WebChannel initialisation).
        self.dashboard_view.loadFinished.connect(self._on_dashboard_load_finished)

        dashboard_path = BASE_DIR / "ui" / "dashboard.html"
        self.dashboard_view.setUrl(QUrl.fromLocalFile(str(dashboard_path)))

        # Right Panel: Browser View (The Website being inspected)
        self.browser_view = self.browser_ctrl.get_ui_component()
        
        self.splitter.addWidget(self.dashboard_view)
        self.splitter.addWidget(self.browser_view)
        
        # 30% Dashboard, 70% Browser — set explicit ratio + minimum widths for laptop screens
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 7)
        
        # Enforce minimum panel widths so neither collapses on small screens
        self.dashboard_view.setMinimumWidth(380)
        self.browser_view.setMinimumWidth(400)
        
        # Set initial proportional sizes based on 1600px window width
        self.splitter.setSizes([480, 1120])
        
        layout.addWidget(self.splitter)
        self.setCentralWidget(central_widget)

    def _setup_bridge(self):
        # Handle commands from JS
        self.bridge.commandReceived.connect(self._handle_js_command)

    def _on_dashboard_load_finished(self, ok):
        """Push project context once the dashboard HTML has finished loading.

        We can't push immediately on loadFinished because the inline <script>
        block in dashboard.html (which defines setProjectContext) may not have
        executed yet on slower machines — give it a short delay, then push.
        """
        if not ok:
            print("[Studio] dashboard.html failed to load", flush=True)
            return
        print(f"[Studio] dashboard.html loaded; will push context in 250ms", flush=True)
        QTimer.singleShot(250, self._push_project_context)

    def _push_project_context(self):
        """Send tool/lang/url/name to the dashboard JS and auto-navigate the
        right-pane browser to the project URL. Safe to call multiple times —
        it only fires once thanks to the _context_pushed guard."""
        if self._context_pushed:
            return
        self._context_pushed = True

        tool_val    = self.project_tool or ""
        lang_val    = self.project_lang or ""
        app_url_val = self.app_url or ""
        proj_name   = self.project_name or ""

        print(f"[Studio] Pushing project context to dashboard: tool={tool_val!r}, lang={lang_val!r}, url={app_url_val!r}, name={proj_name!r}", flush=True)

        # Wrap in try/catch so we get an actual error message back if
        # setProjectContext throws. runJavaScript's callback receives None
        # on uncaught JS exceptions, which would otherwise hide the cause.
        js_payload = (
            f"(function(){{"
            f"  try {{"
            f"    if (typeof setProjectContext !== 'function') return 'setProjectContext-missing';"
            f"    setProjectContext({json.dumps(tool_val)}, {json.dumps(lang_val)}, {json.dumps(app_url_val)}, {json.dumps(proj_name)});"
            f"    return 'ok';"
            f"  }} catch (e) {{"
            f"    return 'ERR: ' + (e && e.message ? e.message : String(e));"
            f"  }}"
            f"}})()"
        )

        def _cb(result):
            print(f"[Studio] setProjectContext result: {result!r}", flush=True)
            if result == 'setProjectContext-missing':
                # Retry once after 500ms — page JS may still be parsing.
                self._context_pushed = False
                QTimer.singleShot(500, self._push_project_context)

        self.dashboard_view.page().runJavaScript(js_payload, _cb)

        if app_url_val.strip():
            print(f"[Studio] Loading URL in browser pane: {app_url_val}", flush=True)
            self.browser_ctrl.load_url(app_url_val)

    def _on_elements_captured(self, data):
        """Elements captured from browser_controller -> send to Dashboard UI"""
        json_data = json.dumps(data)
        self.bridge.locatorsReceived.emit(json_data)

    def _handle_js_command(self, action, payload_str):
        """Handle actions requested by the Dashboard UI"""
        try:
            payload = json.loads(payload_str) if payload_str else {}
            
            if action == "dashboard_ready":
                # The JS side has confirmed the bridge is up — push context
                # via the shared helper (which is idempotent).
                print("[Studio] dashboard_ready received from JS", flush=True)
                self._push_project_context()
            
            elif action == "launch_url":
                url = payload.get("url")
                self.browser_ctrl.load_url(url)
            
            elif action == "toggle_capture":
                status = payload.get("active", False)
                if status:
                    self.browser_ctrl.start_capturing()
                    # Move keyboard focus to browser panel so backtick key
                    # goes to the Pega page inspector, not the dashboard button
                    QTimer.singleShot(300, lambda: self.browser_view.setFocus())
                else:
                    self.browser_ctrl.stop_capturing()
            
            elif action == "highlight_element":
                l_type = payload.get("type")
                l_val = payload.get("value")
                self.browser_ctrl.highlight_element(l_type, l_val)
            
            elif action == "copy_to_clipboard":
                text = payload.get("text", "")
                QApplication.clipboard().setText(text)

            elif action == "navigate_browser":
                direction = payload.get("direction")
                if direction == "back":
                    self.browser_ctrl.view.back()
                elif direction == "forward":
                    self.browser_ctrl.view.forward()
                elif direction == "reload":
                    self.browser_ctrl.view.reload()

            elif action == "show_message":
                msg = payload.get("message", "")
                QMessageBox.warning(self, "Locator Studio", msg)

            elif action == "verify_locator":
                l_type = payload.get("type")
                l_val = payload.get("value")
                idx = payload.get("idx", -1) # Default to -1 for Live Console
                def on_res(res):
                    count = res if isinstance(res, int) else 0
                    self.bridge.liveVerificationResult.emit(json.dumps({"count": count, "idx": idx}))
                
                safe_val = json.dumps(l_val)
                js = f"window.verifyLiveLocator('{l_type}', {safe_val});"
                self.browser_ctrl.view.page().runJavaScript(js, on_res)

            elif action == "freeze_browser":
                duration = payload.get("duration", 5000)
                js = f"if (typeof window.freezePage === 'function') window.freezePage({duration});"
                self.browser_ctrl.view.page().runJavaScript(js)
                
            elif action == "preview_code":
                # Legacy fallback — also accepted from the dashboard
                # but we now prefer open_po_window which opens the dedicated window.
                # Re-route to open_po_window.
                locators = payload.get("locators", [])
                tool = payload.get("tool", "Playwright")
                lang = payload.get("lang", "TypeScript")
                current_url = payload.get("target_url", "")
                
                # Check for changes in configuration
                changed = []
                if self.project_tool and self.project_tool.lower() != tool.lower():
                    changed.append(f"Tool (Expected: {self.project_tool}, Got: {tool})")
                if self.project_lang and self.project_lang.lower() != lang.lower():
                    changed.append(f"Language (Expected: {self.project_lang}, Got: {lang})")

                proceed = True
                bypass_style = False
                if changed:
                    msg = "The following configuration has changed from the selected project:\n\n"
                    msg += "\n".join(f"- {c}" for c in changed)
                    msg += "\n\nIf you proceed, style inheritance will be disabled and the default fallback generation logic will be used. Do you want to continue?"
                    
                    res = QMessageBox.warning(
                        self, 
                        "Configuration Mismatch Warning", 
                        msg, 
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No
                    )
                    if res == QMessageBox.StandardButton.No:
                        proceed = False
                    else:
                        bypass_style = True

                if proceed:
                    title_cl = "".join(c for c in self.browser_ctrl.view.page().title() if c.isalnum())
                    if not title_cl: title_cl = "MyPage"
                    
                    code = None
                    if not bypass_style and self.project_path:
                        existing_pos = find_existing_page_objects(self.project_path, lang)
                        if existing_pos:
                            sample_codes = []
                            for path in existing_pos[:2]:
                                try:
                                    with open(path, "r", encoding="utf-8") as f:
                                        sample_codes.append(f.read())
                                except Exception:
                                    pass
                            if sample_codes and self.ai_api_key:
                                combined_samples = "\n\n--- NEXT SAMPLE ---\n\n".join(sample_codes)
                                code = self.ai_service.generate_styled_page_object(
                                    tool, lang, title_cl, locators, combined_samples
                                )
                    
                    if not code:
                        code = CodeGenerator.generate_class_content(tool, lang, title_cl, locators)
                    
                    # Open dedicated Gen PO popup window
                    if not hasattr(self, 'po_window') or self.po_window is None:
                        self.po_window = POWindow(self, code, tool, lang, title_cl, len(locators))
                    else:
                        self.po_window.update_data(code, tool, lang, title_cl, len(locators))

                    self.po_window.show()
                    self.po_window.raise_()
                    self.po_window.activateWindow()

            elif action == "save_generated_code":
                content = payload.get("code", "")
                default_filename = payload.get("filename", "MyPage")
                lang = payload.get("lang", "TypeScript")
                
                ext_map = {"Java": ".java", "Python": ".py", "C#": ".cs", "JavaScript": ".js", "TypeScript": ".ts"}
                ext = ext_map.get(lang, ".txt")
                
                fname, _ = QFileDialog.getSaveFileName(self, "Save Page Object", f"{default_filename}{ext}", f"{lang} File (*{ext});;All Files (*)")
                if fname:
                    try:
                        with open(fname, "w", encoding="utf-8") as f:
                            f.write(content)
                        QMessageBox.information(self, "Success", "File saved successfully!")
                    except Exception as e:
                        QMessageBox.critical(self, "Error", f"Could not save file: {e}")

            elif action == "export_to_excel":
                locators = payload.get("locators", [])
                title_cl = "".join(c for c in self.browser_ctrl.view.page().title() if c.isalnum())
                if not title_cl: title_cl = "MyPage"

                fname, _ = QFileDialog.getSaveFileName(self, "Export to Excel", f"{title_cl}_Locators.xlsx", "Excel Files (*.xlsx);;All Files (*)")
                if fname:
                    success = ExcelExporter.export_to_excel(locators, fname)
                    if success:
                        QMessageBox.information(self, "Success", "Excel file saved successfully!")
                    else:
                        QMessageBox.critical(self, "Error", "Failed to export to Excel.")

            elif action == "store_in_db":
                project_name = payload.get("project")
                page_name = payload.get("page")
                locators = payload.get("locators", [])
                tool = payload.get("tool", "Playwright")
                lang = payload.get("lang", "TypeScript")

                db_path = os.path.normpath(BASE_DIR.parent / 'ScriptGenerator' / 'local_database.db')
                
                try:
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()
                    
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS ProjectDetails (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            project_name TEXT NOT NULL,
                            project_path TEXT NOT NULL,
                            project_lang TEXT NOT NULL,
                            project_fw TEXT NOT NULL,
                            project_tool TEXT NOT NULL,
                            package_manager TEXT,
                            project_type TEXT
                        )
                    """)
                    
                    cursor.execute("SELECT id FROM ProjectDetails WHERE project_name=?", (project_name,))
                    res = cursor.fetchone()
                    if res:
                        project_id = res[0]
                    else:
                        cursor.execute("""
                            INSERT INTO ProjectDetails (project_name, project_path, project_lang, project_fw, project_tool)
                            VALUES (?, ?, ?, ?, ?)
                        """, (project_name, "N/A", lang, "N/A", tool))
                        project_id = cursor.lastrowid
                    
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS Locators (
                            ID INTEGER PRIMARY KEY AUTOINCREMENT,
                            Page_Name VARCHAR(255),
                            Locator_Name VARCHAR(255),
                            Locator_Type VARCHAR(255),
                            Method VARCHAR(255),
                            Value VARCHAR(500),
                            Created_On DATETIME,
                            project_id INTEGER,
                            UNIQUE(Page_Name, Locator_Name)
                        )
                    """)
                    
                    try:
                        cursor.execute("ALTER TABLE Locators ADD COLUMN project_id INTEGER")
                    except Exception:
                        pass
                    
                    inserted = 0
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    for loc in locators:
                        try:
                            cursor.execute("""
                                INSERT INTO Locators (Page_Name, Locator_Name, Locator_Type, Method, Value, Created_On, project_id)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            """, (page_name, loc.get("nameHint", "elem"), loc.get("type", ""), loc.get("action", "Click"), loc.get("value", ""), now, project_id))
                            inserted += 1
                        except sqlite3.IntegrityError:
                            pass
                            
                    conn.commit()
                    conn.close()
                    QMessageBox.information(self, "Success", f"Successfully stored {inserted} locators in DB.")
                except Exception as e:
                    QMessageBox.critical(self, "DB Error", str(e))

            elif action == "open_po_window":
                locators = payload.get("locators", [])
                tool     = payload.get("tool", "Playwright")
                lang     = payload.get("lang", "TypeScript")

                title_cl = "".join(c for c in self.browser_ctrl.view.page().title() if c.isalnum())
                if not title_cl: title_cl = "MyPage"

                code = CodeGenerator.generate_class_content(tool, lang, title_cl, locators)

                if not hasattr(self, 'po_window') or self.po_window is None:
                    self.po_window = POWindow(self, code, tool, lang, title_cl, len(locators))
                else:
                    self.po_window.update_data(code, tool, lang, title_cl, len(locators))

                self.po_window.show()
                self.po_window.raise_()
                self.po_window.activateWindow()

            elif action == "open_merge_window":
                locators = payload.get("locators", [])
                tool = payload.get("tool", "Playwright")
                lang = payload.get("lang", "TypeScript")
                current_url = payload.get("target_url", "")
                
                changed = []
                if self.project_tool and self.project_tool.lower() != tool.lower():
                    changed.append(f"Tool (Expected: {self.project_tool}, Got: {tool})")
                if self.project_lang and self.project_lang.lower() != lang.lower():
                    changed.append(f"Language (Expected: {self.project_lang}, Got: {lang})")

                proceed = True
                bypass_style = False
                if changed:
                    msg = "The following configuration has changed from the selected project:\n\n"
                    msg += "\n".join(f"- {c}" for c in changed)
                    msg += "\n\nIf you proceed, style inheritance will be disabled and the default fallback merge logic will be used. Do you want to continue?"
                    
                    res = QMessageBox.warning(
                        self, 
                        "Configuration Mismatch Warning", 
                        msg, 
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No
                    )
                    if res == QMessageBox.StandardButton.No:
                        proceed = False
                    else:
                        bypass_style = True

                if proceed:
                    if not hasattr(self, 'merge_window') or self.merge_window is None:
                        self.merge_window = MergeWindow(self, locators, tool, lang, bypass_style=bypass_style)
                    else:
                        self.merge_window.update_data(locators, tool, lang, bypass_style=bypass_style)

                    # Show at computed size (right-pane-sized, not maximized)
                    self.merge_window.show()
                    self.merge_window.raise_()
                    self.merge_window.activateWindow()

            elif action == "open_grid_window":
                locators = payload.get("locators", [])
                tool = payload.get("tool", "Playwright")
                lang = payload.get("lang", "TypeScript")
                
                if not hasattr(self, 'grid_window') or self.grid_window is None:
                    self.grid_window = GridWindow(self, locators, tool, lang)
                else:
                    self.grid_window.update_data(locators, tool, lang)

                self.grid_window.show()
                self.grid_window.raise_()
                self.grid_window.activateWindow()

            elif action == "request_ai_locators":
                if not self.ai_api_key:
                    # Emit error so dashboard can stop the spinner
                    error_result = json.dumps({"elemIdx": payload.get("elemIdx", -1), "locators": [], "error": "No API key configured in .env"})
                    self.bridge.aiLocatorsReceived.emit(error_result)
                    return
                
                name_hint = payload.get("nameHint", "element")
                outer_html = payload.get("outerHtml", "")
                tool = payload.get("tool", "Playwright")
                elem_idx = payload.get("elemIdx", -1)
                
                def runner():
                    try:
                        ai_locators = self.ai_service.generate_unique_xpath(name_hint, outer_html, tool)
                        result = {"elemIdx": elem_idx, "locators": ai_locators}
                        self.bridge.aiLocatorsReceived.emit(json.dumps(result))
                    except Exception as e:
                        logging.error(f"AI XPath generation failed: {e}")
                        err = json.dumps({"elemIdx": elem_idx, "locators": [], "error": str(e)})
                        self.bridge.aiLocatorsReceived.emit(err)
                        
                threading.Thread(target=runner, daemon=True).start()
                
        except Exception as e:
            logging.error(f"Error handling JS command {action}: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    studio = LocatorStudio()
    studio.showMaximized()   # launch maximized so all panels and buttons are visible
    sys.exit(app.exec())
