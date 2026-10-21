# MASTER BRIEF — Wellfound Startup Discovery Agent

> Single-file, copy-pasteable reference for the whole project.
> Copy this entire file wherever you need the complete picture.

---

## 1. WHAT THIS PROJECT IS

A **local-first agent that finds real 1–10 employee startups** on Wellfound
through your own visible, signed-in browser session, verifies their official
websites, and exposes the whole job through a Telegram control panel plus
timestamped CSV / Excel / JSON / run-summary exports.

It is a **rewrite** of an old internship-application bot. The old bot
(`wfbot/`) is **preserved untouched** but no longer started.

| | |
|---|---|
| Package | `startup_agent/` (independent of `wfbot/`) |
| Entry point | `run.py` (single-instance lock at `data/startup_agent.lock`) |
| Database | SQLite at `data/startups.db` |
| Legacy DB | `data/applications.db` — **preserved, never touched** |
| Tests | **189 passing** (93 new + 96 legacy smoke) |
| Live data | **207 real companies** (177 DBpedia + 30 Wellfound), 206 confirmed 1-10 |
| Runtime deps | `httpx`, `python-dotenv` (Playwright drives the visible browser) |

---

## 1A. THE WELLFOUND SHEET (the deliverable)

Exactly four columns, one row per confirmed 1-10 company:

| Startup Name | Website | Platform | Members |
| --- | --- | --- | --- |
| Mnemio AI | https://mnemioai.com | Wellfound | 1-10 Employees |
| Rozgoo | https://rozgoo.com | Wellfound | 1-10 Employees |
| yarla | https://yarla.pro | Wellfound | 1-10 Employees |
| Chromatic AI | https://chromaticlabs.co | Wellfound | 1-10 Employees |
| Wubble.AI | https://wubble.ai | Wellfound | 1-10 Employees |
| Langslide | https://langslide.ai | Wellfound | 1-10 Employees |
| Pruning Labs | https://pruninglabs.com | Wellfound | 1-10 Employees |
| Scratchly | https://scratchlycrm.app | Wellfound | 1-10 Employees |
| Travelaux | https://travelaux.com | Wellfound | 1-10 Employees |
| Hoslr AI | https://hoslr.com | Wellfound | 1-10 Employees |

*(a verified live run produced 29 such rows in one pass)*

**How the data arrives - your own visible browser:**

```powershell
.\.venv\Scripts\python.exe -m startup_agent.cli collect --max 25   # one-off
.\.venv\Scripts\python.exe run.py                                  # at every start
#   .env: STARTUP_WELLFOUND_BROWSER=true, STARTUP_WELLFOUND_MAX=25

.\.venv\Scripts\python.exe -m startup_agent.cli sheet --platform wellfound --format xlsx
```

The collector (`startup_agent/sources/wellfound_browser.py`):

1. opens a **visible** Chromium with your `browser_profile` (your sign-in)
2. opens `https://wellfound.com/jobs`, opens the filter drawer, switches off
   every other company size, switches on
   **`form-input--companySizes--SIZE_1_10`** and presses "View results"
3. reads the startup cards (`[data-test="StartupResult"]`) -> name, headcount,
   `/company/<slug>` link
4. opens each company profile and reads the **official website** from the
   `Website` row (the `websiteLink` button), the headcount and the tagline
5. hands the records to the agent for dedup, storage and export

**What it will not do**: spoof the browser, rotate proxies, solve CAPTCHAs,
defeat a login or ignore robots. A verification page **pauses the run** and
asks you to finish it yourself. Pages are paced like a person reading them.

**The filter is enforced twice**: the site filter selects 1-10, and the
verified report only accepts `size_match = 1 AND lead_status = 'verified'` -
so `11-50`, `51-200`, `10+` and missing headcounts can never appear.

Telegram: **🚀 Start new scan** (runs the collection in the background) and
**📥 Download reports -> 📋 Startup sheet** (builds the file).

---

## 1B. THE PUBLIC-PAGE SCRAPER (new, separate tool)

A **second, independent** collector for public pages. It is not the apply-bot,
not the signed-in collector, and not on the nightly schedule - it runs only
when a human asks for it.

```powershell
.\.venv\Scripts\python.exe -m startup_agent.cli scrape-prime   # once: clear the check
.\.venv\Scripts\python.exe -m startup_agent.cli scrape          # the run
.\.venv\Scripts\python.exe -m startup_agent.cli scrape --pages 1 --max 20
.\.venv\Scripts\python.exe -m startup_agent.cli scrape --profile browser_profile
```

**Output**: `exports/wf-scrape-<timestamp>.{csv,xlsx,json}`, one row per company,
with **exactly eight columns in this order** - a clean data snapshot, with no
pipeline state (no status, no contacted flag):

`company_name | website | domain | team_size | niche | wellfound_profile_url | date_scraped | wellfound_company_id`

- `domain` is derived from `website` (bare host, lowercased, no scheme, no
  `www.`, no path).
- `wellfound_company_id` is Wellfound's company id - the reference key.
- The records also land in `startups.db` under the source `wellfound_public`;
  `cli scrape-export` rebuilds the files from that database without scraping.

| Piece | Detail |
|---|---|
| Package | `startup_agent/wfscrape/` (apollo, roles, discover, enrich, export, runner) |
| Discovery | plain HTTP over `/role/r/<slug>?page=N` (robots.txt allows it, no challenge) |
| Enrichment | Playwright headless over `/company/<slug>` |
| Structured data | the embedded Next.js `__NEXT_DATA__` / Apollo cache - never visible HTML |
| Company identity | Wellfound's **company id** (`StartupResult:<id>`), so one row per company |
| Size filter | exact - `companySize == SIZE_1_10`; the site's own filter is UI-only |
| Website | `companyUrl` on the profile page (the listing page has no website field) |
| Niche | `marketTaggings` -> the site's own market/industry tags |
| Role list | `config/wf_scrape_roles.txt` - one slug per line, editable, `#` comments |
| Telegram | start, finish + unique-company count, errors, and the CSV as a document |
| Security check | waits for the challenge; if it does not clear the run **stops** - never bypassed |
| Warm-up | `scrape-prime` opens a visible browser once so the profile holds the clearance cookie |

**Never** does: log in, spoof a fingerprint, rotate a user agent, solve a
challenge, or act on any account. It reads public pages only.

---

## 2. COMPLIANCE DECISION (the most important part)

Wellfound's terms prohibit automated harvesting, and their public pages return
`403` / robots restrictions to anonymous HTTP clients. The project therefore
draws a hard line:

- ❌ no browser stealth / fingerprint spoofing
- ❌ no CAPTCHA solving or bypass
- ❌ no automated login (you sign in yourself)
- ❌ no proxy rotation or rate-limit bypass
- ❌ no robots.txt bypass (not even via redirects)
- ❌ no headless scraping of Wellfound's HTML over plain HTTP

What it **does** have is a **browser collector in your own visible, signed-in
session** (`startup_agent/sources/wellfound_browser.py`): it opens a normal
Chromium window, uses Wellfound's own **"1-10 employees"** filter, reads the
startup cards and opens each company profile to get its website. It is paced
like a person, it is bounded by a configurable maximum, and **a verification
page stops the run** instead of being worked around. That is your account and
your decision to make.

For the plain-HTTP adapters, `wellfound.com` and `angel.co` are **permanently
blocked** in the URL adapter, in the dataset adapter, in the Telegram layer
and in the setup wizard.

### What IS allowed (and implemented)

| Source | How it is used | Policy |
|---|---|---|
| **Wellfound (your browser)** | visible Chromium, your session, site's own 1-10 filter, profile visits | your account; stops at any challenge |
| **DBpedia** (open dataset) | One bounded SPARQL query; `robots.txt` allows `/sparql` | real published headcounts |
| **Manual import** | CSV / TSV / JSON / JSONL / XLSX / saved HTML | your own data, your own export |
| **Saved page** | Parse HTML you saved from your own browser | never makes a network request |
| **Allow-listed URLs** | public pages whose terms + robots permit | explicit allow-list, per-host rate limit |

### The browser collector, in detail

| Piece | Detail |
|---|---|
| Browser | `chromium.launch_persistent_context`, **headless=False**, your `browser_profile` |
| Entry | `https://wellfound.com/jobs` |
| Filter | drawer toggle `SearchBar-ToggleFilterControlPanelButton`, other sizes unchecked, `form-input--companySizes--SIZE_1_10` checked, "View results" |
| Verify | `[data-test^="ActiveFilterSummary-FilterButton--"]` must show "Clear 1-10 employees" |
| Cards | `[data-test="StartupResult"]` -> `h2` name, `N-M Employees` chip, `a[href^="/company/"]` |
| Profile | card `h2` = company name (profile title only used when the card shows a URL), `Website` row -> `button[class*="websiteLink"]` = official site, `N-M Employees` from the page text, `h2` = tagline |
| Pacing | 1.2-2.4 s between profile pages (configurable), 1.2-2.2 s between scrolls |
| Bounds | the run's max-profile budget (settings panel, 1-200) |
| Pagination | scroll to the bottom + "Next / Load more", the 1-10 filter is re-checked after every page |
| Website | the `Website` row / `websiteLink` button, else the first external link that is not a social/directory profile |
| Verification | one HTTP GET per site (`startup_agent/verify.py`): 2xx + HTML on the published domain = `verified` |
| Stop conditions | CAPTCHA/login text -> **pause and ask the owner**, never bypassed |

### Verified robots results (live checks)

| Host | `robots.txt` | Verdict |
|---|---|---|
| `query.wikidata.org` | `Disallow: /sparql` | **rejected** — SPARQL not used |
| `www.wikidata.org` | allows `Special:EntityData/*` | allowed, but P1128 coverage was 0 for small firms |
| `dbpedia.org` | allows `/sparql` | **used** (971 small firms with headcount + website) |
| `en.wikipedia.org` | allows `/wiki/` | used only for manual capture |
| `www.ycombinator.com` | allows `/`, blocks `/companies?*` | not used |

---

## 3. FILE MAP (with sizes)

```
run.py                        54    new entry point + single-instance lock
setup_discovery.py             6    wrapper -> setup_wizard.main()
README.md                    400    full documentation
.env.example                  72    every config key, documented

startup_agent/
  app.py                     260    composition root, logging+redaction, scheduler, auto-scan
  cli.py                     430    init / import / discover / export / scan / runs / report / settings
  config.py                  320    STARTUP_* settings, validation, safe_summary
  discovery.py               440    orchestration: import, sync, dataset, URL, export
  export.py                  800    CSV/JSON/stdlib OOXML XLSX + the report schemas
  jobs.py                    900    NEW: background scan manager, settings, checkpoints
  models.py                  330    Company, statuses, Import/Run summaries
  normalize.py               609    URLs, sizes, identity, dedup keys, validation
  setup_wizard.py            219    interactive config, preserves legacy .env keys
  store.py                  1300    SQLite schema, dedup, scan jobs + checkpoints
  telegram.py               1600    the Telegram control panel
  verify.py                  260    NEW: independent website verification (one GET)
  sources/
    datasets.py              312    NEW: DBpedia SPARQL adapter (real data)
    extract.py               478    JSON-LD / meta / anchor extraction
    files.py                 481    CSV/TSV/JSON/JSONL/XLSX/HTML import
    htmlparse.py             140    small dependency-free HTML tree
    web.py                   357    allow-listed URL adapter + robots engine
    wellfound.py             190    local saved-Wellfound-page parsing only
    wellfound_browser.py     520    NEW: Wellfound collection via your own browser

tests/
  test_startup_agent.py     1100    49 synthetic/fixture tests (no network)
  test_scan_control.py       900    NEW: 43 scan/verification/control-panel tests
  test_smoke.py             1643    96 legacy tests (preserved)

tools/browser_capture.py       70    opens a visible browser for manual saving
tools/wellfound_capture.py    190    NEW: manual Wellfound capture (you drive, it reads)
templates/companies_template.csv   recommended import header
imports/README.md                     how to drop files
docs/legacy_internship_readme.md     preserved old docs
wfbot/, data/applications.db, browser_profile/   PRESERVED LEGACY
```

---

## 4. THE FIVE FLOWS

### Flow A — Dataset discovery (the "real data" path)

```
Discover Startups (Telegram)  |  cli discover --datasets
        │
        ▼
DiscoveryService.discover_datasets()
        │
        ▼
DbpediaSource.fetch(limit)
   ├─ _check_policy():  allow-list?  robots.txt?  blocked host?
   ├─ _query_json():    honest UA, per-host rate limit, timeout,
   │                   401/403/429 = STOP, 5xx = retry, size cap
   └─ build_query():    FILTER(?e >= 1 && ?e <= 10)  ← target range baked in
        │
        ▼
Company objects (name, website, employee_count, industry, location,
description, source_url, source_id, notes)
        │
        ▼
evaluate_company() → classify size → store.upsert_company()
        │
        ▼
RunSummary → Telegram panel + runs table
```

### Flow B — Manual import

```
Telegram document  |  imports/ folder (Sync)  |  cli import <path>
        │
        ▼
FileImporter.parse()  → CSV / TSV / JSON / JSONL / XLSX / HTML
        │                  (wellfound saved page → parse_wellfound_saved)
        ▼
company_from_mapping()  (column aliases, no invented values)
        │
        ▼
evaluate_company() → upsert_company() → ImportSummary
        │
        ├─ content hash already seen? → "already imported", no duplicates
        └─ failed/empty import? → still recorded (failed-to-process history)
```

### Flow C — URL discovery (allow-listed only)

```
seed URL / user URL
   → check_url(): wellfound/angel.co blocked? host allow-listed?
   → enqueue_url(): done→reset, failed→keep attempts, blocked→STAYS blocked
   → pending_urls(limit, max_attempts)
   → fetch_companies(): robots → GET → manual redirect loop
        (each redirect target re-checked against robots BEFORE fetching)
   → extract_companies() → upsert → mark_queue(done/failed/blocked)
```

### Flow D — Export

```
Export Data (Telegram)  |  cli export --format all [--confirmed-only]
        │
        ▼
ExportService.export(kind)
   ├─ list_companies(only_confirmed = size_match=1 AND status NOT IN duplicate/failed)
   ├─ _unique_path(): startups-YYYYMMDD-HHMMSS[-N].<ext>   ← never overwrites
   ├─ CSV  : utf-8-sig (Excel-safe BOM), full 24 columns
   ├─ JSON : job_urls as a real array, size_match as bool
   └─ XLSX : stdlib zip + OOXML, inline strings (no formulas, no openpyxl)
        │
        ▼
Telegram: sends the files + the on-disk path
```

### Flow E — Telegram control panel (background job manager)

```
run.py → lock → Config → Store → DiscoveryService → ScanManager → Telegram
    → jobs.recover()          an interrupted run becomes resumable
    → startup_notice()        sent ONCE per database (meta flag)
    → _auto_scan()            the real Wellfound run starts in the background
    → long-poll loop          backoff 1→60s, per-update error isolation

A scan NEVER runs inside a Telegram handler:

  Telegram "Start new scan"
        │  (25/50/100/Custom · new only or new+retry · confirm)
        ▼
  ScanManager.start()          one active run at a time (JobBusy otherwise)
        │  creates scan_jobs + scan_items rows (the checkpoint)
        ▼
  background asyncio task
        ├─ collector.discover_profiles()   1-10 filter, scroll/page
        ├─ collector.inspect_profile()     per profile
        ├─ WebsiteVerifier.verify()        one polite HTTP GET
        ├─ store.upsert_company()          dedup + lead_status
        └─ store.scan_items update         checkpoint after every profile
        ▼
  completed / stopped / failed / paused(auth)
```

Pause blocks the worker between profiles (the browser stays open); stop keeps
every record and marks the run *stopped*; a crash or restart leaves the
checkpoint intact and the run can be resumed.

---

## 5. TELEGRAM CONTROL PANEL

Six-button reply keyboard (2 columns, persistent):

```
🚀 Start New Scan │ 📊 Live Status
📥 Download Reports│ ⚙️ Scan Settings
🗂 Recent Runs     │ ❓ Help
```

Inline main menu (nine actions, context aware):

| Action | Behaviour |
|---|---|
| 🚀 Start new scan | asks 25/50/100/Custom, new-only or new+retry, shows a confirmation, starts the background job and returns the run ID |
| ⏸ Pause / ▶️ Resume | really stops/continues the worker at the next safe point |
| ⏹ Stop scan | asks first, then stops, saves and marks the run *stopped* |
| 📊 Live status | the real counters, edited in place in one message |
| 📥 Download reports | Excel / CSV / JSON / run summary, sent as documents |
| ⚙️ Scan settings | max profiles, delay, retries, timeout, retry-incomplete, auto-report, notifications - validated and stored in SQLite |
| 🗂 Recent runs | id, time, status, inspected, new verified, errors, export availability; open summary, download, resume |
| 🔁 Retry incomplete leads | only eligible incomplete records; reports recovered vs remaining |
| ❓ Help | how every control behaves |

Report scopes: newest verified leads of the latest run · all verified leads
(master) · incomplete leads · run history. The 4-column startup sheet is still
one button away.

Commands: `/start /scan /status /pause /resume /stop /cancel /help`

Safety: only the configured chat is authorised; the token never appears in
logs, panels or exports (URL errors are sanitised, `SecretFilter` scrubs the
log handlers).

---

## 6. DATA MODEL

### Tables

| Table | Purpose |
|---|---|
| `companies` | one row per company (24 exported columns + `raw_json` + the verification fields) |
| `company_keys` | identity keys → record_id (dedup) |
| `company_sources` | every source observation, first/last seen |
| `imports` | one row per import attempt (incl. failures) |
| `import_errors` | per-row import errors |
| `runs` | one row per discovery run |
| `scan_jobs` | one row per background scan: status, counters, settings, checkpoint flag |
| `scan_items` | the per-profile work queue of a run (status, attempts, result) |
| `queue` | URL queue: status, attempts, last_error |
| `exports` | export history |
| `meta` | key/value (paused, telegram_offset, startup_notice_sent, last_daily_run_date, scan_settings, browser_owner) |

### Report schema (Excel / CSV / JSON - identical)

```
Startup Name, Official Website, Employee Range, Exact Member Count,
Wellfound Profile URL, Website Verification Status, Discovery Date,
Last Checked, Source of Website Evidence, Processing Status
```

`Exact Member Count` is blank unless independently verified. Debug fields and
session data stay in the database, never in a report.

### Full dump columns (the legacy `startups-*` export)

```
record_id, name, website, platform, employee_count, employee_min, employee_max,
size_status, size_match, industry, niche, description, wellfound_url,
job_urls, location, source_name, source_url, source_id, discovered_at,
last_checked_at, status, validation_status, completeness, duplicate_of, notes,
website_status, website_evidence, website_checked_at, website_final_url,
member_count_exact, lead_status, retry_count, last_run_id, discovered_run_id
```

### Lead status (per lead, the control-panel state)

| Status | Meaning |
|---|---|
| `discovered` | seen, not yet eligible or not yet checked |
| `processing` | being read right now |
| `verified` | confirmed 1-10 **and** the website answered with a real page |
| `incomplete` | confirmed 1-10, but the website is missing/unverified - retriable |
| `failed` | the profile could not be read after the retry limit |
| `skipped_duplicate` | already known and verified - never a new lead again |

### Website status

| Status | Meaning |
|---|---|
| `not_checked` | no check has run yet |
| `verified` | HTTP 2xx + HTML on the published domain (or a subdomain) |
| `redirected` | the site redirects to a different domain - stays incomplete |
| `unreachable` | timeout, DNS, TLS or 4xx/5xx |
| `missing` | the profile lists no website |
| `blocked` | a social/directory profile or a private address - not an official site |

### Statuses (tracked separately)

| Status | Meaning |
|---|---|
| `discovered` | seen, not yet validated |
| `validated` | size confirmed + a source URL present |
| `needs_review` | size unknown/partial, or conflicting sizes |
| `duplicate` | possible duplicate (never auto-merged) |
| `failed` | could not be processed |

### Size policy (never guesses)

| Input | Result |
|---|---|
| `1-10`, `2-5`, `5` | **confirmed** (fully inside target) |
| `1-20`, `10+`, `fewer than 20` | **partial** → review, not confirmed |
| `11-50` | **outside** |
| missing / `Unknown` | **unknown** → never a match |
| `Founded 2019-2020` | rejected (year range, needs employee context) |

### Identity & dedup

- **Strong keys**: canonical Wellfound profile URL · source id · website host
- **Weak signal**: same name + same location → flagged `duplicate`, never merged
- **Content fingerprint** makes repeated imports idempotent
- `record_id` = `co_` + sha256 of the strongest key (stable across runs)
- Conflicting sizes → notes + `needs_review` (never silently overwritten)

---

## 7. CONFIGURATION (`.env`)

```dotenv
TELEGRAM_BOT_TOKEN=...        # never printed anywhere
TELEGRAM_CHAT_ID=...          # only this chat is authorised

STARTUP_TARGET_SIZE=1-10

# permitted open datasets (real headcounts)
STARTUP_ENABLE_DATASETS=true
STARTUP_DATASET_SOURCES=dbpedia
STARTUP_DATASET_LIMIT=250

# permitted URLs
STARTUP_ENABLE_URL_DISCOVERY=true
STARTUP_ALLOWED_DOMAINS=dbpedia.org
STARTUP_SEED_URLS=
STARTUP_MIN_REQUEST_INTERVAL_SECONDS=3
STARTUP_REQUEST_TIMEOUT_SECONDS=30
STARTUP_MAX_RETRIES=2
STARTUP_MAX_URLS_PER_RUN=20
STARTUP_MAX_PAGE_MB=2

STARTUP_DAILY_RUN_AT=09:00     # local time, 12-hour display in Telegram
STARTUP_WELLFOUND_BROWSER=true # collect in your own visible browser
STARTUP_WELLFOUND_MAX=25      # default max company profiles per run
STARTUP_AUTO_SCAN=true        # start a real scan when the agent boots
STARTUP_BROWSER_PROFILE=browser_profile
STARTUP_DB_PATH=data/startups.db
STARTUP_IMPORT_DIR=imports
STARTUP_EXPORT_DIR=exports
STARTUP_LOG_DIR=logs
STARTUP_MAX_IMPORT_MB=10
STARTUP_MAX_EXPORT_ROWS=0      # 0 = no cap
STARTUP_LOG_LEVEL=INFO
STARTUP_USER_AGENT=StartupDiscoveryAgent/1.0 (personal research; respects robots.txt)
```

The scan settings themselves (max profiles, delay, retries, timeout,
retry-incomplete, auto-report, notifications) live in the database and are
managed from the Telegram **Scan settings** panel, so they survive a restart.

Legacy `WF_*` keys are **preserved and ignored** by the new agent.
`setup_discovery.py` rewrites only the keys you change (comments and unknown
keys survive — covered by a test).

---

## 8. COMMANDS

```powershell
.\.venv\Scripts\python.exe setup_discovery.py            # interactive config
.\.venv\Scripts\python.exe -m startup_agent.cli init      # create folders
.\.venv\Scripts\python.exe -m startup_agent.cli scan --max 25   # real scan, own browser
.\.venv\Scripts\python.exe -m startup_agent.cli scan --mode retry  # retry incomplete
.\.venv\Scripts\python.exe -m startup_agent.cli discover --datasets
.\.venv\Scripts\python.exe -m startup_agent.cli collect --max 25   # one-off collector
.\.venv\Scripts\python.exe -m startup_agent.cli import imports
.\.venv\Scripts\python.exe -m startup_agent.cli export --format all --confirmed-only
.\.venv\Scripts\python.exe -m startup_agent.cli report --scope latest --format xlsx
.\.venv\Scripts\python.exe -m startup_agent.cli runs       # recent scan runs
.\.venv\Scripts\python.exe -m startup_agent.cli settings --set max_profiles=50
.\.venv\Scripts\python.exe -m startup_agent.cli sheet --platform wellfound --format xlsx
.\.venv\Scripts\python.exe -m startup_agent.cli stats
.\.venv\Scripts\python.exe run.py                         # start the Telegram agent
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

---

## 9. TEST COVERAGE (189 tests, all hermetic)

`tests/test_scan_control.py` (44) - the control panel and the job manager:

- strict 1-10 filter (a 51-200 card is rejected, the profile size wins)
- website verification: reachable/redirected/404/timeout/social/private
- duplicate detection across two separate runs
- checkpoint recovery after a simulated crash
- pause while a profile is being read (the worker really stops)
- graceful stop keeps the data and can be resumed
- concurrent scan prevention (`JobBusy`)
- empty scan results (no file is written for an empty report)
- Excel/CSV/JSON generation with one shared schema
- Telegram report delivery (a real document is sent)
- unauthorized callbacks change nothing
- invalid settings are rejected and not applied
- browser errors and the retry limit
- restart recovery (`recover()` → `resume_job()`)
- completed runs cannot be resumed
- a second process cannot take the browser profile (ownership guard)

`tests/test_startup_agent.py` (49):

- size parsing & classification, year-range rejection, 12-hour clock
- URL canonicalisation, website keys, stable record ids, name-only never merges
- import: missing fields, normalisation, repeated import, failed import
  recorded, prior-export record id update, JSON/JSONL wrappers, XLSX round-trip
- saved HTML: JSON-LD page, Wellfound directory page, login-page rejection
- dedup: website as secondary key, same name different locations, weak
  duplicate flagging
- queue: resume, attempt limit, blocked URL stays blocked, done→pending
- web policy: allow-list/subdomains, wellfound+angel.co blocked
- **redirects**: redirect to a non-allow-listed host is never fetched; redirect
  to Wellfound is blocked before the request
- **dataset adapter**: fixture SPARQL → companies with real headcounts, pipe
  separated homepages cleaned, archive URLs dropped, robots respected,
  allow-list respected, query bounded by the target range, re-run creates no
  duplicates
- **browser collector**: the card/profile extraction scripts are run against
  local fixtures in a real headless Chromium; the website cleaner; and the
  service path (collector → store → 4-column sheet, 51-200 excluded)
- Telegram: 2×3 keyboard, panels, callback routing (back / results / export /
  pause / resume), unauthorised chat rejected, secret filter
- setup wizard preserves legacy `.env` keys and comments

`tests/test_smoke.py` (96) — legacy internship bot, untouched and green.

---

## 10. CURRENT LIVE STATE

```
data/startups.db   177 real companies from DBpedia + 30 from Wellfound
                   206 confirmed target-size (1-10) companies
                   43 websites independently verified, the rest incomplete
scan_jobs          the background scan runs (id, status, counters, checkpoint)
scan_items         the per-profile work queue of every run
runs               dataset run + the background Wellfound runs, 0 failed
exports            exports/startups-*.{csv,xlsx,json}
                   exports/wellfound-sheet-*.{csv,xlsx}   <- the 4-column sheet
                   exports/verified-*.{csv,xlsx,json}, exports/latest-run-*,
                   exports/run-history-*, exports/run-summary-*.txt
agent              running, polling Telegram, daily run 09:00 local
```

### Verified live runs of the new job manager

```
S-20261020-220835-8418  new_retry  completed
   14 inspected · 13 eligible · 13 websites verified · 1 rejected · 3 duplicates
S-20261020-221259-c9c2  new_retry  completed
   11 inspected · 10 eligible · 10 websites verified · 9 duplicates skipped
S-20261020-221741-25c8  resumed after a restart, completed
   11 inspected · 10 eligible · 5 verified · 5 incomplete (sites were down)
```

The websites were checked with a real HTTP request, e.g.
`Wellfound company profile · HTTP 200 at https://langslide.ai`. Certa
(51-200 Employees) is collected but correctly classified as **outside** the
target band, so it never reaches a verified report. A pause issued from
another process was observed by the running worker, which stopped taking new
profiles until it was resumed.

---

## 11. KNOWN LIMITATIONS

1. **Wellfound automation runs in your own visible browser session.** The
   terms prohibit automated harvesting, so the account and the risk are yours.
   A verification page pauses the run instead of being bypassed.
2. **One browser per profile.** A second launch fails with "close any Chrome
   window that is using the profile".
3. **DBpedia headcounts are Wikipedia infobox values** → they can be dated.
   The note field says so on every record.
4. **DBpedia's live endpoint is occasionally slow/down** → a run can report a
   timeout or HTTP 502 instead of data. Re-run, or import an export.
5. `dbo:description` gives a short tagline, not a full paragraph.
6. Generic HTML directory pages may yield only a profile-level record —
   CSV/JSON is the reliable multi-company format.
7. The XLSX writer supports text/numeric cells and clickable links (no
   formulas).
8. Telegram live behaviour needs a valid token/chat; tests never contact it.
9. Timezone display uses system local time (`tzdata` may be needed for named
   zones on Windows).
10. The **Exact Member Count** column stays empty: Wellfound publishes a
    range, never an exact headcount.

---

## 12. WHAT WAS CHANGED / PRESERVED

**Added**: `startup_agent/` (15 modules), `run.py`, `watchdog.py`,
`setup_discovery.py`,
`tools/browser_capture.py`, `templates/`, `imports/README.md`, `docs/legacy_*`,
`data/startups.db`, `README.md`, `.env.example`, `.gitignore`, `requirements.txt`,
`setup.ps1`, `run_bot.cmd`, `deploy/install_windows_task.ps1` (rewritten for
the new agent), `deploy/agentctl.ps1`.

**Rewritten in the control-panel work**: `startup_agent/jobs.py` (new),
`startup_agent/verify.py` (new), `startup_agent/telegram.py` (new control
panel), `startup_agent/export.py` (report schemas + real hyperlinks),
`startup_agent/store.py` (scan tables + verification columns),
`startup_agent/models.py` (lead/website statuses), `startup_agent/app.py`
(job manager + auto-scan), `startup_agent/cli.py` (scan/runs/report/settings).

**Preserved untouched**: `wfbot/` (10 modules), `data/applications.db`
(34 rows), `browser_profile/`, root legacy `setup_wizard.py`, `deploy/`
(banner only), legacy `WF_*` keys in `.env`, all 96 legacy tests, the
4-column startup sheet.

**No infrastructure was touched** — no AWS, no VPS, no deployment, no charges.

---

## 13. HOW TO COPY THIS BRIEF

1. Open `MASTER_BRIEF.md` → Ctrl+A → Ctrl+C.
2. Paste anywhere (ChatGPT, Notion, email, another AI).

For the raw code, use `MASTER_BRIEF.md` as the map and copy each file from the
tree in section 3.
