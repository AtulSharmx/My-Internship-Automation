# Legacy deployment notes (preserved; not used for the new agent)

> **Status:** This directory documents the preserved internship bot and its
> original always-on deployment. The project did not deploy, modify, or
> validate any AWS/VPS resources. `run.py` now starts the local-first startup
> discovery agent, so these notes must be reviewed before reuse. The new agent
> does not need Chromium for normal imports/exports and does not automate
> Wellfound. See the root `README.md` for the supported local workflow.
>
> The commands and costs below are retained as historical documentation only.

---

# Start it on this laptop (no terminal, no scripts)

The supported way to run the new agent on this machine. Everything below is
local; no VPS, no cloud, no monthly cost.

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install_windows_task.ps1
```

That registers the scheduled task **WellFound Intern Bot**, which starts
`.venv\Scripts\pythonw.exe watchdog.py` at every sign-in. `pythonw.exe` has no
console, so nothing appears on screen and no terminal is ever opened.

After that the only thing you do is **switch the laptop on and sign in**. The
agent connects to Telegram, starts its scan, and keeps itself alive; a crash
is restarted within 15 seconds.

| File | Purpose |
| --- | --- |
| `install_windows_task.ps1` | Registers / refreshes the logon task |
| `agentctl.ps1` | `status`, `start`, `stop`, `restart`, `install`, `uninstall` |
| `..\watchdog.py` | The silent supervisor the task starts |
| `..\run_bot.cmd` | Manual start / stop from a terminal or by double-clicking |

```powershell
powershell -ExecutionPolicy Bypass -File deploy\agentctl.ps1 status
```

The full details are in the root `README.md`, section *Automatic start*.

---

# Running the bot 24/7 without your laptop

The bot is a normal Python process with a visible Chromium. To run it without
your laptop it needs an **always-on machine** that stays awake and logged in.

You cannot move the Wellfound session by copying `browser_profile/`: Chromium
encrypts cookies per machine/user, so the new machine has to sign in **once**.
Everything after that is automatic.

## Pick a route

| Route | Cost | Effort | Best for |
| --- | --- | --- | --- |
| **Linux VPS + Docker** (recommended) | ~₹450–800/month | I can deploy it over SSH | cheapest, truly no laptop |
| **Windows VPS** | ~₹1,200–2,000/month | RDP in, run the same commands as your laptop | easiest to see/solve checks |
| **Raspberry Pi 5 / mini PC at home** | ~₹8,000 one-time | flash SD, SSH from laptop | residential IP = fewest Wellfound checks |
| Keep the laptop | free | set lid-close to “do nothing”, sleep off | zero hardware |

**Datacenter IPs get more CAPTCHA challenges than home IPs.** If you can leave a
₹8k Raspberry Pi at home, that is the most reliable long-term option. If you
want the cheapest zero-hardware option, take a Linux VPS and be ready to solve a
check occasionally.

---

## Option A — Linux VPS + Docker (I can do this for you)

Rent an Ubuntu 24.04 VPS with **2 vCPU / 4 GB RAM / 40 GB disk** (Hetzner CX22,
Contabo VPS S, DigitalOcean 2 GB, or Oracle Cloud Always Free ARM). 2 GB works
but 4 GB is comfortable for Chromium.

### 1. From this laptop, build a clean upload

```powershell
powershell -ExecutionPolicy Bypass -File deploy\make_bundle.ps1
```

### 2. Upload and extract

```powershell
scp wfbot-deploy.tar.gz root@SERVER_IP:/opt/
```

```bash
ssh root@SERVER_IP
mkdir -p /opt/wfbot
tar -xzf /opt/wfbot-deploy.tar.gz -C /opt/wfbot
rm /opt/wfbot-deploy.tar.gz
```

> `browser_profile/` is deliberately not uploaded; see “First login” below.

### 3. Install and start

```bash
cd /opt/wfbot
sudo bash deploy/setup_vps.sh
```

That installs Docker, sets the timezone to Asia/Kolkata, builds the image,
mounts `data/`, `logs/` and `browser_profile/`, and starts the container with
`restart: unless-stopped` (it survives reboots and crashes).

### 4. First login (one time)

Stop the bot, start the VNC helper, and log in through an SSH tunnel:

```bash
cd /opt/wfbot
docker compose -f deploy/docker-compose.yml down
docker compose -f deploy/docker-compose.yml run --rm -it \
  -p 127.0.0.1:5900:5900 wfbot bash deploy/login.sh
```

On your laptop, in a second terminal:

```powershell
ssh -L 5900:localhost:5900 root@SERVER_IP
```

Open a VNC viewer (RealVNC Viewer / TigerVNC) at `localhost:5900`, sign in to
Wellfound in the Chromium window, and wait for “Signed in — session saved”.
The helper exits by itself.

Then start the bot:

```bash
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml logs -f
```

Tap **Status** and **Applications** in Telegram - the server should answer. `/info` and `/report` remain available as typed commands.

### Everyday commands

```bash
cd /opt/wfbot
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f --tail=100
docker compose -f deploy/docker-compose.yml restart
docker compose -f deploy/docker-compose.yml down     # stop
```

Updates: rebuild the bundle on the laptop, `scp` it, extract over `/opt/wfbot`
(keep `.env`, `data/`, `browser_profile/`), then
`docker compose -f deploy/docker-compose.yml up -d --build`.

Back up `data/applications.db` now and then — it is the application history and
the de-duplication memory.

---

## Option A2 — Linux VPS without Docker

Same server. Upload/extract as above, then:

```bash
sudo bash /opt/wfbot/deploy/setup_native.sh
```

This creates a `wfbot` user, a venv, installs Chromium, and runs the bot under
`xvfb-run` via systemd (`systemctl status wfbot`, `journalctl -u wfbot -f`).
First login is the same VNC idea, but run:

```bash
Xvfb :99 -screen 0 1440x900x24 &
DISPLAY=:99 x11vnc -nopw -forever -shared -bg
DISPLAY=:99 /opt/wfbot/.venv/bin/python /opt/wfbot/setup_wizard.py --login
```

Run it as the `wfbot` user and tunnel port 5900 the same way.

---

## Option B — Windows VPS

Rent a Windows Server 2022/2025 VPS (4 GB RAM), connect with RDP, and treat it
exactly like your laptop:

1. Install Python 3.12 (tick **Add python.exe to PATH**).
2. Copy the project folder (without `.venv` and `browser_profile`).
3. In the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

4. Put your `.env` next to `run.py`.
5. Sign in to Wellfound once in the Chromium window the agent opens.
6. Register the logon task:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install_windows_task.ps1
```

The task starts the agent at every logon, without a console window, and
restarts it after a crash. Keep the VPS logged in (locking the screen is fine;
sleep/logoff is not).

---

## Option C — Raspberry Pi / mini PC at home

Best anti-bot option because it uses your home IP. Raspberry Pi 5 (8 GB) with
Raspberry Pi OS 64-bit:

```bash
sudo apt update
sudo apt install -y python3-venv xvfb x11vnc
# copy the project to /opt/wfbot first (same tar as Option A)
sudo bash /opt/wfbot/deploy/setup_native.sh
```

Then do the one-time login with the VNC steps from Option A2. After that you can
unplug the monitor/keyboard: the Pi keeps running headless under Xvfb, and the
bot is reachable only through Telegram.

---

## Notes

- `WF_HEADLESS=false` + Xvfb is intentional: the browser keeps a normal desktop
  fingerprint even though there is no monitor attached.
- The bot applies **once a day at 20:00 (8:00 PM) server time** (`WF_DAILY_RUN_AT`). If the
  container/server was down during the slot, it starts a catch-up pass when it
  comes back within four hours; otherwise it waits for the next day. The process
  still stays up for Telegram and the scheduler.
- The 8:00 PM daily summary and "today" counters use the server timezone. The setup
  scripts set **Asia/Kolkata**; keep the server and `WF_TIMEZONE` aligned.
- Only internships posted in the **previous 24 hours** are eligible, newest
  first. `1 day ago` is treated as outside the window. `WF_MAX_JOB_AGE_DAYS=1`
  and `WF_REQUIRE_POSTED_AGE=true` are the defaults.
- There is no per-run or per-day application cap by default
  (`WF_MAX_APPS_PER_RUN=0`, `WF_MAX_APPS_PER_DAY=0`): every fresh match is
  attempted.
- The bot still never solves CAPTCHAs. If Wellfound challenges it, you get a
  Telegram message and the bot pauses; open the VNC session (Option A) or RDP
  (Option B) and solve it once.
- Keep `.env` private. It contains the Telegram token and, if set, the Wellfound
  password.
