#!/usr/bin/env bash
# Native install (no Docker) on Ubuntu 22.04/24.04, run as root.
#
# Upload the project to /opt/wfbot first, then:
#   sudo bash /opt/wfbot/deploy/setup_native.sh
#
# For a different location: sudo APP_DIR=/srv/wfbot bash .../setup_native.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo." >&2
  exit 1
fi

APP_DIR="${APP_DIR:-/opt/wfbot}"
cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "Missing $APP_DIR/.env - upload the project together with its .env." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip xvfb x11vnc unzip curl

timedatectl set-timezone Asia/Kolkata || true

id -u wfbot >/dev/null 2>&1 || useradd -r -d "$APP_DIR" -s /bin/bash wfbot

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/ms-playwright" \
  .venv/bin/python -m playwright install --with-deps chromium

mkdir -p data logs browser_profile
chown -R wfbot:wfbot "$APP_DIR"

cp deploy/wfbot.service /etc/systemd/system/wfbot.service
sed -i "s#/opt/wfbot#${APP_DIR}#g" /etc/systemd/system/wfbot.service

systemctl daemon-reload
systemctl enable --now wfbot
sleep 3
systemctl --no-pager --lines=20 status wfbot || true

echo
echo "Logs:  journalctl -u wfbot -f"
echo "Check /info and /report in Telegram."
