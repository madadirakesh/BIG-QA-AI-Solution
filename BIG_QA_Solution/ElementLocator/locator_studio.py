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
from PyQt6.QtCore import Qt, QUrl, pyqtSlot, QObject, pyqtSignal, QTimer
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


class MergeWindow(QMainWindow):
    def __init__(self, parent, locators, tool, lang):
        super().__init__(parent)
        self.setWindowTitle("Smart Merge")
        self.locators = locators
        self.tool = tool
        self.lang = lang
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
            new_code = CodeGenerator.generate_class_content(
                self.tool, self.lang, title_cl, self.locators
            )
            payload = {
                "elements": self.locators,
                "tool":     self.tool,
                "lang":     self.lang,
                "new_code": new_code,
                "target_file": "",
                "merged_code": ""
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

    def update_data(self, locators, tool, lang):
        self.locators = locators
        self.tool = tool
        self.lang = lang
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
                        new_code    = CodeGenerator.generate_class_content(tool, lang, title_cl, locators)
                        merged_code = MergeEngine.merge_locators(current_code, locators, tool, lang)

                        # Push result directly via runJavaScript — no signal needed
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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Locator Studio")
        # Minimum size ensures all panels are usable; app always starts maximized
        self.setMinimumSize(1200, 700)
        self.resize(1600, 900)   # fallback size
        
        # Load environment
        load_dotenv(BASE_DIR.parent / "ScriptGenerator" / ".env")
        
        # Initialize Backend Services
        self.browser_ctrl = BrowserController()
        self.bridge = StudioBridge()
        self.merge_window = None
        
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
        
        # UI Setup
        self._setup_ui()
        self._setup_bridge()
        
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

    def _on_elements_captured(self, data):
        """Elements captured from browser_controller -> send to Dashboard UI"""
        json_data = json.dumps(data)
        self.bridge.locatorsReceived.emit(json_data)

    def _handle_js_command(self, action, payload_str):
        """Handle actions requested by the Dashboard UI"""
        try:
            payload = json.loads(payload_str) if payload_str else {}
            
            if action == "launch_url":
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
                locators = payload.get("locators", [])
                tool = payload.get("tool", "Playwright")
                lang = payload.get("lang", "TypeScript")
                
                title_cl = "".join(c for c in self.browser_ctrl.view.page().title() if c.isalnum())
                if not title_cl: title_cl = "MyPage"
                
                content = CodeGenerator.generate_class_content(tool, lang, title_cl, locators)
                
                response = {
                    "code": content,
                    "default_filename": title_cl,
                    "lang": lang
                }
                self.bridge.codePreviewReady.emit(json.dumps(response))

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

            elif action == "open_merge_window":
                locators = payload.get("locators", [])
                tool = payload.get("tool", "Playwright")
                lang = payload.get("lang", "TypeScript")
                
                if not hasattr(self, 'merge_window') or self.merge_window is None:
                    self.merge_window = MergeWindow(self, locators, tool, lang)
                else:
                    self.merge_window.update_data(locators, tool, lang)

                # Show at computed size (right-pane-sized, not maximized)
                self.merge_window.show()
                self.merge_window.raise_()
                self.merge_window.activateWindow()

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
