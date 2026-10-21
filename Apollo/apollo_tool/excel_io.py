"""Read the input spreadsheet and write the finished, formatted workbook."""

from __future__ import annotations

import csv
import json
import math
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .config import ADDED_COLUMNS, REQUIRED_COLUMNS
from .models import CompanyResult, InputRow, InputWorkbook

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Calibri", size=11)
LINK_FONT = Font(name="Calibri", size=11, color="0563C1", underline="single")
BAND_FILL = PatternFill("solid", fgColor="F4F7FB")
VERIFIED_FILL = PatternFill("solid", fgColor="C6EFCE")
GUESSED_FILL = PatternFill("solid", fgColor="FFEB9C")
LOW_FILL = PatternFill("solid", fgColor="FCE4D6")
SKIP_FILL = PatternFill("solid", fgColor="E7E6E6")
THIN = Side(style="thin", color="D9E2F3")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

WIDTH_CAPS = {
    "company_name": 34,
    "domain": 28,
    "team_size": 18,
    "niche": 34,
    "wellfound_profile_url": 58,
    "date_scraped": 24,
    "wellfound_company_id": 24,
    "email": 34,
    "email_source": 14,
    "notes": 82,
    "company": 30,
    "full_name": 26,
    "title": 38,
    "source": 12,
    "syntax_mx_validation": 34,
    "confidence": 12,
    "website_verification_status": 20,
    "exact_member_count": 18,
    "last_checked": 24,
    "source_of_website_evidence": 58,
    "processing_status": 16,
    "employee_name": 28,
    "company_members": 20,
    "job_title": 38,
    "website": 34,
}


class InputError(ValueError):
    pass


def normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split("@", 1)[-1]
    text = text.split(":", 1)[0]
    text = text.strip().strip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


def _stringify_header(value: Any) -> str:
    return str(value).strip().lstrip("\ufeff") if value is not None else ""


# Common spreadsheet labels are normalized before matching. Case, spaces, and
# punctuation (for example "Company Name" vs "company_name") are ignored.
HEADER_ALIASES: dict[str, set[str]] = {
    "company_name": {
        "company",
        "organization",
        "organisation",
        "startup",
        "startup_name",
        "company_title",
    },
    "website": {
        "website_url",
        "official_website",
        "official_site",
        "company_website",
        "web_address",
        "website_link",
    },
    "domain": {"domain_name", "company_domain", "website_domain", "root_domain"},
    "team_size": {
        "company_size",
        "employee_count",
        "employee_range",
        "employees",
        "employees_range",
        "headcount",
        "team_count",
    },
    "niche": {"industry", "sector", "category", "industries"},
    "wellfound_profile_url": {
        "wellfound_url",
        "wellfound_profile",
        "profile_url",
        "wellfound_company_url",
    },
    "date_scraped": {"scraped_date", "scraped_at", "date_collected", "discovery_date"},
    "wellfound_company_id": {"wellfound_id", "company_id", "wellfound_company_slug"},
}


def _normalize_header(value: Any) -> str:
    text = _stringify_header(value).casefold()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _canonical_header(value: Any) -> str:
    normalized = _normalize_header(value)
    for canonical, aliases in HEADER_ALIASES.items():
        if normalized == canonical or normalized in aliases:
            return canonical
    return normalized


def _missing_core_columns(canonical_headers: list[str]) -> list[str]:
    """Return the minimum columns needed to process a row.

    The legacy layout has an explicit domain and niche column, while the
    verified Wellfound export has only an official website. The latter is
    enough because the domain is derived from the URL.
    """
    present = set(canonical_headers)
    missing: list[str] = []
    if "company_name" not in present:
        missing.append("company_name (or Startup Name)")
    if "website" not in present and "domain" not in present:
        missing.append("website (or domain)")
    return missing


def _wellfound_slug(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"/company/([^/?#]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    if text and "://" not in text and "/" not in text:
        return text
    return ""


def _build_input_row(
    headers: list[str],
    canonical_headers: list[str],
    values: list[Any],
    row_number: int,
) -> InputRow:
    data: dict[str, Any] = {}
    for index, key in enumerate(canonical_headers):
        if key:
            data.setdefault(key, values[index])

    # Verified exports use "Employee Range"; "Exact Member Count" is a useful
    # fallback when the range is blank.
    if not str(data.get("team_size") or "").strip():
        exact = data.get("exact_member_count")
        if str(exact or "").strip():
            data["team_size"] = exact

    profile_url = data.get("wellfound_profile_url")
    if not str(data.get("wellfound_company_id") or "").strip():
        slug = _wellfound_slug(profile_url)
        if slug:
            data["wellfound_company_id"] = slug

    domain = normalize_domain(data.get("domain")) or normalize_domain(data.get("website"))
    return InputRow(
        row_number=row_number,
        values=values,
        data=data,
        domain=domain,
        company_name=str(data.get("company_name") or "").strip(),
        wellfound_company_id=str(data.get("wellfound_company_id") or "").strip(),
    )


def _find_header_row(ws) -> int:
    max_scan = min(ws.max_row or 1, 10)
    for row_idx in range(1, max_scan + 1):
        values = {
            _canonical_header(ws.cell(row=row_idx, column=col).value)
            for col in range(1, min(ws.max_column or 1, 40) + 1)
        }
        if "company_name" in values and ("website" in values or "domain" in values):
            return row_idx
    return 1


def _header_cells(ws, row_idx: int) -> list[tuple[str, int]]:
    cells: list[tuple[str, int]] = []
    for col in range(1, (ws.max_column or 0) + 1):
        header = _stringify_header(ws.cell(row=row_idx, column=col).value)
        if header:
            cells.append((header, col))
    return cells


def _select_input_sheet(wb):
    """Pick the first sheet whose header row has all required columns.

    Fall back to the sheet with the most matching headers, then to the first
    sheet and the old heuristic. This handles workbooks with an instructions
    or cover sheet before the data.
    """
    required = {column.casefold() for column in REQUIRED_COLUMNS}
    best: tuple[int, Any, int, list[tuple[str, int]]] | None = None
    for ws in wb.worksheets:
        max_scan = min(ws.max_row or 1, 10)
        for row_idx in range(1, max_scan + 1):
            cells = _header_cells(ws, row_idx)
            if not cells:
                continue
            normalized = {_canonical_header(header) for header, _ in cells}
            matches = len(required & normalized)
            if "company_name" in normalized and ("website" in normalized or "domain" in normalized):
                return ws, row_idx, cells
            if best is None or matches > best[0]:
                best = (matches, ws, row_idx, cells)
    if best is not None:
        return best[1], best[2], best[3]
    ws = wb.worksheets[0]
    row_idx = _find_header_row(ws)
    return ws, row_idx, _header_cells(ws, row_idx)


def _read_xlsx(path: Path) -> InputWorkbook:
    wb = load_workbook(path, data_only=False)
    if not wb.sheetnames:
        raise InputError("Workbook has no sheets")
    ws, header_row, header_cells = _select_input_sheet(wb)
    headers = [header for header, _ in header_cells]
    column_indexes = [col for _, col in header_cells]
    canonical_headers = [_canonical_header(header) for header in headers]
    if not headers:
        raise InputError("Could not find a header row")

    missing = _missing_core_columns(canonical_headers)
    if missing:
        raise InputError("Missing required columns: " + ", ".join(missing))

    number_formats: dict[str, str] = {}
    for idx, header in enumerate(headers):
        actual_col = column_indexes[idx]
        for row_idx in range(header_row + 1, min(header_row + 5, ws.max_row or header_row) + 1):
            cell = ws.cell(row=row_idx, column=actual_col)
            if cell.value not in (None, ""):
                number_formats[header] = cell.number_format or "General"
                break
        number_formats.setdefault(header, "General")

    widths: dict[str, float] = {}
    for idx, header in enumerate(headers):
        actual_col = column_indexes[idx]
        width = ws.column_dimensions[get_column_letter(actual_col)].width
        if width:
            widths[header] = width

    rows: list[InputRow] = []
    for row_idx in range(header_row + 1, (ws.max_row or 0) + 1):
        values = [ws.cell(row=row_idx, column=col).value for col in column_indexes]
        if all(v in (None, "") for v in values):
            continue
        rows.append(
            _build_input_row(headers, canonical_headers, values, row_idx)
        )
    if not rows:
        raise InputError("The spreadsheet has no data rows")
    wb.close()
    return InputWorkbook(
        path=path,
        sheet_name=ws.title,
        headers=headers,
        rows=rows,
        number_formats=number_formats,
        column_widths=widths,
    )


def _read_csv(path: Path) -> InputWorkbook:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        all_rows = [row for row in reader if any(str(cell).strip() for cell in row)]
    if not all_rows:
        raise InputError("CSV is empty")
    headers = [_stringify_header(cell) for cell in all_rows[0]]
    while headers and headers[-1] == "":
        headers.pop()
    canonical_headers = [_canonical_header(header) for header in headers]
    missing = _missing_core_columns(canonical_headers)
    if missing:
        raise InputError("Missing required columns: " + ", ".join(missing))
    rows: list[InputRow] = []
    for offset, raw in enumerate(all_rows[1:], start=2):
        values = [raw[i] if i < len(raw) else "" for i in range(len(headers))]
        rows.append(_build_input_row(headers, canonical_headers, values, offset))
    if not rows:
        raise InputError("The CSV has no data rows")
    return InputWorkbook(
        path=path,
        sheet_name=path.stem[:31] or "Companies",
        headers=headers,
        rows=rows,
        number_formats={header: "General" for header in headers},
        column_widths={},
    )


def _read_json(path: Path) -> InputWorkbook:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"Could not read JSON: {exc}") from exc

    if isinstance(payload, dict):
        records = payload.get("records")
    else:
        records = payload
    if not isinstance(records, list):
        raise InputError("JSON must be a list of records or contain a 'records' list")
    records = [record for record in records if isinstance(record, dict)]
    if not records:
        raise InputError("The JSON file has no records")

    headers = [_stringify_header(key) for key in records[0].keys()]
    headers = [header for header in headers if header]
    canonical_headers = [_canonical_header(header) for header in headers]
    missing = _missing_core_columns(canonical_headers)
    if missing:
        raise InputError("Missing required columns: " + ", ".join(missing))

    rows: list[InputRow] = []
    for offset, record in enumerate(records, start=2):
        values = [record.get(header) for header in headers]
        if all(value in (None, "") for value in values):
            continue
        rows.append(_build_input_row(headers, canonical_headers, values, offset))
    if not rows:
        raise InputError("The JSON file has no data rows")
    return InputWorkbook(
        path=path,
        sheet_name="Verified",
        headers=headers,
        rows=rows,
        number_formats={header: "General" for header in headers},
        column_widths={},
    )


def read_input(path: Path) -> InputWorkbook:
    if not path.exists():
        raise InputError(f"File not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _read_csv(path)
    if suffix == ".json":
        return _read_json(path)
    if suffix in {".xlsx", ".xlsm"}:
        return _read_xlsx(path)
    if suffix == ".xls":
        raise InputError("Old .xls files are not supported. Save the file as .xlsx, .csv or .json first.")
    raise InputError("Only .xlsx, .xlsm, .csv and .json files are supported")


def _display_len(value: Any) -> int:
    if isinstance(value, datetime):
        return 16
    if isinstance(value, date):
        return 12
    return len(str(value)) if value is not None else 0


def _safe_sheet_title(title: str) -> str:
    title = re.sub(r"[\[\]:*?/\\]", " ", title or "").strip()
    title = title[:31] or "Companies"
    if title.lower() == "team_emails":
        title = "Companies"
    return title


def _wrap_height(text: str, width: int) -> int:
    if not text:
        return 20
    lines = math.ceil(len(str(text)) / max(width - 2, 10))
    return max(20, min(96, 15 * lines + 4))


def _style_header(ws, columns: int) -> None:
    for col in range(1, columns + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"
    ws.print_title_rows = "1:1"
    ws.sheet_view.showGridLines = False


def _fit_columns(ws, headers: list[str], rows: list[list[Any]]) -> None:
    for idx, header in enumerate(headers, start=1):
        cap = WIDTH_CAPS.get(_canonical_header(header), 40)
        longest = len(header) + 2
        for row in rows:
            if idx - 1 < len(row):
                longest = max(longest, _display_len(row[idx - 1]) + 2)
        width = max(10, min(cap, longest))
        ws.column_dimensions[get_column_letter(idx)].width = width


def _apply_data_style(ws, headers: list[str], row_count: int) -> None:
    for row_idx in range(2, row_count + 1):
        for col_idx, header in enumerate(headers, start=1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = BODY_FONT
            cell.border = BORDER
            canonical = _canonical_header(header)
            cell.alignment = Alignment(
                vertical="top",
                wrap_text=canonical
                in {
                    "notes",
                    "niche",
                    "title",
                    "syntax_mx_validation",
                    "wellfound_profile_url",
                    "source_of_website_evidence",
                    "job_title",
                },
            )
            if row_idx % 2 == 0:
                cell.fill = BAND_FILL


def _result_for_row(
    row: InputRow,
    by_domain: dict[str, CompanyResult],
    by_wfid: dict[str, CompanyResult],
) -> CompanyResult | None:
    if row.domain and row.domain in by_domain:
        return by_domain[row.domain]
    if row.wellfound_company_id and row.wellfound_company_id in by_wfid:
        return by_wfid[row.wellfound_company_id]
    return None


def _unprocessed_note(row: InputRow, statuses: dict[str, dict[str, Any]]) -> str:
    if not row.domain:
        return "missing domain/website — skipped; no credit spent"
    status = statuses.get(row.domain, {})
    state = status.get("status")
    if state in {"attempting", "unknown"}:
        return (
            "previous run stopped during reveal; a credit may have been spent. "
            "Not retried automatically — manual review needed."
        )
    if state == "stopped":
        return "previous run stopped before reveal; no credit spent"
    return "not processed yet (run limit reached or waiting); no credit spent"


def _employee_rows(
    input_wb: InputWorkbook,
    by_domain: dict[str, CompanyResult],
    by_wfid: dict[str, CompanyResult],
) -> list[list[Any]]:
    """Build the clean, people-first view requested for the finished workbook.

    One row per discovered employee, with the email intentionally kept as the
    last column. Companies that were not processed yet still get one row so no
    input company disappears from the primary sheet.
    """
    rows: list[list[Any]] = []
    seen_domains: set[str] = set()
    for input_row in input_wb.rows:
        if not input_row.domain or input_row.domain in seen_domains:
            continue
        seen_domains.add(input_row.domain)
        result = _result_for_row(input_row, by_domain, by_wfid)
        company = (
            (result.company_name if result else "")
            or input_row.company_name
            or input_row.domain
        )
        website = str(input_row.data.get("website") or "").strip()
        if not website and input_row.domain:
            website = f"https://{input_row.domain}"
        members = ""
        if result and str(result.facts.employees or "").strip():
            members = str(result.facts.employees).strip()
        if not members:
            members = str(
                input_row.data.get("exact_member_count")
                or input_row.data.get("team_size")
                or ""
            ).strip()

        people = list(result.people) if result else []
        if people:
            for person in people:
                rows.append(
                    [
                        person.full_name or person.name_raw,
                        company,
                        members,
                        website,
                        person.title,
                        person.email,
                    ]
                )
            continue
        if result and (result.revealed_name or result.email):
            rows.append(
                [
                    result.revealed_name or "",
                    company,
                    members,
                    website,
                    "",
                    result.email or "",
                ]
            )
            continue
        rows.append(["", company, members, website, "", ""])
    return rows


def _add_employees_sheet(
    wb: Workbook,
    input_wb: InputWorkbook,
    by_domain: dict[str, CompanyResult],
    by_wfid: dict[str, CompanyResult],
) -> None:
    name = "employees"
    if name in wb.sheetnames:
        name = "employee_list"
    ws = wb.create_sheet(name, 0)
    headers = [
        "Employee Name",
        "Company Name",
        "Company Members",
        "Website",
        "Job Title",
        "Email",
    ]
    rows = _employee_rows(input_wb, by_domain, by_wfid)
    ws.append(headers)
    for values in rows:
        ws.append(values)

    _style_header(ws, len(headers))
    _fit_columns(ws, headers, rows)
    _apply_data_style(ws, headers, len(rows) + 1)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
    ws.freeze_panes = "A2"
    ws.sheet_properties.tabColor = "2E7D32"
    ws.sheet_view.showGridLines = False

    for row_idx, values in enumerate(rows, start=2):
        ws.cell(row=row_idx, column=3).alignment = Alignment(
            horizontal="center", vertical="top", wrap_text=True
        )
        title_cell = ws.cell(row=row_idx, column=5)
        title_cell.alignment = Alignment(vertical="top", wrap_text=True)
        if title_cell.value:
            ws.row_dimensions[row_idx].height = _wrap_height(str(title_cell.value), 38)
        website_cell = ws.cell(row=row_idx, column=4)
        if isinstance(website_cell.value, str) and website_cell.value.startswith(
            ("http://", "https://")
        ):
            website_cell.hyperlink = website_cell.value
            website_cell.font = LINK_FONT
        email_cell = ws.cell(row=row_idx, column=6)
        email_cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
        if email_cell.value:
            email_cell.hyperlink = f"mailto:{email_cell.value}"
            email_cell.font = LINK_FONT
    wb.active = 0


def write_output(
    input_wb: InputWorkbook,
    results: list[CompanyResult],
    output_path: Path,
    statuses: dict[str, dict[str, Any]] | None = None,
) -> Path:
    statuses = statuses or {}
    by_domain = {result.domain: result for result in results}
    by_wfid = {result.wellfound_company_id: result for result in results if result.wellfound_company_id}

    headers = list(input_wb.headers)
    canonical_headers = [_canonical_header(header) for header in headers]
    for added in ADDED_COLUMNS:
        if added not in canonical_headers:
            headers.append(added)
            canonical_headers.append(added)
    index = {}
    for i, key in enumerate(canonical_headers):
        index.setdefault(key, i)

    wb = Workbook()
    ws = wb.active
    ws.title = _safe_sheet_title(input_wb.sheet_name)
    ws.append(headers)

    main_rows: list[list[Any]] = []
    for row in input_wb.rows:
        values = list(row.values) + [None] * max(0, len(headers) - len(row.values))
        result = _result_for_row(row, by_domain, by_wfid)
        email = ""
        source = ""
        notes = ""
        if result is None:
            notes = _unprocessed_note(row, statuses)
        elif result.dry_run:
            email = ""
            source = ""
            notes = result.notes
        else:
            email = result.email or ""
            source = result.email_source if email else ""
            notes = result.notes
        values[index["email"]] = email
        values[index["email_source"]] = source
        values[index["notes"]] = notes
        if result is not None:
            niche_idx = index.get("niche")
            team_idx = index.get("team_size")
            if (
                niche_idx is not None
                and result.facts.niche
                and str(values[niche_idx] or "").strip() == ""
            ):
                values[niche_idx] = result.facts.niche
            if (
                team_idx is not None
                and result.facts.employees
                and str(values[team_idx] or "").strip() == ""
            ):
                values[team_idx] = result.facts.employees
        main_rows.append(values)

    for values in main_rows:
        ws.append(values)

    _style_header(ws, len(headers))
    _fit_columns(ws, headers, main_rows)
    _apply_data_style(ws, headers, len(main_rows) + 1)

    notes_idx = index["notes"] + 1
    notes_width = int(ws.column_dimensions[get_column_letter(notes_idx)].width or 82)
    for row_idx, values in enumerate(main_rows, start=2):
        notes = str(values[index["notes"]] or "")
        if notes:
            ws.row_dimensions[row_idx].height = _wrap_height(notes, notes_width)
        for header in ("email", "email_source"):
            cell = ws.cell(row=row_idx, column=index[header] + 1)
            cell.alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
        source_cell = ws.cell(row=row_idx, column=index["email_source"] + 1)
        if source_cell.value == "verified":
            source_cell.fill = VERIFIED_FILL
            source_cell.font = Font(name="Calibri", size=11, bold=True, color="1E6B45")
        elif source_cell.value == "guessed":
            source_cell.fill = GUESSED_FILL
            source_cell.font = Font(name="Calibri", size=11, bold=True, color="8A5A00")

        email_cell = ws.cell(row=row_idx, column=index["email"] + 1)
        if email_cell.value:
            email_cell.hyperlink = f"mailto:{email_cell.value}"
            email_cell.font = LINK_FONT

        url_header = "wellfound_profile_url"
        if url_header in index:
            url_cell = ws.cell(row=row_idx, column=index[url_header] + 1)
            if isinstance(url_cell.value, str) and url_cell.value.startswith(("http://", "https://")):
                url_cell.hyperlink = url_cell.value
                url_cell.font = LINK_FONT

        team_idx = index.get("team_size")
        if team_idx is not None:
            ws.cell(row=row_idx, column=team_idx + 1).alignment = Alignment(
                horizontal="center", vertical="top"
            )
        date_idx = index.get("date_scraped")
        if date_idx is not None:
            cell = ws.cell(row=row_idx, column=date_idx + 1)
            fmt = input_wb.number_formats.get(input_wb.headers[date_idx], "General")
            if fmt and fmt != "General":
                cell.number_format = fmt
            cell.alignment = Alignment(horizontal="center", vertical="top")
        id_idx = index.get("wellfound_company_id")
        if id_idx is not None:
            cell = ws.cell(row=row_idx, column=id_idx + 1)
            if isinstance(cell.value, (int, float)):
                cell.number_format = "0"
            cell.alignment = Alignment(horizontal="center", vertical="top")

    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(main_rows) + 1}"
    ws.sheet_properties.tabColor = "1F3864"

    # ------------------------------------------------------------ team sheet
    team = wb.create_sheet("team_emails")
    team_headers = [
        "company",
        "domain",
        "full_name",
        "title",
        "email",
        "source",
        "syntax/mx validation",
        "confidence",
    ]
    team.append(team_headers)

    team_rows: list[list[Any]] = []
    seen_domains: set[str] = set()
    ordered_domains: list[str] = []
    for row in input_wb.rows:
        if row.domain and row.domain not in seen_domains:
            seen_domains.add(row.domain)
            ordered_domains.append(row.domain)
    for domain in ordered_domains:
        input_row = next((row for row in input_wb.rows if row.domain == domain), None)
        result = _result_for_row(input_row, by_domain, by_wfid) if input_row else None
        if not result or not result.people:
            continue
        company = result.company_name or (input_row.company_name if input_row else domain)
        for person in result.people:
            team_rows.append(
                [
                    company,
                    domain,
                    person.full_name or person.name_raw,
                    person.title,
                    person.email,
                    person.source,
                    person.validation,
                    person.confidence,
                ]
            )

    for values in team_rows:
        team.append(values)

    _style_header(team, len(team_headers))
    _fit_columns(team, team_headers, team_rows)
    _apply_data_style(team, team_headers, len(team_rows) + 1)
    team.auto_filter.ref = f"A1:{get_column_letter(len(team_headers))}{len(team_rows) + 1}"
    team.sheet_properties.tabColor = "2E7D32"

    for row_idx in range(2, len(team_rows) + 2):
        source_cell = team.cell(row=row_idx, column=6)
        validation_cell = team.cell(row=row_idx, column=7)
        confidence_cell = team.cell(row=row_idx, column=8)
        email_cell = team.cell(row=row_idx, column=5)
        if source_cell.value == "verified":
            source_cell.fill = VERIFIED_FILL
            source_cell.font = Font(name="Calibri", size=11, bold=True, color="1E6B45")
        elif source_cell.value == "guessed":
            source_cell.fill = GUESSED_FILL
            source_cell.font = Font(name="Calibri", size=11, bold=True, color="8A5A00")
        if confidence_cell.value == "low" or (
            isinstance(validation_cell.value, str)
            and any(
                marker in validation_cell.value
                for marker in ("no_mx", "failed", "invalid", "not_found")
            )
        ):
            validation_cell.fill = LOW_FILL
            confidence_cell.fill = LOW_FILL
        elif isinstance(validation_cell.value, str) and validation_cell.value.startswith("skipped"):
            validation_cell.fill = SKIP_FILL
            confidence_cell.fill = SKIP_FILL
        if email_cell.value:
            email_cell.hyperlink = f"mailto:{email_cell.value}"
            email_cell.font = LINK_FONT
        for col in (5, 6, 8):
            team.cell(row=row_idx, column=col).alignment = Alignment(
                horizontal="center", vertical="top", wrap_text=True
            )
        team.row_dimensions[row_idx].height = _wrap_height(
            str(validation_cell.value or ""), 34
        )

    _add_employees_sheet(wb, input_wb, by_domain, by_wfid)

    wb.properties.title = "Apollo email enrichment"
    wb.properties.creator = "Apollo Email Finder"
    wb.properties.description = "Generated locally. Verified email revealed via Apollo UI; guesses are pattern-derived."

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.stem + ".tmp" + output_path.suffix)
    wb.save(temp_path)
    try:
        os.replace(temp_path, output_path)
    except PermissionError:
        alternate = output_path.with_name(
            output_path.stem + f"_{datetime.now().strftime('%H%M%S')}" + output_path.suffix
        )
        os.replace(temp_path, alternate)
        return alternate
    return output_path
