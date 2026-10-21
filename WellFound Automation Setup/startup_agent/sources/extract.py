"""Extraction of company fields from a parsed HTML page.

This module never makes a network request.  It is used by the URL adapter (which
performs the request after robots/terms checks) and by the local file importer
(for saved pages and browser-assisted exports).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit

from ..models import Company, SizeInfo
from ..normalize import (
    canonical_wellfound_url,
    clean_name,
    clean_text,
    name_key,
    normalize_url,
    parse_employee_size,
    source_slug,
    website_key,
)
from .htmlparse import Node, parse_html_tree

SOCIAL_AND_DIRECTORY_HOSTS = {
    "linkedin.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "tiktok.com",
    "github.com",
    "medium.com",
    "crunchbase.com",
    "angel.co",
    "wellfound.com",
    "producthunt.com",
    "ycombinator.com",
    "f6s.com",
    "glassdoor.com",
    "indeed.com",
    "naukri.com",
    "internshala.com",
    "yourstory.com",
    "techcrunch.com",
}

_JOB_PATH_RE = re.compile(
    r"/(?:jobs?|careers?|positions?|openings?|opportunities)(?:/|$)", re.IGNORECASE
)
_JOB_TEXT_RE = re.compile(r"\b(?:jobs?|careers?|open roles|openings)\b", re.IGNORECASE)
_ORG_TYPES = {"organization", "corporation", "company", "localbusiness", "startup"}


@dataclass(slots=True)
class Anchor:
    href: str
    text: str
    node: Node


@dataclass(slots=True)
class PageData:
    url: str = ""
    title: str = ""
    h1: str = ""
    site_name: str = ""
    description: str = ""
    canonical: str = ""
    address: str = ""
    meta: dict[str, str] = field(default_factory=dict)
    anchors: list[Anchor] = field(default_factory=list)
    jsonld: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    root: Node | None = None

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower().rstrip(".")

    @property
    def is_directory(self) -> bool:
        return is_directory_host(self.host)


def is_directory_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host in SOCIAL_AND_DIRECTORY_HOSTS or any(
        host.endswith("." + item) for item in SOCIAL_AND_DIRECTORY_HOSTS
    )


def origin(url: str) -> str:
    normalized = normalize_url(url)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    return f"{parts.scheme}://{parts.netloc}"


def parse_page(markup: str, url: str = "") -> PageData:
    root = parse_html_tree(markup)
    page = PageData(url=normalize_url(url), root=root)
    title_node = root.find("title")
    page.title = title_node.text(limit=500) if title_node else ""
    h1_node = root.find("h1")
    page.h1 = h1_node.text(limit=500) if h1_node else ""
    address_node = root.find("address")
    page.address = address_node.text(limit=500) if address_node else ""

    for node in root.find_all("meta"):
        key = (node.attr("property") or node.attr("name") or "").strip().lower()
        content = clean_text(node.attr("content"), 2000)
        if key and content and key not in page.meta:
            page.meta[key] = content

    for node in root.find_all("link"):
        rel = node.attr("rel").lower()
        if "canonical" in rel.split():
            page.canonical = normalize_url(node.attr("href"), base=page.url)
            if page.canonical:
                break

    page.site_name = clean_text(
        page.meta.get("og:site_name")
        or page.meta.get("application-name")
        or page.meta.get("apple-mobile-web-app-title"),
        200,
    )
    page.description = clean_text(
        page.meta.get("og:description") or page.meta.get("description"), 4000
    )

    for node in root.find_all("a", attr="href", limit=5000):
        raw_href = clean_text(node.attr("href"), 2000)
        if not raw_href or raw_href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        href = normalize_url(raw_href, base=page.url)
        if not href:
            continue
        text = node.text(limit=500)
        page.anchors.append(Anchor(href=href, text=text, node=node))

    for node in root.find_all("script", attr="type", contains="ld+json", limit=30):
        text = node.text(skip_tags={"style", "noscript"}, limit=200_000).strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            # Some saved pages contain a JSON-LD script with a trailing comma.
            try:
                value = json.loads(text.rstrip().rstrip(","))
            except (TypeError, ValueError):
                continue
        page.jsonld.extend(_flatten_jsonld(value))

    page.text = root.text(limit=80_000)
    return page


def _flatten_jsonld(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            result.extend(_flatten_jsonld(item))
        return result
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("@graph"), list):
        result = _flatten_jsonld(value["@graph"])
        others = {key: item for key, item in value.items() if key != "@graph"}
        if others.get("@type"):
            result.insert(0, others)
        return result
    return [value]


def _types(obj: dict[str, Any]) -> set[str]:
    value = obj.get("@type") or obj.get("type") or ""
    if isinstance(value, list):
        return {str(item).lower() for item in value}
    return {str(value).lower()} if value else set()


def _first_text(value: Any, limit: int = 2000) -> str:
    if isinstance(value, list):
        for item in value:
            text = _first_text(item, limit)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("name", "value", "text", "@value"):
            if key in value:
                text = _first_text(value[key], limit)
                if text:
                    return text
        return ""
    return clean_text(value, limit)


def _url_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [normalize_url(value)] if normalize_url(value) else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_url_values(item))
        return result
    if isinstance(value, dict):
        for key in ("url", "sameAs", "@id"):
            if key in value:
                result = _url_values(value[key])
                if result:
                    return result
    return []


def _official_website(obj: dict[str, Any], page: PageData) -> str:
    candidates: list[str] = []
    for key in ("url", "sameAs", "mainEntityOfPage", "subjectOf"):
        candidates.extend(_url_values(obj.get(key)))
    candidates.extend(_url_values(obj.get("potentialAction")))
    for candidate in candidates:
        host = (urlsplit(candidate).hostname or "").lower().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        if not host:
            continue
        if host in SOCIAL_AND_DIRECTORY_HOSTS or any(
            host.endswith("." + item) for item in SOCIAL_AND_DIRECTORY_HOSTS
        ):
            continue
        if page.is_directory and host == page.host:
            continue
        return candidate
    # A company's own page can use its canonical URL as the website.
    if not page.is_directory:
        return page.canonical or page.url
    return ""


def _wellfound_from_values(values: Iterable[str]) -> str:
    for value in values:
        profile = canonical_wellfound_url(value)
        if profile:
            return profile
    return ""


def _address_text(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value, 500)
    if isinstance(value, list):
        parts = [_address_text(item) for item in value]
        return clean_text(", ".join(part for part in parts if part), 500)
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"):
        item = value.get(key)
        if isinstance(item, dict):
            item = item.get("name") or item.get("@id") or ""
        text = clean_text(item, 200)
        if text and text not in parts:
            parts.append(text)
    return clean_text(", ".join(parts), 500)


def _size_from_jsonld(value: Any) -> SizeInfo:
    if value is None:
        return SizeInfo()
    if isinstance(value, list):
        for item in value:
            size = _size_from_jsonld(item)
            if size.known:
                return size
        return SizeInfo(raw=clean_text(value, 200), label="Unknown")
    if isinstance(value, dict):
        minimum = value.get("minValue", value.get("minimum"))
        maximum = value.get("maxValue", value.get("maximum"))
        nested = value.get("value")
        if minimum is not None or maximum is not None:
            try:
                low = int(str(minimum).replace(",", "")) if minimum is not None else None
                high = int(str(maximum).replace(",", "")) if maximum is not None else None
            except ValueError:
                low = high = None
            if low is not None or high is not None:
                if high is None:
                    label = f"{low}+ employees"
                elif low is None:
                    label = f"up to {high} employees"
                elif low == high:
                    label = f"{low} employee" if low == 1 else f"{low} employees"
                else:
                    label = f"{low}-{high} employees"
                return SizeInfo(
                    raw=clean_text(value, 200),
                    minimum=low,
                    maximum=high,
                    label=label,
                )
        if nested is not None:
            return _size_from_jsonld(nested)
        return SizeInfo(raw=clean_text(value, 200), label="Unknown")
    return parse_employee_size(value)


def _industry(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(text for text in (_first_text(item, 200) for item in value) if text)
    return _first_text(value, 300)


def _job_urls_from_jsonld(obj: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key in ("subjectOf", "hasPart", "itemListElement"):
        value = obj.get(key)
        if not value:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                item_type = _types(item)
                if item_type and not any("jobposting" in kind for kind in item_type):
                    continue
                for url in _url_values(item.get("url") or item.get("@id")):
                    if _JOB_PATH_RE.search(urlsplit(url).path):
                        result.append(url)
    return result


def _company_from_jsonld(obj: dict[str, Any], page: PageData) -> Company | None:
    name = clean_name(obj.get("name") or obj.get("legalName") or obj.get("alternateName"))
    if not name:
        return None
    page_host = page.host
    website = _official_website(obj, page)
    profile = _wellfound_from_values(
        [
            *_url_values(obj.get("sameAs")),
            *_url_values(obj.get("url")),
            page.canonical,
            page.url,
        ]
    )
    size = _size_from_jsonld(
        obj.get("numberOfEmployees") or obj.get("employeeCount") or obj.get("size")
    )
    description = _first_text(obj.get("description"), 4000)
    industry = _industry(obj.get("industry") or obj.get("category") or obj.get("sector"))
    location = _address_text(obj.get("address") or obj.get("location"))
    job_urls = _job_urls_from_jsonld(obj)
    return Company(
        record_id="",
        name=name,
        website=website,
        employee_count=size.label if size.known else clean_text(
            obj.get("numberOfEmployees") or obj.get("employeeCount"), 200
        ),
        employee_min=size.minimum,
        employee_max=size.maximum,
        industry=industry,
        description=description,
        wellfound_url=profile,
        job_urls=job_urls,
        location=location,
        source_url=page.canonical or page.url,
        source_name=page_host,
        source_id=source_slug(profile) if profile else "",
        raw={"jsonld": obj, "page": page.url},
    )


def _directory_self_company(obj: dict[str, Any], page: PageData) -> bool:
    if not page.is_directory:
        return False
    name = clean_name(obj.get("name"))
    if not name:
        return False
    site = page.site_name or page.h1
    return bool(site and name_key(name) == name_key(site))


def extract_jsonld_companies(page: PageData) -> list[Company]:
    companies: list[Company] = []
    for obj in page.jsonld:
        if not (_types(obj) & _ORG_TYPES):
            continue
        if _directory_self_company(obj, page):
            continue
        company = _company_from_jsonld(obj, page)
        if company:
            companies.append(company)
    return companies


def _find_website_link(page: PageData) -> str:
    preferred: list[str] = []
    fallback: list[str] = []
    for anchor in page.anchors:
        host = (urlsplit(anchor.href).hostname or "").lower().rstrip(".")
        if not host or host == page.host:
            continue
        if host in SOCIAL_AND_DIRECTORY_HOSTS or any(
            host.endswith("." + item) for item in SOCIAL_AND_DIRECTORY_HOSTS
        ):
            continue
        text = anchor.text.lower()
        if any(word in text for word in ("website", "homepage", "visit site", "visit website", "company site")):
            preferred.append(anchor.href)
        elif not _JOB_PATH_RE.search(urlsplit(anchor.href).path):
            fallback.append(anchor.href)
    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]
    if not page.is_directory:
        return page.canonical or origin(page.url)
    return ""


def _extract_job_links(page: PageData) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for anchor in page.anchors:
        path = urlsplit(anchor.href).path
        if _JOB_PATH_RE.search(path) or _JOB_TEXT_RE.search(anchor.text):
            if any(bad in path.lower() for bad in ("/applications", "/signup", "/login")):
                continue
            if anchor.href not in seen:
                seen.add(anchor.href)
                result.append(anchor.href)
        if len(result) >= 25:
            break
    return result


def _title_name(page: PageData) -> str:
    if page.h1 and name_key(page.h1) not in {"home", "welcome", "about", "about us"}:
        return clean_name(page.h1)
    if page.site_name:
        return clean_name(page.site_name)
    title = re.split(r"\s+[|–—]\s+", page.title, maxsplit=1)[0]
    return clean_name(title or page.title)


def extract_fallback_company(page: PageData, *, source_name: str = "") -> Company | None:
    name = _title_name(page)
    if not name:
        return None
    website = _find_website_link(page)
    profile = _wellfound_from_values(
        [
            *[anchor.href for anchor in page.anchors],
            page.canonical,
            page.url,
        ]
    )
    size = parse_employee_size(page.text, require_context=True)
    location = page.address or _first_text(page.meta.get("og:locality"), 300)
    if not location:
        location = _first_text(page.meta.get("business:contact_data:locality"), 300)
    industry = _first_text(
        page.meta.get("industry")
        or page.meta.get("sector")
        or page.meta.get("category")
        or page.meta.get("article:section"),
        300,
    )
    job_urls = _extract_job_links(page)
    return Company(
        record_id="",
        name=name,
        website=website,
        employee_count=size.label if size.known else "",
        employee_min=size.minimum,
        employee_max=size.maximum,
        industry=industry,
        description=page.description,
        wellfound_url=profile,
        job_urls=job_urls,
        location=location,
        source_url=page.canonical or page.url,
        source_name=source_name or page.host,
        source_id=source_slug(profile) if profile else "",
        raw={
            "title": page.title,
            "h1": page.h1,
            "meta": {
                key: page.meta[key]
                for key in (
                    "og:title",
                    "og:description",
                    "og:url",
                    "description",
                    "industry",
                    "sector",
                )
                if key in page.meta
            },
        },
    )


def _dedupe(companies: Iterable[Company]) -> list[Company]:
    result: list[Company] = []
    seen: set[str] = set()
    for company in companies:
        key = (
            canonical_wellfound_url(company.wellfound_url)
            or website_key(company.website)
            or f"{name_key(company.name)}|{clean_text(company.location, 100).lower()}"
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(company)
    return result


def extract_companies(page: PageData, *, source_name: str = "") -> list[Company]:
    """Extract one or more companies from metadata/JSON-LD or page text."""
    companies = extract_jsonld_companies(page)
    if not companies:
        fallback = extract_fallback_company(page, source_name=source_name)
        if fallback:
            companies = [fallback]
    for company in companies:
        company.source_name = source_name or company.source_name or page.host
    return _dedupe(companies)
