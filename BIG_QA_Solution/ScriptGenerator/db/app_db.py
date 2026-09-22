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

def open_own_connection():
    """
    A connection the caller owns and must close, never the one cached on `g`.

    Required by anything that writes outside the lifetime of a request. The
    performance-run stream is the case that matters: Flask returns its
    streaming Response, `teardown_appcontext` closes the request's connection,
    and only then does the generator finish the run and persist its numbers.
    Reusing `g.db` there fails with "Cannot operate on a closed database", and
    the run's statistics are lost.
    """
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

# PerformanceRunStats column -> the header Locust writes for it in _stats.csv.
PERFORMANCE_STATS_COLUMNS = (
    ("request_type", "Type"),
    ("name", "Name"),
    ("request_count", "Request Count"),
    ("failure_count", "Failure Count"),
    ("median_response_time", "Median Response Time"),
    ("average_response_time", "Average Response Time"),
    ("min_response_time", "Min Response Time"),
    ("max_response_time", "Max Response Time"),
    ("average_content_size", "Average Content Size"),
    ("requests_per_sec", "Requests/s"),
    ("failures_per_sec", "Failures/s"),
    ("pct_50", "50%"),
    ("pct_66", "66%"),
    ("pct_75", "75%"),
    ("pct_80", "80%"),
    ("pct_90", "90%"),
    ("pct_95", "95%"),
    ("pct_98", "98%"),
    ("pct_99", "99%"),
    ("pct_99_9", "99.9%"),
    ("pct_99_99", "99.99%"),
    ("pct_100", "100%"),
)

# PerformanceRunHistory column -> the header Locust writes for it in
# _stats_history.csv. One row per sampling interval of a run, which is what
# makes a run's timeline (response times, throughput and failures over its
# duration) plottable - `_stats.csv` only carries the final totals.
PERFORMANCE_HISTORY_COLUMNS = (
    ("timestamp", "Timestamp"),
    ("user_count", "User Count"),
    ("request_type", "Type"),
    ("name", "Name"),
    ("requests_per_sec", "Requests/s"),
    ("failures_per_sec", "Failures/s"),
    ("pct_50", "50%"),
    ("pct_66", "66%"),
    ("pct_75", "75%"),
    ("pct_80", "80%"),
    ("pct_90", "90%"),
    ("pct_95", "95%"),
    ("pct_98", "98%"),
    ("pct_99", "99%"),
    ("pct_99_9", "99.9%"),
    ("pct_99_99", "99.99%"),
    ("pct_100", "100%"),
    ("total_request_count", "Total Request Count"),
    ("total_failure_count", "Total Failure Count"),
    ("total_median_response_time", "Total Median Response Time"),
    ("total_average_response_time", "Total Average Response Time"),
    ("total_min_response_time", "Total Min Response Time"),
    ("total_max_response_time", "Total Max Response Time"),
    ("total_average_content_size", "Total Average Content Size"),
)

_PERFORMANCE_STATS_TEXT_COLUMNS = {"request_type", "name"}


def _stats_value(column, raw):
    """Coerce one CSV cell for its column; blanks and 'N/A' become NULL."""
    text = (raw or "").strip()
    if column in _PERFORMANCE_STATS_TEXT_COLUMNS:
        return text
    if not text or text.upper() in ("N/A", "NAN"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _normalized_header(header):
    """
    Fold a CSV header to a comparable form.

    Locust has shipped the history percentile columns as both "95%" and
    "95% percentile", so a run's numbers must not hinge on which spelling the
    installed version writes.
    """
    text = (header or "").strip().strip('"').lower()
    if text.endswith(" percentile"):
        text = text[: -len(" percentile")].strip()
    return text


def _row_cell(row, header):
    """Read `header` from a CSV row, tolerating header spelling differences."""
    if header in row:
        return row[header]
    wanted = _normalized_header(header)
    for key, value in row.items():
        if _normalized_header(key) == wanted:
            return value
    return None


def next_performance_run_id(cursor, project_id, script_file):
    """
    The next run number for a script of a performance project.

    Runs are counted per (project, script) because that is what one run covers,
    so a script's runs read 1, 2, 3... and line up for comparison. Called with
    the caller's cursor so the read and the insert share one transaction and two
    runs finishing together cannot be handed the same number.
    """
    cursor.execute(
        "SELECT COALESCE(MAX(run_id), 0) + 1 FROM PerformanceRunStats "
        "WHERE project_id = ? AND script_file IS ?",
        (project_id, script_file),
    )
    return cursor.fetchone()[0]


def insert_performance_run_stats(project_id, run_context, rows):
    """
    Persist the rows of one run's `_stats.csv` (per-request plus "Aggregated").

    `run_context` supplies the run configuration the numbers belong to:
    script_file, run_at, concurrent_users, spawn_rate and duration. Every row of
    the run is stamped with the same freshly allocated `run_id`.

    Returns that run_id, or 0 when there is nothing to write or the write fails
    - a reporting write must never take down the run that produced it.
    """
    if not project_id or not rows:
        return 0

    lead = ("project_id", "run_id", "script_file", "run_at",
            "concurrent_users", "spawn_rate", "duration")
    columns = lead + tuple(column for column, _ in PERFORMANCE_STATS_COLUMNS)
    statement = (
        f"INSERT INTO PerformanceRunStats ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )

    script_file = run_context.get("script_file")
    conn = open_own_connection()
    try:
        with conn:
            # A plain SELECT takes no write lock, so without BEGIN IMMEDIATE two
            # runs finishing together could both read the same MAX(run_id).
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            run_id = next_performance_run_id(cursor, project_id, script_file)
            params = []
            for row in rows:
                values = [
                    project_id,
                    run_id,
                    script_file,
                    run_context.get("run_at"),
                    run_context.get("concurrent_users"),
                    run_context.get("spawn_rate"),
                    run_context.get("duration"),
                ]
                values.extend(_stats_value(column, _row_cell(row, header))
                              for column, header in PERFORMANCE_STATS_COLUMNS)
                params.append(values)
            cursor.executemany(statement, params)
        return run_id
    except Exception as e:
        print(f"Error saving performance run stats: {e}")
        return 0
    finally:
        conn.close()


def insert_performance_run_history(project_id, run_id, run_context, rows):
    """
    Persist the rows of one run's `_stats_history.csv` - one sample per interval.

    `run_id` is the number `insert_performance_run_stats` allocated for the same
    run, so a run's totals and its timeline are read back together.

    Returns the number of samples written, or 0 when there is nothing to write
    or the write fails - like the totals, a reporting write must never take down
    the run that produced it.
    """
    if not project_id or not run_id or not rows:
        return 0

    lead = ("project_id", "run_id", "script_file", "run_at")
    columns = lead + tuple(column for column, _ in PERFORMANCE_HISTORY_COLUMNS)
    statement = (
        f"INSERT INTO PerformanceRunHistory ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )

    params = []
    for row in rows:
        values = [
            project_id,
            run_id,
            run_context.get("script_file"),
            run_context.get("run_at"),
        ]
        values.extend(_stats_value(column, _row_cell(row, header))
                      for column, header in PERFORMANCE_HISTORY_COLUMNS)
        params.append(values)

    conn = open_own_connection()
    try:
        with conn:
            conn.cursor().executemany(statement, params)
        return len(params)
    except Exception as e:
        print(f"Error saving performance run history: {e}")
        return 0
    finally:
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

    create_performance_details_table = """
    CREATE TABLE IF NOT EXISTS PerformanceDetails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_name TEXT NOT NULL UNIQUE,
        application_url TEXT NOT NULL,
        project_path TEXT,
        concurrent_user_count INTEGER,
        spawn_rate INTEGER,
        run_duration INTEGER
    );
    """

    # One payload configuration per (performance project, test script): the
    # payload file that drives the script, the script parameter -> payload node
    # mapping, and the per-request response-time thresholds saved from the
    # Payload Configuration dialog.
    create_performance_payload_table = """
    CREATE TABLE IF NOT EXISTS PerformancePayloadConfig (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        perf_id INTEGER NOT NULL,
        script_file TEXT NOT NULL,
        payload_type TEXT NOT NULL,
        payload_file TEXT,
        payload_name TEXT,
        source_file_name TEXT,
        record_tag TEXT,
        row_count INTEGER,
        mappings TEXT,
        thresholds TEXT,
        generated_script TEXT,
        updated_at TEXT,
        UNIQUE(perf_id, script_file),
        FOREIGN KEY(perf_id) REFERENCES PerformanceDetails(id)
    );
    """

    # One row per line of Locust's `<prefix>_stats.csv`, captured at the end of
    # every performance run: the per-request rows plus the "Aggregated" row.
    # The leading columns record the run configuration the numbers belong to,
    # since the CSV itself only carries the measurements.
    #
    # `run_id` is a sequence number shared by every row of one run and counted
    # per (project, script) - the unit a run actually covers - so run 1, 2, 3 of
    # a script line up for comparison. `id` cannot serve this: it numbers rows,
    # not runs.
    #
    # Column names are the CSV headers in snake_case ("Median Response Time" ->
    # median_response_time, "Requests/s" -> requests_per_sec, "99.9%" ->
    # pct_99_9), because spaces, slashes and % need quoting in every statement.
    create_performance_run_stats_table = """
    CREATE TABLE IF NOT EXISTS PerformanceRunStats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        run_id INTEGER,
        script_file TEXT,
        run_at TEXT,
        concurrent_users INTEGER,
        spawn_rate INTEGER,
        duration TEXT,
        request_type TEXT,
        name TEXT,
        request_count INTEGER,
        failure_count INTEGER,
        median_response_time REAL,
        average_response_time REAL,
        min_response_time REAL,
        max_response_time REAL,
        average_content_size REAL,
        requests_per_sec REAL,
        failures_per_sec REAL,
        pct_50 REAL,
        pct_66 REAL,
        pct_75 REAL,
        pct_80 REAL,
        pct_90 REAL,
        pct_95 REAL,
        pct_98 REAL,
        pct_99 REAL,
        pct_99_9 REAL,
        pct_99_99 REAL,
        pct_100 REAL,
        FOREIGN KEY(project_id) REFERENCES PerformanceDetails(id)
    );
    """

    # One row per sampling interval of Locust's `<prefix>_stats_history.csv`,
    # captured at the end of every performance run and keyed to the same
    # `run_id` as that run's totals in PerformanceRunStats.
    #
    # This is the run's timeline: `_stats.csv` says what a run ended up at,
    # while these samples say how it got there - which is what shows a warm-up
    # period, a percentile spike, or the moment failures started. Locust writes
    # only the "Aggregated" line per interval unless --csv-full-history is
    # given, so a sample covers the whole run, not one endpoint.
    create_performance_run_history_table = """
    CREATE TABLE IF NOT EXISTS PerformanceRunHistory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        run_id INTEGER NOT NULL,
        script_file TEXT,
        run_at TEXT,
        timestamp REAL,
        user_count INTEGER,
        request_type TEXT,
        name TEXT,
        requests_per_sec REAL,
        failures_per_sec REAL,
        pct_50 REAL,
        pct_66 REAL,
        pct_75 REAL,
        pct_80 REAL,
        pct_90 REAL,
        pct_95 REAL,
        pct_98 REAL,
        pct_99 REAL,
        pct_99_9 REAL,
        pct_99_99 REAL,
        pct_100 REAL,
        total_request_count INTEGER,
        total_failure_count INTEGER,
        total_median_response_time REAL,
        total_average_response_time REAL,
        total_min_response_time REAL,
        total_max_response_time REAL,
        total_average_content_size REAL,
        FOREIGN KEY(project_id) REFERENCES PerformanceDetails(id)
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
            cursor.execute(create_performance_details_table)
            cursor.execute(create_performance_payload_table)
            cursor.execute(create_performance_run_stats_table)
            cursor.execute(create_performance_run_history_table)
            cursor.execute(create_project_git_config_table)
            
            # Indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_locators_project_id ON Locators(project_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_projectdata_project_details_id ON ProjectData(project_details_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_templatefiles_template_id ON TemplateFiles(template_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_backupfiles_project_id ON Backupfiles(Project_ID)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_perfrunstats_project_id ON PerformanceRunStats(project_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_perfrunhistory_run "
                           "ON PerformanceRunHistory(project_id, script_file, run_id)")

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
                cursor.execute("ALTER TABLE PerformancePayloadConfig ADD COLUMN thresholds TEXT")
            except Exception: pass

            try:
                cursor.execute("ALTER TABLE PerformanceRunStats ADD COLUMN run_id INTEGER")
            except Exception: pass

            # Indexed after the migration above, so a table created before
            # run_id existed still gets the index once the column is added.
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_perfrunstats_run "
                           "ON PerformanceRunStats(project_id, script_file, run_id)")

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
