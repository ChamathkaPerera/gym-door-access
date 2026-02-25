#!/usr/bin/env python3
"""
Export Logs CLI — Export access logs to CSV file.

Usage:
    python scripts/export_logs.py
    python scripts/export_logs.py --from 2026-01-01 --to 2026-02-17
    python scripts/export_logs.py --output /home/pi/logs_export.csv
    python scripts/export_logs.py --summary
    python scripts/export_logs.py --recent 20
"""

import os
import sys
import argparse
import yaml
from datetime import datetime

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from modules.database import Database
from modules.logger import setup_logging, AccessLogger


def load_config():
    """Load configuration."""
    config_path = os.path.join(PROJECT_ROOT, "config/settings.yaml")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="Lumora Door Access — Export Access Logs")
    parser.add_argument("--from", dest="start_date", type=str,
                        help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="end_date", type=str,
                        help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", "-o", type=str,
                        default=None,
                        help="Output CSV file path")
    parser.add_argument("--summary", action="store_true",
                        help="Show daily summary instead of export")
    parser.add_argument("--recent", type=int, default=None,
                        help="Show N most recent log entries")
    args = parser.parse_args()

    os.chdir(PROJECT_ROOT)
    config = load_config()

    # Initialize
    system_config = config.get("system", {})
    log_config = {**config.get("logging", {}), "log_level": system_config.get("log_level", "INFO")}
    setup_logging(log_config)

    db_config = config.get("database", {})
    db_path = os.path.join(PROJECT_ROOT, db_config.get("path", "data/door_access.db"))
    db = Database(db_path)
    access_logger = AccessLogger(db, config.get("logging", {}))

    # Daily summary
    if args.summary:
        date = datetime.now().strftime("%Y-%m-%d")
        if args.start_date:
            date = args.start_date

        summary = access_logger.get_daily_summary(date)
        print(f"\n📊 Daily Summary for {summary['date']}")
        print(f"   Total events: {summary['total_events']}")
        print(f"   Granted: {summary['granted']}")
        print(f"   Denied: {summary['denied']}")
        print(f"   Face entries: {summary['face_entries']}")
        print(f"   Fingerprint entries: {summary['fingerprint_entries']}")
        print(f"   Button events: {summary['button_events']}")
        return

    # Recent logs
    if args.recent:
        logs = db.get_recent_logs(args.recent)
        if not logs:
            print("No access logs found.")
            return

        print(f"\n{'Time':<22} {'User':<15} {'Method':<12} {'Dir':<5} {'Status':<10}")
        print("-" * 66)
        for log in logs:
            ts = log["timestamp"][:19]
            name = (log.get("user_name") or "Unknown")[:14]
            method = log["method"][:11]
            direction = log.get("direction", "?")
            status = log["status"]
            print(f"{ts:<22} {name:<15} {method:<12} {direction:<5} {status:<10}")

        print(f"\nShowing {len(logs)} entries")
        return

    # CSV export
    if args.output:
        output_path = args.output
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(PROJECT_ROOT, f"data/logs/export_{timestamp}.csv")

    start_date = None
    end_date = None
    if args.start_date:
        start_date = f"{args.start_date}T00:00:00"
    if args.end_date:
        end_date = f"{args.end_date}T23:59:59"

    count = access_logger.export_to_csv(output_path, start_date, end_date)
    print(f"\n✅ Exported {count} records to: {output_path}")


if __name__ == "__main__":
    main()
