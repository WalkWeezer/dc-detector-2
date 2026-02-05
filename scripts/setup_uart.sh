#!/usr/bin/env bash
# DC-Detector v0.2 — UART setup for Raspberry Pi 5
#
# Enables the two UARTs needed by DC-Detector:
#   UART0 (/dev/ttyAMA0, GPIO14 TX / GPIO15 RX) — LoRa / ESP32
#   UART3 (/dev/ttyAMA3, GPIO4  TX / GPIO5  RX) — MAVLink flight controller
#
# Bluetooth is moved off UART0 (disabled) so LoRa can use it.
#
# Run once:  sudo bash scripts/setup_uart.sh
# Then reboot: sudo reboot

set -e

BOOT_CONFIG="/boot/firmware/config.txt"

# Fallback for older Pi OS
if [ ! -f "$BOOT_CONFIG" ]; then
    BOOT_CONFIG="/boot/config.txt"
fi

if [ ! -f "$BOOT_CONFIG" ]; then
    echo "ERROR: Cannot find boot config at /boot/firmware/config.txt or /boot/config.txt"
    exit 1
fi

echo "============================================================"
echo " DC-Detector — UART setup for Raspberry Pi 5"
echo "============================================================"
echo ""
echo " Config file: $BOOT_CONFIG"
echo ""
echo " Will enable:"
echo "   UART0 /dev/ttyAMA0 (GPIO14/15) — LoRa / ESP32"
echo "   UART3 /dev/ttyAMA3 (GPIO4/5)   — MAVLink FC"
echo "   Bluetooth will be DISABLED to free UART0"
echo ""

# ---- Check if already configured ----
if grep -q "# DC-Detector UART" "$BOOT_CONFIG" 2>/dev/null; then
    echo "DC-Detector UART block already exists in $BOOT_CONFIG"
    echo "Remove the existing block first if you want to reconfigure."
    echo ""
    grep -A 10 "# DC-Detector UART" "$BOOT_CONFIG"
    exit 0
fi

# ---- Backup ----
BACKUP="${BOOT_CONFIG}.bak.$(date +%Y%m%d_%H%M%S)"
cp "$BOOT_CONFIG" "$BACKUP"
echo "Backup saved: $BACKUP"

# ---- Append UART config ----
cat >> "$BOOT_CONFIG" <<'EOF'

# DC-Detector UART configuration
# UART0 (GPIO14/15) for LoRa/ESP32
enable_uart=1
dtparam=uart0=on
# Disable Bluetooth to free UART0
dtoverlay=disable-bt
# UART3 (GPIO4/5) for MAVLink flight controller
dtoverlay=uart3-pi5
EOF

echo ""
echo "UART configuration added to $BOOT_CONFIG"

# ---- Disable Bluetooth service (hciuart) ----
systemctl disable hciuart 2>/dev/null || true
echo "Bluetooth hciuart service disabled"

# ---- Add user to dialout group ----
CURRENT_USER="${SUDO_USER:-$USER}"
if id -nG "$CURRENT_USER" | grep -qw dialout; then
    echo "User '$CURRENT_USER' is already in dialout group"
else
    usermod -aG dialout "$CURRENT_USER"
    echo "User '$CURRENT_USER' added to dialout group"
fi

echo ""
echo "============================================================"
echo " UART setup complete!"
echo ""
echo " After reboot, verify with:"
echo "   ls -la /dev/ttyAMA0   # LoRa (GPIO14/15)"
echo "   ls -la /dev/ttyAMA3   # MAVLink (GPIO4/5)"
echo ""
echo " Test with:"
echo "   python tools/check_uart.py --port /dev/ttyAMA0 --listen 5"
echo "   python tools/check_uart.py --port /dev/ttyAMA3 --listen 5"
echo ""
echo " REBOOT NOW:  sudo reboot"
echo "============================================================"
