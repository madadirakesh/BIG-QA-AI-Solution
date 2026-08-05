import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "local_database.db")
TRANSIENT_PROJECT_NAME_PREFIXES = ("mcp-test-", "strict-")
TRANSIENT_PROJECT_PATH_PREFIXES = ("/tmp/mcp-git-e2e-",)


def _seed_admin_password():
    # Store the default admin password hashed. Plaintext fallback only if the
    # util can't be imported (standalone run); login still upgrades it on first use.
    try:
        from utils.password_util import hash_password
        return hash_password('admin123')
    except Exception:
        return 'admin123'

def get_db():
    try:
        from flask import g, has_app_context
        if has_app_context():
            if 'db' not in g:
                g.db = sqlite3.connect(DB_PATH)
                g.db.row_factory = sqlite3.Row
            return g.db
    except ImportError:
        pass
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def execute_query(query, params=None):
    conn = get_db()
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(query, params or [])
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        print(f"Error executing query: {e}")
        return []
    finally:
        try:
            from flask import g, has_app_context
            if not (has_app_context() and hasattr(g, 'db') and g.db is conn):
                conn.close()
        except ImportError:
            conn.close()

def execute_update(query, params=None):
    conn = get_db()
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(query, params or [])
            return cursor.rowcount
    except Exception as e:
        print(f"Error executing update: {e}")
        return None
    finally:
        try:
            from flask import g, has_app_context
            if not (has_app_context() and hasattr(g, 'db') and g.db is conn):
                conn.close()
        except ImportError:
            conn.close()

def fetch_data(query, params=None):
    return execute_query(query, params)

def update_data(query, params=None):
    return execute_update(query, params)

def insert_data(query, params=None):
    return execute_update(query, params)


def purge_transient_test_projects():
    """
    Remove only known transient MCP verification project records.

    This is intentionally narrow so legitimate user projects are never touched.
    It only targets the temporary project names and /tmp paths used by local
    verification harnesses.
    """
    name_conditions = " OR ".join("project_name LIKE ?" for _ in TRANSIENT_PROJECT_NAME_PREFIXES)
    path_conditions = " OR ".join("project_path LIKE ?" for _ in TRANSIENT_PROJECT_PATH_PREFIXES)
    where_clause = " OR ".join(filter(None, [name_conditions, path_conditions]))
    if not where_clause:
        return 0

    params = [f"{prefix}%" for prefix in TRANSIENT_PROJECT_NAME_PREFIXES]
    params.extend(f"{prefix}%" for prefix in TRANSIENT_PROJECT_PATH_PREFIXES)

    conn = get_db()
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT id FROM ProjectDetails WHERE {where_clause}", params)
            ids = [row[0] for row in cursor.fetchall()]
            if not ids:
                return 0

            placeholders = ",".join("?" for _ in ids)
            for table, column in (
                ("ProjectGitConfig", "project_details_id"),
                ("ProjectData", "project_details_id"),
                ("ProjectInputs", "projectId"),
                ("Backupfiles", "Project_ID"),
                ("Locators", "project_id"),
            ):
                cursor.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", ids)
            cursor.execute(f"DELETE FROM ProjectDetails WHERE id IN ({placeholders})", ids)
            return len(ids)
    except Exception as e:
        print(f"Error purging transient test projects: {e}")
        return 0
    finally:
        try:
            from flask import g, has_app_context
            if not (has_app_context() and hasattr(g, 'db') and g.db is conn):
                conn.close()
        except ImportError:
            conn.close()

def init_db():
    create_users_table = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        role TEXT NOT NULL,
        password TEXT NOT NULL,
        verified INTEGER DEFAULT 0
    );
    """
    
    create_session_table = """
    CREATE TABLE IF NOT EXISTS SessionDetails (
        userid INTEGER,
        SessionActive INTEGER,
        SessionTime TEXT,
        FOREIGN KEY(userid) REFERENCES users(id)
    );
    """
    
    create_project_table = """
    CREATE TABLE IF NOT EXISTS ProjectDetails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_name TEXT NOT NULL,
        project_path TEXT NOT NULL,
        project_lang TEXT NOT NULL,
        project_fw TEXT NOT NULL,
        project_tool TEXT NOT NULL,
        package_manager TEXT,
        project_type TEXT
    );
    """

    create_locators_table = """
                CREATE TABLE IF NOT EXISTS Locators (
                    ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    Page_Name VARCHAR(255),
                    Locator_Name VARCHAR(255),
                    Locator_Type VARCHAR(255),
                    Method VARCHAR(255),
                    Value VARCHAR(500),
                    Created_On DATETIME,
                    project_id INTEGER,
                    FOREIGN KEY(project_id) REFERENCES ProjectDetails(id)
                    UNIQUE(project_id, Page_Name, Locator_Name)
                )
            """

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

    create_project_templates_table = """
    CREATE TABLE IF NOT EXISTS ProjectTemplates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool TEXT NOT NULL,
        language TEXT NOT NULL,
        framework TEXT NOT NULL,
        description TEXT,
        default_run_commands TEXT
    );
    """

    create_template_files_table = """
    CREATE TABLE IF NOT EXISTS TemplateFiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        template_id INTEGER NOT NULL,
        file_path TEXT NOT NULL,
        file_content TEXT,
        is_binary BOOLEAN DEFAULT 0,
        FOREIGN KEY(template_id) REFERENCES ProjectTemplates(id)
    );
    """

    create_backupfiles_table = """
    CREATE TABLE IF NOT EXISTS Backupfiles (
        ID INTEGER PRIMARY KEY AUTOINCREMENT,
        Project_ID INTEGER,
        FileName VARCHAR(100),
        FileContent BLOB,
        FilePath TEXT,
        BackupID INTEGER,
        CreatedOn DATETIME,
        Type VARCHAR(100),
        FOREIGN KEY(Project_ID) REFERENCES ProjectDetails(id)
    );
    """

    create_project_inputs_table = """
    CREATE TABLE IF NOT EXISTS ProjectInputs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        projectId INTEGER,
        req_name TEXT NOT NULL,
        requirement TEXT NOT NULL,
        FOREIGN KEY(projectId) REFERENCES ProjectDetails(id)
    );
    """

    create_project_git_config_table = """
    CREATE TABLE IF NOT EXISTS ProjectGitConfig (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_url TEXT,
        username TEXT,
        access_token TEXT,
        project_details_id INTEGER,
        FOREIGN KEY(project_details_id) REFERENCES ProjectDetails(id)
    );
    """

    create_app_license_table = """
    CREATE TABLE IF NOT EXISTS AppLicense (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        license_key TEXT,
        status TEXT,
        message TEXT,
        licensed_to TEXT,
        last_checked_at TEXT,
        updated_at TEXT
    );
    """

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(create_users_table)
            cursor.execute(create_session_table)
            cursor.execute(create_project_table)
            cursor.execute(create_locators_table)
            cursor.execute(create_project_data_table)
            cursor.execute(create_project_templates_table)
            cursor.execute(create_template_files_table)
            cursor.execute(create_backupfiles_table)
            cursor.execute(create_project_inputs_table)
            cursor.execute(create_project_git_config_table)
            cursor.execute(create_app_license_table)
            
            # Indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_locators_project_id ON Locators(project_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_projectdata_project_details_id ON ProjectData(project_details_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_templatefiles_template_id ON TemplateFiles(template_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_backupfiles_project_id ON Backupfiles(Project_ID)")

            # Migrations
            try:
                cursor.execute("ALTER TABLE ProjectDetails ADD COLUMN package_manager TEXT")
            except Exception: pass
            
            try:
                cursor.execute("ALTER TABLE ProjectDetails ADD COLUMN project_type TEXT")
            except Exception: pass

            try:
                cursor.execute("ALTER TABLE ProjectDetails ADD COLUMN run_commands TEXT")
            except Exception: pass

            try:
                cursor.execute("ALTER TABLE ProjectTemplates ADD COLUMN default_run_commands TEXT")
            except Exception: pass

            try:
                cursor.execute("ALTER TABLE Locators ADD COLUMN project_id INTEGER")
            except Exception: pass

            try:
                cursor.execute("ALTER TABLE AppLicense ADD COLUMN licensed_to TEXT")
            except Exception: pass

            try:
                cursor.execute("ALTER TABLE AppLicense ADD COLUMN last_checked_at TEXT")
            except Exception: pass

            try:
                cursor.execute("ALTER TABLE AppLicense ADD COLUMN updated_at TEXT")
            except Exception: pass
            
            # Insert admin user if not exists
            cursor.execute("SELECT * FROM users WHERE email = 'admin@big.com'")
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO users (name, email, role, password, verified) VALUES (?, ?, ?, ?, ?)",
                    ('Admin', 'admin@big.com', 'admin', _seed_admin_password(), 1)
                )
            
            conn.commit()
            print("Database initialized successfully.")
    except Exception as e:
        print(f"Error initializing database: {e}")

if __name__ == '__main__':
    init_db()
