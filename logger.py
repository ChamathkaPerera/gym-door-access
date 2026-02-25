"""
Logger Module — Local access logging with CSV export capability.

All access events are stored in SQLite (via Database module) and can be
exported to CSV for offline review. Also configures the Python logging system.
"""

import os
import csv
import logging
import logging.handlers
from datetime import datetime


def setup_logging(config):
    """
    Configure the Python logging system with file and console handlers.

    Args:
        config: dict from settings.yaml 'logging' section.
    """
    log_file = config.get("log_file", "data/logs/system.log")
    max_size_mb = config.get("max_log_size_mb", 10)
    backup_count = config.get("backup_count", 5)
    log_level = config.get("log_level", "INFO")

    # Create log directory
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))

    # Format
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_size_mb * 1024 * 1024,
        backupCount=backup_count
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)

    logging.info("Logging initialized — level=%s, file=%s", log_level, log_file)


class AccessLogger:
    """High-level access logging with CSV export."""

    def __init__(self, database, config):
        """
        Args:
            database: Database instance for storing logs.
            config: dict from settings.yaml 'logging' section.
        """
        self.db = database
        self.save_images = config.get("save_face_images", True)
        self.images_dir = config.get("face_images_dir", "data/faces")
        self.access_log_file = config.get("access_log_file", "data/logs/access.log")

        # Set up a dedicated access log file
        self._access_logger = logging.getLogger("access")
        access_dir = os.path.dirname(self.access_log_file)
        if access_dir:
            os.makedirs(access_dir, exist_ok=True)

        handler = logging.handlers.RotatingFileHandler(
            self.access_log_file, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        self._access_logger.addHandler(handler)
        self._access_logger.setLevel(logging.INFO)

    def log_access_granted(self, user_id, user_name, method, direction="in",
                           confidence=0.0, image_path=None):
        """
        Log a successful access event.

        Args:
            user_id: ID of the recognized user.
            user_name: Name of the user.
            method: 'face' or 'fingerprint'.
            direction: 'in' or 'out'.
            confidence: Match confidence (0.0-1.0).
            image_path: Path to captured face image.
        """
        log_id = self.db.log_access(
            user_id=user_id,
            user_name=user_name,
            method=method,
            direction=direction,
            status="granted",
            image_path=image_path,
            confidence=confidence
        )
        self._access_logger.info(
            "GRANTED | %s | %s | %s | confidence=%.3f | log_id=%d",
            user_name, method, direction, confidence, log_id
        )

    def log_access_denied(self, method, direction="in", image_path=None):
        """Log a failed access attempt."""
        log_id = self.db.log_access(
            user_id=None,
            user_name="Unknown",
            method=method,
            direction=direction,
            status="denied",
            image_path=image_path
        )
        self._access_logger.info(
            "DENIED  | Unknown | %s | %s | log_id=%d", method, direction, log_id
        )

    def log_button_event(self, button_type, action="pressed"):
        """
        Log a button event.

        Args:
            button_type: 'outside' or 'inside'.
            action: 'pressed', 'door_opened', etc.
        """
        method = f"{button_type}_button"
        direction = "out" if button_type == "inside" else "in"
        status = "alert" if button_type == "outside" else "granted"

        self.db.log_access(
            user_id=None,
            user_name="Button",
            method=method,
            direction=direction,
            status=status
        )
        self._access_logger.info(
            "BUTTON  | %s | %s | %s", button_type, action, direction
        )

    def export_to_csv(self, output_path, start_date=None, end_date=None):
        """
        Export access logs to a CSV file.

        Args:
            output_path: Path for the CSV output file.
            start_date: Filter start date (ISO format string).
            end_date: Filter end date (ISO format string).

        Returns:
            int: Number of records exported.
        """
        if start_date and end_date:
            logs = self.db.get_logs_by_date(start_date, end_date)
        else:
            logs = self.db.get_recent_logs(limit=10000)

        if not logs:
            logging.info("No logs to export")
            return 0

        # Ensure output directory exists
        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        fieldnames = [
            "id", "user_id", "user_name", "method", "direction",
            "status", "timestamp", "confidence", "image_path"
        ]

        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for log in logs:
                writer.writerow(log)

        logging.info("Exported %d access logs to %s", len(logs), output_path)
        return len(logs)

    def get_daily_summary(self, date=None):
        """
        Get a summary of access events for a given date.

        Args:
            date: Date string (YYYY-MM-DD). Defaults to today.

        Returns:
            dict: Summary statistics.
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        start = f"{date}T00:00:00"
        end = f"{date}T23:59:59"
        logs = self.db.get_logs_by_date(start, end)

        summary = {
            "date": date,
            "total_events": len(logs),
            "granted": sum(1 for l in logs if l["status"] == "granted"),
            "denied": sum(1 for l in logs if l["status"] == "denied"),
            "face_entries": sum(1 for l in logs if l["method"] == "face"),
            "fingerprint_entries": sum(1 for l in logs if l["method"] == "fingerprint"),
            "button_events": sum(1 for l in logs if "button" in l["method"]),
        }

        return summary
