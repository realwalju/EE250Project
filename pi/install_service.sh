#!/usr/bin/env bash
# Installs the hub as a systemd service so it starts on boot and restarts
# itself if it crashes. Run once on the Pi, from anywhere:
#
#   bash ~/EE250Project/pi/install_service.sh
#
# Afterwards:
#   sudo systemctl status desk-hub     # is it running?
#   journalctl -u desk-hub -f          # live log (same output as running hub.py)
#   sudo systemctl restart desk-hub    # after a git pull
#   sudo systemctl disable --now desk-hub   # go back to running it by hand
#
# AI-assisted: generated with Claude (Anthropic), reviewed by the team.

set -euo pipefail

PI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$PI_DIR/.venv/bin/python"
USER_NAME="$(id -un)"

if [ ! -x "$PYTHON" ]; then
    echo "No venv at $PI_DIR/.venv. Create it first (see README, step 1)." >&2
    exit 1
fi

sudo tee /etc/systemd/system/desk-hub.service >/dev/null <<EOF
[Unit]
Description=EE250 Fortnite Desk Companion hub
After=network-online.target mosquitto.service
Wants=network-online.target mosquitto.service

[Service]
User=$USER_NAME
WorkingDirectory=$PI_DIR
ExecStart=$PYTHON $PI_DIR/hub.py --broker localhost
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now mosquitto
sudo systemctl enable --now desk-hub
echo
echo "desk-hub installed and started. Live log: journalctl -u desk-hub -f"
