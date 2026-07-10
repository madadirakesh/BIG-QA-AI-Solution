import sys
import os
import platform
import json
import re

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


def find_existing_page_objects(project_path, language, limit=5):
    """
    Recursively scan the project for existing Page Object source files and return
    them ranked best-first.

    The result is DETERMINISTIC and quality-ranked so that callers (which usually
    take the first 1-2 files as an AI style reference) always get the most
    representative page objects, producing consistent styling across runs.
    """
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

    EXCLUDE_DIRS = {
        '.venv', 'venv', 'env', '.env', '.git', '.idea', '.vscode',
        '__pycache__', 'node_modules', 'target', 'bin', 'obj', 'dist',
        'build', '.pytest_cache', 'site-packages'
    }
    # Cap how many files we actually open/read so a huge repo can't stall the scan.
    MAX_FILES_TO_READ = 80

    candidates = []  # (score, path)
    files_read = 0
    for root, dirs, files in os.walk(project_path):
        # Prune unwanted directories in-place
        dirs[:] = [d for d in dirs if d.lower() not in EXCLUDE_DIRS]

        # Match on folder-name SEGMENTS relative to the project, NOT a substring of
        # the absolute path. This avoids the bug where an ancestor folder (or the
        # project path itself) containing "page"/"pom" makes every file match.
        try:
            rel_root = os.path.relpath(root, project_path)
        except ValueError:
            rel_root = root
        root_segments = {s.lower() for s in rel_root.replace("\\", "/").split("/") if s and s != "."}
        folder_is_po = any(("page" in seg or "pom" in seg) for seg in root_segments)

        for file in files:
            if not file.endswith(target_ext):
                continue
            file_lower = file.lower()
            stem = file_lower[:-len(target_ext)]
            name_is_po = "page" in file_lower or "pom" in file_lower

            # Only consider files that look like page objects by name or location.
            if not (name_is_po or folder_is_po):
                continue

            if files_read >= MAX_FILES_TO_READ:
                continue

            full_path = os.path.join(root, file)
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read(60000)  # first ~60KB is plenty to judge style
            except Exception:
                continue
            files_read += 1

            content_lower = content.lower()
            # Skip files that aren't real classes/modules (empty stubs, data files).
            if "class " not in content_lower and "export" not in content_lower and "def " not in content_lower:
                continue

            score = 0
            if name_is_po:
                score += 50
            if stem.endswith(("page", "pageobject", "pom", "_po")):
                score += 20
            # A concrete page object that actually defines locators/elements is a
            # far better style reference than an abstract/base class.
            if any(k in content_lower for k in (
                "by.", "locator", "find_element", "findelement", "page.locator",
                "getby", "@findby", "css", "xpath"
            )):
                score += 15
            # Base/abstract classes are still useful but less representative.
            if any(k in stem for k in ("base", "abstract")):
                score -= 12
            # Prefer reasonably-sized files: not empty stubs, not giant god-classes.
            line_count = content.count("\n") + 1
            if 15 <= line_count <= 400:
                score += 10
            elif line_count < 5:
                score -= 20

            candidates.append((score, full_path))

    # Highest score first; path as a stable tie-breaker for deterministic output.
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return [path for _, path in candidates[:limit]]

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


def build_style_samples(project_path, language, max_files=2):
    """
    Discover existing page objects and return a single combined style-reference
    string (the top `max_files` ranked page objects joined together), or "" when
    nothing usable is found. Centralises the logic that every generate/merge path
    used to duplicate.
    """
    if not project_path:
        return ""
    existing_pos = find_existing_page_objects(project_path, language)
    if not existing_pos:
        return ""
    sample_codes = []
    for path in existing_pos[:max_files]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                sample_codes.append(f.read())
        except Exception:
            pass
    return "\n\n--- NEXT SAMPLE ---\n\n".join(sample_codes)


def _lang_tokens(value):
    """Canonicalise a language label into a set of comparable tokens so that
    equivalent spellings ('TypeScript', 'Typescript', 'ts', 'JS / TS') are
    treated as matching and don't spuriously trip the config-mismatch check."""
    if not value:
        return set()
    tokens = set()
    for raw in re.split(r'[^a-z0-9#+.]+', value.lower()):
        if not raw:
            continue
        if raw in ("typescript", "ts", "tsx"):
            tokens.add("ts")
        elif raw in ("javascript", "js", "jsx", "node", "nodejs"):
            tokens.add("js")
        elif raw in ("python", "py"):
            tokens.add("py")
        elif raw in ("java",):
            tokens.add("java")
        elif raw in ("c#", "csharp", "cs", "dotnet", ".net", "net"):
            tokens.add("cs")
        else:
            tokens.add(raw)
    return tokens


def _values_match(expected, actual):
    """True if the two tool/language labels refer to the same thing. A missing
    `expected` (no project context) never counts as a mismatch."""
    if not expected:
        return True
    a, b = _lang_tokens(expected), _lang_tokens(actual)
    if not a or not b:
        return False
    return bool(a & b)


def config_changes(project_tool, project_lang, sel_tool, sel_lang):
    """Return a list of human-readable mismatch descriptions between the project
    context and the user-selected tool/language (empty list == fully compatible)."""
    changed = []
    if not _values_match(project_tool, sel_tool):
        changed.append(f"Tool (Expected: {project_tool}, Got: {sel_tool})")
    if not _values_match(project_lang, sel_lang):
        changed.append(f"Language (Expected: {project_lang}, Got: {sel_lang})")
    return changed
def filter_locators(locators, tool):
    if tool and tool.lower() == "selenium":
        filtered = []
        for loc in locators:
            l_type = loc.get("type", "")
            if l_type.startswith("getBy") or l_type == "Test ID":
                continue
            # Copy to avoid side-effects
            loc_copy = dict(loc)
            if "alternatives" in loc_copy and isinstance(loc_copy["alternatives"], list):
                loc_copy["alternatives"] = [
                    alt for alt in loc_copy["alternatives"]
                    if not (alt.get("type", "").startswith("getBy") or alt.get("type", "") == "Test ID")
                ]
            filtered.append(loc_copy)
        return filtered
    return locators



class StudioBridge(QObject):
    """Bridge for communication between Python and the Dashboard UI"""
    locatorsReceived = pyqtSignal(str) # Send to JS
    aiLocatorsReceived = pyqtSignal(str) # Send AI locators to JS
    mergePreviewReady = pyqtSignal(str) # Send Merge preview to JS
    codePreviewReady = pyqtSignal(str) # Send Generated Code to JS
    liveVerificationResult = pyqtSignal(str) # Send verification result to JS
    urlChanged = pyqtSignal(str)           # Push browser URL changes to JS
    windowOpened = pyqtSignal(str)         # Notify JS a new window/tab opened (JSON: {url, title})
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

            elif action == "grid_gen_po":
                po_payload = {
                    "locators": self.locators,
                    "tool":     self.tool,
                    "lang":     self.lang
                }
                self._parent_studio._handle_js_command("open_po_window", json.dumps(po_payload))

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
                self.raise_()
                self.activateWindow()
                # Clear any pending actions in JS to prevent click-through / event propagation
                self.view.page().runJavaScript(
                    "if (typeof _pendingAction !== 'undefined') { _pendingAction = null; _pendingPayload = null; }"
                )
                if fname:
                    try:
                        with open(fname, "w", encoding="utf-8") as f:
                            f.write(content)
                        saved_msg = f"Page Object saved to:\n{fname}"
                        self.view.page().runJavaScript(f"showLightboxAlert({json.dumps(saved_msg)});")
                    except Exception as e:
                        self.view.page().runJavaScript(f"showLightboxAlert({json.dumps(f'Could not save file: {e}')});")

            elif action == "close_po_window":
                self.close()

        except Exception as e:
            print(f"[POWindow] _handle_command ERROR ({action}): {e}")
            logging.error(f"Error handling POWindow command {action}: {e}")



class MergeWindow(QMainWindow):
    def __init__(self, parent, locators, tool, lang, bypass_style=False):
        super().__init__(parent)
        self.setWindowTitle("Smart Merge")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
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
        # Buttons use a Python-side polling timer instead.
        # We still keep the channel so JS initBridge() doesn't crash, but buttons
        # go via the pending-action queue / getPendingAction() polling.
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
        # If a file-browse dialog is currently open, ignore any OS-initiated
        # close events (macOS sends a closeEvent to the parent QMainWindow when
        # a sheet-modal dialog is dismissed via Cancel / Escape).
        if getattr(self, '_browse_in_progress', False):
            print("[MergeWindow] Ignoring closeEvent — file browse in progress")
            event.ignore()
            return
        self._poll_timer.stop()
        super().closeEvent(event)
        if hasattr(self._parent_studio, "merge_window"):
            self._parent_studio.merge_window = None

    def _select_merge_target_file(self):
        """Open the target-file picker using a single dialog strategy per OS."""
        dialog_title = "Select existing Page Object file to merge with"
        file_filter = "All Files (*)"
        use_qt_dialog = platform.system() == "Linux"

        try:
            if use_qt_dialog:
                dialog = QFileDialog(self, dialog_title, "", file_filter)
                dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
                dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
                dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
                if dialog.exec():
                    files = dialog.selectedFiles()
                    if files:
                        return files[0]
                return ""

            fname, _ = QFileDialog.getOpenFileName(self, dialog_title, "", file_filter)
            return fname or ""
        except Exception as e:
            logging.error(f"Merge file dialog failed: {e}")
            self.view.page().runJavaScript(
                f"showLightboxAlert({json.dumps(f'Could not open file browser: {e}')});"
            )
            return ""

    def _build_merge_preview_payload(self, fname, locators, tool, lang):
        """Generate Smart Merge preview data without letting style/project errors abort the flow."""
        with open(fname, "r", encoding="utf-8") as f:
            current_code = f.read()

        try:
            page_title = self._parent_studio.browser_ctrl.view.page().title()
        except Exception:
            page_title = ""

        title_cl = "".join(c for c in page_title if c.isalnum()) or "MyPage"
        new_code = None
        merged_code = None

        # Try style-aware generation first, but never let it block the file load.
        if not self.bypass_style and self._parent_studio.project_path:
            try:
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
            except Exception as e:
                logging.error(f"Smart Merge style generation failed; falling back to default merge: {e}")

        if not new_code:
            try:
                new_code = CodeGenerator.generate_class_content(tool, lang, title_cl, locators)
            except Exception as e:
                logging.error(f"Smart Merge code generation failed; using current file as preview fallback: {e}")
                new_code = current_code

        if not merged_code:
            try:
                merged_code = MergeEngine.merge_locators(current_code, locators, tool, lang)
            except Exception as e:
                logging.error(f"Smart Merge merge generation failed; using current file as preview fallback: {e}")
                merged_code = current_code

        return {
            "target_file": fname,
            "new_code": new_code,
            "merged_code": merged_code,
            "original_code": current_code
        }

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
                locators = filter_locators(locators, tool)


                # ── Layer 1: Python-side flag ──────────────────────────────
                # Guard closeEvent so any OS-level close event sent to this
                # QMainWindow while the native file dialog is open is ignored.
                self._browse_in_progress = True
                self._poll_timer.stop()

                # ── Layer 2: JS-side guard + disable Cancel button ─────────
                # Runs synchronously in the renderer before QFileDialog blocks,
                # so any phantom click on the HTML Cancel during the OS dialog
                # is a no-op even if the web view receives stray mouse events.
                self.view.page().runJavaScript(
                    "window._browseActive = true;"
                    " var cb = document.getElementById('merge-cancel-btn');"
                    " if (cb) cb.disabled = true;"
                )

                # Use self as parent so the dialog is modal to this window, and focus
                # naturally returns here when dismissed (especially important on macOS).
                fname = self._select_merge_target_file()

                # Explicitly raise and activate the window to ensure it comes to foreground
                # after the file dialog is dismissed.
                self.raise_()
                self.activateWindow()

                # Delay clearing the browse guard and re-enabling the Cancel button by 500ms
                # to prevent click-through and race conditions where the web view processes
                # stray click/mouse-up events from the dismissed native dialog.
                def _restart_poll(_result=None):
                    self._poll_timer.start(250)

                def _re_enable():
                    self._browse_in_progress = False
                    self.view.page().runJavaScript(
                        "window._browseActive = false;"
                        " var cb = document.getElementById('merge-cancel-btn');"
                        " if (cb) cb.disabled = false;"
                        " if (typeof _pendingActions !== 'undefined') { _pendingActions = []; }",
                        _restart_poll
                    )

                QTimer.singleShot(500, _re_enable)
                if fname:
                    self.view.page().runJavaScript(
                        "if (typeof showLoading === 'function') { showLoading('Loading target file preview...'); }"
                    )
                    QApplication.processEvents()

                    def _load_selected_file_preview():
                        try:
                            result_json = json.dumps(
                                self._build_merge_preview_payload(fname, locators, tool, lang)
                            )
                            self.view.page().runJavaScript(
                                f"(function(){{ try{{ loadMergedFile({result_json}); }}"
                                f" catch(e){{ console.error('loadMergedFile error:',e); }} }})()"
                            )
                        except Exception as e:
                            logging.error(f"Smart Merge browse flow failed for {fname}: {e}")
                            self.view.page().runJavaScript(
                                f"if (typeof hideLoading === 'function') hideLoading();"
                                f"showLightboxAlert({json.dumps(f'Failed to load selected file: {e}')});"
                            )

                    QTimer.singleShot(0, _load_selected_file_preview)
                else:
                    self.view.page().runJavaScript(
                        "if (typeof hideLoading === 'function') { hideLoading(); }"
                    )

            elif action == "refresh_merge_preview":
                locators = payload.get("locators", [])
                tool     = payload.get("tool", self.tool)
                lang     = payload.get("lang", self.lang)
                file_path = payload.get("file_path", "")
                locators = filter_locators(locators, tool)

                bypass_style = payload.get("bypass_style", self.bypass_style)
                QApplication.processEvents()

                def _refresh_merge_preview():
                    title_cl = "".join(
                        c for c in self._parent_studio.browser_ctrl.view.page().title()
                        if c.isalnum()
                    )
                    if not title_cl:
                        title_cl = "MyPage"

                    new_code = None
                    merged_code = None
                    current_code = ""

                    if file_path:
                        try:
                            with open(file_path, "r", encoding="utf-8") as f:
                                current_code = f.read()
                        except Exception as e:
                            print(f"Error reading file for preview: {e}")

                    if not bypass_style and self._parent_studio.project_path:
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
                                if file_path:
                                    merged_code = self._parent_studio.ai_service.merge_locators_with_style(
                                        tool, lang, current_code, locators
                                    )

                    if not new_code:
                        new_code = CodeGenerator.generate_class_content(tool, lang, title_cl, locators)
                    if file_path and not merged_code:
                        merged_code = MergeEngine.merge_locators(current_code, locators, tool, lang)

                    result_json = json.dumps({
                        "target_file":   file_path,
                        "new_code":      new_code,
                        "merged_code":   merged_code,
                        "original_code": current_code
                    })
                    self.view.page().runJavaScript(
                        f"(function(){{ try{{ loadMergedFile({result_json}); }} catch(e){{ console.error('loadMergedFile error:',e); }} }})()"
                    )

                QTimer.singleShot(0, _refresh_merge_preview)

            elif action == "smart_merge_confirm":
                file_path   = payload.get("file_path")
                merged_code = payload.get("merged_code")
                if file_path and merged_code:
                    try:
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(merged_code)
                        self.view.page().runJavaScript(f"showLightboxAlert('File merged and saved successfully!');")
                        QTimer.singleShot(1500, self.close)
                        if hasattr(self._parent_studio, "merge_window"):
                            self._parent_studio.merge_window = None
                    except Exception as e:
                        self.view.page().runJavaScript(f"showLightboxAlert({json.dumps(f'Failed to save merged file: {e}')});")

        except Exception as e:
            print(f"[MergeWindow] _handle_command ERROR ({action}): {e}")
            logging.error(f"Error handling MergeWindow command {action}: {e}")
            self.view.page().runJavaScript(
                "if (typeof hideLoading === 'function') { hideLoading(); }"
            )


class SessionWatcher(QObject):
    """Long-polls the web app's heartbeat; emits sessionLost when the session ends."""
    sessionLost = pyqtSignal()

    def __init__(self, server_url, token, parent=None):
        super().__init__(parent)
        self.server_url = (server_url or "").rstrip('/')
        self.token = token or ""
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        import time
        import urllib.request
        url = f"{self.server_url}/api/locator-heartbeat?token={self.token}"
        fail_count = 0
        while self._running:
            reachable = True
            started = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=40) as resp:
                    active = bool(json.loads(resp.read().decode('utf-8')).get('active'))
            except Exception:
                reachable = False
                active = False
            if not self._running:
                return
            if active:
                fail_count = 0
                elapsed = time.monotonic() - started
                if elapsed < 2:
                    time.sleep(2 - elapsed)
                continue
            if reachable:
                self.sessionLost.emit()
                return
            fail_count += 1
            if fail_count >= 2:
                self.sessionLost.emit()
                return
            time.sleep(2)


class LocatorStudio(QMainWindow):
    def __init__(self, project_path=None, language=None, framework=None, tool=None, app_url=None,
                 auth_server=None, auth_token=None):
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
        self.locators     = []
        
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
        # New window/tab opened by an action -> let the dashboard add a switch step
        self.browser_ctrl.windowOpened.connect(self._on_window_opened)

        # Fix #7: wire the dashboard view into the browser controller so it can
        # call window.onBrowserUrlChanged() when the user navigates back/forward.
        self.browser_ctrl._dashboard_view = self.dashboard_view
        
        # Force foreground on Windows/OS after launch
        QTimer.singleShot(150, self.force_foreground)

        # Watch the launching web session; close the studio when it logs out / shuts down.
        self.session_watcher = None
        if auth_server and auth_token:
            self.session_watcher = SessionWatcher(auth_server, auth_token, self)
            self.session_watcher.sessionLost.connect(self._on_session_lost)
            self.session_watcher.start()

    def _on_session_lost(self):
        if getattr(self, '_session_closing', False):
            return
        self._session_closing = True
        QApplication.quit()

    def force_foreground(self):
        self.raise_()
        self.activateWindow()
        if platform.system() == "Windows":
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
            self.show()
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint)
            self.show()

    def show_lightbox_alert(self, message):
        """Show a lightbox alert in the active/visible HTML view."""
        # Find which view is currently visible and active
        target_view = self.dashboard_view
        if self.grid_window and self.grid_window.isVisible():
            target_view = self.grid_window.view
        elif self.po_window and self.po_window.isVisible():
            target_view = self.po_window.view
        elif self.merge_window and self.merge_window.isVisible():
            target_view = self.merge_window.view
            
        target_view.page().runJavaScript(f"showLightboxAlert({json.dumps(message)});")

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
        filtered_data = filter_locators(data, self.project_tool)
        self.locators.extend(filtered_data)
        json_data = json.dumps(filtered_data)
        self.bridge.locatorsReceived.emit(json_data)


    def _on_window_opened(self, url, title):
        """A new window/tab was opened by an action -> tell the dashboard so it
        can add a (de-duplicated) 'switch to window' step."""
        try:
            self.bridge.windowOpened.emit(json.dumps({"url": url, "title": title}))
        except Exception as e:
            print(f"[Studio] _on_window_opened error: {e}", flush=True)

    def _handle_js_command(self, action, payload_str):
        """Handle actions requested by the Dashboard UI"""
        try:
            payload = json.loads(payload_str) if payload_str else {}
            
            if action == "dashboard_ready":
                # The JS side has confirmed the bridge is up — push context
                # via the shared helper (which is idempotent).
                print("[Studio] dashboard_ready received from JS", flush=True)
                self._push_project_context()
            
            elif action == "tool_changed":
                self.project_tool = payload.get("tool", self.project_tool)

            
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
                self.show_lightbox_alert(msg)

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
                
                locators = filter_locators(locators, tool)

                
                # Check for changes in configuration
                changed = config_changes(self.project_tool, self.project_lang, tool, lang)

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

                # Issue #13: Windows associates .ts with MPEG-TS video.
                # For TypeScript, add a .txt alternative in the dialog filter so users
                # can save as plain text and open safely in their code editor.
                if lang == "TypeScript":
                    file_filter = "TypeScript Files (*.ts);;Text Files (*.txt);;All Files (*)"
                else:
                    file_filter = f"{lang} File (*{ext});;All Files (*)"
                
                fname, _ = QFileDialog.getSaveFileName(self, "Save Page Object", f"{default_filename}{ext}", file_filter)
                if fname:
                    try:
                        with open(fname, "w", encoding="utf-8") as f:
                            f.write(content)
                        if lang == "TypeScript" and fname.endswith(".ts"):
                            self.show_lightbox_alert(
                                f"File saved successfully!\n\nPath: {fname}\n\n"
                                "⚠️ Note: Windows may open .ts files in a media player.\n"
                                "Right-click → Open With → your code editor (VS Code, Notepad++, etc.)"
                            )
                        else:
                            self.show_lightbox_alert("File saved successfully!")
                    except Exception as e:
                        self.show_lightbox_alert(f"Could not save file: {e}")

            elif action == "export_to_excel":
                locators = payload.get("locators", [])
                locators = filter_locators(locators, self.project_tool)
                title_cl = "".join(c for c in self.browser_ctrl.view.page().title() if c.isalnum())
                if not title_cl: title_cl = "MyPage"

                fname, _ = QFileDialog.getSaveFileName(self, "Export to Excel", f"{title_cl}_Locators.xlsx", "Excel Files (*.xlsx);;All Files (*)")
                if fname:
                    success = ExcelExporter.export_to_excel(locators, fname)
                    if success:
                        self.show_lightbox_alert("Excel file saved successfully!")
                    else:
                        self.show_lightbox_alert("Failed to export to Excel.")

            elif action == "store_in_db":
                project_name = payload.get("project")
                page_name = payload.get("page")
                locators = payload.get("locators", [])
                tool = payload.get("tool", "Playwright")
                lang = payload.get("lang", "TypeScript")
                locators = filter_locators(locators, tool)


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
                    self.show_lightbox_alert(f"Successfully stored {inserted} locators in DB.")
                except Exception as e:
                    self.show_lightbox_alert(f"DB Error: {e}")

            elif action == "open_po_window":
                locators = payload.get("locators", [])
                tool     = payload.get("tool", "Playwright")
                lang     = payload.get("lang", "TypeScript")
                locators = filter_locators(locators, tool)
                self.locators = locators


                # Warn (and disable style inheritance) only on a genuine
                # tool/language mismatch with the selected project.
                changed = config_changes(self.project_tool, self.project_lang, tool, lang)
                proceed = True
                bypass_style = False
                if changed:
                    if payload.get("bypass_style", False):
                        bypass_style = True
                    else:
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

                if not proceed:
                    return

                title_cl = "".join(c for c in self.browser_ctrl.view.page().title() if c.isalnum())
                if not title_cl: title_cl = "MyPage"

                # Scenario A: style inheritance from existing project page objects.
                code = None
                if not bypass_style and self.project_path:
                    combined_samples = build_style_samples(self.project_path, lang)
                    if combined_samples and self.ai_api_key:
                        code = self.ai_service.generate_styled_page_object(
                            tool, lang, title_cl, locators, combined_samples
                        )
                        if not code:
                            print("[open_po_window] Styled generation returned empty; using fallback generator.", flush=True)

                # Scenario B: fallback to the template generator.
                if not code:
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
                locators = filter_locators(locators, tool)
                self.locators = locators


                changed = config_changes(self.project_tool, self.project_lang, tool, lang)

                proceed = True
                bypass_style = False
                if changed:
                    if payload.get("bypass_style", False):
                        bypass_style = True
                    else:
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
                    if (
                        not hasattr(self, 'merge_window')
                        or self.merge_window is None
                        or not self.merge_window.isVisible()
                    ):
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
                locators = filter_locators(locators, tool)
                self.locators = locators

                
                if not hasattr(self, 'grid_window') or self.grid_window is None:
                    self.grid_window = GridWindow(self, locators, tool, lang)
                else:
                    self.grid_window.update_data(locators, tool, lang)

                self.grid_window.show()
                self.grid_window.raise_()
                self.grid_window.activateWindow()

            elif action == "edit_element":
                idx = payload.get("idx")
                field = payload.get("field")
                value = payload.get("value")
                if isinstance(idx, int) and 0 <= idx < len(self.locators):
                    self.locators[idx][field] = value
                    if field == 'nameHint':
                        self.locators[idx]['name'] = value
                    if getattr(self, "grid_window", None):
                        self.grid_window.update_data(self.locators, self.project_tool, self.project_lang)

            elif action == "delete_element":
                idx = payload.get("idx")
                if isinstance(idx, int) and 0 <= idx < len(self.locators):
                    self.locators.pop(idx)
                    if getattr(self, "grid_window", None):
                        self.grid_window.update_data(self.locators, self.project_tool, self.project_lang)

            elif action == "swap_element_alt":
                elem_idx = payload.get("elemIdx")
                alt_idx = payload.get("altIdx")
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
                        if getattr(self, "grid_window", None):
                            self.grid_window.update_data(self.locators, self.project_tool, self.project_lang)

            elif action == "reorder_elements":
                old_idx = payload.get("oldIdx")
                new_idx = payload.get("newIdx")
                if isinstance(old_idx, int) and isinstance(new_idx, int):
                    if 0 <= old_idx < len(self.locators) and 0 <= new_idx < len(self.locators):
                        item = self.locators.pop(old_idx)
                        self.locators.insert(new_idx, item)
                        if getattr(self, "grid_window", None):
                            self.grid_window.update_data(self.locators, self.project_tool, self.project_lang)

            elif action == "clear_elements":
                self.locators = []
                if getattr(self, "grid_window", None):
                    self.grid_window.update_data(self.locators, self.project_tool, self.project_lang)

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
