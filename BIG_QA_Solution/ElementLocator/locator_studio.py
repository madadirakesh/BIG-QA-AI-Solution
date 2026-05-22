import sys
import os
import json
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

class LocatorStudio(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Locator Studio")
        self.resize(1600, 900)
        
        # Load environment
        load_dotenv(BASE_DIR.parent / "ScriptGenerator" / ".env")
        
        # Initialize Backend Services
        self.browser_ctrl = BrowserController()
        self.bridge = StudioBridge()
        
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

    def _setup_ui(self):
        central_widget = QWidget()
        layout = QHBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left Panel: Dashboard UI (HTML/JS)
        self.dashboard_view = QWebEngineView()
        
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
        
        # 30% Dashboard, 70% Browser
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 7)
        
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

            elif action == "init_merge_modal":
                locators = payload.get("locators", [])
                tool = payload.get("tool", "Playwright")
                lang = payload.get("lang", "TypeScript")
                
                title_cl = "".join(c for c in self.browser_ctrl.view.page().title() if c.isalnum())
                if not title_cl: title_cl = "MyPage"
                new_code = CodeGenerator.generate_class_content(tool, lang, title_cl, locators)
                
                self.bridge.mergePreviewReady.emit(json.dumps({
                    "target_file": "",
                    "new_code": new_code,
                    "merged_code": ""
                }))

            elif action == "browse_merge_file":
                locators = payload.get("locators", [])
                tool = payload.get("tool", "Playwright")
                lang = payload.get("lang", "TypeScript")
                
                fname, _ = QFileDialog.getOpenFileName(self, "Select existing Page Object file to merge with", "", "All Files (*)")
                if fname:
                    try:
                        with open(fname, 'r', encoding='utf-8') as f:
                            current_code = f.read()
                            
                        title_cl = "".join(c for c in self.browser_ctrl.view.page().title() if c.isalnum())
                        if not title_cl: title_cl = "MyPage"
                        new_code = CodeGenerator.generate_class_content(tool, lang, title_cl, locators)
                        
                        merged_code = MergeEngine.merge_locators(current_code, locators, tool, lang)
                        
                        self.bridge.mergePreviewReady.emit(json.dumps({
                            "target_file": fname,
                            "new_code": new_code,
                            "merged_code": merged_code
                        }))
                    except Exception as e:
                        QMessageBox.critical(self, "Merge Error", f"Failed to read file: {e}")

            elif action == "smart_merge_confirm":
                file_path = payload.get("file_path")
                merged_code = payload.get("merged_code")
                if file_path and merged_code:
                    try:
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(merged_code)
                        QMessageBox.information(self, "Success", "File merged and saved successfully!")
                    except Exception as e:
                        QMessageBox.critical(self, "Error", f"Failed to save merged file: {e}")

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
    # Apply global styling if needed, or rely on HTML/CSS
    studio = LocatorStudio()
    studio.show()
    sys.exit(app.exec())
