import subprocess
import sys
import os
import threading
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

# Import third-party libraries
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QSplitter, QLabel, QLineEdit, QPushButton, 
                             QComboBox, QListWidget, QListWidgetItem, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QAbstractItemView, QDialog, 
                             QTextEdit, QFileDialog, QMessageBox, QAbstractScrollArea,
                             QInputDialog)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer

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
    filename='error.log',
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class MergeDialog(QDialog):
    def __init__(self, new_code: str, tool: str, lang: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Smart Merge Page Object")
        # Set an explicit large size for side-by-side code review
        self.resize(1200, 800)
        self.new_code = new_code
        self.tool = tool
        self.lang = lang
        self.target_file_path = ""
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Top: File Browser
        file_box = QHBoxLayout()
        self.file_path_field = QLineEdit()
        self.file_path_field.setPlaceholderText("Select existing Page Object file to merge with...")
        self.file_path_field.setReadOnly(True)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.browse_file)
        file_box.addWidget(QLabel("Target File:"))
        file_box.addWidget(self.file_path_field)
        file_box.addWidget(browse_btn)
        layout.addLayout(file_box)

        # Middle: Split View
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left Panel (New Code)
        left_box = QVBoxLayout()
        left_box.addWidget(QLabel("<b>New Generated Locators:</b>"))
        self.new_code_editor = QTextEdit()
        self.new_code_editor.setPlainText(self.new_code)
        self.new_code_editor.setFontFamily("Consolas")
        left_box.addWidget(self.new_code_editor)
        left_widget = QWidget()
        left_widget.setLayout(left_box)
        splitter.addWidget(left_widget)
        
        # Right Panel (Target File Content)
        right_box = QVBoxLayout()
        right_box.addWidget(QLabel("<b>Existing File Content:</b>"))
        self.target_code_editor = QTextEdit()
        self.target_code_editor.setFontFamily("Consolas")
        right_box.addWidget(self.target_code_editor)
        right_widget = QWidget()
        right_widget.setLayout(right_box)
        splitter.addWidget(right_widget)
        
        layout.addWidget(splitter)

        # Bottom Buttons
        btn_layout = QHBoxLayout()
        self.merge_btn = QPushButton("Merge New -> Existing (Smart Push)")
        self.merge_btn.setStyleSheet("background-color: #f39c12; color: white; font-weight: bold;")
        self.merge_btn.setDisabled(True)
        self.merge_btn.clicked.connect(self.do_smart_merge)
        
        save_btn = QPushButton("Save Merged File")
        save_btn.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold;")
        save_btn.clicked.connect(self.save_file)
        
        copy_btn = QPushButton("Copy to Clipboard")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.target_code_editor.toPlainText()))
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        
        btn_layout.addWidget(self.merge_btn)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(copy_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def browse_file(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Select existing Page Object file", "", "All Files (*)")
        if fname:
            self.target_file_path = fname
            self.file_path_field.setText(fname)
            try:
                with open(fname, 'r', encoding='utf-8') as f:
                    self.target_code_editor.setPlainText(f.read())
                self.merge_btn.setDisabled(False)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to read file: {e}")

    def do_smart_merge(self):
        # We need the locator list to do a smart merge, or we parse from new_code
        # For simplicity, let's pass a list of locators or re-parse if needed
        # In this implementation, I'll pass the list from MainWindow if possible, 
        # but the merge engine currently takes content. 
        # Actually, let's assume MainWindow passes the locator list to the dialog.
        
        # Retrieve the locator objects from MainWindow (set on dialog externally)
        if not hasattr(self, 'new_locators'):
            QMessageBox.warning(self, "Warning", "No locator data found for smart merge.")
            return

        current_target_code = self.target_code_editor.toPlainText()
        merged_code = MergeEngine.merge_locators(current_target_code, self.new_locators, self.tool, self.lang)
        self.target_code_editor.setPlainText(merged_code)
        QMessageBox.information(self, "Merged", "Smart merge completed using provider-specific logic.")

    def save_file(self):
        if not self.target_file_path:
            # Savename as fallback
            self.target_file_path, _ = QFileDialog.getSaveFileName(self, "Save Merged File", "MergedPage.txt", "All Files (*)")
        
        if self.target_file_path:
            try:
                with open(self.target_file_path, 'w', encoding='utf-8') as f:
                    f.write(self.target_code_editor.toPlainText())
                QMessageBox.information(self, "Success", "File saved successfully!")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save file: {e}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Element Locator Studio (Python Edition)")
        # Force maximize and bring to front
        self.showMaximized()
        self.raise_()
        self.activateWindow()

        # Load keys from the central .env in ScriptGenerator
        base_dir = Path(__file__).resolve().parent
        env_path = base_dir.parent / "ScriptGenerator" / ".env"
        load_dotenv(env_path)
        
        self.ai_tool = os.getenv("AI_TOOL", "GEMINI").strip().upper()
        self.ai_model = os.getenv("AI_MODEL", "").strip().strip('"')
        self.ai_api_key = os.getenv("API_KEY", "").strip().strip('"')
        
        # Default models if not specified
        if not self.ai_model:
            model_defaults = {
                "GEMINI": "gemini-2.5-flash",
                "GOOGLE": "gemini-2.5-flash",
                "OPENAI": "gpt-4o",
                "CLAUDE": "claude-3-5-sonnet-20240620",
                "ANTHROPIC": "claude-3-5-sonnet-20240620",
                "COPILOT": "gpt-4"
            }
            self.ai_model = model_defaults.get(self.ai_tool, "gemini-1.5-flash")

        # Build dynamic URL based on AI_TOOL
        if self.ai_tool in ["GEMINI", "GOOGLE"]:
            self.ai_api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.ai_model}:generateContent?key="
        elif self.ai_tool == "OPENAI":
            self.ai_api_url = "https://api.openai.com/v1/chat/completions"
        elif self.ai_tool in ["CLAUDE", "ANTHROPIC"]:
            self.ai_api_url = "https://api.anthropic.com/v1/messages"
        elif self.ai_tool == "COPILOT":
            self.ai_api_url = "https://api.githubcopilot.com/chat/completions"
        else:
            # Fallback to Gemini format or generic
            self.ai_api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.ai_model}:generateContent?key="

        self.ai_service = AIService(self.ai_tool, self.ai_model, self.ai_api_key, self.ai_api_url)

        self.browser_ctrl = BrowserController()
        # Connect bridge signal to slot
        self.browser_ctrl.pybridge.locatorsReceived.connect(self.process_raw_locators)

        self.locator_queue = [] # List of dicts
        self.current_hover_locators = [] # List of dicts
        self.last_known_page_title = "MyPage"

        self._setup_ui()
        
        # Aggressively force focus after a short delay
        QTimer.singleShot(100, self.force_focus)

    def force_focus(self):
        # On Windows, toggling StaysOnTop often successfully steals focus
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.show() # Make sure it's showing
        self.showMaximized()
        self.raise_()
        self.activateWindow()
        # Remove the pin after a moment
        QTimer.singleShot(1000, self._remove_pin)

    def _remove_pin(self):
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()
        self.showMaximized()

    def _setup_ui(self):
        # Left Panel (Config & Queue)
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        
        # 1. Config Section
        config_label = QLabel("<b>Configuration</b>")
        left_layout.addWidget(config_label)

        url_box = QHBoxLayout()
        self.url_field = QLineEdit()
        self.url_field.setText("")
        url_btn = QPushButton("Launch URL")
        url_btn.clicked.connect(lambda: self.browser_ctrl.load_url(self.url_field.text()))
        url_box.addWidget(QLabel("Target URL:"))
        url_box.addWidget(self.url_field)
        url_box.addWidget(url_btn)
        left_layout.addLayout(url_box)

        combo_box = QHBoxLayout()
        self.tool_combo = QComboBox()
        self.tool_combo.addItems(["Selenium", "Playwright"])
        
        self.lang_combo = QComboBox()
        
        self.tool_combo.currentTextChanged.connect(self.on_tool_changed)
        
        combo_box.addWidget(QLabel("Tool:"))
        combo_box.addWidget(self.tool_combo)
        combo_box.addWidget(QLabel("Lang:"))
        combo_box.addWidget(self.lang_combo)
        left_layout.addLayout(combo_box)

        left_layout.addWidget(QLabel("<b>Locator Priorities (Drag & Drop to Reorder)</b>"))
        
        self.prefs_list = QListWidget()
        self.prefs_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        left_layout.addWidget(self.prefs_list)
        self.on_tool_changed(self.tool_combo.currentText())

        # Capture Section
        cap_box = QHBoxLayout()
        self.toggle_cap_btn = QPushButton("▶")
        self.toggle_cap_btn.setToolTip("Start Capturing Elements")
        self.toggle_cap_btn.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold;")
        self.toggle_cap_btn.clicked.connect(self.toggle_capturing)

        self.freeze_btn = QPushButton("❄")
        self.freeze_btn.setToolTip("Freeze Website for 10s (Capture Dynamic Elements)")
        self.freeze_btn.setStyleSheet("background-color: #6366f1; color: white; font-weight: bold;")
        self.freeze_btn.clicked.connect(self.freeze_website)

        cap_box.addWidget(QLabel("Status:"))
        cap_box.addWidget(self.toggle_cap_btn)
        cap_box.addWidget(self.freeze_btn)
        cap_box.addStretch()
        left_layout.addLayout(cap_box)

        # 2. Queue Section
        left_layout.addWidget(QLabel("<b>PO Queue</b>"))
        self.queue_table = QTableWidget(0, 4)
        self.queue_table.setHorizontalHeaderLabels(["Name", "Type", "Action", ""])
        self.queue_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.queue_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.queue_table.setColumnWidth(3, 30)
        self.queue_table.cellChanged.connect(self._on_queue_cell_changed)
        left_layout.addWidget(self.queue_table)

        btn_box = QHBoxLayout()
        btn_box2 = QHBoxLayout()
        clear_q_btn = QPushButton("Clear Queue")
        clear_q_btn.clicked.connect(self.clear_queue)
        
        merge_btn = QPushButton("Merge to existing")
        merge_btn.setStyleSheet("background-color: #f39c12; color: white; font-weight: bold;")
        merge_btn.clicked.connect(self.open_merge_dialog)
        
        gen_btn = QPushButton("Generate Page Object File")
        gen_btn.setStyleSheet("background-color: #0984e3; color: white; font-weight: bold;")
        gen_btn.clicked.connect(self.generate_code_file)
        
        export_btn = QPushButton("Export to Excel")
        export_btn.setStyleSheet("background-color: #10ac84; color: white; font-weight: bold;")
        export_btn.clicked.connect(self.export_queue)

        store_db_btn = QPushButton("Store Locators in DB")
        store_db_btn.setStyleSheet("background-color: #9b59b6; color: white; font-weight: bold;")
        store_db_btn.clicked.connect(self.store_locators_to_db)

        btn_box.addWidget(clear_q_btn)
        btn_box.addWidget(merge_btn)
        btn_box2.addWidget(gen_btn)
        btn_box2.addWidget(export_btn)
        btn_box2.addWidget(store_db_btn)
        left_layout.addLayout(btn_box)
        left_layout.addLayout(btn_box2)

        # Bottom Panel (Current Locators)
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        
        bot_head = QHBoxLayout()
        bot_head.addWidget(QLabel("<b>Current Element Locators (Double Click row to Add to Queue)</b>"))
        add_sel_btn = QPushButton("Add Selected to PO Queue")
        add_sel_btn.setStyleSheet("background-color: #2ecc71; color: white; font-weight: bold;")
        add_sel_btn.clicked.connect(self.add_selected_to_queue)
        bot_head.addStretch()
        bot_head.addWidget(add_sel_btn)
        bottom_layout.addLayout(bot_head)

        self.cur_loc_table = QTableWidget(0, 5)
        self.cur_loc_table.setHorizontalHeaderLabels(["Name", "Type", "Value", "Quality", "Highlight"])
        self.cur_loc_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.cur_loc_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.cur_loc_table.setColumnWidth(4, 40)
        self.cur_loc_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Enable editing: Single click to edit, or F2. Double click is reserved for Adding to Queue.
        self.cur_loc_table.setEditTriggers(QAbstractItemView.EditTrigger.SelectedClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.cur_loc_table.doubleClicked.connect(self.add_selected_to_queue)
        bottom_layout.addWidget(self.cur_loc_table)

        # Putting it together with Splitters
        right_split = QSplitter(Qt.Orientation.Vertical)
        browser_ui = self.browser_ctrl.get_ui_component()
        right_split.addWidget(browser_ui)
        right_split.addWidget(bottom_widget)
        # 70% Browser, 30% Table
        right_split.setStretchFactor(0, 7)
        right_split.setStretchFactor(1, 3)

        main_split = QSplitter(Qt.Orientation.Horizontal)
        main_split.addWidget(left_widget)
        main_split.addWidget(right_split)
        # 25% Sidebar, 75% Content
        main_split.setStretchFactor(0, 1)
        main_split.setStretchFactor(1, 3)

        self.setCentralWidget(main_split)
        self.apply_dark_theme()

    def on_tool_changed(self, tool):
        self.update_locator_prefs_ui(tool)
        self.update_language_combo(tool)

    def update_language_combo(self, tool):
        current_lang = self.lang_combo.currentText()
        self.lang_combo.clear()
        if tool == "Selenium":
            self.lang_combo.addItems(["Java", "Python", "C#"])
        else:
            self.lang_combo.addItems(["JavaScript", "TypeScript", "Java", "Python"])
        
        # Try to restore previous selection if it's still available
        index = self.lang_combo.findText(current_lang)
        if index >= 0:
            self.lang_combo.setCurrentIndex(index)

    def update_locator_prefs_ui(self, tool):
        self.prefs_list.clear()
        prefs = []
        if tool == "Selenium":
            prefs = ["ID", "Name", "CSS", "XPath", "Link Text", "Partial Link", "Tag Name"]
        else:
            prefs = ["getByTestId", "getByRole", "getByText", "getByLabel", "getByPlaceholder", "getByAltText", "getByTitle", "CSS", "XPath", "ID", "Name", "Semantic"]
        
        for p in prefs:
            item = QListWidgetItem(p)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.prefs_list.addItem(item)

    def toggle_capturing(self):
        if not self.browser_ctrl.is_capturing:
            self.browser_ctrl.start_capturing()
            self.toggle_cap_btn.setText("■")
            self.toggle_cap_btn.setStyleSheet("background-color: #e74c3c; color: white; font-weight: bold;")
            self.toggle_cap_btn.setToolTip("Stop Capturing Elements")
        else:
            self.browser_ctrl.stop_capturing()
            self.toggle_cap_btn.setText("▶")
            self.toggle_cap_btn.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold;")
            self.toggle_cap_btn.setToolTip("Start Capturing Elements")

    def freeze_website(self):
        self.freeze_btn.setDisabled(True)
        self.freeze_btn.setText("...")
        self.browser_ctrl.freeze_page(10000)
        QTimer.singleShot(10000, self._unfreeze_website)

    def _unfreeze_website(self):
        self.freeze_btn.setDisabled(False)
        self.freeze_btn.setText("❄")

    def process_raw_locators(self, data):
        if not data:
            return

        outer_html = data[0].get("outerHtml", "")
        name_hint = data[0].get("nameHint", "elem")
        p_title = data[0].get("pageTitle", "")
        if p_title:
            self.last_known_page_title = p_title

        if self.ai_api_key and outer_html:
            def runner():
                ai_locators = self.ai_service.generate_locators(name_hint, outer_html, self.tool_combo.currentText())
                if ai_locators:
                    self.current_hover_locators = ai_locators
                    # Use QTimer or invokeMethod to update UI back on main thread
                    self._render_current_locators_safe()
            
            threading.Thread(target=runner, daemon=True).start()
            return
            
        self.current_hover_locators = self._filter_locators(data)
        self._render_current_locators()

    def _filter_locators(self, data):
        res = []
        active_prefs = []
        for i in range(self.prefs_list.count()):
            item = self.prefs_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                active_prefs.append(item.text())

        # Give Categories depending on rank
        for i, pref_name in enumerate(active_prefs):
            for item in data:
                t = item.get("type", "")
                if t == pref_name:
                    cat = "Best" if i == 0 else "Good" if i <= 2 else "Ok" if i <= 4 else "Un-Reliable"
                    res.append({
                        "name": item.get("nameHint", "elem"),
                        "type": t,
                        "value": item.get("value", ""),
                        "category": cat,
                        "action": "Click"
                    })
        return res

    def _render_current_locators_safe(self):
        QTimer.singleShot(0, self._render_current_locators)

    def _render_current_locators(self):
        self.cur_loc_table.setRowCount(0)
        for loc in self.current_hover_locators:
            r = self.cur_loc_table.rowCount()
            self.cur_loc_table.insertRow(r)
            
            # 0: Name (Editable)
            self.cur_loc_table.setItem(r, 0, QTableWidgetItem(loc.get("name", "")))
            # 1: Type (Non-editable)
            type_item = QTableWidgetItem(loc.get("type", ""))
            type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.cur_loc_table.setItem(r, 1, type_item)
            # 2: Value (Editable)
            self.cur_loc_table.setItem(r, 2, QTableWidgetItem(loc.get("value", "")))
            # 3: Quality (Non-editable)
            qual_item = QTableWidgetItem(loc.get("category", ""))
            qual_item.setFlags(qual_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.cur_loc_table.setItem(r, 3, qual_item)
            
            # 4: Highlight Button
            focus_btn = QPushButton("🎯")
            focus_btn.setToolTip("Highlight element in webpage")
            focus_btn.setFixedSize(30, 25)
            focus_btn.clicked.connect(lambda checked, row=r: self.highlight_selected(row))
            self.cur_loc_table.setCellWidget(r, 4, focus_btn)
            
            # Store full obj in row user data
            self.cur_loc_table.item(r, 0).setData(Qt.ItemDataRole.UserRole, loc)

    def highlight_selected(self, row):
        l_type = self.cur_loc_table.item(row, 1).text()
        l_val = self.cur_loc_table.item(row, 2).text()
        self.browser_ctrl.highlight_element(l_type, l_val)

    def add_selected_to_queue(self):
        selected = self.cur_loc_table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Selection", "Please select a row first.")
            return
            
        row = selected[0].row()
        # Pull edited values from the table
        ename = self.cur_loc_table.item(row, 0).text()
        etype = self.cur_loc_table.item(row, 1).text()
        evalue = self.cur_loc_table.item(row, 2).text()
        
        orig_data = self.cur_loc_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if orig_data:
            loc = orig_data.copy()
            loc["name"] = ename
            loc["type"] = etype
            loc["value"] = evalue
            loc["action"] = "Click"
            self.locator_queue.append(loc)
            
            # Render in queue table
            r = self.queue_table.rowCount()
            self.queue_table.insertRow(r)
            
            name_item = QTableWidgetItem(loc["name"])
            self.queue_table.setItem(r, 0, name_item)
            
            type_item = QTableWidgetItem(loc["type"])
            type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.queue_table.setItem(r, 1, type_item)
            
            action_combo = QComboBox()
            action_combo.addItems(["Click", "Type", "Clear", "GetText", "SelectByVisibleText", "IsDisplayed"])
            action_combo.currentTextChanged.connect(lambda txt, i=r: self._on_action_changed(i, txt))
            self.queue_table.setCellWidget(r, 2, action_combo)
            
            del_btn = QPushButton("🗑️")
            del_btn.setFixedSize(25, 25)
            del_btn.clicked.connect(lambda checked, row_idx=r: self.remove_queued_item(row_idx))
            self.queue_table.setCellWidget(r, 3, del_btn)

    def open_merge_dialog(self):
        if not self.locator_queue:
            QMessageBox.warning(self, "Empty Queue", "No locators to merge. Add some elements to the PO Queue first.")
            return

        tool = self.tool_combo.currentText()
        lang = self.lang_combo.currentText()
        title_cl = "".join(c for c in self.last_known_page_title if c.isalnum())
        if not title_cl: title_cl = "MyPage"

        content = CodeGenerator.generate_class_content(tool, lang, title_cl, self.locator_queue)
        
        dlg = MergeDialog(content, tool, lang, self)
        dlg.new_locators = self.locator_queue
        dlg.exec()

    def _on_queue_cell_changed(self, row, col):
        if col == 0: # Name changed
            new_name = self.queue_table.item(row, col).text()
            if row < len(self.locator_queue):
                self.locator_queue[row]["name"] = new_name

    def _on_action_changed(self, row, text):
        if row < len(self.locator_queue):
            self.locator_queue[row]["action"] = text

    def clear_queue(self):
        self.locator_queue.clear()
        self.queue_table.setRowCount(0)

    def remove_queued_item(self, row):
        if row < self.queue_table.rowCount():
            self.queue_table.removeRow(row)
            if row < len(self.locator_queue):
                del self.locator_queue[row]
            # Since rows indices change after removal, we need to refresh the connectors for the remaining delete buttons is quite tricky with lambda.
            # A safer way is to just clear and re-render the whole queue table.
            self._refresh_queue_table()

    def _refresh_queue_table(self):
        self.queue_table.setRowCount(0)
        for i, loc in enumerate(self.locator_queue):
            r = self.queue_table.rowCount()
            self.queue_table.insertRow(r)
            self.queue_table.setItem(r, 0, QTableWidgetItem(loc["name"]))
            self.queue_table.setItem(r, 1, QTableWidgetItem(loc["type"]))
            
            action_combo = QComboBox()
            action_combo.addItems(["Click", "Type", "Clear", "GetText", "SelectByVisibleText", "IsDisplayed"])
            action_combo.setCurrentText(loc["action"])
            action_combo.currentTextChanged.connect(lambda txt, idx=r: self._on_action_changed(idx, txt))
            self.queue_table.setCellWidget(r, 2, action_combo)
            
            del_btn = QPushButton("🗑️")
            del_btn.clicked.connect(lambda checked, idx=r: self.remove_queued_item(idx))
            self.queue_table.setCellWidget(r, 3, del_btn)

    def generate_code_file(self):
        if not self.locator_queue:
            QMessageBox.warning(self, "Queue is empty", "Add locators to queue first.")
            return

        tool = self.tool_combo.currentText()
        lang = self.lang_combo.currentText()
        
        title_cl = "".join(c for c in self.last_known_page_title if c.isalnum())
        if not title_cl: title_cl = "MyPage"

        content = CodeGenerator.generate_class_content(tool, lang, title_cl, self.locator_queue)

        dialog = QDialog(self)
        dialog.setWindowTitle("Generated Code Preview")
        dialog.resize(600, 400)
        vbox = QVBoxLayout(dialog)
        text_edit = QTextEdit()
        text_edit.setFontFamily("Consolas")
        text_edit.setPlainText(content)
        vbox.addWidget(text_edit)

        hbox = QHBoxLayout()
        ok_btn = QPushButton("Save")
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        hbox.addWidget(ok_btn)
        hbox.addWidget(cancel_btn)
        vbox.addLayout(hbox)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            final_content = text_edit.toPlainText()
            ext_map = {"Java": ".java", "Python": ".py", "C#": ".cs", "JavaScript": ".js", "TypeScript": ".ts"}
            ext = ext_map.get(lang, ".txt")
            fname, _ = QFileDialog.getSaveFileName(self, "Save Page Object", f"{title_cl}{ext}", f"{lang} File (*{ext});;All Files (*)")
            if fname:
                try:
                    with open(fname, "w", encoding="utf-8") as f:
                        f.write(final_content)
                    QMessageBox.information(self, "Success", "File saved successfully!")
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Could not save file: {e}")

    def export_queue(self):
        if not self.locator_queue:
            QMessageBox.warning(self, "Queue is empty", "Add locators to queue first.")
            return

        title_cl = "".join(c for c in self.last_known_page_title if c.isalnum())
        if not title_cl: title_cl = "MyPage"

        fname, _ = QFileDialog.getSaveFileName(self, "Export to Excel", f"{title_cl}_Locators.xlsx", "Excel Files (*.xlsx);;All Files (*)")
        if fname:
            success = ExcelExporter.export_to_excel(self.locator_queue, fname)
            if success:
                QMessageBox.information(self, "Success", "Excel file saved successfully!")
            else:
                QMessageBox.critical(self, "Error", "Failed to export to Excel.")

    def store_locators_to_db(self):
        if not self.locator_queue:
            QMessageBox.warning(self, "Queue is empty", "Add locators to queue first.")
            return

        page_name, ok = QInputDialog.getText(self, "Page Name", "Enter the page name for these locators:")
        if not ok or not page_name:
            return

        # Path to the shared local database in ScriptGenerator
        db_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'ScriptGenerator', 'local_database.db'))
        
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # Create table if not exists as per requirements
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS Locators (
                    ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    Page_Name VARCHAR(255),
                    Locator_Name VARCHAR(255),
                    Locator_Type VARCHAR(255),
                    Method VARCHAR(255),
                    Value VARCHAR(500),
                    Created_On DATETIME,
                    UNIQUE(Page_Name, Locator_Name)
                )
            """)
            
            inserted_count = 0
            skipped_count = 0
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            for loc in self.locator_queue:
                l_name = loc.get("name", "")
                l_type = loc.get("type", "")
                l_method = loc.get("action", "Click")
                l_value = loc.get("value", "")
                
                try:
                    # Insert values cautiously using parameterized query
                    cursor.execute("""
                        INSERT INTO Locators (Page_Name, Locator_Name, Locator_Type, Method, Value, Created_On)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (page_name, l_name, l_type, l_method, l_value, now))
                    inserted_count += 1
                except sqlite3.IntegrityError:
                    skipped_count += 1
            
            conn.commit()
            conn.close()
            
            msg = f"Successfully stored {inserted_count} locators in DB."
            if skipped_count > 0:
                msg += f"\nSkipped {skipped_count} duplicates (Page Name + Locator Name must be unique)."
            
            QMessageBox.information(self, "Success", msg)
            
        except Exception as e:
            QMessageBox.critical(self, "Database Error", f"Failed to store locators: {e}")

    def apply_dark_theme(self):
        self.setStyleSheet("""
            QWidget { background-color: #2D3436; color: #DFE6E9; font-family: 'Segoe UI', Arial, sans-serif; }
            QLineEdit, QComboBox, QListWidget, QTableWidget, QTextEdit { background-color: #353b48; border: 1px solid #718093; color: white; padding: 4px; }
            QPushButton { background-color: #34495e; border: none; padding: 6px; border-radius: 3px; color: white; }
            QPushButton:hover { background-color: #415A77; }
            QHeaderView::section { background-color: #2d3436; padding: 4px; border: 1px solid #718093; }
        """)


if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        window = MainWindow()
        window.showMaximized()
        sys.exit(app.exec())
    except Exception as e:
        logging.exception("Application crashed during startup or main loop execution:")
        # Optionally show a message box if app object was successfully created
        if 'app' in locals():
            QMessageBox.critical(None, "Application Error", f"The application has crashed. Details logged to error.log.\n\nError: {e}")
        raise e
