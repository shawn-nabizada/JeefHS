import os
import requests
from flask import Flask, render_template, request, flash, redirect, url_for
from sqlalchemy import create_engine, text
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables (local development)
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24))

# --- Configuration ---
ADAFRUIT_IO_USERNAME = os.getenv("ADAFRUIT_IO_USERNAME")
ADAFRUIT_IO_KEY = os.getenv("ADAFRUIT_IO_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Fix Postgres URL for SQLAlchemy (Render/Neon use postgres://, SQLAlchemy needs postgresql://)
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Database Engine
engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    except Exception as e:
        print(f"DB Config Error: {e}")

# --- Helper Functions ---
def aio_get(feed_key):
    """Fetch the last known value from an Adafruit IO feed."""
    if not ADAFRUIT_IO_USERNAME or not ADAFRUIT_IO_KEY:
        return None
    
    url = f"https://io.adafruit.com/api/v2/{ADAFRUIT_IO_USERNAME}/feeds/{feed_key}/data/last"
    headers = {"X-AIO-Key": ADAFRUIT_IO_KEY}
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"AIO Get Error ({feed_key}): {e}")
    return None

def aio_send(feed_key, value):
    """Publish a value to an Adafruit IO feed."""
    if not ADAFRUIT_IO_USERNAME or not ADAFRUIT_IO_KEY:
        return False

    url = f"https://io.adafruit.com/api/v2/{ADAFRUIT_IO_USERNAME}/feeds/{feed_key}/data"
    headers = {"X-AIO-Key": ADAFRUIT_IO_KEY, "Content-Type": "application/json"}
    payload = {"value": value}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=5)
        return r.status_code in [200, 201]
    except Exception as e:
        print(f"AIO Send Error ({feed_key}): {e}")
        return False

# --- Routes ---

@app.route('/')
def dashboard():
    """Home Page: Live status of 3 sensors."""
    # Feeds: temperature, humidity, mode_status
    temp_data = aio_get("temperature")
    hum_data = aio_get("humidity")
    mode_data = aio_get("mode_status")

    context = {
        "temp": round(float(temp_data['value']), 1) if temp_data else "--",
        "humid": round(float(hum_data['value']), 1) if hum_data else "--",
        "mode": mode_data['value'] if mode_data else "UNKNOWN",
        "last_update": temp_data['created_at'] if temp_data else datetime.now().isoformat()
    }
    return render_template("dashboard.html", **context)

@app.route('/environment', methods=['GET', 'POST'])
def environment():
    """Historical Data: Date selection + SQL Query + Chart.js."""
    selected_date = request.form.get("date", datetime.now().strftime("%Y-%m-%d"))
    data_points = []
    
    if engine:
        try:
            with engine.connect() as conn:
                # Query matches the schema in database_interface.py
                query = text("""
                    SELECT timestamp, temperature, humidity 
                    FROM measurements 
                    WHERE timestamp::date = :date 
                    ORDER BY timestamp ASC
                """)
                result = conn.execute(query, {"date": selected_date})
                # Convert to list of dicts for JSON serialization
                data_points = [
                    {"t": row.timestamp.isoformat(), "temp": row.temperature, "hum": row.humidity}
                    for row in result
                ]
        except Exception as e:
            flash(f"Database Error: {e}", "danger")

    return render_template("environment.html", data=data_points, selected_date=selected_date)

@app.route('/security')
def security():
    """Security Page: List intrusions from DB."""
    events = []
    if engine:
        try:
            with engine.connect() as conn:
                query = text("""
                    SELECT timestamp, event_type, mode, image_path 
                    FROM security_events 
                    ORDER BY timestamp DESC 
                    LIMIT 50
                """)
                events = conn.execute(query).fetchall()
        except Exception as e:
            flash(f"Database Error: {e}", "danger")

    return render_template("security.html", events=events)

@app.route('/security/control', methods=['POST'])
def security_control():
    """Handle Security Mode Toggles."""
    action = request.form.get("action")
    if action in ["HOME", "AWAY", "NIGHT"]:
        if aio_send("mode_select", action):
            flash(f"Mode changed to {action}", "success")
        else:
            flash("Failed to send command to Adafruit IO", "danger")
    return redirect(url_for('security'))

@app.route('/devices', methods=['GET', 'POST'])
def devices():
    """Device Control Page: Toggle 3 devices."""
    if request.method == 'POST':
        device = request.form.get("device")
        state = request.form.get("state")
        
        # Mapping form names to Adafruit IO Feed Keys
        # Ensure these match config.json "CONTROL_FEEDS"
        feed_map = {
            "fan": "fan_control",
            "buzzer": "buzzer_control",
            "party": "party_mode_control"
        }
        
        if device in feed_map:
            if aio_send(feed_map[device], state):
                flash(f"{device.title()} turned {state}", "success")
            else:
                flash("Failed to communicate with device", "danger")
    
    return render_template("devices.html")

@app.route('/about')
def about():
    return render_template("about.html")

if __name__ == '__main__':
    # Local dev run
    app.run(debug=True, port=5000)