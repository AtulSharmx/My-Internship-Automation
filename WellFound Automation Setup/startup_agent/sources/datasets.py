"""Permitted public dataset sources (structured, documented, no scraping).

These adapters read **open datasets through their documented public query
interfaces**.  They are not crawlers: every request is a single, explicit
query against a public SPARQL endpoint whose ``robots.txt`` allows the path.

Rules enforced here (identical to the URL adapter):

* the host must be in ``STARTUP_ALLOWED_DOMAINS``;
* ``robots.txt`` is fetched and obeyed before the first query;
* an honest user agent, a per-host rate limit, timeouts and a response size
  cap are always applied;
* ``401``/``403``/``429`` are treated as access denial and stop the run
  instead of being retried around;
* Wellfound / AngelList are permanently blocked;
* values are copied as published - a missing field stays blank, it is never
  guessed, and the source of every record is kept for attribution.

DBpedia (https://dbpedia.org) is a public dataset released under CC BY-SA 4.0
/ CC0.  ``dbo:numberOfEmployees`` is derived from English Wikipedia company
infoboxes, so a headcount is a *published* value that may be dated; the record
notes say so.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import Config
from ..models import Company
from ..normalize import clean_text, name_key, normalize_url
from .web import (
    ComplianceError,
    HostRateLimiter,
    RobotsCache,
    SourceError,
    is_wellfound_host,
)

log = logging.getLogger("startup_agent.datasets")

_DBPEDIA_ENDPOINT = "https://dbpedia.org/sparql"
_ARCHIVE_HOSTS = {"web.archive.org", "archive.org"}
_JUNK_SITE_MARKERS = ("example.com", "example.org", "example.net")


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _resource_label(value: str) -> str:
    """Turn a DBpedia resource URI into a human label."""
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        slug = value.rstrip("/").split("/")[-1]
    else:
        slug = value
    slug = slug.split("?")[0]
    text = re.sub(r"[_\-]+", " ", slug)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _clean_website(value: str) -> str:
    """DBpedia stores pipe-separated homepage variants; keep a usable URL."""
    if not value:
        return ""
    for chunk in str(value).split("|"):
        candidate = chunk.strip()
        if not candidate.lower().startswith(("http://", "https://")):
            continue
        url = normalize_url(candidate)
        if not url:
            continue
        host = _host(url)
        if not host or host in _ARCHIVE_HOSTS:
            continue
        if any(marker in host for marker in _JUNK_SITE_MARKERS):
            continue
        return url
    return ""


@dataclass(slots=True)
class DatasetCompany:
    name: str
    employees: int | None = None
    website: str = ""
    industry: str = ""
    location: str = ""
    description: str = ""
    source_id: str = ""
    source_url: str = ""
    notes: str = ""


class DatasetSource:
    """Base class for permitted structured dataset adapters."""

    key = "dataset"
    label = "Dataset"
    endpoint = ""
    documentation = ""

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
                    "Accept": "application/sparql-results+json,application/json;q=0.9",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -- helpers -------------------------------------------------------
    async def _check_policy(self, url: str) -> None:
        """Allow-list + robots.txt gate, identical to the URL adapter."""
        host = _host(url)
        if is_wellfound_host(host):
            raise ComplianceError(
                "Wellfound/AngelList cannot be queried automatically (terms of use)"
            )
        if not self.cfg.domain_allowed(host):
            raise ComplianceError(
                f"{host} is not in STARTUP_ALLOWED_DOMAINS; add it explicitly to use this source"
            )
        decision = await self.robots.check(self._http(), url, self.cfg.user_agent)
        if not decision.allowed:
            raise ComplianceError(f"robots.txt: {decision.reason}")

    async def _query_json(self, query: str, *, limit_bytes: int = 8_000_000) -> dict[str, Any]:
        """Run one SPARQL query and return the parsed JSON result."""
        url = self.endpoint
        await self._check_policy(url)
        client = self._http()
        attempts = max(1, self.cfg.max_retries + 1)
        last: SourceError | None = None
        for attempt in range(attempts):
            await self.limiter.wait(_host(url))
            try:
                response = await client.get(
                    url,
                    params={"query": query, "format": "application/sparql-results+json"},
                )
            except httpx.TimeoutException as exc:
                last = SourceError(f"query timed out ({type(exc).__name__})", retryable=True)
            except httpx.HTTPError as exc:
                last = SourceError(f"network error ({type(exc).__name__})", retryable=True)
            else:
                if response.status_code in {401, 403}:
                    raise ComplianceError(
                        f"HTTP {response.status_code} access denied; stopping this source"
                    )
                if response.status_code == 429:
                    raise ComplianceError(
                        "HTTP 429 rate limit; stopping this source instead of retrying around it"
                    )
                if response.status_code >= 500:
                    last = SourceError(
                        f"HTTP {response.status_code}", retryable=True
                    )
                elif response.status_code >= 400:
                    raise SourceError(f"HTTP {response.status_code}")
                else:
                    body = response.content
                    if len(body) > limit_bytes:
                        raise SourceError("result set is larger than the allowed size")
                    try:
                        payload = json.loads(body.decode("utf-8", errors="replace"))
                    except ValueError:
                        raise SourceError("the endpoint did not return valid JSON") from None
                    if not isinstance(payload, dict):
                        raise SourceError("the endpoint returned an unexpected payload")
                    return payload
            if attempt + 1 < attempts:
                await asyncio.sleep(1.5 * (attempt + 1))
        raise last or SourceError("query failed")

    @staticmethod
    def _bindings(payload: dict[str, Any]) -> list[dict[str, str]]:
        results = payload.get("results") or {}
        bindings = results.get("bindings") or []
        rows: list[dict[str, str]] = []
        for binding in bindings:
            row = {
                key: str(value.get("value", ""))
                for key, value in binding.items()
                if isinstance(value, dict)
            }
            if row:
                rows.append(row)
        return rows

    # -- public API ----------------------------------------------------
    async def fetch(self, limit: int) -> list[Company]:
        raise NotImplementedError


class DbpediaSource(DatasetSource):
    """DBpedia: open dataset with published company headcounts.

    The query selects companies whose published ``dbo:numberOfEmployees``
    falls inside the configured target range, so the result is already
    filtered by the same rule the agent applies to every other source.
    """

    key = "dbpedia"
    label = "DBpedia (open dataset)"
    endpoint = _DBPEDIA_ENDPOINT
    documentation = "https://www.dbpedia.org/"

    def _build_query(self, limit: int) -> str:
        low = self.cfg.target_size_min
        high = self.cfg.target_size_max
        return f"""
PREFIX dbo:  <http://dbpedia.org/ontology/>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX dct:  <http://purl.org/dc/terms/>
SELECT DISTINCT ?c ?name ?e ?site ?industry ?city ?description WHERE {{
  ?c dbo:numberOfEmployees ?e ;
     rdfs:label ?name ;
     foaf:homepage ?site .
  FILTER(?e >= {low} && ?e <= {high})
  FILTER(lang(?name) = "en")
  FILTER NOT EXISTS {{
    ?c dct:subject ?cat .
    FILTER(CONTAINS(LCASE(STR(?cat)), "school") || CONTAINS(LCASE(STR(?cat)), "universit")
        || CONTAINS(LCASE(STR(?cat)), "college") || CONTAINS(LCASE(STR(?cat)), "educational"))
  }}
  OPTIONAL {{ ?c dbo:industry ?industry }}
  OPTIONAL {{ ?c dbo:locationCity ?city }}
  OPTIONAL {{ ?c dbo:description ?description . FILTER(lang(?description) = "en") }}
}} LIMIT {max(1, int(limit))}
""".strip()

    async def fetch(self, limit: int) -> list[Company]:
        payload = await self._query_json(self._build_query(limit))
        rows = self._bindings(payload)

        # One company can appear once per industry/city; merge on the URI.
        merged: dict[str, DatasetCompany] = {}
        for row in rows:
            uri = row.get("c", "")
            if not uri:
                continue
            name = clean_text(row.get("name", ""), 200)
            if not name:
                continue
            record = merged.get(uri)
            if record is None:
                record = DatasetCompany(
                    name=name,
                    employees=int(row["e"]) if row.get("e", "").isdigit() else None,
                    website=_clean_website(row.get("site", "")),
                    industry=_resource_label(row.get("industry", "")),
                    location=_resource_label(row.get("city", "")),
                    description=clean_text(row.get("description", ""), 400),
                    source_id=uri,
                    source_url=f"https://dbpedia.org/page/{uri.rstrip('/').split('/')[-1]}",
                    notes=(
                        "Employee count published by DBpedia (derived from Wikipedia "
                        "infoboxes) and may be dated."
                    ),
                )
                merged[uri] = record
            else:
                if not record.industry and row.get("industry"):
                    record.industry = _resource_label(row["industry"])
                if not record.location and row.get("city"):
                    record.location = _resource_label(row["city"])
                if not record.description and row.get("description"):
                    record.description = clean_text(row["description"], 400)
                if not record.website and row.get("site"):
                    record.website = _clean_website(row["site"])

        companies: list[Company] = []
        for uri, record in merged.items():
            if not record.name:
                continue
            employee_count = ""
            if record.employees:
                employee_count = (
                    "1 employee"
                    if record.employees == 1
                    else f"{record.employees} employees"
                )
            company = Company(
                record_id="",
                name=record.name,
                website=record.website,
                employee_count=employee_count,
                industry=record.industry,
                description=record.description,
                location=record.location,
                source_name=self.key,
                source_url=record.source_url,
                source_id=record.source_id,
                notes=record.notes,
                raw={"dataset": self.key, "uri": uri, "employees": record.employees},
            )
            company.name_key = name_key(company.name)
            companies.append(company)
        log.info(
            "dbpedia returned %d company rows (%d unique)", len(rows), len(companies)
        )
        return companies


def build_dataset_sources(cfg: Config) -> list[DatasetSource]:
    """Instantiate the dataset adapters named in ``STARTUP_DATASET_SOURCES``."""
    sources: list[DatasetSource] = []
    for name in cfg.dataset_sources:
        key = name.strip().lower()
        if key == "dbpedia":
            sources.append(DbpediaSource(cfg))
        else:
            log.warning("unknown dataset source ignored: %s", name)
    return sources
