# app.py
import os
import json
import sqlite3
import time
from datetime import datetime
from threading import Lock
from flask import Flask, request, jsonify, g, Response, session
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

# ---------- config ----------
BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(os.path.dirname(__file__), "accident_system.db")
# change these for production
APP_SECRET = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
HW_API_KEY = os.environ.get("HW_API_KEY", "REPLACE_WITH_STRONG_KEY")

# Frontend origins you use in dev (Live Server). Add both localhost and 127.0.0.1 if you alternate.
ALLOWED_ORIGINS = ["http://127.0.0.1:5500", "http://localhost:5500", "http://127.0.0.1:5500/"]


app = Flask(__name__)
app.secret_key = APP_SECRET
# cookie settings (dev)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'   # Good default for dev
app.config['SESSION_COOKIE_SECURE'] = False     # Set True in production with HTTPS

# Allow credentialed CORS (cookies)
CORS(app, supports_credentials=True, resources={r"/*": {"origins": ALLOWED_ORIGINS}})

# ---------- DB helpers ----------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH, check_same_thread=False)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv

# ---------- helper: resolve vehicle identifier (accept numeric id or vehicle_id text) ----------
def find_vehicle_by_identifier(ident):
    """
    Try to find a vehicle row by vehicle_id (text). If not found and ident is numeric,
    try lookup by numeric id column. Returns (row, used_field) where used_field is
    'vehicle_id' or 'id' or (None, None) if not found.
    """
    if ident is None:
        return None, None
    ident_str = str(ident).strip()
    if not ident_str:
        return None, None

    # First try vehicle_id (text)
    row = query_db("SELECT * FROM vehicles WHERE vehicle_id = ?", (ident_str,), one=True)
    if row:
        return row, "vehicle_id"

    # If ident is numeric, try id column
    try:
        iid = int(ident_str)
    except Exception:
        return None, None

    row = query_db("SELECT * FROM vehicles WHERE id = ?", (iid,), one=True)
    if row:
        return row, "id"

    return None, None

# ---------- init tables ----------
def init_db():
    db = sqlite3.connect(DB_PATH)
    cur = db.cursor()
    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fullname TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # vehicles summary
    cur.execute("""
    CREATE TABLE IF NOT EXISTS vehicles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_id TEXT UNIQUE NOT NULL,
        model TEXT,
        owner TEXT,
        registration TEXT,
        accident_details TEXT
        -- assigned_user_id INTEGER,  # <-- REMOVED
        -- FOREIGN KEY (assigned_user_id) REFERENCES users(id)  # <-- REMOVED
    )
    """)

    # accident events history
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
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_vehicle_time ON accident_events(vehicle_id, timestamp DESC)")
    db.commit()
    db.close()

# create tables on start
init_db()

# ---------- simple in-memory SSE pubsub ----------
_sse_subscribers = {}   # vehicle_id -> list of queues (lists)
_sse_lock = Lock()

def sse_subscribe(vehicle_id):
    q = []
    with _sse_lock:
        _sse_subscribers.setdefault(vehicle_id, []).append(q)
    return q

def sse_unsubscribe(vehicle_id, q):
    with _sse_lock:
        arr = _sse_subscribers.get(vehicle_id)
        if arr and q in arr:
            arr.remove(q)

def sse_publish(vehicle_id, payload):
    with _sse_lock:
        arr = _sse_subscribers.get(vehicle_id, [])
        for q in arr:
            q.append(payload)

# ---------- Auth: signup / login / session / logout ----------
@app.route("/signup", methods=["POST"])
def signup():
    data = request.get_json() or {}
    fullname = (data.get("fullname") or "").strip()
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if not fullname or not username or not email or not password:
        return jsonify(success=False, message="All fields are required"), 400

    pw_hash = generate_password_hash(password)
    db = get_db()
    try:
        db.execute("INSERT INTO users (fullname, username, email, password_hash) VALUES (?, ?, ?, ?)",
                   (fullname, username, email, pw_hash))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify(success=False, message="Username or email already exists"), 400
    return jsonify(success=True, message="Account created"), 201

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify(success=False, message="Username and password required"), 400

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    db.close()
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify(success=False, message="Invalid username or password"), 401

    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["fullname"] = user["fullname"]
    return jsonify(success=True, user={"id": user["id"], "username": user["username"], "fullname": user["fullname"]})

@app.route("/session", methods=["GET"])
def check_session():
    if 'user_id' in session:
        return jsonify(logged_in=True, user={'id': session['user_id'], 'username': session['username'], 'fullname': session['fullname']})
    return jsonify(logged_in=False)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify(success=True)

# ---------- Vehicle & validation endpoints ----------
@app.route("/add_vehicle", methods=["POST"])
def add_vehicle():
    data = request.get_json() or {}
    # accept either 'vehicle_id' (preferred) or legacy 'id'
    vid_raw = data.get("vehicle_id") if data.get("vehicle_id") is not None else data.get("id")
    vid = (str(vid_raw).strip() if vid_raw is not None else "")
    if not vid:
        return jsonify(success=False, message="vehicle id required"), 400
    model = data.get("model", "")
    owner = data.get("owner", "")
    registration = data.get("registration", "")
    # store into vehicle_id column (text) so both numeric and alphanumeric ids work
    db = get_db()
    db.execute("""INSERT INTO vehicles (vehicle_id, model, owner, registration, accident_details)
                  VALUES (?, ?, ?, ?, ?)
                  ON CONFLICT(vehicle_id) DO UPDATE SET
                    model=excluded.model, owner=excluded.owner, registration=excluded.registration""",
               (vid, model, owner, registration, None))
    db.commit()
    return jsonify(success=True, message="vehicle added/updated", vehicle_id=vid), 201

@app.route("/vehicle/<vid>", methods=["GET"])
def get_vehicle(vid):
    # Try to resolve vid by vehicle_id first, then by numeric id
    row, used = find_vehicle_by_identifier(vid)
    if not row:
        return jsonify(found=False, message="Vehicle not found"), 404
    vehicle = dict(row)
    return jsonify(found=True, vehicle=vehicle)

@app.route("/vehicle/<vid>/events", methods=["GET"])
def get_vehicle_events(vid):
    # Resolve vehicle to determine candidate keys for events lookup
    row, used = find_vehicle_by_identifier(vid)
    candidates = []
    if row:
        # prefer vehicle_id column value when present
        vtext = row.get("vehicle_id")
        if vtext:
            candidates.append(str(vtext))
        # also allow the numeric id (as string) if present
        iid = row.get("id")
        if iid is not None:
            candidates.append(str(iid))
    else:
        # no matching vehicle row; still attempt to query events by provided vid string
        candidates.append(str(vid))

    # build SQL with IN (...) and parameter placeholders
    placeholders = ",".join("?" for _ in candidates)
    sql = f"SELECT * FROM accident_events WHERE vehicle_id IN ({placeholders}) ORDER BY timestamp DESC LIMIT 100"
    rows = query_db(sql, tuple(candidates))
    events = [dict(r) for r in rows]
    return jsonify(events=events)

@app.route("/validateID", methods=["POST"])
def validate_id():
    """
    POST JSON: { "vehicleID": "<your vehicle_id>" }
    Returns vehicle summary and recent accident events looked up by vehicles.vehicle_id
    Expected response format for frontend:
    {
      "valid": true,
      "vehicle": {
        "id": 1,
        "vehicle_id": "VHL2023",
        "model": "Audi A3",
        "owner": "John Doe",
        "registration": "TN-09-AB-0009",
        "accidentDetails": "..."
      },
      "events": [
        {
          "id": 1,
          "vehicle_id": "VHL2023",
          "intensity": 4.5,
          "lat": 12.34,
          "lng": 56.78,
          "timestamp": "2025-12-09T14:30:00Z"
        }
      ]
    }
    """
    data = request.get_json(silent=True) or {}
    vid_raw = (data.get("vehicleID") or data.get("vehicle_id") or "")
    vid = str(vid_raw).strip()
    if not vid:
        return jsonify(valid=False, message="vehicleID required"), 400

    # Resolve vehicle row by vehicle_id or numeric id
    vehicle_row, used = find_vehicle_by_identifier(vid)
    if not vehicle_row:
        return jsonify(valid=False, message="vehicle not found"), 200
    
    vehicle = dict(vehicle_row)
    
    # Rename accident_details to accidentDetails for frontend compatibility
    vehicle_out = {
        "id": vehicle.get("id"),
        "vehicle_id": vehicle.get("vehicle_id"),
        "model": vehicle.get("model"),
        "owner": vehicle.get("owner"),
        "registration": vehicle.get("registration"),
        "accidentDetails": vehicle.get("accident_details") or "No recent accidents reported."
    }

    # Build candidate keys for events query (vehicle_id text and numeric id as string)
    candidates = []
    if vehicle.get("vehicle_id"):
        candidates.append(str(vehicle.get("vehicle_id")))
    if vehicle.get("id") is not None:
        candidates.append(str(vehicle.get("id")))
    # dedupe
    candidates = list(dict.fromkeys(candidates))
    
    if candidates:
        placeholders = ",".join("?" for _ in candidates)
        sql = f"SELECT id, vehicle_id, intensity, lat, lng, timestamp, created_at FROM accident_events WHERE vehicle_id IN ({placeholders}) ORDER BY created_at DESC LIMIT 50"
        events = query_db(sql, tuple(candidates))
        
        # Format events for frontend (rename created_at to timestamp if timestamp is missing)
        events_out = []
        for evt in events:
            evt_dict = dict(evt)
            # Use timestamp field, fallback to created_at
            if not evt_dict.get("timestamp") and evt_dict.get("created_at"):
                evt_dict["timestamp"] = evt_dict["created_at"]
            # Ensure all expected fields are present
            events_out.append({
                "id": evt_dict.get("id"),
                "vehicle_id": evt_dict.get("vehicle_id"),
                "intensity": evt_dict.get("intensity"),
                "lat": evt_dict.get("lat"),
                "lng": evt_dict.get("lng"),
                "timestamp": evt_dict.get("timestamp")
            })
    else:
        events_out = []

    return jsonify(valid=True, vehicle=vehicle_out, events=events_out), 200

# ---------- Hardware endpoint (protected by API key) ----------
@app.route("/hardware/event", methods=["POST"])
def hardware_event():
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if key != HW_API_KEY:
        return jsonify(success=False, message="unauthorized"), 401
    data = request.get_json() or {}
    vid_raw = data.get("vehicleID") if data.get("vehicleID") is not None else data.get("vehicle_id")
    if vid_raw is None:
        return jsonify(success=False, message="vehicleID required"), 400
    vid = str(vid_raw).strip()
    intensity = data.get("intensity")
    lat = data.get("lat")
    lng = data.get("lng")
    ts = data.get("timestamp") or datetime.utcnow().isoformat()
    notes = data.get("notes") or data.get("accidentDetails") or ""
    raw = json.dumps(data)
    db = get_db()
    cur = db.cursor()
    # transactional insert and update
    cur.execute("BEGIN")
    # always store event.vehicle_id as text string
    cur.execute("""INSERT INTO accident_events (vehicle_id,intensity,lat,lng,timestamp,raw_payload)
                   VALUES (?, ?, ?, ?, ?, ?)""", (vid, intensity, lat, lng, ts, raw))

    # Try update by vehicle_id (preferred)
    cur.execute("""UPDATE vehicles SET
                   accident_details=?, updated_at=CURRENT_TIMESTAMP
                   WHERE vehicle_id=?""", (notes, vid))
    if cur.rowcount == 0:
        # If not updated and vid is numeric, try updating by numeric id
        try:
            iid = int(vid)
        except Exception:
            iid = None
        if iid is not None:
            cur.execute("""UPDATE vehicles SET
                           accident_details=?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=?""", (notes, iid))
    # if update affected 0 rows, create minimal vehicle record (store vid into vehicle_id column)
    if cur.rowcount == 0:
        cur.execute("""INSERT INTO vehicles (vehicle_id, accident_details, created_at)
                       VALUES (?, ?, CURRENT_TIMESTAMP)""", (vid, notes))
    db.commit()
    # publish to SSE (use vid string)
    payload = {"type":"accident_event", "vehicleID": vid, "intensity": intensity, "lat": lat, "lng": lng, "timestamp": ts, "notes": notes}
    sse_publish(vid, json.dumps(payload))
    return jsonify(success=True), 201

# ---------- SSE for vehicle ----------
@app.route("/stream/vehicle/<vid>")
def stream_vehicle(vid):
    def event_stream(q):
        try:
            yield f"data: {json.dumps({'type':'connected','vehicleID':vid})}\n\n"
            idle_counter = 0
            while True:
                if q:
                    item = q.pop(0)
                    yield f"data: {item}\n\n"
                else:
                    time.sleep(1)
                    idle_counter += 1
                    if idle_counter >= 10:
                        idle_counter = 0
                        yield "data: {}\n\n"
                    continue
        finally:
            sse_unsubscribe(vid, q)
    q = sse_subscribe(vid)
    return Response(event_stream(q), mimetype="text/event-stream")

# ---------- root ----------
@app.route("/")
def index():
    return jsonify(message="Accident Detection Backend with sessions: up"), 200

@app.route("/vehicles", methods=["GET"])
def list_vehicles():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    vehicles = db.execute("SELECT * FROM vehicles").fetchall()
    db.close()
    return jsonify([dict(row) for row in vehicles])

if __name__ == "__main__":
    print("Starting Flask app (sessions enabled)...")
    app.run(host="0.0.0.0", port=5000, debug=True)


