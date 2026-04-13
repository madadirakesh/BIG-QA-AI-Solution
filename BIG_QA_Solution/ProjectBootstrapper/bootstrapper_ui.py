import os
import sys
import threading
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QLineEdit, QComboBox, QPushButton, QMessageBox, 
                             QFormLayout, QGroupBox, QTextEdit, QFileDialog)
from PyQt6.QtCore import pyqtSignal, QTimer, Qt

# Ensure we can import our engine
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from bootstrapper_engine import BootstrapperEngine
from environment_setup import EnvironmentSetup

class BootstrapperUI(QWidget):
    def __init__(self):
        super().__init__()
        self._setup_ui()
        self.update_combinations()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Basic Info
        info_group = QGroupBox("Basic Configuration")
        info_layout = QFormLayout()
        self.project_name_field = QLineEdit()
        self.project_name_field.setText("MyQAAutomationProject")
        
        path_box = QHBoxLayout()
        self.path_field = QLineEdit()
        self.path_field.setText(os.path.expanduser("~/Documents"))
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_path)
        path_box.addWidget(self.path_field)
        path_box.addWidget(browse_btn)

        self.url_field = QLineEdit()
        self.url_field.setText("https://example.com/login")
        self.username_field = QLineEdit()
        self.password_field = QLineEdit()
        self.password_field.setEchoMode(QLineEdit.EchoMode.Password)

        info_layout.addRow("Project Name:", self.project_name_field)
        info_layout.addRow("Save Location:", path_box)
        info_layout.addRow("Application URL:", self.url_field)
        info_layout.addRow("Username:", self.username_field)
        info_layout.addRow("Password:", self.password_field)
        info_group.setLayout(info_layout)
        layout.addWidget(info_group)

        # Tech Stack Selection
        stack_group = QGroupBox("Technology Stack")
        stack_layout = QFormLayout()

        self.tool_combo = QComboBox()
        self.tool_combo.addItems(["Selenium", "Playwright"])
        self.tool_combo.currentTextChanged.connect(self.update_combinations)

        self.lang_combo = QComboBox()
        self.lang_combo.currentTextChanged.connect(self.update_frameworks)

        self.fw_combo = QComboBox()
        self.pm_combo = QComboBox()

        stack_layout.addRow("Tool:", self.tool_combo)
        stack_layout.addRow("Language:", self.lang_combo)
        stack_layout.addRow("Framework (BDD):", self.fw_combo)
        stack_layout.addRow("Package Manager:", self.pm_combo)
        stack_group.setLayout(stack_layout)
        layout.addWidget(stack_group)

        # Generate Button
        self.generate_btn = QPushButton("Generate Framework")
        self.generate_btn.setStyleSheet("background-color: #0984e3; color: white; font-weight: bold; font-size: 16px; padding: 10px;")
        self.generate_btn.clicked.connect(self.generate_project)
        layout.addWidget(self.generate_btn)

        # Log Output
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFontFamily("Consolas")
        layout.addWidget(QLabel("Logs / Environment Setup:"))
        layout.addWidget(self.log_output)

    def browse_path(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Directory to Generate Project", self.path_field.text())
        if directory:
            self.path_field.setText(directory)

    def update_combinations(self):
        tool = self.tool_combo.currentText()
        self.lang_combo.clear()
        if tool == "Selenium":
            self.lang_combo.addItems(["Java", "Python", "C#"])
        elif tool == "Playwright":
            self.lang_combo.addItems(["Java", "Python", "JS / TS"])

    def update_frameworks(self):
        tool = self.tool_combo.currentText()
        lang = self.lang_combo.currentText()
        self.fw_combo.clear()
        self.pm_combo.clear()

        if lang == "Java":
            self.fw_combo.addItem("Cucumber")
            self.pm_combo.addItem("Maven (pom.xml)")
        elif lang == "Python":
            self.fw_combo.addItem("Jbehave / Behave")
            self.pm_combo.addItem("Pip (requirements.txt)")
        elif lang == "C#":
            self.fw_combo.addItem("Reqnroll")
            self.pm_combo.addItem("NuGet")
        elif lang == "JS / TS":
            self.fw_combo.addItem("Cucumber (JS)")
            self.pm_combo.addItem("NPM (package.json)")

    def log(self, message):
        # We need to dispatch to UI thread if we are in a background thread
        QTimer.singleShot(0, lambda: self.log_output.append(message))

    def generate_project(self):
        p_name = self.project_name_field.text().strip()
        p_path = self.path_field.text().strip()
        tool = self.tool_combo.currentText()
        lang = self.lang_combo.currentText()
        fw = self.fw_combo.currentText()
        pm = self.pm_combo.currentText()
        url = self.url_field.text().strip()
        user = self.username_field.text().strip()
        pwd = self.password_field.text().strip()

        if not p_name or not p_path:
            QMessageBox.warning(self, "Validation Error", "Project name and path are required.")
            return

        self.generate_btn.setDisabled(True)
        self.log("=========================================")
        self.log(f"Starting Scaffolding for {tool} + {lang} ...")
        
        # Check environment first
        env_ok, missing = EnvironmentSetup.verify_environment(lang)
        if not env_ok:
            self.log(f"[WARNING] Missing system dependencies: {', '.join(missing)}.")
            self.log("You might need to install them manually after generation.")
            reply = QMessageBox.question(self, 'Missing Dependencies', 
                                         f"System is missing: {', '.join(missing)}.\nDo you want to continue project generation anyway?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                self.generate_btn.setDisabled(False)
                return
        else:
            self.log("[OK] Pre-requisite system dependencies verified.")

        # Background thread for generation and installation
        threading.Thread(target=self._run_generation_thread, 
                         args=(p_name, p_path, tool, lang, fw, pm, url, user, pwd, env_ok), 
                         daemon=True).start()

    def _run_generation_thread(self, p_name, p_path, tool, lang, fw, pm, url, user, pwd, env_ok):
        success, res = BootstrapperEngine.generate_project(p_name, p_path, tool, lang, fw, pm, url, user, pwd)
        if not success:
            self.log(f"[ERROR] Scaffolding failed: {res}")
            QTimer.singleShot(0, lambda: self.generate_btn.setDisabled(False))
            return

        target_dir = res
        self.log(f"[SUCCESS] Code generation complete. Directory: {target_dir}")
        self.log("Starting background dependency injection...")

        inst_success, inst_msg = EnvironmentSetup.install_project_dependencies(target_dir, pm)
        if inst_success:
            self.log("[SUCCESS] Dependencies installed successfully.")
            if inst_msg:
                self.log(f"Output: {inst_msg[:200]}...") # truncated output to avoid freeze
        else:
            self.log("[ERROR] Dependency installation failed:")
            self.log(inst_msg)
            
        self.log("=========================================")
        self.log("Running Sanity Check (Smoke Test)...")
        smoke_ok, smoke_msg = BootstrapperEngine.execute_smoke_test(target_dir, lang, pm)
        if smoke_ok:
            self.log("[SUCCESS] Smoke Test Passed.")
            self.log(f"Test Output: {smoke_msg[:200]}...")
        else:
            self.log("[ERROR] Smoke Test Failed.")
            self.log(f"Test Output: {smoke_msg}")

        QTimer.singleShot(0, lambda: self.generate_btn.setDisabled(False))
        
        msg = "Project Scaffolding Complete!"
        if env_ok and inst_success and smoke_ok:
            msg += "\nAll checks passed. Ready to start scripting!"
        QTimer.singleShot(0, lambda: QMessageBox.information(self, "Success", msg))
