"""Zero-credit Apollo recon pass 4: title-filter URL behaviour."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "apollo_profile"
RUN_DIR = ROOT / "recon" / "out" / f"pass4-{time.strftime('%Y%m%d-%H%M%S')}"
BASE = "https://app.apollo.io/#/people?q_organization_domains_list[]=linear.app&include_similar_titles=true"

QUERIES = [
    ("ceo", ["CEO"]),
    ("founder", ["Founder"]),
    ("cofounder", ["Co-Founder"]),
    ("cto", ["CTO"]),
    ("head_talent", ["Head of Talent"]),
    ("head_people", ["Head of People"]),
    ("head_hr", ["Head of HR"]),
    ("recruiter", ["Recruiter"]),
    ("leadership_combo", ["Founder", "Co-Founder", "CEO", "CTO"]),
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def extract(page: Page, name: str) -> dict:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(RUN_DIR / f"{name}.png"), full_page=False)
    except Exception:  # noqa: BLE001
        pass
    (RUN_DIR / f"{name}.html").write_text(await page.content(), encoding="utf-8")
    data = await page.evaluate(
        """() => {
            const text = (el) => (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
            const rows = [...document.querySelectorAll('[role="row"][id^="table-row-"]')].map((row) => {
                const cell = (id) => row.querySelector(`[role="cell"][data-id="${id}"]`);
                const nameCell = cell('contact.name');
                const emailCell = cell('contact.emails');
                const btn = emailCell ? emailCell.querySelector('button[data-tour-id]') : null;
                return {
                    name: nameCell ? text(nameCell).replace(/------/g, '').trim() : '',
                    masked: nameCell ? !!nameCell.querySelector('button') : false,
                    title: cell('contact.job_title') ? text(cell('contact.job_title')).replace(/Copy$/, '').trim() : '',
                    email: btn ? btn.getAttribute('data-tour-id') : null,
                };
            });
            const body = document.body ? document.body.innerText : '';
            return {
                url: location.href,
                rows,
                total: (body.match(/([\\d,]+)\\s+of\\s+([\\d,]+)/) || [])[0] || null,
                credit: (body.match(/\\d+\\s+credits?/i) || [])[0] || null,
                bodyText: body,
            };
        }"""
    )
    body = data.pop("bodyText", "")
    (RUN_DIR / f"{name}.txt").write_text(body, encoding="utf-8")
    (RUN_DIR / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    log(f"{name}: total={data['total']} rows={len(data['rows'])} credits={data['credit']}")
    for r in data["rows"][:8]:
        log(f"    {r['name']} | {r['title']} | {r['email']} | masked={r['masked']}")
    return data


async def main() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log(f"output dir: {RUN_DIR}")
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            locale="en-US",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            # baseline
            await page.goto(BASE, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(7000)
            await extract(page, "00_all")
            for name, titles in QUERIES:
                params = "&".join(f"person_titles[]={quote(t)}" for t in titles)
                url = f"{BASE}&{params}"
                log(f"query {name}: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(7000)
                await extract(page, name)
        except Exception as exc:  # noqa: BLE001
            log(f"error: {exc}")
        finally:
            (RUN_DIR / "DONE").write_text("done\n", encoding="utf-8")
            log("PASS4 DONE - holding 4 minutes")
            try:
                await page.wait_for_timeout(4 * 60 * 1000)
            except Exception:  # noqa: BLE001
                pass
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
