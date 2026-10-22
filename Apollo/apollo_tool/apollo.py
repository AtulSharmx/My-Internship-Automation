"""Playwright driver for the Apollo web UI.

Only one credit-spending action exists in this module: ``reveal_email``.
Everything else is navigation and reading what Apollo already shows for free.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .config import Settings
from .models import CompanyFacts, Person, RevealResult
from .names import looks_masked

log = logging.getLogger(__name__)

CHALLENGE_MARKERS = (
    "verify you are human",
    "security check",
    "security challenge",
    "security challenge did not complete",
    "failed to fetch results",
    "widget error",
    "turnstile",
    "verification failed",
    "unusual activity",
    "captcha",
    "are you a robot",
    "challenge required",
    "access denied",
    "just a moment",
)

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
IGNORE_EMAIL_DOMAINS = {
    "apollo.io",
    "example.com",
    "sentry.io",
    "wixpress.com",
    "sentry-next.wixpress.com",
}


class ApolloError(RuntimeError):
    pass


class LoginRequired(ApolloError):
    pass


class ChallengeDetected(ApolloError):
    def __init__(self, message: str, screenshot: Path | None = None) -> None:
        super().__init__(message)
        self.screenshot = screenshot


class RevealCaptureError(ApolloError):
    def __init__(self, message: str, screenshot: Path | None = None, html_path: Path | None = None) -> None:
        super().__init__(message)
        self.screenshot = screenshot
        self.html_path = html_path


def _chrome_binary() -> str:
    configured = os.getenv("APOLLO_CHROME_PATH", "").strip()
    candidates = [
        configured,
        shutil.which("chrome") or "",
        shutil.which("google-chrome") or "",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise ApolloError(
        "Google Chrome was not found. Set APOLLO_CHROME_PATH to chrome.exe in .env."
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class PeoplePage:
    people: list[Person] = field(default_factory=list)
    total_text: str = ""
    org_id: str = ""
    body_text: str = ""


def _title_case(value: str) -> str:
    small = {"and", "or", "of", "for", "to", "in", "the", "a", "an"}
    words = []
    for word in value.split():
        words.append(word if word in {"&", "&amp;"} else (word.lower() if word.lower() in small else word.title()))
    return " ".join(words)


def _clean_lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", line).strip() for line in (text or "").splitlines() if line.strip()]


def _after_label(lines: list[str], labels: tuple[str, ...], max_lines: int = 4) -> str:
    lowered = [line.lower().rstrip(":") for line in lines]
    for idx, line in enumerate(lowered):
        if line in labels:
            for value in lines[idx + 1 : idx + 1 + max_lines]:
                if value.lower().rstrip(":") not in {
                    "industries",
                    "keywords",
                    "number of employees",
                    "annual revenue",
                    "revenue",
                    "stage",
                    "location",
                    "sic codes",
                    "naics codes",
                    "founded year",
                    "founding year",
                    "links",
                    "about",
                    "show all",
                }:
                    return value
    return ""


def parse_company_facts(text: str) -> CompanyFacts:
    lines = _clean_lines(text)
    facts = CompanyFacts()

    employees = ""
    match = re.search(r"Number of employees\s*\n\s*([\d,]+)\s*employees?", text, re.I)
    if match:
        employees = match.group(1).replace(",", "")
    else:
        match = re.search(r"([\d,]+)\s+employees", text, re.I)
        if match:
            employees = match.group(1).replace(",", "")
    facts.employees = employees

    industries: list[str] = []
    for idx, line in enumerate(lines):
        if line.lower().rstrip(":") == "industries":
            for value in lines[idx + 1 : idx + 8]:
                low = value.lower().rstrip(":")
                if low in {
                    "keywords",
                    "number of employees",
                    "annual revenue",
                    "revenue",
                    "stage",
                    "location",
                    "sic codes",
                    "naics codes",
                    "founded year",
                    "founding year",
                    "show all",
                    "links",
                }:
                    break
                if value and value not in industries:
                    industries.append(_title_case(value))
            break
    facts.industries = industries[:6]

    stage = _after_label(lines, ("stage",))
    if stage and stage.lower() not in {"no stage", "n/a", "-", "cold", "warm", "hot"}:
        facts.stage = stage

    revenue = ""
    match = re.search(r"(?:Annual revenue|Revenue)\s*\n\s*(\$[^\n]+)", text, re.I)
    if match:
        revenue = match.group(1).strip()
    facts.revenue = revenue

    founded = ""
    match = re.search(r"(?:Founded year|Founding year)\s*\n\s*(\d{4})", text, re.I)
    if match:
        founded = match.group(1)
    facts.founded = founded

    location = _after_label(lines, ("location",))
    if location:
        facts.location = location

    funding = _after_label(lines, ("recent funding", "funding"))
    if funding and "no relevant funding" not in funding.lower() and funding.lower() not in {"funding", "4"}:
        facts.funding = funding

    keywords: list[str] = []
    for idx, line in enumerate(lines):
        if line.lower().rstrip(":") == "keywords":
            for value in lines[idx + 1 : idx + 9]:
                if value.lower() in {"show all", "number of employees", "industry", "industries"}:
                    break
                if value and value not in keywords:
                    keywords.append(value)
            break
    facts.keywords = keywords[:8]
    return facts


class ApolloBrowser:
    def __init__(self, settings: Settings, hide_window_preference: bool = True) -> None:
        self.settings = settings
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._pw = None
        self._browser: Browser | None = None
        self._chrome_process: subprocess.Popen | None = None
        self._debug_port: int | None = None
        self.hide_window_preference = hide_window_preference
        self.window_hidden = False
        self._cdp = None
        self._window_id: int | None = None
        self._normal_bounds: dict[str, object] | None = None

    @property
    def is_open(self) -> bool:
        return bool(self.context and self.page and not self.page.is_closed())

    async def _ensure_window_handle(self) -> bool:
        if not self.page or not self.context:
            return False
        if self._cdp is not None and self._window_id is not None:
            return True
        try:
            self._cdp = await self.context.new_cdp_session(self.page)
            target = await self._cdp.send("Target.getTargetInfo")
            target_id = (target.get("targetInfo") or {}).get("targetId")
            params = {"targetId": target_id} if target_id else {}
            info = await self._cdp.send("Browser.getWindowForTarget", params)
            self._window_id = int(info["windowId"])
            self._normal_bounds = dict(info.get("bounds") or {})
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not get Chrome window handle: %s", exc)
            self._cdp = None
            self._window_id = None
            return False

    async def hide_window(self) -> None:
        """Move the Chrome window off-screen without minimizing it.

        Off-screen keeps the page rendering normally (minimized Chrome can stop
        producing screenshots), while keeping it out of the user's way.
        """
        if self.settings.headless or self.window_hidden:
            return
        if not await self._ensure_window_handle():
            return
        assert self._cdp is not None and self._window_id is not None
        try:
            current = await self._cdp.send(
                "Browser.getWindowBounds", {"windowId": self._window_id}
            )
            bounds = current.get("bounds") or {}
            if bounds.get("windowState") != "minimized":
                self._normal_bounds = dict(bounds)
            await self._cdp.send(
                "Browser.setWindowBounds",
                {
                    "windowId": self._window_id,
                    "bounds": {
                        "left": -32000,
                        "top": -32000,
                        "width": 1440,
                        "height": 1000,
                        "windowState": "normal",
                    },
                },
            )
            self.window_hidden = True
            log.info("Chrome window moved off-screen")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not hide Chrome window: %s", exc)

    async def show_window(self) -> None:
        if self.settings.headless or not self.window_hidden:
            return
        if not await self._ensure_window_handle():
            return
        assert self._cdp is not None and self._window_id is not None
        try:
            bounds = self._normal_bounds or {
                "left": 80,
                "top": 60,
                "width": 1440,
                "height": 1000,
            }
            restore: dict[str, object] = {
                "windowState": "normal",
                "left": int(bounds.get("left", 80)),
                "top": int(bounds.get("top", 60)),
                "width": int(bounds.get("width", 1440)),
                "height": int(bounds.get("height", 1000)),
            }
            await self._cdp.send(
                "Browser.setWindowBounds",
                {"windowId": self._window_id, "bounds": restore},
            )
            self.window_hidden = False
            if self.page:
                await self.page.bring_to_front()
            log.info("Chrome window restored on-screen")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not show Chrome window: %s", exc)

    async def apply_window_preference(self) -> None:
        if self.settings.headless:
            return
        if self.hide_window_preference:
            await self.hide_window()
        else:
            await self.show_window()

    async def start(self) -> None:
        if self.is_open:
            await self.apply_window_preference()
            return
        if self._browser or self._pw or self._chrome_process:
            await self.close()

        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        chrome = _chrome_binary()
        port = _free_port()
        args = [
            chrome,
            f"--user-data-dir={self.settings.profile_dir}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--start-maximized",
            "https://app.apollo.io/",
        ]
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self._chrome_process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._debug_port = port
        endpoint = f"http://127.0.0.1:{port}"
        ready = False
        for _ in range(80):
            if self._chrome_process.poll() is not None:
                await self.close()
                raise ApolloError(
                    "Chrome exited while starting. Close any Chrome window using the apollo_profile folder and try again."
                )
            try:
                with urllib.request.urlopen(endpoint + "/json/version", timeout=1) as response:
                    if response.status == 200:
                        ready = True
                        break
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.25)
        if not ready:
            await self.close()
            raise ApolloError("Chrome started but the local debugging port did not become ready.")

        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(endpoint)
        except Exception:
            await self.close()
            raise
        if not self._browser.contexts:
            await self.close()
            raise ApolloError("Chrome connected without a browser context.")
        self.context = self._browser.contexts[0]
        pages = [page for page in self.context.pages if not page.is_closed()]
        self.page = pages[-1] if pages else await self.context.new_page()
        self.context.set_default_timeout(30_000)
        self.page.on("dialog", lambda dialog: asyncio.create_task(dialog.dismiss()))
        if self.hide_window_preference:
            await self.hide_window()
        log.info(
            "Chrome started normally with profile %s (debug port %s)",
            self.settings.profile_dir,
            port,
        )

    async def close(self) -> None:
        browser = self._browser
        process = self._chrome_process
        if browser:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
        self._browser = None
        self.context = None
        self.page = None
        self._cdp = None
        self._window_id = None
        self._normal_bounds = None
        self.window_hidden = False
        if process and process.poll() is None:
            try:
                process.terminate()
                await asyncio.to_thread(process.wait, 5)
            except Exception:  # noqa: BLE001
                try:
                    process.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._chrome_process = None
        self._debug_port = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = None
        log.info("Apollo Chrome closed")

    async def _pause(self, low: float | None = None, high: float | None = None) -> None:
        await asyncio.sleep(random.uniform(low or self.settings.page_delay_min, high or self.settings.page_delay_max))

    async def _body(self) -> str:
        assert self.page is not None
        return await self.page.evaluate("document.body ? document.body.innerText : ''")

    async def _check_challenge(self) -> None:
        body = (await self._body()).lower()
        if any(marker in body for marker in CHALLENGE_MARKERS):
            path = await self._screenshot("challenge")
            raise ChallengeDetected(
                "Apollo showed a verification/challenge screen. The browser was left open.",
                screenshot=path,
            )

    async def _check_login(self) -> None:
        assert self.page is not None
        url = self.page.url.lower()
        if "/login" in url or "/signup" in url:
            raise LoginRequired("Apollo is not logged in. Log in in the Chrome window, then try again.")
        try:
            if await self.page.locator('input[type="password"]').count():
                if await self.page.locator('input[type="password"]').first.is_visible():
                    raise LoginRequired("Apollo is showing a login form.")
        except LoginRequired:
            raise
        except Exception:  # noqa: BLE001
            pass

    async def _screenshot(self, name: str) -> Path | None:
        if not self.page:
            return None
        self.settings.logs_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.logs_dir / f"{name}_{time.strftime('%Y%m%d-%H%M%S')}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=False)
            return path
        except Exception:  # noqa: BLE001
            return None

    async def _dump_error(self, name: str) -> tuple[Path | None, Path | None]:
        screenshot = await self._screenshot(name)
        html_path = None
        if self.page:
            html_path = self.settings.logs_dir / f"{name}_{time.strftime('%Y%m%d-%H%M%S')}.html"
            try:
                html_path.write_text(await self.page.content(), encoding="utf-8")
            except Exception:  # noqa: BLE001
                html_path = None
        return screenshot, html_path

    async def goto(self, url: str) -> None:
        assert self.page is not None
        for attempt in range(2):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=90_000)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    raise ApolloError(f"Could not open Apollo page: {exc}") from exc
                await asyncio.sleep(3)
        await self.page.wait_for_timeout(1500)
        await self._check_login()
        await self._check_challenge()

    async def ensure_logged_in(self) -> None:
        await self.goto("https://app.apollo.io/")
        await asyncio.sleep(3)
        # The old check required a visible "/people" nav link. Apollo's SPA can
        # take longer than that and still be fully logged in, which caused false
        # "login required" stops.  A real login page is detected by _check_login;
        # here we only need the application shell to finish rendering.
        for _ in range(10):
            try:
                body = (await self._body()).casefold()
                if any(
                    marker in body
                    for marker in ("people", "companies", "home", "search")
                ):
                    await self._check_login()
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1.5)
        await self._check_login()
        raise LoginRequired(
            "Apollo login was not detected. A Chrome window is open; log in there and try again."
        )

    async def _wait_for_results(self) -> None:
        assert self.page is not None
        try:
            await self.page.wait_for_function(
                """() => {
                    const rows = document.querySelectorAll('[role="row"][id^="table-row-"]');
                    if (rows.length) return true;
                    const body = (document.body ? document.body.innerText : '').toLowerCase();
                    return body.includes('no results') || body.includes('0 people') ||
                           body.includes('no people') || body.includes('results updated') ||
                           body.includes('try adjusting') || body.includes('no contacts') ||
                           body.includes('failed to fetch results') || body.includes('widget error');
                }""",
                timeout=35_000,
            )
        except Exception:  # noqa: BLE001
            pass

    async def _extract_people(self) -> tuple[list[Person], str, str]:
        assert self.page is not None
        data = await self.page.evaluate(
            """() => {
                const text = (el) => (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
                const cleanName = (raw) => raw.replace(/------/g, '').replace(/\\s+/g, ' ').trim();
                const rows = [...document.querySelectorAll('[role="row"][id^="table-row-"]')];
                const out = rows.map((row) => {
                    const cell = (id) => row.querySelector(`[role="cell"][data-id="${id}"]`);
                    const nameCell = cell('contact.name');
                    const emailCell = cell('contact.emails');
                    const accountCell = cell('contact.account');
                    const link = nameCell ? nameCell.querySelector('a[href*="/people/"]') : null;
                    const button = emailCell ? emailCell.querySelector('button[data-tour-id^="email-cell-"]') : null;
                    const rawName = nameCell ? text(nameCell) : '';
                    const orgLink = accountCell ? accountCell.querySelector('a[href*="/organizations/"]') : null;
                    return {
                        name_raw: rawName,
                        name: cleanName(rawName),
                        masked: rawName.includes('------') || !!(nameCell && nameCell.querySelector('button')),
                        href: link ? link.getAttribute('href') : '',
                        title: cell('contact.job_title') ? text(cell('contact.job_title')).replace(/Copy$/, '').trim() : '',
                        company: accountCell ? text(accountCell) : '',
                        email_status: button ? button.getAttribute('data-tour-id') : '',
                        email_button_text: button ? text(button) : '',
                        location: cell('contact.location') ? text(cell('contact.location')) : '',
                        employees: cell('account.number_of_employees') ? text(cell('account.number_of_employees')) : '',
                        industries: cell('account.industries') ? text(cell('account.industries')) : '',
                        org_href: orgLink ? orgLink.getAttribute('href') : '',
                    };
                });
                const body = document.body ? document.body.innerText : '';
                const totalMatch = body.match(/[\\d,]+\\s*-\\s*[\\d,]+\\s+of\\s+[\\d,]+/);
                return { rows: out, total: totalMatch ? totalMatch[0] : '', body };
            }"""
        )
        people: list[Person] = []
        org_id = ""
        for item in data.get("rows", []):
            href = item.get("href") or ""
            key_match = re.search(r"/people/([A-Za-z0-9]+)", href)
            person_key = key_match.group(1) if key_match else f"{item.get('name','')}|{item.get('title','')}"
            raw = item.get("name_raw", "")
            masked = bool(item.get("masked")) or looks_masked(raw)
            name = item.get("name", "")
            if not name:
                continue
            status = item.get("email_status") or ""
            person = Person(
                person_key=person_key,
                name_raw=raw,
                full_name=name,
                masked=masked,
                title=item.get("title", ""),
                href=href,
                location=item.get("location", ""),
                reason=status,
            )
            people.append(person)
            if not org_id:
                org_match = re.search(r"/organizations/([A-Za-z0-9]+)", item.get("org_href") or "")
                if org_match:
                    org_id = org_match.group(1)
        return people, data.get("total", ""), org_id

    async def search_people(self, domain: str, page_num: int = 1) -> PeoplePage:
        url = (
            "https://app.apollo.io/#/people?q_organization_domains_list[]="
            f"{quote(domain)}&page={page_num}"
        )
        await self.goto(url)
        if page_num > 1:
            # Apollo's SPA can keep the previous result set when only the hash
            # page parameter changes. Reloading makes page 2 deterministic.
            assert self.page is not None
            await self.page.reload(wait_until="domcontentloaded", timeout=90_000)
            await self.page.wait_for_timeout(1500)
            await self._check_login()
            await self._check_challenge()
        await self._wait_for_results()
        await self._pause()
        await self._check_challenge()
        people, total, org_id = await self._extract_people()
        body = await self._body()
        return PeoplePage(people=people, total_text=total, org_id=org_id, body_text=body)

    async def search_titles(self, domain: str, titles: list[str]) -> PeoplePage:
        params = "&".join(f"person_titles[]={quote(title)}" for title in titles)
        url = (
            "https://app.apollo.io/#/people?q_organization_domains_list[]="
            f"{quote(domain)}&include_similar_titles=true&{params}"
        )
        await self.goto(url)
        await self._wait_for_results()
        await self._pause()
        await self._check_challenge()
        people, total, org_id = await self._extract_people()
        body = await self._body()
        return PeoplePage(people=people, total_text=total, org_id=org_id, body_text=body)

    async def org_facts(self, org_id: str) -> CompanyFacts:
        if not org_id:
            return CompanyFacts()
        await self.goto(f"https://app.apollo.io/#/organizations/{org_id}")
        await asyncio.sleep(3)
        body = await self._body()
        return parse_company_facts(body)

    async def person_facts(self) -> CompanyFacts:
        body = await self._body()
        return parse_company_facts(body)

    def _read_credits(self, body: str) -> int | None:
        match = re.search(r"(\d{1,6})\s+credits?", body, re.I)
        return int(match.group(1)) if match else None

    async def _extract_emails(self) -> list[str]:
        assert self.page is not None
        raw = await self.page.evaluate(
            """() => {
                const found = new Set();
                const re = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/ig;
                document.querySelectorAll('a[href^="mailto:"]').forEach((a) => {
                    found.add(a.getAttribute('href').slice(7).split('?')[0]);
                });
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                    const text = (node.nodeValue || '').trim();
                    if (text.includes('@')) {
                        (text.match(re) || []).forEach((value) => found.add(value));
                    }
                }
                document.querySelectorAll('input, textarea').forEach((el) => {
                    const value = el.value || '';
                    if (value.includes('@')) (value.match(re) || []).forEach((v) => found.add(v));
                });
                return [...found];
            }"""
        )
        emails: list[str] = []
        for value in raw:
            email = value.strip().strip(".,;:()[]<>").lower()
            if not email or "*" in email:
                continue
            if "@" not in email:
                continue
            domain = email.rsplit("@", 1)[1]
            if domain in IGNORE_EMAIL_DOMAINS or domain.endswith(".apollo.io"):
                continue
            emails.append(email)
        # preserve order, dedupe
        return list(dict.fromkeys(emails))

    async def _contact_section_emails(self) -> list[str]:
        """Emails from the profile's Contact information section, if present."""
        assert self.page is not None
        try:
            section = self.page.locator('[data-testid="accordion-item"]').filter(
                has_text="Contact information"
            ).first
            if not await section.count():
                return []
            text = await section.inner_text()
        except Exception:  # noqa: BLE001
            return []
        emails: list[str] = []
        for value in EMAIL_RE.findall(text):
            email = value.strip().strip(".,;:()[]<>").lower()
            if "*" in email or "@" not in email:
                continue
            if email.rsplit("@", 1)[1] in IGNORE_EMAIL_DOMAINS:
                continue
            emails.append(email)
        return list(dict.fromkeys(emails))

    async def _find_revealed_email(self, expected_domain: str) -> str | None:
        contact = await self._contact_section_emails()
        if contact:
            picked = self._pick_email(contact, expected_domain)
            if picked:
                return picked
        return self._pick_email(await self._extract_emails(), expected_domain)

    def _pick_email(self, emails: list[str], expected_domain: str) -> str | None:
        if not emails:
            return None
        expected = (expected_domain or "").lower().strip(".")
        for email in emails:
            domain = email.rsplit("@", 1)[1]
            if expected and (domain == expected or domain.endswith("." + expected)):
                return email
        for email in emails:
            domain = email.rsplit("@", 1)[1]
            if domain not in {
                "gmail.com",
                "yahoo.com",
                "hotmail.com",
                "outlook.com",
                "icloud.com",
                "protonmail.com",
                "proton.me",
            }:
                return email
        return emails[0]

    async def _profile_identity(self) -> tuple[str, str]:
        assert self.page is not None
        name = ""
        title = ""
        try:
            heading = self.page.locator(
                '[data-testid="entity-page-header"] [role="heading"][aria-level="1"]'
            ).first
            if await heading.count():
                name = (await heading.inner_text()).strip()
        except Exception:  # noqa: BLE001
            pass
        if not name:
            try:
                crumb = self.page.locator(
                    'nav[aria-label="Breadcrumbs"] [aria-current="page"]'
                ).first
                if await crumb.count():
                    name = (await crumb.inner_text()).strip()
            except Exception:  # noqa: BLE001
                pass
        try:
            title_locator = self.page.locator('[data-testid="contact-header-title"]').first
            if await title_locator.count():
                title = (await title_locator.inner_text()).strip()
        except Exception:  # noqa: BLE001
            pass
        if not name or not title:
            try:
                body = await self._body()
                if name:
                    match = re.search(
                        re.escape(name) + r"\s*\n\s*([^\n]+?)\s+at\s+[^\n]+",
                        body,
                    )
                    if match and not title:
                        title = match.group(1).strip()
                if not name:
                    match = re.search(
                        r"(?:^|\n)People\s*\n([^\n]+)\n(?:[A-Z]{1,3}\n)?\1\s*\n([^\n]+?)\s+at\s+",
                        body,
                    )
                    if match:
                        name = match.group(1).strip()
                        if not title:
                            title = match.group(2).strip()
            except Exception:  # noqa: BLE001
                pass
        name = re.sub(r"\s*-{4,}\s*$", "", name).strip()
        return name, title

    async def reveal_email(
        self,
        person_href: str,
        expected_domain: str,
        before_click_cb=None,
    ) -> RevealResult:
        assert self.page is not None
        if person_href.startswith("#"):
            person_href = "https://app.apollo.io/" + person_href
        elif person_href.startswith("/"):
            person_href = "https://app.apollo.io" + person_href
        await self.goto(person_href)
        await asyncio.sleep(4)
        await self._check_challenge()
        await self._check_login()

        # Wait for the profile header to render before deciding the button is missing.
        try:
            await self.page.wait_for_selector(
                'button[data-tour-id^="email-cell-"]',
                state="attached",
                timeout=15_000,
            )
        except Exception:  # noqa: BLE001
            pass

        body = await self._body()
        credits_before = self._read_credits(body)

        buttons = self.page.get_by_role("button", name=re.compile(r"^\s*Access email\s*$", re.I))
        count = await buttons.count()
        if not count:
            screenshot, html_path = await self._dump_error("reveal_no_button")
            raise RevealCaptureError(
                "Could not find the 'Access email' button on the person profile.",
                screenshot=screenshot,
                html_path=html_path,
            )
        button = buttons.first

        if before_click_cb is not None:
            await before_click_cb(credits_before)

        await button.click()
        await asyncio.sleep(2)
        await self._check_challenge()

        email = await self._find_revealed_email(expected_domain)
        body_mid = await self._body()
        credits_mid = self._read_credits(body_mid)
        credit_already_spent = (
            credits_before is not None
            and credits_mid is not None
            and credits_mid < credits_before
        )
        offline = "internet connection appears to be offline" in body_mid.lower()

        if not email and not credit_already_spent and not offline:
            # Some plans show a confirmation modal first. Only click a button in a
            # visible dialog that is clearly about revealing an email. Unrelated
            # dialogs (for example Apollo's Auto-Score upsell) must be ignored.
            try:
                dialogs = self.page.locator('[role="dialog"]:visible')
                unavailable_markers = (
                    "not enough credit",
                    "email is unavailable",
                    "email unavailable",
                    "no email available",
                    "upgrade to access email",
                    "upgrade to reveal email",
                    "upgrade to unlock email",
                )
                confirm_labels = (
                    "Access email",
                    "Confirm",
                    "Continue",
                    "Reveal email",
                    "Unlock email",
                    "Use 1 credit",
                    "Yes, access email",
                )
                for index in range(await dialogs.count()):
                    dialog = dialogs.nth(index)
                    dialog_text = (await dialog.inner_text()).lower()
                    if any(marker in dialog_text for marker in unavailable_markers):
                        screenshot, html_path = await self._dump_error("reveal_unavailable")
                        raise RevealCaptureError(
                            "Apollo reported that this email is unavailable or that there are not enough credits.",
                            screenshot=screenshot,
                            html_path=html_path,
                        )
                    if not any(
                        phrase in dialog_text
                        for phrase in (
                            "access email",
                            "reveal email",
                            "unlock email",
                            "use 1 credit",
                            "confirm email",
                        )
                    ):
                        continue
                    clicked = False
                    for label in confirm_labels:
                        confirm = dialog.get_by_role(
                            "button", name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
                        )
                        if await confirm.count():
                            await confirm.first.click()
                            await asyncio.sleep(2)
                            await self._check_challenge()
                            clicked = True
                            break
                    if clicked:
                        break
            except RevealCaptureError:
                raise
            except Exception:  # noqa: BLE001
                pass

        deadline = time.monotonic() + self.settings.reveal_timeout_seconds
        while not email and not offline and time.monotonic() < deadline:
            await asyncio.sleep(1.2)
            body_loop = await self._body()
            offline = "internet connection appears to be offline" in body_loop.lower()
            email = await self._find_revealed_email(expected_domain)

        # A transient Apollo UI/network failure can hide the email even though the
        # reveal succeeded. Reloading the profile is free and does not click Access
        # email again; Apollo usually shows the already-revealed address.
        if not email:
            log.info("Email not visible after reveal; reloading profile once to recover it")
            try:
                if "/people/" in (self.page.url or ""):
                    await self.page.reload(wait_until="domcontentloaded", timeout=60_000)
                else:
                    await self.goto(person_href)
                await asyncio.sleep(4)
                await self._check_challenge()
                await self._check_login()
                for _ in range(12):
                    email = await self._find_revealed_email(expected_domain)
                    if email:
                        break
                    await asyncio.sleep(1)
                if not email:
                    # One direct navigation as a fallback to the exact profile route.
                    await self.goto(person_href)
                    await asyncio.sleep(4)
                    for _ in range(8):
                        email = await self._find_revealed_email(expected_domain)
                        if email:
                            break
                        await asyncio.sleep(1)
            except (ChallengeDetected, LoginRequired):
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Profile reload recovery failed: %s", exc)

        await asyncio.sleep(1)
        body_after = await self._body()
        credits_after = self._read_credits(body_after)
        # Apollo's header credit counter can lag behind a successful reveal.
        # Poll briefly, then use the known one-credit cost if the UI still shows
        # the old balance. This keeps the ledger and notes accurate.
        if email and credits_before is not None and (
            credits_after is None or credits_after >= credits_before
        ):
            for _ in range(6):
                await asyncio.sleep(1)
                credits_after = self._read_credits(await self._body())
                if credits_after is not None and credits_after < credits_before:
                    break
            if credits_after is None or credits_after >= credits_before:
                credits_after = max(0, credits_before - 1)
        name, title = await self._profile_identity()

        if not email:
            screenshot, html_path = await self._dump_error("reveal_failed")
            raise RevealCaptureError(
                "Clicked 'Access email' but could not read a revealed address. "
                "A credit may have been spent; the company was marked for manual review.",
                screenshot=screenshot,
                html_path=html_path,
            )

        screenshot = await self._screenshot("revealed")
        return RevealResult(
            email=email,
            full_name=name,
            title=title,
            credits_before=credits_before,
            credits_after=credits_after,
            screenshot_path=screenshot,
        )
