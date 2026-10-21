"""User-driven capture of Wellfound pages into the agent's import folder.

WHAT THIS IS
    A visible, normal Chromium window that **you** drive.  The script opens a
    page, waits while you browse, and then writes the *rendered* page (the DOM
    as the browser actually shows it) to ``imports/`` so the agent can read it
    and build the startup sheet.

    This is the same workflow as "Save page as" (Ctrl+S) - only more reliable
    for JavaScript-rendered sites such as Wellfound, and it keeps the full
    result list of a filtered search in one file.

WHAT THIS IS NOT
    It does not scrape on its own.  It does not click, scroll, log in, solve a
    CAPTCHA, spoof the browser, rotate proxies or work around robots.txt or
    rate limits.  Every navigation and every filter is done by you, in a
    window you can see, in your own signed-in session.

    Wellfound's terms do not allow automated harvesting, so the collection
    stays under your hands.  The agent then does the reading, the size
    filtering and the spreadsheet.

HOW TO USE
    1.  python tools/wellfound_capture.py
    2.  In the browser: sign in if you want to, set the filter
        "Company size: 1-10", scroll to load the results you want.
    3.  Come back to this window and press Enter -> the page is saved.
    4.  Repeat for the next page/filter if you want more.
    5.  Build the sheet:
            python -m startup_agent.cli import imports
            python -m startup_agent.cli sheet --platform wellfound --format xlsx
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "https://wellfound.com/companies"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Open Wellfound in a visible browser and save the page you view"
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="page to open")
    parser.add_argument(
        "--profile",
        default=str(ROOT / "browser_profile"),
        help="persistent browser profile (keeps your own sign-in)",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "imports"),
        help="folder for the captured pages (default: imports)",
    )
    parser.add_argument(
        "--prefix",
        default="wellfound",
        help="file name prefix for the captured pages",
    )
    parser.add_argument(
        "--screenshot",
        action="store_true",
        help="also save a PNG next to each captured page (for your own records)",
    )
    args = parser.parse_args()

    if not args.url.lower().startswith(("http://", "https://")):
        print("URL must start with http:// or https://", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is not installed. Run:\n"
            "  .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n"
            "  .venv\\Scripts\\python.exe -m playwright install chromium",
            file=sys.stderr,
        )
        return 2

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(args.profile).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)

    print("=" * 68)
    print(" MANUAL CAPTURE - you drive the browser, the agent reads the result")
    print("=" * 68)
    print(" 1. Sign in yourself if you want to (nothing is automated).")
    print(" 2. Set the filter 'Company size: 1-10' in the site filter panel.")
    print(" 3. Scroll until the companies you want are listed.")
    print(" 4. Come back here and press Enter to save this page.")
    print()
    print(f"Pages are written to: {out_dir}")
    print("=" * 68)
    print()

    saved: list[Path] = []
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=False,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                args=["--disable-session-crashed-bubble", "--hide-scrollbars"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
            print(f"Opened: {page.url}")
            print()

            while True:
                try:
                    input(
                        "Press Enter to save this page (or type 'q' then Enter to stop)… "
                    )
                except EOFError:
                    break
                answer = ""
                if answer.strip().lower() == "q":
                    break

                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                suffix = f"-{len(saved) + 1}" if saved else ""
                target = out_dir / f"{args.prefix}-{stamp}{suffix}.html"
                try:
                    # The rendered DOM: exactly what the page shows right now.
                    html = page.content()
                except Exception as exc:  # noqa: BLE001
                    print(f"Could not read the page: {exc}", file=sys.stderr)
                    break
                target.write_text(html, encoding="utf-8")
                saved.append(target)
                print(f"Saved: {target}  ({len(html) // 1024} KB)")

                if args.screenshot:
                    shot = target.with_suffix(".png")
                    try:
                        page.screenshot(path=str(shot), full_page=False)
                        print(f"Saved: {shot}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"Screenshot skipped: {exc}", file=sys.stderr)

                try:
                    more = input("Capture another page? (Enter = yes, q = stop) ").strip()
                except EOFError:
                    break
                if more.lower().startswith("q"):
                    break
            context.close()
    except Exception as exc:  # noqa: BLE001 - make manual use understandable
        print(f"Browser helper failed: {exc}", file=sys.stderr)
        return 1

    if not saved:
        print("No pages were captured.")
        return 1
    print()
    print(f"Captured {len(saved)} page(s). Now build the sheet:")
    print("  .venv\\Scripts\\python.exe -m startup_agent.cli import imports")
    print(
        "  .venv\\Scripts\\python.exe -m startup_agent.cli sheet "
        "--platform wellfound --format xlsx"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
