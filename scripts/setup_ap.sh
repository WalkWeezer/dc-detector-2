#!/usr/bin/env bash
# DC-Detector v0.2 — Wi-Fi Access Point setup for Raspberry Pi 5
#
# This script installs and configures hostapd + dnsmasq so the Pi
# broadcasts its own Wi-Fi network and serves the DC-Detector web UI.
#
# Run once:  sudo bash scripts/setup_ap.sh
#
# After reboot the AP starts automatically.  Web UI is at http://192.168.4.1:8080
#
# Configuration is read from config.yaml (ap section) or uses defaults below.

set -e

# ---- Defaults (override via config.yaml ap section) ----
AP_SSID="${AP_SSID:-DC-Detector}"
AP_PASSWORD="${AP_PASSWORD:-dcdetector}"
AP_CHANNEL="${AP_CHANNEL:-6}"
AP_IP="${AP_IP:-192.168.4.1}"
AP_INTERFACE="${AP_INTERFACE:-wlan0}"

# Try to read from config.yaml if yq is available
if command -v python3 &>/dev/null && [ -f "$(dirname "$0")/../config.yaml" ]; then
    CFG="$(dirname "$0")/../config.yaml"
    AP_SSID=$(python3 -c "import yaml; c=yaml.safe_load(open('$CFG')); print(c.get('ap',{}).get('ssid','$AP_SSID'))" 2>/dev/null || echo "$AP_SSID")
    AP_PASSWORD=$(python3 -c "import yaml; c=yaml.safe_load(open('$CFG')); print(c.get('ap',{}).get('password','$AP_PASSWORD'))" 2>/dev/null || echo "$AP_PASSWORD")
    AP_CHANNEL=$(python3 -c "import yaml; c=yaml.safe_load(open('$CFG')); print(c.get('ap',{}).get('channel','$AP_CHANNEL'))" 2>/dev/null || echo "$AP_CHANNEL")
    AP_IP=$(python3 -c "import yaml; c=yaml.safe_load(open('$CFG')); print(c.get('ap',{}).get('ip','$AP_IP'))" 2>/dev/null || echo "$AP_IP")
fi

echo "============================================================"
echo " DC-Detector — Wi-Fi AP setup"
echo " SSID:      $AP_SSID"
echo " Password:  $AP_PASSWORD"
echo " Channel:   $AP_CHANNEL"
echo " IP:        $AP_IP"
echo " Interface: $AP_INTERFACE"
echo "============================================================"

# ---- Install packages ----
echo "Installing hostapd and dnsmasq..."
apt-get update -qq
apt-get install -y -qq hostapd dnsmasq

# ---- Stop services during config ----
systemctl stop hostapd 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true

# ---- Static IP for wlan0 ----
DHCPCD_CONF="/etc/dhcpcd.conf"
if ! grep -q "# DC-Detector AP" "$DHCPCD_CONF" 2>/dev/null; then
    cat >> "$DHCPCD_CONF" <<EOF

# DC-Detector AP
interface $AP_INTERFACE
    static ip_address=${AP_IP}/24
    nohook wpa_supplicant
EOF
    echo "Static IP configured in $DHCPCD_CONF"
fi

# ---- hostapd config ----
cat > /etc/hostapd/hostapd.conf <<EOF
interface=$AP_INTERFACE
driver=nl80211
ssid=$AP_SSID
hw_mode=g
channel=$AP_CHANNEL
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$AP_PASSWORD
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
EOF

# Point hostapd to config
sed -i 's|^#DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd 2>/dev/null || true
echo "hostapd configured"

# ---- dnsmasq config ----
cat > /etc/dnsmasq.d/dc-detector.conf <<EOF
interface=$AP_INTERFACE
dhcp-range=192.168.4.10,192.168.4.50,255.255.255.0,24h
address=/#/$AP_IP
EOF
echo "dnsmasq configured"

# ---- Unmask & enable services ----
systemctl unmask hostapd
systemctl enable hostapd
systemctl enable dnsmasq

# ---- Restart ----
systemctl restart dhcpcd
systemctl restart dnsmasq
systemctl restart hostapd

echo ""
echo "============================================================"
echo " AP is running!"
echo " Connect to Wi-Fi: $AP_SSID"
echo " Open browser:     http://${AP_IP}:8080"
echo "============================================================"
