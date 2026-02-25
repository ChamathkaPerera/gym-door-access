"""
User Manager — Enrollment, identification, and linking face + fingerprint.

Coordinates between the face recognition module, fingerprint module,
and database to provide unified user management.
"""

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)


class UserManager:
    """Manage user enrollment, identification, and data linking."""

    def __init__(self, database, face_module, fingerprint_module):
        """
        Args:
            database: Database instance.
            face_module: FaceRecognitionModule instance.
            fingerprint_module: FingerprintModule instance.
        """
        self.db = database
        self.face = face_module
        self.fingerprint = fingerprint_module

    def register_user(self, name, camera, enroll_face=True, enroll_fingerprint=True):
        """
        Register a new user with face encoding and/or fingerprint.

        Steps:
            1. Capture face images and compute average encoding
            2. Enroll fingerprint on R503 sensor
            3. Store both in DB linked to the same user ID
            4. Backup fingerprint template to DB

        Args:
            name: Display name for the user.
            camera: cv2.VideoCapture instance.
            enroll_face: Whether to enroll face.
            enroll_fingerprint: Whether to enroll fingerprint.

        Returns:
            dict: {user_id, name, has_face, has_fingerprint} or None on failure.
        """
        face_encoding = None
        fingerprint_id = -1
        face_images = []

        # --- Step 1: Face Enrollment ---
        if enroll_face:
            logger.info("Starting face enrollment for '%s'", name)
            face_encoding, face_images = self.face.enroll_face(camera)

            if face_encoding is None:
                logger.error("Face enrollment failed for '%s'", name)
                if not enroll_fingerprint:
                    return None

        # --- Step 2: Fingerprint Enrollment ---
        if enroll_fingerprint and self.fingerprint.is_connected():
            logger.info("Starting fingerprint enrollment for '%s'", name)
            fingerprint_id = self.fingerprint.enroll_fingerprint()

            if fingerprint_id is None:
                logger.error("Fingerprint enrollment failed for '%s'", name)
                fingerprint_id = -1
                if face_encoding is None:
                    return None

        # --- Step 3: Store in Database ---
        user_id = self.db.add_user(name, face_encoding, fingerprint_id)

        # Save face images to disk
        if face_images:
            for frame in face_images:
                self.face.save_face_image(frame, user_id)

        # --- Step 4: Backup fingerprint template ---
        if fingerprint_id >= 0 and self.fingerprint.is_connected():
            template = self.fingerprint.download_template(fingerprint_id)
            if template:
                self.db.save_fingerprint_template(user_id, fingerprint_id, template)

        # Reload face encodings cache
        self._reload_face_cache()

        result = {
            "user_id": user_id,
            "name": name,
            "has_face": face_encoding is not None,
            "has_fingerprint": fingerprint_id >= 0,
            "fingerprint_id": fingerprint_id
        }

        logger.info(
            "User '%s' registered (ID=%d, face=%s, fingerprint=%s)",
            name, user_id, result["has_face"], result["has_fingerprint"]
        )
        return result

    def identify_by_face(self, camera, num_attempts=3):
        """
        Attempt to identify a user by face recognition.

        Args:
            camera: cv2.VideoCapture instance.
            num_attempts: Number of frame captures to try.

        Returns:
            tuple: (user_dict, frame, confidence) or (None, frame, 0.0)
        """
        result, frame = self.face.capture_and_recognize(camera, num_attempts)

        if result:
            user = self.db.get_user(result["user_id"])
            return user, frame, result["confidence"]

        return None, frame, 0.0

    def identify_by_fingerprint(self, timeout=10):
        """
        Attempt to identify a user by fingerprint.

        Args:
            timeout: Seconds to wait for finger placement.

        Returns:
            tuple: (user_dict, confidence) or (None, 0.0)
        """
        if not self.fingerprint.is_connected():
            logger.warning("Fingerprint sensor not connected")
            return None, 0.0

        result = self.fingerprint.search_fingerprint(timeout)

        if result:
            user = self.db.get_user_by_fingerprint(result["fingerprint_id"])
            if user:
                return user, result["confidence"]
            else:
                logger.warning(
                    "Fingerprint ID %d matched on sensor but not found in database",
                    result["fingerprint_id"]
                )

        return None, 0.0

    def identify_user(self, camera=None, try_face=True, try_fingerprint=True,
                      fingerprint_timeout=10):
        """
        Attempt to identify a user using available methods.

        Tries face recognition first, then fingerprint if face fails.

        Args:
            camera: cv2.VideoCapture instance (needed for face recognition).
            try_face: Whether to attempt face recognition.
            try_fingerprint: Whether to attempt fingerprint.
            fingerprint_timeout: Seconds to wait for fingerprint.

        Returns:
            dict: {user, method, confidence, frame} or None.
        """
        frame = None

        # Try face first
        if try_face and camera is not None:
            user, frame, confidence = self.identify_by_face(camera)
            if user:
                return {
                    "user": user,
                    "method": "face",
                    "confidence": confidence,
                    "frame": frame
                }
            logger.info("Face recognition failed, trying fingerprint...")

        # Fall back to fingerprint
        if try_fingerprint and self.fingerprint.is_connected():
            user, confidence = self.identify_by_fingerprint(fingerprint_timeout)
            if user:
                return {
                    "user": user,
                    "method": "fingerprint",
                    "confidence": confidence,
                    "frame": frame
                }

        return None

    def update_user_face(self, user_id, camera):
        """
        Re-enroll a user's face (update encoding).

        Args:
            user_id: ID of the user to update.
            camera: cv2.VideoCapture instance.

        Returns:
            bool: True if successful.
        """
        user = self.db.get_user(user_id)
        if not user:
            logger.error("User %d not found", user_id)
            return False

        encoding, images = self.face.enroll_face(camera)
        if encoding is None:
            return False

        self.db.update_user_face(user_id, encoding)

        for frame in images:
            self.face.save_face_image(frame, user_id)

        self._reload_face_cache()
        return True

    def update_user_fingerprint(self, user_id):
        """
        Re-enroll a user's fingerprint (update sensor template).

        Args:
            user_id: ID of the user to update.

        Returns:
            bool: True if successful.
        """
        user = self.db.get_user(user_id)
        if not user:
            logger.error("User %d not found", user_id)
            return False

        # Delete old fingerprint from sensor if exists
        old_fid = user.get("fingerprint_id", -1)
        if old_fid >= 0:
            self.fingerprint.delete_fingerprint(old_fid)

        new_fid = self.fingerprint.enroll_fingerprint()
        if new_fid is None:
            return False

        self.db.update_user_fingerprint(user_id, new_fid)

        # Backup new template
        template = self.fingerprint.download_template(new_fid)
        if template:
            self.db.save_fingerprint_template(user_id, new_fid, template)

        return True

    def delete_user(self, user_id):
        """
        Delete a user and their fingerprint from the sensor.

        Args:
            user_id: ID of the user to delete.

        Returns:
            bool: True if successful.
        """
        user = self.db.get_user(user_id)
        if not user:
            logger.error("User %d not found", user_id)
            return False

        # Delete fingerprint from sensor
        fid = user.get("fingerprint_id", -1)
        if fid >= 0 and self.fingerprint.is_connected():
            self.fingerprint.delete_fingerprint(fid)

        # Soft-delete in database
        self.db.delete_user(user_id)

        # Reload face cache
        self._reload_face_cache()

        logger.info("User '%s' (ID=%d) deleted", user["name"], user_id)
        return True

    def list_users(self):
        """
        List all active users.

        Returns:
            list: List of user dicts.
        """
        users = self.db.get_all_users()
        # Remove raw encoding data for display
        result = []
        for u in users:
            result.append({
                "id": u["id"],
                "name": u["name"],
                "has_face": u.get("face_encoding") is not None,
                "fingerprint_id": u.get("fingerprint_id", -1),
                "registered_at": u.get("registered_at"),
            })
        return result

    def _reload_face_cache(self):
        """Reload face encodings from DB into the recognition module."""
        users = self.db.get_all_users()
        self.face.load_known_faces(users)
        self.face.save_encodings_cache()
