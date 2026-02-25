# Lumora Door Access System

Face recognition + fingerprint door access system for Raspberry Pi 3B+. Works **fully offline** with local SQLite storage and optional Firebase cloud sync.

## Features

- **Face Recognition** — HOG-based detection with 128-d face embeddings (dlib)
- **Fingerprint Auth** — R503 sensor via UART with LED status feedback
- **Dual Biometric** — Face + fingerprint linked per user, fingerprint fallback on face failure
- **Offline-First** — All recognition runs locally; syncs to Firebase when internet is available
- **Auto-Start** — systemd service starts on boot after power loss
- **PIR Motion Detection** — System wakes from idle on motion, activates camera + flash
- **Access Logging** — SQLite logs with CSV export + Firebase sync
- **Inside/Outside Buttons** — Buzzer alert for visitors, relay unlock for exit

## Hardware

| Component | Connection | Purpose |
|-----------|-----------|---------|
| RPi 3B+ | — | Main controller |
| USB Camera | USB | Face capture |
| R503 Fingerprint | UART (GPIO14/15) | Fingerprint auth |
| PIR Sensor | GPIO 17 | Motion detection |
| Relay Module | GPIO 27 | Door lock control |
| Buzzer | GPIO 22 | Audio alerts |
| Outside Button | GPIO 23 | Visitor notification |
| Inside Button | GPIO 24 | Manual door release |
| Flash LED | GPIO 25 | Camera illumination |

## Wiring Diagram

```
RPi 3B+ GPIO (BCM)
├── GPIO 14 (TX) ──→ R503 RX
├── GPIO 15 (RX) ──→ R503 TX
├── GPIO 17 ───────→ PIR OUT
├── GPIO 22 ───────→ Buzzer (+) ──→ GND
├── GPIO 23 ───────→ Outside Button ──→ GND (pull-up)
├── GPIO 24 ───────→ Inside Button ──→ GND (pull-up)
├── GPIO 25 ───────→ MOSFET Gate ──→ Flash LED
├── GPIO 27 ───────→ Relay IN
├── 3.3V ──────────→ R503 VCC, PIR VCC
├── 5V ────────────→ Relay VCC
└── GND ───────────→ Common GND
```

## Quick Start

### 1. Install on Raspberry Pi
```bash
git clone <repo-url> ~/door-access
cd ~/door-access
sudo bash setup.sh
```

### 2. Configure
Edit `config/settings.yaml` with your GPIO pins and Firebase credentials.

### 3. Enroll a User
```bash
cd ~/door-access && source venv/bin/activate
python scripts/enroll_user.py --name "Your Name"
```

### 4. Run
```bash
# Test mode
python main.py

# Production (auto-starts on boot)
sudo systemctl start door-access
sudo systemctl status door-access
```

## Project Structure

```
├── config/
│   └── settings.yaml           # Configuration (GPIO, camera, Firebase)
├── modules/
│   ├── database.py             # SQLite (users, logs, sync queue)
│   ├── face_recognition_module.py  # Face detection + matching
│   ├── fingerprint_module.py   # R503 UART communication
│   ├── gpio_controller.py      # Relay, buzzer, PIR, buttons, flash
│   ├── user_manager.py         # Enrollment + identification
│   ├── logger.py               # Access logging + CSV export
│   └── firebase_sync.py        # Background cloud sync
├── scripts/
│   ├── enroll_user.py          # CLI: user enrollment
│   └── export_logs.py          # CLI: export logs to CSV
├── services/
│   └── door-access.service     # systemd auto-start service
├── main.py                     # Entry point (state machine)
├── setup.sh                    # Installation script
└── requirements.txt            # Python dependencies
```

## System Flow

```
IDLE ──(PIR motion)──→ DETECTING ──(face match)──→ DOOR OPEN ──→ IDLE
                           │
                      (face fail)
                           │
                      FINGERPRINT ──(match)──→ DOOR OPEN ──→ IDLE
                           │
                        (fail)
                           │
                        DENIED ──→ IDLE

INSIDE BUTTON ──→ DOOR OPEN ──→ IDLE
OUTSIDE BUTTON ──→ BUZZER ALERT ──→ IDLE
```

## CLI Commands

```bash
# Enroll user (face + fingerprint)
python scripts/enroll_user.py --name "John"

# Enroll face only
python scripts/enroll_user.py --name "Jane" --face-only

# List users
python scripts/enroll_user.py --list

# Delete user
python scripts/enroll_user.py --delete 3

# Export logs to CSV
python scripts/export_logs.py --from 2026-01-01 --to 2026-02-17

# View recent logs
python scripts/export_logs.py --recent 20

# Daily summary
python scripts/export_logs.py --summary
```

## Development (Without Hardware)

Set `simulate_gpio: true` in `config/settings.yaml` to run on a laptop without GPIO/sensors. All hardware interactions are logged to console instead.

## License

MIT
