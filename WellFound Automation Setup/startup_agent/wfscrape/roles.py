"""The editable list of Wellfound role pages the scraper walks.

Wellfound has no flat company directory - its search is organised by job role.
Each role has a public landing page (``/role/r/<slug>``) that lists companies
hiring for that role, and every company on those pages is a real startup.

The list lives in a plain text file so it can be edited without touching any
code::

    # one role slug per line
    software-engineer
    full-stack-engineer

Blank lines and ``#`` comments are ignored.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Used when the roles file is missing, so the tool still works out of the box.
DEFAULT_ROLE_SLUGS: tuple[str, ...] = (
    "software-engineer",
    "full-stack-engineer",
    "full-stack-developer",
    "backend-engineer",
    "backend-developer",
    "frontend-engineer",
    "ai-engineer",
    "mobile-engineer",
    "devops-engineer",
    "machine-learning-engineer",
    "data-scientist",
    "data-analyst",
    "product-manager",
    "product-designer",
    "ui-ux-designer",
    "operations-manager",
    "marketing",
    "growth-marketer",
    "account-manager",
    "customer-success",
    "sales-manager",
    "financial-analyst",
    "hr-manager",
    "qa-engineer",
    "security-engineer",
)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_roles(text: str) -> list[str]:
    """Parse the role list out of a text file, keeping the file's order."""
    roles: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        slug = line.lower()
        if not _SLUG_RE.match(slug):
            continue
        if slug in seen:
            continue
        seen.add(slug)
        roles.append(slug)
    return roles


def load_roles(path: Path | str) -> list[str]:
    """Read the role list from ``path``.

    A missing or unreadable file falls back to :data:`DEFAULT_ROLE_SLUGS`
    rather than failing the run - the tool should still work.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return list(DEFAULT_ROLE_SLUGS)
    roles = parse_roles(text)
    return roles or list(DEFAULT_ROLE_SLUGS)


def role_page_url(slug: str, page: int = 1) -> str:
    """The public URL of one page of a role landing page."""
    base = f"https://wellfound.com/role/r/{slug}"
    page = max(1, int(page or 1))
    return base if page == 1 else f"{base}?page={page}"
