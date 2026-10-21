"""Manual browser helper for pages that cannot be fetched automatically.

Examples:
    python tools/browser_capture.py https://wellfound.com/companies
    python tools/browser_capture.py https://example.com/startups

The script opens a normal, visible Chromium window with a persistent profile.  It
does not scrape, click through, solve CAPTCHAs, or bypass a login.  You use the
browser yourself, then save the page (Ctrl+S) to the imports folder and import it
through the bot or CLI.

This is intentionally a *manual* workflow: Wellfound's terms prohibit automated
harvesting, so only the user's own browser session and own saved copy are used.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Open a page for manual saving")
    parser.add_argument("url", help="page to open")
    parser.add_argument(
        "--profile",
        default=str(ROOT / "browser_profile"),
        help="persistent browser profile directory (default: browser_profile)",
    )
    args = parser.parse_args()
    if not args.url.lower().startswith(("http://", "https://")):
        print("URL must start with http:// or https://", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is not installed. Run: "
            ".venv\\Scripts\\python.exe -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    profile = Path(args.profile).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    print("Opening a normal browser. This tool does not extract or submit anything.")
    print("Sign in yourself if needed, then save the page with Ctrl+S into:")
    print(f"  {ROOT / 'imports'}")
    print("Close the browser window when you are done.")
    print()
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
            page.goto(args.url, wait_until="domcontentloaded", timeout=45_000)
            print(f"Opened: {page.url}")
            print("Use the browser normally, then close it.")
            try:
                input("Press Enter here after closing the browser…")
            except EOFError:
                pass
            context.close()
    except Exception as exc:  # noqa: BLE001 - make manual use understandable
        print(f"Browser helper failed: {exc}", file=sys.stderr)
        return 1
    print("Done. Import the saved file with /import or the CLI:")
    print("  .venv\\Scripts\\python.exe -m startup_agent.cli import imports\\your-file.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
