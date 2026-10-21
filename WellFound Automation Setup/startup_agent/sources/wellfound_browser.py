"""Wellfound company collection through your own visible browser session.

WHAT THIS DOES
    Opens a normal, visible Chromium window with your existing profile (so you
    are already signed in), applies Wellfound's own **"1-10 employees"** filter
    to the startup results, then visits each matching company profile to read
    its official website.  The result is a startup record per company: name,
    website, member count and the Wellfound profile link.

WHAT THIS DOES NOT DO
    It does not spoof the browser, rotate proxies, solve or bypass CAPTCHAs,
    or defeat a login.  If Wellfound shows a challenge or a sign-in wall, the
    collector **stops** and reports it, so you can deal with it yourself in
    the visible window.  Requests are paced like a person reading pages, and
    every run is bounded by ``max_companies``.

    Wellfound's terms do not permit automated harvesting.  You run this on
    your own account, at your own risk, for your own research.

THE FLOW
    1. open https://wellfound.com/jobs in your profile
    2. open the filter drawer, switch off every other company size, switch on
       "1-10 employees", press "View results"
    3. read the startup cards (name + headcount + profile link), scrolling /
       paging until the requested number is gathered or results run out
    4. for every company: open its profile page and read the website
    5. hand each record back to the job manager for size checks, verification,
       dedup, storage and checkpointing
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from ..config import Config
from ..models import Company
from ..normalize import canonical_wellfound_url, clean_text, name_key, normalize_url
from .extract import SOCIAL_AND_DIRECTORY_HOSTS

log = logging.getLogger("startup_agent.wellfound_browser")

WELLFOUND_SEARCH_URL = "https://wellfound.com/jobs"
SIZE_FILTER_ID = "form-input--companySizes--SIZE_1_10"
SIZE_FILTER_PREFIX = "form-input--companySizes--"

#: The startup cards on the search results page.
EXTRACT_STARTUPS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  for (const card of document.querySelectorAll('[data-test="StartupResult"]')) {
    const name = (card.querySelector('h2')?.innerText || '').trim();
    let size = '';
    for (const el of card.querySelectorAll('span')) {
      const text = (el.innerText || '').trim();
      if (/^\d[\d,]*\s*(?:-\s*\d[\d,]*|\+)?\s+Employees?$/i.test(text)) {
        size = text;
        break;
      }
    }
    const link = card.querySelector('a[href^="/company/"]');
    const href = link ? (link.getAttribute('href') || '') : '';
    if (!name || !href) continue;
    const key = href + '|' + name;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      name: name,
      size: size,
      href: href,
      text: (card.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 400),
    });
  }
  return out;
}
"""

#: The company profile page: headcount, official website and tagline.
#: If the structured Website row is absent the script falls back to the first
#: external link that is not a social/directory profile.
EXTRACT_PROFILE_JS = r"""
() => {
  const text = (document.body.innerText || '').replace(/\s+/g, ' ').trim();
  const sizeMatch = text.match(/(\d[\d,]*\s*(?:[-–—]\s*\d[\d,]*|\+)?)\s+Employees?/i);
  let website = '';
  let source = '';
  const labels = Array.from(document.querySelectorAll('dd'));
  const websiteLabel = labels.find((d) => /^website$/i.test((d.innerText || '').trim()));
  if (websiteLabel) {
    const holder = websiteLabel.nextElementSibling;
    if (holder) {
      const button = holder.querySelector('button[class*="websiteLink"], a[href^="http"]');
      if (button) {
        website = (button.innerText || '').trim() || (button.getAttribute('href') || '').trim();
        source = 'Website section';
      }
    }
  }
  if (!website) {
    for (const button of document.querySelectorAll('button')) {
      if (/websiteLink/i.test(button.className || '')) {
        website = (button.innerText || '').trim();
        source = 'Website button';
        break;
      }
    }
  }
  if (!website) {
    const blocked = /(?:wellfound\.com|linkedin\.com|twitter\.com|x\.com|facebook\.com|instagram\.com|youtube\.com|github\.com|medium\.com|crunchbase\.com|angel\.co|glassdoor\.com|indeed\.com|producthunt\.com|ycombinator\.com)/i;
    for (const a of document.querySelectorAll('a[href^="http"]')) {
      const href = (a.getAttribute('href') || '').trim();
      if (!href || blocked.test(href)) continue;
      website = href;
      source = 'external link';
      break;
    }
  }
  const taglineEl = document.querySelector('[data-test="Overview"] h2, main h2, main h3');
  const tagline = taglineEl ? (taglineEl.innerText || '').trim().slice(0, 300) : '';
  const nameEl = document.querySelector('h1');
  const companyName = nameEl ? (nameEl.innerText || '').trim().slice(0, 200) : '';
  return {
    size: sizeMatch ? sizeMatch[0] : '',
    website: website,
    website_source: source,
    tagline: tagline,
    name: companyName,
    text: text.slice(0, 1500),
  };
}
"""

_ACTIVE_FILTER_LABELS_JS = r"""
() => Array.from(document.querySelectorAll(
  '[data-test^="ActiveFilterSummary-FilterButton--"]'
)).map((el) => el.getAttribute('aria-label') || '')
"""

#: Scroll the result list, then click a Next/Load more control if one exists.
LOAD_MORE_JS = r"""
() => {
  window.scrollTo(0, document.body.scrollHeight);
  const controls = Array.from(document.querySelectorAll('button, a'));
  const next = controls.find((el) => {
    const label = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '')).trim();
    return /^(next|next page|load more|show more|more results)$/i.test(label);
  });
  if (next && !next.disabled) {
    next.click();
    return 'clicked';
  }
  return 'scrolled';
}
"""

_CHALLENGE_MARKERS = (
    "just a moment",
    "verify you are human",
    "checking your browser",
    "enable javascript and cookies to continue",
    "access denied",
)

#: External links that are never a company's official website.
_NON_OFFICIAL_HOSTS = SOCIAL_AND_DIRECTORY_HOSTS | {
    "wellfound.com",
    "angel.co",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "github.com",
    "medium.com",
    "crunchbase.com",
    "glassdoor.com",
    "indeed.com",
    "producthunt.com",
    "ycombinator.com",
    "f6s.com",
    "lever.co",
    "greenhouse.io",
    "ashbyhq.com",
    "workday.com",
    "smartrecruiters.com",
    "jobvite.com",
    "recruitee.com",
    "bamboohr.com",
    "notion.site",
}


class BrowserError(RuntimeError):
    """The browser could not be driven."""


class ChallengeError(BrowserError):
    """Wellfound asked for a CAPTCHA/login - the run stops, it is not bypassed."""


@dataclass(slots=True)
class CollectProgress:
    """Progress callback payload."""

    message: str
    found: int = 0
    visited: int = 0


@dataclass(slots=True)
class ProfileCard:
    """One company card on the Wellfound results page."""

    name: str
    profile_url: str
    size: str = ""
    text: str = ""


@dataclass(slots=True)
class ProfileData:
    """What a company profile page publishes."""

    name: str = ""
    website: str = ""
    website_source: str = ""
    size: str = ""
    tagline: str = ""
    profile_url: str = ""


def _clean_website(value: str) -> str:
    """Wellfound shows a bare host such as ``www.getcerta.com/``."""
    text = (value or "").strip()
    if not text:
        return ""
    # A button can show a label such as "Visit website" instead of a URL.
    if not re.search(r"[a-z0-9-]+\.[a-z]{2,}", text, flags=re.I):
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text.lstrip("/")
    url = normalize_url(text)
    if not url:
        return ""
    host = (url.split("//", 1)[-1].split("/", 1)[0] or "").lower()
    host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    if not host or host in _NON_OFFICIAL_HOSTS:
        return ""
    if any(host.endswith("." + blocked) for blocked in _NON_OFFICIAL_HOSTS):
        return ""
    return url


def _looks_like_url(value: str) -> bool:
    """True for text that is a URL rather than a company name."""
    text = (value or "").strip().lower()
    if text.startswith(("http://", "https://", "www.")):
        return True
    return "/" in text and "." in text


def _clean_profile_name(value: str) -> str:
    """Profile page titles look like "Certa Careers - Insights and Opportunities"."""
    text = clean_text(value, 200)
    parts = re.split(r"\s+careers\b", text, flags=re.IGNORECASE)
    cleaned = parts[0].strip(" -–—|·")
    return cleaned or text


class WellfoundBrowserCollector:
    """Collect 1-10 employee startups (name, website, members) via a browser."""

    def __init__(
        self,
        cfg: Config,
        *,
        profile_dir: str | Path | None = None,
        headless: bool = False,
        progress: Callable[[CollectProgress], Awaitable[None]] | None = None,
        nav_timeout_ms: int = 45_000,
        min_delay: float = 1.2,
        max_delay: float = 2.4,
        max_scrolls: int = 25,
    ) -> None:
        self.cfg = cfg
        self.profile_dir = Path(profile_dir or cfg.browser_profile)
        self.headless = headless
        self.progress = progress
        self.nav_timeout_ms = max(5_000, int(nav_timeout_ms))
        self.min_delay = max(0.0, float(min_delay))
        self.max_delay = max(self.min_delay, float(max_delay))
        self.max_scrolls = max(1, int(max_scrolls))
        self._playwright = None
        self._context = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on the machine
            raise BrowserError(
                "Playwright is not installed. Run: "
                ".venv\\Scripts\\python.exe -m pip install -r requirements.txt"
            ) from exc
        self._playwright = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
        except Exception as exc:  # noqa: BLE001 - locked profile is a normal case
            await self._stop_playwright()
            raise BrowserError(
                "The browser profile could not be opened. Close any Chrome window "
                "that is using "
                f"{self.profile_dir.name!r}, then try again. ({type(exc).__name__})"
            ) from exc

    async def _stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
            self._context = None
        await self._stop_playwright()

    async def __aenter__(self) -> "WellfoundBrowserCollector":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _pause(self, low: float | None = None, high: float | None = None) -> None:
        """Pace the browsing like a person reading pages."""
        low = self.min_delay if low is None else low
        high = self.max_delay if high is None else high
        await asyncio.sleep(random.uniform(low, max(low, high)))

    async def _report(self, message: str, found: int = 0, visited: int = 0) -> None:
        if self.progress is not None:
            try:
                await self.progress(CollectProgress(message, found, visited))
            except Exception:  # noqa: BLE001 - progress must not break a run
                pass

    def _page(self):
        if self._context is None:
            raise BrowserError("the browser is not running")
        pages = self._context.pages
        return pages[0] if pages else self._context.new_page()

    async def _check_challenge(self, page) -> None:
        """Stop on a CAPTCHA/login wall - never try to get around it."""
        try:
            body = (await page.inner_text("body"))[:2500].lower()
        except Exception:  # noqa: BLE001
            return
        for marker in _CHALLENGE_MARKERS:
            if marker in body:
                raise ChallengeError(
                    "Wellfound showed a verification/sign-in page. The collector "
                    "stopped instead of bypassing it - please open the browser "
                    "window and complete the check yourself, then resume the scan."
                )

    # ------------------------------------------------------------------
    # filter
    # ------------------------------------------------------------------
    async def _size_filter_active(self, page) -> bool:
        labels = await page.evaluate(_ACTIVE_FILTER_LABELS_JS)
        return any("1-10 employees" in str(label).lower() for label in labels)

    async def _apply_size_filter(self, page) -> bool:
        """Switch Wellfound's own company-size filter to 1-10 employees."""
        if await self._size_filter_active(page):
            return True

        toggle = page.locator('[data-test="SearchBar-ToggleFilterControlPanelButton"]')
        if not await toggle.count():
            toggle = page.get_by_role("button", name="Filters", exact=True)
        if not await toggle.count():
            log.warning("Wellfound search has no Filters button")
            return False
        await toggle.first.click()
        await self._pause(1.4, 2.4)

        label = page.locator(f'label[for="{SIZE_FILTER_ID}"]')
        if not await label.count():
            log.warning("Wellfound has no %s filter", SIZE_FILTER_ID)
            return False

        # Switch off every other company size, keep only 1-10.
        for index in range(await page.locator(f'input[id^="{SIZE_FILTER_PREFIX}"]').count()):
            box = page.locator(f'input[id^="{SIZE_FILTER_PREFIX}"]').nth(index)
            box_id = await box.get_attribute("id") or ""
            if box_id == SIZE_FILTER_ID:
                continue
            try:
                if await box.is_checked():
                    other = page.locator(f'label[for="{box_id}"]')
                    if await other.count():
                        await other.first.evaluate("el => el.click()")
                        await self._pause(0.4, 0.9)
            except Exception:  # noqa: BLE001 - a stubborn filter is not fatal
                continue

        checkbox = page.locator(f'input[id="{SIZE_FILTER_ID}"]')
        await checkbox.first.wait_for(state="attached", timeout=8_000)
        for _ in range(2):
            if await checkbox.first.is_checked():
                break
            await label.first.evaluate("el => el.click()")
            await self._pause(0.8, 1.4)
        if not await checkbox.first.is_checked():
            log.warning("could not switch on the 1-10 employees filter")
            return False

        view = page.get_by_role("button", name="View results", exact=False)
        if await view.count():
            await view.first.click()
            await self._pause(2.5, 4.0)
        return await self._size_filter_active(page)

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    async def _open_search(self) -> object:
        page = self._page()
        await page.goto(
            WELLFOUND_SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=max(60_000, self.nav_timeout_ms),
        )
        await self._pause(2.0, 3.2)
        await self._check_challenge(page)
        applied = await self._apply_size_filter(page)
        if not applied:
            raise BrowserError(
                "Could not apply the 1-10 employees filter. The collector stopped "
                "rather than guessing the result set."
            )
        await self._report("Filter applied: 1-10 employees", 0, 0)
        return page

    async def discover_profiles(
        self, max_profiles: int, *, on_card: Callable[[ProfileCard], None] | None = None
    ) -> list[ProfileCard]:
        """Read company cards, scrolling/paging until ``max_profiles``.

        Only the Wellfound results list is touched here.  Profile visits happen
        later, one at a time, so a crash or pause never loses the discovered
        queue.
        """
        limit = max(1, int(max_profiles))
        page = await self._open_search()
        cards: list[ProfileCard] = []
        seen: set[str] = set()
        stagnant = 0
        for _ in range(self.max_scrolls):
            found = await page.evaluate(EXTRACT_STARTUPS_JS)
            new_cards = 0
            for raw in found:
                href = str(raw.get("href") or "")
                profile = canonical_wellfound_url("https://wellfound.com" + href)
                name = clean_text(raw.get("name", ""), 200)
                if not profile or not name or profile in seen:
                    continue
                seen.add(profile)
                card = ProfileCard(
                    name=name,
                    profile_url=profile,
                    size=clean_text(raw.get("size", ""), 100),
                    text=clean_text(raw.get("text", ""), 400),
                )
                cards.append(card)
                new_cards += 1
                if on_card is not None:
                    on_card(card)
                if len(cards) >= limit:
                    break
            await self._report(f"Found {len(cards)} startup card(s)", len(cards), 0)
            if len(cards) >= limit:
                break
            stagnant = stagnant + 1 if new_cards == 0 else 0
            if stagnant >= 3:
                break
            try:
                await page.evaluate(LOAD_MORE_JS)
            except Exception:  # noqa: BLE001 - scrolling is best-effort
                pass
            await self._pause(1.2, 2.2)
            await self._check_challenge(page)
            # Pagination must never silently drop the size filter.
            try:
                still_filtered = await self._size_filter_active(page)
            except Exception:  # noqa: BLE001 - a transient render
                still_filtered = True
            if not still_filtered:
                raise BrowserError(
                    "The 1-10 employees filter was lost while paging. The "
                    "collector stopped rather than collecting unfiltered results."
                )
        return cards[:limit]

    async def inspect_profile(self, profile_url: str) -> ProfileData:
        """Open one company profile and read its published fields."""
        page = self._page()
        try:
            await page.goto(
                profile_url,
                wait_until="domcontentloaded",
                timeout=self.nav_timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001 - the manager retries
            raise BrowserError(
                f"could not open the profile ({type(exc).__name__})"
            ) from exc
        await self._pause()
        await self._check_challenge(page)
        try:
            data = await page.evaluate(EXTRACT_PROFILE_JS)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(
                f"could not read the profile ({type(exc).__name__})"
            ) from exc
        website = _clean_website(str(data.get("website") or ""))
        size_text = clean_text(str(data.get("size") or ""), 40)
        tagline = clean_text(str(data.get("tagline") or ""), 300)
        company_name = clean_text(str(data.get("name") or ""), 200)
        if tagline and tagline.lower() == str(data.get("text") or "")[: len(tagline)].lower():
            tagline = ""
        if not website:
            log.info("no website listed on %s", profile_url)
        return ProfileData(
            name=company_name,
            website=website,
            website_source=clean_text(str(data.get("website_source") or ""), 40),
            size=size_text,
            tagline=tagline,
            profile_url=profile_url,
        )

    # ------------------------------------------------------------------
    # whole-run helper (kept for the CLI and existing integrations)
    # ------------------------------------------------------------------
    async def collect(self, max_companies: int = 25) -> list[Company]:
        """Return the companies found, with website and member count."""
        cards = await self.discover_profiles(max_companies)
        companies: list[Company] = []
        for card in cards:
            data = await self.inspect_profile(card.profile_url)
            name = card.name
            if _looks_like_url(name):
                cleaned = _clean_profile_name(data.name)
                if cleaned and not _looks_like_url(cleaned):
                    name = cleaned
            website = data.website
            if _looks_like_url(name) and website:
                host = website.split("//", 1)[-1].split("/", 1)[0]
                if host.startswith("www."):
                    host = host[4:]
                if host:
                    name = host
            size_text = data.size or card.size
            company = Company(
                record_id="",
                name=name,
                website=website,
                employee_count=size_text,
                industry="",
                description=data.tagline,
                wellfound_url=card.profile_url,
                location="",
                source_url=card.profile_url,
                source_name="wellfound",
                source_id=card.profile_url.rstrip("/").split("/")[-1],
                notes=(
                    "Collected from the Wellfound company profile in your own "
                    "browser session; headcount and website are as published there."
                ),
                raw={
                    "wellfound_profile": card.profile_url,
                    "card_text": card.text,
                    "website_source": data.website_source,
                },
            )
            company.name_key = name_key(company.name)
            companies.append(company)
            await self._report(f"Read {name}", len(companies), len(companies))
            await self._pause()
        return companies
