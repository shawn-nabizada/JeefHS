"""
Database Interface for JeefHS.
Handles local SQLite storage and background synchronization to Neon (Postgres).
"""

import sqlite3
import threading
import time
import os
import logging
from typing import Dict
from dotenv import load_dotenv

# Try importing psycopg2; handle gracefully if missing (dev mode)
try:
    import psycopg2
    from psycopg2 import sql
except ImportError:
    psycopg2 = None

logger = logging.getLogger(__name__)

# Load environment variables (for DATABASE_URL)
load_dotenv()

# --- Local SQLite Schema ---
CREATE_ENV_TABLE = """
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    temperature REAL,
    humidity REAL,
    synced INTEGER DEFAULT 0
);
"""

CREATE_SEC_TABLE = """
CREATE TABLE IF NOT EXISTS security_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT,
    image_path TEXT,
    mode TEXT,
    synced INTEGER DEFAULT 0
);
"""

# --- Cloud (Neon) Schema (Reference) ---
# Run these SQL commands in your Neon SQL Editor once to set up the cloud DB.
"""
CREATE TABLE IF NOT EXISTS measurements (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP,
    temperature FLOAT,
    humidity FLOAT,
    device_id TEXT
);

CREATE TABLE IF NOT EXISTS security_events (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP,
    event_type TEXT,
    image_path TEXT,
    mode TEXT,
    device_id TEXT
);
"""


class DatabaseInterface:
    def __init__(self, config: Dict):
        self.config = config
        self.local_db = "jeefhs_local.db"
        # The DATABASE_URL comes from the .env file
        self.pg_conn_str = os.getenv("DATABASE_URL")
        self.device_id = "pi_01"  # Identifier for this device in the cloud

        self._init_local_db()

        if not self.pg_conn_str:
            logger.warning("DATABASE_URL not found in .env. Cloud sync will be disabled.")
        elif psycopg2 is None:
            logger.warning("psycopg2 library not found. Cloud sync will be disabled.")

        # Start background sync thread
        self.running = True
        self.sync_thread = threading.Thread(target=self._sync_loop, name="DBSyncWorker", daemon=True)
        self.sync_thread.start()

    def _init_local_db(self):
        """Initialize SQLite tables."""
        try:
            with sqlite3.connect(self.local_db) as conn:
                cursor = conn.cursor()
                cursor.execute(CREATE_ENV_TABLE)
                cursor.execute(CREATE_SEC_TABLE)
                conn.commit()
            logger.info(f"Local database '{self.local_db}' initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize local DB: {e}")

    def log_environment(self, data: Dict):
        """Log environmental data to local DB."""
        try:
            # Safely handle missing keys
            ts = data.get('timestamp')
            temp = data.get('temperature')
            hum = data.get('humidity')

            with sqlite3.connect(self.local_db) as conn:
                conn.execute(
                    "INSERT INTO measurements (timestamp, temperature, humidity, synced) VALUES (?, ?, ?, 0)",
                    (ts, temp, hum)
                )
            logger.debug("Logged environmental data locally.")
        except Exception as e:
            logger.error(f"Failed to log env data locally: {e}")

    def log_security(self, data: Dict, event_type: str = "motion"):
        """Log security event to local DB."""
        try:
            ts = data.get('timestamp')
            img = data.get('image_path')
            mode = data.get('mode')

            with sqlite3.connect(self.local_db) as conn:
                conn.execute(
                    "INSERT INTO security_events (timestamp, event_type, image_path, mode, synced) VALUES (?, ?, ?, ?, 0)",
                    (ts, event_type, img, mode)
                )
            logger.debug("Logged security data locally.")
        except Exception as e:
            logger.error(f"Failed to log security data locally: {e}")

    def _sync_loop(self):
        """Background loop to push unsynced records to Neon."""
        logger.info("Database sync thread started.")
        while self.running:
            # Check requirements
            if not self.pg_conn_str or psycopg2 is None:
                time.sleep(60)
                continue

            try:
                # We sync in batches to avoid locking the DB for too long
                self._sync_measurements()
                self._sync_security()
            except Exception as e:
                logger.error(f"Sync cycle error: {e}")

            # Sleep 10 seconds before next sync attempt
            time.sleep(10)

    def _sync_measurements(self):
        with sqlite3.connect(self.local_db) as local_conn:
            local_cur = local_conn.cursor()
            # 1. Fetch unsynced rows (Batch of 50)
            local_cur.execute("SELECT id, timestamp, temperature, humidity FROM measurements WHERE synced=0 LIMIT 50")
            rows = local_cur.fetchall()

            if not rows:
                return

            try:
                # 2. Push to Neon
                with psycopg2.connect(self.pg_conn_str) as pg_conn:
                    with pg_conn.cursor() as pg_cur:
                        ids_to_mark = []
                        for row in rows:
                            row_id, ts, temp, hum = row
                            pg_cur.execute(
                                "INSERT INTO measurements (timestamp, temperature, humidity, device_id) VALUES (%s, %s, %s, %s)",
                                (ts, temp, hum, self.device_id)
                            )
                            ids_to_mark.append(row_id)

                        pg_conn.commit()

                        # 3. Mark as synced locally ONLY if Cloud push succeeded
                        if ids_to_mark:
                            placeholders = ','.join('?' * len(ids_to_mark))
                            local_conn.execute(f"UPDATE measurements SET synced=1 WHERE id IN ({placeholders})", ids_to_mark)
                            local_conn.commit()
                            logger.info(f"Synced {len(rows)} measurement records to cloud.")

            except psycopg2.Error as e:
                logger.warning(f"Postgres connection failed during measurement sync: {e}")

    def _sync_security(self):
        with sqlite3.connect(self.local_db) as local_conn:
            local_cur = local_conn.cursor()
            local_cur.execute("SELECT id, timestamp, event_type, image_path, mode FROM security_events WHERE synced=0 LIMIT 50")
            rows = local_cur.fetchall()

            if not rows:
                return

            try:
                with psycopg2.connect(self.pg_conn_str) as pg_conn:
                    with pg_conn.cursor() as pg_cur:
                        ids_to_mark = []
                        for row in rows:
                            row_id, ts, evt, img, mode = row
                            pg_cur.execute(
                                "INSERT INTO security_events (timestamp, event_type, image_path, mode, device_id) VALUES (%s, %s, %s, %s, %s)",
                                (ts, evt, img, mode, self.device_id)
                            )
                            ids_to_mark.append(row_id)

                        pg_conn.commit()

                        if ids_to_mark:
                            placeholders = ','.join('?' * len(ids_to_mark))
                            local_conn.execute(f"UPDATE security_events SET synced=1 WHERE id IN ({placeholders})", ids_to_mark)
                            local_conn.commit()
                            logger.info(f"Synced {len(rows)} security records to cloud.")

            except psycopg2.Error as e:
                logger.warning(f"Postgres connection failed during security sync: {e}")

    def close(self):
        """Stop the sync thread gracefully."""
        self.running = False
        if self.sync_thread.is_alive():
            self.sync_thread.join(timeout=2)