"""Zero-credit Apollo recon pass 3: company page, masked profile, pagination, filters."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "apollo_profile"
RUN_DIR = ROOT / "recon" / "out" / f"pass3-{time.strftime('%Y%m%d-%H%M%S')}"
PEOPLE = "https://app.apollo.io/#/people?q_organization_domains_list[]=linear.app"
ORG = "https://app.apollo.io/#/organizations/5dd807f270e72901507f3094"
MASKED_PERSON = "https://app.apollo.io/#/people/6728a25ab7d598000130a0c3"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def dump(page: Page, name: str) -> dict:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(RUN_DIR / f"{name}.png"), full_page=False)
    except Exception as exc:  # noqa: BLE001
        log(f"screenshot failed: {exc}")
    (RUN_DIR / f"{name}.html").write_text(await page.content(), encoding="utf-8")
    data = await page.evaluate(
        """() => {
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
            };
            const text = (el) => (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
            const rows = [...document.querySelectorAll('[role="row"][id^="table-row-"]')].map((row) => {
                const cell = (id) => row.querySelector(`[role="cell"][data-id="${id}"]`);
                const nameCell = cell('contact.name');
                const emailCell = cell('contact.emails');
                const link = nameCell ? nameCell.querySelector('a[href*="/people/"]') : null;
                const emailButton = emailCell ? emailCell.querySelector('button[data-tour-id]') : null;
                return {
                    name_raw: nameCell ? text(nameCell) : '',
                    masked: nameCell ? !!nameCell.querySelector('button') : false,
                    person_href: link ? link.getAttribute('href') : null,
                    title: cell('contact.job_title') ? text(cell('contact.job_title')).replace(/Copy$/, '').trim() : '',
                    email_tour: emailButton ? emailButton.getAttribute('data-tour-id') : null,
                    location: cell('contact.location') ? text(cell('contact.location')) : '',
                    employees: cell('account.number_of_employees') ? text(cell('account.number_of_employees')) : '',
                    industries: cell('account.industries') ? text(cell('account.industries')) : '',
                };
            });
            return {
                url: location.href,
                title: document.title,
                bodyText: document.body ? document.body.innerText : '',
                rows,
                credit_text: (document.body ? document.body.innerText : '').match(/\\d+\\s+credits?/i)?.[0] || null,
                dialogs: [...document.querySelectorAll('[role="dialog"]')].map((d) => text(d).slice(0, 3000)),
            };
        }"""
    )
    body = data.pop("bodyText", "")
    (RUN_DIR / f"{name}.txt").write_text(body, encoding="utf-8")
    (RUN_DIR / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    log(f"captured {name}: {data['url']} | rows={len(data['rows'])} | credits={data['credit_text']}")
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
            # 1. company page
            log("opening organization page")
            await page.goto(ORG, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(8000)
            await dump(page, "01_org")

            # 2. masked person direct URL
            log("opening masked person page")
            await page.goto(MASKED_PERSON, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(7000)
            await dump(page, "02_masked_person")

            # 3. page 2 of people search
            log("opening people search page 2")
            await page.goto(PEOPLE + "&page=2", wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(8000)
            data = await dump(page, "03_people_page2")
            log(f"page2 first 5: {json.dumps([r['name_raw'] for r in data['rows'][:5]])}")

            # 4. Email Status filter
            log("opening Email Status filter")
            await page.goto(PEOPLE, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(8000)
            await page.get_by_text("Email Status", exact=True).first.click(timeout=10000)
            await page.wait_for_timeout(2500)
            await dump(page, "04_email_status_filter")

        except Exception as exc:  # noqa: BLE001
            log(f"error: {exc}")
            await dump(page, "error_state")
        finally:
            (RUN_DIR / "DONE").write_text("done\n", encoding="utf-8")
            log("PASS3 DONE - holding browser 4 minutes")
            try:
                await page.wait_for_timeout(4 * 60 * 1000)
            except Exception:  # noqa: BLE001
                pass
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
