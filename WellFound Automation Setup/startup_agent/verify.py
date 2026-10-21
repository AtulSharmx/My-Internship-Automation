"""Independent website verification.

The Wellfound profile tells us which site a company publishes.  That is only
a claim: the domain may be dead, parked, or redirect somewhere unrelated.  This
module performs one polite HTTP GET per company website and records what it
actually observed.

Safety rules:
    * only http(s), never credentials in the URL;
    * localhost / private / link-local / reserved addresses are refused
      (prevents the bot being used to probe the local network);
    * at most five redirects, a hard timeout, and a small body sample;
    * no crawling: exactly one request to the published site.

A site is ``verified`` only when it answers 2xx with HTML and stays on the
published domain (or a subdomain of it).  A redirect to a different domain is
recorded as ``redirected`` and stays out of verified exports.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .models import WebsiteStatus
from .normalize import normalize_url
from .sources.extract import is_directory_host

log = logging.getLogger("startup_agent.verify")

_MAX_BODY = 262_144  # 256 KB is enough to prove it is a real HTML page
_MAX_REDIRECTS = 5
_HONEST_UA = (
    "Mozilla/5.0 (compatible; StartupDiscoveryAgent/1.0; "
    "+personal-research; website-verification)"
)


@dataclass(slots=True)
class WebsiteCheck:
    status: str
    final_url: str = ""
    reason: str = ""
    http_status: int = 0

    @property
    def ok(self) -> bool:
        return self.status == WebsiteStatus.VERIFIED

    @property
    def evidence(self) -> str:
        if self.status == WebsiteStatus.VERIFIED:
            return f"HTTP {self.http_status or 200} at {self.final_url}"
        if self.reason:
            return self.reason
        return WebsiteStatus.LABELS.get(self.status, self.status)


def _host_only(value: str) -> str:
    text = (value or "").strip().lower().rstrip(".")
    return text[4:] if text.startswith("www.") else text


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _same_site(published: str, final: str) -> bool:
    published = _host_only(published)
    final = _host_only(final)
    if not published or not final:
        return False
    return (
        final == published
        or final.endswith("." + published)
        or published.endswith("." + final)
    )


class WebsiteVerifier:
    """One-GET website checker with an injectable transport for tests."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        check_dns: bool = True,
        user_agent: str = _HONEST_UA,
    ) -> None:
        self.timeout = timeout
        self.transport = transport
        self.check_dns = check_dns
        self.user_agent = user_agent

    async def _resolves_public(self, host: str) -> tuple[bool, str]:
        # A literal IP address is always checked, even when DNS lookups are
        # disabled for the tests.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if _is_public_ip(host):
                return True, ""
            return False, "private address"
        if not self.check_dns:
            return True, ""
        try:
            loop = asyncio.get_running_loop()
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM), timeout=8.0
            )
        except (OSError, asyncio.TimeoutError):
            return False, "domain does not resolve"
        addresses = {info[4][0] for info in infos if info[4]}
        if not addresses:
            return False, "domain does not resolve"
        if any(not _is_public_ip(address) for address in addresses):
            return False, "private address"
        return True, ""

    async def verify(self, url: str, *, company_name: str = "") -> WebsiteCheck:
        target = normalize_url(url)
        if not target:
            return WebsiteCheck(WebsiteStatus.MISSING, reason="no website listed")

        parts = urlsplit(target)
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return WebsiteCheck(WebsiteStatus.MISSING, reason="invalid website URL")
        if is_directory_host(host):
            return WebsiteCheck(
                WebsiteStatus.BLOCKED,
                reason="social/directory profile, not an official company website",
            )
        public, why = await self._resolves_public(host)
        if not public:
            return WebsiteCheck(
                WebsiteStatus.UNREACHABLE, reason=f"not checked: {why}"
            )

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Language": "en-US,en;q=0.8",
        }
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
                follow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                headers=headers,
            ) as client:
                async with client.stream("GET", target) as response:
                    final_url = normalize_url(str(response.url)) or target
                    content_type = (response.headers.get("content-type") or "").lower()
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= _MAX_BODY:
                            break
                    body = b"".join(chunks)[:_MAX_BODY]
                    status_code = response.status_code
        except httpx.TooManyRedirects:
            return WebsiteCheck(
                WebsiteStatus.REDIRECTED, reason="too many redirects"
            )
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return WebsiteCheck(
                WebsiteStatus.UNREACHABLE, reason=f"timed out after {self.timeout:.0f}s"
            )
        except httpx.HTTPError as exc:
            return WebsiteCheck(
                WebsiteStatus.UNREACHABLE,
                reason=f"connection failed ({type(exc).__name__})",
            )
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop a scan
            log.debug("website check failed for %s: %s", host, type(exc).__name__)
            return WebsiteCheck(
                WebsiteStatus.UNREACHABLE,
                reason=f"check failed ({type(exc).__name__})",
            )

        if status_code >= 400:
            return WebsiteCheck(
                WebsiteStatus.UNREACHABLE,
                final_url=final_url,
                reason=f"HTTP {status_code}",
                http_status=status_code,
            )
        looks_html = "html" in content_type or body[:512].lstrip().lower().startswith(
            (b"<!doctype html", b"<html")
        )
        if not looks_html:
            return WebsiteCheck(
                WebsiteStatus.UNREACHABLE,
                final_url=final_url,
                reason="response is not an HTML page",
                http_status=status_code,
            )

        final_host = (urlsplit(final_url).hostname or "").lower().rstrip(".")
        if not _same_site(host, final_host):
            return WebsiteCheck(
                WebsiteStatus.REDIRECTED,
                final_url=final_url,
                reason=f"redirects to a different domain ({_host_only(final_host)})",
                http_status=status_code,
            )
        if _host_only(host) != _host_only(final_host):
            return WebsiteCheck(
                WebsiteStatus.VERIFIED,
                final_url=final_url,
                reason=f"HTTP {status_code}; subdomain of {_host_only(host)}",
                http_status=status_code,
            )
        return WebsiteCheck(
            WebsiteStatus.VERIFIED,
            final_url=final_url,
            reason=f"HTTP {status_code}",
            http_status=status_code,
        )

    async def close(self) -> None:
        """Kept for symmetry; the client is short-lived per check."""


async def verify_many(
    verifier: WebsiteVerifier,
    items: list[tuple[str, str]],
    *,
    concurrency: int = 1,
) -> list[WebsiteCheck]:
    """Verify a small batch, sequentially by default (polite and simple)."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(url: str, name: str) -> WebsiteCheck:
        async with semaphore:
            return await verifier.verify(url, company_name=name)

    return await asyncio.gather(*(one(url, name) for url, name in items))
