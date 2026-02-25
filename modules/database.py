"""
Database Module — SQLite local storage for users, access logs, and sync queue.

Tables:
    - users: face encodings + fingerprint IDs linked to named users
    - access_logs: every access event (face/fingerprint/button)
    - sync_queue: pending Firebase sync items
"""

import sqlite3
import os
import pickle
import threading
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class Database:
    """Thread-safe SQLite database for the door access system."""

    def __init__(self, db_path="data/door_access.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._ensure_directory()
        self._init_db()

    def _ensure_directory(self):
        """Create database directory if it doesn't exist."""
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

    def _get_connection(self):
        """Get a new connection (one per call for thread safety)."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        """Initialize database tables."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        face_encoding BLOB,
                        fingerprint_id INTEGER DEFAULT -1,
                        registered_at TEXT NOT NULL,
                        updated_at TEXT,
                        synced INTEGER DEFAULT 0,
                        active INTEGER DEFAULT 1
                    );

                    CREATE TABLE IF NOT EXISTS access_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        user_name TEXT DEFAULT 'Unknown',
                        method TEXT NOT NULL,
                        direction TEXT DEFAULT 'in',
                        status TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        image_path TEXT,
                        confidence REAL DEFAULT 0.0,
                        synced INTEGER DEFAULT 0,
                        FOREIGN KEY (user_id) REFERENCES users(id)
                    );

                    CREATE TABLE IF NOT EXISTS sync_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        table_name TEXT NOT NULL,
                        record_id INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        attempts INTEGER DEFAULT 0,
                        last_attempt TEXT
                    );

                    CREATE TABLE IF NOT EXISTS fingerprint_backup (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        fingerprint_id INTEGER NOT NULL,
                        template_data BLOB,
                        created_at TEXT NOT NULL,
                        synced INTEGER DEFAULT 0,
                        FOREIGN KEY (user_id) REFERENCES users(id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_access_logs_timestamp
                        ON access_logs(timestamp);
                    CREATE INDEX IF NOT EXISTS idx_access_logs_synced
                        ON access_logs(synced);
                    CREATE INDEX IF NOT EXISTS idx_users_fingerprint
                        ON users(fingerprint_id);
                    CREATE INDEX IF NOT EXISTS idx_sync_queue_table
                        ON sync_queue(table_name);
                """)
                conn.commit()
                logger.info("Database initialized at %s", self.db_path)
            finally:
                conn.close()

    # -------------------------------------------------------------------------
    # User Operations
    # -------------------------------------------------------------------------

    def add_user(self, name, face_encoding=None, fingerprint_id=-1):
        """
        Register a new user with optional face encoding and fingerprint ID.

        Args:
            name: User's display name.
            face_encoding: 128-d numpy array (will be pickled), or None.
            fingerprint_id: ID from the R503 sensor, or -1 if not enrolled.

        Returns:
            int: The new user's ID.
        """
        encoding_blob = pickle.dumps(face_encoding) if face_encoding is not None else None
        now = datetime.now().isoformat()

        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """INSERT INTO users (name, face_encoding, fingerprint_id, registered_at)
                       VALUES (?, ?, ?, ?)""",
                    (name, encoding_blob, fingerprint_id, now)
                )
                user_id = cursor.lastrowid
                # Add to sync queue
                conn.execute(
                    """INSERT INTO sync_queue (table_name, record_id, action, created_at)
                       VALUES ('users', ?, 'create', ?)""",
                    (user_id, now)
                )
                conn.commit()
                logger.info("User '%s' registered with ID %d", name, user_id)
                return user_id
            finally:
                conn.close()

    def update_user_face(self, user_id, face_encoding):
        """Update a user's face encoding."""
        encoding_blob = pickle.dumps(face_encoding)
        now = datetime.now().isoformat()

        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE users SET face_encoding=?, updated_at=?, synced=0 WHERE id=?",
                    (encoding_blob, now, user_id)
                )
                conn.execute(
                    """INSERT INTO sync_queue (table_name, record_id, action, created_at)
                       VALUES ('users', ?, 'update', ?)""",
                    (user_id, now)
                )
                conn.commit()
                logger.info("Updated face encoding for user %d", user_id)
            finally:
                conn.close()

    def update_user_fingerprint(self, user_id, fingerprint_id):
        """Update a user's fingerprint ID."""
        now = datetime.now().isoformat()

        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE users SET fingerprint_id=?, updated_at=?, synced=0 WHERE id=?",
                    (fingerprint_id, now, user_id)
                )
                conn.execute(
                    """INSERT INTO sync_queue (table_name, record_id, action, created_at)
                       VALUES ('users', ?, 'update', ?)""",
                    (user_id, now)
                )
                conn.commit()
                logger.info("Updated fingerprint for user %d → sensor ID %d", user_id, fingerprint_id)
            finally:
                conn.close()

    def get_user(self, user_id):
        """Get a single user by ID."""
        with self._lock:
            conn = self._get_connection()
            try:
                row = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
                if row:
                    return self._row_to_user(row)
                return None
            finally:
                conn.close()

    def get_user_by_fingerprint(self, fingerprint_id):
        """Look up a user by their fingerprint sensor ID."""
        with self._lock:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM users WHERE fingerprint_id=? AND active=1",
                    (fingerprint_id,)
                ).fetchone()
                if row:
                    return self._row_to_user(row)
                return None
            finally:
                conn.close()

    def get_all_users(self):
        """Get all active users with their face encodings deserialized."""
        with self._lock:
            conn = self._get_connection()
            try:
                rows = conn.execute("SELECT * FROM users WHERE active=1").fetchall()
                return [self._row_to_user(row) for row in rows]
            finally:
                conn.close()

    def delete_user(self, user_id):
        """Soft-delete a user (mark inactive)."""
        now = datetime.now().isoformat()
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE users SET active=0, updated_at=?, synced=0 WHERE id=?",
                    (now, user_id)
                )
                conn.execute(
                    """INSERT INTO sync_queue (table_name, record_id, action, created_at)
                       VALUES ('users', ?, 'delete', ?)""",
                    (user_id, now)
                )
                conn.commit()
                logger.info("User %d deactivated", user_id)
            finally:
                conn.close()

    def _row_to_user(self, row):
        """Convert a database row to a user dict with deserialized encoding."""
        user = dict(row)
        if user.get("face_encoding"):
            user["face_encoding"] = pickle.loads(user["face_encoding"])
        return user

    # -------------------------------------------------------------------------
    # Fingerprint Backup
    # -------------------------------------------------------------------------

    def save_fingerprint_template(self, user_id, fingerprint_id, template_data):
        """Back up a fingerprint template from the sensor to the local DB."""
        now = datetime.now().isoformat()
        blob = pickle.dumps(template_data) if template_data else None

        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """INSERT INTO fingerprint_backup
                       (user_id, fingerprint_id, template_data, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, fingerprint_id, blob, now)
                )
                conn.commit()
                logger.info("Fingerprint template backed up for user %d", user_id)
            finally:
                conn.close()

    def get_fingerprint_templates(self):
        """Get all backed-up fingerprint templates."""
        with self._lock:
            conn = self._get_connection()
            try:
                rows = conn.execute("SELECT * FROM fingerprint_backup").fetchall()
                results = []
                for row in rows:
                    entry = dict(row)
                    if entry.get("template_data"):
                        entry["template_data"] = pickle.loads(entry["template_data"])
                    results.append(entry)
                return results
            finally:
                conn.close()

    # -------------------------------------------------------------------------
    # Access Log Operations
    # -------------------------------------------------------------------------

    def log_access(self, user_id, user_name, method, direction, status,
                   image_path=None, confidence=0.0):
        """
        Log an access event.

        Args:
            user_id: ID of recognized user (None if unknown).
            user_name: Name of the user or 'Unknown'.
            method: 'face', 'fingerprint', 'inside_button', 'outside_button'.
            direction: 'in' or 'out'.
            status: 'granted', 'denied', 'alert'.
            image_path: Path to the captured face image.
            confidence: Match confidence score (0.0-1.0).
        """
        now = datetime.now().isoformat()

        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """INSERT INTO access_logs
                       (user_id, user_name, method, direction, status,
                        timestamp, image_path, confidence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, user_name, method, direction, status,
                     now, image_path, confidence)
                )
                log_id = cursor.lastrowid
                conn.execute(
                    """INSERT INTO sync_queue (table_name, record_id, action, created_at)
                       VALUES ('access_logs', ?, 'create', ?)""",
                    (log_id, now)
                )
                conn.commit()
                logger.info("Access log: %s %s via %s → %s", user_name, direction, method, status)
                return log_id
            finally:
                conn.close()

    def get_recent_logs(self, limit=50):
        """Get the most recent access logs."""
        with self._lock:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM access_logs ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def get_logs_by_date(self, start_date, end_date):
        """Get access logs within a date range (ISO format strings)."""
        with self._lock:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    """SELECT * FROM access_logs
                       WHERE timestamp BETWEEN ? AND ?
                       ORDER BY timestamp DESC""",
                    (start_date, end_date)
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    # -------------------------------------------------------------------------
    # Sync Queue Operations
    # -------------------------------------------------------------------------

    def get_pending_sync(self, table_name=None, limit=100):
        """Get pending sync items, optionally filtered by table."""
        with self._lock:
            conn = self._get_connection()
            try:
                if table_name:
                    rows = conn.execute(
                        """SELECT * FROM sync_queue
                           WHERE table_name=? ORDER BY created_at ASC LIMIT ?""",
                        (table_name, limit)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM sync_queue ORDER BY created_at ASC LIMIT ?",
                        (limit,)
                    ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def mark_synced(self, table_name, record_id):
        """Mark a record as synced in its source table and remove from queue."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    f"UPDATE {table_name} SET synced=1 WHERE id=?",
                    (record_id,)
                )
                conn.execute(
                    "DELETE FROM sync_queue WHERE table_name=? AND record_id=?",
                    (table_name, record_id)
                )
                conn.commit()
            finally:
                conn.close()

    def update_sync_attempt(self, queue_id):
        """Increment sync attempt counter."""
        now = datetime.now().isoformat()
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE sync_queue SET attempts=attempts+1, last_attempt=? WHERE id=?",
                    (now, queue_id)
                )
                conn.commit()
            finally:
                conn.close()

    def get_unsynced_logs(self):
        """Get all access logs that haven't been synced."""
        with self._lock:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM access_logs WHERE synced=0 ORDER BY timestamp ASC"
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def get_unsynced_users(self):
        """Get all users that haven't been synced."""
        with self._lock:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM users WHERE synced=0"
                ).fetchall()
                return [self._row_to_user(row) for row in rows]
            finally:
                conn.close()

    # -------------------------------------------------------------------------
    # Stats
    # -------------------------------------------------------------------------

    def get_stats(self):
        """Get summary statistics."""
        with self._lock:
            conn = self._get_connection()
            try:
                total_users = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE active=1"
                ).fetchone()[0]
                total_logs = conn.execute(
                    "SELECT COUNT(*) FROM access_logs"
                ).fetchone()[0]
                pending_sync = conn.execute(
                    "SELECT COUNT(*) FROM sync_queue"
                ).fetchone()[0]
                today = datetime.now().strftime("%Y-%m-%d")
                today_access = conn.execute(
                    "SELECT COUNT(*) FROM access_logs WHERE timestamp LIKE ?",
                    (f"{today}%",)
                ).fetchone()[0]
                return {
                    "total_users": total_users,
                    "total_logs": total_logs,
                    "pending_sync": pending_sync,
                    "today_access": today_access
                }
            finally:
                conn.close()
