"""Permitted, user-initiated public URL discovery.

Safeguards enforced here:

* URL discovery is off unless explicitly enabled.
* Every host must be in ``STARTUP_ALLOWED_DOMAINS``.
* ``wellfound.com`` is always blocked - its terms prohibit automated harvesting.
* ``robots.txt`` is fetched and obeyed; access-denied/rate-limit responses stop
  the run instead of being retried around.
* Requests are rate-limited per host and use an honest user agent.
* No cookies, CAPTCHA solving, proxy rotation, stealth patches or fingerprint
  spoofing are used.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..config import Config
from ..models import Company
from ..normalize import normalize_url
from .extract import extract_companies, parse_page

log = logging.getLogger("startup_agent.web")

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


class SourceError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ComplianceError(SourceError):
    """The source says no, or access control blocks the request."""


@dataclass(slots=True)
class RobotsDecision:
    allowed: bool
    reason: str = ""
    retryable: bool = False


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _safe_display_url(url: str) -> str:
    """URL for logs without a potentially sensitive query string."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def is_wellfound_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return (
        host == "wellfound.com"
        or host.endswith(".wellfound.com")
        or host == "angel.co"
        or host.endswith(".angel.co")
    )


class HostRateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        if self.min_interval <= 0:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            delay = self.min_interval - (now - last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = time.monotonic()


class RobotsCache:
    def __init__(self, *, ttl_seconds: float = 3600.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, RobotsDecision]] = {}

    async def check(self, client: httpx.AsyncClient, url: str, user_agent: str) -> RobotsDecision:
        host = _host(url)
        cached = self._cache.get(host)
        if cached and (time.monotonic() - cached[0]) < self.ttl_seconds:
            return cached[1]
        scheme = urlsplit(url).scheme or "https"
        robots_url = f"{scheme}://{host}/robots.txt"
        # A transient network/5xx failure while reading the policy is retried a
        # couple of times.  A policy decision (401/403/429/disallow) is final
        # and is never retried around.
        attempts = 3
        decision: RobotsDecision | None = None
        for attempt in range(attempts):
            try:
                response = await client.get(
                    robots_url,
                    headers={"User-Agent": user_agent, "Accept": "text/plain,*/*;q=0.1"},
                    timeout=15.0,
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                decision = RobotsDecision(
                    False,
                    f"robots.txt could not be fetched ({type(exc).__name__})",
                    retryable=True,
                )
            else:
                if 300 <= response.status_code < 400:
                    decision = RobotsDecision(
                        False, "robots.txt redirected; not following a policy redirect"
                    )
                elif response.status_code in {401, 403, 429}:
                    decision = RobotsDecision(
                        False, f"robots.txt returned HTTP {response.status_code}; stopping"
                    )
                elif response.status_code >= 500:
                    decision = RobotsDecision(
                        False,
                        f"robots.txt returned HTTP {response.status_code}; stopping",
                        retryable=True,
                    )
                elif response.status_code == 404:
                    decision = RobotsDecision(True, "robots.txt not found")
                else:
                    parser = RobotFileParser()
                    parser.parse(response.text.splitlines())
                    allowed = parser.can_fetch(user_agent, url) and parser.can_fetch("*", url)
                    decision = RobotsDecision(
                        allowed,
                        "allowed by robots.txt" if allowed else "disallowed by robots.txt",
                    )
            if decision is not None and not decision.retryable:
                break
            if attempt + 1 < attempts:
                await asyncio.sleep(1.5 * (attempt + 1))
        if decision is None:
            decision = RobotsDecision(False, "robots.txt could not be fetched")
        if not decision.retryable:
            # Only a settled policy decision is cached; a transient failure is
            # retried on the next request instead of being remembered.
            self._cache[host] = (time.monotonic(), decision)
        return decision


class WebDiscoverySource:
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
        self.limiter = limiter or HostRateLimiter(cfg.min_request_interval_seconds)

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

    def check_url(self, value: str) -> tuple[bool, str, str]:
        """Return ``(allowed, normalized_url, reason)`` without any request."""
        url = normalize_url(value)
        if not url:
            return False, "", "not a valid http(s) URL"
        host = _host(url)
        if is_wellfound_host(host):
            return (
                False,
                url,
                "Wellfound's terms prohibit automated harvesting. "
                "Save/export the data manually and use Import Data instead.",
            )
        if not self.cfg.enable_url_discovery:
            return (
                False,
                url,
                "URL discovery is disabled. Set STARTUP_ENABLE_URL_DISCOVERY=true "
                "and STARTUP_ALLOWED_DOMAINS to sites whose terms permit your use.",
            )
        if not self.cfg.domain_allowed(host):
            return (
                False,
                url,
                f"{host} is not in STARTUP_ALLOWED_DOMAINS. Add only sources you are "
                "authorized to query.",
            )
        return True, url, ""

    async def fetch_companies(self, value: str, *, source_name: str = "") -> list[Company]:
        allowed, url, reason = self.check_url(value)
        if not allowed:
            raise ComplianceError(reason)
        client = self._http()
        host = _host(url)
        await self.limiter.wait(host)
        decision = await self.robots.check(client, url, self.cfg.user_agent)
        if not decision.allowed:
            raise ComplianceError(decision.reason)
        final_url, content_type, body = await self._request_with_retries(client, url)
        final_host = _host(final_url)
        if is_wellfound_host(final_host):
            raise ComplianceError(
                "the URL redirected to Wellfound; automated harvesting is not permitted"
            )
        if not self.cfg.domain_allowed(final_host):
            raise ComplianceError(
                f"the URL redirected to {final_host}, which is not in STARTUP_ALLOWED_DOMAINS"
            )
        if final_host != host:
            redirected_decision = await self.robots.check(
                client, final_url, self.cfg.user_agent
            )
            if not redirected_decision.allowed:
                raise ComplianceError(
                    f"redirect target robots.txt: {redirected_decision.reason}"
                )
            await self.limiter.wait(final_host)
        if not content_type or "html" not in content_type.lower():
            raise SourceError(f"unsupported content type: {content_type or 'unknown'}")
        page = parse_page(body, url=final_url)
        if self._looks_like_login(final_url, page):
            raise ComplianceError(
                "the server returned a login/access page; access control will not be bypassed"
            )
        companies = extract_companies(page, source_name=source_name or host)
        if not companies:
            raise SourceError("page fetched but no company records were found")
        for company in companies:
            if not company.source_url:
                company.source_url = final_url
            company.source_name = source_name or company.source_name or host
        return companies

    @staticmethod
    def _looks_like_login(url: str, page: Any) -> bool:
        path = urlsplit(url).path.lower()
        if any(token in path for token in ("/login", "/signin", "/sign-in", "/auth/", "/register")):
            return True
        text = (getattr(page, "text", "") or "")[:4000].lower()
        if any(
            token in text
            for token in (
                "captcha",
                "verify you are human",
                "checking your browser",
                "sign in to continue",
                "log in to continue",
                "you must be signed in",
                "authentication required",
            )
        ):
            return True
        return False

    async def _request_with_retries(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str, str, str]:
        last: SourceError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                return await self._request_once(client, url)
            except ComplianceError:
                raise
            except SourceError as exc:
                last = exc
                if not exc.retryable or attempt >= self.cfg.max_retries:
                    raise
                delay = min(30.0, 2.0 ** (attempt + 1))
                safe_url = _safe_display_url(url)
                log.info("retrying %s in %.1fs after: %s", safe_url, delay, exc)
                await asyncio.sleep(delay)
        raise last or SourceError("request failed")

    async def _request_once(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str, str, str]:
        current = url
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                async with client.stream(
                    "GET", current, follow_redirects=False
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = str(response.headers.get("location") or "").strip()
                        if not location:
                            raise SourceError("redirect response had no Location header")
                        target = normalize_url(urljoin(current, location))
                        if not target:
                            raise SourceError("redirect target was not a valid http(s) URL")
                        target_host = _host(target)
                        if is_wellfound_host(target_host):
                            raise ComplianceError(
                                "the URL redirected to Wellfound; automated harvesting "
                                "is not permitted"
                            )
                        if not self.cfg.domain_allowed(target_host):
                            raise ComplianceError(
                                f"the URL redirected to {target_host}, which is not in "
                                "STARTUP_ALLOWED_DOMAINS"
                            )
                        if target_host != _host(current):
                            # Check the redirect target *before* requesting its
                            # page, so a redirect cannot bypass robots policy.
                            await self.limiter.wait(target_host)
                            decision = await self.robots.check(
                                client, target, self.cfg.user_agent
                            )
                            if not decision.allowed:
                                raise ComplianceError(
                                    f"redirect target robots.txt: {decision.reason}"
                                )
                        current = target
                        continue
                    if response.status_code in {401, 403}:
                        raise ComplianceError(
                            f"HTTP {response.status_code} access denied; stopping this source"
                        )
                    if response.status_code == 429:
                        raise ComplianceError(
                            "HTTP 429 rate limit; stopping this source instead of retrying around it"
                        )
                    if response.status_code >= 500:
                        raise SourceError(
                            f"HTTP {response.status_code}", retryable=True
                        )
                    if response.status_code == 404:
                        raise SourceError("HTTP 404 page not found")
                    if response.status_code >= 400:
                        raise SourceError(f"HTTP {response.status_code}")
                    content_type = response.headers.get("content-type", "")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.cfg.max_page_bytes:
                            raise SourceError(
                                f"page is larger than {self.cfg.max_page_bytes // 1024} KB"
                            )
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    encoding = response.encoding or "utf-8"
                    try:
                        text = raw.decode(encoding, errors="replace")
                    except (LookupError, UnicodeError):
                        text = raw.decode("utf-8", errors="replace")
                    return str(response.url), content_type, text
            raise SourceError("too many redirects")
        except ComplianceError:
            raise
        except SourceError:
            raise
        except httpx.TimeoutException as exc:
            raise SourceError(f"request timed out ({type(exc).__name__})", retryable=True) from None
        except httpx.HTTPError as exc:
            raise SourceError(f"network error ({type(exc).__name__})", retryable=True) from None
