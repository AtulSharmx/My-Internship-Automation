"""Targeted zero-credit Apollo recon pass 2.

Opens the already-authenticated persistent profile, inspects the people table,
tooltips, one full-name profile, one masked profile, the company panel and the
Email Status filter. Never clicks an email reveal control.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "apollo_profile"
RUN_DIR = ROOT / "recon" / "out" / f"inspect-{time.strftime('%Y%m%d-%H%M%S')}"
PEOPLE_URL = "https://app.apollo.io/#/people?q_organization_domains_list[]=linear.app"
DOMAIN = "linear.app"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def dump(page: Page, name: str) -> dict:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(RUN_DIR / f"{name}.png"), full_page=False)
    except Exception as exc:  # noqa: BLE001
        log(f"screenshot failed: {exc}")
    html = await page.content()
    (RUN_DIR / f"{name}.html").write_text(html, encoding="utf-8")
    data = await page.evaluate(
        """() => {
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
            };
            const text = (el) => (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
            const buttons = [...document.querySelectorAll('button')].filter(visible).map((el) => ({
                text: text(el).slice(0, 140),
                aria: el.getAttribute('aria-label'),
                tour: el.getAttribute('data-tour-id'),
                disabled: el.disabled,
            })).filter((b) => b.text || b.aria);
            const tooltips = [...document.querySelectorAll('[role="tooltip"], .zp_tooltip, [data-tooltip-content]')]
                .map((el) => text(el)).filter(Boolean);
            const rows = [...document.querySelectorAll('[role="row"][id^="table-row-"]')].map((row) => {
                const cell = (id) => row.querySelector(`[role="cell"][data-id="${id}"]`);
                const nameCell = cell('contact.name');
                const emailCell = cell('contact.emails');
                const accountCell = cell('contact.account');
                const link = nameCell ? nameCell.querySelector('a[href*="/people/"]') : null;
                const emailButton = emailCell ? emailCell.querySelector('button[data-tour-id]') : null;
                return {
                    name: nameCell ? text(nameCell).replace(/------/g, '').trim() : '',
                    name_raw: nameCell ? text(nameCell) : '',
                    masked: nameCell ? !!nameCell.querySelector('button') : false,
                    person_href: link ? link.getAttribute('href') : null,
                    title: cell('contact.job_title') ? text(cell('contact.job_title')).replace(/Copy$/, '').trim() : '',
                    company: accountCell ? text(accountCell) : '',
                    company_html: accountCell ? accountCell.innerHTML.slice(0, 1500) : '',
                    email_button_text: emailButton ? text(emailButton) : '',
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
                buttons,
                tooltips,
                rows,
                credit_text: (document.body ? document.body.innerText : '').match(/\\d+\\s+credits?/i)?.[0] || null,
            };
        }"""
    )
    body = data.pop("bodyText", "")
    (RUN_DIR / f"{name}.txt").write_text(body, encoding="utf-8")
    (RUN_DIR / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    log(f"captured {name}: {data['url']} | rows={len(data.get('rows', []))} | credits={data.get('credit_text')}")
    return data


async def goto_people(page: Page) -> None:
    await page.goto(PEOPLE_URL, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(8000)


async def hover_and_tooltips(page: Page, locator, label: str) -> None:
    try:
        await locator.hover(timeout=5000)
        await page.wait_for_timeout(1500)
        tips = await page.evaluate(
            """() => [...document.querySelectorAll('[role="tooltip"], [data-tooltip-content], .zp_tooltip')]
                .map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean)"""
        )
        # also capture any element that appears positioned near the cursor
        body = await page.evaluate("document.body.innerText")
        log(f"tooltip after hovering {label}: {tips}")
        (RUN_DIR / f"tooltip_{label}.txt").write_text(
            "TOOLTIPS:\n" + "\n".join(tips) + "\n\nBODY:\n" + body, encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        log(f"hover {label} failed: {exc}")


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
        page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))

        try:
            await goto_people(page)
            data = await dump(page, "01_people_rows")
            if data.get("rows"):
                first = data["rows"][0]
                log(f"first row: {json.dumps(first, indent=2)[:1200]}")

            # hover the green email icon button on the first row
            email_btn = page.locator('[role="row"][id^="table-row-"]').first.locator(
                'button[data-tour-id^="email-cell-"]'
            )
            if await email_btn.count():
                await hover_and_tooltips(page, email_btn.first, "email_button")

            # hover the masked-name question mark
            masked_row = page.locator('[role="row"][id^="table-row-"]').filter(has=page.locator('button span:text("------")')).first
            if await masked_row.count():
                await hover_and_tooltips(page, masked_row.locator('[data-id="contact.name"] button').first, "mask")
                await dump(page, "02_masked_tooltip")

            # open a full-name profile
            log("opening full-name profile (row 1)")
            await page.locator('[role="row"][id^="table-row-"]').first.locator(
                '[data-id="contact.name"] a'
            ).first.click()
            await page.wait_for_timeout(7000)
            await dump(page, "03_profile_fullname")
            await goto_people(page)

            # open a masked profile
            log("opening masked profile")
            masked_link = page.locator('[role="row"][id^="table-row-"]').filter(
                has=page.locator('button span:text("------")')
            ).first.locator('[data-id="contact.name"] a').first
            await masked_link.click()
            await page.wait_for_timeout(7000)
            await dump(page, "04_profile_masked")
            await goto_people(page)

            # inspect company cell internals and click through
            log("inspecting company cell")
            company_cell = page.locator('[role="row"][id^="table-row-"]').first.locator(
                '[data-id="contact.account"]'
            ).first
            cell_html = await company_cell.evaluate("el => el.innerHTML")
            (RUN_DIR / "company_cell.html").write_text(cell_html, encoding="utf-8")
            log(f"company cell html: {cell_html[:600]}")
            company_link = company_cell.locator("a, button").first
            if await company_link.count():
                await company_link.click()
            else:
                await company_cell.click()
            await page.wait_for_timeout(7000)
            await dump(page, "05_company_detail")

            # inspect the Email Status filter
            await goto_people(page)
            log("opening Email Status filter")
            try:
                await page.get_by_text("Email Status", exact=True).first.click(timeout=8000)
                await page.wait_for_timeout(2500)
                await dump(page, "06_email_status_filter")
            except Exception as exc:  # noqa: BLE001
                log(f"email status filter click failed: {exc}")

        except Exception as exc:  # noqa: BLE001
            log(f"recon error: {exc}")
            await dump(page, "error_state")
        finally:
            (RUN_DIR / "DONE").write_text("done\n", encoding="utf-8")
            log("INSPECT DONE - browser left open for 5 minutes")
            try:
                await page.wait_for_timeout(5 * 60 * 1000)
            except Exception:  # noqa: BLE001
                pass
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
