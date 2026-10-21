#!/usr/bin/env bash
# One-time interactive Wellfound login on a headless server (Docker route).
#
# Stop the bot first so two Chromium instances never share the profile:
#   docker compose -f deploy/docker-compose.yml down
#
# Then start this helper:
#   docker compose -f deploy/docker-compose.yml run --rm -it \
#     -p 127.0.0.1:5900:5900 wfbot bash deploy/login.sh
#
# On your laptop open an SSH tunnel:
#   ssh -L 5900:localhost:5900 root@SERVER_IP
# and point a VNC viewer (RealVNC/TigerVNC) at localhost:5900.
# Log in to Wellfound in that window; the helper detects success and exits.
#
# Afterwards: docker compose -f deploy/docker-compose.yml up -d
set -euo pipefail

Xvfb :99 -screen 0 1440x900x24 &
export DISPLAY=:99
sleep 1

x11vnc -display :99 -nopw -forever -shared -quiet -bg

echo
echo "VNC is listening on 127.0.0.1:5900 (inside the container)."
echo "On your laptop run:  ssh -L 5900:localhost:5900 root@SERVER_IP"
echo "then connect your VNC viewer to localhost:5900."
echo
echo "A Chromium window will open on Wellfound's login page. Sign in there."
echo "This script finishes by itself once you are signed in (or after ~7 min)."
echo

python setup_wizard.py --login --profile-dir /app/browser_profile
