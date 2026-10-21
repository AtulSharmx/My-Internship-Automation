#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 22.04/24.04 server (Docker route).
#
# 1. Upload the project to /opt/wfbot (see deploy/make_bundle.ps1).
# 2. Run:  sudo bash /opt/wfbot/deploy/setup_vps.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash deploy/setup_vps.sh" >&2
  exit 1
fi

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "Missing $APP_DIR/.env - upload the project together with its .env." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi

# The daily summary (20:00) and "today" counters use local time.
timedatectl set-timezone Asia/Kolkata || true

mkdir -p data logs browser_profile

docker compose -f deploy/docker-compose.yml up -d --build

sleep 5
docker compose -f deploy/docker-compose.yml ps

echo
echo "Done. Follow the bot with:"
echo "  cd $APP_DIR && docker compose -f deploy/docker-compose.yml logs -f"
echo "Check /info and /report in Telegram."
