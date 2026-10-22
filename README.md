# My Internship Automation

Two independent, standalone automation tools for finding startup opportunities and enriching company data — both running locally on your machine, using your own browser sessions, with full Telegram control panels.

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
│   └── README.md               # Apollo-specific docs
│
└── WellFound Automation Setup/  # Wellfound Startup Discovery Agent
    ├── startup_agent/          # Core discovery + verification pipeline
    ├── wfbot/                  # Legacy internship application bot (preserved)
    ├── tools/                  # Manual capture helpers
    ├── deploy/                 # Windows autostart, Docker, VPS scripts
    ├── config/, data/, exports/, imports/, logs/, templates/
    ├── run.py                  # Entry point (Telegram agent + background scan)
    ├── setup.ps1, setup_wizard.py, setup_discovery.py
    ├── watchdog.py             # Supervisor: auto-restart, daily schedule
    ├── requirements.txt
    └── README.md               # WellFound-specific docs
```

---

## 🔍 Wellfound Startup Discovery Agent

Finds **real 1–10 employee startups** on Wellfound through your own visible, signed-in browser session, verifies their official websites, and gives you a complete Telegram control panel. Uses a two-phase approach: Phase 1 walks public role pages (HTTP, robots.txt‑allowed), Phase 2 reads each company's profile in a plain browser, extracting the same embedded Apollo data the page renders from.

**Responsible design**: reads only public pages, respects robots.txt, no login needed for Phase 1, rate-limited, stops and reports on any security check (never bypassed). Your own account, your own risk.

- **What it collects**: company name, website, team size (1–10), industry/niche, official website, verification status
- **Output**: 4-column sheet (Name, Website, Platform, Members) or full verified export (10 columns)
- **Telegram control**: Scan start/stop, live progress, download exports
- **Responsible**: no stealth, no CAPTCHA bypass, no rate-limit bypass, pauses on verification pages for manual action

---

## 🔐 Apollo Email Finder

Takes a company list, finds one real contact email per company via Apollo, and intelligently guesses the rest of the team's emails from the pattern. Uses a Playwright-driven persistent logged-in browser, title-priority ranking, SQLite credit ledger, pattern-matching, and MX validation.

**Responsible design**: Apollo's free plan has very limited credits, so this tool never spends more than one credit per company, keeps a persistent ledger so it never re-spends on an already-processed company, and lets you set exactly how many new companies to spend on per run.

- **What it does**: upload company spreadsheet → Apollo finds one verified email per company → guesses teammate patterns → MX-validates → outputs Excel with two sheets (people-first + all data) + team_emails sheet
- **Credit safety**: one credit per company max, persistent SQLite ledger, never re-spends, limit cap per run, stops on challenge (you solve it, then Retry)
- **Local UI**: upload/start/live-progress desktop popup at http://127.0.0.1:8765
- **Telegram**: upload files, start runs, live status, stop, latest file, laptop reopen
- **One-time login**: log in to Apollo once in the Chrome window; profile persists for all later runs

---

## 🔗 How the tools connect

The Wellfound tool's output (company name + website + domain) feeds directly into the Apollo tool's input. Export the Wellfound leads as an Excel/CSV file, then upload it to Apollo to find emails and team patterns for each company.

---

## 🛠 Requirements

- **Windows 10/11** (primary), Linux/macOS (Apollo works; WellFound browser collector needs Chromium)
- **Python 3.11+** (tested on 3.14)
- **Edge or Chrome** installed (for persistent profiles + app-mode popup)
- **Playwright browsers** — installed automatically by `setup.ps1` / `setup_wizard.py`

---

## 📄 License

MIT — use freely, modify, distribute. No warranty.

---

## 🙋 Support

- **Apollo issues** → check `Apollo/README.md` and `Apollo/logs/`
- **WellFound issues** → check `WellFound Automation Setup/README.md` and `logs/startup_agent.log`
- **Telegram bot not responding** → verify `TELEGRAM_BOT_TOKEN` in `.env`, check chat ID matches, ensure only one instance runs (mutex protected)

---

*Built for personal research use. Respect platform Terms of Service. You run the browser, you own the account, you take the risk.*