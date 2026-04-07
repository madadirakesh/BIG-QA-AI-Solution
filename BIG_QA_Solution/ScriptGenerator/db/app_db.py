import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_database.db")

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
