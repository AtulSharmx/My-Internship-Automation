# Wellfound Startup Discovery Agent

A local-first agent that finds **real 1–10 employee startups** on Wellfound through your own visible, signed-in browser session, verifies their official websites, and gives you a complete Telegram control panel for the whole job.

---

## What it does

- Opens a **normal Chrome window** with your own profile (no stealth, no fingerprint spoofing, no proxy rotation, no CAPTCHA solving).
- Applies Wellfound's own **1–10 employees** filter and reads the rendered results, scrolling/paging until the requested number of profiles is found.
- Visits every eligible company profile, extracts the **official website** (never a social profile, never an invented domain).
- Verifies each website with one polite HTTP request and records the evidence.
- Stores everything in SQLite with a per-lead processing state (`discovered` / `processing` / `verified` / `incomplete` / `failed` / `skipped_duplicate`), de-duplicated across runs.
- Runs the scan in a **background job manager** with checkpoints, so Telegram stays responsive and a pause, a stop, a crash or a restart never loses work.
- Exports Excel / CSV / JSON / run-summary files straight into the chat.

Additional data sources are still supported: permitted open datasets with published headcounts (DBpedia), manual imports (CSV, TSV, JSON, JSONL, XLSX, or a saved HTML page) and allow-listed public URLs.

The legacy internship application bot is preserved in `wfbot/`. It is **not** started by the new entry point and its `data/applications.db` is untouched.

---

## Important compliance decision

Wellfound's terms prohibit automated harvesting, and public company pages have returned `403`/robots restrictions. This project therefore does **not** contain browser stealth, CAPTCHA solving, login automation, fingerprint spoofing, proxy rotation, or rate-limit bypass.

The supported Wellfound path is a **visible browser in your own signed-in profile**: you drive it, you own the account, you take the risk. If Wellfound shows a verification or sign-in page, the scan **pauses and asks you to act** in that window — it is never bypassed. `wellfound.com` and `angel.co` are also always blocked by the plain-HTTP URL-discovery adapter.

---

## Quick start (Windows)

```powershell
cd "WellFound Automation Setup"
powershell -ExecutionPolicy Bypass -File setup.ps1
.\.venv\Scripts\python.exe setup_discovery.py
.\.venv\Scripts\python.exe -m startup_agent.cli init
.\.venv\Scripts\python.exe -m startup_agent.cli scan --max 25    # real browser scan
.\.venv\Scripts\python.exe -m startup_agent.cli sheet --platform wellfound --format xlsx
.\.venv\Scripts\python.exe -m startup_agent.cli import imports\companies.csv
.\.venv\Scripts\python.exe -m startup_agent.cli scrape-prime   # once, clears the check
.\.venv\Scripts\python.exe -m startup_agent.cli scrape          # public-page scrape
.\.venv\Scripts\python.exe run.py                                # Telegram control panel
```

`run.py` starts the Telegram agent. It requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; the setup wizard can detect the chat id after you send the bot a message.

If `STARTUP_WELLFOUND_BROWSER=true`, starting the agent also starts a real Wellfound scan in the background (or resumes the one that was interrupted). Set `STARTUP_AUTO_SCAN=false` if you only want the control panel.

Python 3.11+ is supported; development/testing here uses Python 3.14.

---

## Automatic start (open the laptop and it just runs)

Install the logon task **once**. After that you never open a terminal, never run a script and never start anything by hand: switch the laptop on, sign in to Windows, and the Telegram agent comes up on its own — silently, with no console window, and it restarts itself if anything goes wrong.

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install_windows_task.ps1
```

The task **WellFound Intern Bot** starts `.venv\Scripts\pythonw.exe watchdog.py`. `pythonw.exe` has no console, so nothing flashes on screen.

| What happens | When |
| --- | --- |
| The supervisor starts the agent | Every time you sign in to Windows |
| The agent is restarted after a crash | 15 seconds later, for as long as it keeps dying |
| The task restarts the supervisor itself | 1 minute after it stops, 999 times over |
| The task is started again if it was ever stopped by hand | Every 15 minutes |
| A Wellfound scan starts on its own | Right after the agent comes up (`STARTUP_AUTO_SCAN=true`) |
| The daily scan runs | Every day at `STARTUP_DAILY_RUN_AT` (09:00 by default) |

The laptop has to be on and awake; the screen does not have to be watched. Wellfound runs in your own visible browser window, exactly as it does today.

### Checking that it is alive

```powershell
.\.venv\Scripts\python.exe watchdog.py status
```

```
Wellfound agent status
  supervisor : running (pid 6580)
  agent     : running (pid 9404)
  switched  : on
```

The same information is in `logs\watchdog.log`; the agent's own log is `logs\startup_agent.log`.

### Controlling it

```powershell
.\.venv\Scripts\python.exe watchdog.py stop      # switch the agent off
.\.venv\Scripts\python.exe watchdog.py start     # switch it back on
.\.venv\Scripts\python.exe watchdog.py restart   # both in one go
```

or, the same commands without typing the Python path:

```powershell
.\run_bot.cmd stop
.\run_bot.cmd start
.\run_bot.cmd restart
```

---

## Telegram control panel (run.py)

After `/start`, the bot shows a persistent custom keyboard:

| Button | Action |
|--------|--------|
| 🔍 **Scan** | Start / resume a Wellfound browser scan |
| 📊 **Sheet** | Export current leads (XLSX / CSV / JSON) |
| 📥 **Import** | Upload a CSV/JSON/XLSX to add companies manually |
| ⚙️ **Settings** | Inline buttons: scan limit, daily time, URL allow-list, dataset toggle |
| 📈 **Status** | Live job status: phase, progress, current company, queue depth |
| ⏹ **Stop** | Graceful stop after current profile |
| 📋 **Logs** | Last 50 log lines |

**Inline settings** (from ⚙️ Settings):
- Scan limit (default 25, stored in DB)
- Daily run time (HH:MM, blank = disabled)
- URL discovery allow-list (comma-separated domains)
- Dataset toggle (DBpedia on/off)

All exports are delivered as Telegram documents. The bot only accepts updates from the configured `TELEGRAM_CHAT_ID`; every other chat is ignored.

---

## Input formats

### Manual import (CLI or Telegram)

`startup_agent.cli import` accepts CSV, TSV, JSON, JSONL, XLSX, or a saved HTML page. Columns are matched case-insensitively; synonyms are recognised.

| Canonical | Synonyms |
|-----------|----------|
| `company_name` | `name`, `startup_name`, `Startup Name` |
| `website` | `official_website`, `Official Website`, `url` |
| `domain` | `domain_name`, `root_domain` |
| `team_size` | `employee_range`, `Employee Range`, `Exact Member Count`, `headcount` |
| `niche` | `industry`, `industries`, `sector` |
| `wellfound_profile_url` | `profile_url`, `Wellfound Profile URL`, `angellist_url` |
| `wellfound_company_id` | `company_id`, `Wellfound Company ID` |
| `date_scraped` | `discovery_date`, `Discovery Date` |

A `domain` is derived from `website` if missing. Rows without a usable domain are skipped with a warning.

### Verified Wellfound export (current format)

`Startup Name, Official Website, Employee Range, Exact Member Count, Wellfound Profile URL, Website Verification Status, Discovery Date, Last Checked, Source of Website Evidence, Processing Status`

### Legacy eight-column layout

`company_name, website, domain, team_size, niche, wellfound_profile_url, date_scraped, wellfound_company_id`

---

## Output workbook (Telegram or CLI `sheet`)

Three sheets, written to `exports/` with a timestamped filename:

| Sheet | Contents |
|-------|----------|
| `leads` | One row per company: `company_name`, `domain`, `website`, `website_verified`, `team_size`, `niche`, `wellfound_profile_url`, `wellfound_company_id`, `status`, `last_checked`, `verification_evidence`, `notes` |
| `raw` | Exact rows as stored in the DB (includes all import columns) |
| `run_summary` | One row per background job: `run_id`, `started_at`, `finished_at`, `profiles_target`, `profiles_found`, `profiles_verified`, `credits_used`, `status` |

Filters, frozen header row, and column widths are applied.

---

## Configuration (`.env`)

Copy `.env.example` to `.env` and fill in the required values. The file is **git-ignored**; never commit real tokens.

```ini
# --- Telegram -------------------------------------------------------------
TELEGRAM_BOT_TOKEN=              # from @BotFather
TELEGRAM_CHAT_ID=                # numeric id or @channelusername

# --- Target companies -----------------------------------------------------
STARTUP_TARGET_SIZE=1-10         # employee range to keep

# --- Permitted URL discovery ---------------------------------------------
STARTUP_ENABLE_URL_DISCOVERY=false
STARTUP_ALLOWED_DOMAINS=         # comma/semicolon separated, subdomains included
STARTUP_SEED_URLS=               # optional public listing/profile URLs on allowed domains
STARTUP_MIN_REQUEST_INTERVAL_SECONDS=3
STARTUP_REQUEST_TIMEOUT_SECONDS=20
STARTUP_MAX_RETRIES=2
STARTUP_MAX_URLS_PER_RUN=20
STARTUP_MAX_PAGE_MB=2

# --- Permitted open datasets (real company records) ----------------------
STARTUP_ENABLE_DATASETS=false
STARTUP_DATASET_SOURCES=dbpedia
STARTUP_DATASET_LIMIT=250

# --- Optional daily discovery --------------------------------------------
STARTUP_DAILY_RUN_AT=09:00       # blank = manual only

# --- Wellfound through your own browser (optional) -----------------------
STARTUP_WELLFOUND_BROWSER=false
STARTUP_WELLFOUND_MAX=25
STARTUP_BROWSER_PROFILE=browser_profile
STARTUP_AUTO_SCAN=true

# --- Public Wellfound scraper (startup_agent.cli scrape) ----------------
WFSCRAPE_ROLES_FILE=config/wf_scrape_roles.txt
WFSCRAPE_MAX_PAGES_PER_ROLE=3
WFSCRAPE_MAX_COMPANIES=150
WFSCRAPE_TARGET_SIZE=1-10
WFSCRAPE_HTTP_DELAY_SECONDS=1.5
WFSCRAPE_BROWSER_DELAY_SECONDS=2.5
WFSCRAPE_HEADLESS=true
WFSCRAPE_PROFILE=data/wfscrape_profile
WFSCRAPE_NAV_TIMEOUT_MS=45000
WFSCRAPE_CHALLENGE_WAIT_SECONDS=30

# --- Storage / imports / exports -----------------------------------------
STARTUP_DB_PATH=data/startups.db
STARTUP_IMPORT_DIR=imports
STARTUP_EXPORT_DIR=exports
STARTUP_LOG_DIR=logs
STARTUP_MAX_IMPORT_MB=10
STARTUP_MAX_EXPORT_ROWS=0        # 0 = no cap

# --- Diagnostics ----------------------------------------------------------
STARTUP_TELEGRAM_POLL_TIMEOUT=30
STARTUP_LOG_LEVEL=INFO
STARTUP_USER_AGENT=StartupDiscoveryAgent/1.0 (personal research; respects robots.txt)
```

---

## Project structure

```
WellFound Automation Setup/
├── startup_agent/
│   ├── __init__.py
│   ├── cli.py              # Typer CLI: init, scan, sheet, import, scrape, scrape-prime
│   ├── config.py           # Settings from .env (pydantic-settings)
│   ├── db.py               # SQLite: leads, runs, settings, url_queue
│   ├── browser.py          # Playwright: Wellfound profile, company pages, challenge detect
│   ├── scraper.py          # Public-page HTTP scraper (roles → profiles → websites)
│   ├── datasets.py         # DBpedia SPARQL (CC BY-SA / CC0, robots permits /sparql)
│   ├── jobs.py             # Background job manager (checkpointed, resumable)
│   ├── telegram_bot.py     # Telegram control panel (run.py entry)
│   ├── models.py           # Pydantic models: Lead, Run, Job, Settings
│   └── utils.py            # Helpers: domain normalize, verification, export
├── wfbot/                  # Legacy internship bot (preserved, separate DB)
├── watchdog.py             # Supervisor: ensures agent runs, restarts on crash
├── run.py                  # Entry: python run.py  →  telegram_bot.main()
├── run_bot.cmd             # Windows wrapper for watchdog commands
├── setup.ps1               # Creates venv, installs deps, Playwright browsers
├── setup_discovery.py      # Interactive .env wizard (detects chat id)
├── setup_wizard.py         # Full interactive setup (used by setup_discovery.py)
├── deploy/
│   └── install_windows_task.ps1  # Creates "WellFound Intern Bot" logon task
├── config/
│   └── wf_scrape_roles.txt # Role list for public scraper (editable)
├── templates/              # (reserved for future web UI)
├── static/                 # (reserved for future web UI)
├── tests/
├── requirements.txt
├── .env.example
├── .gitignore
├── MASTER_BRIEF.md         # Full design spec
└── README.md               # This file
```

---

## Key files

| File | Purpose |
|------|---------|
| `run.py` | Starts the Telegram agent (`startup_agent.telegram_bot.main()`) |
| `watchdog.py` | Supervisor: launches/restarts `run.py`, installs logon task |
| `startup_agent/cli.py` | All CLI commands: `init`, `scan`, `sheet`, `import`, `scrape`, `scrape-prime` |
| `startup_agent/jobs.py` | Background job manager with checkpoints, resume, pause, stop |
| `startup_agent/browser.py` | Playwright: Wellfound login, filter, scroll, company profile, website extraction |
| `startup_agent/scraper.py` | Public-page HTTP scraper (roles → profiles → websites) |
| `startup_agent/datasets.py` | DBpedia SPARQL query for 1–10 employee companies |
| `startup_agent/db.py` | SQLite schema: `leads`, `runs`, `settings`, `url_queue` |
| `startup_agent/telegram_bot.py` | Telegram control panel: keyboard, inline settings, exports |
| `startup_agent/config.py` | Pydantic-settings from `.env` |
| `deploy/install_windows_task.ps1` | Creates Windows logon task for auto-start |
| `config/wf_scrape_roles.txt` | Role list for public scraper (one per line) |
| `data/startups.db` | SQLite database (git-ignored) |
| `browser_profile/` | Your signed-in Chrome profile for Wellfound (git-ignored) |
| `data/wfscrape_profile/` | Unsigned-in profile for public scraper (git-ignored) |
| `exports/` | Timestamped XLSX/CSV/JSON exports (git-ignored) |
| `imports/` | Manual import files (git-ignored) |
| `logs/` | `watchdog.log`, `startup_agent.log` (git-ignored) |

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover: DB schema, domain normalization, verification logic, CLI parsing, export formatting, job checkpointing.

---

## Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Scan pauses immediately | Wellfound shows verification/login | Solve in the Chrome window, scan resumes automatically |
| "No profiles found" | Filter too strict or page structure changed | Check `WFSCRAPE_ROLES_FILE`; adjust `WFSCRAPE_MAX_PAGES_PER_ROLE` |
| Telegram bot doesn't respond | Wrong `TELEGRAM_CHAT_ID` or token | Run `setup_discovery.py` to detect chat id after messaging bot |
| Daily scan doesn't run | `STARTUP_DAILY_RUN_AT` blank or agent off | Set time in `.env` or via ⚙️ Settings; ensure watchdog `status` shows `switched: on` |
| Exports empty | No verified leads yet | Run a scan first; `sheet` only exports `verified`/`incomplete` leads |

---

## License

Internal tooling. Not for public distribution.