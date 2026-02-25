#!/usr/bin/env python3
"""
Enroll User CLI — Register new users with face + fingerprint.

Usage:
    python scripts/enroll_user.py --name "John Doe"
    python scripts/enroll_user.py --name "Jane" --face-only
    python scripts/enroll_user.py --name "Bob" --fingerprint-only
    python scripts/enroll_user.py --list
    python scripts/enroll_user.py --delete 3
"""

import os
import sys
import argparse
import yaml

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from modules.database import Database
from modules.face_recognition_module import FaceRecognitionModule
from modules.fingerprint_module import FingerprintModule, SimulatedFingerprintModule
from modules.user_manager import UserManager
from modules.logger import setup_logging


def load_config():
    """Load configuration."""
    config_path = os.path.join(PROJECT_ROOT, "config/settings.yaml")
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def init_modules(config):
    """Initialize modules needed for enrollment."""
    system_config = config.get("system", {})
    simulate = system_config.get("simulate_gpio", False)

    # Logging
    log_config = {
        **config.get("logging", {}),
        "log_level": system_config.get("log_level", "INFO"),
    }
    setup_logging(log_config)

    # Database
    db_config = config.get("database", {})
    db_path = os.path.join(PROJECT_ROOT, db_config.get("path", "data/door_access.db"))
    db = Database(db_path)

    # Face Recognition
    face = FaceRecognitionModule(config.get("face_recognition", {}))
    users = db.get_all_users()
    face.load_known_faces(users)

    # Fingerprint
    if simulate:
        fingerprint = SimulatedFingerprintModule(config.get("fingerprint", {}))
    else:
        fingerprint = FingerprintModule(config.get("fingerprint", {}))
    fingerprint.connect()

    # User Manager
    user_manager = UserManager(db, face, fingerprint)

    return db, face, fingerprint, user_manager


def enroll(args, config):
    """Enroll a new user."""
    db, face, fingerprint, user_manager = init_modules(config)

    enroll_face = not args.fingerprint_only
    enroll_fp = not args.face_only

    camera = None
    if enroll_face:
        cam_config = config.get("camera", {})
        camera = face.open_camera(
            device_index=cam_config.get("device_index", 0),
            width=cam_config.get("resolution_width", 640),
            height=cam_config.get("resolution_height", 480),
        )
        if camera is None:
            print("ERROR: Could not open camera. Use --fingerprint-only to skip face enrollment.")
            if not enroll_fp:
                sys.exit(1)
            enroll_face = False

    print(f"\n{'='*50}")
    print(f"  Enrolling User: {args.name}")
    print(f"  Face: {'Yes' if enroll_face else 'No'}")
    print(f"  Fingerprint: {'Yes' if enroll_fp else 'No'}")
    print(f"{'='*50}\n")

    if enroll_face:
        print("📷 Look at the camera. Multiple photos will be captured.")
        print("   Move your head slightly between captures for better accuracy.\n")

    if enroll_fp:
        print("👆 You will be asked to place your finger on the sensor twice.\n")

    input("Press ENTER to begin enrollment...")

    result = user_manager.register_user(
        name=args.name,
        camera=camera,
        enroll_face=enroll_face,
        enroll_fingerprint=enroll_fp
    )

    if camera:
        face.close_camera(camera)

    if result:
        print(f"\n✅ User enrolled successfully!")
        print(f"   ID: {result['user_id']}")
        print(f"   Name: {result['name']}")
        print(f"   Face enrolled: {'✅' if result['has_face'] else '❌'}")
        print(f"   Fingerprint enrolled: {'✅' if result['has_fingerprint'] else '❌'}")
        if result['has_fingerprint']:
            print(f"   Fingerprint sensor ID: {result['fingerprint_id']}")
    else:
        print("\n❌ Enrollment failed. Check logs for details.")
        sys.exit(1)


def list_users(config):
    """List all registered users."""
    db, _, _, user_manager = init_modules(config)
    users = user_manager.list_users()

    if not users:
        print("No users registered.")
        return

    print(f"\n{'ID':<6} {'Name':<20} {'Face':<8} {'Finger ID':<12} {'Registered'}")
    print("-" * 70)

    for user in users:
        face_icon = "✅" if user["has_face"] else "❌"
        fp_id = user["fingerprint_id"] if user["fingerprint_id"] >= 0 else "N/A"
        reg_date = user.get("registered_at", "Unknown")[:19]
        print(f"{user['id']:<6} {user['name']:<20} {face_icon:<8} {str(fp_id):<12} {reg_date}")

    print(f"\nTotal: {len(users)} users")


def delete_user(args, config):
    """Delete a user."""
    db, face, fingerprint, user_manager = init_modules(config)
    user = db.get_user(args.delete)

    if not user:
        print(f"User ID {args.delete} not found.")
        sys.exit(1)

    confirm = input(f"Delete user '{user['name']}' (ID={user['id']})? (y/N): ")
    if confirm.lower() != "y":
        print("Cancelled.")
        return

    user_manager.delete_user(args.delete)
    print(f"✅ User '{user['name']}' deleted.")


def main():
    parser = argparse.ArgumentParser(description="Lumora Door Access — User Enrollment")
    parser.add_argument("--name", type=str, help="Name of the user to enroll")
    parser.add_argument("--face-only", action="store_true", help="Only enroll face (skip fingerprint)")
    parser.add_argument("--fingerprint-only", action="store_true", help="Only enroll fingerprint (skip face)")
    parser.add_argument("--list", action="store_true", help="List all registered users")
    parser.add_argument("--delete", type=int, help="Delete a user by ID")
    parser.add_argument("--stats", action="store_true", help="Show system statistics")

    args = parser.parse_args()

    os.chdir(PROJECT_ROOT)
    os.makedirs("data/faces", exist_ok=True)
    os.makedirs("data/logs", exist_ok=True)

    config = load_config()

    if args.list:
        list_users(config)
    elif args.delete:
        delete_user(args, config)
    elif args.stats:
        db, _, _, _ = init_modules(config)
        stats = db.get_stats()
        print(f"\n📊 System Statistics")
        print(f"   Users: {stats['total_users']}")
        print(f"   Total logs: {stats['total_logs']}")
        print(f"   Today's accesses: {stats['today_access']}")
        print(f"   Pending sync: {stats['pending_sync']}")
    elif args.name:
        enroll(args, config)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
