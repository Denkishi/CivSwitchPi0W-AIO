#!/usr/bin/env bash
# CivSwitch Pi Zero W — setup script
# Run once on a fresh Raspberry Pi OS (Bookworm) on Pi Zero W.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== CivSwitch Pi Zero W Setup ==="

# ── 1. System packages ──────────────────────────────────────────
echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    hostapd dnsmasq python3 python3-pip python3-venv python3-serial \
    iw libcap2-bin

# ── 2. USB gadget serial (g_serial / dwc2) ──────────────────────
echo "[2/5] Configuring USB gadget serial..."

# Enable the dwc2 overlay in config.txt.
# Use our own marker, not a `grep ^dtoverlay=dwc2`: stock Pi OS already ships
# `dtoverlay=dwc2,dr_mode=host` under [cm5], which such a grep matches, so the
# overlay would silently never be added. Emit an explicit [all] header too, so
# the line can't be swallowed by a preceding board-specific section.
CONFIG_FILE="/boot/firmware/config.txt"
[ -f "$CONFIG_FILE" ] || CONFIG_FILE="/boot/config.txt"
MARKER="# CivSwitch USB gadget serial"

if grep -qF "$MARKER" "$CONFIG_FILE"; then
    echo "  dwc2 overlay already configured in $CONFIG_FILE"
else
    printf '\n%s\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n' "$MARKER" \
        | sudo tee -a "$CONFIG_FILE" > /dev/null
    echo "  Added dtoverlay=dwc2,dr_mode=peripheral to $CONFIG_FILE"
fi

# Load dwc2 and g_serial at boot
if ! grep -q "^dwc2" /etc/modules; then
    echo "dwc2" | sudo tee -a /etc/modules > /dev/null
fi
if ! grep -q "^g_serial" /etc/modules; then
    echo "g_serial" | sudo tee -a /etc/modules > /dev/null
fi

# Load now if not already
sudo modprobe dwc2 2>/dev/null || true
sudo modprobe g_serial 2>/dev/null || true

# ── 3. Disable getty on ttyGS0 ───────────────────────────────────
echo "[3/5] Disabling getty on ttyGS0..."
sudo systemctl stop serial-getty@ttyGS0.service 2>/dev/null || true
sudo systemctl disable serial-getty@ttyGS0.service 2>/dev/null || true
sudo systemctl mask serial-getty@ttyGS0.service 2>/dev/null || true

# ── 4. Prevent NetworkManager/hostapd/dnsmasq from auto-starting ─
echo "[4/5] Configuring services..."
# We manage hostapd and dnsmasq ourselves
sudo systemctl stop hostapd 2>/dev/null || true
sudo systemctl disable hostapd 2>/dev/null || true
sudo systemctl stop dnsmasq 2>/dev/null || true
sudo systemctl disable dnsmasq 2>/dev/null || true

# NOTE: wlan0 is NOT permanently unmanaged here on purpose.
# bridge_mode.py releases it from NetworkManager when bridge mode starts
# and hands it back when bridge mode stops, so the Pi keeps its normal
# Wi-Fi connection (and SSH) whenever the bridge is idle.
# Remove any unmanaged rule left by an earlier version of this installer.
if [ -f /etc/NetworkManager/conf.d/civswitch.conf ]; then
    sudo rm -f /etc/NetworkManager/conf.d/civswitch.conf
    sudo systemctl restart NetworkManager 2>/dev/null || true
    echo "  Removed stale unmanaged-devices rule for wlan0"
fi

# ── 5. Install supervisor as a systemd service ───────────────────
echo "[5/5] Installing supervisor service..."
sudo tee /etc/systemd/system/civswitch.service > /dev/null <<EOF
[Unit]
Description=CivSwitch Pi Zero W Supervisor
After=network.target
Wants=network.target
# Give up instead of crash-looping forever: 3 failures in 2 minutes and the
# unit stays down, so a broken supervisor can never fight the rest of boot.
# These two keys belong in [Unit], not [Service] -- systemd silently ignores
# them under [Service] and the limit never applies.
StartLimitIntervalSec=120
StartLimitBurst=3

[Service]
Type=simple
ExecStart=/usr/bin/python3 -u ${SCRIPT_DIR}/supervisor.py
WorkingDirectory=${SCRIPT_DIR}
Restart=on-failure
RestartSec=10
User=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable civswitch.service

# Persist the journal across reboots. Without this, a failure that takes the
# Pi off the network leaves no log to read after rebooting.
#
# Creating /var/log/journal is NOT enough: Raspberry Pi OS ships
# `Storage=volatile` in /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf
# to spare the SD card, which pins the journal to /run/log/journal (RAM) even
# when the directory exists.
#
# The 99- prefix matters. systemd sorts drop-ins by FILENAME across all
# directories, and the last one wins -- being under /etc does not by itself
# beat /usr/lib. A 10- prefix loses to Raspberry Pi's 40-.
sudo mkdir -p /etc/systemd/journald.conf.d
sudo rm -f /etc/systemd/journald.conf.d/10-civswitch-persistent.conf
sudo tee /etc/systemd/journald.conf.d/99-civswitch-persistent.conf > /dev/null <<'EOF'
# Added by CivSwitch install.sh. Delete this file and restart
# systemd-journald to go back to Raspberry Pi OS's RAM-only journal.
[Journal]
Storage=persistent
SystemMaxUse=32M
EOF
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null || true
sudo systemctl restart systemd-journald
# Move what is already in the RAM journal onto disk; at boot
# systemd-journal-flush.service does this, but not after a live config change.
sudo journalctl --flush 2>/dev/null || true
echo "  Enabled persistent journal (Storage=persistent, capped at 32M)"


echo ""
echo "=== Setup complete ==="
echo "Reboot to activate USB gadget serial, then connect via:"
echo "  - Windows: the new COM port in Device Manager"
echo "  - Linux:   /dev/ttyACM0"
echo "  - macOS:   /dev/cu.usbmodem*"
echo ""
echo "The supervisor starts automatically on boot."
echo "Use the PC GUI (civswitch_gui.py) to control everything."
