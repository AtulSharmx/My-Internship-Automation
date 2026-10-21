"""Normalization, size parsing and quality evaluation.

Keeping these helpers pure makes the import and URL-discovery adapters easy to
test without a database or a network.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .models import Company, CompanyStatus, SizeInfo, SizeStatus

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    pass


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
_WHITESPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_RE = re.compile(r"<[^>]+>")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def strip_html(value: object) -> str:
    """Turn an HTML snippet into plain, single-space text."""
    text = str(value or "")
    if "<" in text and ">" in text:
        parser = _TextExtractor()
        try:
            parser.feed(text)
            text = " ".join(parser.parts)
        except Exception:  # noqa: BLE001 - malformed HTML should not break import
            text = _TAG_RE.sub(" ", text)
    return clean_text(html.unescape(text), 20_000)


def clean_text(value: object, max_chars: int = 4000) -> str:
    """Collapse whitespace, remove control characters and cap the length."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False)
    text = str(value).replace("\u00a0", " ").replace("\u202f", " ")
    text = _CONTROL_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if max_chars and len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def clean_name(value: object) -> str:
    text = clean_text(value, 300)
    text = re.sub(r"^(?:company|startup|organization|organisation)\s*:\s*", "", text, flags=re.I)
    return text.strip(" -–—|·")


def truncate(value: str, limit: int = 3900) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def safe_filename(name: str, fallback: str = "file") -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(name or "")).strip(" ._")
    name = re.sub(r"\s+", "_", name)
    return (name or fallback)[:120]


# ---------------------------------------------------------------------------
# URLs and company identity
# ---------------------------------------------------------------------------
_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "ref_",
    "referrer",
    "source",
    "trk",
    "trkCampaign",
    "mkt_tok",
}
_TRACKING_PREFIXES = ("utm_", "hsa_", "vero_", "oly_")

_SOCIAL_AND_DIRECTORY_HOSTS = {
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


def is_http_url(value: object) -> bool:
    text = str(value or "").strip()
    if not re.match(r"^https?://", text, flags=re.I):
        return False
    try:
        parts = urlsplit(text)
    except ValueError:
        return False
    return bool(parts.netloc and parts.hostname)


def normalize_url(value: object, base: str = "") -> str:
    """Return a canonical http(s) URL, or ``""`` when it is not usable.

    Tracking parameters, fragments, default ports, duplicate slashes and a
    leading ``www.`` are removed.  A real path is kept because it can identify a
    company profile; ``https://acme.test/`` and ``https://acme.test`` are the
    same URL here.
    """
    if value is None:
        return ""
    text = clean_text(value, 3000)
    if not text or text.lower() in {"n/a", "na", "none", "unknown", "-", "—"}:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, flags=re.I):
        if base:
            text = urljoin(base, text)
        elif re.match(r"^[\w.-]+\.[a-z]{2,}(?:[/:?#]|$)", text, flags=re.I):
            text = "https://" + text
        else:
            return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        return ""
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "")
    if path != "/":
        path = path.rstrip("/")
    if path == "/":
        path = ""
    query_pairs = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower in _TRACKING_KEYS or lower.startswith(_TRACKING_PREFIXES):
            continue
        query_pairs.append((key, val))
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def website_key(value: object) -> str:
    """A conservative secondary dedup key: the official website hostname.

    Social profiles and directory profiles are intentionally excluded; they
    are not official company websites and would create false merges.
    """
    url = normalize_url(value)
    if not url:
        return ""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or host in _SOCIAL_AND_DIRECTORY_HOSTS:
        return ""
    if any(host.endswith("." + blocked) for blocked in _SOCIAL_AND_DIRECTORY_HOSTS):
        return ""
    return host


def canonical_wellfound_url(value: object) -> str:
    """Return ``https://wellfound.com/company/<slug>`` when applicable."""
    url = normalize_url(value)
    if not url:
        return ""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if host != "wellfound.com" and not host.endswith(".wellfound.com"):
        return ""
    match = re.match(r"^/company/([^/]+)(?:/.*)?$", parts.path, flags=re.I)
    if not match:
        return ""
    slug = match.group(1).strip()
    if not slug or slug.lower() in {"jobs", "profile"}:
        return ""
    return f"https://wellfound.com/company/{slug}"


def normalize_source_url(value: object) -> str:
    """Keep local ``saved://`` references for browser-assisted imports."""
    text = clean_text(value, 1000)
    if text.lower().startswith(("saved://", "file://")):
        return text
    return normalize_url(text)


def source_slug(value: object) -> str:
    """Extract a stable-looking slug from a profile/job/company URL."""
    url = normalize_url(value)
    if not url:
        return ""
    parts = urlsplit(url)
    bits = [bit for bit in parts.path.split("/") if bit]
    if not bits:
        return ""
    if "company" in bits:
        idx = bits.index("company")
        if idx + 1 < len(bits):
            return bits[idx + 1]
    return bits[-1]


_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:inc|llc|ltd|limited|pvt|private|corp|corporation|co|company"
    r"|technologies|technology|tech|solutions|systems|labs|lab|studios|studio"
    r"|ventures|venture|holdings|holding|group|gmbh|plc)\b\.?",
    re.IGNORECASE,
)


def name_key(value: object) -> str:
    """Normalized name used only for *possible* duplicate review.

    This key is never used on its own to merge records.  Two companies with the
    same name are kept separate unless a profile URL, source id, website or
    identical content proves they are the same.
    """
    raw = clean_name(value).lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", raw)
    text = _LEGAL_SUFFIX_RE.sub(" ", text)
    key = _WHITESPACE_RE.sub(" ", text).strip()
    return key or re.sub(r"[^a-z0-9]+", " ", raw).strip()


def normalized_location(value: object) -> str:
    text = clean_text(value, 300).lower()
    text = re.sub(r"[^a-z0-9, ]+", " ", text)
    return _WHITESPACE_RE.sub(" ", text).strip(" ,")


def content_fingerprint(company: Company) -> str:
    raw = "|".join(
        [
            company.source_name.strip().lower(),
            company.source_url.strip().lower(),
            name_key(company.name),
            website_key(company.website),
            normalized_location(company.location),
            clean_text(company.employee_count, 100).lower(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def identity_keys(company: Company) -> list[tuple[str, str]]:
    """Strong identity keys used for automatic upserts.

    A name alone is deliberately absent.  The content fingerprint at the end
    makes repeated imports of the same row idempotent even when the source had
    no website or profile URL.
    """
    keys: list[tuple[str, str]] = []
    if company.record_id:
        keys.append(("record", company.record_id))
    profile = canonical_wellfound_url(company.wellfound_url)
    if profile:
        keys.append(("profile", profile))
    if company.source_id and company.source_name:
        keys.append(("source", f"{company.source_name.strip().lower()}:{company.source_id.strip()}"))
    host = website_key(company.website)
    if host:
        keys.append(("website", host))
    keys.append(("content", content_fingerprint(company)))
    # Preserve order while removing duplicates.
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for item in keys:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def record_id_for(company: Company) -> str:
    """Stable internal id derived from the strongest available identity."""
    profile = canonical_wellfound_url(company.wellfound_url)
    host = website_key(company.website)
    if profile:
        seed = "profile:" + profile
    elif company.source_name and company.source_id:
        seed = f"source:{company.source_name.strip().lower()}:{company.source_id.strip()}"
    elif host:
        seed = f"website:{host}:{name_key(company.name)}"
    else:
        seed = "content:" + content_fingerprint(company)
    return "co_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Employee size
# ---------------------------------------------------------------------------
_RANGE_RE = re.compile(
    r"(?<![\d.])(\d[\d,]*)\s*(?:-|–|—|to)\s*(\d[\d,]*)(?:\s*(?:employees?|people|team members?|headcount))?",
    re.IGNORECASE,
)
_PLUS_RE = re.compile(r"(?<![\d.])(\d[\d,]*)\s*\+\s*(?:employees?|people|team members?)", re.IGNORECASE)
_SINGLE_RE = re.compile(
    r"(?<![\d.])(\d[\d,]*)\s*(?:employees?|people|team members?|headcount)", re.IGNORECASE
)
_FEWER_RE = re.compile(
    r"(?:fewer|less)\s+than\s+(\d[\d,]*)\s*(?:employees?|people)?", re.IGNORECASE
)
_OR_FEWER_RE = re.compile(
    r"(\d[\d,]*)\s*(?:or\s+fewer|or\s+less)\s*(?:employees?|people)?", re.IGNORECASE
)
_SELF_RE = re.compile(r"\bself[-\s]?employed\b", re.IGNORECASE)
_UNKNOWN_VALUES = {"", "unknown", "n/a", "na", "none", "not available", "not disclosed", "-", "—", "?"}
_CONTEXT_WORDS = ("employee", "people", "team", "headcount", "staff", "person")


def _number(value: str) -> int:
    return int(value.replace(",", ""))


def _context_near(text: str, start: int, end: int, window: int = 28) -> bool:
    nearby = text[max(0, start - window) : min(len(text), end + window)].lower()
    return any(word in nearby for word in _CONTEXT_WORDS)


def parse_employee_size(value: object, *, require_context: bool = False) -> SizeInfo:
    """Parse common employee-count formats without guessing.

    ``require_context`` is used when parsing arbitrary page text, so that a
    year range such as ``2019-2020`` is not mistaken for a head-count.
    """
    raw = clean_text(value, 200)
    if raw.lower() in _UNKNOWN_VALUES:
        return SizeInfo(raw=raw, label="Unknown")
    if _SELF_RE.search(raw):
        return SizeInfo(raw=raw, minimum=1, maximum=1, label="1 employee")

    match = _FEWER_RE.search(raw)
    if match and (not require_context or _context_near(raw, match.start(), match.end())):
        upper = max(1, _number(match.group(1)) - 1)
        return SizeInfo(raw=raw, minimum=1, maximum=upper, label=_size_label(1, upper))

    match = _OR_FEWER_RE.search(raw)
    if match and (not require_context or _context_near(raw, match.start(), match.end())):
        upper = max(1, _number(match.group(1)))
        return SizeInfo(raw=raw, minimum=1, maximum=upper, label=_size_label(1, upper))

    for match in _RANGE_RE.finditer(raw):
        if require_context and not _context_near(raw, match.start(), match.end()):
            continue
        low, high = _number(match.group(1)), _number(match.group(2))
        if low > high:
            low, high = high, low
        if (
            1900 <= low <= high <= datetime.now().year + 1
            and (
                require_context
                or re.search(r"\b(?:founded|established|since|year|est\.)\b", raw, re.I)
            )
        ):
            # Page text often contains a founding-year range ("Founded
            # 2019-2020"); that is not an employee count.
            continue
        return SizeInfo(raw=raw, minimum=low, maximum=high, label=_size_label(low, high))

    match = _PLUS_RE.search(raw)
    if match and (not require_context or _context_near(raw, match.start(), match.end())):
        low = _number(match.group(1))
        return SizeInfo(raw=raw, minimum=low, maximum=None, label=f"{low}+ employees")

    match = _SINGLE_RE.search(raw)
    if match and (not require_context or _context_near(raw, match.start(), match.end())):
        number = _number(match.group(1))
        return SizeInfo(raw=raw, minimum=number, maximum=number, label=_size_label(number, number))

    if not require_context:
        # A bare value in a dedicated CSV column is unambiguous.
        if re.fullmatch(r"\d[\d,]*", raw):
            number = _number(raw)
            return SizeInfo(raw=raw, minimum=number, maximum=number, label=_size_label(number, number))
        if re.fullmatch(r"\d[\d,]*\s*\+", raw):
            number = _number(raw.rstrip("+").strip())
            return SizeInfo(raw=raw, minimum=number, maximum=None, label=f"{number}+ employees")

    return SizeInfo(raw=raw, label="Unknown")


def _size_label(low: int, high: int | None) -> str:
    if high is None:
        return f"{low}+ employees"
    if low == high:
        return f"{low} employee" if low == 1 else f"{low} employees"
    return f"{low}-{high} employees"


def classify_size(
    size: SizeInfo, target_min: int = 1, target_max: int = 10
) -> str:
    """Classify a size against the target band.

    Only a fully explicit range inside the target is a *confirmed* match.  An
    open-ended ``1+`` or a broad ``1-20`` range is ``partial``; a missing value
    is ``unknown``.  Missing data is never promoted to a match.
    """
    low, high = size.minimum, size.maximum
    if low is None and high is None:
        return SizeStatus.UNKNOWN
    if high is None:
        assert low is not None
        if low > target_max:
            return SizeStatus.OUTSIDE
        return SizeStatus.PARTIAL
    if high < target_min or low is not None and low > target_max:
        return SizeStatus.OUTSIDE
    if low is None:
        return SizeStatus.PARTIAL
    if low >= target_min and high <= target_max:
        return SizeStatus.CONFIRMED
    return SizeStatus.PARTIAL


# ---------------------------------------------------------------------------
# Industry / niche
# ---------------------------------------------------------------------------
_INDUSTRY_ALIASES: dict[str, str] = {
    "artificial intelligence": "ai/ml",
    "machine learning": "ai/ml",
    "generative ai": "ai/ml",
    "ai": "ai/ml",
    "ml": "ai/ml",
    "software": "saas",
    "saas": "saas",
    "b2b saas": "saas",
    "fintech": "fintech",
    "financial technology": "fintech",
    "finance": "fintech",
    "healthcare": "healthtech",
    "health tech": "healthtech",
    "healthtech": "healthtech",
    "ecommerce": "ecommerce",
    "e-commerce": "ecommerce",
    "retail": "ecommerce",
    "developer tools": "devtools",
    "devtools": "devtools",
    "climate": "climatetech",
    "climate tech": "climatetech",
    "clean energy": "climatetech",
    "education": "edtech",
    "edtech": "edtech",
    "consumer": "consumer",
    "marketplace": "marketplace",
    "logistics": "logistics",
    "proptech": "proptech",
    "real estate": "proptech",
    "cybersecurity": "cybersecurity",
    "security": "cybersecurity",
    "web3": "web3",
    "blockchain": "web3",
    "crypto": "web3",
}


def niche_from_industry(industry: object) -> str:
    text = clean_text(industry, 300).lower()
    if not text:
        return ""
    if text in _INDUSTRY_ALIASES:
        return _INDUSTRY_ALIASES[text]
    for alias, niche in _INDUSTRY_ALIASES.items():
        if alias in text:
            return niche
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:80]


# ---------------------------------------------------------------------------
# Quality evaluation
# ---------------------------------------------------------------------------
_QUALITY_FIELDS = (
    "name",
    "website",
    "employee_count",
    "industry",
    "description",
    "wellfound_url",
    "job_urls",
    "location",
    "source_url",
)


def completeness_score(company: Company) -> int:
    """Percentage of the requested data fields present (not a validity claim)."""
    present = 0
    for field_name in _QUALITY_FIELDS:
        value = getattr(company, field_name)
        if field_name == "name":
            ok = bool(value)
        elif field_name == "job_urls":
            ok = bool(value)
        else:
            ok = bool(str(value or "").strip())
        present += 1 if ok else 0
    return round(100 * present / len(_QUALITY_FIELDS))


def evaluate_company(company: Company, target_min: int = 1, target_max: int = 10) -> Company:
    """Normalize fields and recompute size/completeness/validation in place."""
    company.name = clean_name(company.name)
    company.name_key = name_key(company.name)
    company.website = normalize_url(company.website)
    company.website_key = website_key(company.website)
    company.wellfound_url = canonical_wellfound_url(company.wellfound_url)
    company.industry = clean_text(company.industry, 300)
    company.niche = niche_from_industry(company.industry)
    company.description = strip_html(company.description)[:4000]
    company.location = clean_text(company.location, 300)
    company.source_name = clean_text(company.source_name, 120)
    company.source_url = normalize_source_url(company.source_url)
    company.notes = clean_text(company.notes, 2000)
    company.job_urls = clean_url_list(company.job_urls)

    size = parse_employee_size(company.employee_count)
    if not size.known and (company.employee_min is not None or company.employee_max is not None):
        size = SizeInfo(
            raw=company.employee_count,
            minimum=company.employee_min,
            maximum=company.employee_max,
        )
    company.employee_count = clean_text(company.employee_count, 100)
    if not company.employee_count and size.known:
        if size.label:
            company.employee_count = size.label
        elif size.minimum is not None:
            company.employee_count = _size_label(size.minimum, size.maximum)
        elif size.maximum is not None:
            company.employee_count = f"up to {size.maximum} employees"
    company.employee_min = size.minimum
    company.employee_max = size.maximum
    company.size_status = classify_size(size, target_min, target_max)
    company.size_match = company.size_status == SizeStatus.CONFIRMED

    company.completeness = completeness_score(company)

    if not company.name:
        company.validation_status = CompanyStatus.FAILED
    elif not (company.source_url or company.website or company.wellfound_url):
        company.validation_status = CompanyStatus.NEEDS_REVIEW
    elif not company.employee_count:
        company.validation_status = CompanyStatus.NEEDS_REVIEW
    elif company.size_status in {SizeStatus.UNKNOWN, SizeStatus.PARTIAL}:
        company.validation_status = CompanyStatus.NEEDS_REVIEW
    else:
        company.validation_status = CompanyStatus.VALIDATED

    if company.duplicate_of:
        company.status = CompanyStatus.DUPLICATE
    elif company.validation_status == CompanyStatus.FAILED:
        company.status = CompanyStatus.FAILED
    elif company.validation_status == CompanyStatus.VALIDATED:
        company.status = CompanyStatus.VALIDATED
    else:
        company.status = CompanyStatus.NEEDS_REVIEW

    if not company.record_id:
        company.record_id = record_id_for(company)
    return company


def clean_url_list(values: object) -> list[str]:
    """Normalize a list/semicolon/newline/comma-separated set of URLs."""
    if values is None:
        return []
    if isinstance(values, str):
        text = values.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    values = parsed
                else:
                    values = [text]
            except json.JSONDecodeError:
                values = re.split(r"[;\n|,]+", text)
        else:
            values = re.split(r"[;\n|,]+", text)
    elif not isinstance(values, (list, tuple, set)):
        values = [values]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = normalize_url(value)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def parse_datetime(value: object) -> str:
    """Return an ISO timestamp, or now when the value is absent/invalid."""
    if value:
        text = clean_text(value, 100)
        if text:
            try:
                normalized = text.replace("Z", "+00:00")
                moment = datetime.fromisoformat(normalized)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
            except ValueError:
                pass
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y"):
                try:
                    moment = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                    return moment.isoformat(timespec="seconds")
                except ValueError:
                    continue
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def split_text_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [clean_text(item, 1000) for item in value if clean_text(item, 1000)]
    return [part.strip() for part in re.split(r"[;\n|]+", str(value)) if part.strip()]


def unique_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def is_unknown(value: Any) -> bool:
    return clean_text(value, 200).lower() in _UNKNOWN_VALUES
