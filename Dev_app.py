# Dev_app.py
"""
Flask 3.x app with:
- vehicles (latest summary)
- accident_events (append-only full history)
- verbose logging for local debugging
"""
from flask import Flask, request, jsonify, send_from_directory
import sqlite3, os, logging, traceback, datetime
from flask_cors import CORS

# --- App setup ---
app = Flask(__name__, static_folder="static")
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)  # dev only
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")

DB_PATH = os.path.join(os.path.dirname(__file__), "accident_system.db")

# --- DB helpers ---
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """
    Ensure required tables and columns exist. Idempotent migration:
    - create vehicles and accident_events tables if missing
    - add vehicles.created_at if missing
    - add accident_events.details and accident_events.meta_json if missing
    """
    db = get_db()
    try:
        # 1) Ensure base tables exist (without optional columns)
        db.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id TEXT UNIQUE NOT NULL,
            model TEXT,
            owner TEXT,
            registration TEXT,
            accident_details TEXT
            -- created_at may be added below if missing
        );
        """)
        db.execute("""
        CREATE TABLE IF NOT EXISTS accident_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id TEXT NOT NULL,
            event_time DATETIME DEFAULT CURRENT_TIMESTAMP
            -- details and meta_json may be added below if missing
            -- FOREIGN KEY(vehicle_id) REFERENCES vehicles(vehicle_id)
        );
        """)
        db.commit()

        # 2) Check and add missing columns in 'vehicles'
        cur = db.execute("PRAGMA table_info(vehicles);")
        vehicle_cols = [row["name"] for row in cur.fetchall()]
        if "created_at" not in vehicle_cols:
            db.execute("ALTER TABLE vehicles ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP;")
            app.logger.info("Migration: added 'created_at' column to vehicles")

        # 3) Check and add missing columns in 'accident_events'
        cur = db.execute("PRAGMA table_info(accident_events);")
        event_cols = [row["name"] for row in cur.fetchall()]
        if "details" not in event_cols:
            db.execute("ALTER TABLE accident_events ADD COLUMN details TEXT;")
            app.logger.info("Migration: added 'details' column to accident_events")
        if "meta_json" not in event_cols:
            db.execute("ALTER TABLE accident_events ADD COLUMN meta_json TEXT;")
            app.logger.info("Migration: added 'meta_json' column to accident_events")

        db.commit()
        app.logger.info("DB initialization/migration completed at %s", DB_PATH)
    finally:
        db.close()

# initialize DB now (Flask 3.x compatible)
with app.app_context():
    init_db()

# --- Request logging for debug ---
@app.before_request
def log_request():
    try:
        preview = None
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                preview = request.get_json(silent=True)
            except Exception:
                preview = "<non-json or unreadable>"
        app.logger.debug("Incoming request: %s %s from %s; json=%s; args=%s",
                         request.method, request.path, request.remote_addr, preview, dict(request.args))
    except Exception:
        app.logger.exception("Error while logging request")

# --- Routes ---
@app.route("/add_vehicle", methods=["POST", "OPTIONS"])
def add_vehicle():
    # handle preflight
    if request.method == "OPTIONS":
        return jsonify(success=True, message="ok (preflight)"), 200

    try:
        raw = request.get_data()
        app.logger.debug("RAW REQUEST BYTES: %s", raw)
        try:
            app.logger.debug("RAW REQUEST TEXT: %s", raw.decode('utf-8'))
        except Exception as ex:
            app.logger.debug("Could not decode raw body: %s", ex)

        if not request.is_json:
            app.logger.warning("Request is not JSON. Headers: %s", dict(request.headers))
            return jsonify(success=False, message="Expected application/json"), 400

        data = request.get_json()
        app.logger.debug("Payload parsed JSON: %s", data)

        vehicle_id = (data.get("vehicle_id") or "").strip()
        model = (data.get("model") or "").strip()
        owner = (data.get("owner") or "").strip()
        registration = (data.get("registration") or "").strip()
        accident_details = (data.get("accident_details") or "").strip()
        # optional extra metadata (could be GPS, severity, etc)
        meta = data.get("meta")  # may be dict or None

        if not vehicle_id:
            return jsonify(success=False, message="vehicle_id is required"), 400

        db = get_db()
        action = None
        try:
            # Start a transaction (sqlite autocommit disabled while executing multiple statements)
            # 1) Ensure vehicles has a row for this vehicle_id (insert only if missing)
            db.execute(
                "INSERT OR IGNORE INTO vehicles (vehicle_id, model, owner, registration, accident_details) VALUES (?, ?, ?, ?, ?)",
                (vehicle_id, model, owner, registration, accident_details)
            )

            # 2) Detect whether row existed before by selecting it
            cur = db.execute("SELECT id, model, owner, registration, accident_details, created_at FROM vehicles WHERE vehicle_id = ?", (vehicle_id,))
            existing = cur.fetchone()
            if existing is None:
                # unlikely, but treat as created
                action = "created"
            else:
                # If the posted details differ from existing, update the summary (so vehicles is always latest)
                # We treat identical data as "updated" for simplicity — adjust per your business logic.
                db.execute(
                    "UPDATE vehicles SET model=?, owner=?, registration=?, accident_details=?, created_at=CURRENT_TIMESTAMP WHERE vehicle_id=?",
                    (model, owner, registration, accident_details, vehicle_id)
                )
                # If the row was just inserted by INSERT OR IGNORE above, action should be 'created'
                # Determine with a quick check: if created_at equals previous value -> updated, else created
                action = "updated" if existing else "created"

            # 3) Append an event to accident_events (always)
            meta_json = None
            if meta is not None:
                # store meta as JSON string; keep small to avoid huge blobs
                import json
                try:
                    meta_json = json.dumps(meta)
                except Exception:
                    meta_json = None

            db.execute(
                "INSERT INTO accident_events (vehicle_id, details, meta_json) VALUES (?, ?, ?)",
                (vehicle_id, accident_details, meta_json)
            )

            db.commit()
            app.logger.info("add_vehicle: action=%s vehicle_id=%s", action, vehicle_id)
        except Exception as e:
            db.rollback()
            app.logger.exception("DB error during add_vehicle")
            raise
        finally:
            db.close()

        # Choose status code: 201 for created, 200 for updated
        status_code = 201 if action == "created" else 200
        return jsonify(success=True, message=f"Vehicle {action}", action=action, vehicle_id=vehicle_id), status_code

    except Exception as e:
        tb = traceback.format_exc()
        app.logger.error("Unhandled exception in add_vehicle: %s\n%s", e, tb)
        return jsonify(success=False, message="Server error", error=str(e), traceback=tb), 500


@app.route("/vehicles", methods=["GET"])
def list_vehicles():
    try:
        db = get_db()
        rows = db.execute("SELECT id, vehicle_id, model, owner, registration, accident_details, created_at FROM vehicles ORDER BY created_at DESC").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()

@app.route("/events", methods=["GET"])
def list_events():
    # optional query param ?vehicle_id=...
    vehicle_id = request.args.get("vehicle_id")
    db = get_db()
    try:
        if vehicle_id:
            rows = db.execute("SELECT id, vehicle_id, event_time, details, meta_json FROM accident_events WHERE vehicle_id=? ORDER BY event_time DESC", (vehicle_id,)).fetchall()
        else:
            rows = db.execute("SELECT id, vehicle_id, event_time, details, meta_json FROM accident_events ORDER BY event_time DESC LIMIT 500").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()

@app.route("/health")
def health():
    return jsonify(status="ok", time=str(datetime.datetime.now()))

# Serve frontend file if placed in ./static/Dev_app.html
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "Dev_app.html")

if __name__ == "__main__":
    # Local dev only
    app.run(host="127.0.0.1", port=5001, debug=True)
