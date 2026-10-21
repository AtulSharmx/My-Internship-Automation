"""Phase 2: read each company profile page for its own website and its tags.

The public role pages do not carry a company's external website - that field
lives on the company's own Wellfound profile.  This phase opens those profile
pages in a plain browser and reads the same embedded Next.js/Apollo data the
page renders from.

There is no account and no sign-in here.  The browser profile only ever holds
the anonymous Cloudflare clearance cookie.

Wellfound puts a security check in front of the profile pages.  It is a
challenge, not a login: a real browser clears it by itself.  This module waits
for it to clear and, if it does not, **stops and reports** - it never tries to
work around it, spoof a fingerprint or rotate a user agent.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Callable

from ..config import Config
from ..normalize import normalize_url, website_key
from ..sources.extract import SOCIAL_AND_DIRECTORY_HOSTS
from .apollo import StartupProfile, find_profile, parse_apollo_state, size_code_for

log = logging.getLogger("startup_agent.wfscrape.enrich")

WELLFOUND_BASE = "https://wellfound.com"

#: Signs of the security check page.
CHALLENGE_MARKERS = (
    "just a moment",
    "security check",
    "checking your browser",
    "enable javascript and cookies to continue",
    "verify you are human",
)

#: The visible website button on a company profile, used only as a fallback
#: when the embedded data does not carry ``companyUrl``.
_WEBSITE_DOM_JS = r"""
() => {
  const button = document.querySelector('button[class*="websiteLink"]');
  if (button) {
    const text = (button.innerText || '').trim();
    if (text) return text;
  }
  const labels = Array.from(document.querySelectorAll('dd'));
  const label = labels.find((d) => /^website$/i.test((d.innerText || '').trim()));
  if (label) {
    const holder = label.nextElementSibling;
    if (holder) {
      const link = holder.querySelector('a[href^="http"]');
      if (link) return (link.getAttribute('href') || '').trim();
    }
  }
  return '';
}
"""

#: The visible headcount, used only as a fallback.
_SIZE_DOM_JS = r"""
() => {
  const text = (document.body.innerText || '').replace(/\s+/g, ' ');
  const match = text.match(/(\d[\d,]*\s*(?:[-–—]\s*\d[\d,]*|\+)?)\s+Employees?/i);
  return match ? match[0] : '';
}
"""

_SIZE_TEXT_RE = re.compile(
    r"(\d[\d,]*)\s*(?:[-–—]\s*(\d[\d,]*))?\s*(?:\+|Employees?)", re.IGNORECASE
)


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed or the browser could not be started."""


class ChallengeError(RuntimeError):
    """The security check did not clear.  The run stops; it is not bypassed."""


def clean_website(value: str) -> str:
    """Turn a published website into a plain https URL, or ``""``.

    A social profile or a directory listing is not a company's official
    website, so it is dropped rather than exported.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.search(r"[a-z0-9-]+\.[a-z]{2,}", text, flags=re.I):
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text.lstrip("/")
    url = normalize_url(text)
    if not url:
        return ""
    host = website_key(url)
    if not host:
        # A social/directory host, or the Wellfound profile itself.
        return ""
    if host in SOCIAL_AND_DIRECTORY_HOSTS:
        return ""
    return url


def parse_size_text(value: str) -> str:
    """Turn ``"1-10 Employees"`` into Wellfound's ``"SIZE_1_10"``."""
    text = str(value or "").strip()
    match = _SIZE_TEXT_RE.search(text)
    if not match:
        return ""
    low = int(match.group(1).replace(",", ""))
    high = match.group(2)
    if high:
        high_value = int(high.replace(",", ""))
        label = f"{low}-{high_value}"
    elif "+" in text:
        label = f"{low}+"
    else:
        label = str(low)
    try:
        return size_code_for(label)
    except ValueError:
        return ""


class ProfileFetcher:
    """Open Wellfound company profile pages in a plain browser."""

    def __init__(
        self,
        cfg: Config,
        *,
        headless: bool | None = None,
        progress: Callable[[str], None] | None = None,
        profile_dir: Path | str | None = None,
    ) -> None:
        self.cfg = cfg
        self.headless = cfg.wfscrape_headless if headless is None else bool(headless)
        self.progress = progress
        self.profile_dir = Path(profile_dir or cfg.wfscrape_profile)
        self._playwright = None
        self._context = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on the machine
            raise BrowserUnavailable(
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
        except Exception as exc:  # noqa: BLE001 - a locked profile is normal
            await self.close()
            raise BrowserUnavailable(
                "The browser profile could not be opened. Close any browser "
                f"window using {self.profile_dir.name!r} and try again. "
                f"({type(exc).__name__})"
            ) from exc

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
            self._context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None

    async def __aenter__(self) -> "ProfileFetcher":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _page(self):
        """The page to drive.  Playwright's ``new_page`` is a coroutine."""
        if self._context is None:
            raise BrowserUnavailable("the browser is not running")
        pages = self._context.pages
        return pages[0] if pages else await self._context.new_page()

    async def _looks_like_challenge(self, page) -> bool:
        try:
            title = (await page.title()).lower()
        except Exception:  # noqa: BLE001
            title = ""
        if any(marker in title for marker in CHALLENGE_MARKERS):
            return True
        try:
            body = (await page.inner_text("body"))[:2000].lower()
        except Exception:  # noqa: BLE001
            return False
        return any(marker in body for marker in CHALLENGE_MARKERS)

    async def _wait_for_challenge(self, page) -> bool:
        """Give the security check time to clear on its own."""
        wait = max(0, int(self.cfg.wfscrape_challenge_wait_seconds))
        if wait <= 0:
            return not await self._looks_like_challenge(page)
        deadline = asyncio.get_running_loop().time() + wait
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(3.0)
            if not await self._looks_like_challenge(page):
                return True
        return not await self._looks_like_challenge(page)

    async def _wait_for_profile_data(self, page, slug: str, timeout: float = 20.0):
        """Wait for the company record to arrive in the embedded data.

        The data island is part of the server-rendered HTML, so this only has
        to cover the moment right after a security check clears.
        """
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while True:
            try:
                markup = await page.content()
            except Exception:  # noqa: BLE001 - the page may be navigating
                markup = ""
            profile = find_profile(parse_apollo_state(markup), slug)
            if profile is not None:
                return profile, markup
            if asyncio.get_running_loop().time() >= deadline:
                return None, markup
            await asyncio.sleep(2.0)

    def _report(self, message: str) -> None:
        if self.progress is None:
            return
        try:
            self.progress(message)
        except Exception:  # noqa: BLE001 - progress must never break a run
            pass

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    async def fetch(self, slug: str) -> StartupProfile:
        """Read one company profile page."""
        page = await self._page()
        url = f"{WELLFOUND_BASE}/company/{slug}"
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=max(5_000, int(self.cfg.wfscrape_nav_timeout_ms)),
            )
        except Exception as exc:  # noqa: BLE001 - the caller retries
            raise BrowserUnavailable(
                f"{url}: could not open the page ({type(exc).__name__})"
            ) from exc

        if await self._looks_like_challenge(page):
            self._report("Wellfound showed a security check - waiting for it to clear")
            if not await self._wait_for_challenge(page):
                raise ChallengeError(
                    f"{url}: the security check did not clear within "
                    f"{self.cfg.wfscrape_challenge_wait_seconds}s. The run stopped "
                    "instead of working around it. Run `scrape-prime` once in a "
                    "visible browser, then try again."
                )

        status = response.status if response is not None else None
        if status in {403, 429}:
            raise ChallengeError(
                f"{url}: HTTP {status}. The run stopped instead of retrying around it."
            )

        try:
            markup = await page.content()
        except Exception as exc:  # noqa: BLE001
            raise BrowserUnavailable(
                f"{url}: could not read the page ({type(exc).__name__})"
            ) from exc

        state = parse_apollo_state(markup)
        profile = find_profile(state, slug)
        if profile is None:
            # The page may still be settling right after a challenge cleared.
            profile, markup = await self._wait_for_profile_data(page, slug)
            state = parse_apollo_state(markup)
        if profile is None:
            profile = StartupProfile(company_id="", name="", slug=slug)
            profile.website_source = ""

        if not profile.company_url:
            fallback = await self._dom_website(page)
            cleaned = clean_website(fallback)
            if cleaned:
                profile.company_url = cleaned
                profile.website_source = "Wellfound company profile (visible website row)"
        if not profile.company_size:
            fallback = await self._dom_size(page)
            code = parse_size_text(fallback)
            if code:
                profile.company_size = code
        if not profile.name:
            profile.name = await self._dom_name(page, slug)
        return profile

    async def _dom_website(self, page) -> str:
        try:
            return str(await page.evaluate(_WEBSITE_DOM_JS) or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _dom_size(self, page) -> str:
        try:
            return str(await page.evaluate(_SIZE_DOM_JS) or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _dom_name(self, page, slug: str) -> str:
        try:
            text = str(await page.evaluate("() => (document.querySelector('h1')?.innerText || '').trim()") or "")
        except Exception:  # noqa: BLE001
            text = ""
        return text[:200] or slug.replace("-", " ").title()

    async def prime(self, slug: str = "mattermost", attempts: int = 3) -> bool:
        """Clear the security check once, in a visible browser.

        The browser profile keeps the clearance cookie, so every later run can
        stay headless.  The check is intermittent - it clears on some attempts
        and not on others - so the sequence is retried a few times, and success
        means the company data really came back rather than merely that the
        challenge page went away.
        """
        self._report("Opening a visible browser to clear the security check...")
        for attempt in range(1, max(1, int(attempts)) + 1):
            self._report(f"  attempt {attempt} of {attempts}")
            for url in (
                f"{WELLFOUND_BASE}/",
                f"{WELLFOUND_BASE}/company/{slug}",
            ):
                page = await self._page()
                try:
                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=max(5_000, int(self.cfg.wfscrape_nav_timeout_ms)),
                    )
                except Exception as exc:  # noqa: BLE001
                    raise BrowserUnavailable(
                        f"{url}: could not open the page ({type(exc).__name__})"
                    ) from exc
                if await self._looks_like_challenge(page):
                    if not await self._wait_for_challenge(page):
                        self._report(f"  {url} is still showing the security check")
                        break
                self._report(f"  {url} opened")
                await asyncio.sleep(2.0)
            else:
                # Both pages opened.  Confirm the company data really arrived.
                profile, _markup = await self._wait_for_profile_data(
                    await self._page(), slug
                )
                if profile is not None:
                    return True
                self._report("  the page opened but the company data did not load")
            await asyncio.sleep(5.0)
        return False
