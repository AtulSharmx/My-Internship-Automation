# Wellfound Startup Discovery Agent

A local-first agent that finds **real 1-10 employee startups** on Wellfound
through your own visible, signed-in browser session, verifies their official
websites, and gives you a complete Telegram control panel for the whole job.

What it does:

- opens a normal Chrome window with your own profile (no stealth, no
  fingerprint spoofing, no proxy rotation, no CAPTCHA solving),
- applies Wellfound's own **1-10 employees** filter and reads the rendered
  results, scrolling/paging until the requested number of profiles is found,
- visits every eligible company profile, extracts the **official website**
  (never a social profile, never an invented domain),
- verifies each website with one polite HTTP request and records the
  evidence,
- stores everything in SQLite with a per-lead processing state
  (`discovered` / `processing` / `verified` / `incomplete` / `failed` /
  `skipped_duplicate`), de-duplicated across runs,
- runs the scan in a **background job manager** with checkpoints, so Telegram
  stays responsive and a pause, a stop, a crash or a restart never loses work,
- exports Excel / CSV / JSON / run-summary files straight into the chat.

Additional data sources are still supported: permitted open datasets with
published headcounts (DBpedia), manual imports (CSV, TSV, JSON, JSONL, XLSX,
or a saved HTML page) and allow-listed public URLs.

The legacy internship application bot is preserved in `wfbot/`. It is **not**
started by the new entry point and its `data/applications.db` is untouched.

## Important compliance decision

Wellfound's terms prohibit automated harvesting, and public company pages have
returned `403`/robots restrictions. This project therefore does **not** contain
browser stealth, CAPTCHA solving, login automation, fingerprint spoofing,
proxy rotation, or rate-limit bypass.

The supported Wellfound path is a **visible browser in your own signed-in
profile**: you drive it, you own the account, you take the risk. If Wellfound
shows a verification or sign-in page, the scan **pauses and asks you to act**
in that window - it is never bypassed. `wellfound.com` and `angel.co` are
also always blocked by the plain-HTTP URL-discovery adapter.

## Quick start (Windows)

```powershell
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

`run.py` starts the Telegram agent. It requires `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`; the setup wizard can detect the chat id after you send the
bot a message.

If `STARTUP_WELLFOUND_BROWSER=true`, starting the agent also starts a real
Wellfound scan in the background (or resumes the one that was interrupted).
Set `STARTUP_AUTO_SCAN=false` if you only want the control panel.

Python 3.11+ is supported; development/testing here uses Python 3.14.

## Automatic start (open the laptop and it just runs)

Install the logon task **once**. After that you never open a terminal, never
run a script and never start anything by hand: switch the laptop on, sign in
to Windows, and the Telegram agent comes up on its own - silently, with no
console window, and it restarts itself if anything goes wrong.

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install_windows_task.ps1
```

The task **WellFound Intern Bot** starts `.venv\Scripts\pythonw.exe
watchdog.py`. `pythonw.exe` has no console, so nothing flashes on screen.

| What happens | When |
| --- | --- |
| The supervisor starts the agent | Every time you sign in to Windows |
| The agent is restarted after a crash | 15 seconds later, for as long as it keeps dying |
| The task restarts the supervisor itself | 1 minute after it stops, 999 times over |
| The task is started again if it was ever stopped by hand | Every 15 minutes |
| A Wellfound scan starts on its own | Right after the agent comes up (`STARTUP_AUTO_SCAN=true`) |
| The daily scan runs | Every day at `STARTUP_DAILY_RUN_AT` (09:00 by default) |

The laptop has to be on and awake; the screen does not have to be watched.
Wellfound runs in your own visible browser window, exactly as it does today.

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

The same information is in `logs\watchdog.log`; the agent's own log is
`logs\startup_agent.log`.

### Controlling it

```powershell
.\.venv\Scripts\python.exe watchdog.py stop      # switch the agent off
.\.venv\Scripts\python.exe watchdog.py start     # switch it back on
.\.venv\Scripts\python.exe watchdog.py restart   # both in one go
```

or, the same commands without typing the Python path:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\agentctl.ps1 status
powershell -ExecutionPolicy Bypass -File deploy\agentctl.ps1 stop
powershell -ExecutionPolicy Bypass -File deploy\agentctl.ps1 start
powershell -ExecutionPolicy Bypass -File deploy\agentctl.ps1 uninstall
```

`stop` writes `data\stop_agent`; the supervisor then idles instead of
restarting the agent, and the task stays quietly registered. `start` removes
that marker and the agent comes back within a few seconds. Deleting the task
(`uninstall`) removes the autostart completely.

### Moving the project folder

Everything is resolved relative to the script, so the folder can be moved
anywhere. Run `deploy\install_windows_task.ps1` once more afterwards so the
task picks up the new path.

## Telegram control panel

The persistent keyboard has three buttons. Everything else is gone.

| Button | Action |
| --- | --- |
| 🚀 Start Scan | Starts the background scan immediately with the saved settings. |
| ⏹ Stop | Stops the active scan; everything collected so far is kept. |
| 📥 Download | Opens the two download options. |

The Download screen has exactly two options:

| Button | File |
| --- | --- |
| 📄 Download this file | One Excel file with all data collected so far (verified + incomplete leads). |
| 🗂 Download all past | A ZIP with every report already saved in the exports folder. |

While a scan runs, the chat shows a single status word: **🏃 Running**,
**⏳ Stopping**, **⏹ Stopped**, **✅ Done**, **❌ Failed**, or **🔐 Verify**
when Wellfound asks for a manual check. Complete the check in the visible
browser window and tap **🚀 Start Scan** again to continue that run.

Useful commands: `/start` sends the welcome message with the three-button
keyboard again; `/scan`, `/stop`, and `/download` do the same as the buttons.

Only the configured chat is authorized. The token is not included in logs,
messages, or exports. The new three-button keyboard is sent once after this
UI change; routine restarts do not resend it.

## Target-size policy

The default target is `1-10` employees and is configurable with
`STARTUP_TARGET_SIZE`.

- **Confirmed**: an explicit range is fully inside the target (for example
  `1-10`, `2-5`, or `5` when the target is 1-10).
- **Partial**: the range overlaps the target but is not fully inside it, or is
  open-ended (`1-20`, `10+`). These are kept separately for review and are not
  counted as confirmed matches.
- **Outside**: an explicit range is fully outside the target (`11-50`).
- **Unknown**: missing or unparseable size. This is never promoted to a match.

Employee counts from page text require nearby context (employee/team/headcount)
so dates such as `2019-2020` are not mistaken for headcounts. Conflicting sizes
from repeated observations are retained in notes and the record is moved to
`Needs review` rather than silently choosing one.

## Data fields

The report exports (Excel / CSV / JSON) use one consistent schema:

`Startup Name`, `Official Website`, `Employee Range`, `Exact Member Count`,
`Wellfound Profile URL`, `Website Verification Status`, `Discovery Date`,
`Last Checked`, `Source of Website Evidence`, `Processing Status`.

- `Exact Member Count` stays **blank** unless the number was independently
  verified; Wellfound publishes a range, never an exact count.
- `Website Verification Status` is one of `verified`, `redirected`,
  `unreachable`, `missing`, `blocked` or `not_checked`.
- `Source of Website Evidence` records where the domain came from and what the
  check saw (for example `Wellfound company profile · HTTP 200 at https://…`).
- `Processing Status` is the per-lead state: `discovered`, `processing`,
  `verified`, `incomplete`, `failed` or `skipped_duplicate`.

Internal fields (`record_id`, `validation_status`, `completeness`, `notes`, …)
stay in the full database dump (`startups-*.csv/json/xlsx`) and never appear
in the report files. Session data, cookies and the bot token are never
exported.

Missing values are left blank or marked `Unknown`; they are never invented.
Original industry labels and descriptions are retained, while `niche` is a
separate normalized value.

## Deduplication and IDs

- Internal IDs are deterministic: `co_` plus a hash of the canonical Wellfound
  profile URL, source id, or normalized website host/name, with a content hash
  fallback.
- Strong keys are canonical profile URL, source id, and official website host.
  Content fingerprints make repeated imports idempotent.
- A similar name alone never merges records. Same-name/same-location records
  without a strong key are flagged as possible duplicates and kept separate.
- URL variants, tracking parameters, `www`, fragments, default ports, and
  redirects are normalized. A redirect to a blocked/non-allowlisted host is
  refused.
- Every source observation is retained in `company_sources` with first/last
  seen timestamps; the original discovery timestamp is not overwritten.
- A company that was already verified is **skipped** on the next run
  (`skipped_duplicate`) and is never exported as a new lead again.

## Exports and storage

- SQLite database: `data/startups.db` (new; the legacy
  `data/applications.db` is preserved).
- Imports: `imports/` (template at `templates/companies_template.csv`).
- Exports: `exports/`; filenames contain a timestamp and a unique suffix, so no
  earlier export is overwritten.
- Formats: UTF-8 CSV with a BOM for Excel compatibility, dependency-free OOXML
  `.xlsx` (frozen header row, auto-filter, clickable links, readable column
  widths), and JSON with `job_urls` as a real array.
- Report scopes: **new verified leads of the latest run**, **all verified
  leads (master)**, **incomplete leads**, and **run history/statistics**.
- An empty report is never written, so a failed scan cannot overwrite a valid
  earlier report with empty output.
- Telegram receives the file itself (`sendDocument`). A CSV that is too large
  for Telegram is split into parts; anything else that is too large is
  explained instead of being sent as a broken link.

## Getting Wellfound data (your own browser) and the startup sheet

Wellfound's terms do not allow automated harvesting, so this is **your call
and your account**: the collector drives a **visible, normal Chromium window
with your own signed-in session** (`browser_profile/`), applies Wellfound's own
**"1-10 employees"** filter, and then opens each matching company profile to
read its official website.

It does **not** spoof the browser, rotate proxies, solve CAPTCHAs or defeat a
login. If Wellfound shows a verification page, the run **pauses** and asks you
to finish it yourself in the window. Pages are paced like a person reading
them, and every run is bounded.

```powershell
# one-off run (opens the browser, collects, closes)
.\.venv\Scripts\python.exe -m startup_agent.cli scan --max 25

# or let it run every time you start the agent
# .env:  STARTUP_WELLFOUND_BROWSER=true
#        STARTUP_AUTO_SCAN=true
.\.venv\Scripts\python.exe run.py

# build the sheet
.\.venv\Scripts\python.exe -m startup_agent.cli sheet --platform wellfound --format xlsx
```

In Telegram: **🚀 Start Scan** runs the same workflow in the background,
and **📥 Download -> 📄 Download this file** returns one Excel file with all
data collected so far.

The sheet has exactly four columns:

| Startup Name | Website | Platform | Members |
| --- | --- | --- | --- |
| Mnemio AI | https://mnemioai.com | Wellfound | 1-10 Employees |
| Rozgoo | https://rozgoo.com | Wellfound | 1-10 Employees |
| yarla | https://yarla.pro | Wellfound | 1-10 Employees |

Only companies whose published size is **fully inside** the target range
(1-10 by default) are included, so `11-50`, `51-200`, `10+` or a missing
headcount never reach the sheet.

If you would rather drive the browser completely by hand, use
`tools/wellfound_capture.py` instead - it saves the page *you* are looking at
and never touches the site on its own.

## Scraping Wellfound's public pages (the startup list)

A second, **separate** collector lives in `startup_agent/wfscrape/`. It does
not use the apply-bot, does not use the signed-in browser session and does not
touch the nightly schedule - it runs **only when you ask for it**:

```powershell
# one-off run (telegram is notified when it starts, finishes and on errors)
.\.venv\Scripts\python.exe -m startup_agent.cli scrape

# narrow it down
.\.venv\Scripts\python.exe -m startup_agent.cli scrape --pages 1 --max 20
.\.venv\Scripts\python.exe -m startup_agent.cli scrape --size 1-10 --verify

# rebuild the snapshot from the database, without scraping again
.\.venv\Scripts\python.exe -m startup_agent.cli scrape-export
```

Every run writes three files to `exports/` - `wf-scrape-<timestamp>.csv`,
`.xlsx` and `.json` - with **exactly these eight columns, in this order**:

| company_name | website | domain | team_size | niche | wellfound_profile_url | date_scraped | wellfound_company_id |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AIKOCorp | https://aikocorp.ai | aikocorp.ai | 1-10 | Cyber Security, Artificial Intelligence / Machine Learning | https://wellfound.com/company/aikocorp | 2026-10-20 | 10276427 |
| Big Leap Health | https://bigleaphealth.com | bigleaphealth.com | 1-10 | Healthcare, Health Care Information Technology | https://wellfound.com/company/big-leap-health | 2026-10-20 | 10726341 |

- `domain` is derived from `website`: bare host, lowercase, no scheme, no
  `www.`, no path.
- `niche` holds the market/industry tags Wellfound publishes for the company.
- `wellfound_company_id` is Wellfound's own company id - the stable reference
  key that de-duplicates a company across roles.
- The snapshot is **data only**. It carries no pipeline state: no status, no
  contacted flag, no lead stage. It is a clean handoff for another project.

The companies are also stored in `startups.db` under the source
`wellfound_public`, so a second run reuses the ones it already has and the
existing Telegram **Download Reports** buttons work as usual.
`scrape-export` rebuilds the files from that database without scraping.

### How it works

Wellfound renders its pages server-side and ships the whole result set as a
Next.js `__NEXT_DATA__` script holding an Apollo cache. The collector reads
**that structured data**, never the visible page text, so a redesign cannot
break it:

1. **Discovery (plain HTTP).** It walks the public role landing pages
   (`https://wellfound.com/role/r/<slug>?page=N`), which `robots.txt` allows,
   and reads each company's id, name, slug and published size.
2. **Enrichment (browser).** It opens each company profile
   (`https://wellfound.com/company/<slug>`) and reads the company's own
   external website (`companyUrl`) and its market/industry tags
   (`marketTaggings`).

Wellfound's own company-size filter is a UI filter and is **not** a URL
parameter - but the published size travels with every company, so the "1-10
employees" filter is applied exactly, never approximately.

### The role list

Wellfound has no flat company directory: it organises companies by the role
they hire for. The role list is a plain text file you can edit yourself -
`config/wf_scrape_roles.txt`:

```text
# one role slug per line -> https://wellfound.com/role/r/<slug>
software-engineer
full-stack-engineer
ai-engineer
```

Add or remove lines, then run `scrape` again. Nothing else changes.

### One row per company

Companies are de-duplicated by Wellfound's **company id**, not by job posting
id, so a company hiring for five roles appears exactly once.

### The security check

Wellfound puts a Cloudflare check in front of the company profile pages. It is
a challenge, not a login, and a real browser clears it by itself. The
collector waits for it and **stops and reports** if it does not clear - it
never spoofs a fingerprint, rotates a user agent, solves a challenge or logs
in to anything.

A brand-new browser profile has no clearance cookie yet, so warm it up once
in a visible window:

```powershell
.\.venv\Scripts\python.exe -m startup_agent.cli scrape-prime
```

After that every run stays headless. If you already have a profile with a
clearance cookie, point the scraper at it instead:

```powershell
.\.venv\Scripts\python.exe -m startup_agent.cli scrape --profile browser_profile
```

The scraper uses its own profile (`data/wfscrape_profile/`) by default, so it
never collides with the running agent, which owns `browser_profile/`.

### Compliance

Wellfound's `robots.txt` explicitly invites crawlers ("Hey human, We like
robots too") and allows the role and company paths this collector reads. The
collector honours it: it checks every URL against the policy, paces its
requests, uses an honest user agent, sends no cookies or credentials, and
stops on `401`/`403`/`429` instead of retrying around them. Wellfound's terms
still prohibit automated harvesting, so this is your call - it reads public
pages and acts on no account.

## Getting real companies (dataset discovery)

The agent can pull **real** company records from a permitted open dataset.
DBpedia publishes company headcounts (`dbo:numberOfEmployees`, derived from
Wikipedia infoboxes), so the 1-10 employee filter works on genuine data
instead of guesses.

```dotenv
STARTUP_ENABLE_DATASETS=true
STARTUP_DATASET_SOURCES=dbpedia
STARTUP_ALLOWED_DOMAINS=dbpedia.org
STARTUP_DATASET_LIMIT=250
```

```powershell
.\.venv\Scripts\python.exe -m startup_agent.cli discover --datasets
```

What this does, in order:

1. fetches `robots.txt` for the dataset host and obeys it (DBpedia allows
   `/sparql`; a host that disallows it is refused, not worked around);
2. runs one bounded SPARQL query that selects companies whose published
   headcount falls inside `STARTUP_TARGET_SIZE`;
3. stores each record with its name, website, employee count, industry,
   location, short description, source URL and source id;
4. never invents a missing field - a blank stays blank.

The dataset host must be allow-listed explicitly, requests use an honest user
agent with a per-host rate limit, and a `401`/`403`/`429` stops the run instead
of being retried around. DBpedia data is CC BY-SA / CC0; every record keeps
its `dbpedia.org` source URL for attribution.

Because the published headcount is a Wikipedia infobox value, it can be
dated - the note field says so on every record.

## Permitted URL discovery

URL discovery is **off by default**. To enable it:

```dotenv
STARTUP_ENABLE_URL_DISCOVERY=true
STARTUP_ALLOWED_DOMAINS=example.com,another.example
STARTUP_SEED_URLS=https://example.com/startups
STARTUP_MIN_REQUEST_INTERVAL_SECONDS=3
STARTUP_DAILY_RUN_AT=20:00
```

The adapter:

- requires an exact allow-listed host or subdomain;
- fetches and obeys `robots.txt`, including after redirects;
- uses an honest user agent, timeouts, bounded page size, and per-host rate
  limiting;
- stops on `401`/`403`/`429`, login screens, CAPTCHA/challenge text, or a
  redirect outside the allow-list;
- never sends cookies or credentials and does not bypass access controls;
- keeps the URL queue resumable across runs with attempt limits.

If a page cannot be fetched legitimately, use the manual import path. A failed
or empty discovery run is reported as such, never as success.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

The suite is hermetic: 189 tests, no network and no live Wellfound login
required. It covers the strict 1-10 filter, website verification (mocked HTTP
transport), cross-run dedup, checkpoint recovery, pause/resume **while a
profile is being read**, graceful stop, concurrent-scan prevention, empty
results, Excel/CSV/JSON generation, Telegram report delivery, unauthorized
callbacks, invalid settings, browser errors with the retry limit, restart
recovery, browser-ownership coordination, and the completed/failed state
transitions - plus the legacy bot's own suite.

## Project layout

```text
run.py                       new entry point (single-instance lock)
watchdog.py                  silent supervisor: keeps the agent alive at logon
setup_discovery.py           new configuration wizard
setup.ps1                    Windows dependency/setup helper
startup_agent/
  app.py                     composition root, logging, scheduler, auto-scan
  config.py                  STARTUP_* configuration
  jobs.py                    background scan manager, settings, checkpoints
  verify.py                  independent website verification (one GET)
  models.py                  records, summaries, statuses
  normalize.py               URLs, sizes, identity, validation
  store.py                   SQLite, dedup, scan jobs and their checkpoints
  discovery.py               import/sync/discover/export orchestration
  export.py                  CSV/JSON/dependency-free XLSX + report schemas
  telegram.py                the Telegram control panel
  cli.py                     local command-line interface
  sources/
    files.py                 CSV/JSON/JSONL/XLSX/HTML import
    datasets.py              permitted open datasets (DBpedia SPARQL)
    extract.py               JSON-LD/meta/anchor extraction
    htmlparse.py             small dependency-free HTML tree
    wellfound.py             local saved-Wellfound-page parsing only
    wellfound_browser.py     Wellfound collection through your own browser
    web.py                   allowlisted public URL adapter
  wfscrape/                 NEW: the public-page startup collector
    apollo.py                Wellfound's embedded Next.js/Apollo data
    roles.py                 the editable role list
    discover.py              phase 1: the public role pages (plain HTTP)
    enrich.py                phase 2: the company profiles (browser)
    export.py                the four-column CSV report
    runner.py                orchestration + Telegram notifications
config/wf_scrape_roles.txt  the role list you edit
tools/browser_capture.py     manual visible-browser helper
tools/wellfound_capture.py   manual Wellfound capture (you drive, it reads)
templates/                   CSV template and synthetic example
wfbot/                       preserved legacy internship bot
data/applications.db         preserved legacy data
docs/legacy_*                preserved legacy documentation/config example
```

## Legacy bot and deployment

The old internship workflow, its tests (96 passing), `wfbot/` package,
`setup_wizard.py`, `browser_profile/`, scheduled-task wrapper, and
`data/applications.db` remain in place. `run.py` no longer imports `wfbot`, so a
logon-triggered scheduled task now starts the discovery agent instead of the
internship bot.

The `deploy/` folder holds the local Windows task (used by the new agent) and
the preserved notes for the old remote deployment. No AWS resources,
permissions, or charges were created. The new
agent is local-first and needs only Python, SQLite, `httpx`, and
`python-dotenv`; Playwright is optional and only used by the manual browser
helper / legacy bot. See `docs/legacy_internship_readme.md` and
`docs/legacy_env.example` for the old setup and deployment notes.

## Known limitations

- Wellfound automation runs in **your own visible browser session** and pauses
  for you on a verification page; it never bypasses one. Wellfound's terms
  prohibit automated harvesting, so the risk and the account are yours.
- Only one browser may use `browser_profile/` at a time. If a Chrome window
  already has it open, the scan fails with "close any Chrome window that is
  using the profile".
- A company is only `verified` when its site answers with a real HTML page on
  the published domain. A site that redirects to a different domain stays
  `incomplete` and is never exported as verified.
- The exact member count is never invented: Wellfound publishes a range, so
  the *Exact Member Count* column stays blank.
- DBpedia headcounts are published infobox values and can be dated; the record
  note says so. The live endpoint is occasionally slow, so a run can report a
  timeout instead of data - re-run it, or import an export instead.
- Generic HTML directory pages may yield only a profile-level record; CSV/JSON
  is the reliable format for multi-company data.
- The XLSX writer supports text/numeric cells and the simple workbook shape it
  produces; complex Excel formulas/styles are not imported.
- Telegram live behavior requires a valid token/chat and a running process; the
  test suite covers panels and callbacks without contacting Telegram.
- Timezone display uses the machine's local timezone. Python's `tzdata` package
  may be needed on some Windows installations for named zones; the agent itself
  uses system local time.
