# Apollo Email Finder

Local, credit-safe email enrichment driven through the real Apollo web UI in a persistent Chrome profile. There is **no Apollo API usage and no email-sending code**.

## What it does

For each **new** company in the uploaded spreadsheet:

1. **Opens Apollo's People search** for the company domain and reads up to two free pages (plus a title-priority search).
2. **Ranks candidates**: Founder/Co-Founder → CEO → CTO → Head of Talent/People/HR/Recruiting → everyone else.
3. **Reveals at most one email**, choosing a badge-verified full-name candidate within the top available title tier when possible. Masked names are skipped in favor of the highest-ranked full-name candidate.
4. **Derives one company pattern** from the revealed address and applies it locally to other full-name teammates.
5. **Validates guesses** for syntax and MX. Failures stay in the team sheet with low confidence.
6. **Fills blank `niche` and `team_size` cells** from Apollo company data. Existing values are never overwritten.
7. **Rewrites the workbook after each company** so partial results survive a stop.

The `employees` sheet is **first** and is the simple, people-first view:
`Employee Name`, `Company Name`, `Company Members`, `Website`, `Job Title`, and `Email` (email is **always the last column**).

Companies that were skipped or not reached still get one placeholder row so nothing disappears. The original upload columns are kept in the next sheet with `email`, `email_source` (`verified` / `guessed`), and `notes` appended. A final `team_emails` sheet lists every collected teammate, including masked contacts with a blank email and a note.

---

## How it works — simple explanation

### The run loop
For each new company (skipping ones already processed in the SQLite ledger):

1. **Search people** on Apollo for the company's domain — up to two free pages, plus a title-priority search for leadership / talent titles.
2. **Dedupe and rank** the collected people by title tier (Founder > CEO > CTO > Head of Talent → everyone else).
3. **Select the best candidate** — preferring verified + full name, falling back to the highest-ranked title tier if needed.
4. **Reveal the email** by clicking Apollo's "Access email" button (this spends one Apollo Free-plan credit).
5. **Derive a company email pattern** from the revealed address (e.g., `first.last`).
6. **Apply the pattern** to all other full-name teammates locally — masked names are only guessed when the pattern can be rendered from first name + last initial.
7. **Validate every guessed address** for syntax and MX record existence.
8. **Fill `niche` and `team_size`** from Apollo's company facts (employees, industries). Existing values are never overwritten.
9. **Rewrite the output workbook** after each company so a stop or crash never loses partial results.

### Credit safety (the most important section)
Apollo's free plan has **very limited credits**. This tool is deliberately designed to never spend more than **one credit per company**, and to **never re-spend on an already-processed company**.

- The SQLite ledger is in `data/apollo.db`. Every company's credit attempt is **committed before the Access email click**. If the process dies after this point, the company is never clicked again automatically.
- **Completed, attempting, and unknown companies are not retried automatically** on the next run.
- Every UI run is live: only the selected new companies can use a credit. The `limit` box caps the maximum.
- If Apollo shows a **challenge** (verification screen), the run stops and leaves Chrome open so you can solve it manually, then hit **Retry** in Telegram.
- The **Recovery: reset a company** control deletes a ledger row only after explicit confirmation. Use it only after checking Apollo manually, because it allows another credit to be spent later.

**This is a deliberate, responsible design**: you control exactly how many new companies to spend on per run, and the ledger guarantees you never accidentally re-spend credits.

---

## The local UI: upload / start / live progress

1. **`Start Apollo Tool.cmd`** creates the virtual environment on first launch and opens the local UI.
2. On the **first run, Chrome opens** with the persistent `apollo_profile` folder. **Log in to Apollo once.** Later runs reuse the session.
3. The UI opens on a clean upload card. Drop an `.xlsx`, `.xlsm`, `.csv`, or `.json` file, choose how many new companies to process, and press **Start run**.
4. The upload window closes automatically once the run starts. Apollo then works in normal Chrome (hidden by default) and Chrome closes itself when the run finishes. It returns automatically if a challenge or login screen stops the run; use **Show Apollo browser** to bring it back manually.

**Never close the Chrome window while a run is active** — Apollo's security widget depends on it being open.

### Desktop popup
The desktop window (Edge/Chrome `--app` mode, 560×780px, no tabs, no address bar) at `http://127.0.0.1:8765/desktop` shows only the upload card, limit buttons, and start control. It resets itself when **Start** is pressed in Telegram, closes automatically after a run begins, and can be reopened with the **Laptop** button. The app keeps its Apollo Chrome window hidden during the run and closes it when the run finishes.

---

## Telegram integration: what it notifies and why

The Telegram bot is the no-CMD option. Type `/start` once, then use the persistent buttons:

| Button | Action |
|---|---|
| 📎 Send file | Upload company spreadsheet (XLSX/CSV/JSON) |
| 🚀 Start | Pick limit (5/10/25/50/100) → run begins |
| 🖥️ Laptop | Open/raise the desktop popup |
| 📊 Status | Live progress + credits used |
| ⏹ Stop | Graceful stop after current company |
| 📄 Latest file | Re-send last output workbook |

The bot also notifies you if:
- A **challenge** appears in the Apollo Chrome window (solve it, then Retry).
- **Login is required** (open the Chrome window, log in, then Retry).
- A run **completes** with the finished workbook.
- Credits are used / remaining.

---

## Tech stack (check real dependencies)

| Dependency | Purpose |
|---|---|
| `fastapi>=0.115` | Local web UI & API |
| `uvicorn[standard]>=0.30` | ASGI server |
| `playwright>=1.49` | Persistent Chrome profile, Apollo UI automation |
| `openpyxl>=3.1` | XLSX input / output |
| `dnspython>=2.6` | MX record validation |
| `python-multipart>=0.0.9` | Form uploads |
| `jinja2>=3.1` | HTML templates (desktop window) |
| `pytest>=8.0` | Tests (46 passed: Telegram formatting, limit buttons, menu, runner state, email validation, pattern derivation, DB ops) |
| `python-dotenv>=1.0` | `.env` configuration loading |

---

## Setup & how to run

```powershell
# 1. Create venv and install dependencies
cd Apollo
powershell -ExecutionPolicy Bypass -File "Start Apollo Tool.cmd"

# 2. On first run, Chrome opens with apollo_profile. Log in to Apollo once.
#    Later runs reuse the same profile — no need to log in again.

# 3. Launch the local UI (or just run the Telegram bot)
.\.venv\Scripts\python.exe run_ui.py          # opens http://127.0.0.1:8765
# or headless:
.\.venv\Scripts\python.exe run_ui.py --no-browser

# 4. Or use the Telegram bot directly:
.\.venv\Scripts\pythonw.exe telegram_bot.py   # hidden, no console window

# 5. First chat that sends /start is paired automatically.
#    All further chats are ignored (bot is locked to one user / chat).
```

### Commands from the UI / Telegram

| Action | What happens |
|---|---|
| Upload file + choose limit | File saved, workbook parsed, run starts |
| Press **Start run** | Chrome Apollo window activates, run begins |
| Press **Status** | Live: current company, credits used, verified/guessed counts |
| Press **Stop** | Stops after current company, partial workbook saved |
| Press **Laptop** | Reopens the desktop popup (fresh upload) |
| Press **Latest file** | Sends the most recent output Excel file |
| Press **Reset company** | Deletes a ledger row (confirmation required) |

---

## Output format: the two sheets in the output Excel file

Based on the real export code (`excel_io.py`), the workbook has **two main sheets** plus a run summary:

### 1. `employees` sheet (people-first, the primary deliverable)

- **Columns** (in order): `Employee Name`, `Company Name`, `Company Members`, `Website`, `Job Title`, `Email`
- **Email is always the last column**.
- One row per discovered employee.
- Companies with no collected contacts are **intentionally omitted** from this sheet (they remain in the "All data" sheet).
- Email cells are color-coded:
  - **Green** = verified by Apollo (safe to use)
  - **Yellow** = high-confidence pattern guess (format matched a common pattern)
  - **Orange** = low-confidence / partial-name guess (masked name, unusual format)
  - **Grey** = no email collected (hover for the exact reason)
- Bold header, frozen header row, sized columns, mailto/website links.
- Color key and "Why an email may be blank" table are included at the bottom.

### 2. `All data` sheet (original upload columns + enriched columns)

- **Columns**: `Startup Name`, `Official Website`, `Employee Range`, `Exact Member Count`, `Wellfound Profile URL`, `Website Verification Status`, `Discovery Date`, `Last Checked`, `Source of Website Evidence`, `Processing Status`, `email`, `email_source`, `notes`
- Original upload columns are **preserved exactly**.
- `email` = the collected email address (or blank).
- `email_source` = `verified` (revealed by Apollo) or `guessed` (derived from pattern).
- `notes` = detailed provenance: pattern used, credit delta, Apollo facts (employees, industry, founded, revenue), domain mismatch notes, etc.

### 3. `team_emails` sheet (every collected teammate)

- **Columns**: `company`, `domain`, `full_name`, `title`, `email`, `source`, `syntax/mx validation`, `confidence`
- Lists **every** collected teammate, including masked contacts with a blank email and a note.
- Filters, frozen header, validation results, confidence labels, and color-coded skipped/low-confidence rows.

### 4. `Run summary` sheet (AT A GLANCE)

- Metrics: Mode, Input companies, Companies with collected contacts, Companies without collected contacts, Contacts listed, Emails filled (coverage), Verified emails, Pattern guesses (high/low), Contacts without email, Credits spent.
- Email colour key.
- "Why an email may be blank" table.
- How to read this workbook notes.

---

## Common issues

| Symptom | Cause | Fix |
|---|---|---|
| Popup opens repeatedly | Old code reopened it on uploads and run-start detection | Fixed: **Start** opens it once; it closes after a run begins. Use **Laptop** to reopen. |
| "Login required" immediately | Chrome profile not logged in | Open the Apollo Chrome window, log in, then retry. |
| Challenge stops run | Apollo verification screen | Solve in the restored Chrome window, then **Retry** in Telegram or popup. |
| No workbook delivered | Run failed before finish | Check `logs/telegram_bot.log`; latest successful export is in `exports/`. |
| Telegram bot unresponsive | Multiple bot instances | `taskkill /f /im pythonw.exe` then restart via shortcut or `pythonw.exe telegram_bot.py`. |

---

## Environment (`.env`)

Copy `.env.example` to `.env` and fill in the token. Apollo credentials are **not stored here** — you log in once in the Chrome window.

```ini
# Apollo profile and storage
APOLLO_PROFILE_DIR=apollo_profile
APOLLO_DB_PATH=data/apollo.db
APOLLO_EXPORTS_DIR=exports
APOLLO_HEADLESS=0          # 1 = headless Chrome (no desktop popup); 0 = with popup
APOLLO_PEOPLE_PAGES=2     # How many free people pages to read per company

# BotFather token. Keep it local; never commit this file.
TELEGRAM_BOT_TOKEN=

# Optional: lock the bot to one Telegram chat. Leave blank to pair the first /start.
TELEGRAM_ALLOWED_CHAT_ID=
```

---

## Closing note

Personal / educational project, only automates my own logged-in account's normal actions, never bypasses or evades any security check — stops and alerts me on a challenge. Not affiliated with Apollo.