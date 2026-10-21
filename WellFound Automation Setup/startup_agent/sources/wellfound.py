"""Local parsing for pages a user saved manually from Wellfound.

There is deliberately **no network code here**.  Wellfound's terms restrict
automated harvesting, so the agent does not crawl the site.  A user can save a
page they are already allowed to view (for example with Ctrl+S) and import that
local HTML file; this module extracts the company fields from it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..models import Company
from ..normalize import (
    canonical_wellfound_url,
    clean_name,
    normalize_url,
    parse_employee_size,
    source_slug,
)
from .extract import (
    SOCIAL_AND_DIRECTORY_HOSTS,
    PageData,
    _find_website_link,
    extract_companies,
    extract_fallback_company,
    parse_page,
)
from .htmlparse import Node

_CHALLENGE_MARKERS = (
    "just a moment",
    "verify you are human",
    "checking your browser",
    "enable javascript and cookies to continue",
    "cf-challenge",
    "captcha",
)
_LOGIN_MARKERS = (
    "sign in to wellfound",
    "log in to wellfound",
    "join wellfound",
)


class SavedPageError(RuntimeError):
    """The saved file is a login/challenge page, not a company page."""


def _context_for_anchor(anchor: Node) -> Node:
    """Smallest ancestor that contains only this company link."""
    fallback = anchor.parent or anchor
    for ancestor in anchor.ancestors(limit=8):
        links = ancestor.find_all("a", attr="href", contains="/company/", limit=5)
        if len(links) == 1:
            fallback = ancestor
        else:
            break
    return fallback


def _website_in_node(node: Node, page_host: str) -> str:
    preferred: list[str] = []
    fallback: list[str] = []
    for link in node.find_all("a", attr="href", limit=200):
        href = normalize_url(link.attr("href"))
        if not href:
            continue
        host = (urlsplit(href).hostname or "").lower().rstrip(".")
        if not host or host == page_host:
            continue
        if host in SOCIAL_AND_DIRECTORY_HOSTS or any(
            host.endswith("." + blocked) for blocked in SOCIAL_AND_DIRECTORY_HOSTS
        ):
            continue
        text = link.text(limit=200).lower()
        if any(word in text for word in ("website", "homepage", "visit site", "visit website")):
            preferred.append(href)
        else:
            fallback.append(href)
    return (preferred or fallback or [""])[0]


def _name_from_anchor(anchor: Node) -> str:
    text = clean_name(anchor.text(limit=300))
    if text:
        return text
    image = anchor.find("img")
    if image:
        alt = clean_name(image.attr("alt"))
        if alt:
            return alt
    return ""


def _directory_companies(page: PageData, saved_name: str) -> list[Company]:
    companies: list[Company] = []
    seen: set[str] = set()
    page_host = page.host
    for anchor in page.anchors:
        profile = canonical_wellfound_url(anchor.href)
        if not profile or profile in seen:
            continue
        name = _name_from_anchor(anchor.node)
        if not name:
            continue
        context = _context_for_anchor(anchor.node)
        text = context.text(limit=5000)
        size = parse_employee_size(text, require_context=True)
        website = _website_in_node(context, page_host)
        job_urls = [
            link.href
            for link in page.anchors
            if link.node is not None
            and link.href
            and link.href.startswith(profile.rstrip("/") + "/")
            and "/jobs" in urlsplit(link.href).path.lower()
        ]
        seen.add(profile)
        companies.append(
            Company(
                record_id="",
                name=name,
                website=website,
                employee_count=size.label if size.known else "",
                employee_min=size.minimum,
                employee_max=size.maximum,
                industry="",
                description="",
                wellfound_url=profile,
                job_urls=job_urls[:25],
                location="",
                source_url=page.canonical or f"saved://{saved_name}",
                source_name="wellfound_saved",
                source_id=source_slug(profile),
                raw={"saved_page": saved_name, "card_text": text[:2000]},
            )
        )
    return companies


def parse_wellfound_saved(markup: str, filename: str = "") -> list[Company]:
    """Parse a locally saved Wellfound page.

    Supports a saved company profile and a saved directory/list page.  It never
    fetches, clicks, or opens a browser.
    """
    page = parse_page(markup, url="")
    lowered = (page.text or "").lower()
    canonical_profile = canonical_wellfound_url(page.canonical)
    anchor_profiles: list[str] = []
    seen_profiles: set[str] = set()
    for anchor in page.anchors:
        candidate = canonical_wellfound_url(anchor.href)
        if candidate and candidate not in seen_profiles:
            seen_profiles.add(candidate)
            anchor_profiles.append(candidate)

    if canonical_profile:
        profile = canonical_profile
        is_directory_page = False
    elif len(anchor_profiles) >= 2:
        profile = ""
        is_directory_page = True
    elif anchor_profiles:
        profile = anchor_profiles[0]
        is_directory_page = False
    else:
        profile = ""
        is_directory_page = False

    if is_directory_page:
        companies = _directory_companies(page, filename or "saved.html")
        if companies:
            return companies

    if not profile and not anchor_profiles:
        if any(marker in lowered for marker in _CHALLENGE_MARKERS + _LOGIN_MARKERS):
            raise SavedPageError(
                "The saved page looks like a login or CAPTCHA/challenge page. "
                "Open Wellfound manually in your own browser, complete the normal "
                "sign-in yourself, then save the company page and import it again."
            )

    if profile:
        page.url = profile
        companies = extract_companies(page, source_name="wellfound_saved")
        if not companies:
            fallback = extract_fallback_company(page, source_name="wellfound_saved")
            if fallback:
                companies = [fallback]
        for company in companies:
            company.wellfound_url = profile
            company.source_url = profile
            company.source_name = "wellfound_saved"
            company.source_id = source_slug(profile)
            if not company.website:
                company.website = _find_website_link(page)
            if not company.location and page.address:
                company.location = page.address
            if not company.description:
                company.description = page.description
        return companies

    companies = _directory_companies(page, filename or "saved.html")
    if companies:
        return companies

    if any(marker in lowered for marker in _CHALLENGE_MARKERS + _LOGIN_MARKERS):
        raise SavedPageError(
            "No company records were found and the saved page looks like a login "
            "or challenge page. Save the page after signing in normally in your own browser."
        )
    return []
