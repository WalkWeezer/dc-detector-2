#!/usr/bin/env bash
# DC-Detector v0.2 — UART setup for Raspberry Pi 5
#
# Enables the two UARTs needed by DC-Detector:
#   UART0 (/dev/ttyAMA0, GPIO14 TX / GPIO15 RX) — LoRa / ESP32
#   UART3 (/dev/ttyAMA3, GPIO4  TX / GPIO5  RX) — MAVLink flight controller
#
# Also:
#   - Disables serial console (getty) on UART0 so LoRa can use it
#   - Removes console=serial0 from kernel cmdline
#   - Creates udev rule for persistent permissions
#   - Adds user to dialout group
#   - Bluetooth is NOT affected (Pi 5 uses separate internal UART for BT)
#
# Run once:  sudo bash scripts/setup_uart.sh
# Then reboot: sudo reboot

set -e

BOOT_CONFIG="/boot/firmware/config.txt"
CMDLINE="/boot/firmware/cmdline.txt"

# Fallback for older Pi OS
if [ ! -f "$BOOT_CONFIG" ]; then
    BOOT_CONFIG="/boot/config.txt"
    CMDLINE="/boot/cmdline.txt"
fi

if [ ! -f "$BOOT_CONFIG" ]; then
    echo "ERROR: Cannot find boot config at /boot/firmware/config.txt or /boot/config.txt"
    exit 1
fi

echo "============================================================"
echo " DC-Detector — UART setup for Raspberry Pi 5"
echo "============================================================"
echo ""
echo " Will configure:"
echo "   UART0 /dev/ttyAMA0 (GPIO14/15) — LoRa / ESP32"
echo "   UART3 /dev/ttyAMA3 (GPIO4/5)   — MAVLink FC"
echo "   Disable serial console on UART0"
echo "   Bluetooth is NOT affected"
echo ""

# ---- 1. Disable serial console on UART0 ----
echo "--- Disabling serial console on ttyAMA0 ---"
systemctl stop serial-getty@ttyAMA0.service 2>/dev/null || true
systemctl disable serial-getty@ttyAMA0.service 2>/dev/null || true
echo "  serial-getty@ttyAMA0 disabled"

# Remove console=serial0,* from kernel cmdline
if [ -f "$CMDLINE" ] && grep -q "console=serial0" "$CMDLINE"; then
    cp "$CMDLINE" "${CMDLINE}.bak.$(date +%Y%m%d_%H%M%S)"
    sed -i 's/console=serial0,[0-9]* //' "$CMDLINE"
    echo "  Removed console=serial0 from $CMDLINE"
else
    echo "  No console=serial0 in cmdline (OK)"
fi

# ---- 2. Add UART overlays to config.txt ----
if grep -q "# DC-Detector UART" "$BOOT_CONFIG" 2>/dev/null; then
    echo ""
    echo "--- DC-Detector UART block already in $BOOT_CONFIG (skipping) ---"
else
    cp "$BOOT_CONFIG" "${BOOT_CONFIG}.bak.$(date +%Y%m%d_%H%M%S)"
    cat >> "$BOOT_CONFIG" <<'EOF'

# DC-Detector UART configuration
# UART0 (GPIO14/15) for LoRa/ESP32
enable_uart=1
dtparam=uart0=on
# UART3 (GPIO4/5) for MAVLink flight controller
dtoverlay=uart3-pi5
EOF
    echo ""
    echo "--- UART overlays added to $BOOT_CONFIG ---"
fi

# ---- 3. udev rule for persistent permissions ----
UDEV_RULE="/etc/udev/rules.d/99-dc-detector-uart.rules"
echo 'KERNEL=="ttyAMA*", GROUP="dialout", MODE="0660"' > "$UDEV_RULE"
echo ""
echo "--- udev rule created: $UDEV_RULE ---"

# ---- 4. Add user to dialout group ----
CURRENT_USER="${SUDO_USER:-$USER}"
if id -nG "$CURRENT_USER" | grep -qw dialout; then
    echo "--- User '$CURRENT_USER' already in dialout group ---"
else
    usermod -aG dialout "$CURRENT_USER"
    echo "--- User '$CURRENT_USER' added to dialout group ---"
fi

echo ""
echo "============================================================"
echo " UART setup complete!  Bluetooth is preserved."
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
