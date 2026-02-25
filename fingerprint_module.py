"""
Fingerprint Module — R503 sensor communication via UART.

Handles enrollment, searching, deletion, and LED control for the R503
fingerprint sensor connected via UART (GPIO14/15 on RPi).
Templates are stored on-sensor for fast matching & backed up to SQLite.
"""

import logging
import time
import serial

logger = logging.getLogger(__name__)

try:
    from pyfingerprint.pyfingerprint import PyFingerprint
    FINGERPRINT_AVAILABLE = True
except ImportError:
    FINGERPRINT_AVAILABLE = False
    logger.warning("pyfingerprint not installed. Fingerprint module disabled.")

# R503 LED color codes (Aura LED)
LED_COLORS = {
    "red": 0x01,
    "blue": 0x02,
    "purple": 0x03,
    "green": 0x04,
    "yellow": 0x05,
    "cyan": 0x06,
    "white": 0x07,
}

LED_MODES = {
    "breathing": 0x01,
    "flashing": 0x02,
    "on": 0x03,
    "off": 0x04,
    "gradual_on": 0x05,
    "gradual_off": 0x06,
}


class FingerprintModule:
    """Interface to the R503 fingerprint sensor via UART."""

    def __init__(self, config):
        """
        Args:
            config: dict from settings.yaml 'fingerprint' section.
        """
        self.port = config.get("uart_port", "/dev/ttyS0")
        self.baud_rate = config.get("baud_rate", 57600)
        self.timeout = config.get("timeout", 2)
        self.security_level = config.get("security_level", 3)
        self.max_templates = config.get("max_templates", 200)
        self.led_config = config.get("led_colors", {})

        self.sensor = None
        self._connected = False

    def connect(self):
        """
        Initialize connection to the R503 sensor.

        Returns:
            bool: True if connected successfully.
        """
        if not FINGERPRINT_AVAILABLE:
            logger.error("pyfingerprint library not available")
            return False

        try:
            self.sensor = PyFingerprint(
                self.port,
                self.baud_rate,
                0xFFFFFFFF,  # Address
                0x00000000   # Password
            )

            if not self.sensor.verifyPassword():
                logger.error("Fingerprint sensor password verification failed")
                return False

            self.sensor.setSecurityLevel(self.security_level)

            template_count = self.sensor.getTemplateCount()
            storage_capacity = self.sensor.getStorageCapacity()

            logger.info(
                "Fingerprint sensor connected: %d/%d templates stored",
                template_count, storage_capacity
            )
            self._connected = True
            self._set_led("off", "blue")
            return True

        except Exception as e:
            logger.error("Failed to connect to fingerprint sensor: %s", e)
            self._connected = False
            return False

    def disconnect(self):
        """Close the sensor connection."""
        if self.sensor:
            self._set_led("off", "blue")
            self._connected = False
            logger.info("Fingerprint sensor disconnected")

    def is_connected(self):
        """Check if sensor is connected."""
        return self._connected

    def search_fingerprint(self, timeout_seconds=10):
        """
        Wait for a finger and search for a match.

        The LED will show:
            - Blue (breathing): Waiting for finger
            - Green (on): Match found
            - Red (flashing): No match

        Args:
            timeout_seconds: Max seconds to wait for a finger.

        Returns:
            dict or None: {fingerprint_id, confidence} if matched.
        """
        if not self._connected:
            logger.error("Sensor not connected")
            return None

        try:
            # Blue breathing — waiting for finger
            self._set_led("breathing", "blue")

            # Wait for finger to be placed
            start_time = time.time()
            logger.info("Waiting for finger on sensor...")

            while not self.sensor.readImage():
                if time.time() - start_time > timeout_seconds:
                    logger.info("Fingerprint scan timeout")
                    self._set_led("off", "blue")
                    return None
                time.sleep(0.1)

            # Finger detected — convert to template
            self._set_led("on", "blue")
            self.sensor.convertImage(0x01)

            # Search in stored templates
            result = self.sensor.searchTemplate()
            finger_id = result[0]
            confidence = result[1]

            if finger_id == -1:
                # No match
                logger.info("Fingerprint not recognized")
                self._set_led("flashing", "red")
                time.sleep(1.5)
                self._set_led("off", "red")
                return None
            else:
                # Match found
                logger.info(
                    "Fingerprint matched: ID=%d, confidence=%d",
                    finger_id, confidence
                )
                self._set_led("on", "green")
                time.sleep(1.5)
                self._set_led("off", "green")
                return {
                    "fingerprint_id": finger_id,
                    "confidence": confidence
                }

        except Exception as e:
            logger.error("Fingerprint search error: %s", e)
            self._set_led("flashing", "red")
            time.sleep(1)
            self._set_led("off", "red")
            return None

    def enroll_fingerprint(self):
        """
        Enroll a new fingerprint (captures 2 samples).

        The LED will show:
            - Purple (breathing): Waiting for first finger
            - Purple (on): First capture done, remove finger
            - Blue (breathing): Waiting for second finger
            - Green (on): Enrollment success
            - Red (flashing): Enrollment failure

        Returns:
            int or None: New fingerprint template ID on success, None on failure.
        """
        if not self._connected:
            logger.error("Sensor not connected")
            return None

        try:
            # --- First capture ---
            self._set_led("breathing", "purple")
            logger.info("Place finger on sensor for first capture...")

            # Wait for finger
            timeout = 15
            start = time.time()
            while not self.sensor.readImage():
                if time.time() - start > timeout:
                    logger.error("Enrollment timeout on first capture")
                    self._set_led("off", "purple")
                    return None
                time.sleep(0.1)

            self.sensor.convertImage(0x01)
            self._set_led("on", "purple")
            logger.info("First capture done. Remove finger...")

            # Wait for finger removal
            time.sleep(1)
            while self.sensor.readImage():
                time.sleep(0.1)

            # --- Second capture ---
            self._set_led("breathing", "blue")
            logger.info("Place same finger again for second capture...")

            start = time.time()
            while not self.sensor.readImage():
                if time.time() - start > timeout:
                    logger.error("Enrollment timeout on second capture")
                    self._set_led("off", "blue")
                    return None
                time.sleep(0.1)

            self.sensor.convertImage(0x02)

            # Create model from 2 captures
            if self.sensor.createTemplate() == 0:
                logger.error("Fingerprint captures do not match")
                self._set_led("flashing", "red")
                time.sleep(1.5)
                self._set_led("off", "red")
                return None

            # Find next free position
            position = self._find_free_position()
            if position is None:
                logger.error("Fingerprint storage full")
                self._set_led("flashing", "red")
                time.sleep(1.5)
                self._set_led("off", "red")
                return None

            # Store template
            self.sensor.storeTemplate(position, 0x01)

            logger.info("Fingerprint enrolled at position %d", position)
            self._set_led("on", "green")
            time.sleep(1.5)
            self._set_led("off", "green")

            return position

        except Exception as e:
            logger.error("Fingerprint enrollment error: %s", e)
            self._set_led("flashing", "red")
            time.sleep(1)
            self._set_led("off", "red")
            return None

    def delete_fingerprint(self, position):
        """
        Delete a fingerprint template from the sensor.

        Args:
            position: Template position to delete.

        Returns:
            bool: True if deleted successfully.
        """
        if not self._connected:
            return False

        try:
            if self.sensor.deleteTemplate(position):
                logger.info("Fingerprint template %d deleted", position)
                return True
            return False
        except Exception as e:
            logger.error("Failed to delete fingerprint %d: %s", position, e)
            return False

    def download_template(self, position):
        """
        Download a template from the sensor for backup.

        Args:
            position: Template position to download.

        Returns:
            list or None: Template characteristics data.
        """
        if not self._connected:
            return None

        try:
            self.sensor.loadTemplate(position, 0x01)
            characteristics = self.sensor.downloadCharacteristics(0x01)
            logger.debug("Downloaded template from position %d", position)
            return characteristics
        except Exception as e:
            logger.error("Failed to download template %d: %s", position, e)
            return None

    def upload_template(self, position, characteristics):
        """
        Upload a template to the sensor (restore from backup).

        Args:
            position: Position to store the template.
            characteristics: Template data to upload.

        Returns:
            bool: True if uploaded successfully.
        """
        if not self._connected:
            return False

        try:
            self.sensor.uploadCharacteristics(0x01, characteristics)
            self.sensor.storeTemplate(position, 0x01)
            logger.info("Template uploaded to position %d", position)
            return True
        except Exception as e:
            logger.error("Failed to upload template to %d: %s", position, e)
            return False

    def get_template_count(self):
        """Get number of stored templates on sensor."""
        if not self._connected:
            return 0
        try:
            return self.sensor.getTemplateCount()
        except Exception:
            return 0

    def _find_free_position(self):
        """Find the next available template position on sensor."""
        try:
            # Get table of used positions
            table = self.sensor.getTemplateIndex(0)
            for i in range(len(table)):
                if table[i] is False:
                    return i
            # Check additional pages if needed
            for page in range(1, 4):
                try:
                    table = self.sensor.getTemplateIndex(page)
                    page_offset = page * len(table)
                    for i in range(len(table)):
                        if table[i] is False:
                            return page_offset + i
                except Exception:
                    break
            return None
        except Exception as e:
            logger.error("Error finding free position: %s", e)
            return None

    def _set_led(self, mode, color):
        """
        Set the R503 aura LED.

        Args:
            mode: 'breathing', 'flashing', 'on', 'off', 'gradual_on', 'gradual_off'.
            color: 'red', 'blue', 'purple', 'green', 'yellow', 'cyan', 'white'.
        """
        if not self._connected or self.sensor is None:
            return

        mode_code = LED_MODES.get(mode, 0x04)
        color_code = LED_COLORS.get(color, 0x02)

        try:
            # R503 LED control via packet
            # Some pyfingerprint versions support this directly;
            # otherwise we send a raw command
            if hasattr(self.sensor, 'setLED'):
                self.sensor.setLED(mode_code, color_code, 0x00, 0x00)
            else:
                # Construct the aura LED control packet manually
                self._send_led_command(mode_code, color_code)
        except Exception as e:
            logger.debug("LED control not supported or error: %s", e)

    def _send_led_command(self, control, color, speed=0x30, count=0x00):
        """
        Send raw aura LED control command to R503.

        Packet: Header(2) + Address(4) + PID(1) + Length(2) + InstrCode(1) +
                Control(1) + Speed(1) + Color(1) + Count(1) + Checksum(2)
        """
        try:
            # Use the underlying serial port if accessible
            if hasattr(self.sensor, '_serial'):
                packet = bytearray([
                    0xEF, 0x01,                         # Header
                    0xFF, 0xFF, 0xFF, 0xFF,             # Address
                    0x01,                                # PID (command)
                    0x00, 0x07,                          # Length
                    0x35,                                # Instruction code (AuraLedConfig)
                    control,                             # Control mode
                    speed,                               # Speed
                    color,                               # Color
                    count,                               # Count (0 = infinite)
                ])
                # Calculate checksum
                checksum = sum(packet[6:]) & 0xFFFF
                packet.append((checksum >> 8) & 0xFF)
                packet.append(checksum & 0xFF)

                self.sensor._serial.write(packet)
                time.sleep(0.1)
                # Read and discard response
                self.sensor._serial.read(self.sensor._serial.in_waiting)
        except Exception as e:
            logger.debug("Raw LED command failed: %s", e)


class SimulatedFingerprintModule:
    """Simulated fingerprint sensor for development without hardware."""

    def __init__(self, config):
        self._connected = False
        self._templates = {}
        self._next_id = 0
        logger.info("Using SIMULATED fingerprint sensor")

    def connect(self):
        self._connected = True
        logger.info("[SIM] Fingerprint sensor connected")
        return True

    def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected

    def search_fingerprint(self, timeout_seconds=10):
        logger.info("[SIM] Fingerprint search — returning no match")
        return None

    def enroll_fingerprint(self):
        fid = self._next_id
        self._next_id += 1
        self._templates[fid] = True
        logger.info("[SIM] Enrolled fingerprint ID=%d", fid)
        return fid

    def delete_fingerprint(self, position):
        self._templates.pop(position, None)
        return True

    def download_template(self, position):
        return [0] * 512  # Fake template

    def upload_template(self, position, characteristics):
        self._templates[position] = True
        return True

    def get_template_count(self):
        return len(self._templates)
