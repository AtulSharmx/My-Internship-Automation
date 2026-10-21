"""Zero-credit Apollo recon.

Opens a visible real-Chrome window with a dedicated persistent profile, waits for
the user to log in manually, then captures the company / people pages for one
domain. It never clicks any email-reveal control.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
import time
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "apollo_profile"
RUN_DIR = ROOT / "recon" / "out" / time.strftime("%Y%m%d-%H%M%S")

CHALLENGE_MARKERS = (
    "verify you are human",
    "security check",
    "unusual activity",
    "captcha",
    "are you a robot",
    "challenge required",
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def first_domain_from_imports() -> str | None:
    folder = ROOT / "imports"
    if not folder.exists():
        return None
    files = sorted(
        [p for p in folder.iterdir() if p.suffix.lower() in {".xlsx", ".xlsm", ".csv"}],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return None
    path = files[0]
    rows: list[dict[str, str]] = []
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    else:
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        values = ws.iter_rows(values_only=True)
        header = [str(c).strip() if c is not None else "" for c in next(values)]
        for row in values:
            rows.append({header[i]: row[i] for i in range(min(len(header), len(row)))})
        wb.close()
    for row in rows:
        domain = str(row.get("domain") or "").strip().lower()
        if domain:
            return domain.removeprefix("https://").removeprefix("http://").split("/")[0]
        website = str(row.get("website") or "").strip().lower()
        if website:
            return website.removeprefix("https://").removeprefix("http://").split("/")[0]
    return None


async def dump(page: Page, name: str) -> dict:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(RUN_DIR / f"{name}.png"), full_page=False)
    except Exception as exc:  # noqa: BLE001
        log(f"screenshot failed for {name}: {exc}")
    html = await page.content()
    (RUN_DIR / f"{name}.html").write_text(html, encoding="utf-8")
    data = await page.evaluate(
        """() => {
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
            const buttons = [...document.querySelectorAll('button, a[role="button"], [role="button"]')]
                .filter(visible)
                .map((el) => ({
                    text: textOf(el).slice(0, 120),
                    aria: el.getAttribute('aria-label'),
                    title: el.getAttribute('title'),
                    testid: el.getAttribute('data-testid'),
                    tag: el.tagName.toLowerCase(),
                }))
                .filter((b) => b.text || b.aria || b.title);
            const inputs = [...document.querySelectorAll('input, textarea, select')]
                .filter(visible)
                .map((el) => ({
                    type: el.getAttribute('type'),
                    name: el.getAttribute('name'),
                    placeholder: el.getAttribute('placeholder'),
                    aria: el.getAttribute('aria-label'),
                }));
            const emailish = [...document.querySelectorAll('body *')]
                .filter((el) => visible(el) && el.children.length === 0 && /@/.test(textOf(el)))
                .map((el) => textOf(el).slice(0, 200))
                .slice(0, 40);
            const verified = [...document.querySelectorAll('body *')]
                .filter((el) => visible(el) && el.children.length === 0 && /verified/i.test(textOf(el)))
                .map((el) => ({
                    text: textOf(el).slice(0, 80),
                    cls: String(el.className).slice(0, 120),
                    parent: textOf(el.parentElement).slice(0, 160),
                }))
                .slice(0, 40);
            const links = [...document.querySelectorAll('a[href]')]
                .map((a) => ({href: a.getAttribute('href'), text: textOf(a).slice(0, 100)}))
                .filter((l) => /\\/(people|companies)\\//.test(l.href || ''))
                .slice(0, 40);
            return {
                url: location.href,
                title: document.title,
                bodyText: document.body ? document.body.innerText : '',
                buttons,
                inputs,
                emailish,
                verified,
                links,
            };
        }"""
    )
    body = data.pop("bodyText", "")
    (RUN_DIR / f"{name}.txt").write_text(body, encoding="utf-8")
    (RUN_DIR / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    log(f"captured {name}: {data['url']} | {len(body)} chars text")
    return data


async def is_logged_in(page: Page) -> bool:
    try:
        nav = page.locator('a[href*="/people"]')
        if await nav.count() and await nav.first.is_visible():
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


async def dismiss_common_overlays(page: Page) -> None:
    for text in ("Accept all", "Accept All", "Accept cookies", "Got it", "Maybe later", "Close"):
        try:
            button = page.get_by_role("button", name=text, exact=True)
            if await button.count() and await button.first.is_visible():
                await button.first.click(timeout=2000)
                log(f"dismissed overlay: {text}")
                await page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            continue


async def wait_for_login(page: Page, timeout_minutes: int) -> None:
    log("waiting for manual Apollo login...")
    deadline = time.monotonic() + timeout_minutes * 60
    last_note = 0.0
    while time.monotonic() < deadline:
        try:
            if await is_logged_in(page):
                log("logged in (People nav is visible)")
                return
            body = (await page.evaluate("document.body ? document.body.innerText : ''")).lower()
            if any(marker in body for marker in CHALLENGE_MARKERS):
                await dump(page, "challenge_during_login")
                raise SystemExit("CHALLENGE detected during login - stopping, browser left open")
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"login poll note: {exc}")
        if time.monotonic() - last_note > 15:
            log(f"still waiting | url={page.url}")
            last_note = time.monotonic()
        await asyncio.sleep(2)
    raise SystemExit("login wait timed out")


async def try_people_urls(page: Page, domain: str) -> Page:
    candidates = [
        f"https://app.apollo.io/#/people?q_organization_domains_list[]={domain}",
        f"https://app.apollo.io/people?q_organization_domains_list[]={domain}",
        f"https://app.apollo.io/#/people?organization_domains[]={domain}",
    ]
    best: tuple[int, Page] = (-1, page)
    for idx, url in enumerate(candidates, start=1):
        log(f"trying people URL {idx}: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            log(f"goto failed: {exc}")
            continue
        await page.wait_for_timeout(7000)
        await dismiss_common_overlays(page)
        data = await dump(page, f"people_url_{idx}")
        score = 0
        score += 3 * (1 if domain in data.get("bodyText", "").lower() else 0)
        score += min(len(data.get("emailish", [])), 10)
        score += min(len(data.get("verified", [])), 10)
        log(f"people URL {idx} score={score}")
        if score > best[0]:
            best = (score, page)
    return best[1]


async def try_company_page(page: Page, domain: str) -> None:
    candidates = [
        f"https://app.apollo.io/#/companies?q_organization_domains_list[]={domain}",
        f"https://app.apollo.io/companies?q_organization_domains_list[]={domain}",
    ]
    for idx, url in enumerate(candidates, start=1):
        log(f"trying company URL {idx}: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            log(f"goto failed: {exc}")
            continue
        await page.wait_for_timeout(7000)
        await dismiss_common_overlays(page)
        data = await dump(page, f"companies_url_{idx}")
        links = [l for l in data.get("links", []) if "/companies/" in (l.get("href") or "")]
        if links:
            href = links[0]["href"]
            target = href if href.startswith("http") else f"https://app.apollo.io{href}"
            log(f"opening company detail: {target}")
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(7000)
                await dismiss_common_overlays(page)
                await dump(page, "company_detail")
            except Exception as exc:  # noqa: BLE001
                log(f"company detail failed: {exc}")
            return


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", default=None)
    parser.add_argument("--login-timeout-minutes", type=int, default=30)
    parser.add_argument("--hold-minutes", type=int, default=20, help="keep browser open after recon")
    args = parser.parse_args()

    domain = (args.domain or first_domain_from_imports() or "").strip().lower()
    if not domain:
        domain = "linear.app"
        log(f"no domain found in imports/ - using fallback {domain}")
    log(f"recon domain: {domain}")

    PROFILE.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log(f"output dir: {RUN_DIR}")

    async with async_playwright() as pw:
        context: BrowserContext = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
            locale="en-US",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        context.on("page", lambda p: p.on("dialog", lambda d: asyncio.create_task(d.dismiss())))
        page = context.pages[0] if context.pages else await context.new_page()
        page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))

        try:
            await page.goto("https://app.apollo.io/", wait_until="domcontentloaded", timeout=90000)
        except Exception as exc:  # noqa: BLE001
            log(f"initial goto failed: {exc}")

        await wait_for_login(page, args.login_timeout_minutes)
        await page.wait_for_timeout(3000)
        await dismiss_common_overlays(page)
        await dump(page, "01_after_login")

        body = (await page.evaluate("document.body.innerText")).lower()
        if any(marker in body for marker in CHALLENGE_MARKERS):
            await dump(page, "challenge_home")
            raise SystemExit("CHALLENGE on home page - stopping")

        try:
            await try_people_urls(page, domain)
            await try_company_page(page, domain)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"recon navigation error: {exc}")
            await dump(page, "error_state")

        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / "DONE").write_text("done\n", encoding="utf-8")
        log("RECON DONE")
        log(f"browser held open for {args.hold_minutes} minutes")
        await page.wait_for_timeout(args.hold_minutes * 60 * 1000)
        await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
