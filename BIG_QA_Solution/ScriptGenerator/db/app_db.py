import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "local_database.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def execute_query(query, params=None):
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params or [])
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        print(f"Error executing query: {e}")
        return []

def execute_update(query, params=None):
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params or [])
            conn.commit()
            return cursor.rowcount
    except Exception as e:
        print(f"Error executing update: {e}")
        return None

def fetch_data(query, params=None):
    return execute_query(query, params)

def update_data(query, params=None):
    return execute_update(query, params)

def insert_data(query, params=None):
    return execute_update(query, params)

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
                    UNIQUE(Page_Name, Locator_Name)
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

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(create_users_table)
            cursor.execute(create_session_table)
            cursor.execute(create_project_table)
            cursor.execute(create_locators_table)
            cursor.execute(create_project_data_table)

            # Migrations
            try:
                cursor.execute("ALTER TABLE ProjectDetails ADD COLUMN package_manager TEXT")
            except Exception: pass
            
            try:
                cursor.execute("ALTER TABLE ProjectDetails ADD COLUMN project_type TEXT")
            except Exception: pass

            try:
                cursor.execute("ALTER TABLE Locators ADD COLUMN project_id INTEGER")
            except Exception: pass
            
            # Insert admin user if not exists
            cursor.execute("SELECT * FROM users WHERE email = 'admin@big.com'")
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO users (name, email, role, password, verified) VALUES (?, ?, ?, ?, ?)",
                    ('Admin', 'admin@big.com', 'admin', 'admin123', 1)
                )
            
            conn.commit()
            print("Database initialized successfully.")
    except Exception as e:
        print(f"Error initializing database: {e}")

if __name__ == '__main__':
    init_db()
