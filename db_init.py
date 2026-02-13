# db_init.py
import sqlite3
import os
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), "accident_system.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Create users table if not exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fullname TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create vehicles table if not exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id TEXT UNIQUE NOT NULL,
            model TEXT,
            owner TEXT,
            registration TEXT,
            accident_details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create accident_events table if not exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accident_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id TEXT,
            intensity REAL,
            lat REAL,
            lng REAL,
            timestamp TEXT,
            raw_payload TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Add demo data only if tables are empty
    if not cur.execute("SELECT 1 FROM vehicles LIMIT 1").fetchone():
        # Insert a demo vehicle
        cur.execute("""
            INSERT INTO vehicles (vehicle_id, model, owner, registration, accident_details)
            VALUES (?, ?, ?, ?, ?)
        """, ("VHL2023", "Audi A3", "Aravindhan", "TN-09-AB-0009", "No recent accidents reported."))

    if not cur.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        # Insert demo user (password: Demo@1234)
        demo_pw_hash = generate_password_hash("Demo@1234")
        cur.execute("""
            INSERT INTO users (fullname, username, email, password_hash)
            VALUES (?, ?, ?, ?)
        """, ("Demo User", "DemoUser1!", "demo@example.com", demo_pw_hash))

    conn.commit()
    conn.close()
    print("Database initialized successfully:", DB_PATH)

if __name__ == "__main__":
    init_db()
