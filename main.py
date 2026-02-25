#!/usr/bin/env python3
"""
Lumora Door Access System — Main Entry Point

State machine controlling the door access flow:
    IDLE → PIR motion → DETECTING → face match → DOOR_OPEN → IDLE
                                  → face fail → FINGERPRINT → match → DOOR_OPEN
                                                             → fail → DENIED
    INSIDE BUTTON → DOOR_OPEN → IDLE
    OUTSIDE BUTTON → BUZZER ALERT → IDLE
"""

import os
import sys
import signal
import time
import logging
import yaml
from enum import Enum, auto

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from modules.database import Database
from modules.face_recognition_module import FaceRecognitionModule
from modules.fingerprint_module import FingerprintModule, SimulatedFingerprintModule
from modules.gpio_controller import GPIOController
from modules.user_manager import UserManager
from modules.logger import setup_logging, AccessLogger
from modules.firebase_sync import FirebaseSync

logger = logging.getLogger(__name__)


# =============================================================================
# System States
# =============================================================================

class State(Enum):
    IDLE = auto()
    DETECTING = auto()
    FINGERPRINT_CHECK = auto()
    DOOR_OPEN = auto()
    DENIED = auto()
    BUZZER_ALERT = auto()
    ENROLLMENT = auto()
    SHUTTING_DOWN = auto()


# =============================================================================
# Main Application
# =============================================================================

class DoorAccessSystem:
    """Main orchestrator for the door access system."""

    def __init__(self, config_path="config/settings.yaml"):
        self.config = self._load_config(config_path)
        self.state = State.IDLE
        self._running = False
        self._camera = None

        # Initialize all modules
        self._init_modules()

    def _load_config(self, config_path):
        """Load configuration from YAML file."""
        abs_path = os.path.join(PROJECT_ROOT, config_path)
        if not os.path.exists(abs_path):
            print(f"ERROR: Config file not found: {abs_path}")
            sys.exit(1)

        with open(abs_path, "r") as f:
            config = yaml.safe_load(f)

        return config

    def _init_modules(self):
        """Initialize all system modules."""
        system_config = self.config.get("system", {})
        simulate = system_config.get("simulate_gpio", False)

        # Logging
        log_config = {
            **self.config.get("logging", {}),
            "log_level": system_config.get("log_level", "INFO"),
        }
        setup_logging(log_config)
        logger.info("=" * 60)
        logger.info("Lumora Door Access System — Starting")
        logger.info("=" * 60)

        # Database
        db_config = self.config.get("database", {})
        db_path = os.path.join(PROJECT_ROOT, db_config.get("path", "data/door_access.db"))
        self.db = Database(db_path)
        logger.info("Database initialized")

        # Face Recognition
        self.face = FaceRecognitionModule(self.config.get("face_recognition", {}))

        # Load known faces from DB
        users = self.db.get_all_users()
        self.face.load_known_faces(users)
        if not self.face.load_encodings_cache():
            logger.info("No encodings cache found — using DB-loaded encodings")

        # Fingerprint Sensor
        self.fingerprint = None
        logger.info("R&D Mode: Fingerprint auth explicitly disabled")

        # GPIO Controller
        self.gpio = None
        logger.info("R&D Mode: GPIO/Hardware disabled")

        # User Manager
        self.user_manager = UserManager(self.db, self.face, self.fingerprint)

        # Access Logger
        self.access_logger = AccessLogger(self.db, self.config.get("logging", {}))

        # Firebase Sync
        firebase_config = self.config.get("firebase", {})
        self.firebase = FirebaseSync(firebase_config, self.db)
        if firebase_config.get("enabled", False):
            self.firebase.start()

        # Stats
        stats = self.db.get_stats()
        logger.info(
            "System ready — %d users, %d total logs, %d pending sync",
            stats["total_users"], stats["total_logs"], stats["pending_sync"]
        )

    # -------------------------------------------------------------------------
    # Camera Management
    # -------------------------------------------------------------------------

    def _open_camera(self):
        """Open the USB camera."""
        if self._camera is not None:
            return self._camera

        cam_config = self.config.get("camera", {})
        self._camera = self.face.open_camera(
            device_index=cam_config.get("device_index", 0),
            width=cam_config.get("resolution_width", 640),
            height=cam_config.get("resolution_height", 480),
            warmup=cam_config.get("warmup_time", 1.0)
        )
        return self._camera

    def _close_camera(self):
        """Release the camera."""
        if self._camera is not None:
            self.face.close_camera(self._camera)
            self._camera = None

    # -------------------------------------------------------------------------
    # Button Callbacks
    # -------------------------------------------------------------------------

    def _on_outside_button(self):
        """Callback when outside button is pressed."""
        if self.state == State.IDLE or self.state == State.DENIED:
            logger.info("Outside button pressed — alerting insiders")
            self.state = State.BUZZER_ALERT
            self.gpio.ring_buzzer("alert")
            self.access_logger.log_button_event("outside", "alert")
            time.sleep(0.5)
            self.state = State.IDLE

    def _on_inside_button(self):
        """Callback when inside button is pressed."""
        logger.info("Inside button pressed — opening door")
        self.gpio.activate_relay()
        self.gpio.ring_buzzer("success")
        self.access_logger.log_button_event("inside", "door_opened")
        self.state = State.DOOR_OPEN

        # Reset state after relay timeout
        relay_duration = self.config.get("gpio", {}).get("relay_active_duration", 5)
        time.sleep(relay_duration)
        self.state = State.IDLE

    # -------------------------------------------------------------------------
    # Main State Machine
    # -------------------------------------------------------------------------

    def run(self):
        """Main event loop — state machine."""
        self._running = True

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Set up button interrupt callbacks
        if self.gpio:
            self.gpio.setup_button_callbacks(
                outside_callback=self._on_outside_button,
                inside_callback=self._on_inside_button
            )

        pir_config = self.config.get("pir", {})
        idle_timeout = pir_config.get("idle_timeout", 30)
        cooldown = pir_config.get("cooldown", 2)
        poll_interval = pir_config.get("active_check_interval", 0.1)

        logger.info("Entering main loop — waiting for motion...")

        while self._running:
            try:
                if self.state == State.IDLE:
                    self._handle_idle(poll_interval)

                elif self.state == State.DETECTING:
                    self._handle_detecting()

                elif self.state == State.FINGERPRINT_CHECK:
                    self._handle_fingerprint()

                elif self.state == State.DOOR_OPEN:
                    # Door is open — wait for relay to deactivate
                    time.sleep(0.5)
                    if not self.gpio.is_relay_active():
                        self.state = State.IDLE

                elif self.state == State.DENIED:
                    self._handle_denied()

                elif self.state == State.BUZZER_ALERT:
                    # Handled by callback, just wait
                    time.sleep(0.1)

                elif self.state == State.SHUTTING_DOWN:
                    break

            except Exception as e:
                logger.error("Main loop error: %s", e, exc_info=True)
                time.sleep(1)

        self._shutdown()

    def _handle_idle(self, poll_interval):
        """IDLE state — skip PIR, go directly to detecting."""
        logger.info("R&D Mode: Bypassing motion detection")
        self.state = State.DETECTING

    def _handle_detecting(self):
        """DETECTING state — capture face and attempt recognition."""
        camera = self._open_camera()
        if camera is None:
            logger.error("Camera unavailable — falling back to fingerprint")
            self.state = State.FINGERPRINT_CHECK
            return

        # Try face recognition
        result = self.user_manager.identify_by_face(camera)
        user, frame, confidence = result

        if user:
            # Face recognized — grant access
            image_path = None
            if frame is not None:
                image_path = self.face.save_face_image(frame, user["id"])

            print(f"\n*** Access Granted: {user['name']} (Confidence: {confidence:.2f}) ***\n")
            self._grant_access(user, "face", confidence, image_path)
        else:
            # Face failed — save unknown face image
            if frame is not None:
                self.face.save_face_image(frame, "unknown")

            logger.info("Face not recognized — Access Denied")
            print("\n*** Access Denied: Face not recognized ***\n")
            self.state = State.DENIED

    def _handle_fingerprint(self):
        """FINGERPRINT_CHECK state — scan fingerprint."""
        if not self.fingerprint.is_connected():
            logger.warning("Fingerprint sensor not available")
            self.state = State.DENIED
            return

        user, confidence = self.user_manager.identify_by_fingerprint(timeout=10)

        if user:
            self._grant_access(user, "fingerprint", confidence)
        else:
            self.state = State.DENIED

    def _handle_denied(self):
        """DENIED state — access denied, reset after delay."""
        logger.info("Access DENIED — no match found")
        if self.gpio:
            self.gpio.ring_buzzer("denied")
            self.gpio.flash_off()
        self.access_logger.log_access_denied("biometric")

        # Turn off flash and camera
        self._close_camera()

        # Wait before returning to idle
        time.sleep(3)
        self.state = State.IDLE

    def _grant_access(self, user, method, confidence, image_path=None):
        """Grant access to an identified user."""
        logger.info(
            "Access GRANTED to '%s' via %s (confidence=%.3f)",
            user["name"], method, confidence
        )

        # Activate relay (unlock door)
        if self.gpio:
            self.gpio.activate_relay()
            self.gpio.ring_buzzer("success")
            self.gpio.flash_off()

        # Log the event
        self.access_logger.log_access_granted(
            user_id=user["id"],
            user_name=user["name"],
            method=method,
            direction="in",
            confidence=confidence,
            image_path=image_path
        )

        self._close_camera()
        self.state = State.DOOR_OPEN

    # -------------------------------------------------------------------------
    # Shutdown
    # -------------------------------------------------------------------------

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info("Received signal %d — shutting down", signum)
        self._running = False
        self.state = State.SHUTTING_DOWN

    def _shutdown(self):
        """Graceful shutdown — clean up all resources."""
        logger.info("Shutting down...")

        # Stop Firebase sync
        self.firebase.stop()

        # Close camera
        self._close_camera()

        # Disconnect fingerprint sensor
        if self.fingerprint:
            self.fingerprint.disconnect()

        # Turn off all outputs and clean up GPIO
        if self.gpio:
            self.gpio.cleanup()

        # Final sync attempt
        try:
            self.firebase.sync_now()
        except Exception:
            pass

        logger.info("System shutdown complete")


# =============================================================================
# Entry Point
# =============================================================================

def main():
    """Entry point for the door access system."""
    # Change to project directory
    os.chdir(PROJECT_ROOT)

    # Create data directories
    os.makedirs("data/faces", exist_ok=True)
    os.makedirs("data/logs", exist_ok=True)

    # Load config path from command line or use default
    config_path = "config/settings.yaml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    # Start the system
    system = DoorAccessSystem(config_path)

    try:
        system.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down")
    except Exception as e:
        logger.critical("Fatal error: %s", e, exc_info=True)
    finally:
        system._shutdown()


if __name__ == "__main__":
    main()
