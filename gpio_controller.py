"""
GPIO Controller — Manage relay, buzzer, PIR sensor, buttons, and flash LED.

Supports both real RPi.GPIO and a simulated mode for development.
"""

import logging
import time
import threading

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    logger.warning("RPi.GPIO not available. Using simulation mode.")


class GPIOController:
    """Control all GPIO-connected hardware: relay, buzzer, PIR, buttons, flash."""

    def __init__(self, config, simulate=False):
        """
        Args:
            config: dict from settings.yaml 'gpio' section.
            simulate: Force simulation mode (True for dev on non-RPi).
        """
        self.simulate = simulate or not GPIO_AVAILABLE

        # Pin assignments
        self.PIN_PIR = config.get("pir_sensor", 17)
        self.PIN_RELAY = config.get("relay", 27)
        self.PIN_BUZZER = config.get("buzzer", 22)
        self.PIN_OUTSIDE_BTN = config.get("outside_button", 23)
        self.PIN_INSIDE_BTN = config.get("inside_button", 24)
        self.PIN_FLASH = config.get("flash_led", 25)

        # Timing
        self.relay_duration = config.get("relay_active_duration", 5)
        self.buzzer_duration = config.get("buzzer_alert_duration", 3)
        self.debounce_time = config.get("debounce_time", 300)

        # State
        self._relay_active = False
        self._flash_on = False
        self._relay_timer = None

        if not self.simulate:
            self._setup_gpio()
        else:
            logger.info("[SIM] GPIO controller in simulation mode")

    def _setup_gpio(self):
        """Initialize GPIO pins."""
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        # Outputs
        GPIO.setup(self.PIN_RELAY, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(self.PIN_BUZZER, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(self.PIN_FLASH, GPIO.OUT, initial=GPIO.LOW)

        # Inputs with pull-up resistors
        GPIO.setup(self.PIN_PIR, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        GPIO.setup(self.PIN_OUTSIDE_BTN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self.PIN_INSIDE_BTN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        logger.info(
            "GPIO initialized — PIR:%d, Relay:%d, Buzzer:%d, OutBtn:%d, InBtn:%d, Flash:%d",
            self.PIN_PIR, self.PIN_RELAY, self.PIN_BUZZER,
            self.PIN_OUTSIDE_BTN, self.PIN_INSIDE_BTN, self.PIN_FLASH
        )

    # -------------------------------------------------------------------------
    # Relay (Door Lock)
    # -------------------------------------------------------------------------

    def activate_relay(self, duration=None):
        """
        Activate relay to unlock door for a specified duration.

        Args:
            duration: Seconds to keep relay active (default from config).
        """
        if duration is None:
            duration = self.relay_duration

        if self._relay_active:
            logger.debug("Relay already active, extending duration")
            if self._relay_timer:
                self._relay_timer.cancel()
        else:
            if self.simulate:
                logger.info("[SIM] RELAY ON — Door unlocked")
            else:
                GPIO.output(self.PIN_RELAY, GPIO.HIGH)
            self._relay_active = True
            logger.info("Relay activated for %d seconds", duration)

        # Auto-deactivate after duration
        self._relay_timer = threading.Timer(duration, self.deactivate_relay)
        self._relay_timer.daemon = True
        self._relay_timer.start()

    def deactivate_relay(self):
        """Deactivate relay (lock door)."""
        if self.simulate:
            logger.info("[SIM] RELAY OFF — Door locked")
        else:
            GPIO.output(self.PIN_RELAY, GPIO.LOW)
        self._relay_active = False
        logger.info("Relay deactivated — door locked")

    def is_relay_active(self):
        """Check if relay is currently active."""
        return self._relay_active

    # -------------------------------------------------------------------------
    # Buzzer
    # -------------------------------------------------------------------------

    def ring_buzzer(self, pattern="alert", duration=None):
        """
        Ring the buzzer with a specified pattern.

        Args:
            pattern: 'short' (single beep), 'alert' (3 beeps),
                     'long' (continuous), 'sos', 'success' (2 short).
            duration: Override total duration for 'long' pattern.
        """
        if duration is None:
            duration = self.buzzer_duration

        thread = threading.Thread(
            target=self._buzzer_pattern,
            args=(pattern, duration),
            daemon=True
        )
        thread.start()

    def _buzzer_pattern(self, pattern, duration):
        """Execute buzzer pattern in a separate thread."""
        patterns = {
            "short": [(0.2, 0)],
            "success": [(0.1, 0.1), (0.1, 0)],
            "alert": [(0.3, 0.2), (0.3, 0.2), (0.3, 0)],
            "denied": [(0.5, 0.2), (0.5, 0)],
            "long": [(duration, 0)],
            "sos": [
                (0.1, 0.1), (0.1, 0.1), (0.1, 0.3),  # S
                (0.3, 0.1), (0.3, 0.1), (0.3, 0.3),  # O
                (0.1, 0.1), (0.1, 0.1), (0.1, 0),     # S
            ],
        }

        beeps = patterns.get(pattern, patterns["alert"])

        for on_time, off_time in beeps:
            self._buzzer_on()
            time.sleep(on_time)
            self._buzzer_off()
            if off_time > 0:
                time.sleep(off_time)

    def _buzzer_on(self):
        if self.simulate:
            logger.debug("[SIM] BUZZER ON")
        else:
            GPIO.output(self.PIN_BUZZER, GPIO.HIGH)

    def _buzzer_off(self):
        if self.simulate:
            logger.debug("[SIM] BUZZER OFF")
        else:
            GPIO.output(self.PIN_BUZZER, GPIO.LOW)

    # -------------------------------------------------------------------------
    # PIR Sensor
    # -------------------------------------------------------------------------

    def read_pir(self):
        """
        Read PIR motion sensor state.

        Returns:
            bool: True if motion detected.
        """
        if self.simulate:
            return False

        return GPIO.input(self.PIN_PIR) == GPIO.HIGH

    # -------------------------------------------------------------------------
    # Buttons
    # -------------------------------------------------------------------------

    def setup_button_callbacks(self, outside_callback=None, inside_callback=None):
        """
        Set up interrupt-driven button callbacks with debouncing.

        Args:
            outside_callback: Function called when outside button is pressed.
            inside_callback: Function called when inside button is pressed.
        """
        if self.simulate:
            logger.info("[SIM] Button callbacks registered (simulation mode)")
            self._outside_callback = outside_callback
            self._inside_callback = inside_callback
            return

        if outside_callback:
            GPIO.add_event_detect(
                self.PIN_OUTSIDE_BTN,
                GPIO.FALLING,
                callback=lambda ch: outside_callback(),
                bouncetime=self.debounce_time
            )
            logger.info("Outside button callback registered on GPIO %d", self.PIN_OUTSIDE_BTN)

        if inside_callback:
            GPIO.add_event_detect(
                self.PIN_INSIDE_BTN,
                GPIO.FALLING,
                callback=lambda ch: inside_callback(),
                bouncetime=self.debounce_time
            )
            logger.info("Inside button callback registered on GPIO %d", self.PIN_INSIDE_BTN)

    def read_outside_button(self):
        """Read outside button state (active low)."""
        if self.simulate:
            return False
        return GPIO.input(self.PIN_OUTSIDE_BTN) == GPIO.LOW

    def read_inside_button(self):
        """Read inside button state (active low)."""
        if self.simulate:
            return False
        return GPIO.input(self.PIN_INSIDE_BTN) == GPIO.LOW

    # -------------------------------------------------------------------------
    # Flash LED
    # -------------------------------------------------------------------------

    def flash_on(self):
        """Turn on the camera flash LED."""
        if self.simulate:
            logger.info("[SIM] FLASH LED ON")
        else:
            GPIO.output(self.PIN_FLASH, GPIO.HIGH)
        self._flash_on = True

    def flash_off(self):
        """Turn off the camera flash LED."""
        if self.simulate:
            logger.info("[SIM] FLASH LED OFF")
        else:
            GPIO.output(self.PIN_FLASH, GPIO.LOW)
        self._flash_on = False

    def is_flash_on(self):
        """Check if flash LED is currently on."""
        return self._flash_on

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------

    def cleanup(self):
        """Clean up GPIO resources. Call on program exit."""
        if self._relay_timer:
            self._relay_timer.cancel()

        if not self.simulate:
            GPIO.output(self.PIN_RELAY, GPIO.LOW)
            GPIO.output(self.PIN_BUZZER, GPIO.LOW)
            GPIO.output(self.PIN_FLASH, GPIO.LOW)
            GPIO.cleanup()

        logger.info("GPIO cleaned up")
