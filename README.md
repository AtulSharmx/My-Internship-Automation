# My internship automation

Two tools I built for my own internship search. Both run locally on my machine against my
own signed-in browser sessions, and both are driven from a Telegram bot so I can start a
run and pull the results from my phone.

They are separate tools that happen to chain together. The Wellfound one finds companies,
the Apollo one finds who to email at them.

## Wellfound startup discovery

Finds real 1 to 10 person startups on Wellfound and verifies their actual company website,
then gives back a sheet of name, website, platform and team size.

It works in two phases. Phase 1 walks the public role pages over plain HTTP, within what
robots.txt allows. Phase 2 opens each company profile in a visible browser and reads the
embedded data the page already renders from, rather than trying to reconstruct it.

Output is a four column sheet, or a ten column export once websites are verified.

## Apollo email finder

Takes that company list and finds one real contact email per company through Apollo, then
works out the rest of the team's addresses from the pattern it sees and MX-validates them
before writing them out.

The design problem here was credits. Apollo's free plan gives you very few, and the
obvious way to write this tool burns them on companies you already looked up. So it spends
at most one credit per company, keeps a SQLite ledger of every company it has already
processed so a re-run never spends twice on the same one, and takes a hard cap per run.

You log in to Apollo once in the Chrome window it opens. The profile persists, so later
runs do not ask again.

Output is an Excel file with a people-first sheet, a full data sheet, and a team_emails
sheet.

## What these tools will not do

This matters more than the feature list, so it is not buried in it.

Neither tool bypasses anything. No stealth mode, no CAPTCHA solving, no working around
rate limits. Both read only pages that are public to a signed-in user, and both are rate
limited on purpose.

If either one hits a security check or a verification page, it stops and tells you on
Telegram instead of trying to get past it. You go and clear it yourself, then press
retry.

It is your account and your browser doing the browsing, so it is your risk and your
responsibility under the platform's terms. I built these to save myself hours of manual
copying, not to extract data at a scale a person could not.

## Running them

Each tool has its own setup and its own README with the detail:

- `Apollo/README.md`
- `WellFound Automation Setup/README.md`

On Windows, `setup.ps1` or `setup_wizard.py` in the Wellfound folder installs the
Playwright browsers for you. Apollo starts with `run_ui.py`, which brings up a local
server and a desktop popup at `http://127.0.0.1:8765`.

Both need `TELEGRAM_BOT_TOKEN` in `.env` and a chat ID that matches. Only one instance of
each can run at a time, guarded by a mutex.

## Tests

```bash
pytest "WellFound Automation Setup/tests"
pytest Apollo/tests
```

Apollo covers the credit ledger, the email pattern matching, MX checking, the Excel
writer, the runner and the Telegram bot. The Wellfound side covers scan control and a
smoke test over the pipeline.

## Requirements

Windows 10 or 11 is the main target, tested on Python 3.14 and working on 3.11 and up.
Apollo runs on Linux and macOS too; the Wellfound collector needs a Chromium browser
available. Edge or Chrome should be installed, since both tools use a persistent browser
profile and the app-mode popup window.

## Architecture

`WellFound Automation Setup/ARCHITECTURE.md` has the full picture: the pipeline stages,
the SQLite schema, the export formats and the test coverage.

## License

MIT.
