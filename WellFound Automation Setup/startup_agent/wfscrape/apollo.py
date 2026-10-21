"""Read Wellfound's embedded Next.js/Apollo data instead of visible HTML.

Every Wellfound page ships a ``__NEXT_DATA__`` script whose ``apolloState``
holds an Apollo normalized cache.  The records in that cache are the same ones
the page renders from, so reading them is both more complete and far more
stable than scraping visible text - a CSS change cannot break it, and a
redesigned card cannot hide a field.

Nothing in this module makes a network request; it only parses markup that was
already fetched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: The Next.js data island.  The attribute order varies between builds, so the
#: id is matched loosely.
_NEXT_DATA_RE = re.compile(
    r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.S | re.I
)

#: Wellfound's own company-size enum.  These are the values that appear in
#: ``companySize``; they are not the same strings the site shows a visitor.
WELLFOUND_SIZE_CODES: dict[str, str] = {
    "1-10": "SIZE_1_10",
    "11-50": "SIZE_11_50",
    "51-200": "SIZE_51_200",
    "201-500": "SIZE_201_500",
    "501-1000": "SIZE_501_1000",
}

#: The reverse mapping: Wellfound's enum back to the range a visitor sees.
WELLFOUND_SIZE_LABELS: dict[str, str] = {
    "SIZE_1_10": "1-10",
    "SIZE_11_50": "11-50",
    "SIZE_51_200": "51-200",
    "SIZE_201_500": "201-500",
    "SIZE_501_1000": "501-1000",
}


class ApolloParseError(RuntimeError):
    """The page did not contain a readable Next.js/Apollo data island."""


def size_code_for(label: str) -> str:
    """Map ``"1-10"`` to Wellfound's ``"SIZE_1_10"``.

    Raises ``ValueError`` for a range Wellfound does not publish.
    """
    key = str(label or "").strip().replace("–", "-").replace("—", "-")
    if key in WELLFOUND_SIZE_CODES:
        return WELLFOUND_SIZE_CODES[key]
    raise ValueError(
        f"unsupported company size {label!r}; "
        f"choose one of: {', '.join(WELLFOUND_SIZE_CODES)}"
    )


def size_label(code: str) -> str:
    """Map ``"SIZE_1_10"`` back to ``"1-10"`` (unknown codes pass through)."""
    return WELLFOUND_SIZE_LABELS.get(str(code or "").strip(), str(code or "").strip())


def parse_apollo_state(markup: str) -> dict[str, Any]:
    """Return the Apollo normalized cache embedded in a Wellfound page.

    An empty dict is returned when the page has no data island (for example a
    login or security-check page) so callers can simply test for emptiness.
    """
    if not markup:
        return {}
    match = _NEXT_DATA_RE.search(markup)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except (TypeError, ValueError) as exc:
        raise ApolloParseError("the Next.js data island is not valid JSON") from exc
    if not isinstance(data, dict):
        return {}
    props = data.get("props")
    if not isinstance(props, dict):
        return {}
    page_props = props.get("pageProps")
    if not isinstance(page_props, dict):
        return {}
    state = page_props.get("apolloState")
    if not isinstance(state, dict):
        return {}
    data_block = state.get("data")
    if isinstance(data_block, dict):
        return data_block
    return state


def _resolve_refs(state: dict[str, Any], value: Any) -> list[dict[str, Any]]:
    """Resolve a list of Apollo ``{"__ref": "Type:id"}`` pointers."""
    if not isinstance(value, list):
        return []
    resolved: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        ref = item.get("__ref")
        if not isinstance(ref, str):
            continue
        target = state.get(ref)
        if isinstance(target, dict):
            resolved.append(target)
    return resolved


def _labels(state: dict[str, Any], value: Any, *keys: str) -> list[str]:
    """Resolve references and collect their first non-empty label.

    Badges use ``label``; market/location tags use ``displayName``.
    """
    labels: list[str] = []
    for record in _resolve_refs(state, value):
        for key in keys:
            text = record.get(key)
            if isinstance(text, str) and text.strip():
                label = text.strip()
                if label not in labels:
                    labels.append(label)
                break
    return labels


@dataclass(slots=True)
class StartupCard:
    """One company as listed on a role landing page."""

    company_id: str
    name: str
    slug: str
    company_size: str = ""
    high_concept: str = ""
    badges: list[str] = field(default_factory=list)

    @property
    def profile_url(self) -> str:
        return f"https://wellfound.com/company/{self.slug}"

    @property
    def size_label(self) -> str:
        return size_label(self.company_size)


@dataclass(slots=True)
class StartupProfile:
    """A company profile page: its own website plus the tags it publishes."""

    company_id: str
    name: str
    slug: str
    company_size: str = ""
    company_url: str = ""
    high_concept: str = ""
    market_tags: list[str] = field(default_factory=list)
    location_tags: list[str] = field(default_factory=list)
    badges: list[str] = field(default_factory=list)
    website_source: str = ""

    @property
    def profile_url(self) -> str:
        return f"https://wellfound.com/company/{self.slug}"

    @property
    def size_label(self) -> str:
        return size_label(self.company_size)

    @property
    def niche(self) -> str:
        """The industry/market tags as one readable string."""
        return ", ".join(self.market_tags)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def startup_cards(state: dict[str, Any]) -> list[StartupCard]:
    """Every company card in an Apollo state, keyed by Wellfound company id."""
    cards: list[StartupCard] = []
    seen: set[str] = set()
    for key, record in state.items():
        if not key.startswith("StartupResult:") or not isinstance(record, dict):
            continue
        company_id = _text(record.get("id"))
        slug = _text(record.get("slug"))
        name = _text(record.get("name"))
        if not company_id or not slug or not name:
            continue
        if company_id in seen:
            continue
        seen.add(company_id)
        cards.append(
            StartupCard(
                company_id=company_id,
                name=name,
                slug=slug,
                company_size=_text(record.get("companySize")),
                high_concept=_text(record.get("highConcept")),
                badges=_labels(state, record.get("badges"), "label", "displayName"),
            )
        )
    cards.sort(key=lambda card: card.name.lower())
    return cards


def _profile_from(state: dict[str, Any], record: dict[str, Any]) -> StartupProfile:
    company_id = _text(record.get("id"))
    slug = _text(record.get("slug"))
    return StartupProfile(
        company_id=company_id,
        name=_text(record.get("name")),
        slug=slug,
        company_size=_text(record.get("companySize")),
        company_url=_text(record.get("companyUrl")),
        high_concept=_text(record.get("highConcept")),
        market_tags=_labels(state, record.get("marketTaggings"), "displayName", "name"),
        location_tags=_labels(
            state, record.get("locationTaggings"), "displayName", "name"
        ),
        badges=_labels(state, record.get("badges"), "label", "displayName"),
        website_source="Wellfound company profile" if record.get("companyUrl") else "",
    )


def startup_profiles(state: dict[str, Any]) -> list[StartupProfile]:
    """Every company profile record in an Apollo state."""
    profiles: list[StartupProfile] = []
    seen: set[str] = set()
    for key, record in state.items():
        if not key.startswith("Startup:") or not isinstance(record, dict):
            continue
        slug = _text(record.get("slug"))
        company_id = _text(record.get("id"))
        if not slug or not company_id or company_id in seen:
            continue
        seen.add(company_id)
        profiles.append(_profile_from(state, record))
    return profiles


def find_profile(state: dict[str, Any], slug: str) -> StartupProfile | None:
    """The profile record for one company slug, if the page carries it."""
    wanted = str(slug or "").strip().lower()
    if not wanted:
        return None
    for profile in startup_profiles(state):
        if profile.slug.lower() == wanted:
            return profile
    return None


def filter_by_size(
    cards: Iterable[StartupCard], size_code: str
) -> list[StartupCard]:
    """Keep only the companies whose published size matches ``size_code``."""
    wanted = str(size_code or "").strip()
    if not wanted:
        return list(cards)
    return [card for card in cards if card.company_size == wanted]
