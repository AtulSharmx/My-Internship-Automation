# Wellfound Startup Discovery Agent

A local-first agent that finds **real 1–10 employee startups** on Wellfound through your own visible, signed-in browser session, verifies their official websites, and gives you a complete Telegram control panel for the whole job.

## What it does

- Opens a **normal Chrome window** with your own profile (no stealth, no fingerprint spoofing, no proxy rotation, no CAPTCHA solving).
- Applies Wellfound's own **1–10 employees** filter and reads the rendered results, scrolling/paging until the requested number of profiles is found.
- Visits every eligible company profile, extracts the **official website** (never a social profile, never an invented domain).
- Verifies each website with a polite HTTP request and records the evidence.
- Stores everything in SQLite with a per-lead processing state (`discovered` / `processing` / `verified` / `incomplete` / `failed` / `skipped_duplicate`), de-duplicated across runs.
- Runs the scan in a **background job manager** with checkpoints, so Telegram stays responsive and a pause, a stop, a crash or a restart never loses work.
- Exports Excel / CSV / JSON / run-summary files straight into the chat.

Additional data sources are still supported: permitted open datasets with published headcounts (DBpedia), manual imports (CSV, TSV, JSON, JSONL, XLSX, or a saved HTML page) and allow-listed public URLs.

The legacy internship application bot is preserved in `wfbot/`. It is **not** started by the new entry point and its `data/applications.db` is untouched.

---

## Responsible design notes

This project was built with **compliance first**. The supported Wellfound path is a **visible browser in your own signed-in profile**: you drive it, you own the account, you take the risk. If Wellfound shows a verification or sign-in page, the scan **pauses and asks you to act** in that window — it is never bypassed. The code contains **no browser stealth, no CAPTCHA solving, no login automation, no fingerprint spoofing, no proxy rotation, and no rate-limit bypass**.

- Reads **only public role pages** (robots.txt–allowed) in Phase 1.
- Phase 2 uses a **plain browser with your own profile**; the security check is waited for and, if it does not clear, the run **stops and reports** — it never works around it.
- `wellfound.com` and `angel.co` are always blocked by the URL-discovery adapter.
- Rate-limited with a configurable minimum interval (default 3 seconds) per host.
- Honest user agent: `StartupDiscoveryAgent/1.0 (personal research; respects robots.txt)`.

**This is a deliberate design strength**: the tool never pretends to be something it isn't, and it never spends time or risk on things it cannot control.

---

## How it works — simple explanation

### Phase 1 — Public role-page discovery
The code reads Wellfound's **public role landing pages** (`/role/r/<slug>?page=N`). These pages are allowed by `robots.txt` and carry the full result set in **embedded Next.js/Apollo data**. The company size published on each card is used to filter for 1–10 employees, and the Wellfound company ID is the stable identity key — one company has one ID no matter how many roles it's hiring for.

### Phase 2 — Browser profile enrichment
Every company's **external website** lives on its own Wellfound profile page. This phase opens those pages in a plain browser (Playwright) and reads the **same embedded Apollo data** the page renders from. There is no account and no sign-in here — the browser profile only ever holds the anonymous Cloudflare clearance cookie.

If Wellfound shows a **security check** (not a login), the code waits for it to clear on its own. If it does not clear within the timeout, the run **stops and reports** — it never tries to work around it, spoof a fingerprint or rotate a user agent.

---

## Real folder/file structure

```text
WellFound Automation Setup/
├── startup_agent/
│   ├── __init__.py              # Package init
│   ├── cli.py                 # Typer CLI: init, scan, sheet, import, scrape, stats
│   ├── config.py              # Settings from .env (pydantic-settings)
│   ├── db.py                  # SQLite: leads, runs, settings, url_queue
│   ├── browser.py             # Playwright: Wellfound profile, company pages, challenge detect
│   ├── scraper.py             # Public-page HTTP scraper (roles → profiles → websites)
│   ├── datasets.py            # DBpedia SPARQL (CC BY-SA / CC0, robots permits /sparql)
│   ├── jobs.py                # Background job manager (checkpointed, resumable)
│   ├── telegram.py            # Telegram control panel (keyboard, inline settings, exports)
│   ├── models.py              # Pydantic models: Lead, Run, Job, Settings
│   ├── export.py              # CSV / XLSX / JSON export of the scrape snapshot
│   ├── wfscrape/
│   │   ├── __init__.py
│   │   ├── roles.py           # Editable role slug list (one per line)
│   │   ├── discover.py        # Phase 1: walk role pages, collect target-sized companies
│   │   ├── enrich.py          # Phase 2: read company profiles, extract website & tags
│   │   ├── export.py          # Write snapshot as CSV / XLSX / JSON
│   │   └── runner.py          # Orchestrate Phase 1 + Phase 2, report to Telegram
│   ├── app.py                 # Telegram app entry (run.py → telegram_bot.main())
│   ├── setup_wizard.py        # Interactive .env wizard (detects chat id)
│   ├── setup_discovery.py     # Convenience wrapper for setup_wizard
│   ├── watchdog.py            # Supervisor: auto-restart, daily schedule
│   ├── run.py                 # Entry: python run.py → telegram_bot.main()
│   ├── run_bot.cmd            # Windows wrapper for watchdog commands
│   ├── config/
│   │   └── wf_scrape_roles.txt # Role list for public scraper (editable)
│   ├── templates/
│   │   └── companies_template.csv
│   ├── static/
│   └── tests/
├── wfbot/                     # Legacy internship bot (preserved, separate DB)
├── watchdog.py                # Supervisor: ensures agent runs, restarts on crash
├── run.py                     # Entry: python run.py → telegram_bot.main()
├── run_bot.cmd                # Windows wrapper for watchdog commands
├── setup.ps1                  # Creates venv, installs deps, Playwright browsers
├── setup_discovery.py         # Interactive .env wizard (detects chat id)
├── setup_wizard.py            # Full interactive setup (used by setup_discovery.py)
├── config/
│   └── wf_scrape_roles.txt    # Role list for public scraper (editable)
├── templates/                 # (reserved for future web UI)
├── static/                    # (reserved for future web UI)
├── tests/
├── requirements.txt
├── .env.example               # Copy to .env, add TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
├── .gitignore                 # Git-ignored: .env, browser_profile/, data/, exports/, imports/, logs/
├── MASTER_BRIEF.md            # Full design spec (internal)
└── README.md                  # This file
```

---

## Actual tech stack (check real dependencies)

| Dependency | Purpose |
|---|---|
| Python 3.11+ (tested on 3.14) | Runtime |
| `playwright>=1.49` | Persistent browser control (Phase 2 enrichment) |
| `httpx>=0.27` | Async HTTP (Phase 1 public pages, website verification) |
| `python-dotenv>=1.0` | `.env` configuration loading |
| `openpyxl>=3.1` | XLSX export (via `export.py` → `_xlsx_bytes`) |
| `pytest>=8.0` | Tests (`252 passed` across DB, domain, verification, CLI, export, job checkpointing) |
| `playwright` browsers (Chromium) | Downloaded automatically by `setup.ps1` / `setup_wizard.py` |

---

## Setup & how to run

```powershell
# 1. Create venv and install dependencies
cd "WellFound Automation Setup"
powershell -ExecutionPolicy Bypass -File setup.ps1

# 2. Run the setup wizard (creates .env, detects Telegram chat id)
.venv\Scripts\python.exe setup_discovery.py

# 3. Initialize folders and import sample data (optional)
.venv\Scripts\python.exe -m startup_agent.cli init
.venv\Scripts\python.exe -m startup_agent.cli import imports\companies.csv

# 4. One-time: warm the browser profile (clears security check once)
.venv\Scripts\python.exe -m startup_agent.cli scrape-prime

# 5. Start the Telegram control panel
.venv\Scripts\python.exe run.py
```

### Commands from the CLI

| Command | What it does |
|---|---|
| `python -m startup_agent.cli scan --max 25` | Run a real scan in the foreground (same code as Telegram) |
| `python -m startup_agent.cli sheet --platform wellfound --format xlsx` | Export the confirmed 1-10 company sheet (4 columns: Name, Website, Platform, Members) |
| `python -m startup_agent.cli scrape` | Manual public scrape (Phase 1 only) |
| `python -m startup_agent.cli scrape-prime` | Clear Wellfound's security check once in a visible browser |
| `python -m startup_agent.cli stats` | Show counts and integrity from the database |
| `python -m startup_agent.cli export --format all` | Create timestamped exports |

---

## Sample output — verified export column names

The verified export (10 columns, one row per confirmed 1-10 company):

| Startup Name | Official Website | Employee Range | Exact Member Count | Wellfound Profile URL | Website Verification Status | Discovery Date | Last Checked | Source of Website Evidence | Processing Status |
|---|---|---|---|---|---|---|---|---|---|
| Mnemio AI | https://mnemioai.com | Wellfound | 1-10 Employees | https://wellfound.com/company/mnemio | verified | 2026-10-20T22:21:57+00:00 | 2026-10-20T22:21:57+00:00 | Wellfound company profile — HTTP 200 | verified |
| Rozgoo | https://rozgoo.com | Wellfound | 1-10 Employees | https://wellfound.com/company/rozgoo | verified | 2026-10-20T22:21:57+00:00 | 2026-10-20T22:21:57+00:00 | Wellfound company profile — HTTP 200 | verified |

**Legacy eight-column layout** (from earlier runs):

`company_name, website, domain, team_size, niche, wellfound_profile_url, date_scraped, wellfound_company_id`

Sample CSV row:

```
Historiq,https://historiq.com,historiq.com,1-10,"Technology, Public Sector",https://wellfound.com/company/historiq,2026-10-20,10925154
```

---

## Output workbook (Telegram or CLI `sheet`)

Three sheets, written to `exports/` with a timestamped filename:

| Sheet | Contents |
|---|---|
| `leads` | One row per company: `company_name`, `domain`, `website`, `website_verified`, `team_size`, `niche`, `wellfound_profile_url`, `wellfound_company_id`, `status`, `last_checked`, `verification_evidence`, `notes` |
| `raw` | Exact rows as stored in the DB (includes all import columns) |
| `run_summary` | One row per background job: `run_id`, `started_at`, `finished_at`, `profiles_target`, `profiles_found`, `profiles_verified`, `credits_used`, `status` |

Filters, frozen header row, and column widths are applied.

---

## Closing note

Personal / educational project, not affiliated with Wellfound. Respect the platform's Terms of Service. You drive the browser, you own the account, you take the risk.

---