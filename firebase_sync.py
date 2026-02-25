"""
Firebase Sync Module — Background synchronization of local data to Firebase.

Runs in a background thread, periodically checking for internet connectivity
and syncing unsynced users, access logs, and face images to Firebase.
Uses exponential backoff on connection failures.
"""

import os
import time
import json
import pickle
import logging
import threading
import socket
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import firebase_admin
    from firebase_admin import credentials, db as firebase_db, storage
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    logger.warning("firebase-admin not installed. Firebase sync disabled.")


class FirebaseSync:
    """Background Firebase synchronization service."""

    def __init__(self, config, database):
        """
        Args:
            config: dict from settings.yaml 'firebase' section.
            database: Database instance.
        """
        self.enabled = config.get("enabled", True)
        self.cred_path = config.get("credentials_path", "config/firebase_credentials.json")
        self.db_url = config.get("database_url", "")
        self.storage_bucket = config.get("storage_bucket", "")
        self.sync_interval = config.get("sync_interval", 60)
        self.max_retry_delay = config.get("max_retry_delay", 300)
        self.upload_images = config.get("upload_images", True)

        self.database = database
        self._firebase_app = None
        self._initialized = False
        self._running = False
        self._thread = None
        self._retry_delay = 5  # Initial retry delay in seconds
        self._last_sync_time = None
        self._sync_stats = {"users": 0, "logs": 0, "images": 0, "errors": 0}

    def initialize(self):
        """
        Initialize Firebase Admin SDK.

        Returns:
            bool: True if initialized successfully.
        """
        if not self.enabled:
            logger.info("Firebase sync is disabled in config")
            return False

        if not FIREBASE_AVAILABLE:
            logger.error("firebase-admin SDK not installed")
            return False

        if not os.path.exists(self.cred_path):
            logger.error("Firebase credentials file not found: %s", self.cred_path)
            return False

        if not self.db_url:
            logger.error("Firebase database_url not configured")
            return False

        try:
            cred = credentials.Certificate(self.cred_path)

            init_kwargs = {
                "credential": cred,
                "databaseURL": self.db_url,
            }
            if self.storage_bucket:
                init_kwargs["storageBucket"] = self.storage_bucket

            self._firebase_app = firebase_admin.initialize_app(cred, init_kwargs)
            self._initialized = True
            logger.info("Firebase initialized successfully")
            return True

        except Exception as e:
            logger.error("Firebase initialization failed: %s", e)
            return False

    def start(self):
        """Start the background sync thread."""
        if not self._initialized:
            if not self.initialize():
                logger.warning("Firebase not initialized — sync disabled")
                return

        self._running = True
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()
        logger.info("Firebase sync thread started (interval=%ds)", self.sync_interval)

    def stop(self):
        """Stop the background sync thread."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("Firebase sync thread stopped")

    def is_running(self):
        """Check if sync thread is running."""
        return self._running and self._thread and self._thread.is_alive()

    def get_sync_stats(self):
        """Get synchronization statistics."""
        return {
            **self._sync_stats,
            "last_sync": self._last_sync_time,
            "is_running": self.is_running(),
        }

    # -------------------------------------------------------------------------
    # Background Sync Loop
    # -------------------------------------------------------------------------

    def _sync_loop(self):
        """Main sync loop running in a background thread."""
        logger.info("Sync loop started")

        while self._running:
            try:
                if self._check_internet():
                    self._perform_sync()
                    self._retry_delay = 5  # Reset on success
                else:
                    logger.debug("No internet — skipping sync")
                    self._retry_delay = min(
                        self._retry_delay * 2,
                        self.max_retry_delay
                    )
            except Exception as e:
                logger.error("Sync error: %s", e)
                self._sync_stats["errors"] += 1
                self._retry_delay = min(
                    self._retry_delay * 2,
                    self.max_retry_delay
                )

            # Wait for next sync interval
            wait_time = max(self.sync_interval, self._retry_delay)
            for _ in range(int(wait_time)):
                if not self._running:
                    return
                time.sleep(1)

    def _perform_sync(self):
        """Execute all pending sync operations."""
        logger.debug("Starting sync cycle...")

        # Sync users
        self._sync_users()

        # Sync access logs
        self._sync_access_logs()

        # Pull remote updates (new users from admin panel)
        self._pull_remote_users()

        self._last_sync_time = datetime.now().isoformat()
        logger.debug("Sync cycle complete")

    # -------------------------------------------------------------------------
    # Sync Users
    # -------------------------------------------------------------------------

    def _sync_users(self):
        """Sync unsynced users to Firebase."""
        unsynced = self.database.get_unsynced_users()

        if not unsynced:
            return

        logger.info("Syncing %d users to Firebase", len(unsynced))
        ref = firebase_db.reference("users")

        for user in unsynced:
            try:
                user_data = {
                    "name": user["name"],
                    "fingerprint_id": user.get("fingerprint_id", -1),
                    "registered_at": user.get("registered_at"),
                    "updated_at": user.get("updated_at"),
                    "active": bool(user.get("active", True)),
                    "has_face_encoding": user.get("face_encoding") is not None,
                }

                # Upload face encoding as base64 if available
                if user.get("face_encoding") is not None:
                    import base64
                    encoded = base64.b64encode(
                        pickle.dumps(user["face_encoding"])
                    ).decode("utf-8")
                    user_data["face_encoding_b64"] = encoded

                ref.child(str(user["id"])).set(user_data)
                self.database.mark_synced("users", user["id"])
                self._sync_stats["users"] += 1
                logger.debug("Synced user %d: %s", user["id"], user["name"])

            except Exception as e:
                logger.error("Failed to sync user %d: %s", user["id"], e)
                self._sync_stats["errors"] += 1

    # -------------------------------------------------------------------------
    # Sync Access Logs
    # -------------------------------------------------------------------------

    def _sync_access_logs(self):
        """Sync unsynced access logs to Firebase."""
        unsynced = self.database.get_unsynced_logs()

        if not unsynced:
            return

        logger.info("Syncing %d access logs to Firebase", len(unsynced))
        ref = firebase_db.reference("access_logs")

        for log_entry in unsynced:
            try:
                log_data = {
                    "user_id": log_entry.get("user_id"),
                    "user_name": log_entry.get("user_name", "Unknown"),
                    "method": log_entry["method"],
                    "direction": log_entry.get("direction", "in"),
                    "status": log_entry["status"],
                    "timestamp": log_entry["timestamp"],
                    "confidence": log_entry.get("confidence", 0.0),
                    "device_id": socket.gethostname(),
                }

                # Upload image to Firebase Storage if available
                image_path = log_entry.get("image_path")
                if image_path and self.upload_images and os.path.exists(image_path):
                    image_url = self._upload_image(image_path, log_entry["id"])
                    if image_url:
                        log_data["image_url"] = image_url
                        self._sync_stats["images"] += 1

                ref.child(str(log_entry["id"])).set(log_data)
                self.database.mark_synced("access_logs", log_entry["id"])
                self._sync_stats["logs"] += 1

            except Exception as e:
                logger.error("Failed to sync log %d: %s", log_entry["id"], e)
                self._sync_stats["errors"] += 1

    def _upload_image(self, image_path, log_id):
        """
        Upload a face image to Firebase Storage.

        Returns:
            str: Public URL of the uploaded image, or None.
        """
        if not self.storage_bucket:
            return None

        try:
            bucket = storage.bucket()
            blob_name = f"access_images/{log_id}_{os.path.basename(image_path)}"
            blob = bucket.blob(blob_name)
            blob.upload_from_filename(image_path)
            blob.make_public()
            return blob.public_url
        except Exception as e:
            logger.error("Image upload failed: %s", e)
            return None

    # -------------------------------------------------------------------------
    # Pull Remote Users
    # -------------------------------------------------------------------------

    def _pull_remote_users(self):
        """
        Check Firebase for new users registered remotely (e.g., admin panel).
        Download their data and add to local DB if not present.
        """
        try:
            ref = firebase_db.reference("remote_enrollments")
            remote_data = ref.get()

            if not remote_data:
                return

            for remote_id, user_data in remote_data.items():
                if not user_data.get("pending", False):
                    continue

                name = user_data.get("name", "Remote User")
                logger.info("Found pending remote enrollment: %s", name)

                # Add to local DB (face/fingerprint enrollment needs physical presence)
                user_id = self.database.add_user(name, face_encoding=None, fingerprint_id=-1)

                # Mark as processed in Firebase
                ref.child(remote_id).update({
                    "pending": False,
                    "local_id": user_id,
                    "processed_at": datetime.now().isoformat(),
                })

                logger.info("Remote user '%s' added locally (ID=%d)", name, user_id)

        except Exception as e:
            logger.error("Failed to pull remote users: %s", e)

    # -------------------------------------------------------------------------
    # Manual Sync
    # -------------------------------------------------------------------------

    def sync_now(self):
        """Trigger an immediate sync (blocking call)."""
        if not self._initialized:
            logger.warning("Firebase not initialized")
            return False

        if not self._check_internet():
            logger.warning("No internet connection")
            return False

        try:
            self._perform_sync()
            return True
        except Exception as e:
            logger.error("Manual sync failed: %s", e)
            return False

    # -------------------------------------------------------------------------
    # Utilities
    # -------------------------------------------------------------------------

    @staticmethod
    def _check_internet(host="8.8.8.8", port=53, timeout=3):
        """
        Check internet connectivity by attempting a socket connection.

        Args:
            host: DNS server to connect to.
            port: Port to connect to.
            timeout: Connection timeout in seconds.

        Returns:
            bool: True if internet is available.
        """
        try:
            socket.setdefaulttimeout(timeout)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
            sock.close()
            return True
        except (socket.error, OSError):
            return False
