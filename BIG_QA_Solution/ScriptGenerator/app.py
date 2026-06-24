import os
import sys
import json
import subprocess
import threading
import webbrowser
import uuid
import secrets
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response, stream_with_context
import pytz
from dotenv import load_dotenv
from scripts.deploy_team_templates import seed

# Path setup
BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
PROJECT_BOOTSTRAPPER_DIR = ROOT_DIR / "ProjectBootstrapper"

if str(PROJECT_BOOTSTRAPPER_DIR) not in sys.path:
    sys.path.append(str(PROJECT_BOOTSTRAPPER_DIR))

from ProjectBootstrapper.bootstrapper_engine import BootstrapperEngine
from ProjectBootstrapper.environment_setup import EnvironmentSetup
from db.app_db import fetch_data, insert_data, update_data, init_db, get_db
# encrypt_for_app / decrypt_for_app protect the password stored in ProjectData with an app master
# key (Flask .env). decrypt_for_app passes legacy plaintext rows through unchanged.
from utils.crypto_util import encrypt_for_app, decrypt_for_app
from utils.password_util import hash_password, verify_password, is_hashed

# Load environment variables early
load_dotenv(BASE_DIR / ".env")

def install_prerequisites():
    req_file = ROOT_DIR / "requirements.txt"
    if req_file.exists():
        print(f"Checking prerequisites from {req_file}...")
        try:
            # Using -q to keep it quiet unless there is an error
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q', '-r', str(req_file)])
        except subprocess.CalledProcessError as e:
            print(f"Warning: Failed to install some prerequisites: {e}")
    else:
        print("requirements.txt not found, skipping prerequisites installation.")

# Global dictionary for background bootstrapper jobs
bootstrapper_jobs = {}

# Tracks the currently running Element Locator Studio subprocess.
# Prevents launching a duplicate when the user accidentally clicks the
# Launch button while a Studio session (e.g. Smart Merge) is already open.
_locator_proc = None

app = Flask(__name__)
# In production, use os.environ.get('SECRET_KEY')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # Cache static files for 1 year

@app.teardown_appcontext
def close_connection(exception):
    from flask import g
    db = getattr(g, 'db', None)
    if db is not None:
        db.close()

@app.context_processor
def inject_user():

    projects = []
    if session.get('user_id'):
        try:
            # project_path lets the active-project selector run the shared dependency pre-check
            projects = fetch_data("SELECT id, project_name, project_path FROM ProjectDetails ORDER BY project_name ASC")
        except Exception:
            pass
    return dict(
        user_name=session.get('user_name', 'Guest'),
        user_role=session.get('user_role', 'guest').lower(),
        global_projects=projects,
        active_project_id=session.get('active_project_id'),
        active_project_name=session.get('active_project_name')
    )

def login_required(role=None):
    def wrapper(f):
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                flash('Please log in to access this page.', 'warning')
                return redirect(url_for('login'))
            if role and session.get('user_role', '').lower() != role.lower() and session.get('user_role', '').lower() != 'admin':
                flash('You do not have permission to access that page.', 'error')
                return redirect(url_for('home'))
            return f(*args, **kwargs)
        decorated_function.__name__ = f.__name__
        return decorated_function
    return wrapper

# Endpoints reachable without login; everything else needs a session.
# locator_heartbeat is public: the detached Locator Studio process has no
# session cookie, so it authenticates with a one-time launch token instead.
PUBLIC_ENDPOINTS = {'login', 'logout', 'index', 'static', 'locator_heartbeat'}

# launch token -> user_id of whoever launched Locator Studio. The studio polls
# the heartbeat with its token; we report active only while that user's session
# is live. In-memory, so a web-app restart forgets all tokens and fails closed.
LOCATOR_LAUNCH_TOKENS = {}

# user_id -> threading.Event, set on logout to release that user's held
# long-poll heartbeat so the Locator Studio closes immediately.
LOCATOR_LOGOUT_EVENTS = {}
_LOCATOR_EVENTS_LOCK = threading.Lock()

def _locator_logout_event(user_id):
    with _LOCATOR_EVENTS_LOCK:
        ev = LOCATOR_LOGOUT_EVENTS.get(user_id)
        if ev is None:
            ev = threading.Event()
            LOCATOR_LOGOUT_EVENTS[user_id] = ev
        return ev

@app.before_request
def require_authentication():
    # Central guard: enforce login for every route except the public ones.
    endpoint = request.endpoint
    if endpoint is None or endpoint in PUBLIC_ENDPOINTS:
        return None
    if 'user_id' not in session:
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Authentication required'}), 401
        flash('Please log in to access this page.', 'warning')
        return redirect(url_for('login'))
    return None

@app.after_request
def add_no_cache_headers(response):
    # no-store stops the browser cache/bfcache from re-showing authenticated
    # pages (and old login input) on the Back button after logout.
    if not request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('home'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Already signed in? Never show the login page again (e.g. the browser Back
    # button after login). Without this, base.html still sees a live session and
    # renders the dashboard chrome at the /login URL — the broken empty-dashboard
    # state. Send authenticated users straight to their dashboard instead.
    if 'user_id' in session:
        return redirect(url_for('home'))
    if request.method == 'POST':
        email = request.form.get('email', '')
        password = request.form.get('password', '')

        if not email or not password:
            flash('All fields are required!', 'error')
            return render_template('login.html')

        user = fetch_data("SELECT * FROM users WHERE email = ?", (email.lower(),))
        
        if not user:
            flash('Login failed! Unknown username or password.', 'error')
        else:
            user = user[0]
            # Matches both hashed and legacy plaintext stored passwords.
            password_ok = verify_password(user['password'], password)
            if user['verified'] == 0:
                flash(f"Hi, {user['name']}! Your account is pending approval.", 'warning')
            elif user['verified'] == 2 and password_ok:
                flash("Account Disabled", 'error')
            elif user['verified'] == 1 and not password_ok:
                flash('Login failed! Invalid username or password.', 'error')
            elif user['verified'] == 1 and password_ok:
                # Login successful

                # Upgrade legacy plaintext rows to a hash on successful login.
                if not is_hashed(user['password']):
                    update_data("UPDATE users SET password = ? WHERE id = ?",
                                (hash_password(password), user['id']))

                session['user_id'] = user['id']
                session['user_name'] = user['name']
                session['user_role'] = user['role']

                local_tz = pytz.timezone('US/Eastern')
                current_time = datetime.now(local_tz)

                # Update session table
                existing_session = fetch_data("SELECT COUNT(*) as cnt FROM SessionDetails WHERE userid = ?", (user['id'],))
                if existing_session and existing_session[0]['cnt'] == 0:
                    insert_data("INSERT INTO SessionDetails (userid, SessionActive, SessionTime) VALUES (?, ?, ?)", 
                                (user['id'], 1, current_time))
                else:
                    update_data("UPDATE SessionDetails SET SessionActive = ?, SessionTime = ? WHERE userid = ?", 
                                (1, current_time, user['id']))

                flash(f"Login successful! Welcome back, {user['name']}!", 'success')
                return redirect(url_for('home'))

    return render_template('login.html')

@app.route('/logout')
def logout():
    user_id = session.get('user_id')
    if user_id:
        local_tz = pytz.timezone('US/Eastern')
        current_time = datetime.now(local_tz)
        update_data("UPDATE SessionDetails SET SessionActive = ?, SessionTime = ? WHERE userid = ?",
                    (0, current_time, user_id))
        # Drop this user's Locator Studio tokens so the next heartbeat fails closed.
        for tok in [t for t, uid in LOCATOR_LAUNCH_TOKENS.items() if uid == user_id]:
            LOCATOR_LAUNCH_TOKENS.pop(tok, None)
        _locator_logout_event(user_id).set()
    session.clear()
    flash('You have been logged out!', 'info')
    return redirect(url_for('login'))

@app.route('/home')
@login_required()
def home():
    return render_template('home.html')

@app.route('/api/set-active-project', methods=['POST'])
@login_required()
def set_active_project():
    data = request.json
    project_id = data.get('project_id')
    project_name = data.get('project_name')
    
    if project_id:
        session['active_project_id'] = project_id
        session['active_project_name'] = project_name
        return jsonify({"status": "success", "message": f"Active project set to {project_name}"})
    else:
        # Clear it if passed empty
        session.pop('active_project_id', None)
        session.pop('active_project_name', None)
        return jsonify({"status": "success", "message": "Active project cleared"})

@app.route('/configurations')
@login_required()
def configurations():
    if session.get('user_role', '').lower() not in ['qa', 'admin']:
        flash('Not authorized', 'error')
        return redirect(url_for('home'))
    return render_template('configurations.html')

@app.route('/admin/add-user', methods=['GET', 'POST'])
@login_required(role='admin')
def add_user():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'add':
            name = request.form.get('name')
            email = request.form.get('email')
            role = request.form.get('role', 'QA')
            password = request.form.get('password')
            
            if not name or not email or not password:
                flash('All fields are required!', 'error')
            else:
                existing = fetch_data("SELECT * FROM users WHERE email = ?", (email,))
                if existing:
                    flash('User with this email already exists!', 'warning')
                else:
                    insert_data("INSERT INTO users (name, email, role, password, verified) VALUES (?, ?, ?, ?, 1)",
                                (name, email, role, hash_password(password)))
                    flash(f'User {name} added successfully.', 'success')
                    return redirect(url_for('add_user'))

    # Fetch existing users
    users = fetch_data("SELECT id, name, email, role, verified FROM users ORDER BY id DESC")
    return render_template('add_user.html', users=users)

@app.route('/qa/script-developer', methods=['GET'])
@login_required()
def script_developer():
    if session.get('user_role', '').lower() not in ['qa', 'admin']:
        flash('Not authorized', 'error')
        return redirect(url_for('home'))
        
    projects = fetch_data("SELECT * FROM ProjectDetails")
    return render_template('script_developer.html', projects=projects)

def _build_directory_tree(path):
    tree = {'name': os.path.basename(path), 'type': 'directory', 'children': []}
    try:
        entries = sorted(os.listdir(path))
        for entry in entries:
            if entry in ['.DS_Store', '.git', '__pycache__', '.pytest_cache', 'target', 'node_modules', 'bin', 'obj'] or entry.endswith('.pyc'):
                continue
            full_path = os.path.join(path, entry)
            if os.path.isdir(full_path):
                tree['children'].append(_build_directory_tree(full_path))
            else:
                tree['children'].append({'name': entry, 'type': 'file'})
    except PermissionError:
        pass
    return tree

def _bootstrapper_worker(job_id, p_name, p_path, tool, lang, fw, pm, url, user, pwd, version_profile=None):
    try:
        success, res = BootstrapperEngine.generate_project(p_name, p_path, tool, lang, fw, pm, url, user, pwd,
                                                           version_profile=version_profile)
        if not success:
            bootstrapper_jobs[job_id] = {"status": "error", "message": f"Scaffolding failed: {res}"}
            return

        target_dir = res
        bootstrapper_jobs[job_id] = {"status": "processing", "message": "Scaffolding complete. Starting dependency install..."}

        # Per-phase status updates: EnvironmentSetup splits the install into discrete phases
        # (dependency resolution, browser download, etc.) and invokes this callback before
        # each one so the polling endpoint reflects what is actually running right now,
        # instead of a single static "Installing dependencies..." message that hides minutes
        # of activity. See EnvironmentSetup._build_install_phases for the phase list.
        def _update_install_status(phase_message):
            bootstrapper_jobs[job_id] = {"status": "processing", "message": phase_message}

        inst_success, inst_msg = EnvironmentSetup.install_project_dependencies(
            target_dir, pm, tool, status_cb=_update_install_status
        )
        if not inst_success:
            bootstrapper_jobs[job_id] = {"status": "error", "message": f"Dependency installation failed: {inst_msg}"}
            return

        bootstrapper_jobs[job_id] = {"status": "processing", "message": "Dependencies installed. Running smoke test..."}
        smoke_ok, smoke_msg = BootstrapperEngine.execute_smoke_test(target_dir, tool, lang, fw, pm)
        if not smoke_ok:
            bootstrapper_jobs[job_id] = {"status": "error", "message": f"Smoke Test failed: {smoke_msg}"}
            return

        tree_data = _build_directory_tree(target_dir)

        bootstrapper_jobs[job_id] = {
            "status": "completed",
            "message": "Project Scaffolding Complete! All checks passed.",
            "target_dir": target_dir,
            "tree": tree_data,
            "project_metadata": {
                "projectName": p_name,
                "projectPath": p_path,
                "language": lang,
                "framework": fw,
                "tool": tool,
                "packageManager": pm,
                "url": url,
                "username": user,
                "password": pwd
            }
        }
    except Exception as e:
        bootstrapper_jobs[job_id] = {"status": "error", "message": str(e)}

# ─────────────────────────────────────────────────────────────────────────────
# Native folder / file pickers
#
# These helpers pop a real OS folder-picker on the user's desktop and return
# the selected absolute path. Because Flask serves the UI but the picker lives
# on whatever machine the Flask process is running on, this assumes the user
# is on the same host as the server (which is how this app is run in practice
# — locally, via the IDE extension).
#
# Picker strategy:
#   macOS   : AppleScript "choose folder" / "choose file" (always installed).
#   Windows : tkinter subprocess (ships with the standard Python installer).
#   Linux   : try tkinter → zenity → kdialog. Tk is preferred when present
#             (consistent look across DEs), zenity is the GNOME standard and
#             widely pre-installed on Ubuntu/Fedora, kdialog covers KDE.
#
# Each picker is its own helper that EITHER returns a path string (possibly
# empty if the user cancelled) OR raises. The dispatcher catches the
# "binary/module missing" exceptions and falls through; any other failure is
# logged so the issue is visible next time. Previously this whole block lived
# inside a bare `except Exception: return ""`, which silently masked a
# missing python3-tk install and made the Browse button look broken.
# ─────────────────────────────────────────────────────────────────────────────

# Time we let a desktop picker stay open before we give up. Plenty for a user
# to navigate, short enough that a hung Flask request thread eventually frees.
_PICKER_TIMEOUT_SECONDS = 300


def _tk_subprocess_kwargs():
    """Windows needs CREATE_NO_WINDOW so the spawned python.exe doesn't flash a console."""
    import platform
    if platform.system() == 'Windows':
        return {"creationflags": 0x08000000}
    return {}


def _pick_directory_tkinter(prompt):
    """Folder picker via tkinter in a subprocess. Raises ModuleNotFoundError if tk is missing."""
    safe_prompt = json.dumps(prompt)
    script = (
        "import tkinter as tk; "
        "from tkinter import filedialog; "
        "root = tk.Tk(); "
        "root.withdraw(); "
        "root.attributes('-topmost', True); "
        f"path = filedialog.askdirectory(title={safe_prompt}); "
        "print(path, end='')"
    )
    result = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True, text=True,
        timeout=_PICKER_TIMEOUT_SECONDS,
        **_tk_subprocess_kwargs(),
    )
    # The subprocess hides ImportError as a non-zero exit; surface it explicitly so the
    # dispatcher can fall through to the next picker instead of giving up.
    if result.returncode != 0 and "No module named 'tkinter'" in (result.stderr or ""):
        raise ModuleNotFoundError("tkinter not installed in subprocess Python")
    if result.returncode != 0:
        raise RuntimeError(f"tkinter dialog exit {result.returncode}: {(result.stderr or '').strip()}")
    return result.stdout.strip()


def _pick_directory_zenity(prompt):
    """Folder picker via zenity (GNOME). Raises FileNotFoundError if zenity is missing."""
    result = subprocess.run(
        ['zenity', '--file-selection', '--directory', '--title', prompt],
        capture_output=True, text=True,
        timeout=_PICKER_TIMEOUT_SECONDS,
    )
    # zenity: 0 = picked, 1 = cancelled (treat as empty selection), >1 = real error.
    if result.returncode > 1:
        raise RuntimeError(f"zenity exit {result.returncode}: {(result.stderr or '').strip()}")
    return result.stdout.strip()


def _pick_directory_kdialog(prompt):
    """Folder picker via kdialog (KDE). Raises FileNotFoundError if kdialog is missing."""
    start_dir = os.path.expanduser('~')
    result = subprocess.run(
        ['kdialog', '--getexistingdirectory', start_dir, '--title', prompt],
        capture_output=True, text=True,
        timeout=_PICKER_TIMEOUT_SECONDS,
    )
    if result.returncode > 1:
        raise RuntimeError(f"kdialog exit {result.returncode}: {(result.stderr or '').strip()}")
    return result.stdout.strip()


def _pick_file_tkinter(prompt):
    """File picker via tkinter in a subprocess."""
    safe_prompt = json.dumps(prompt)
    script = (
        "import tkinter as tk; "
        "from tkinter import filedialog; "
        "root = tk.Tk(); "
        "root.withdraw(); "
        "root.attributes('-topmost', True); "
        f"path = filedialog.askopenfilename(title={safe_prompt}); "
        "print(path, end='')"
    )
    result = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True, text=True,
        timeout=_PICKER_TIMEOUT_SECONDS,
        **_tk_subprocess_kwargs(),
    )
    if result.returncode != 0 and "No module named 'tkinter'" in (result.stderr or ""):
        raise ModuleNotFoundError("tkinter not installed in subprocess Python")
    if result.returncode != 0:
        raise RuntimeError(f"tkinter dialog exit {result.returncode}: {(result.stderr or '').strip()}")
    return result.stdout.strip()


def _pick_file_zenity(prompt):
    """File picker via zenity."""
    result = subprocess.run(
        ['zenity', '--file-selection', '--title', prompt],
        capture_output=True, text=True,
        timeout=_PICKER_TIMEOUT_SECONDS,
    )
    if result.returncode > 1:
        raise RuntimeError(f"zenity exit {result.returncode}: {(result.stderr or '').strip()}")
    return result.stdout.strip()


def _pick_file_kdialog(prompt):
    """File picker via kdialog."""
    start_dir = os.path.expanduser('~')
    result = subprocess.run(
        ['kdialog', '--getopenfilename', start_dir, '--title', prompt],
        capture_output=True, text=True,
        timeout=_PICKER_TIMEOUT_SECONDS,
    )
    if result.returncode > 1:
        raise RuntimeError(f"kdialog exit {result.returncode}: {(result.stderr or '').strip()}")
    return result.stdout.strip()


def _run_picker_chain(pickers, prompt):
    """
    Try each picker in order, falling through ONLY when the picker's underlying tool is missing
    (ModuleNotFoundError for tkinter, FileNotFoundError for zenity/kdialog). Other failures are
    logged and we still continue to the next picker — if every picker fails the caller sees "".

    Cancellation (picker returned but path is empty) is treated as "user said no", not as a
    failure, so we do NOT fall through to the next picker in that case.
    """
    import logging
    for picker in pickers:
        try:
            path = picker(prompt)
        except (ModuleNotFoundError, FileNotFoundError) as missing:
            logging.info("Picker %s unavailable: %s", picker.__name__, missing)
            continue
        except subprocess.TimeoutExpired:
            logging.warning("Picker %s timed out after %ss", picker.__name__, _PICKER_TIMEOUT_SECONDS)
            return ""  # user walked away — don't open another dialog behind their back
        except Exception as e:
            logging.warning("Picker %s failed: %s", picker.__name__, e)
            continue

        return os.path.normpath(path) if path else ""

    logging.error("No working file/folder picker found on this host.")
    return ""


def _get_directory_path(prompt="Select Directory"):
    """Open a native folder picker and return the selected absolute path, or '' if cancelled."""
    import platform
    try:
        if platform.system() == 'Darwin':
            # macOS uses the native AppleScript dialog — always present, no fallback needed.
            safe_prompt = json.dumps(prompt)
            script = (
                f'tell application "System Events" to activate\n'
                f'tell application "System Events"\n'
                f'set folderPath to choose folder with prompt {safe_prompt}\n'
                f'POSIX path of folderPath\n'
                f'end tell'
            )
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True, text=True,
                timeout=_PICKER_TIMEOUT_SECONDS,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return ""

        if platform.system() == 'Windows':
            # Windows: tkinter is shipped with the standard Python installer, so just use it.
            return _run_picker_chain([_pick_directory_tkinter], prompt)

        # Linux (and anything else): tkinter is best when available; otherwise GTK then KDE.
        return _run_picker_chain(
            [_pick_directory_tkinter, _pick_directory_zenity, _pick_directory_kdialog],
            prompt,
        )
    except Exception as e:
        import logging
        logging.exception("Unexpected error in _get_directory_path: %s", e)
        return ""

@app.route('/api/browse-directory', methods=['GET'])
def browse_directory():
    path = _get_directory_path("Select Project Save Location")
    return jsonify({"path": path})

def _get_file_path(prompt="Select File"):
    """Open a native file picker and return the selected absolute path, or '' if cancelled.

    Same picker-chain strategy as _get_directory_path above — see that function's docstring
    for platform-by-platform notes.
    """
    import platform
    try:
        if platform.system() == 'Darwin':
            safe_prompt = json.dumps(prompt)
            script = (
                f'tell application "System Events" to activate\n'
                f'tell application "System Events"\n'
                f'set filePath to choose file with prompt {safe_prompt}\n'
                f'POSIX path of filePath\n'
                f'end tell'
            )
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True, text=True,
                timeout=_PICKER_TIMEOUT_SECONDS,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            return ""

        if platform.system() == 'Windows':
            return _run_picker_chain([_pick_file_tkinter], prompt)

        return _run_picker_chain(
            [_pick_file_tkinter, _pick_file_zenity, _pick_file_kdialog],
            prompt,
        )
    except Exception as e:
        import logging
        logging.exception("Unexpected error in _get_file_path: %s", e)
        return ""

@app.route('/api/browse-file', methods=['GET'])
@login_required()
def browse_file():
    path = _get_file_path("Select File to Load")
    content = ""
    if path and os.path.exists(path):
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    return jsonify({"path": path, "content": content})

@app.route('/api/bootstrap-project', methods=['POST'])
@login_required()
def bootstrap_project():
    data = request.json
    p_name = data.get('projectName', '').strip()
    p_path = data.get('projectPath', '').strip()
    tool = data.get('tool', '')
    lang = data.get('language', '')
    fw = data.get('framework', '')
    pm = data.get('packageManager', '')
    url = data.get('url', '').strip()
    user = data.get('username', '').strip()
    pwd = data.get('password', '').strip()
    # Optional version-profile label; None -> engine uses the template's default profile.
    version_profile = (data.get('versionProfile') or '').strip() or None

    if not p_name or not p_path:
        return jsonify({"status": "error", "message": "Project name and path are required."}), 400

    env_ok, missing = EnvironmentSetup.verify_environment(lang)
    if not env_ok:
        return jsonify({"status": "error", "message": f"Missing system dependencies: {', '.join(missing)}"}), 400

    job_id = str(uuid.uuid4())
    bootstrapper_jobs[job_id] = {"status": "processing", "message": "Initializing process..."}

    threading.Thread(target=_bootstrapper_worker,
                     args=(job_id, p_name, p_path, tool, lang, fw, pm, url, user, pwd, version_profile),
                     daemon=True).start()

    return jsonify({"status": "processing", "job_id": job_id})

@app.route('/api/version-profiles', methods=['GET'])
@login_required()
def version_profiles():
    # Feeds the modal dropdown; empty list -> no version choice for this template.
    from ProjectBootstrapper.versions_catalog import profiles_for
    tool = request.args.get('tool', '')
    lang = request.args.get('language', '')
    fw = request.args.get('framework', '')
    profiles = profiles_for(tool, lang, fw)
    return jsonify({"profiles": [p["label"] for p in profiles]})

@app.route('/api/preflight-dependencies', methods=['POST'])
@login_required()
def preflight_dependencies():
    """Pre-creation dependency report for the create-project form.

    Given the selected stack and version profile, return each required system tool with its
    required-vs-detected version and an ok/missing/mismatch status, so the user can see (and
    install) anything missing BEFORE submitting — instead of hitting a late, mid-install error
    from verify_environment()/the install phases. Advisory only: this never blocks creation,
    the form still lets the user proceed and install in parallel.
    """
    from ProjectBootstrapper.versions_catalog import resolve_versions
    data = request.json or {}
    tool = data.get('tool', '')
    lang = data.get('language', '')
    fw = data.get('framework', '')
    # Empty string -> None so resolve_versions falls back to the stack's default profile.
    label = (data.get('versionProfile') or '').strip() or None

    # The resolved profile supplies the version a tool must match (e.g. {"java": "17"}); when the
    # stack has no version choice this is {} and the report degrades to presence-only checks.
    profile_versions = resolve_versions(tool, lang, fw, label)
    required_versions = data.get('requiredVersions') or {}
    if required_versions:
        profile_versions = {**profile_versions, **required_versions}
    deps = EnvironmentSetup.required_dependencies(tool, lang, fw, profile_versions)
    all_ok = all(d['status'] == 'ok' for d in deps)
    return jsonify({"dependencies": deps, "allOk": all_ok})

@app.route('/api/bootstrap-status/<job_id>', methods=['GET'])
@login_required()
def bootstrap_status(job_id):
    if job_id not in bootstrapper_jobs:
        return jsonify({"status": "error", "message": "Job ID not found"})
        
    job = bootstrapper_jobs[job_id]
    
    # Auto-insert or update database immediately after scaffolding completes
    if job.get('status') in ['completed'] and 'db_inserted' not in job:
        meta = job.get('project_metadata', {})
        if meta:
            try:
                # Ensure the project name is appended to the base path
                full_project_path = os.path.join(meta['projectPath'], meta['projectName'])
                
                # Check if there is an existing project with this name where path is not configured (N/A, empty or NULL)
                existing_p = fetch_data(
                    "SELECT id FROM ProjectDetails WHERE project_name = ? AND (project_path IS NULL OR project_path = '' OR project_path = 'N/A' OR LOWER(project_path) = 'n/a')",
                    (meta['projectName'],)
                )
                
                if existing_p:
                    p_id = existing_p[0]['id']
                    update_data(
                        "UPDATE ProjectDetails SET project_path=?, project_lang=?, project_fw=?, project_tool=?, package_manager=?, project_type=? WHERE id=?",
                        (full_project_path, meta['language'], meta['framework'], meta['tool'], meta.get('packageManager'), 'New', p_id)
                    )
                else:
                    insert_data("INSERT INTO ProjectDetails (project_name, project_path, project_lang, project_fw, project_tool, package_manager, project_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (meta['projectName'], full_project_path, meta['language'], meta['framework'], meta['tool'], meta.get('packageManager'), 'New'))
                    
                    # Save Project Data (URL/Credentials)
                    new_id_res = fetch_data("SELECT id FROM ProjectDetails WHERE project_path = ?", (full_project_path,))
                    if new_id_res:
                        p_id = new_id_res[0]['id']
                    else:
                        p_id = None
                
                if p_id:
                    # Update or insert ProjectData (URL/Credentials)
                    existing_data = fetch_data("SELECT id FROM ProjectData WHERE project_details_id = ?", (p_id,))
                    if existing_data:
                        update_data("UPDATE ProjectData SET baseurl=?, username=?, password=? WHERE project_details_id=?",
                                    (meta.get('url'), meta.get('username'), encrypt_for_app(meta.get('password')), p_id))
                    else:
                        insert_data("INSERT INTO ProjectData (baseurl, username, password, project_details_id) VALUES (?, ?, ?, ?)",
                                    (meta.get('url'), meta.get('username'), encrypt_for_app(meta.get('password')), p_id))
            except Exception as e:
                import logging
                logging.error(f"Database insertion/update failed: {e}")
        job['db_inserted'] = True

    return jsonify(job)

@app.route('/api/remove-missing-projects', methods=['POST'])
@login_required()
def remove_missing_projects():
    if session.get('user_role', '').lower() != 'admin':
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
        
    projects = fetch_data("SELECT id, project_path, project_name FROM ProjectDetails")
    removed = []
    
    for p in projects:
        # Check if project directory exists
        full_path = os.path.join(p['project_path'], p['project_name'])
        if not os.path.exists(full_path):
            update_data("DELETE FROM ProjectDetails WHERE id = ?", (p['id'],))
            removed.append(p['project_name'])
            
    return jsonify({"status": "success", "removed": removed})

@app.route('/qa/script-runner', methods=['GET'])
@login_required()
def script_runner():
    if session.get('user_role', '').lower() not in ['qa', 'admin']:
        flash('Not authorized', 'error')
        return redirect(url_for('home'))
        
    query = """
        SELECT pd.*, pt.default_run_commands 
        FROM ProjectDetails pd
        LEFT JOIN ProjectTemplates pt ON pd.project_tool = pt.tool 
            AND (LOWER(pd.project_lang) = LOWER(pt.language) OR (pd.project_lang = 'JS / TS' AND pt.language = 'TypeScript'))
            AND pd.project_fw = pt.framework
    """
    projects = fetch_data(query)
    return render_template('script_runner.html', projects=projects)

@app.route('/qa/test-case-generator', methods=['GET'])
@login_required()
def test_case_generator():
    if session.get('user_role', '').lower() not in ['qa', 'admin']:
        flash('Not authorized', 'error')
        return redirect(url_for('home'))
    projects = fetch_data("SELECT id, project_name, project_path FROM ProjectDetails ORDER BY project_name ASC")
    return render_template('test_case_generator.html', projects=projects)

@app.route('/api/project-inputs/<int:project_id>', methods=['GET'])
@login_required()
def get_project_inputs(project_id):
    inputs = fetch_data("SELECT id, req_name, requirement FROM ProjectInputs WHERE projectId = ? ORDER BY id DESC", (project_id,))
    return jsonify({"status": "success", "data": inputs})

@app.route('/api/save-project-input', methods=['POST'])
@login_required()
def save_project_input():
    data = request.json
    project_id = data.get('projectId')
    req_name = data.get('req_name', '').strip()
    requirement = data.get('requirement', '').strip()
    
    if not project_id:
        return jsonify({"status": "error", "message": "Project selection is required to save a requirement."}), 400
    if not req_name or not requirement:
        return jsonify({"status": "error", "message": "Requirement name and content are required."}), 400
        
    try:
        insert_data("INSERT INTO ProjectInputs (projectId, req_name, requirement) VALUES (?, ?, ?)", (project_id, req_name, requirement))
        return jsonify({"status": "success", "message": "Requirement saved successfully."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/qa/launch-element-locator', methods=['GET'])
@login_required()
def launch_element_locator():
    global _locator_proc
    try:
        project_path = request.args.get('project_path', '')
        language = request.args.get('language', '')
        framework = request.args.get('framework', '')
        tool = request.args.get('tool', '')
        app_url = request.args.get('app_url', '')

        # Diagnostic: log exactly what the endpoint received from JS
        print(f"[App] launch-element-locator query string: {request.query_string!r}", flush=True)
        print(f"[App] launch-element-locator parsed args: project_path={project_path!r}, language={language!r}, framework={framework!r}, tool={tool!r}, app_url={app_url!r}", flush=True)

        # Fallback to active project session details if parameters are missing
        active_project_id = session.get('active_project_id')
        if active_project_id:
            pd = fetch_data("SELECT project_path, project_lang, project_fw, project_tool FROM ProjectDetails WHERE id = ?", (active_project_id,))
            if pd:
                row = pd[0]
                if not project_path:
                    project_path = row.get('project_path', '')
                if not language:
                    language = row.get('project_lang', '')
                if not framework:
                    framework = row.get('project_fw', '')
                if not tool:
                    tool = row.get('project_tool', '')
            
            pdata = fetch_data("SELECT baseurl FROM ProjectData WHERE project_details_id = ?", (active_project_id,))
            if pdata and not app_url:
                app_url = pdata[0].get('baseurl', '')

        # ── Guard: prevent duplicate launches ─────────────────────────────
        # If the previously launched process is still alive (poll() == None),
        # or if we detect any running launcher.py process, do NOT spawn a second
        # instance. This prevents accidental duplicate launches.
        studio_running = False
        if _locator_proc is not None and _locator_proc.poll() is None:
            studio_running = True
        else:
            if sys.platform == 'win32':
                try:
                    # Try wmic first (CREATE_NO_WINDOW = 0x08000000 so no console window pops up)
                    # NOTE: filter on Name='python.exe'/'pythonw.exe' so the wmic process
                    # itself (whose command line contains the literal "launcher.py" from the
                    # query) does NOT self-match and produce a false "already running" result.
                    res = subprocess.run(
                        ['wmic', 'process', 'where',
                         "CommandLine like '%launcher.py%' and (Name='python.exe' or Name='pythonw.exe')",
                         'get', 'ProcessId'],
                        capture_output=True, text=True, creationflags=0x08000000
                    )
                    lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
                    if len(lines) > 1 and any(x.isdigit() for x in lines[1:]):
                        studio_running = True
                except Exception:
                    # Fallback to powershell if wmic is disabled or unavailable.
                    # Same Name filter as above to avoid the powershell process self-matching.
                    try:
                        res = subprocess.run(
                            ['powershell', '-Command', "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*launcher.py*' -and $_.Name -like 'python*' } | Select-Object -ExpandProperty ProcessId"],
                            capture_output=True, text=True, creationflags=0x08000000
                        )
                        pids = [l.strip() for l in res.stdout.splitlines() if l.strip().isdigit()]
                        if pids:
                            studio_running = True
                    except Exception:
                        pass
            else:
                try:
                    res = subprocess.run(['pgrep', '-f', 'ElementLocator/launcher.py'], capture_output=True)
                    if res.returncode == 0:
                        studio_running = True
                except Exception:
                    pass

        if studio_running:
            print("[App] launch-element-locator: Studio already running, ignoring duplicate launch.", flush=True)
            return jsonify({'status': 'already_running', 'message': 'Element Locator Studio is already open.'})

        # Treat the literal string "None" / "null" / "undefined" as missing — these
        # can come from Jinja rendering a NULL DB value, or JS reading an undefined
        # dataset attribute and coercing it to a string.
        def _sanitize(v):
            if v is None:
                return ''
            s = str(v).strip()
            if s.lower() in ('none', 'null', 'undefined', 'n/a'):
                return ''
            return s

        project_path = _sanitize(project_path)
        language = _sanitize(language)
        framework = _sanitize(framework)
        tool = _sanitize(tool)
        app_url = _sanitize(app_url)

        locator_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'ElementLocator', 'launcher.py'))

        token = secrets.token_urlsafe(32)
        LOCATOR_LAUNCH_TOKENS[token] = session['user_id']
        server_url = request.host_url.rstrip('/')

        # Project args 1-5 passed positionally so server_url and token sit at argv 6 and 7.
        cmd = [sys.executable, locator_path,
               project_path, language, framework, tool, app_url,
               server_url, token]

        # NOTE: Do NOT use shell=True here. On Windows, shell=True with a list of
        # arguments causes the extra args to be lost/mangled by cmd.exe, so the
        # launched studio receives no parameters. Launching python.exe directly
        # via CreateProcess passes argv through cleanly.
        popen_kwargs = {}
        if sys.platform == 'win32':
            popen_kwargs['creationflags'] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            popen_kwargs['start_new_session'] = True

        # IMPORTANT: A DETACHED_PROCESS child inherits NO valid standard handles.
        # The locator studio prints diagnostics on startup (the very first
        # print() runs before any try/except), and writing to an invalid stdout
        # raises OSError, killing the process instantly — i.e. the studio
        # "crashes" the moment it is launched. Give the child real handles by
        # redirecting stdout/stderr to a log file (also useful for diagnostics)
        # and feeding it a null stdin.
        log_path = os.path.join(os.path.dirname(locator_path), 'element_locator.log')
        log_file = open(log_path, 'a', buffering=1, encoding='utf-8', errors='replace')
        popen_kwargs['stdout'] = log_file
        popen_kwargs['stderr'] = subprocess.STDOUT
        popen_kwargs['stdin'] = subprocess.DEVNULL

        _locator_proc = subprocess.Popen(cmd, **popen_kwargs)
        # Parent keeps no need for the handle; the child has inherited its own copy.
        log_file.close()
        print(f"[App] Launching command: {cmd} (logging to {log_path})")
        return jsonify({'status': 'success', 'message': 'Element Locator Studio launched.'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/locator-heartbeat', methods=['GET'])
def locator_heartbeat():
    token = request.args.get('token', '')
    user_id = LOCATOR_LAUNCH_TOKENS.get(token)
    if not user_id:
        return jsonify({'active': False})

    ev = _locator_logout_event(user_id)
    ev.clear()
    keepalive_s, slice_s, waited = 25, 5, 0
    while waited < keepalive_s:
        rows = fetch_data("SELECT SessionActive FROM SessionDetails WHERE userid = ?", (user_id,))
        active = bool(rows) and rows[0]['SessionActive'] == 1 and token in LOCATOR_LAUNCH_TOKENS
        if not active:
            return jsonify({'active': False})
        if ev.wait(slice_s):
            ev.clear()
        waited += slice_s
    return jsonify({'active': True})

@app.route('/api/select-directory', methods=['GET'])
@login_required()
def select_directory():
    path = _get_directory_path("Select Existing Project Directory")
    return jsonify({"path": path})

@app.route('/api/detect-project', methods=['POST'])
@login_required()
def detect_project():
    try:
        data = request.get_json()
        path = data.get('path')
        if not path or not os.path.exists(path):
            return jsonify({"error": "Invalid path"}), 400
        
        files = os.listdir(path)
        tool = "UnKnown"
        language = "Unknown"
        framework = "Unknown"
        packager = "Unknown"

        # Signature patterns
        packages = {
            'pom.xml': 'Maven',
            'build.gradle':'Gradle',
            'package.json':'NPM',
            'requirements.txt': 'Pip',
        }

        extensions_found = []
        feature_path = "none"
        page_path = "none"
        step_path = "none"
        required_versions = {}  # inferred runtime versions, keyed java/python/dotnet/node

        for root, dirs, files in os.walk(path):
            if any(skip in root for skip in ['node_modules', '.git', 'venv', 'target', 'bin']):
                continue
            
            rel_root = os.path.relpath(root, path)
            basename_lower = os.path.basename(root).lower()

            # Feature Path Detection (look for .feature files)
            if feature_path == "none" and any(f.endswith(".feature") for f in files):
                feature_path = rel_root

            # Step Path Detection (look for folders with 'step' or files with 'step')
            if step_path == "none":
                if "step" in basename_lower or any("step" in f.lower() for f in files):
                    step_path = rel_root

            # Page Path Detection (look for folders with 'page' or files with 'page')
            if page_path == "none":
                if "page" in basename_lower or any("page" in f.lower() for f in files):
                    page_path = rel_root

            for file in files:
                ext = os.path.splitext(file)[1]
                if ext not in extensions_found:
                    extensions_found.append(ext)

                if file in ['pom.xml', 'package.json', 'requirements.txt', 'build.gradle',
                            'pyproject.toml', 'runtime.txt'] or file.endswith('.csproj'):
                    try:
                        with open(os.path.join(root, file), 'r', errors='ignore') as f:
                            content = f.read().lower()

                            EnvironmentSetup.infer_required_versions(file, content, required_versions)

                            # Tool checks...
                            if 'selenium' in content: tool='Selenium'
                            elif 'playwright' in content: tool='Playwright'

                            #Java Framework Check
                            if 'cucumber' in content: framework = 'Cucumber'
                            elif 'testng' in content: framework = 'TestNg'
                            elif 'junit' in content: framework = 'JUnit'

                            #Python Framework Check
                            if 'jbehave' in content: framework = 'JBehave'
                            elif 'pytest' in content: framework = 'Pytest'

                            # C# Specific Frameworks
                            if 'specflow' in content: framework ='SpecFlow'
                            if 'nunit' in content: framework = 'NUnit'
                            if 'xunit' in content: framework ='xUnit'
                            if 'mstest' in content: framework ='MSTest'

                            # Update Packager for C#
                            if file.endswith('.csproj'):
                                packager = 'NuGet'
                            elif file in packages:
                                packager = packages[file]

                    except Exception:
                        pass

        if '.java' in extensions_found:
            language = 'Java'
        elif '.py' in extensions_found:
            language = 'Python'
        elif '.ts' in extensions_found:
            language = 'TypeScript'
        elif '.js' in extensions_found:
            language = 'JavaScript'
        elif '.cs' in extensions_found:
            language = 'C#'

        return jsonify({
            "tool": tool,
            "language": language,
            "framework": framework,
            "packager": packager,
            "feature_path": feature_path,
            "page_path": page_path,
            "step_path": step_path,
            "required_versions": required_versions
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

def is_valid_git_url(url):
    if not url:
        return False
    url = url.strip()
    if url.startswith("http://") or url.startswith("https://") or url.startswith("git@") or url.startswith("ssh://"):
        return True
    return False

def detect_project_settings_helper(path):
    if not path or not os.path.exists(path):
        return "Unknown", "Unknown", "Unknown", "Unknown", "Existing"
        
    tool = "Unknown"
    language = "Unknown"
    framework = "Unknown"
    packager = "Unknown"

    packages = {
        'pom.xml': 'Maven',
        'build.gradle': 'Gradle',
        'package.json': 'NPM',
        'requirements.txt': 'Pip',
    }

    extensions_found = []

    for root, dirs, files in os.walk(path):
        if any(skip in root for skip in ['node_modules', '.git', 'venv', 'target', 'bin']):
            continue
        
        for file in files:
            ext = os.path.splitext(file)[1]
            if ext not in extensions_found:
                extensions_found.append(ext)

            if file in ['pom.xml', 'package.json', 'requirements.txt', 'build.gradle', 'pyproject.toml', 'runtime.txt'] or file.endswith('.csproj'):
                try:
                    with open(os.path.join(root, file), 'r', errors='ignore') as f:
                        content = f.read().lower()

                        if 'selenium' in content: tool = 'Selenium'
                        elif 'playwright' in content: tool = 'Playwright'

                        if 'cucumber' in content: framework = 'Cucumber'
                        elif 'testng' in content: framework = 'TestNg'
                        elif 'junit' in content: framework = 'JUnit'
                        elif 'jbehave' in content: framework = 'JBehave'
                        elif 'pytest' in content: framework = 'Pytest'
                        elif 'specflow' in content: framework = 'SpecFlow'
                        elif 'nunit' in content: framework = 'NUnit'
                        elif 'xunit' in content: framework = 'xUnit'
                        elif 'mstest' in content: framework = 'MSTest'

                        if file.endswith('.csproj'):
                            packager = 'NuGet'
                        elif file in packages:
                            packager = packages[file]
                except Exception:
                    pass

    if '.java' in extensions_found:
        language = 'Java'
    elif '.py' in extensions_found:
        language = 'Python'
    elif '.ts' in extensions_found:
        language = 'TypeScript'
    elif '.js' in extensions_found:
        language = 'JavaScript'
    elif '.cs' in extensions_found:
        language = 'C#'

    return tool, language, framework, packager, "Existing"

@app.route('/api/projects', methods=['GET'])
@login_required()
def get_projects():
    try:
        projects = fetch_data("SELECT id, project_name, project_path FROM ProjectDetails ORDER BY project_name ASC")
        return jsonify({"status": "success", "data": projects})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/unconfigured-projects', methods=['GET'])
@login_required()
def get_unconfigured_projects():
    try:
        projects = fetch_data(
            "SELECT id, project_name FROM ProjectDetails "
            "WHERE project_path IS NULL OR project_path = '' OR project_path = 'N/A' OR LOWER(project_path) = 'n/a' "
            "GROUP BY project_name ORDER BY project_name ASC"
        )
        return jsonify({"status": "success", "data": projects})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/save-project-config', methods=['POST'])
@login_required()
def save_project_config():
    try:
        data = request.json
        is_new_project = data.get('is_new_project', True)
        project_id = data.get('project_id')
        project_name = data.get('project_name', '').strip()
        project_path = data.get('project_path', '').strip()
        baseurl = data.get('baseurl', '').strip()
        app_username = data.get('app_username', data.get('username', '')).strip()
        app_password = data.get('app_password', data.get('password', '')).strip()
        git_repo_url = data.get('git_repo_url', '').strip()
        git_username = data.get('git_username', '').strip()
        git_pa_token = data.get('git_pa_token', '').strip()
        run_commands = data.get('run_commands', '').strip()
        is_git_configured = data.get('is_git_configured', False)

        if git_repo_url and not project_path:
            return jsonify({"status": "error", "message": "Local Automation Directory path is mandatory when Git Repository URL is provided."}), 400

        if not project_path:
            return jsonify({"status": "error", "message": "Local Automation Directory path is required."}), 400

        # Auto-resolve if the project path already exists in the database
        if project_path:
            existing_path = fetch_data("SELECT id, project_name FROM ProjectDetails WHERE project_path = ?", (project_path,))
            if existing_path:
                is_new_project = False
                project_id = existing_path[0]['id']
                if not project_name:
                    project_name = existing_path[0]['project_name']

        # Auto-derive project name if not provided
        if not project_name and project_path:
            project_name = os.path.basename(os.path.normpath(project_path))
            if not project_name:
                project_name = "Local_Project"

        # 1. Validation
        if not project_name:
            return jsonify({"status": "error", "message": "Project Name is required."}), 400
            
        # Duplicate project name check
        if is_new_project:
            existing = fetch_data("SELECT id FROM ProjectDetails WHERE project_name = ?", (project_name,))
            if existing:
                return jsonify({"status": "error", "message": f"Project Name '{project_name}' already exists."}), 400
        else:
            if not project_id:
                return jsonify({"status": "error", "message": "Project ID is required for update."}), 400
            existing = fetch_data("SELECT id FROM ProjectDetails WHERE project_name = ? AND id != ?", (project_name, project_id))
            if existing:
                return jsonify({"status": "error", "message": f"Project Name '{project_name}' is already used by another project."}), 400
                
        # Git username & token validation when Git URL is provided
        if git_repo_url:
            if not is_valid_git_url(git_repo_url):
                return jsonify({"status": "error", "message": "Invalid Git Repository URL format. Must start with http://, https://, git@, or ssh://"}), 400
            if not git_username:
                return jsonify({"status": "error", "message": "Git Username is mandatory when Git Repository URL is provided."}), 400
            if not git_pa_token:
                return jsonify({"status": "error", "message": "Git PA Token is mandatory when Git Repository URL is provided."}), 400
        else:
            # Check if username or token are provided but git URL is NOT
            if git_username or git_pa_token:
                return jsonify({"status": "error", "message": "Git Repository URL is mandatory when Git Username or Git PA Token is provided."}), 400

        # 2. Git Clone / Setup logic
        if not is_git_configured:
            if git_repo_url:
                auth_config = {
                    "repo_url": git_repo_url,
                    "username": git_username,
                    "access_token": git_pa_token
                }
                git_res = GitService.download_project_from_git(project_path, auth_config)
                if not git_res["success"]:
                    return jsonify({"status": "error", "message": f"Git clone/setup failed: {git_res['message']}"}), 500
            else:
                os.makedirs(project_path, exist_ok=True)
        else:
            os.makedirs(project_path, exist_ok=True)
            if git_repo_url:
                auth_config = {
                    "repo_url": git_repo_url,
                    "username": git_username,
                    "access_token": git_pa_token
                }
                GitService.sync_git_config(project_path, auth_config)

        # 3. Resolve the project stack settings (tool / language / framework / packager).
        #
        # Detection rules:
        #   * Scan the folder ONLY when the Local Automation Directory path exists AND it
        #     actually contains code. For a git-backed project the clone/sync above runs
        #     synchronously, so by this point the cloned code is already saved on disk and
        #     the scan reflects the real project.
        #   * Never fabricate defaults. Whatever the scan cannot determine (or when there
        #     is no code yet) falls back to the value provided in the payload, and then to
        #     the value already stored in the DB (on update) - so we don't silently assign
        #     Playwright / Cucumber / TypeScript / NPM to a project that isn't that stack.
        def _provided(value):
            value = (value or '').strip()
            return '' if value.lower() in ('unknown', 'n/a', 'none') else value

        def _dir_has_code(p):
            if not p or not os.path.isdir(p):
                return False
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in ('node_modules', '.git', 'venv', 'target', 'bin')]
                if files:
                    return True
            return False

        prov_tool = _provided(data.get('tool'))
        prov_language = _provided(data.get('language'))
        prov_framework = _provided(data.get('framework'))
        prov_packager = _provided(data.get('packager'))

        # Detect from the local code only once it is present; otherwise leave detection blank.
        det_tool = det_language = det_framework = det_packager = ''
        if _dir_has_code(project_path):
            d_tool, d_language, d_framework, d_packager, _ = detect_project_settings_helper(project_path)
            det_tool = _provided(d_tool)
            det_language = _provided(d_language)
            det_framework = _provided(d_framework)
            det_packager = _provided(d_packager)

        # 4. Insert or Update ProjectDetails
        if is_new_project:
            insert_data(
                "INSERT INTO ProjectDetails (project_name, project_path, project_lang, project_fw, project_tool, package_manager, project_type, run_commands) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (project_name, project_path,
                 det_language or prov_language,
                 det_framework or prov_framework,
                 det_tool or prov_tool,
                 det_packager or prov_packager,
                 'Existing', run_commands)
            )
            new_p = fetch_data("SELECT id FROM ProjectDetails WHERE project_name = ?", (project_name,))
            if new_p:
                project_id = new_p[0]['id']
            else:
                return jsonify({"status": "error", "message": "Failed to create project record."}), 500
        else:
            # Detected code wins; then payload-provided values; finally keep whatever is
            # already stored so an unresolved field is never blanked out on update.
            existing_settings = fetch_data(
                "SELECT project_lang, project_fw, project_tool, package_manager FROM ProjectDetails WHERE id = ?",
                (project_id,)
            )
            cur = existing_settings[0] if existing_settings else {}
            final_language = det_language or prov_language or (cur.get('project_lang') or '')
            final_framework = det_framework or prov_framework or (cur.get('project_fw') or '')
            final_tool = det_tool or prov_tool or (cur.get('project_tool') or '')
            final_packager = det_packager or prov_packager or (cur.get('package_manager') or '')
            update_data(
                "UPDATE ProjectDetails SET project_name=?, project_path=?, project_lang=?, project_fw=?, project_tool=?, package_manager=?, run_commands=? WHERE id=?",
                (project_name, project_path, final_language, final_framework, final_tool, final_packager, run_commands, project_id)
            )

        # 5. Insert or Update ProjectData
        encrypted_password = encrypt_for_app(app_password)
        pd_existing = fetch_data("SELECT id FROM ProjectData WHERE project_details_id = ?", (project_id,))
        if pd_existing:
            update_data(
                "UPDATE ProjectData SET baseurl=?, username=?, password=? WHERE project_details_id=?",
                (baseurl, app_username, encrypted_password, project_id)
            )
        else:
            insert_data(
                "INSERT INTO ProjectData (baseurl, username, password, project_details_id) VALUES (?, ?, ?, ?)",
                (baseurl, app_username, encrypted_password, project_id)
            )

        # 6. Insert or Update ProjectGitConfig
        pgc_existing = fetch_data("SELECT id FROM ProjectGitConfig WHERE project_details_id = ?", (project_id,))
        if pgc_existing:
            update_data(
                "UPDATE ProjectGitConfig SET repo_url=?, username=?, access_token=? WHERE project_details_id=?",
                (git_repo_url, git_username, git_pa_token, project_id)
            )
        else:
            insert_data(
                "INSERT INTO ProjectGitConfig (repo_url, username, access_token, project_details_id) VALUES (?, ?, ?, ?)",
                (git_repo_url, git_username, git_pa_token, project_id)
            )

        return jsonify({"status": "success", "message": "Project configuration saved successfully.", "project_id": project_id})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/get-project-config/<int:project_id>', methods=['GET'])
@login_required()
def get_project_config(project_id):
    try:
        pd = fetch_data("SELECT * FROM ProjectDetails WHERE id = ?", (project_id,))
        if not pd:
            return jsonify({"status": "error", "message": "Project not found"}), 404
        pd_row = dict(pd[0])
        
        pdata = fetch_data("SELECT baseurl, username, password FROM ProjectData WHERE project_details_id = ?", (project_id,))
        pdata_row = dict(pdata[0]) if pdata else {"baseurl": "", "username": "", "password": ""}
        pdata_row['password'] = decrypt_for_app(pdata_row.get('password'))
        
        git = fetch_data("SELECT repo_url, username, access_token FROM ProjectGitConfig WHERE project_details_id = ?", (project_id,))
        git_row = dict(git[0]) if git else {"repo_url": "", "username": "", "access_token": ""}
        
        path = pd_row.get('project_path', '')
        is_git_configured = False
        if path and os.path.exists(path):
            is_git_configured = os.path.exists(os.path.join(path, '.git'))
            
        return jsonify({
            "status": "success",
            "data": {
                "project_name": pd_row.get('project_name', ''),
                "project_path": pd_row.get('project_path', ''),
                "run_commands": pd_row.get('run_commands', ''),
                "baseurl": pdata_row.get('baseurl', ''),
                "app_username": pdata_row.get('username', ''),
                "app_password": pdata_row.get('password', ''),
                "git_repo_url": git_row.get('repo_url', ''),
                "git_username": git_row.get('username', ''),
                "git_pa_token": git_row.get('access_token', ''),
                "is_git_configured": is_git_configured
            }
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/save-git-config', methods=['POST'])
@login_required()
def save_git_config():
    try:
        create_git_config_table = """
        CREATE TABLE IF NOT EXISTS ProjectGitConfig (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_url TEXT,
            username TEXT,
            access_token TEXT,
            project_details_id INTEGER,
            FOREIGN KEY(project_details_id) REFERENCES ProjectDetails(id)
        );
        """
        update_data(create_git_config_table)
        
        data = request.json
        p_path = data.get('project_path', '').strip()
        repo_url = data.get('repo_url', '')
        username = data.get('username', '')
        access_token = data.get('access_token', '')
        
        if not p_path:
            return jsonify({"status": "error", "message": "Project path is required."}), 400
            
        existing = fetch_data("SELECT id FROM ProjectDetails WHERE project_path = ?", (p_path,))
        p_details_id = None
        
        if existing:
            p_details_id = existing[0]['id']
        else:
            return jsonify({"status": "error", "message": "Project not found in DB. Please select it properly."}), 404
                
        if p_details_id:
            gc_existing = fetch_data("SELECT id FROM ProjectGitConfig WHERE project_details_id = ?", (p_details_id,))
            if gc_existing:
                update_data("UPDATE ProjectGitConfig SET repo_url=?, username=?, access_token=? WHERE project_details_id=?", 
                            (repo_url, username, access_token, p_details_id))
            else:
                insert_data("INSERT INTO ProjectGitConfig (repo_url, username, access_token, project_details_id) VALUES (?, ?, ?, ?)",
                            (repo_url, username, access_token, p_details_id))
            
            # Sync git config to the local repository dynamically!
            try:
                from api.git_service import GitService
                GitService.sync_git_config(p_path, {"repo_url": repo_url, "username": username, "access_token": access_token})
            except Exception as sync_err:
                app.logger.warning(f"Git config sync error: {sync_err}")
                                
        return jsonify({"status": "success", "message": "Git configuration saved successfully."})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/get-git-config', methods=['GET'])
@login_required()
def get_git_config():
    try:
        p_path = request.args.get('project_path', '').strip()
        if not p_path:
            return jsonify({"status": "error", "message": "Project path required"}), 400
            
        existing = fetch_data("SELECT id FROM ProjectDetails WHERE project_path = ?", (p_path,))
        if existing:
            p_id = existing[0]['id']
            data = fetch_data("SELECT repo_url, username, access_token FROM ProjectGitConfig WHERE project_details_id = ?", (p_id,))
            if data:
                return jsonify({"status": "success", "data": data[0]})
        
        return jsonify({"status": "success", "data": {"repo_url": "", "username": "", "access_token": ""}})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

from api.git_service import GitService

@app.route('/api/git/native-action', methods=['POST'])
@login_required()
def git_native_action():
    try:
        data = request.json
        action = data.get('action')
        project_path = data.get('project_path')
        commit_message = data.get('commit_message')
        
        if not action or not project_path:
            return jsonify({"status": "error", "message": "Action and project_path are required"}), 400
            
        existing = fetch_data("SELECT id FROM ProjectDetails WHERE project_path = ?", (project_path,))
        if not existing:
            return jsonify({"status": "error", "message": "Project not found"}), 404
            
        p_id = existing[0]['id']
        git_config = fetch_data("SELECT repo_url, username, access_token FROM ProjectGitConfig WHERE project_details_id = ?", (p_id,))
        auth_config = git_config[0] if git_config else {}
        
        result = GitService.execute_native_action(action, project_path, auth_config, commit_message=commit_message)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

from api.mcp_client import run_mcp_git_prompt

@app.route('/api/git/mcp-action', methods=['POST'])
@login_required()
def git_mcp_action():
    try:
        data = request.json
        prompt = data.get('prompt')
        project_path = data.get('project_path')
        
        if not prompt or not project_path:
            return jsonify({"status": "error", "message": "Prompt and project_path are required"}), 400
            
        # Dynamically sync git config prior to MCP execution to make sure git credentials & user identity are utilized
        existing = fetch_data("SELECT id FROM ProjectDetails WHERE project_path = ?", (project_path,))
        if existing:
            p_id = existing[0]['id']
            git_config = fetch_data("SELECT repo_url, username, access_token FROM ProjectGitConfig WHERE project_details_id = ?", (p_id,))
            if git_config:
                try:
                    from api.git_service import GitService
                    GitService.sync_git_config(project_path, git_config[0])
                except Exception as sync_err:
                    app.logger.warning(f"Git config sync error prior to MCP action: {sync_err}")

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(run_mcp_git_prompt(prompt, project_path))
        finally:
            loop.close()
            
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/project-locators/<int:project_id>', methods=['GET'])
@login_required()
def project_locators(project_id):
    try:
        pages = fetch_data("SELECT DISTINCT Page_Name FROM Locators WHERE project_id = ?", (project_id,))
        page_names = [p['Page_Name'] for p in pages if p['Page_Name']]
        return jsonify({'status': 'success', 'data': page_names})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/db/tables', methods=['GET'])
@login_required(role='admin')
def get_db_tables():
    try:
        tables = fetch_data("SELECT name FROM sqlite_master WHERE type='table'")
        return jsonify({'tables': [t['name'] for t in tables]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/db/table-data', methods=['GET'])
@login_required(role='admin')
def get_table_data():
    try:
        table_name = request.args.get('table')
        if not table_name:
            return jsonify({'error': 'Table name is required'}), 400
        
        tables = fetch_data("SELECT name FROM sqlite_master WHERE type='table'")
        table_names = [t['name'] for t in tables]
        if table_name not in table_names:
            return jsonify({'error': 'Invalid table name'}), 400
            
        columns_info = fetch_data(f"PRAGMA table_info({table_name})")
        columns = [c['name'] for c in columns_info]
        
        data = fetch_data(f"SELECT * FROM {table_name}")
        return jsonify({'columns': columns, 'data': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/db-viewer', methods=['GET'])
@login_required(role='admin')
def db_viewer():
    return render_template('db_viewer.html')

from ScriptRunnerEngine.runner import ScriptRunnerService

@app.route('/api/script-runner/run', methods=['POST'])
@login_required()
def script_runner_run():
    data = request.json
    meta = data.get('project', {})
    env = data.get('environment', '')
    browser = data.get('browser', '')
    tags = data.get('tags', '')
    custom_commands = data.get('custom_commands', '')

    result = ScriptRunnerService.execute_with_healing(meta, env, browser, tags, custom_commands)
    return jsonify(result)

@app.route('/api/script-runner/stream')
@login_required()
def script_runner_stream():
    try:
        project_raw = request.args.get('project', '{}')
        print(f"DEBUG: Streaming request for project: {project_raw}")
        meta = json.loads(project_raw)
        env = request.args.get('environment', '')
        browser = request.args.get('browser', '')
        tags = request.args.get('tags', '')
        custom_commands = request.args.get('custom_commands', '')
        
        return Response(stream_with_context(ScriptRunnerService.execute_with_streaming(meta, env, browser, tags, custom_commands)), mimetype='text/event-stream')
    except Exception as e:
        print(f"DEBUG: Streaming route failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/script-runner/stop', methods=['POST'])
@login_required()
def script_runner_stop():
    from ScriptRunnerEngine.runner import active_processes
    import os, signal
    for pid in list(active_processes.keys()):
        try:
            # On Windows this might need taskkill, on Unix SIGTERM
            if os.name == 'nt':
                os.system(f"taskkill /F /T /PID {pid}")
            else:
                os.kill(pid, signal.SIGTERM)
        except: pass

@app.route('/api/git/execute-command', methods=['POST'])
@login_required()
def git_execute_command():
    try:
        data = request.json
        cmd = data.get('command', '').strip()
        project_path = data.get('project_path', '')
        
        if not cmd or not project_path:
            return jsonify({"output": "Command and project_path are required"}), 400
            
        # Security check: only allow commands starting with 'git'
        if not cmd.lower().startswith('git'):
            return jsonify({"output": "Only Git commands are allowed."}), 400
            
        # Check if project exists in database
        existing = fetch_data("SELECT id FROM ProjectDetails WHERE project_path = ?", (project_path,))
        if not existing:
            return jsonify({"output": "Project not found"}), 404
            
        import subprocess
        result = subprocess.run(cmd, cwd=project_path, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout + result.stderr
        return jsonify({"output": output})
    except Exception as e:
        return jsonify({"output": f"Error: {str(e)}"}), 500





@app.route('/api/script-runner/report', methods=['GET'])
@login_required()
def serve_report():
    file_path = request.args.get('path')
    if not file_path or not os.path.exists(file_path):
        return "Report not found", 404
    
    import mimetypes
    from flask import Response
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    
    mime_type, _ = mimetypes.guess_type(file_path)
    return Response(content, mimetype=mime_type or 'text/html')

@app.route('/api/configure-ai', methods=['GET', 'POST'])
@login_required()
def configure_ai_endpoint():
    env_path = os.path.join(BASE_DIR, '.env')
    
    if request.method == 'GET':
        config = {'AI_TOOL': '', 'AI_MODEL': '', 'API_KEY': ''}
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    if line.startswith('AI_TOOL'):
                        config['AI_TOOL'] = line.split('=', 1)[1].strip().strip('"').strip("'")
                    elif line.startswith('AI_MODEL'):
                        config['AI_MODEL'] = line.split('=', 1)[1].strip().strip('"').strip("'")
                    elif line.startswith('API_KEY'):
                        config['API_KEY'] = line.split('=', 1)[1].strip().strip('"').strip("'")
        return jsonify({"status": "success", "config": config})
        
    # POST
    data = request.json
    ai_tool = data.get('ai_tool')
    ai_model = data.get('ai_model')
    api_key = data.get('api_key')
    
    env_lines = []
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            env_lines = f.readlines()
            
    new_env_lines = []
    updated_keys = {'AI_TOOL': False, 'AI_MODEL': False, 'API_KEY': False}
    
    for line in env_lines:
        if line.startswith('AI_TOOL'):
            new_env_lines.append(f'AI_TOOL = "{ai_tool}"\n')
            updated_keys['AI_TOOL'] = True
        elif line.startswith('AI_MODEL'):
            new_env_lines.append(f'AI_MODEL = "{ai_model}"\n')
            updated_keys['AI_MODEL'] = True
        elif line.startswith('API_KEY'):
            new_env_lines.append(f'API_KEY = "{api_key}"\n')
            updated_keys['API_KEY'] = True
        else:
            new_env_lines.append(line)
            
    if not updated_keys['AI_TOOL']:
        new_env_lines.append(f'AI_TOOL = "{ai_tool}"\n')
    if not updated_keys['AI_MODEL']:
        new_env_lines.append(f'AI_MODEL = "{ai_model}"\n')
    if not updated_keys['API_KEY']:
        new_env_lines.append(f'API_KEY = "{api_key}"\n')
        
    with open(env_path, 'w') as f:
        f.writelines(new_env_lines)
        
    # Also update current env for immediately running backend.py or others
    os.environ['AI_TOOL'] = str(ai_tool)
    os.environ['AI_MODEL'] = str(ai_model)
    os.environ['API_KEY'] = str(api_key)
        
    status_data = None
    try:
        import urllib.request
        import json
        req = urllib.request.Request('http://127.0.0.1:8000/health')
        with urllib.request.urlopen(req, timeout=3) as response:
            status_data = json.loads(response.read().decode())
    except Exception as e:
        status_data = {"error": str(e)}
        
    return jsonify({"status": "success", "system_status": status_data})

@app.route('/api/configure-jira', methods=['GET', 'POST'])
@login_required()
def configure_jira_endpoint():
    env_path = os.path.join(BASE_DIR, '.env')
    
    if request.method == 'GET':
        config = {'jira_server': '', 'jira_email': '', 'jira_api_token': ''}
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    if line.startswith('JIRA_SERVER') or line.startswith('jira_server'):
                        config['jira_server'] = line.split('=', 1)[1].strip().strip('"').strip("'")
                    elif line.startswith('JIRA_EMAIL') or line.startswith('jira_email'):
                        config['jira_email'] = line.split('=', 1)[1].strip().strip('"').strip("'")
                    elif line.startswith('JIRA_API_TOKEN') or line.startswith('jira_api_token'):
                        config['jira_api_token'] = line.split('=', 1)[1].strip().strip('"').strip("'")
        return jsonify({"status": "success", "config": config})
        
    # POST
    data = request.json
    jira_server = data.get('jira_server')
    jira_email = data.get('jira_email')
    jira_api_token = data.get('jira_api_token')
    
    env_lines = []
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            env_lines = f.readlines()
            
    new_env_lines = []
    updated_keys = {'JIRA_SERVER': False, 'JIRA_EMAIL': False, 'JIRA_API_TOKEN': False}
    
    for line in env_lines:
        if line.startswith('JIRA_SERVER') or line.startswith('jira_server'):
            new_env_lines.append(f'JIRA_SERVER = "{jira_server}"\n')
            updated_keys['JIRA_SERVER'] = True
        elif line.startswith('JIRA_EMAIL') or line.startswith('jira_email'):
            new_env_lines.append(f'JIRA_EMAIL = "{jira_email}"\n')
            updated_keys['JIRA_EMAIL'] = True
        elif line.startswith('JIRA_API_TOKEN') or line.startswith('jira_api_token'):
            new_env_lines.append(f'JIRA_API_TOKEN = "{jira_api_token}"\n')
            updated_keys['JIRA_API_TOKEN'] = True
        else:
            new_env_lines.append(line)
            
    if not updated_keys['JIRA_SERVER']:
        new_env_lines.append(f'JIRA_SERVER = "{jira_server}"\n')
    if not updated_keys['JIRA_EMAIL']:
        new_env_lines.append(f'JIRA_EMAIL = "{jira_email}"\n')
    if not updated_keys['JIRA_API_TOKEN']:
        new_env_lines.append(f'JIRA_API_TOKEN = "{jira_api_token}"\n')
        
    with open(env_path, 'w') as f:
        f.writelines(new_env_lines)
        
    os.environ['JIRA_SERVER'] = str(jira_server)
    os.environ['JIRA_EMAIL'] = str(jira_email)
    os.environ['JIRA_API_TOKEN'] = str(jira_api_token)
        
    return jsonify({"status": "success"})

import ast

def _get_prompt_functions(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
        tree = ast.parse(source)
        functions = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                func_info = {
                    "name": node.name,
                    "start": node.lineno,
                    "end": node.end_lineno
                }
                for child in ast.walk(node):
                    if isinstance(child, ast.Return):
                        func_info["return_start"] = child.lineno
                        func_info["return_end"] = child.end_lineno
                        break
                functions.append(func_info)
        return functions
    except Exception as e:
        print(f"Error parsing {filepath}: {e}")
        return []

@app.route('/qa/configure-prompts', methods=['GET'])
@login_required()
def configure_prompts():
    if session.get('user_role', '').lower() not in ['qa', 'admin']:
        flash('Not authorized', 'error')
        return redirect(url_for('home'))
    return render_template('configure_prompts.html')

@app.route('/api/prompts', methods=['GET'])
@login_required()
def list_prompts():
    prompts_dir = ROOT_DIR / "prompts"
    if not prompts_dir.exists():
        return jsonify({"status": "error", "message": "Prompts directory not found"}), 404
        
    prompt_list = []
    for py_file in prompts_dir.glob("*.py"):
        if py_file.name == "__init__.py":
            continue
        functions = _get_prompt_functions(py_file)
        for fn in functions:
            prompt_list.append({
                "file": py_file.name,
                "function": fn["name"]
            })
            
    return jsonify({"status": "success", "data": prompt_list})

@app.route('/api/prompts/<filename>/<function_name>', methods=['GET', 'POST'])
@login_required()
def handle_prompt_function(filename, function_name):
    prompts_dir = ROOT_DIR / "prompts"
    file_path = prompts_dir / filename
    
    if not file_path.exists():
        return jsonify({"status": "error", "message": "File not found"}), 404
        
    functions = _get_prompt_functions(file_path)
    target_fn = next((f for f in functions if f["name"] == function_name), None)
    
    if not target_fn:
        return jsonify({"status": "error", "message": "Function not found in file"}), 404
        
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    if request.method == 'GET':
        if "return_start" in target_fn and "return_end" in target_fn:
            start_idx = target_fn["return_start"] - 1
            end_idx = target_fn["return_end"]
        else:
            start_idx = target_fn["start"] - 1
            end_idx = target_fn["end"]
            
        fn_source = "".join(lines[start_idx : end_idx])
        
        text = fn_source.strip()
        if text.startswith('return'):
            text = text[6:].strip()
        if text.startswith('(') and text.endswith(')'):
            text = text[1:-1]
            if text.startswith('\n'): text = text[1:]
            if text.endswith('\n'): text = text[:-1]
            
        return jsonify({"status": "success", "content": text})
        
    if request.method == 'POST':
        data = request.json
        new_content = data.get('content', '')
        if not new_content:
            return jsonify({"status": "error", "message": "Content cannot be empty"}), 400
            
        if "return_start" in target_fn and "return_end" in target_fn:
            start_idx = target_fn["return_start"] - 1
            end_idx = target_fn["return_end"]
        else:
            start_idx = target_fn["start"] - 1
            end_idx = target_fn["end"]
            
        return_first_line = lines[start_idx]
        indent = return_first_line[:len(return_first_line) - len(return_first_line.lstrip())]
        
        if not new_content.endswith('\n'):
            new_content += '\n'
            
        wrapped_content = f"{indent}return (\n{new_content}{indent})\n"
        
        prefix = lines[:start_idx]
        suffix = lines[end_idx:]
        
        new_lines = prefix + [wrapped_content] + suffix
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
            
        return jsonify({"status": "success", "message": "Prompt updated successfully"})

def open_browser(port=5000):
    webbrowser.open_new(f'http://127.0.0.1:{port}/')

def find_free_port(preferred=5000):
    """Return a port to run on: the preferred one if free, otherwise an OS-assigned free port.

    Avoids the 'Address already in use' crash when a previous instance is still running. The chosen
    port is cached in BIG_QA_PORT so the reloader's child process reuses the same URL across restarts.
    """
    import socket
    cached = os.environ.get('BIG_QA_PORT')
    if cached:
        return int(cached)
    port = preferred
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', preferred))
    except OSError:
        s.bind(('127.0.0.1', 0))  # preferred busy — let the OS hand us a free one
        port = s.getsockname()[1]
    finally:
        s.close()
    os.environ['BIG_QA_PORT'] = str(port)
    return port

def launch_backend():
    """Launches the FastAPI backend service using uvicorn."""
    try:
        # Launching with uvicorn directly if possible, or as a background process
        # We use the full module path to ensure it's findable
        env = os.environ.copy()
        env["PYTHONPATH"] = str(BASE_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        
        # Run uvicorn as a subprocess to keep it alive in the background
        cmd = [
            sys.executable, "-m", "uvicorn", 
            "api.backend:app", 
            "--host", "127.0.0.1", 
            "--port", "8000"
        ]
        
        subprocess.Popen(cmd, cwd=str(BASE_DIR), env=env)
        print("AI Backend service (Port 8000) launched via uvicorn.")
    except Exception as e:
        print(f"Failed to launch backend service: {e}")

def check_and_initialize_db():
    print("Checking database connection...")
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
        print("Database connection successful.")
        init_db()
        return True
    except Exception as e:
        print(f"Database connection failed: {e}")
        return False

import asyncio
import pandas as pd
from api.code_injector import CodeInjector
from api.backend import UniversalScriptGenerator, DEFAULT_AI_PROVIDER

@app.route('/api/generate-bdd-code', methods=['POST'])
@login_required()
def generate_bdd_code():
    try:
        project_path = request.form.get('project_path')
        tool = request.form.get('tool')
        language = request.form.get('language')
        strategy = request.form.get('mapping_strategy')
        file_type = request.form.get('file_type')
        project_id = request.form.get('project_id')
        db_locators = request.form.get('db_locators') 
        
        scenario_file = request.files.get('scenario_file')
        po_files = request.files.getlist('po_files')
        
        if not scenario_file:
            return jsonify({'status':'error', 'message': 'No scenario file uploaded'}), 400
            
        scenarios_text = ""
        if file_type == 'BDD':
            scenarios_text = scenario_file.read().decode('utf-8')
        elif file_type == 'Excel':
            try:
                # Need openpyxl for xlsx
                df = pd.read_excel(scenario_file)
                df = df.head(10)
                scenarios_text = df.to_string()
            except ImportError:
                return jsonify({'status':'error', 'message': 'Missing pandas or openpyxl. Run: pip install pandas openpyxl'}), 500
            except Exception as e:
                return jsonify({'status':'error', 'message': f'Error reading Excel: {e}'}), 500
            
        framework = request.form.get('framework')
        if not framework:
            framework = "Pytest" if language.lower() == "python" else "Cucumber" if language.lower() == "java" else "TypeScript"

        support_content = f"Tool: {tool}\nLanguage: {language}\nStrategy: {strategy}\nProject Path: {project_path}\n"
        
        if file_type == 'BDD' and scenario_file:
            fname = scenario_file.filename
            if os.path.exists(os.path.join(project_path, fname)) or os.path.exists(os.path.join(project_path, "features", fname)):
                support_content += "\nDO NOT generate the .feature file.\n"

        # Path Discovery
        discovered_features = "features"
        discovered_steps = "steps"
        discovered_pages = "pages"
        
        if project_path and os.path.exists(project_path):
            for root, dirs, files in os.walk(project_path):
                # Folder exclusions
                if any(skip in root for skip in ['node_modules', '.git', 'venv', 'target', 'bin']):
                    dirs[:] = []
                    continue
                
                # Depth limit check
                depth = root[len(project_path):].count(os.sep)
                if depth > 8:
                    dirs[:] = []
                    continue
                
                lower_root = root.lower()
                rel_path = os.path.relpath(root, project_path)
                if rel_path == ".":
                    continue
                    
                basename_lower = os.path.basename(lower_root)
                if "feature" in basename_lower or any(f.endswith(".feature") for f in files):
                    if discovered_features == "features": discovered_features = rel_path
                if "step" in basename_lower or any("step" in f.lower() for f in files):
                    if discovered_steps == "steps": discovered_steps = rel_path
                if "page" in basename_lower or any("page" in f.lower() for f in files):
                    if discovered_pages == "pages": discovered_pages = rel_path

        support_content += f"\nProject Layout Mappings (CRITICAL):\n- Feature Files MUST be placed inside: {discovered_features}\n- Step Definition Files MUST be placed inside: {discovered_steps}\n- Page Object Files MUST be placed inside: {discovered_pages}\n"

        # Collect existing step definitions to avoid regenerating them and to match style
        if project_path and os.path.exists(project_path):
            try:
                steps_dir = os.path.join(project_path, discovered_steps)
                if os.path.exists(steps_dir) and os.path.isdir(steps_dir):
                    existing_steps = []
                    for root, _, files in os.walk(steps_dir):
                        for f in files:
                            if f.endswith(('.py', '.java', '.ts', '.js', '.cs')):
                                try:
                                    with open(os.path.join(root, f), 'r', encoding='utf-8', errors='ignore') as st_file:
                                        existing_steps.append(f"--- {f} ---\n{st_file.read()}")
                                except Exception:
                                    pass
                    if existing_steps:
                        # Truncate to avoid massive context payloads, but usually steps are manageable
                        steps_text = "\n".join(existing_steps)[:20000]
                        support_content += (
                            "\nEXISTING STEP DEFINITIONS (STRICT DUPLICATE-PREVENTION REFERENCE):\n"
                            "The following step definition files ALREADY EXIST in the project. Use them as the\n"
                            "authoritative reference for coding style, naming, imports, and patterns.\n\n"
                            "STRICT RULES (MUST be followed exactly):\n"
                            "1. DO NOT generate, re-create, or re-emit a step definition for ANY step whose\n"
                            "   definition already exists below. Match by the step's regex/expression pattern\n"
                            "   AND its meaning - if an existing definition already matches a Gherkin step\n"
                            "   (Given/When/Then/And/But/Or), that step is ALREADY IMPLEMENTED. Skip it entirely.\n"
                            "2. Treat parameterized steps as matching: a step like \"I enter \\\"admin\\\"\" is covered\n"
                            "   by an existing definition with a string/regex parameter. Do NOT create a new one.\n"
                            "3. ONLY generate step definitions for steps that have NO matching existing definition.\n"
                            "   If every step in a scenario is already defined, produce NO new step definition code\n"
                            "   for that scenario.\n"
                            "4. NEVER redefine, override, or duplicate an existing step - doing so causes\n"
                            "   'Ambiguous'/'DuplicateStepException' errors at runtime.\n"
                            "5. Reuse the existing definitions and page-object methods they call; do not invent\n"
                            "   parallel implementations.\n"
                            "6. Import all required packages while generating the step definition files \n"
                            f"{steps_text}\n"
                        )
            except Exception as e:
                app.logger.warning(f"Error scanning existing steps: {e}")

        # Collect existing Page Objects for style and pattern matching
        if project_path and os.path.exists(project_path):
            try:
                pages_dir = os.path.join(project_path, discovered_pages)
                if os.path.exists(pages_dir) and os.path.isdir(pages_dir):
                    existing_pages = []
                    for root, _, files in os.walk(pages_dir):
                        for f in files:
                            if f.endswith(('.py', '.java', '.ts', '.js', '.cs')):
                                try:
                                    with open(os.path.join(root, f), 'r', encoding='utf-8', errors='ignore') as pg_file:
                                        existing_pages.append(f"--- {f} ---\n{pg_file.read()}")
                                except Exception:
                                    pass
                    if existing_pages:
                        pages_text = "\n".join(existing_pages)[:20000]
                        support_content += f"\nEXISTING PAGE OBJECTS (STYLE REFERENCE - Match styling exactly):\n{pages_text}\n"
            except Exception as e:
                app.logger.warning(f"Error scanning existing pages: {e}")


        if strategy == 'db' and project_id and db_locators:
            try:
                locs = json.loads(db_locators)
                support_content += "\nDB Locators:\n"
                for page in locs:
                    support_content += f"\n[Page Object: {page}]\n"
                    db_data = fetch_data("SELECT Locator_Name, Locator_Type, Locator_Value FROM Locators WHERE project_id = ? AND Page_Name = ?", (project_id, page))
                    for row in db_data:
                        support_content += f"  - {row['Locator_Name']} (Type: {row['Locator_Type']}): {row['Locator_Value']}\n"
            except Exception:
                pass
        elif strategy == 'local':
            support_content += "\nDO NOT generate Page Object classes.\n"
            support_content += "Local Page Object Files:\n"
            for po in po_files:
                if po.filename:
                    support_content += f"--- {po.filename} ---\n{po.read().decode('utf-8')}\n"

        # Scan for utilities
        support_content += "\nProject Reusable Utilities:\n"
        for util_dir in ["utils", "utilities", "reusables", "src/main/java/utils"]:
            u_path = os.path.join(project_path, util_dir)
            if os.path.exists(u_path) and os.path.isdir(u_path):
                for uf in os.listdir(u_path):
                    if uf.endswith((".py", ".java", ".ts", ".js", ".cs")):
                        try:
                            with open(os.path.join(u_path, uf), "r", encoding="utf-8") as f:
                                support_content += f"--- {uf} ---\n{''.join(f.readlines()[:100])}\n"
                        except Exception:
                            pass

        generator = UniversalScriptGenerator(DEFAULT_AI_PROVIDER, tool, language, framework)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        support_content += "\nFile Mode: New\n"
        
        parsed_files = loop.run_until_complete(generator.generate(scenarios_text, support_content, ""))
        
        if isinstance(parsed_files, dict):
            # Enforce selective generation output
            keys_to_delete = []
            for k in parsed_files.keys():
                k_lower = k.lower()
                if "\nDO NOT generate the .feature file.\n" in support_content and k_lower.endswith(".feature"):
                    keys_to_delete.append(k)
                elif "\nDO NOT generate Page Object classes.\n" in support_content and ("page" in k_lower or "pom" in k_lower):
                    keys_to_delete.append(k)
                # Universal boilerplate stripping
                elif any(bp in k_lower for bp in ["pom.xml", "package.json", "requirements.txt", "driverfactory", "hooks", "conftest.py", "playwright.config", "tsconfig.json", ".csproj", ".sln", "specflow.json", "usings.cs", "Runner"]):
                    keys_to_delete.append(k)
            for k in keys_to_delete:
                del parsed_files[k]
                
        result_files = []
        if isinstance(parsed_files, dict):
            for k, v in parsed_files.items():
                result_files.append({'filename': k, 'content': v, 'path': os.path.join(project_path, k)})
        else:
            result_files.append({'filename': 'generated_code.txt', 'content': "// AI Failed to return valid dict formats.", 'path': os.path.join(project_path, 'generated_code.txt')})

        return jsonify({'status': 'success', 'files': result_files})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/save-generated-files', methods=['POST'])
@login_required()
def save_generated_files():
    try:
        data = request.json
        project_id = data.get('project_id')
        files = data.get('files', [])
        
        if not files:
            return jsonify({'status': 'error', 'message': 'No files provided'}), 400

        backup_id = int(datetime.now().timestamp())
        
        for f in files:
            target_path = f.get('target_path')
            content = f.get('content')
            filename = f.get('filename')
            
            # Create Backup
            if os.path.exists(target_path):
                with open(target_path, 'r', encoding='utf-8') as exists_f:
                    existing_content = exists_f.read()
                
                # Insert DB Backup
                insert_data(
                    "INSERT INTO Backupfiles (Project_ID, FileName, FileContent, FilePath, BackupID, CreatedOn, Type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (project_id, filename, existing_content.encode('utf-8'), target_path, backup_id, datetime.now(), "Backup")
                )
                
                # Safe Inject
                lang = "python" if filename.endswith('.py') else "java" if filename.endswith('.java') else "ts"
                final_content = CodeInjector.inject_methods_safely(existing_content, content, lang)
            else:
                # Insert DB Backup marker for 'New'
                insert_data(
                    "INSERT INTO Backupfiles (Project_ID, FileName, FileContent, FilePath, BackupID, CreatedOn, Type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (project_id, filename, b'', target_path, backup_id, datetime.now(), "New")
                )
                final_content = content
                
            # Write
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, 'w', encoding='utf-8') as out_f:
                out_f.write(final_content)
                
        return jsonify({'status': 'success', 'backup_id': backup_id})
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/preview-merge', methods=['POST'])
@login_required()
def preview_merge():
    try:
        data = request.json
        target_path = data.get('target_path')
        content = data.get('content')
        filename = data.get('filename')

        existing_content = ""
        merged_content = content

        if os.path.exists(target_path):
            with open(target_path, 'r', encoding='utf-8') as exists_f:
                existing_content = exists_f.read()
            
            # Merged content is no longer generated on the fly to save time, as the UI expects the user to manually merge from the newly generated file.
            merged_content = content

        return jsonify({
            'status': 'success',
            'existing_content': existing_content,
            'merged_content': merged_content
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/smart-merge', methods=['POST'])
@login_required()
def smart_merge():
    try:
        data = request.json
        existing_content = data.get('existing_content', '')
        generated_content = data.get('generated_content', '')
        filename = data.get('filename', 'file.py')
        
        from utils.smart_merger import smart_merge_code
        merged = smart_merge_code(existing_content, generated_content, filename)
        
        return jsonify({
            'status': 'success',
            'merged_content': merged
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/save-merged-file', methods=['POST'])
@login_required()
def save_merged_file():
    try:
        data = request.json
        project_id = data.get('project_id')
        target_path = data.get('target_path')
        final_content = data.get('final_content')
        filename = data.get('filename')

        backup_id = int(datetime.now().timestamp())

        if os.path.exists(target_path):
            with open(target_path, 'r', encoding='utf-8') as exists_f:
                existing_content = exists_f.read()
            
            insert_data(
                "INSERT INTO Backupfiles (Project_ID, FileName, FileContent, FilePath, BackupID, CreatedOn, Type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, filename, existing_content.encode('utf-8'), target_path, backup_id, datetime.now(), "Backup")
            )
        else:
            insert_data(
                "INSERT INTO Backupfiles (Project_ID, FileName, FileContent, FilePath, BackupID, CreatedOn, Type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, filename, b'', target_path, backup_id, datetime.now(), "New")
            )

        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, 'w', encoding='utf-8') as out_f:
            out_f.write(final_content)

        return jsonify({'status': 'success', 'backup_id': backup_id})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/rollback-files', methods=['POST'])
@login_required()
def rollback_files():
    try:
        data = request.json
        backup_id = data.get('backup_id')
        
        if not backup_id:
            return jsonify({'status': 'error', 'message': 'Backup ID required'}), 400
            
        backups = fetch_data("SELECT * FROM Backupfiles WHERE BackupID = ?", (backup_id,))
        if not backups:
            return jsonify({'status': 'error', 'message': 'Backup not found'}), 404
            
        for b in backups:
            path = b['FilePath']
            b_type = b['Type']
            content = b['FileContent']
            
            if b_type == 'New':
                if os.path.exists(path):
                    os.remove(path)
            elif b_type == 'Backup':
                with open(path, 'wb') as f:
                    f.write(content)
                    
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/health-status')
@login_required()
def health_status():
    status = {"db": "Healthy", "ai": "Healthy", "server": "Running"}
    
    # Check Database
    try:
        fetch_data("SELECT 1")
    except Exception:
        status["db"] = "Error"
        
    # Check AI Config
    if not os.getenv("API_KEY"):
        status["ai"] = "Key Missing"
        
    return jsonify(status)

@app.route('/sample-app/login', methods=['GET', 'POST'])
def sample_app_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == 'admin' and password == 'password123':
            return "Login Successful! Welcome to the sample dashboard."
        return "Invalid credentials."
    return render_template('sample_login.html')

if __name__ == '__main__':
    port = find_free_port(5000)
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # serve fresh static files during dev
    # Only open the browser and launch backend once (prevents opening twice when Flask reloader is active)
    if not os.environ.get('WERKZEUG_RUN_MAIN'):
        # Install python dependencies from root requirements.txt
        req_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'requirements.txt'))
        if os.path.exists(req_path):
            print(f"Installing dependencies from {req_path}...", flush=True)
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_path])
                print("Dependencies installed successfully.", flush=True)
            except Exception as e:
                print(f"Failed to install dependencies: {e}", flush=True)

        if not check_and_initialize_db():
            sys.exit("Exiting: Database connection failed.")
        threading.Timer(1.25, lambda: open_browser(port)).start()
        #threading.Timer(1.25, seed).start()
        seed()
        launch_backend()

    # Watch templates/static too so HTML/CSS/JS edits also restart the server (triggering the reload).
    extra_files = [str(p) for sub in ('templates', 'static')
                   for p in (BASE_DIR / sub).rglob('*') if p.is_file()]
    app.run(debug=True, use_reloader=True, port=port, threaded=True, extra_files=extra_files)
