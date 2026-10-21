# Apollo Email Finder

Local, credit-safe email enrichment driven through the real Apollo web UI in a persistent Chrome profile. There is no Apollo API usage and no email-sending code.

---

## What it does

For each **new** company in the uploaded spreadsheet:

1. Opens Apollo's People search for the company domain and reads up to two free pages (plus a title-priority search).
2. Ranks candidates: **Founder/Co-Founder** → **CEO** → **CTO** → **Head of Talent/People/HR/Recruiting** → everyone else.
3. Reveals **at most one** email, choosing a badge-verified full-name candidate within the top available title tier when possible. Masked names are skipped in favor of the highest-ranked full-name candidate.
4. Derives one company pattern from the revealed address and applies it locally to other full-name teammates.
5. Validates guesses for syntax and MX. Failures stay in the team sheet with low confidence.
6. Fills blank `niche` and `team_size` cells from Apollo company data. Existing values are never overwritten.
7. Rewrites the workbook after each company so partial results survive a stop.

The `employees` sheet is **first** and is the simple, people-first view:
`Employee Name`, `Company Name`, `Company Members`, `Website`, `Job Title`, and `Email` (email is **always the last column**).
Companies that were skipped or not reached still get one placeholder row so nothing disappears.
The original upload columns are kept in the next sheet with `email`, `email_source` (`verified` / `guessed`), and `notes` appended.
A final `team_emails` sheet lists every collected teammate, including masked contacts with a blank email and a note.

---

## Setup

1. `Start Apollo Tool.cmd` creates the virtual environment on first launch and opens the local UI.
2. On the first run, Chrome opens with the persistent `apollo_profile` folder. **Log in to Apollo once.** Later runs reuse the session.
3. The UI opens with an upload dialog. Drop an `.xlsx`, `.xlsm`, `.csv`, or `.json` file, review the parsed preview, and choose how many new companies to process.
4. Leave **Hide Chrome while running** checked to move the Apollo browser off-screen while it works. It returns automatically if a challenge or login screen stops the run; use **Show Apollo browser** to bring it back manually. Chrome is launched as a normal browser and controlled locally, which keeps Apollo's security widget working; **do not close that Chrome window while a run is active**.

Everything runs locally. The optional Telegram control panel only talks to Telegram for commands, file uploads, and status messages; Apollo itself is still driven through its web UI, with no Apollo API or email-sending code.

---

## Telegram control panel

The bot is the no-CMD option: type `/start` once, then use the persistent buttons **Start**, **Status**, **Stop**, **Latest file**, and **Laptop**. Send an `.xlsx`, `.xlsm`, `.csv`, or `.json` document to the bot, choose a credit limit, and the finished workbook comes back in the same chat.

The bot starts the local FastAPI app hidden, sends Telegram documents through the same `/api/upload` endpoint as the local UI, and uses the same ledger and run manager. The first chat that sends `/start` is paired automatically; later chats are rejected. The token lives only in `.env` as `TELEGRAM_BOT_TOKEN`. The startup shortcut launches `pythonw.exe telegram_bot.py` after login, so the laptop needs no terminal window. Laptop sleep/off means the bot cannot respond; it catches up when the laptop is awake.

The bot also opens a compact app-style window (Edge/Chrome `--app` mode, no tabs or address bar) at `http://127.0.0.1:8765/desktop`. It has the same upload preview, credit-limit buttons, and live progress as Telegram, but no dashboard. If it is closed, use the **Laptop** button to reopen it. Uploads and runs started from either surface are mirrored to the other.

Telegram's bot download limit is 20 MB for incoming documents and 50 MB for outgoing files. Use the web UI for larger inputs. To reinstall the login shortcut, run `.venv\Scripts\python.exe telegram_bot.py --install-startup` once.

---

## Input formats

The uploader accepts both the current verified Wellfound export and the older eight-column layout.

**Verified export columns (XLSX, CSV, or JSON):**

`Startup Name, Official Website, Employee Range, Exact Member Count, Wellfound Profile URL, Website Verification Status, Discovery Date, Last Checked, Source of Website Evidence, Processing Status`

The company domain is derived from `Official Website`, the employee range is used as `team_size`, and the Wellfound company slug is derived from the profile URL.

**Legacy input columns:**

`company_name, website, domain, team_size, niche, wellfound_profile_url, date_scraped, wellfound_company_id`

Header names are matched case-insensitively and spaces/punctuation are ignored (for example, `Company Name` matches `company_name`). Original columns are preserved in the output.

---

## Credit safety

- The SQLite ledger is in `data/apollo.db`.
- A company's credit attempt is committed **before** the Access email click.
- Completed, attempting, and unknown companies are not retried automatically.
- Every UI run is live; only the selected new companies can use a credit. The `limit` box caps the maximum.
- If Apollo shows a challenge, the run stops and leaves/restores Chrome so you can act.
- The **Recovery: reset a company** control deletes a ledger row only after an explicit confirmation. Use it only after checking Apollo manually, because it allows another credit to be spent later.

---

## Output

Workbooks are written to `exports/`. The filename includes the run ID. The first `employees` sheet has the clean columns described above, a bold header, filters, a frozen header row, sized columns, and mailto/website links. The original-data sheet keeps all upload columns, and the `team_emails` sheet has filters, frozen header, validation results, confidence labels, and color-coded skipped/low-confidence rows.

---

## Local development

```powershell
cd Apollo
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe run_ui.py --no-browser
# or hidden bot:
.\.venv\Scripts\pythonw.exe telegram_bot.py
```

`run_ui.py --no-browser` keeps the UI local without opening a tab. The UI listens on `127.0.0.1` only.

---

## Architecture overview

```
telegram_bot.py (pythonw.exe, hidden)
    ├─ BotServer.ensure()  →  run_ui.py (uvicorn, CREATE_NO_WINDOW)  →  apollo_tool/web.py (FastAPI)
    │       │
    │       └─ /api/upload, /api/start, /api/status, /api/stop, /api/reset
    │
    ├─ handle_document → upload → /api/upload (source=telegram) → staged preview → limit buttons
    ├─ handle_run      → /api/start (limit) → RunManager.start() → background task
    ├─ _watch_run      → polls /api/status every ~1.5s → live edits in Telegram
    ├─ _monitor_activity → polls /api/status every ~1.4s → mirrors laptop uploads/runs into chat
    └─ open_desktop    → desktop_window.show_desktop_window() → Edge/Chrome --app=.../desktop

apollo_tool/runner.py (RunManager)
    ├─ RunState (dataclass) → as_dict() → /api/status
    ├─ _run_loop()
    │   ├─ selects new companies (skips processed/attempting/unknown)
    │   ├─ ledger.mark_attempting() before reveal
    │   ├─ browser.ensure_logged_in() → ChallengeDetected / LoginRequired
    │   ├─ browser.company_people() → select_candidate() → browser.reveal_email()
    │   ├─ pattern derivation → guess teammates → syntax+MX validate
    │   ├─ _write_output() after each company
    │   └─ ledger.mark_processed() / mark_no_candidate() / mark_unknown()
    └─ db.record_run_finish()

desktop_window.py
    ├─ _find_window() → exact + fuzzy title match (handles " - Profile N" suffix)
    ├─ _launch() → subprocess.Popen(browser, --app=..., --user-data-dir=desktop_profile)
    ├─ show_desktop_window() → bring to front or launch (6s grace period prevents double-launch)
    └─ close_desktop_window() → WM_CLOSE (tests/future tray)

apollo_tool/static/desktop.js
    ├─ upload via XMLHttpRequest (progress events) → /api/upload (source=desktop)
    ├─ limit pills (1, 5, 10, 25) → Start enabled only when limit chosen
    ├─ poll /api/status every 600ms (running) / 700ms (idle)
    ├─ renderRun() / renderStatus() → live progress, challenge box, finished card
    └─ New run button → resets limit, shows upload view
```

---

## Key files

| File | Purpose |
|------|---------|
| `telegram_bot.py` | Hidden Telegram bot, uploads, live watchers, activity mirror, desktop launch |
| `apollo_tool/web.py` | FastAPI endpoints: `/desktop`, `/api/upload`, `/api/start`, `/api/status`, `/api/stop`, `/api/reset` |
| `apollo_tool/runner.py` | Run orchestration, ledger, Chrome CDP, reveal logic, output writing |
| `apollo_tool/apollo.py` | Playwright CDP, challenge/login handling, people parsing, reveal click |
| `apollo_tool/excel_io.py` | Input parsing (xlsx/xlsm/csv/json) and 3-sheet output workbook generation |
| `apollo_tool/db.py` | SQLite ledger (company attempts) + run history |
| `apollo_tool/config.py` | Settings from `.env` (paths, limits, Telegram token) |
| `desktop_window.py` | Edge/Chrome `--app` launcher with deduplication & grace period |
| `apollo_tool/templates/desktop.html` | Popup markup (upload, limits, live run, finished card) |
| `apollo_tool/static/desktop.js` | Popup logic: upload progress, limit pills, polling, live render |
| `apollo_tool/static/desktop.css` | Popup styling |
| `run_ui.py` | Local server entry (`uvicorn`, port 8765) |
| `Start Apollo Tool.cmd` | Windows launcher (creates venv, runs `run_ui.py`) |
| `data/apollo.db` | SQLite ledger + run history (git-ignored) |
| `apollo_profile/` | Persistent Chrome profile for Apollo login (git-ignored) |
| `desktop_profile/` | Persistent Edge/Chrome profile for popup (git-ignored) |
| `exports/` | Output workbooks (git-ignored) |
| `logs/telegram_bot.log` | Bot diagnostics |
| `logs/telegram_server.log` | Hidden server + polling diagnostics |

---

## Environment (`.env`)

```ini
# Apollo credentials are not stored here. Log in once in the Chrome window.
APOLLO_PROFILE_DIR=apollo_profile
APOLLO_DB_PATH=data/apollo.db
APOLLO_EXPORTS_DIR=exports
APOLLO_HEADLESS=0
APOLLO_PEOPLE_PAGES=2

# BotFather token. Keep it local; never commit this file.
TELEGRAM_BOT_TOKEN=

# Optional: lock the bot to one Telegram chat. Leave blank to pair the first /start.
TELEGRAM_ALLOWED_CHAT_ID=
```

Copy `.env.example` to `.env` and fill in the token.

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 41 passed
```

Tests cover: Telegram formatting, limit buttons, menu structure, elapsed time, upload name sanitization, runner state, email validation, pattern derivation, DB operations.

---

## Common issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Popup opens repeatedly | Old code called `_show_desktop_soon()` on every upload/start | Fixed: now only on actual run start, monitor detection, or explicit **Laptop** |
| "Login required" immediately | Chrome profile not logged in | Open the Apollo Chrome window, log in, then retry |
| Challenge stops run | Apollo verification screen | Solve in the restored Chrome window, then **Retry** in Telegram or popup |
| No workbook delivered | Run failed before finish | Check `logs/telegram_bot.log`; latest successful export is in `exports/` |
| Telegram bot unresponsive | Multiple bot instances | `taskkill /f /im pythonw.exe` then restart via shortcut or `pythonw.exe telegram_bot.py` |

---

## License

Internal tooling. Not for public distribution.