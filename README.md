# My Internship Automation

Two independent, production-ready automation tools for finding startup opportunities and enriching company data — both running locally on your machine, using your own browser sessions, with full Telegram control panels.

---

## 📦 Repository Structure

```
My-Internship-Automation/
├── Apollo/                      # Apollo Email Finder
│   ├── apollo_tool/            # Core FastAPI + runner + web UI
│   ├── telegram_bot.py         # Telegram control panel (primary interface)
│   ├── desktop_window.py       # Native "app mode" popup window
│   ├── run_ui.py               # Start local server + open browser
│   ├── requirements.txt
│   ├── .env.example
│   └── README.md               # Apollo-specific docs
│
├── WellFound Automation Setup/  # Wellfound Startup Discovery Agent
│   ├── startup_agent/          # Core discovery + verification pipeline
│   ├── wfbot/                  # Legacy internship application bot (preserved)
│   ├── tools/                  # Manual capture helpers
│   ├── deploy/                 # Windows autostart, Docker, VPS scripts
│   ├── config/, data/, exports/, imports/, logs/, templates/
│   ├── run.py                  # Entry point (Telegram agent + background scan)
│   ├── setup.ps1, setup_wizard.py, setup_discovery.py
│   ├── watchdog.py             # Supervisor: auto-restart, daily schedule
│   ├── requirements.txt
│   ├── .env.example
│   ├── MASTER_BRIEF.md         # Complete technical reference
│   └── README.md               # WellFound-specific docs
│
└── README.md                   # This file
```

---

## 🚀 Apollo Email Finder

**What it does:** Upload a spreadsheet of companies (XLSX/CSV/JSON) → Apollo finds one verified email per company → returns an enriched Excel file. All via Telegram or a tiny desktop popup.

### Key Features
- **Telegram-first control** — upload files, start runs, pick limits, download results, all from chat
- **Desktop companion** — a 560×780px Edge/Chrome `--app` window (no tabs, no address bar) for upload + live progress
- **Credit-safe** — only spends Apollo credits on *new* companies; already-processed domains are skipped automatically
- **Local-only** — your Apollo cookies stay in `Apollo/apollo_profile/`; nothing leaves your machine
- **Challenge handling** — if Apollo shows a CAPTCHA, the browser opens on your laptop; you solve it, hit **Retry** in Telegram
- **Windows autostart** — `python telegram_bot.py --install-startup` creates a hidden logon shortcut; bot comes up on boot

### Quick Start (Windows)
```powershell
cd Apollo
powershell -ExecutionPolicy Bypass -File setup.ps1   # creates .venv, installs deps, playwright browsers
# Edit .env → add TELEGRAM_BOT_TOKEN
.\.venv\Scripts\python.exe telegram_bot.py --install-startup   # optional: autostart on login
.\.venv\Scripts\python.exe run_ui.py              # or: telegram_bot.py for headless
```

### Telegram Commands
| Command | Action |
|---------|--------|
| 📎 Send file | Upload company spreadsheet (XLSX/CSV/JSON) |
| 🚀 Start | Pick limit (5/10/25/50/100) → run begins |
| 🖥️ Laptop | Open/raise the desktop popup |
| 📊 Status | Live progress + credits used |
| ⏹ Stop | Graceful stop after current company |
| 📄 Latest file | Re-send last output workbook |

### Desktop Popup
The "Laptop" button (or `/desktop` command) opens a native Chromium/Edge window in `--app` mode — no tabs, no URL bar, just the upload card and live run status. It uses a separate `desktop_profile/` so your main browser is unaffected.

---

## 🔍 Wellfound Startup Discovery Agent

**What it does:** Drives your *own visible, signed-in Chrome window* → applies Wellfound's **"1-10 employees"** filter → visits each matching company profile → extracts the **official website** → verifies it with a polite HTTP request → stores everything in SQLite → exports clean Excel/CSV/JSON sheets. All controlled via Telegram.

### Key Features
- **Your browser, your account** — uses `browser_profile/` with your Wellfound login; no stealth, no proxy rotation, no CAPTCHA solving
- **Compliance-first** — pauses on any verification wall; you complete it manually, then resume
- **Background job manager** — scans run in a checkpointed worker; Telegram stays responsive; pause/stop/crash/restart never loses work
- **Deduplication** — canonical Wellfound profile URL + website host + content fingerprint; verified companies never reappear
- **Multi-source** — Wellfound (your browser), DBpedia open dataset, manual CSV/JSON/HTML imports
- **Telegram control panel** — three buttons: **🚀 Start Scan**, **⏹ Stop**, **📥 Download** (Excel/ZIP)
- **Windows autostart** — `deploy\install_windows_task.ps1` registers a logon task running `watchdog.py` (via `pythonw.exe`, no console window); survives crashes, restarts daily at 09:00

### Quick Start (Windows)
```powershell
cd "WellFound Automation Setup"
powershell -ExecutionPolicy Bypass -File setup.ps1
.\.venv\Scripts\python.exe setup_discovery.py
.\.venv\Scripts\python.exe -m startup_agent.cli init
# Edit .env → add TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
.\.venv\Scripts\python.exe -m startup_agent.cli scan --max 25   # one-off browser scan
.\.venv\Scripts\python.exe run.py                                # Telegram agent + auto-scan
```

### Telegram Control Panel
| Button | Action |
|--------|--------|
| 🚀 Start Scan | Starts/resumes background scan with saved settings |
| ⏹ Stop | Stops active scan; collected data kept |
| 📥 Download | 📄 **this file** (all data so far) or 🗂 **all past** (ZIP of exports/) |

While scanning, the chat shows: **🏃 Running**, **⏳ Stopping**, **⏹ Stopped**, **✅ Done**, **❌ Failed**, **🔐 Verify** (manual check needed in browser window).

### Startup Sheet (the deliverable)
Exactly 4 columns, one row per confirmed 1-10 company:

| Startup Name | Website | Platform | Members |
|--------------|---------|----------|---------|
| Mnemio AI | https://mnemioai.com | Wellfound | 1-10 Employees |
| Rozgoo | https://rozgoo.com | Wellfound | 1-10 Employees |

Build it: `.\.venv\Scripts\python.exe -m startup_agent.cli sheet --platform wellfound --format xlsx`

### Public Page Scraper (separate tool)
```powershell
.\.venv\Scripts\python.exe -m startup_agent.cli scrape-prime   # once: warm up profile
.\.venv\Scripts\python.exe -m startup_agent.cli scrape          # headless, structured data
```
Outputs `exports/wf-scrape-<ts>.{csv,xlsx,json}` with 8 columns: `company_name, website, domain, team_size, niche, wellfound_profile_url, date_scraped, wellfound_company_id`.

---

## 🔐 Secrets & Security

**Never committed:**
- `.env` files (bot tokens, chat IDs, API keys)
- `browser_profile/`, `apollo_profile/`, `desktop_profile/` (cookies, login state)
- `data/*.db` (SQLite databases with collected leads)
- `exports/`, `imports/` (your data files)
- `logs/`

**Template files provided:**
- `Apollo/.env.example` → copy to `.env`, add `TELEGRAM_BOT_TOKEN`
- `WellFound Automation Setup/.env.example` → copy to `.env`, add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

---

## 🛠 Requirements

- **Windows 10/11** (primary), Linux/macOS (Apollo works; WellFound browser collector needs Chromium)
- **Python 3.11+** (tested on 3.14)
- **Edge or Chrome** installed (for persistent profiles + app-mode popup)
- **Playwright browsers** — installed automatically by `setup.ps1` / `setup_wizard.py`

---

## 📋 Common Tasks

### Apollo: Run headless on a VPS
```bash
# On the VPS
git clone https://github.com/AtulSharmx/My-Internship-Automation.git
cd My-Internship-Automation/Apollo
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # add TELEGRAM_BOT_TOKEN
.venv/bin/python telegram_bot.py   # runs headless (no desktop popup)
```

### WellFound: Move project folder
Everything is path-relative. Move the folder, then re-run:
```powershell
powershell -ExecutionPolicy Bypass -File deploy\install_windows_task.ps1
```

### WellFound: Check agent status
```powershell
.\.venv\Scripts\python.exe watchdog.py status
# Output:
# Wellfound agent status
#   supervisor : running (pid 6580)
#   agent     : running (pid 9404)
#   switched  : on
```

---

## 🧪 Testing

```bash
# Apollo
cd Apollo && .venv/bin/pytest -q

# WellFound
cd "WellFound Automation Setup" && .venv/Scripts/python.exe -m pytest -q
# 252 passed (189 new + 63 legacy)
```

---

## 📄 License

MIT — use freely, modify, distribute. No warranty.

---

## 🙋 Support

- **Apollo issues** → check `Apollo/README.md` and `Apollo/logs/`
- **WellFound issues** → check `WellFound Automation Setup/MASTER_BRIEF.md` and `logs/startup_agent.log`
- **Telegram bot not responding** → verify `TELEGRAM_BOT_TOKEN` in `.env`, check chat ID matches, ensure only one instance runs (mutex protected)

---

*Built for personal research use. Respect platform Terms of Service. You run the browser, you own the account, you take the risk.*