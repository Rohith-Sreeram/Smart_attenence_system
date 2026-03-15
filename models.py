import os
import sqlite3
import json

# strictly use SQLite for local testing
DB_PATH = "attendance_v3.db"

print(f"--- Database Setup ---")
print(f"Mode: Local/Development (SQLite Only)")
print(f"SQLite Path: {os.path.abspath(DB_PATH)}")
print(f"----------------------")

class DBWrapper:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        self._conn.row_factory = sqlite3.Row
        return DBCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

class DBCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = None

    def execute(self, query, params=None):
        if params:
            self._cursor.execute(query, params)
        else:
            self._cursor.execute(query)
        self.lastrowid = self._cursor.lastrowid

    def fetchone(self):
        row = self._cursor.fetchone()
        if row:
            return dict(row)
        return row

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [dict(r) for r in rows]

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    return DBWrapper(conn)

def ensure_column(cursor, table, column, col_type):
    cursor._cursor.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor._cursor.fetchall()]
    if column not in columns:
        print(f"Adding column {column} to {table}")
        cursor._cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Define SQLite types
    pk_type = "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts_type = "DATETIME"

    # Students Table
    cursor._cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS students (
            id {pk_type},
            name TEXT,
            reg_no TEXT UNIQUE,
            email TEXT UNIQUE,
            section TEXT,
            department TEXT,
            year TEXT,
            face_embedding TEXT,
            rfid_id TEXT UNIQUE,
            password_hash TEXT,
            photo_path TEXT
        )
    ''')

    # Faculty Table
    cursor._cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS faculty (
            id {pk_type},
            name TEXT,
            email TEXT UNIQUE,
            rfid_id TEXT UNIQUE,
            password_hash TEXT
        )
    ''')

    # Departments Table
    cursor._cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS departments (
            id {pk_type},
            name TEXT UNIQUE
        )
    ''')

    # Sections Table
    cursor._cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS sections (
            id {pk_type},
            name TEXT,
            year TEXT,
            academic_duration TEXT,
            department_id INTEGER REFERENCES departments(id) ON DELETE CASCADE,
            faculty_id INTEGER REFERENCES faculty(id) ON DELETE CASCADE,
            duration_min INTEGER DEFAULT 60,
            section TEXT,
            UNIQUE(name, year, department_id, faculty_id)
        )
    ''')

    # Enrollments Table
    cursor._cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS enrollments (
            id {pk_type},
            student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
            section_id INTEGER REFERENCES sections(id) ON DELETE CASCADE,
            UNIQUE(student_id, section_id)
        )
    ''')

    # Attendance Records Table
    cursor._cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS attendance (
            id {pk_type},
            student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
            section_id INTEGER REFERENCES sections(id) ON DELETE CASCADE,
            timestamp {ts_type},
            face_verified BOOLEAN,
            rfid_verified BOOLEAN,
            status TEXT,
            submitted BOOLEAN DEFAULT FALSE,
            is_manual BOOLEAN DEFAULT FALSE,
            subject_name TEXT,
            session_time TEXT
        )
    ''')

    # Timetable Table
    cursor._cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS timetable (
            id {pk_type},
            faculty_id INTEGER REFERENCES faculty(id) ON DELETE CASCADE,
            section_id INTEGER REFERENCES sections(id) ON DELETE CASCADE,
            day_of_week TEXT,
            start_time TEXT,
            end_time TEXT
        )
    ''')

    # Migration: Ensure duration_min and section exist in sections
    ensure_column(cursor, "sections", "duration_min", "INTEGER DEFAULT 60")
    ensure_column(cursor, "sections", "section", "TEXT")

    # Assign existing sections to default faculty
    try:
        cursor._cursor.execute("UPDATE sections SET faculty_id = 1 WHERE faculty_id IS NULL")
    except:
        pass

    conn.commit()
    conn.close()
