"""Phase 1: find the target-sized companies on Wellfound's public role pages.

This phase is plain HTTP.  The public role landing pages
(``https://wellfound.com/role/r/<slug>?page=N``) are allowed by
``robots.txt``, are not behind a security check, and carry the full result
set in their embedded data - so there is no reason to open a browser for them.

Wellfound's own "company size" filter is a UI filter and is **not** exposed as
a URL parameter, but the published size travels with every company, so the
filter is applied here exactly - never approximately.

The company id Wellfound publishes is the identity key: one company has one
id no matter how many roles it is hiring for, so the result is one row per
company.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from ..config import Config
from ..sources.web import HostRateLimiter, RobotsCache
from .apollo import StartupCard, parse_apollo_state, startup_cards
from .roles import role_page_url

log = logging.getLogger("startup_agent.wfscrape.discover")

WELLFOUND_HOST = "wellfound.com"


class DiscoveryError(RuntimeError):
    """A role page could not be read."""


@dataclass(slots=True)
class DiscoveryResult:
    """What phase 1 found."""

    cards: list[StartupCard] = field(default_factory=list)
    pages_fetched: int = 0
    cards_seen: int = 0
    roles_walked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class RoleDiscovery:
    """Read company cards from the public role landing pages."""

    def __init__(
        self,
        cfg: Config,
        *,
        client: httpx.AsyncClient | None = None,
        robots: RobotsCache | None = None,
        limiter: HostRateLimiter | None = None,
    ) -> None:
        self.cfg = cfg
        self._client = client
        self._owns_client = client is None
        self.robots = robots or RobotsCache()
        self.limiter = limiter or HostRateLimiter(cfg.wfscrape_http_delay_seconds)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.cfg.request_timeout_seconds, connect=10.0),
                follow_redirects=False,
                headers={
                    "User-Agent": self.cfg.user_agent,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                    "Accept-Language": "en-US,en;q=0.8",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def fetch_page(self, slug: str, page: int = 1) -> list[StartupCard]:
        """Read one page of a role landing page.

        The URL is checked against ``robots.txt`` first and the request is
        paced, so the run stays inside what the site allows.
        """
        url = role_page_url(slug, page)
        client = self._http()
        await self.limiter.wait(WELLFOUND_HOST)
        decision = await self.robots.check(client, url, self.cfg.user_agent)
        if not decision.allowed:
            raise DiscoveryError(f"{url}: {decision.reason}")
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            raise DiscoveryError(f"{url}: network error ({type(exc).__name__})") from None
        if response.status_code in {401, 403, 429}:
            raise DiscoveryError(
                f"{url}: HTTP {response.status_code}; the run stops here instead of "
                "retrying around it"
            )
        if response.status_code >= 400:
            raise DiscoveryError(f"{url}: HTTP {response.status_code}")
        state = parse_apollo_state(response.text)
        if not state:
            return []
        return startup_cards(state)

    async def discover(
        self,
        roles: list[str],
        *,
        size_code: str,
        max_pages_per_role: int = 3,
        max_companies: int = 150,
        on_card: "callable | None" = None,
    ) -> DiscoveryResult:
        """Walk the role pages and collect the unique target-sized companies."""
        result = DiscoveryResult()
        seen: set[str] = set()
        pages_per_role = max(1, int(max_pages_per_role or 1))
        limit = max(1, int(max_companies or 1))

        for slug in roles:
            if len(result.cards) >= limit:
                break
            result.roles_walked.append(slug)
            for page in range(1, pages_per_role + 1):
                if len(result.cards) >= limit:
                    break
                try:
                    cards = await self.fetch_page(slug, page)
                except DiscoveryError as exc:
                    message = str(exc)
                    log.warning("role page failed: %s", message)
                    result.errors.append(message)
                    # One unreadable page should not abort the whole role.
                    break
                result.pages_fetched += 1
                result.cards_seen += len(cards)
                new_cards = 0
                for card in cards:
                    if card.company_size != size_code:
                        continue
                    if card.company_id in seen:
                        continue
                    seen.add(card.company_id)
                    result.cards.append(card)
                    new_cards += 1
                    if on_card is not None:
                        on_card(card)
                    if len(result.cards) >= limit:
                        break
                log.info(
                    "role %s page %d: %d card(s), %d new %s company(ies)",
                    slug,
                    page,
                    len(cards),
                    new_cards,
                    size_code,
                )
                if not cards:
                    # The role ran out of results; nothing to gain from paging.
                    break
                await asyncio.sleep(self.cfg.wfscrape_http_delay_seconds)
        return result
