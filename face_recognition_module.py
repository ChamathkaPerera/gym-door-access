"""
Face Recognition Module — Capture, encode, and match faces using dlib/face_recognition.

Uses HOG-based detection (fast on CPU) and 128-d face embeddings for comparison.
All known encodings are cached in memory for fast lookup.
"""

import os
import pickle
import logging
import time
import numpy as np

logger = logging.getLogger(__name__)

try:
    import face_recognition
    FACE_RECOGNITION_AVAILABLE = True
except ImportError:
    FACE_RECOGNITION_AVAILABLE = False
    logger.warning("face_recognition library not installed. Face detection disabled.")

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False
    logger.warning("OpenCV not installed. Camera capture disabled.")


class FaceRecognitionModule:
    """Handle face detection, encoding, and matching."""

    def __init__(self, config):
        """
        Args:
            config: dict from settings.yaml 'face_recognition' section.
        """
        self.model = config.get("model", "hog")
        self.tolerance = config.get("tolerance", 0.5)
        self.num_jitters = config.get("num_jitters", 1)
        self.enrollment_samples = config.get("enrollment_samples", 5)
        self.min_face_size = config.get("min_face_size", 40)
        self.cache_file = config.get("encodings_cache_file", "data/encodings_cache.pkl")

        # In-memory cache: {user_id: (name, encoding_128d)}
        self._known_encodings = {}
        self._known_ids = []
        self._known_names = []
        self._known_enc_array = []

    def load_known_faces(self, users):
        """
        Load known face encodings from the database user list into memory.

        Args:
            users: list of user dicts from Database.get_all_users()
        """
        self._known_encodings = {}
        self._known_ids = []
        self._known_names = []
        self._known_enc_array = []

        for user in users:
            if user.get("face_encoding") is not None:
                uid = user["id"]
                name = user["name"]
                encoding = user["face_encoding"]
                self._known_encodings[uid] = (name, encoding)
                self._known_ids.append(uid)
                self._known_names.append(name)
                self._known_enc_array.append(encoding)

        logger.info("Loaded %d known face encodings into memory", len(self._known_ids))

    def capture_frame(self, camera):
        """
        Capture a single frame from the camera.

        Args:
            camera: cv2.VideoCapture instance.

        Returns:
            numpy array (BGR image) or None on failure.
        """
        if not OPENCV_AVAILABLE:
            logger.error("OpenCV not available for capture")
            return None

        ret, frame = camera.read()
        if not ret or frame is None:
            logger.warning("Failed to capture frame from camera")
            return None
        return frame

    def detect_and_encode(self, frame):
        """
        Detect faces in a frame and return their encodings + locations.

        Args:
            frame: BGR numpy array from OpenCV.

        Returns:
            tuple: (encodings_list, locations_list)
        """
        if not FACE_RECOGNITION_AVAILABLE:
            return [], []

        # Convert BGR to RGB for face_recognition
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Detect face locations
        face_locations = face_recognition.face_locations(rgb_frame, model=self.model)

        if not face_locations:
            return [], []

        # Filter small faces
        filtered_locations = []
        for (top, right, bottom, left) in face_locations:
            face_height = bottom - top
            face_width = right - left
            if face_height >= self.min_face_size and face_width >= self.min_face_size:
                filtered_locations.append((top, right, bottom, left))

        if not filtered_locations:
            return [], []

        # Generate encodings
        encodings = face_recognition.face_encodings(
            rgb_frame,
            known_face_locations=filtered_locations,
            num_jitters=self.num_jitters
        )

        return encodings, filtered_locations

    def recognize(self, frame):
        """
        Detect and identify a person in the given frame.

        Args:
            frame: BGR numpy array.

        Returns:
            dict or None: {user_id, name, confidence, face_location} if matched.
        """
        if not self._known_enc_array:
            logger.debug("No known faces loaded")
            return None

        encodings, locations = self.detect_and_encode(frame)

        if not encodings:
            logger.debug("No faces detected in frame")
            return None

        # Check each detected face against known encodings
        for encoding, location in zip(encodings, locations):
            # Compute distances to all known faces
            distances = face_recognition.face_distance(self._known_enc_array, encoding)

            if len(distances) == 0:
                continue

            best_idx = np.argmin(distances)
            best_distance = distances[best_idx]
            confidence = 1.0 - best_distance

            if best_distance <= self.tolerance:
                user_id = self._known_ids[best_idx]
                user_name = self._known_names[best_idx]
                logger.info(
                    "Face matched: %s (ID=%d) confidence=%.3f",
                    user_name, user_id, confidence
                )
                return {
                    "user_id": user_id,
                    "name": user_name,
                    "confidence": confidence,
                    "face_location": location,
                    "distance": best_distance
                }

        logger.info("Face detected but no match (best distance: %.3f)", float(np.min(distances)))
        return None

    def capture_and_recognize(self, camera, num_attempts=3, delay=0.5):
        """
        Capture multiple frames and attempt recognition.

        Tries multiple captures to account for camera warmup and lighting.

        Args:
            camera: cv2.VideoCapture instance.
            num_attempts: Number of frames to try.
            delay: Seconds between attempts.

        Returns:
            tuple: (result_dict or None, best_frame or None)
        """
        best_result = None
        best_frame = None
        best_confidence = 0.0

        for attempt in range(num_attempts):
            frame = self.capture_frame(camera)
            if frame is None:
                time.sleep(delay)
                continue

            result = self.recognize(frame)
            if result and result["confidence"] > best_confidence:
                best_result = result
                best_frame = frame.copy()
                best_confidence = result["confidence"]

            if best_result:
                # Found a good match, no need to keep trying
                break

            time.sleep(delay)

        return best_result, best_frame

    def enroll_face(self, camera, num_samples=None, delay=1.0):
        """
        Capture multiple face images and compute an average encoding.

        Args:
            camera: cv2.VideoCapture instance.
            num_samples: Number of face images to capture (default from config).
            delay: Seconds between captures for variation.

        Returns:
            tuple: (average_encoding, list_of_frames) or (None, []) on failure.
        """
        if num_samples is None:
            num_samples = self.enrollment_samples

        encodings = []
        frames = []
        attempts = 0
        max_attempts = num_samples * 3  # Allow retries

        logger.info("Starting face enrollment — capturing %d samples", num_samples)

        while len(encodings) < num_samples and attempts < max_attempts:
            attempts += 1
            frame = self.capture_frame(camera)
            if frame is None:
                time.sleep(delay)
                continue

            face_encodings, face_locations = self.detect_and_encode(frame)

            if len(face_encodings) == 1:
                encodings.append(face_encodings[0])
                frames.append(frame.copy())
                logger.info(
                    "Enrollment sample %d/%d captured",
                    len(encodings), num_samples
                )
            elif len(face_encodings) > 1:
                logger.warning("Multiple faces detected — show only one face")
            else:
                logger.debug("No face detected, attempt %d/%d", attempts, max_attempts)

            time.sleep(delay)

        if len(encodings) < 2:
            logger.error("Enrollment failed — insufficient samples (%d)", len(encodings))
            return None, []

        # Compute average encoding
        avg_encoding = np.mean(encodings, axis=0)
        logger.info("Face enrollment complete — %d samples averaged", len(encodings))
        return avg_encoding, frames

    def save_face_image(self, frame, user_id, images_dir="data/faces"):
        """
        Save a captured face image to disk.

        Args:
            frame: BGR numpy array.
            user_id: User ID for directory organization.
            images_dir: Base directory for face images.

        Returns:
            str: Path to the saved image file.
        """
        if not OPENCV_AVAILABLE:
            return None

        user_dir = os.path.join(images_dir, str(user_id))
        os.makedirs(user_dir, exist_ok=True)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"face_{timestamp}.jpg"
        filepath = os.path.join(user_dir, filename)

        cv2.imwrite(filepath, frame)
        logger.debug("Saved face image: %s", filepath)
        return filepath

    def save_encodings_cache(self):
        """Save current known encodings to a pickle cache file."""
        cache_dir = os.path.dirname(self.cache_file)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

        with open(self.cache_file, "wb") as f:
            pickle.dump(self._known_encodings, f)
        logger.info("Encodings cache saved to %s", self.cache_file)

    def load_encodings_cache(self):
        """Load known encodings from pickle cache (fast startup)."""
        if os.path.exists(self.cache_file):
            with open(self.cache_file, "rb") as f:
                self._known_encodings = pickle.load(f)

            self._known_ids = list(self._known_encodings.keys())
            self._known_names = [v[0] for v in self._known_encodings.values()]
            self._known_enc_array = [v[1] for v in self._known_encodings.values()]

            logger.info("Loaded %d encodings from cache", len(self._known_ids))
            return True
        return False

    def open_camera(self, device_index=0, width=640, height=480, warmup=1.0):
        """
        Open a USB camera with specified settings.

        Args:
            device_index: /dev/videoN index.
            width: Capture width.
            height: Capture height.
            warmup: Seconds to wait for camera auto-adjust.

        Returns:
            cv2.VideoCapture instance or None.
        """
        if not OPENCV_AVAILABLE:
            logger.error("OpenCV not available")
            return None

        camera = cv2.VideoCapture(device_index)
        if not camera.isOpened():
            logger.error("Failed to open camera at index %d", device_index)
            return None

        camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # Warmup — capture and discard a few frames
        time.sleep(warmup)
        for _ in range(5):
            camera.read()

        logger.info("Camera opened (index=%d, %dx%d)", device_index, width, height)
        return camera

    @staticmethod
    def close_camera(camera):
        """Release camera resources."""
        if camera is not None:
            camera.release()
            logger.info("Camera released")
