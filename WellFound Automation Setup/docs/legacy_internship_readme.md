# Wellfound Intern Application Bot

A scheduled automation that checks **Wellfound** (formerly AngelList Talent)
once a day at **20:00 (8:00 PM) local time** and applies to every internship launched in
the previous 24 hours, newest first, using the profile you already built there —
no resume uploads, no form filling by hand. The process stays running so it can
answer Telegram questions and catch a missed 20:00 slot after a restart.

It targets the roles you asked for out of the box. A title must contain a role
keyword **and** an internship marker (`intern`, `internship`, `co-op`,
`apprentice`) — full-time roles are never matched:

| Role (key) | Title must contain |
| --- | --- |
| Full Stack Developer Intern (`fullstack`) | `full stack`/`fullstack`/`full-stack`/`mern`/`ai-first`/`ai-assisted`/`mobile` **and** an internship marker |
| Web Developer Intern (`web`) | `web developer`/`web engineer`/`frontend`/`vibe coding`/`ai-assisted`/`ai-first` **and** an internship marker |
| Vibe Coding / AI-Assisted Web Intern (`vibe`) | `vibe coding`/`ai-assisted`/`ai-first`/`ai engineer`/`ai developer`/`ai web` **and** an internship marker |
| MERN / AI-First Full Stack Intern (`mern`) | `mern`/`ai-first` **and** an internship marker |
| Full Stack Mobile / Web Intern (`mobile`) | `mobile`/`react native`/`flutter`/`android`/`ios` **and** an internship marker |
| Backend Developer Intern (`backend`) | `backend`/`back-end`/`back end`/`server-side` **and** an internship marker (available, not enabled by default) |

Both conditions are required: Wellfound's search is fuzzy, so a
"Backend Developer Intern" query can also return full-time roles — the bot
never spends an application slot on one. That is also why a title such as
"Lead AI-First MERN Fullstack Engineer" is **not** applied to: it has no
internship marker. Add `backend` to `WF_ROLES` if you want it back.

Everything is reported to Telegram, and the bot is steered with six buttons:
**Status**, **Applications**, **Run now**, **Pause** (or **Resume** when
paused), **Sync** and **More**. No commands are required. When an application
asks a question your
profile cannot answer, the bot asks you in Telegram and waits for your reply
(or one tap on **Skip application**). A compact daily summary also arrives at
`WF_DAILY_REPORT_HOURS` (8pm by default), and `/info` shows exactly when the
last run finished and when the next one starts. The 20:00 summary is a snapshot
of the day up to that point; the nightly pass sends its own run card when it
finishes. The welcome card is sent once, or when a startup sync/profile check
has something to report; routine restarts stay quiet.

---

## 1. Why Python (the "most efficient language" question)

The bottleneck here is **not CPU** — it is the network, Wellfound's own page
renders, and the deliberate human-paced delays we insert. A compiled language
would not send fewer requests or render faster; it would only add work.

| Option | Verdict |
| --- | --- |
| **Python + Playwright** | Chosen. Playwright is the most reliable browser-automation engine, ships its own patched Chromium, handles SPA/modal flows and new tabs properly, and has the richest ecosystem for Telegram/HTTP/parsing. |
| Node + Playwright | Technically equal — same engine. Python wins on the surrounding tooling for scheduling, SQLite and quick maintenance. |
| Go / Rust + headless Chrome | Only worth it for thousands of concurrent sessions across many accounts. For one account making a daily pass it is pure overhead: slower to build, easier to get wrong. |
| `requests` / raw HTTP | Fastest on paper, but fragile: the site is a Next.js SPA behind Cloudflare, and the apply flow happens in the browser. It would require reverse-engineering private GraphQL calls — higher breakage and higher ToS risk, not more efficiency. |

Rule of thumb applied here: **use a real browser, drive it slowly.** Efficiency
came from doing less work (persistent session, de-duplication, a few page loads
per run), not from faster code.

---

## 2. What it does, per run

```
once a day at 20:00 local
├── verify the stored session is really signed in (login if it is not)
├── open the signed-in /jobs search
│     clear the saved role/keyword chip, leaving Wellfound's profile location rule
│     select Internship + 1-10 employees
│     sort by Most recent
│     scroll and extract cards (title, age, company size, location, salary)
├── keep only titles that match a configured role AND contain an intern marker
├── drop cards older than 24 hours, larger than 10 employees, or onsite outside
│     Gurugram; cards with no age are kept only for a strict detail-page check
├── sort candidates by parsed age (newest first; unknown age last)
├── for each fresh listing (no application cap by default):
│     open the listing, re-check age is strictly under 24 hours, title, location,
│     company size and already-applied state
│     if a required form question is unanswered, ask you in Telegram
│     and wait (tap Skip application or reply "skip" to skip that listing)
│     click Apply, fill nothing the profile already answers
│     submit, verify the confirmation, record the result
│     sleep 45-150 s (triangular jitter) before the next one
└── report each application, counters, end time and next run to Telegram
```

The signed-in `/jobs` search is the primary feed: it is the only Wellfound view
with a real **Internship** filter, and it includes onsite Gurugram roles as well
as remote roles that accept the profile's India location. Public role and city
pages are a **fallback only** — used when the authenticated search itself errors,
not when it legitimately returns zero fresh listings.

### Project layout

```
run.py                  entry point (single-instance lock)
setup_wizard.py         interactive setup: .env, Telegram check, one-time login
tools/probe.py          selector smoke test (no applications sent)
wfbot/
  config.py             env parsing, role library, tuning knobs
  models.py             Job / Outcome / RunSummary / Status (+ emoji labels)
  store.py              SQLite: applications, runs, meta (pause, backoff)
  human.py              jittered delays, paced typing, mouse paths, scrolling
  browser.py            persistent Chromium context
  guards.py             Cloudflare/CAPTCHA detection, backoff maths
  wellfound.py          login, search, apply flow (all selectors live here)
  telegram.py           notifications + command long-polling
  runner.py             one daily pass + the 20:00 scheduler (catch-up after a restart)
  app.py                wiring, logging, graceful shutdown
```

---

## 3. Setup

### 3.1 Install

**Windows (one shot):**

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

**Or by hand:**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

Requires **Python 3.11+** (built and tested on 3.14).

### 3.2 Configure

```powershell
.\.venv\Scripts\python.exe setup_wizard.py
```

The wizard:

1. asks for your Telegram bot token (from [@BotFather](https://t.me/BotFather)),
   verifies it with `getMe`, and — if you message the bot — **detects your chat
   id automatically** and sends a test message;
2. lets you pick roles by number or key (`fullstack,web,vibe,mern,mobile`, plus
   optional `backend`);
3. optionally stores Wellfound email/password as a *fallback* (hidden input);
4. sets the daily run time (default 20:00), pacing and optional caps, then
   writes `.env`.

Then sign in to Wellfound once, by hand, in a real browser:

```powershell
.\.venv\Scripts\python.exe setup_wizard.py --login
```

Why manual? The login session is stored in `browser_profile/` and reused for
weeks, so this is a one-time step that also copes with 2FA, Google sign-in and
"email me a code" flows. If you'd rather the bot log in itself, set
`WF_EMAIL` / `WF_PASSWORD` in `.env` — the code path exists, but it is the
fallback, not the recommended route.

### 3.3 Verify and run

```powershell
.\.venv\Scripts\python.exe tools\probe.py            # prints what the bot can see right now
.\.venv\Scripts\python.exe run.py                    # start the process (daily at 20:00 / 8:00 PM)
```

`tools/probe.py` sends **no** applications and writes **nothing** to the
database — it just answers "are my selectors still valid?".

Offline checks (no network, no browser) live in `tests/`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

---

## 4. Telegram reference

**Notifications (bot → you)**

The chat is kept deliberately quiet: one message per thing that matters, and a
card only when a run actually did something.

* ✅ **applied** — company, role, location/size, type, stipend, link, plus a
  screenshot of the confirmation as proof (one message per success)
* ⚠️ **needs review / external / failed** — only when your attention or a
  manual step is needed
* a **question** when a form needs an answer your profile cannot provide. Short
  questions (URLs, numbers, radios) arrive as one numbered message — reply with
  one line per answer, or `skip`. Long free-text questions are asked one at a
  time so a multi-line answer is kept intact. Reusable answers (LinkedIn,
  GitHub, years of experience) are remembered and filled automatically next
  time.
* 🏁 **run card** — sent only when a run applied to something, hit a failure or
  logged an error; a clean daily pass stays silent (see `/info`)
* errors with the exception text
* anti-bot checks, with instructions and the wait window
* automatic pauses ("bot paused: …")
* one compact **daily summary** at 8pm (configurable via
  `WF_DAILY_REPORT_HOURS`), with today / 7-day / quality / response counts.
  It is a snapshot of the day up to 20:00; the nightly pass reports its own
  applications in its run card.

Plain skips — company too big, wrong location, listing closed, already applied —
are **not** messaged one by one; they are counted in the run card and `/report`.

**Buttons (you → bot)**

The keyboard stays visible in the chat. Tapping a button never requires typing.

| Button | Effect |
| --- | --- |
| **Status** | Bot Status, Today's Activity, Application History and Last Application |
| **Applications** | last five applications, links and Pending / Accepted status |
| **Run now** | start an application pass immediately |
| **Pause / Resume** | stop or restart applying; the label matches the current state |
| **Sync** | import manual applications and refresh response statuses |
| **More** | Issues, Roles and Help |

Inside **More**, **Issues** shows failures and forms needing a human;
**Roles** shows the live filter set; **Help** explains the question flow.
The Status panel uses the persistent six-button keyboard; secondary panels
carry only their own small Back and Refresh controls. When a question arrives,
its **Skip application** button is tied to that specific question, so old
buttons cannot skip a new one.

**Advanced commands (typed; hidden from the menu)**

| Command | Effect |
| --- | --- |
| `/status` | Bot Status, Today's Activity, Application History and Last Application |
| `/info` | exact last run (start, finish, duration, result) and next run time + countdown |
| `/report` | today / 48h / 7 days / all time, per-role totals, daily bars, success rate, top companies, goal pace |
| `/goal [N] [YYYY-MM-DD]` | show or set the application target and deadline (`/goal off` clears it) |
| `/last_applied [n]` | last *n* applications with company, role, time, link, response if known |
| `/errors` | recent failures, external-apply handoffs, forms needing a human |
| `/sync` | read Wellfound's `/jobs/applications`, import manual applications, refresh responses |
| `/responses` | Pending / Accepted / Not Accepted counts |
| `/answers` | saved form answers that are filled automatically |
| `/forget <id>` | remove one saved answer (ids are shown by `/answers`) |
| `/roles` | configured roles, location filter, company-size band, apps/run |
| `/pause` / `/resume` | stop / re-enable applying (schedule keeps ticking) |
| `/run` | trigger a run immediately |
| `/cancel` | skip the application whose question is waiting for an answer |
| `/help` | the short button help |

Only `TELEGRAM_CHAT_ID` is obeyed; updates from any other chat are logged and
ignored.

### Reports and goals

`/report` is the "how am I doing?" view:

```text
📊 WELLFOUND REPORT
✅ Applications submitted
Today: 3 · Last 48 hours: 7 · Last 7 days: 31 · All time: 84
🎯 Goal  84 / 100 applied (84%)
By 2026-12-31 (73 days left) you need 0.2/day
Your pace: 2.4/day · At this pace: around 27 Oct 2026
🧩 By role (all time · last 7 days)
• Full Stack Developer Intern: 40 (7d: 15)
• Web Developer Intern: 26 (7d: 9)
...
📈 Last 7 days (one bar per day)
🏢 Top companies
🧪 Quality: confirmed vs unverified, confirmation rate, failed / needs-review counts, skips
```

Set a target (persisted in the database, overrides `WF_APPLY_GOAL` /
`WF_GOAL_DEADLINE`):

```text
/goal 100 2026-12-31     # 100 applications by 31 Dec 2026
/goal 150                # keep the existing deadline
/goal off                # clear the target
```

It also shows your current streak, your best day, today's count against any
optional daily cap, confirmation rate and top companies. The pace numbers are
recomputed every time, so the report tells you both what you are averaging and
how many per day the remaining target needs, and when you will hit the goal at
the current pace. When the goal is reached the bot sends one celebration, then
waits for you to set the next target. A compact summary (today, 7-day, quality,
response counts and goal) is sent automatically at each hour in
`WF_DAILY_REPORT_HOURS` (local time; default `20` = 8pm; `8,20` sends twice).
Use `/info` for the exact last-run and next-run timestamps.

### Application statuses you will see

| Status | Meaning |
| --- | --- |
| ✅ Applied | submitted, confirmation seen |
| ℹ️ Already applied | Wellfound says you applied before |
| 🔗 External apply | the listing hands off to the company's own ATS — link sent to you to finish by hand |
| 📋 Needs your review | a required question the bot refuses to invent an answer for |
| ⚠️ Unverified | submitted but no confirmation appeared — the link is in the message; not counted as confirmed in reports |
| ❌ Failed | an error stopped the attempt |
| 💸 Skipped (unpaid) | unpaid / commission-only / equity-only listing (only when `WF_SKIP_UNPAID=true`) |
| 🏢 Skipped (company cooldown) | another role at an employer you applied to within `WF_COMPANY_COOLDOWN_DAYS` |
| 🚫 Skipped (role mismatch / closed / wrong location) | correctly not applied |

---

## 5. Running 24/7

The Python process stays up so Telegram answers and the 20:00 schedule keep
working, but it only **applies** during the daily pass. If the machine was off
during the slot, it starts one catch-up pass when it returns within four hours;
after that it waits for the next 20:00.

**Windows (Task Scheduler).** Create a task, trigger *At log on* (and *At
startup* if you use auto-login), action:

```
Program:  D:\Projects\WellFound Automation Setup\.venv\Scripts\pythonw.exe
Arguments: run.py
Start in:  D:\Projects\WellFound Automation Setup
```

`pythonw.exe` avoids a console window; because the bot must stay headed to let
you clear the occasional anti-bot check, keep the machine logged in (lock the
screen — that's fine, the browser still renders). The included
`deploy\install_windows_task.ps1` registers the logon task with `run_bot.cmd`,
which restarts the bot after a crash.

**Linux/macOS (systemd user unit).** `~/.config/systemd/user/wfbot.service`:

```ini
[Unit]
Description=Wellfound intern application bot
[Service]
WorkingDirectory=%h/wellfound-bot
ExecStart=%h/wellfound-bot/.venv/bin/python run.py
Restart=always
RestartSec=30
[Install]
WantedBy=default.target
```

`systemctl --user enable --now wfbot`, and `loginctl enable-linger $USER` if the
session ends. A headless VPS needs a virtual display (`xvfb-run`), or set
`WF_HEADLESS=true` — but then you can't clear a challenge by hand, so a
headed session (or a cheap desktop VM) is genuinely better.

Logs rotate in `logs/wfbot.log`; history lives in `data/applications.db`
(inspect it with any SQLite client, e.g. `.schema applications`).

---

## 6. Stealth, anti-bot measures, and the honest trade-offs

You asked for "stealthy automation that prevents Wellfound flagging the
account", including user-agent rotation and CAPTCHA solving. Here is what this
project does instead, and why.

**What it does**

* **One consistent identity.** Persistent profile, fixed viewport, locale,
  timezone and browser build, and real cookies from a real login. Rotating user
  agents *within a session* is one of the clearest automation tells on the
  market — a stable, believable fingerprint is quieter than five unstable ones.
* **Human pacing.** 45-150 s triangular jitter between applications, 3-8 s
  before the first one, gentle skimming scrolls, paced typing (~78 ms/char ± 26
  with hesitation pauses) and mouse movement in steps rather than teleports.
  The process is idle between daily passes; it does not poll the listing feed
  all day. Listings that are only *skipped* (size, location, closed, already
  applied) get a short beat instead of the full pause, so the wait is reserved
  for real applications.
* **Strict 24-hour window, newest first.** The signed-in search is sorted by
  **Most recent**, and every listing's age is parsed (`2 hours ago`, `today`,
  `yesterday`, …). Candidates are sorted by that parsed age, and anything at or
  beyond `WF_MAX_JOB_AGE_DAYS` (default `1` = 24 hours) is skipped — a listing
  shown as `1 day ago` is rejected because it could be nearly 48 hours old. A
  card with no age is kept only for a detail-page check; if the detail page also
  has no `POSTED X AGO` stamp, it is skipped (`WF_REQUIRE_POSTED_AGE=true`).
  Set `WF_MAX_JOB_AGE_DAYS=0` only if you deliberately want any age.
* **Company size is checked twice.** The signed-in search is filtered to
  **1-10 employees**, the card's own size badge is checked before a detail page
  opens, and the detail page's head-count is checked again. The optional 11-50
  fallback remains **off** (`WF_ALLOW_SIZE_FALLBACK=false`).
* **No arbitrary application cap.** By default `WF_MAX_APPS_PER_RUN=0` and
  `WF_MAX_APPS_PER_DAY=0` mean **unlimited**: every fresh match from the
  24-hour window is attempted. Setting either to a positive number restores a
  safety cap, and `/report` shows today's count against an optional daily cap.
  Page opens for size/age checks are unlimited by default too
  (`WF_MAX_SIZE_CHECKS_PER_RUN=0`).
* **Adaptive backoff in interval mode.** The legacy interval mode stretches its
  wait ×1.6 per failure (capped by `WF_BACKOFF_MAX_MULTIPLIER`). The fixed daily
  schedule keeps its 20:00 slot; repeated challenge/login failures still pause
  the bot and ask for a human instead of hammering.
* **Remote or Gurugram only.** The location section of each job page is read:
  remote roles are applied to, onsite roles only when the city is in
  `WF_ACCEPT_LOCATIONS` (default `Gurugram,Gurgaon`), everything else is
  skipped. A "remote" role that only hires in a region outside
  `WF_ACCEPT_REMOTE_REGIONS` (default Everywhere/Anywhere/Worldwide/India)
  is skipped too, so US-only remote listings are not wasted clicks. The
  signed-in search already hides jobs that reject the profile's Gurgaon
  location; the public city page (`WF_LOCATION_PAGES`, default
  `/location/gurgaon`) is used only in the fallback path.
* **Quality gates before the form.** A title must contain a real internship
  marker and a role keyword, and must not contain a non-engineering word from
  `WF_NEGATIVE_KEYWORDS` (`marketing`, `sales`, `content`, `designer`, …).
  Unpaid, commission-only and equity-only cards are skipped only if you opt
  in with `WF_SKIP_UNPAID=true` (off by default: paid and unpaid are both
  welcome). Onsite/hybrid cards outside Gurugram are skipped at the
  card level; remote cards are checked on the detail page and again by
  Wellfound's own modal warning.
* **Location/timezone warning = skip.** If the application modal says *"this job
  does not support the locations or timezones for remote work on your
  profile"* (Wellfound disables every field when it does), the bot skips the
  listing instead of asking you questions that cannot be submitted.
* **Answer bank.** Reusable answers (LinkedIn, GitHub, years of experience,
  work authorization, primary expertise) are stored in `data/applications.db`
  and filled automatically. Questions that name the employer ("How did you
  discover Acme?") or ask for original prose are never banked. Inspect with
  `/answers`, remove with `/forget <id>`.
* **Proof of every apply.** On a confirmed submission the bot captures the
  confirmation screen to `data/proof/<job_id>.png` and sends it with the
  Telegram message (`WF_SCREENSHOT_PROOF`).
* **De-duplication.** A listing we actually attempt is never opened again, so
  each daily pass spends its time on genuinely new roles.
* **Same-company cooldown.** After a real application to an employer, other
  roles at that company are skipped for `WF_COMPANY_COOLDOWN_DAYS` (default 30;
  `0` disables it). Legal suffixes are normalised, so `Acme`, `Acme Inc.` and
  `Acme Technologies Pvt Ltd` count as the same employer.
* **Failed attempts are retried.** A network timeout or transient page error is
  retried up to `WF_MAX_RETRIES` times (default 2) instead of losing the
  listing; submitted and unverified jobs are never retried.
* **Application history sync.** At startup and on `/sync`, the bot reads
  Wellfound's `/jobs/applications` page. Manual applications it never saw are
  imported so it will not duplicate them, and Pending / Accepted / Not Accepted
  statuses are stored and shown in `/report`, `/responses` and
  `/last_applied`.
* **Profile preflight.** At startup the bot reads your profile and warns in
  Telegram if it looks incomplete (missing resume, empty bio/headline). The
  check is advisory: it never blocks an application. Disable with
  `WF_PROFILE_PREFLIGHT=false`.
* **Real errors instead of guesses.** If a required question or file upload is
  detected, the bot stops that application and asks you — it does not fabricate
  answers or auto-tick consent boxes.

**What it deliberately does not do**

* **No CAPTCHA solving, no solver services.** When Cloudflare Turnstile appears,
  `guards.py` detects it, pings you on Telegram and waits for you to click the
  box in the open window (up to `WF_CHALLENGE_WAIT_SECONDS`), then continues.
  If you don't, the bot pauses and tells you. Solving/bypassing CAPTCHAs
  automatically breaks Wellfound's Terms of Service and, in several
  jurisdictions, anti-circumvention law — and it is also the single most
  reliable way to get an account banned.
* **No fingerprint spoofing / stealth patching.** `navigator.webdriver` and
  friends are left alone. Patches that claim to hide automation mostly give a
  false sense of safety; Cloudflare fingerprints the TLS/JA3 layer and behaviour,
  not just a JS flag.
* **Browser pages, not private APIs.** Discovery drives the normal signed-in
  `/jobs` search UI, with public role/city pages as a fallback; applications
  open `/jobs/<id>-<slug>`, and history sync reads `/jobs/applications`. The bot
  does not call Wellfound's private GraphQL endpoints, rotate proxies or bypass
  the UI. Automated access is still subject to Wellfound's terms — see below.

**The risk you are accepting**

Wellfound's terms restrict automated access, and automated applications are
against the spirit of the platform. Running a bot on your account can get it
**rate-limited or banned**, and can get your applications filtered by
recruiters. This project keeps volume and velocity in human territory to make
that unlikely, not impossible — and it is built so the failure mode is "the bot
pauses and tells me", never "the bot quietly hammers the site".

If you want to reduce risk further: raise `WF_MIN_DELAY_SECONDS`, set
`WF_MAX_APPS_PER_RUN` to 2-3 (or a daily cap), and use `WF_COVER_NOTE` so
applications don't look template-generated. Also consider staying reachable
around 20:00 to clear a check — that is exactly what the Telegram notifications
are for.

---

## 7. Maintenance

Wellfound changes its markup; when the bot starts finding nothing:

1. Check `logs/wfbot.log` first: the daily search logs how many cards it saw and
   how many survived filtering. If it says `0 matching/fresh`, there may simply
   be no internship in the 24-hour window.
2. `.\.venv\Scripts\python.exe tools\probe.py --all` — how many anchors does the
   public fallback page have, how many survive filtering, and which listing pages
   did it use?
3. If the signed-in search found `0 cards` but `/jobs` shows results in a browser,
   the filter/sort controls moved: check the `data-test` selectors in
   `search_internships`, `_open_and_apply_search_filters` and
   `_sort_search_by_newest`.
4. If anchors are ~0 on the fallback pages, the role slugs changed. The slugs
   live in `ROLE_LIBRARY[*].slugs` (`wfbot/config.py`) — each valid one renders a
   heading such as "Remote Backend Developer Jobs"; an unknown slug silently
   falls back to a generic list, so check the heading in a browser.
   `WF_SEARCH_URL_TEMPLATE` is the one-line override if you find a better URL
   (it disables the signed-in search).
5. If anchors exist but listings are missing, the row structure moved: adjust
   the `a.closest('div.mb-2, li, article, div[class*="min-h-"]')` chain and the
   `div[class*="text-sm"]` company lookup inside `EXTRACT_ROWS_JS`.
6. Apply-flow changes live in `_find_apply_control`, `UNANSWERED_JS`,
   `_find_submit` and the regexes at the top of the file (`APPLY_NAME_RE`,
   `SUBMIT_NAME_RE`, `CONFIRMATION_RE`).
7. If the bot insists it is signed out while you can see yourself signed in,
   look at `is_logged_in()` and `SIGNED_OUT_MARKERS` in `wfbot/wellfound.py` —
   that is the whole of session detection (cookie names are deliberately not
   used).

Everything selector-ish is in that single module by design.

**Rate-limit / challenge symptoms:** `data/applications.db` meta `paused=1`, a
Telegram "anti-bot check" message, or `runs.note` mentioning a challenge. Clear
the check in the browser, send `/resume`, and if it recurs raise your delays.

---

## 8. Validation status (read this before trusting it)

**Verified against live wellfound.com (no login required):**

* Discovery works end to end: `tools/probe.py --search fullstack|web|vibe|mern|mobile`
  walks the real `/role/r/<slug>` pages (including `?page=2`) and returns
  matching intern listings with salary/location metadata, from a browser that is
  signed out. (`backend` is available as an optional extra role.)
* Row extraction (`EXTRACT_ROWS_JS`), the `/jobs/<id>-<slug>` link format and
  the Cloudflare Turnstile challenge are real findings from live pages.
* Session detection was checked on every page the bot visits — `/jobs`, role
  pages, a job detail page, the application modal and `/login`/`/signup` all
  render visible `Log In` / `Sign Up` controls while signed out. This replaced
  an earlier cookie-name check that reported "session: yes" for anonymous
  visitors (because of `rl_session`). The signed-*in* side of that check still
  needs one `tools/probe.py --login` run after you complete setup; if it says
  `NOT SIGNED IN` despite a working login, extend `SIGNED_OUT_MARKERS` in
  `wfbot/wellfound.py`.
* The signed-out application modal is a **sign-up form**, not a job form. The
  bot detects it (via the same `Log In` markers) and stops with
  `LoginRequired` rather than filling in account-creation fields.

**Verified against a live signed-in session (2026-10-19):**

* **Real submissions work.** Applications to HelioAI and Ivynest were submitted
  and confirmed; the listing flips to **✓ Applied** and the bot now recognises
  that state (plus a reload fallback) instead of reporting `unverified`.
* **Company size** is read from the job page and enforced: `1-10` only in the
  default configuration, `51-200` skipped (the optional 11-50 fallback is off).
* **Location** is read from the job page: remote roles and Gurugram onsite roles
  pass; other onsite cities are skipped. US-only remote roles are skipped by the
  region list.
* **Interactive form answers** were tested on a local form containing a text
  input, a select, a radio group and a required checkbox; all four were detected,
  filled from the supplied answers and cleared from the unanswered list.
* **Posting dates** were read from real listing headers (`POSTED 2 HOURS AGO`,
  `POSTED 3 WEEKS AGO`, `POSTED 4 YEARS AGO`), and job-id order was confirmed to
  track posting recency (4.8M ids ≈ 1 week, 4.6M ≈ 1 month, 3.4M ≈ 1 year).

**Phase 1 quality work verified live (2026-10-20, no submissions):**

* **Custom questions are detected.** On the Infisical modal the detector found
  all **7** employer questions with exact labels and radio options (LinkedIn,
  GitHub, years, Americas yes/no, primary expertise, "how did you discover us",
  three bullets) and correctly ignored the optional Loom-video prompt.
* **Location/timezone mismatch is caught before asking.** That modal displayed
  *"This job does not support the locations or timezones for remote work on
  your profile"* and Wellfound had disabled all 20 custom controls. Disabled
  controls are now excluded from detection, and the flow returns
  `skipped_location` instead of asking unanswerable questions.
* **An enabled modal accepts the answers.** On a remote India-compatible
  internship the field was detected, typed successfully and cleared from the
  unanswered list. The modal was closed without submitting.
* **The real submit control is `JobApplicationModal--SubmitButton`** ("Send
  application"), not the obsolete `QuickApplyModal--SubmitButton`; it is now the
  first selector, so the bot cannot click the background "Apply" button.
* **Card metadata is parsed from live cards** — job type, salary, work mode,
  area and age (`today`, `2 weeks ago`, …) — so stale/unpaid (optional)/onsite
  cards are filtered before a detail page opens. The applications page is also
  read at startup, importing manual applications and response statuses.

**Phase 2 verified live (2026-10-20):**

* **Applications history sync works.** Both the Ongoing and Archived tabs are
  read from `/jobs/applications`; the first run found 25 rows, imported 22
  manual applications the bot had never seen, and refreshed 3 existing records.
  The stored statuses were 15 Pending, 1 Accepted and 9 Not Accepted.
* **Profile preflight uses the candidate page.** `/profile` redirects to a
  company profile, so the check now reads `/profile/edit`; the live profile is
  marked complete, so no warning was emitted.
* **Unpaid roles are allowed by default.** `WF_SKIP_UNPAID=false`; the log no
  longer skips unpaid/equity-only cards, and the filter remains available as an
  opt-in.

**Phase 3 verified live (2026-10-20):**

* **The signed-in `/jobs` search works end to end.** It clears the saved
  role/keyword chip (including a quoted keyword like `"web developer"`), selects
  `Internship` + `1-10 employees`, switches the sort to **Most recent**, and
  extracts the lazily-rendered cards. The live probe saw 65 cards after scrolling
  and correctly skipped the 4 cards whose parsed age was 7 days.
* **The strict 24-hour gate leaves only genuinely new listings.** On the probe
  day the newest matching card was a business-development internship posted
  `today`; it was rejected by the role/negative-keyword matcher, leaving zero
  engineering internships inside the window. That is a correct empty result, not
  a discovery failure.
* **Filter state is rebuilt from scratch.** The probe cleared all filters, ran
  the search, and confirmed the final UI showed exactly `Internship` +
  `1-10 employees` and `Most recent`, with 166 results before age/role filtering.

**Still unverified (edge cases):**

* A **live** Wellfound submission that hits a custom question end to end through
  Telegram (detection and filling are live-tested; the reply loop is
  unit-tested, but the first real custom-question apply will be the final check).
* External ATS hand-offs are detected and reported, not filled.
* CAPTCHA/Turnstile is detected and handed to you; it is never solved
  automatically.
* `WF_SEARCH_URL_TEMPLATE` still exists as an escape hatch: it is formatted
  with `{query}` and replaces the role-page walk entirely if Wellfound ever
  changes those URLs.
