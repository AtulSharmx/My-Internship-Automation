"""The scrape snapshot: one clean handoff file per company.

The column contract is fixed and deliberate - it is a snapshot for another
project to consume, so it carries **data only**, never pipeline state (no
status, no contacted flag, no lead stage):

    company_name | website | domain | team_size | niche
    wellfound_profile_url | date_scraped | wellfound_company_id

``domain`` is derived from ``website`` (bare host, lowercased, no scheme, no
``www.``, no path).  ``wellfound_company_id`` is Wellfound's own company id -
the stable reference key that de-duplicates the company across roles.

The files are written UTF-8 with a BOM so Excel opens them correctly, and they
never overwrite an earlier export - the filename carries a timestamp.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from ..export import _xlsx_bytes
from ..models import Company

#: The columns of the snapshot, in order.  This is the contract.
SCRAPE_COLUMNS: tuple[str, ...] = (
    "company_name",
    "website",
    "domain",
    "team_size",
    "niche",
    "wellfound_profile_url",
    "date_scraped",
    "wellfound_company_id",
)

#: The headers, which are the column names themselves.
SCRAPE_HEADERS: dict[str, str] = {column: column for column in SCRAPE_COLUMNS}

#: Column widths for the spreadsheet.
SCRAPE_WIDTHS: dict[str, float] = {
    "company_name": 30,
    "website": 38,
    "domain": 26,
    "team_size": 12,
    "niche": 52,
    "wellfound_profile_url": 44,
    "date_scraped": 14,
    "wellfound_company_id": 20,
}

#: Columns that are clickable links in the spreadsheet.
SCRAPE_LINK_COLUMNS: tuple[str, ...] = ("website", "wellfound_profile_url")

#: The file kinds this module writes.
SCRAPE_KINDS: tuple[str, ...] = ("csv", "xlsx", "json")

_SUFFIXES = {"csv": ".csv", "xlsx": ".xlsx", "json": ".json"}


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text


def bare_domain(url: str) -> str:
    """The bare host of a URL: lowercase, no scheme, no ``www.``, no path.

    ``https://www.Acme.Startup.com/jobs?x=1`` -> ``acme.startup.com``
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "//" + text
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    host = host.strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _date_part(value: str) -> str:
    """``2026-10-20T23:47:00+00:00`` -> ``2026-10-20``."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10]


def row_from_company(company: Company) -> dict[str, str]:
    """Build one snapshot row from a stored company record."""
    company_id = company.source_id or ""
    if not company_id:
        company_id = str((company.raw or {}).get("wellfound_company_id") or "")
    return {
        "company_name": _cell(company.name),
        "website": _cell(company.website),
        "domain": bare_domain(company.website),
        "team_size": _cell(company.employee_count),
        # The market/industry tags Wellfound publishes for the company.
        "niche": _cell(company.industry),
        "wellfound_profile_url": _cell(company.wellfound_url),
        "date_scraped": _date_part(company.discovered_at),
        "wellfound_company_id": _cell(company_id),
    }


def unique_scrape_path(
    export_dir: Path, stamp: str | None = None, kind: str = "csv"
) -> Path:
    """A timestamped path in ``export_dir`` that does not overwrite anything."""
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = _SUFFIXES.get(kind, ".csv")
    candidate = export_dir / f"wf-scrape-{stamp}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = export_dir / f"wf-scrape-{stamp}-{counter}{suffix}"
        counter += 1
    return candidate


def write_scrape_csv(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Write the snapshot as CSV and return the number of data rows."""
    written = 0
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[SCRAPE_HEADERS[column] for column in SCRAPE_COLUMNS],
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    SCRAPE_HEADERS[column]: _cell(row.get(column, ""))
                    for column in SCRAPE_COLUMNS
                }
            )
            written += 1
    return written


def write_scrape_xlsx(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Write the snapshot as a spreadsheet and return the number of data rows."""
    data = list(rows)
    Path(path).write_bytes(
        _xlsx_bytes(
            data,
            columns=SCRAPE_COLUMNS,
            header_map=SCRAPE_HEADERS,
            widths=SCRAPE_WIDTHS,
            link_columns=SCRAPE_LINK_COLUMNS,
        )
    )
    return len(data)


def write_scrape_json(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Write the snapshot as JSON and return the number of data rows."""
    data = [
        {column: _cell(row.get(column, "")) for column in SCRAPE_COLUMNS}
        for row in rows
    ]
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return len(data)


def write_scrape_files(
    export_dir: Path, rows: Iterable[dict[str, Any]], *, stamp: str | None = None
) -> dict[str, Path]:
    """Write the snapshot as CSV, XLSX and JSON.

    Returns the path of each written file, keyed by kind.
    """
    data = list(rows)
    paths: dict[str, Path] = {}
    for kind, writer in (
        ("csv", write_scrape_csv),
        ("xlsx", write_scrape_xlsx),
        ("json", write_scrape_json),
    ):
        path = unique_scrape_path(export_dir, stamp=stamp, kind=kind)
        writer(path, data)
        paths[kind] = path
    return paths
