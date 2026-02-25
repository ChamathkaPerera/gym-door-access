#!/bin/bash
# =============================================================================
# Lumora Door Access System — Installation Script
# Run on Raspberry Pi 3B+ (Raspbian/Raspberry Pi OS)
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/home/pi/door-access"
SERVICE_NAME="door-access"

echo "============================================"
echo "  Lumora Door Access System — Installer"
echo "============================================"
echo ""

# --- Check if running as root or with sudo ---
if [ "$EUID" -ne 0 ]; then
    echo "Please run with sudo: sudo bash setup.sh"
    exit 1
fi

ACTUAL_USER="${SUDO_USER:-pi}"
ACTUAL_HOME=$(eval echo "~$ACTUAL_USER")

echo "Installing for user: $ACTUAL_USER"
echo "Home directory: $ACTUAL_HOME"
echo ""

# --- Step 1: System Dependencies ---
echo "[1/7] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    python3-pip \
    python3-venv \
    python3-dev \
    cmake \
    build-essential \
    libopenblas-dev \
    liblapack-dev \
    libjpeg-dev \
    libpng-dev \
    libatlas-base-dev \
    libhdf5-dev \
    libhdf5-serial-dev \
    libffi-dev \
    libssl-dev \
    v4l-utils \
    git

echo "  ✅ System dependencies installed"

# --- Step 2: Enable UART for Fingerprint Sensor ---
echo "[2/7] Configuring UART for fingerprint sensor..."

# Enable UART in config.txt
if ! grep -q "enable_uart=1" /boot/config.txt 2>/dev/null && \
   ! grep -q "enable_uart=1" /boot/firmware/config.txt 2>/dev/null; then
    CONFIG_FILE="/boot/config.txt"
    [ -f "/boot/firmware/config.txt" ] && CONFIG_FILE="/boot/firmware/config.txt"
    echo "" >> "$CONFIG_FILE"
    echo "# Enable UART for fingerprint sensor" >> "$CONFIG_FILE"
    echo "enable_uart=1" >> "$CONFIG_FILE"
    echo "  ⚠️  UART enabled — reboot required after setup"
fi

# Disable serial console (frees up /dev/ttyS0)
systemctl disable serial-getty@ttyS0.service 2>/dev/null || true

echo "  ✅ UART configured"

# --- Step 3: Copy project files ---
echo "[3/7] Setting up project directory..."

if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    mkdir -p "$INSTALL_DIR"
    cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR"/
    chown -R "$ACTUAL_USER:$ACTUAL_USER" "$INSTALL_DIR"
fi

echo "  ✅ Project files copied to $INSTALL_DIR"

# --- Step 4: Python Virtual Environment ---
echo "[4/7] Creating Python virtual environment..."

cd "$INSTALL_DIR"
sudo -u "$ACTUAL_USER" python3 -m venv venv
source venv/bin/activate

echo "  ✅ Virtual environment created"

# --- Step 5: Install Python Dependencies ---
echo "[5/7] Installing Python packages (this may take 10-20 minutes on RPi)..."

pip install --upgrade pip setuptools wheel

# Install numpy first (dlib dependency)
pip install numpy

# Install dlib (takes a while on RPi — ~15 minutes)
echo "  ⏳ Installing dlib (this is the slow one — be patient)..."
pip install dlib

# Install remaining requirements
pip install -r requirements.txt

# Install RPi.GPIO
pip install RPi.GPIO

echo "  ✅ Python packages installed"

# --- Step 6: Create data directories ---
echo "[6/7] Creating data directories..."

sudo -u "$ACTUAL_USER" mkdir -p "$INSTALL_DIR/data/faces"
sudo -u "$ACTUAL_USER" mkdir -p "$INSTALL_DIR/data/logs"

echo "  ✅ Data directories created"

# --- Step 7: Install systemd service ---
echo "[7/7] Setting up systemd service..."

# Update service file with correct paths
sed -i "s|/home/pi/door-access|$INSTALL_DIR|g" "$INSTALL_DIR/services/door-access.service"
sed -i "s|User=pi|User=$ACTUAL_USER|g" "$INSTALL_DIR/services/door-access.service"
sed -i "s|Group=pi|Group=$ACTUAL_USER|g" "$INSTALL_DIR/services/door-access.service"

cp "$INSTALL_DIR/services/door-access.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo "  ✅ systemd service installed and enabled"

# --- Done ---
echo ""
echo "============================================"
echo "  ✅ Installation Complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Add your Firebase credentials:"
echo "     cp your_credentials.json $INSTALL_DIR/config/firebase_credentials.json"
echo ""
echo "  2. Update Firebase config in:"
echo "     $INSTALL_DIR/config/settings.yaml"
echo ""
echo "  3. Enroll your first user:"
echo "     cd $INSTALL_DIR"
echo "     source venv/bin/activate"
echo "     python scripts/enroll_user.py --name \"Your Name\""
echo ""
echo "  4. Test the system:"
echo "     python main.py"
echo ""
echo "  5. Start the service:"
echo "     sudo systemctl start $SERVICE_NAME"
echo "     sudo systemctl status $SERVICE_NAME"
echo ""
echo "  6. Reboot to apply UART changes:"
echo "     sudo reboot"
echo ""
