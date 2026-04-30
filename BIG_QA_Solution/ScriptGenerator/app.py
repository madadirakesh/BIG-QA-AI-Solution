import os
import sys
import subprocess
import threading
import webbrowser
import uuid
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
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

app = Flask(__name__)
# In production, use os.environ.get('SECRET_KEY')
app.secret_key = 'your_super_secret_flask_key_here'

@app.context_processor
def inject_user():
    return dict(
        user_name=session.get('user_name', 'Guest'),
        user_role=session.get('user_role', 'guest').lower()
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

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('home'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
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
            if user['verified'] == 0:
                flash(f"Hi, {user['name']}! Your account is pending approval.", 'warning')
            elif user['verified'] == 2 and user['password'] == password:
                flash("Account Disabled", 'error')
            elif user['verified'] == 1 and user['password'] != password:
                flash('Login failed! Invalid username or password.', 'error')
            elif user['verified'] == 1 and user['password'] == password:
                # Login successful
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
    session.clear()
    flash('You have been logged out!', 'info')
    return redirect(url_for('login'))

@app.route('/home')
@login_required()
def home():
    return render_template('home.html')

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
                                (name, email, role, password))
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

def _bootstrapper_worker(job_id, p_name, p_path, tool, lang, fw, pm, url, user, pwd):
    try:
        success, res = BootstrapperEngine.generate_project(p_name, p_path, tool, lang, fw, pm, url, user, pwd)
        if not success:
            bootstrapper_jobs[job_id] = {"status": "error", "message": f"Scaffolding failed: {res}"}
            return

        target_dir = res
        inst_success, inst_msg = EnvironmentSetup.install_project_dependencies(target_dir, pm, tool)
        if not inst_success:
            bootstrapper_jobs[job_id] = {"status": "error", "message": f"Dependency installation failed: {inst_msg}"}
            return

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

def _get_directory_path(prompt="Select Directory"):
    """Helper to get a directory path across different platforms without blocking Flask."""
    try:
        import platform
        import json
        safe_prompt = json.dumps(prompt)
        
        if platform.system() == 'Darwin':
            # MacOS: Use Native AppleScript (reliable, no threading issues)
            script = f'tell application "System Events" to activate\ntell application "System Events"\nset folderPath to choose folder with prompt {safe_prompt}\nPOSIX path of folderPath\nend tell'
            result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        else:
            # Windows/Linux: Use a separate process with Tkinter to avoid main-thread GUI locks
            script = f"import tkinter as tk; from tkinter import filedialog; root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); folder_path = filedialog.askdirectory(title={safe_prompt}); print(folder_path, end='')"
            
            # On Windows, we need to hide the console window for the subprocess
            creation_flags = 0
            if platform.system() == 'Windows':
                creation_flags = 0x08000000 # CREATE_NO_WINDOW
                
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, creationflags=creation_flags)
            if result.returncode == 0 and result.stdout.strip():
                import os
                return os.path.normpath(result.stdout.strip())
        return ""
    except Exception:
        return ""

@app.route('/api/browse-directory', methods=['GET'])
def browse_directory():
    path = _get_directory_path("Select Project Save Location")
    return jsonify({"path": path})

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
    url = data.get('url', '')
    user = data.get('username', '')
    pwd = data.get('password', '')

    if not p_name or not p_path:
        return jsonify({"status": "error", "message": "Project name and path are required."}), 400

    env_ok, missing = EnvironmentSetup.verify_environment(lang)
    if not env_ok:
        return jsonify({"status": "error", "message": f"Missing system dependencies: {', '.join(missing)}"}), 400

    job_id = str(uuid.uuid4())
    bootstrapper_jobs[job_id] = {"status": "processing", "message": "Initializing process..."}

    threading.Thread(target=_bootstrapper_worker, 
                     args=(job_id, p_name, p_path, tool, lang, fw, pm, url, user, pwd), 
                     daemon=True).start()

    return jsonify({"status": "processing", "job_id": job_id})

@app.route('/api/bootstrap-status/<job_id>', methods=['GET'])
@login_required()
def bootstrap_status(job_id):
    if job_id not in bootstrapper_jobs:
        return jsonify({"status": "error", "message": "Job ID not found"}), 404
        
    job = bootstrapper_jobs[job_id]
    
    # Auto-insert into database on completion to save a frontend roundtrip
    if job.get('status') == 'completed' and 'db_inserted' not in job:
        meta = job['project_metadata']
        try:
            insert_data("INSERT INTO ProjectDetails (project_name, project_path, project_lang, project_fw, project_tool, package_manager, project_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (meta['projectName'], meta['projectPath'], meta['language'], meta['framework'], meta['tool'], meta.get('packageManager'), 'New'))
            
            # Save Project Data (URL/Credentials)
            new_id_res = fetch_data("SELECT id FROM ProjectDetails WHERE project_path = ?", (meta['projectPath'],))
            if new_id_res:
                p_id = new_id_res[0]['id']
                insert_data("INSERT INTO ProjectData (baseurl, username, password, project_details_id) VALUES (?, ?, ?, ?)",
                            (meta.get('url'), meta.get('username'), meta.get('password'), p_id))
        except Exception as e:
            pass 
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
        
    projects = fetch_data("SELECT * FROM ProjectDetails")
    return render_template('script_runner.html', projects=projects)

@app.route('/qa/test-case-generator', methods=['GET'])
@login_required()
def test_case_generator():
    if session.get('user_role', '').lower() not in ['qa', 'admin']:
        flash('Not authorized', 'error')
        return redirect(url_for('home'))
    return render_template('test_case_generator.html')

@app.route('/qa/launch-element-locator', methods=['GET'])
@login_required()
def launch_element_locator():
    try:
        locator_path = os.path.join(os.path.dirname(__file__), '..', 'ElementLocator', 'main.py')
        subprocess.Popen([sys.executable, locator_path], shell=(sys.platform == 'win32'))
        return jsonify({'status': 'success', 'message': 'Element Locator Studio launched.'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

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
        for root, dirs, files in os.walk(path):
            if any(skip in root for skip in ['node_modules', '.git', 'venv', 'target', 'bin']):
                continue
            for file in files:
                ext = os.path.splitext(file)[1]
                if ext not in extensions_found:
                    extensions_found.append(ext)
                if file in ['pom.xml', 'package.json', 'requirements.txt', 'build.gradle'] or file.endswith('.csproj'):
                    try:
                        with open(os.path.join(root, file), 'r', errors='ignore') as f:
                            content = f.read().lower()

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

        return jsonify({"tool": tool, "language": language, "framework": framework, "packager": packager})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/save-project-config', methods=['POST'])
@login_required()
def save_project_config():
    try:
        create_project_data_table = """
        CREATE TABLE IF NOT EXISTS ProjectData (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            baseurl TEXT,
            username TEXT,
            password TEXT,
            project_details_id INTEGER,
            FOREIGN KEY(project_details_id) REFERENCES ProjectDetails(id)
        );
        """
        update_data(create_project_data_table)
        
        data = request.json
        p_path = data.get('project_path', '').strip()
        baseurl = data.get('baseurl', '')
        username = data.get('username', '')
        password = data.get('password', '')
        lang = data.get('language', 'Unknown')
        fw = data.get('framework', 'Unknown')
        tool = data.get('tool', 'Unknown')
        packager  = data.get('packager', 'Unknown')
        
        if not p_path:
            return jsonify({"status": "error", "message": "Project path is required."}), 400
            
        existing = fetch_data("SELECT id FROM ProjectDetails WHERE project_path = ?", (p_path,))
        p_details_id = None
        
        if existing:
            p_details_id = existing[0]['id']
        else:
            p_name = os.path.basename(os.path.normpath(p_path))
            if not p_name:
                p_name = "Local_Project"
            insert_data("INSERT INTO ProjectDetails (project_name, project_path, project_lang, project_fw, project_tool, package_manager, project_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (p_name, p_path, lang, fw, tool, packager, 'Existing'))
            new_record = fetch_data("SELECT id FROM ProjectDetails WHERE project_path = ?", (p_path,))
            if new_record:
                p_details_id = new_record[0]['id']
                
        if p_details_id:
            pd_existing = fetch_data("SELECT id FROM ProjectData WHERE project_details_id = ?", (p_details_id,))
            if pd_existing:
                update_data("UPDATE ProjectData SET baseurl=?, username=?, password=? WHERE project_details_id=?", 
                            (baseurl, username, password, p_details_id))
            else:
                insert_data("INSERT INTO ProjectData (baseurl, username, password, project_details_id) VALUES (?, ?, ?, ?)",
                            (baseurl, username, password, p_details_id))
                                
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/get-project-config/<int:project_id>', methods=['GET'])
@login_required()
def get_project_config(project_id):
    try:
        data = fetch_data("SELECT baseurl, username, password FROM ProjectData WHERE project_details_id = ?", (project_id,))
        if data:
            return jsonify({"status": "success", "data": data[0]})
        return jsonify({"status": "success", "data": {"baseurl": "", "username": "", "password": ""}})
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

    result = ScriptRunnerService.execute_with_healing(meta, env, browser, tags)
    return jsonify(result)

@app.route('/api/script-runner/execute-cmd', methods=['POST'])
@login_required()
def script_runner_execute_cmd():
    data = request.json
    cmd = data.get('command', '')
    project_path = data.get('project_path', '')
    
    result = ScriptRunnerService.execute_cmd(cmd, project_path)
    return jsonify(result)

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

def open_browser():
    webbrowser.open_new('http://127.0.0.1:5000/')

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

if __name__ == '__main__':
    # Only open the browser and launch backend once (prevents opening twice when Flask reloader is active)
    if not os.environ.get('WERKZEUG_RUN_MAIN'):
        if not check_and_initialize_db():
            sys.exit("Exiting: Database connection failed.")
        threading.Timer(1.25, open_browser).start()
        #threading.Timer(1.25, seed).start()
        seed()
        launch_backend()
    
    app.run(debug=True, port=5000)
