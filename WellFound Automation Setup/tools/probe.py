"""Selector smoke test - run this whenever the bot starts finding nothing.

    python tools/probe.py                                  # default query
    python tools/probe.py --search "Web Developer Intern"  # one specific role
    python tools/probe.py --url "https://wellfound.com/jobs?query=backend"  # direct results URL
    python tools/probe.py --login                          # only check the stored session

It drives the real search code path, prints what it found and exits. No
applications are submitted and nothing is written to the database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright  # noqa: E402

from wfbot import configure_console  # noqa: E402
from wfbot.browser import open_context  # noqa: E402
from wfbot.config import ROLE_LIBRARY, Config  # noqa: E402
from wfbot.models import RuntimeState  # noqa: E402
from wfbot.wellfound import Wellfound  # noqa: E402


class ConsoleNotifier:
    """Minimal stand-in for the Telegram notifier."""

    def __init__(self) -> None:
        self.state = RuntimeState()

    async def challenge(self, where: str, url: str, wait_seconds: int) -> None:
        print(f"\n[!] Anti-bot check during {where}: {url}")
        print(f"    Solve it in the browser window - waiting up to {wait_seconds}s\n")

    async def challenge_cleared(self) -> None:
        print("[ok] challenge cleared")

    async def paused_automatically(self, reason: str) -> None:
        print(f"[paused] {reason}")

    async def error(self, where: str, exc: BaseException) -> None:
        print(f"[error] {where}: {type(exc).__name__}: {exc}")

    async def run_started(self) -> None:
        print("[run started]")

    async def outcome(self, status: str, job, detail: str = "") -> None:
        print(f"[outcome] {status}: {job.company} - {job.title} {detail}")

    async def run_finished(self, summary, next_run) -> None:
        print(f"[run finished] {summary.one_line()}")


async def probe(search: str, url: str, show_all: bool) -> int:
    cfg = Config.from_env(require_telegram=False)
    if url:
        cfg.search_url_template = url
    role = ROLE_LIBRARY.get(search, None)
    if role is None:
        role = next(
            (r for r in ROLE_LIBRARY.values() if r.label.lower() == search.lower()),
            None,
        )
    if role is None:
        print(f"Unknown role {search!r}. Known keys: {', '.join(ROLE_LIBRARY)}")
        return 2

    print(f"profile dir : {cfg.profile_dir}")
    print(f"headless    : {cfg.headless}")
    print(f"query       : {role.query} (role key: {role.key})")
    if url:
        print(f"search url  : {url}")
    print()

    notifier = ConsoleNotifier()
    async with async_playwright() as playwright:
        context = await open_context(playwright, cfg)
        page = context.pages[0] if context.pages else await context.new_page()
        wf = Wellfound(page, cfg, context, notifier)

        logged_in = await wf.is_logged_in()
        print(
            "session        : "
            + ("signed in" if logged_in else "not signed in (run setup_wizard.py --login)")
        )
        print(f"listing pages  : {', '.join(cfg.search_urls(role))}")

        raw_total = 0
        jobs = []
        try:
            jobs = await wf.search(role)
            raw_total = await page.locator('a[href^="/jobs/"]').count()
        except Exception as exc:  # noqa: BLE001 - probe should report, not traceback
            print(f"[error] search failed: {type(exc).__name__}: {exc}")
            await context.close()
            return 1

        print(f"\npage url   : {page.url}")
        print(f"raw /jobs/ anchors on page: {raw_total}")
        print(f"listings matching role keywords: {len(jobs)}\n")

        shown = jobs if show_all else jobs[:10]
        for job in shown:
            inline = "inline-apply" if job.has_inline_apply else "no inline apply"
            print(f"- {job.company or '?'} | {job.title}")
            print(f"    {job.url}  [{inline}]")
            if job.meta:
                print(f"    {job.meta[:110]}")
        if len(jobs) > len(shown):
            print(f"... {len(jobs) - len(shown)} more (use --all)")

        print("\nIf the numbers above look wrong, Wellfound has changed its markup.")
        print("Updated selectors live in wfbot/wellfound.py (EXTRACT_ROWS_JS and the")
        print("regexes at the top of the file).")
        await context.close()
    return 0 if jobs else 1


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="Wellfound selector probe")
    parser.add_argument("--search", default="Full Stack Developer Intern", help="role label or key")
    parser.add_argument("--url", default="", help="direct results URL template ({query} supported)")
    parser.add_argument("--all", action="store_true", help="print every match")
    parser.add_argument("--login", action="store_true", help="only report session status")
    args = parser.parse_args(argv)

    if args.login:
        async def _check() -> int:
            cfg = Config.from_env(require_telegram=False)
            async with async_playwright() as playwright:
                context = await open_context(playwright, cfg)
                page = context.pages[0] if context.pages else await context.new_page()
                wf = Wellfound(page, cfg, context, ConsoleNotifier())
                ok = await wf.is_logged_in()
                print("Wellfound session: " + ("signed in" if ok else "NOT SIGNED IN"))
                if not ok:
                    print("Run `python setup_wizard.py --login` and sign in by hand.")
                await context.close()
            return 0 if ok else 1

        return asyncio.run(_check())

    return asyncio.run(probe(args.search, args.url, args.all))


if __name__ == "__main__":
    raise SystemExit(main())
