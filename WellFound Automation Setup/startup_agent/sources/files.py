"""CSV, JSON, JSONL, saved-HTML and XLSX import adapters."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from ..models import Company, ImportBatch, ImportErrorRow
from ..normalize import (
    canonical_wellfound_url,
    clean_name,
    clean_text,
    clean_url_list,
    normalize_url,
    parse_datetime,
    parse_employee_size,
    source_slug,
)
from .extract import extract_companies, parse_page
from .wellfound import SavedPageError, parse_wellfound_saved

SUPPORTED_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".html", ".htm", ".xlsx"}


class FileImportError(RuntimeError):
    """The file as a whole could not be parsed."""


_ALIASES: dict[str, set[str]] = {
    "name": {
        "name",
        "company",
        "company_name",
        "companyname",
        "startup",
        "startup_name",
        "organization",
        "organisation",
        "employer",
        "brand",
        "business",
    },
    "website": {
        "website",
        "website_url",
        "company_url",
        "company_website",
        "homepage",
        "site",
        "web",
        "domain",
        "official_website",
    },
    "employee_count": {
        "employee_count",
        "employees",
        "employee_size",
        "employee_range",
        "company_size",
        "size",
        "headcount",
        "team_size",
        "number_of_employees",
        "numberofemployees",
        "staff_count",
    },
    "industry": {
        "industry",
        "market",
        "sector",
        "category",
        "vertical",
        "niche",
    },
    "description": {
        "description",
        "about",
        "company_description",
        "summary",
        "tagline",
        "overview",
    },
    "wellfound_url": {
        "wellfound_url",
        "wellfound",
        "wellfound_profile",
        "wellfound_profile_url",
        "profile_url",
        "company_profile",
        "company_profile_url",
    },
    "job_urls": {
        "job_urls",
        "jobs",
        "job_listings",
        "job_listing_urls",
        "job_url",
        "listing_urls",
        "open_roles",
    },
    "location": {
        "location",
        "company_location",
        "headquarters",
        "hq",
        "city",
        "country",
        "address",
    },
    "source_url": {
        "source_url",
        "source_link",
        "reference_url",
        "verification_url",
        "origin_url",
    },
    "source_name": {
        "source_name",
        "source",
        "dataset",
        "provider",
    },
    "source_id": {
        "source_id",
        "company_id",
        "startup_id",
        "profile_id",
        "slug",
    },
    "discovered_at": {
        "discovered_at",
        "discovery_date",
        "date_discovered",
        "found_at",
        "created_at",
    },
    "status": {"status", "record_status", "validation_status"},
    "notes": {"notes", "note", "comment", "comments"},
    "record_id": {"record_id", "id", "internal_id"},
}


def _normalized_header(value: str) -> str:
    text = clean_text(value, 100).lower().lstrip("\ufeff")
    text = re.sub(r"[\s\-/]+", "_", text)
    text = re.sub(r"[^a-z0-9_]+", "", text)
    return text.strip("_")


_ALIAS_LOOKUP = {
    alias: canonical for canonical, aliases in _ALIASES.items() for alias in aliases
}


def _canonical_row(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        canonical = _ALIAS_LOOKUP.get(_normalized_header(str(key)))
        if canonical and canonical not in result and value not in (None, ""):
            result[canonical] = value
    return result


def _text(value: Any, limit: int = 4000) -> str:
    if isinstance(value, list):
        return "; ".join(_text(item, limit) for item in value if _text(item, limit))
    if isinstance(value, dict):
        return clean_text(json.dumps(value, ensure_ascii=False), limit)
    return clean_text(value, limit)


def _record_id(value: Any) -> str:
    text = clean_text(value, 100)
    return text if re.fullmatch(r"co_[0-9a-f]{16}", text) else ""


def company_from_mapping(
    row: dict[str, Any],
    *,
    source_name: str = "",
    source_url: str = "",
    row_ref: int | str = "",
) -> Company | None:
    """Convert one flexible mapping into a Company (without evaluating it)."""
    data = _canonical_row(row)
    name = clean_name(data.get("name"))
    if not name:
        return None

    wellfound_url = canonical_wellfound_url(data.get("wellfound_url"))
    website = normalize_url(data.get("website"))
    employee_count = _text(data.get("employee_count"), 200)
    size = parse_employee_size(employee_count)
    location = _text(data.get("location"), 300)
    industry = _text(data.get("industry"), 300)
    description = _text(data.get("description"), 4000)
    source = _text(data.get("source_name"), 120) or source_name or "manual_import"
    source_id = _text(data.get("source_id"), 200) or source_slug(wellfound_url)
    source_link = (
        normalize_url(data.get("source_url"))
        or wellfound_url
        or website
        or normalize_url(source_url)
    )
    job_urls = clean_url_list(data.get("job_urls"))
    discovered_at = parse_datetime(data.get("discovered_at"))
    notes = _text(data.get("notes"), 2000)
    record_id = _record_id(data.get("record_id")) or ""

    return Company(
        record_id=record_id,
        name=name,
        website=website,
        employee_count=employee_count or (size.label if size.known else ""),
        employee_min=size.minimum,
        employee_max=size.maximum,
        industry=industry,
        description=description,
        wellfound_url=wellfound_url,
        job_urls=job_urls,
        location=location,
        source_url=source_link,
        source_name=source,
        source_id=source_id,
        discovered_at=discovered_at,
        last_checked_at=discovered_at,
        notes=notes,
        raw=dict(row) if isinstance(row, dict) else {},
    )


class FileImporter:
    """Parse a supported local file into normalized company records.

    Parsing is independent of storage.  The discovery service evaluates and
    upserts the resulting records.
    """

    def __init__(self, *, max_bytes: int = 10 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes

    def parse(self, path: Path, *, source_name: str = "") -> ImportBatch:
        path = Path(path)
        if not path.exists() or not path.is_file():
            raise FileImportError(f"file not found: {path}")
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise FileImportError(
                f"unsupported file type {suffix or '(none)'}; use "
                + ", ".join(sorted(SUPPORTED_SUFFIXES))
            )
        size = path.stat().st_size
        if size > self.max_bytes:
            raise FileImportError(
                f"file is {size // 1024} KB; limit is {self.max_bytes // 1024} KB"
            )
        data = path.read_bytes()
        file_hash = hashlib.sha256(data).hexdigest()
        batch = ImportBatch(file_name=path.name, file_hash=file_hash)
        if suffix in {".csv", ".tsv"}:
            text = self._decode(data)
            batch.format = suffix.lstrip(".")
            self._parse_delimited(text, batch, source_name=source_name or path.stem)
        elif suffix in {".json", ".jsonl", ".ndjson"}:
            text = self._decode(data)
            batch.format = suffix.lstrip(".")
            if suffix == ".json":
                self._parse_json(text, batch, source_name=source_name or path.stem, filename=path.name)
            else:
                self._parse_jsonl(text, batch, source_name=source_name or path.stem, filename=path.name)
        elif suffix in {".html", ".htm"}:
            text = self._decode(data)
            batch.format = "html"
            self._parse_html(text, batch, filename=path.name, source_name=source_name)
        elif suffix == ".xlsx":
            batch.format = "xlsx"
            self._parse_xlsx(path, batch, source_name=source_name or path.stem)
        return batch

    @staticmethod
    def _decode(data: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Delimited text
    # ------------------------------------------------------------------
    def _parse_delimited(self, text: str, batch: ImportBatch, *, source_name: str) -> None:
        sample = text[:8192]
        delimiter = ","
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            delimiter = dialect.delimiter
        except csv.Error:
            if "\t" in sample and sample.count("\t") > sample.count(","):
                delimiter = "\t"
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        if not reader.fieldnames:
            raise FileImportError("CSV/TSV file has no header row")
        rows = list(reader)
        batch.total_rows = len(rows)
        for index, row in enumerate(rows, start=2):
            if not any(str(value or "").strip() for value in row.values()):
                continue
            company = company_from_mapping(
                row, source_name=f"file:{source_name}", source_url="", row_ref=index
            )
            if company is None:
                batch.errors.append(
                    ImportErrorRow(index, "missing company name", clean_text(row, 1000))
                )
                continue
            batch.companies.append(company)

    # ------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------
    def _parse_json(self, text: str, batch: ImportBatch, *, source_name: str, filename: str) -> None:
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise FileImportError(f"invalid JSON: {exc}") from exc
        records = self._json_records(payload)
        batch.total_rows = len(records)
        for index, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                batch.errors.append(ImportErrorRow(index, "JSON item is not an object", ""))
                continue
            company = company_from_mapping(
                record, source_name=f"file:{source_name}", row_ref=index
            )
            if company is None:
                batch.errors.append(
                    ImportErrorRow(index, "missing company name", clean_text(record, 1000))
                )
                continue
            batch.companies.append(company)

    @staticmethod
    def _json_records(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("companies", "data", "results", "items", "records"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            if payload.get("name"):
                return [payload]
        raise FileImportError(
            "JSON must be a list of companies or an object with a companies/data/results list"
        )

    def _parse_jsonl(self, text: str, batch: ImportBatch, *, source_name: str, filename: str) -> None:
        lines = [line for line in text.splitlines() if line.strip()]
        batch.total_rows = len(lines)
        for index, line in enumerate(lines, start=1):
            try:
                record = json.loads(line)
            except ValueError as exc:
                batch.errors.append(ImportErrorRow(index, f"invalid JSON line: {exc}", line[:1000]))
                continue
            if not isinstance(record, dict):
                batch.errors.append(ImportErrorRow(index, "JSON line is not an object", line[:1000]))
                continue
            company = company_from_mapping(
                record, source_name=f"file:{source_name}", row_ref=index
            )
            if company is None:
                batch.errors.append(
                    ImportErrorRow(index, "missing company name", clean_text(record, 1000))
                )
                continue
            batch.companies.append(company)

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------
    def _parse_html(
        self, text: str, batch: ImportBatch, *, filename: str, source_name: str
    ) -> None:
        page = parse_page(text, url="")
        lowered = (page.text or "").lower()
        challenge_markers = (
            "just a moment",
            "verify you are human",
            "checking your browser",
            "enable javascript and cookies to continue",
            "captcha",
        )
        is_wellfound_page = bool(
            canonical_wellfound_url(page.canonical)
            or any(canonical_wellfound_url(anchor.href) for anchor in page.anchors)
        )
        try:
            if is_wellfound_page:
                companies = parse_wellfound_saved(text, filename=f"file:{filename}")
            elif any(marker in lowered for marker in challenge_markers):
                raise FileImportError(
                    "The saved page looks like a login or CAPTCHA/challenge page. "
                    "Open the site manually in your own browser, complete the normal "
                    "sign-in yourself, then save the company/list page and import it again."
                )
            else:
                companies = extract_companies(
                    page, source_name=source_name or f"file:{filename}"
                )
        except SavedPageError as exc:
            raise FileImportError(str(exc)) from exc
        batch.total_rows = len(companies)
        if not companies:
            batch.errors.append(
                ImportErrorRow(
                    "page",
                    "no company records found in the saved HTML; save the company/list page "
                    "after the page has fully loaded",
                    "",
                )
            )
            return
        for company in companies:
            if not company.source_url:
                company.source_url = f"saved://file:{filename}"
            if not company.source_name:
                company.source_name = "saved_html"
            batch.companies.append(company)

    # ------------------------------------------------------------------
    # XLSX (read only the simple workbook shape we export, plus common Excel files)
    # ------------------------------------------------------------------
    def _parse_xlsx(self, path: Path, batch: ImportBatch, *, source_name: str) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                shared: list[str] = []
                if "xl/sharedStrings.xml" in names:
                    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                    for item in root.iter():
                        if item.tag.endswith("}si") or item.tag == "si":
                            shared.append("".join(item.itertext()))
                sheet_name = next(
                    (
                        name
                        for name in sorted(names)
                        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
                    ),
                    "",
                )
                if not sheet_name:
                    raise FileImportError("XLSX has no worksheet")
                root = ElementTree.fromstring(archive.read(sheet_name))
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
            raise FileImportError(f"invalid XLSX: {exc}") from exc

        rows: dict[int, dict[int, str]] = {}
        for cell in root.iter():
            if not (cell.tag.endswith("}c") or cell.tag == "c"):
                continue
            ref = cell.attrib.get("r", "")
            match = re.fullmatch(r"([A-Z]+)(\d+)", ref)
            if not match:
                continue
            col_text, row_text = match.group(1), int(match.group(2))
            col = 0
            for char in col_text:
                col = col * 26 + (ord(char.upper()) - 64)
            value = ""
            cell_type = cell.attrib.get("t", "")
            for child in cell:
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "v" and child.text is not None:
                    value = child.text
                    break
                if tag == "is":
                    value = "".join(child.itertext())
                    break
            if cell_type == "s" and value:
                try:
                    value = shared[int(value)]
                except (ValueError, IndexError):
                    value = ""
            rows.setdefault(row_text, {})[col] = value

        if not rows:
            raise FileImportError("XLSX worksheet is empty")
        header_row = rows[min(rows)]
        headers = {index: value for index, value in header_row.items() if value}
        if not headers:
            raise FileImportError("XLSX worksheet has no header row")
        data_rows = [
            row for number, row in sorted(rows.items()) if number != min(rows)
        ]
        batch.total_rows = len(data_rows)
        for index, cells in enumerate(data_rows, start=2):
            record = {header: cells.get(col, "") for col, header in headers.items()}
            if not any(str(value or "").strip() for value in record.values()):
                continue
            company = company_from_mapping(
                record, source_name=f"file:{source_name}", row_ref=index
            )
            if company is None:
                batch.errors.append(
                    ImportErrorRow(index, "missing company name", clean_text(record, 1000))
                )
                continue
            batch.companies.append(company)
