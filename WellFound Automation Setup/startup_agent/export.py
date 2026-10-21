"""Timestamped CSV, JSON and dependency-free XLSX exports."""

from __future__ import annotations

import csv
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape

from .config import Config
from .store import Store

EXPORT_COLUMNS = (
    "record_id",
    "name",
    "website",
    "platform",
    "employee_count",
    "employee_min",
    "employee_max",
    "size_status",
    "size_match",
    "industry",
    "niche",
    "description",
    "wellfound_url",
    "job_urls",
    "location",
    "source_name",
    "source_url",
    "source_id",
    "discovered_at",
    "last_checked_at",
    "status",
    "validation_status",
    "completeness",
    "duplicate_of",
    "notes",
    "website_status",
    "website_evidence",
    "website_checked_at",
    "website_final_url",
    "member_count_exact",
    "lead_status",
    "retry_count",
    "last_run_id",
    "discovered_run_id",
)

_NUMERIC_COLUMNS = {"employee_min", "employee_max", "completeness", "member_count_exact"}

#: Entities escaped inside XML attributes.
_XML_ATTR_ENTITIES = {'"': "&quot;"}

#: The compact sheet: exactly what a startup shortlist needs.
SHEET_COLUMNS = ("name", "website", "platform", "members")
SHEET_HEADERS = {
    "name": "Startup Name",
    "website": "Website",
    "platform": "Platform",
    "members": "Members",
}

#: The consistent report schema used by Excel, CSV and JSON.
REPORT_COLUMNS = (
    "name",
    "website",
    "employee_range",
    "member_count_exact",
    "wellfound_url",
    "website_status",
    "discovered_at",
    "last_checked_at",
    "website_evidence",
    "lead_status",
)
REPORT_HEADERS = {
    "name": "Startup Name",
    "website": "Official Website",
    "employee_range": "Employee Range",
    "member_count_exact": "Exact Member Count",
    "wellfound_url": "Wellfound Profile URL",
    "website_status": "Website Verification Status",
    "discovered_at": "Discovery Date",
    "last_checked_at": "Last Checked",
    "website_evidence": "Source of Website Evidence",
    "lead_status": "Processing Status",
}
REPORT_WIDTHS = {
    "name": 34,
    "website": 34,
    "employee_range": 16,
    "member_count_exact": 16,
    "wellfound_url": 44,
    "website_status": 22,
    "discovered_at": 22,
    "last_checked_at": 22,
    "website_evidence": 46,
    "lead_status": 18,
}
REPORT_LINK_COLUMNS = ("website", "wellfound_url")

#: Run history / statistics report.
RUN_COLUMNS = (
    "job_id",
    "started_at",
    "finished_at",
    "status",
    "mode",
    "inspected",
    "eligible",
    "rejected",
    "duplicates_skipped",
    "websites_verified",
    "incomplete",
    "errors",
    "retries",
    "new_verified",
    "note",
)
RUN_HEADERS = {
    "job_id": "Run ID",
    "started_at": "Started",
    "finished_at": "Finished",
    "status": "Status",
    "mode": "Mode",
    "inspected": "Inspected",
    "eligible": "Eligible",
    "rejected": "Rejected by size",
    "duplicates_skipped": "Duplicates skipped",
    "websites_verified": "Websites verified",
    "incomplete": "Incomplete",
    "errors": "Errors",
    "retries": "Retries",
    "new_verified": "New verified leads",
    "note": "Note",
}
RUN_WIDTHS = {
    "job_id": 26,
    "started_at": 22,
    "finished_at": 22,
    "status": 14,
    "mode": 26,
    "inspected": 11,
    "eligible": 11,
    "rejected": 16,
    "duplicates_skipped": 18,
    "websites_verified": 18,
    "incomplete": 12,
    "errors": 10,
    "retries": 10,
    "new_verified": 18,
    "note": 50,
}

#: Report scopes offered in the Telegram download menu.
REPORT_SCOPES = ("latest", "verified", "incomplete", "runs", "all")
REPORT_SCOPE_LABELS = {
    "latest": "New verified leads (latest run)",
    "verified": "All verified leads (master)",
    "incomplete": "Incomplete leads",
    "runs": "Run history and statistics",
    "all": "All data so far",
}


class EmptyReportError(ValueError):
    """The report would be empty - nothing is written."""


@dataclass(slots=True)
class ExportResult:
    kind: str
    path: Path
    row_count: int
    scope: str = ""
    run_id: str = ""


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _column_letter(index: int) -> str:
    """1-based column index -> A, B, ... AA."""
    result = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _xml_text(value: Any) -> str:
    text = _cell_text(value)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return escape(text, {'"': "&quot;"})


def _cell_xml(ref: str, value: Any, *, style: int = 0, numeric: bool = False) -> str:
    if value is None or value == "":
        return f'<c r="{ref}" s="{style}"/>' if style else ""
    if numeric:
        try:
            number = int(value)
        except (TypeError, ValueError):
            numeric = False
        else:
            return f'<c r="{ref}" s="{style}"><v>{number}</v></c>'
    # Inline strings are not formulas, so the original text is preserved
    # exactly (including a leading "=", "+", "-" or "@").
    text = _cell_text(value)
    return (
        f'<c r="{ref}" s="{style}" t="inlineStr"><is>'
        f'<t xml:space="preserve">{_xml_text(text)}</t></is></c>'
    )


def _xlsx_bytes(
    rows: list[dict[str, Any]],
    columns: tuple[str, ...] | None = None,
    header_map: dict[str, str] | None = None,
    *,
    sheet_name: str = "Companies",
    widths: dict[str, float] | None = None,
    link_columns: tuple[str, ...] = (),
) -> bytes:
    """Build a valid OOXML workbook with inline strings.

    The sheet has a frozen header row, an auto-filter, readable column widths
    and clickable hyperlinks for the URL columns.
    """
    headers = list(columns or EXPORT_COLUMNS)
    labels = [(header_map or {}).get(column, column) for column in headers]
    sheet_rows: list[str] = []
    header_cells = "".join(
        _cell_xml(f"{_column_letter(index)}1", label, style=1)
        for index, label in enumerate(labels, start=1)
    )
    sheet_rows.append(f'<row r="1">{header_cells}</row>')
    hyperlinks: list[tuple[str, str]] = []
    for row_index, row in enumerate(rows, start=2):
        cells = []
        for col_index, column in enumerate(headers, start=1):
            value = row.get(column, "")
            letter = _column_letter(col_index)
            ref = f"{letter}{row_index}"
            text = _cell_text(value)
            is_link = column in link_columns and text.lower().startswith(
                ("http://", "https://")
            )
            if is_link:
                hyperlinks.append((ref, text))
                cells.append(_cell_xml(ref, text, style=2))
                continue
            cells.append(
                _cell_xml(
                    ref,
                    value,
                    style=0,
                    numeric=column in _NUMERIC_COLUMNS,
                )
            )
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    last_col = _column_letter(len(headers))
    last_row = max(1, len(rows) + 1)
    cols_xml = ""
    if widths:
        entries = []
        for index, column in enumerate(headers, start=1):
            width = widths.get(column)
            if not width:
                continue
            entries.append(
                f'<col min="{index}" max="{index}" width="{float(width):.1f}" customWidth="1"/>'
            )
        if entries:
            cols_xml = "<cols>" + "".join(entries) + "</cols>"
    links_xml = ""
    rels_xml = ""
    if hyperlinks:
        rels = []
        links = []
        for index, (ref, url) in enumerate(hyperlinks, start=1):
            rel_id = f"rId{index}"
            rels.append(
                f'<Relationship Id="{rel_id}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/hyperlink" '
                f'Target="{escape(url, _XML_ATTR_ENTITIES)}" TargetMode="External"/>'
            )
            links.append(f'<hyperlink ref="{ref}" r:id="{rel_id}"/>')
        links_xml = "<hyperlinks>" + "".join(links) + "</hyperlinks>"
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships">' + "".join(rels) + "</Relationships>"
        )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="A1:{last_col}{last_row}"/>'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"{cols_xml}"
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        f'<autoFilter ref="A1:{last_col}{last_row}"/>'
        f"{links_xml}"
        "</worksheet>"
    )

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        '<font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFEFF3F8"/>'
        '<bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )

    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )

    safe_sheet_name = re.sub(r"[\\/*?:\[\]]", "-", str(sheet_name))[:31] or "Sheet1"
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(safe_sheet_name, _XML_ATTR_ENTITIES)}" '
        'sheetId="1" r:id="rId1"/></sheets></workbook>'
    )

    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/></Relationships>'
    )

    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        if rels_xml:
            archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", rels_xml)
    data = buffer.getvalue()
    # Fail fast if the archive is malformed.
    with zipfile.ZipFile(io.BytesIO(data)) as check:
        if check.testzip() is not None:
            raise RuntimeError("generated XLSX failed ZIP integrity check")
    return data


class ExportService:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store

    def _unique_path(self, kind: str) -> Path:
        self.cfg.export_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = {"csv": ".csv", "json": ".json", "xlsx": ".xlsx"}[kind]
        candidate = self.cfg.export_dir / f"startups-{stamp}{suffix}"
        counter = 1
        while candidate.exists():
            candidate = self.cfg.export_dir / f"startups-{stamp}-{counter}{suffix}"
            counter += 1
        return candidate

    def _unique_sheet_path(self, kind: str, platform: str) -> Path:
        self.cfg.export_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = {"csv": ".csv", "xlsx": ".xlsx"}[kind]
        label = "-".join(part for part in (platform.lower(), "sheet") if part) or "sheet"
        candidate = self.cfg.export_dir / f"{label}-{stamp}{suffix}"
        counter = 1
        while candidate.exists():
            candidate = self.cfg.export_dir / f"{label}-{stamp}-{counter}{suffix}"
            counter += 1
        return candidate

    def _rows(self, *, confirmed_only: bool = False) -> list[dict[str, Any]]:
        limit = self.cfg.max_export_rows or max(1, self.store.counts()["total"])
        companies = self.store.list_companies(
            limit=limit, offset=0, only_confirmed=confirmed_only
        )
        return [company.to_export_row() for company in companies]

    def sheet_rows(self, *, platform: str = "") -> list[dict[str, Any]]:
        """The compact sheet: startup name, website, platform and members.

        Only companies whose published size is fully inside the target range are
        included, so 11-50 style headcounts never reach the sheet.
        """
        limit = self.cfg.max_export_rows or max(1, self.store.counts()["total"])
        wanted = platform.strip().lower()
        rows: list[dict[str, Any]] = []
        for company in self.store.list_companies(
            limit=limit, offset=0, only_confirmed=True
        ):
            if wanted and company.platform.lower() != wanted:
                continue
            rows.append(
                {
                    "name": company.name,
                    "website": company.website,
                    "platform": company.platform,
                    "members": company.employee_count or "Unknown",
                }
            )
        return rows

    def export_sheet(self, kind: str, *, platform: str = "") -> ExportResult:
        """Write the compact startup sheet (CSV or XLSX)."""
        kind = kind.lower()
        if kind not in {"csv", "xlsx"}:
            raise ValueError(f"unsupported sheet format: {kind}")
        rows = self.sheet_rows(platform=platform)
        path = self._unique_sheet_path(kind, platform)
        if kind == "csv":
            self._write_sheet_csv(path, rows)
        else:
            path.write_bytes(
                _xlsx_bytes(rows, columns=SHEET_COLUMNS, header_map=SHEET_HEADERS)
            )
        self.store.add_export(f"sheet-{kind}", str(path), len(rows))
        return ExportResult(kind=kind, path=path, row_count=len(rows))

    def export(
        self, kind: str, *, confirmed_only: bool = False
    ) -> ExportResult:
        kind = kind.lower()
        if kind not in {"csv", "json", "xlsx"}:
            raise ValueError(f"unsupported export format: {kind}")
        rows = self._rows(confirmed_only=confirmed_only)
        path = self._unique_path(kind)
        if kind == "csv":
            self._write_csv(path, rows)
        elif kind == "json":
            self._write_json(path, rows)
        else:
            path.write_bytes(_xlsx_bytes(rows))
        self.store.add_export(kind, str(path), len(rows))
        return ExportResult(kind=kind, path=path, row_count=len(rows))

    def export_all(self, *, confirmed_only: bool = False) -> list[ExportResult]:
        return [self.export(kind, confirmed_only=confirmed_only) for kind in ("csv", "xlsx", "json")]

    def _write_csv(self, path: Path, rows: Iterable[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=list(EXPORT_COLUMNS),
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({column: _cell_text(row.get(column, "")) for column in EXPORT_COLUMNS})

    def _write_sheet_csv(self, path: Path, rows: Iterable[dict[str, Any]]) -> None:
        """Write the compact sheet with readable headers."""
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[SHEET_HEADERS.get(column, column) for column in SHEET_COLUMNS],
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {SHEET_HEADERS.get(column, column): _cell_text(row.get(column, ""))
                     for column in SHEET_COLUMNS}
                )

    def _write_json(self, path: Path, rows: list[dict[str, Any]]) -> None:
        companies = []
        for row in rows:
            item = dict(row)
            # JSON export should expose job URLs as a real array.
            try:
                parsed = json.loads(item.get("job_urls") or "[]")
                item["job_urls"] = parsed if isinstance(parsed, list) else []
            except (TypeError, ValueError):
                item["job_urls"] = []
            item["size_match"] = item.get("size_match") == "yes"
            companies.append(item)
        payload = {
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "target_size": self.cfg.target_size_label,
            "count": len(companies),
            "companies": companies,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Reports (the Telegram "Download reports" menu)
    # ------------------------------------------------------------------
    def _unique_report_path(self, scope: str, kind: str) -> Path:
        self.cfg.export_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = {"csv": ".csv", "json": ".json", "xlsx": ".xlsx", "txt": ".txt"}[kind]
        label = {
            "latest": "latest-run",
            "verified": "verified",
            "incomplete": "incomplete",
            "runs": "run-history",
            "summary": "run-summary",
            "all": "all-data",
        }.get(scope, scope)
        candidate = self.cfg.export_dir / f"{label}-{stamp}{suffix}"
        counter = 1
        while candidate.exists():
            candidate = self.cfg.export_dir / f"{label}-{stamp}-{counter}{suffix}"
            counter += 1
        return candidate

    @staticmethod
    def _run_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(row.get("job_id") or ""),
            "started_at": str(row.get("started_at") or ""),
            "finished_at": str(row.get("finished_at") or ""),
            "status": str(row.get("status") or ""),
            "mode": str(row.get("mode") or ""),
            "inspected": int(row.get("inspected") or 0),
            "eligible": int(row.get("eligible") or 0),
            "rejected": int(row.get("rejected") or 0),
            "duplicates_skipped": int(row.get("duplicates_skipped") or 0),
            "websites_verified": int(row.get("websites_verified") or 0),
            "incomplete": int(row.get("incomplete") or 0),
            "errors": int(row.get("errors") or 0),
            "retries": int(row.get("retries") or 0),
            "new_verified": int(row.get("new_verified") or 0),
            "note": str(row.get("note") or "")[:500],
        }

    def report_rows(self, scope: str, run_id: str = "") -> list[dict[str, Any]]:
        """Rows for one report scope (the shared schema across formats)."""
        scope = (scope or "").strip().lower()
        if scope == "latest":
            job = self.store.latest_scan_job() if not run_id else self.store.get_scan_job(run_id)
            job_id = str((job or {}).get("job_id") or "")
            if not job_id:
                return []
            return [
                company.to_report_row()
                for company in self.store.companies_for_run(job_id, "verified")
            ]
        if scope == "verified":
            return [company.to_report_row() for company in self.store.verified_companies()]
        if scope == "incomplete":
            return [
                company.to_report_row()
                for company in self.store.incomplete_report_companies()
            ]
        if scope == "all":
            # One useful file with everything collected so far: verified
            # leads plus the leads that still need a website check.
            companies = {
                company.record_id: company
                for company in (
                    self.store.verified_companies()
                    + self.store.incomplete_report_companies()
                )
            }
            return [
                company.to_report_row()
                for company in sorted(
                    companies.values(), key=lambda item: item.name.casefold()
                )
            ]
        if scope == "runs":
            return [
                self._run_row(row) for row in self.store.list_scan_jobs(limit=200)
            ]
        raise ValueError(f"unknown report scope: {scope}")

    def export_report(
        self, scope: str, kind: str = "xlsx", run_id: str = ""
    ) -> ExportResult:
        """Write one report.  An empty result raises instead of writing a file."""
        kind = (kind or "").strip().lower()
        if kind not in {"csv", "json", "xlsx", "txt"}:
            raise ValueError(f"unsupported report format: {kind}")
        scope = (scope or "").strip().lower()
        if scope not in REPORT_SCOPES:
            raise ValueError(f"unknown report scope: {scope}")

        if kind == "txt" and scope == "latest":
            job = self.store.latest_scan_job() if not run_id else self.store.get_scan_job(run_id)
            job_id = str((job or {}).get("job_id") or "")
            if not job_id:
                raise EmptyReportError("There is no run to summarise yet.")
            return self.export_run_summary(job_id)

        rows = self.report_rows(scope, run_id)
        if not rows:
            raise EmptyReportError(
                f"There is nothing to export for '{REPORT_SCOPE_LABELS.get(scope, scope)}'."
            )
        path = self._unique_report_path(scope, kind)
        if scope == "runs":
            columns, headers, widths = RUN_COLUMNS, RUN_HEADERS, RUN_WIDTHS
            sheet = "Runs"
        else:
            columns, headers, widths = REPORT_COLUMNS, REPORT_HEADERS, REPORT_WIDTHS
            sheet = {
                "latest": "Latest run",
                "verified": "Verified",
                "incomplete": "Incomplete",
                "all": "All data",
            }.get(scope, "Report")
        if kind == "csv":
            self._write_report_csv(path, rows, columns, headers)
        elif kind == "json":
            self._write_report_json(path, rows, scope, columns, headers)
        elif kind == "xlsx":
            path.write_bytes(
                _xlsx_bytes(
                    rows,
                    columns=columns,
                    header_map=headers,
                    sheet_name=sheet,
                    widths=widths,
                    link_columns=REPORT_LINK_COLUMNS if scope != "runs" else (),
                )
            )
        else:
            path.write_text(
                self._plain_table(rows, columns, headers), encoding="utf-8"
            )
        self.store.add_export(f"report-{scope}-{kind}", str(path), len(rows))
        return ExportResult(
            kind=kind, path=path, row_count=len(rows), scope=scope, run_id=run_id
        )

    def _write_report_csv(
        self,
        path: Path,
        rows: list[dict[str, Any]],
        columns: tuple[str, ...],
        headers: dict[str, str],
    ) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[headers.get(column, column) for column in columns],
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        headers.get(column, column): _cell_text(row.get(column, ""))
                        for column in columns
                    }
                )

    def _write_report_json(
        self,
        path: Path,
        rows: list[dict[str, Any]],
        scope: str,
        columns: tuple[str, ...],
        headers: dict[str, str],
    ) -> None:
        records = []
        for row in rows:
            record = {}
            for column in columns:
                value = row.get(column, "")
                record[headers.get(column, column)] = value
            records.append(record)
        payload = {
            "report": scope,
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "target_size": self.cfg.target_size_label,
            "count": len(records),
            "records": records,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _plain_table(
        rows: list[dict[str, Any]],
        columns: tuple[str, ...],
        headers: dict[str, str],
    ) -> str:
        lines: list[str] = []
        for row in rows:
            parts = []
            for column in columns:
                value = _cell_text(row.get(column, ""))
                if column in {"website", "wellfound_url"} and value:
                    value = value.split("://", 1)[-1].split("/", 1)[0]
                parts.append(f"{headers.get(column, column)}: {value}")
            lines.append(" | ".join(parts))
        return "\n".join(lines) + ("\n" if lines else "")

    # ------------------------------------------------------------------
    # Run summary
    # ------------------------------------------------------------------
    def run_summary_text(self, job_id: str) -> str:
        """A compact, human readable summary of one run."""
        job = self.store.get_scan_job(job_id)
        if job is None:
            raise EmptyReportError(f"Run {job_id} was not found.")
        counts = self.store.scan_item_counts(job_id)
        items = self.store.scan_items(job_id)
        verified = [item for item in items if item.get("status") == "done"]
        lines = [
            f"Run {job.get('job_id')}",
            f"Mode: {job.get('mode')}",
            f"Status: {job.get('status')}",
            f"Started: {job.get('started_at')}",
            f"Finished: {job.get('finished_at') or '(still running)'}",
            f"Target size: {self.cfg.target_size_label} employees",
            "",
            "Counts",
            f"  Profiles discovered : {int(job.get('discovered') or 0)}",
            f"  Profiles inspected  : {int(job.get('inspected') or 0)}",
            f"  Eligible (1-{self.cfg.target_size_max})   : {int(job.get('eligible') or 0)}",
            f"  Rejected by size    : {int(job.get('rejected') or 0)}",
            f"  Duplicates skipped  : {int(job.get('duplicates_skipped') or 0)}",
            f"  Websites verified   : {int(job.get('websites_verified') or 0)}",
            f"  Incomplete          : {int(job.get('incomplete') or 0)}",
            f"  Errors              : {int(job.get('errors') or 0)}",
            f"  Retries             : {int(job.get('retries') or 0)}",
            f"  New verified leads  : {int(job.get('new_verified') or 0)}",
            f"  Records written     : {int(job.get('inserted') or 0)} new / "
            f"{int(job.get('updated') or 0)} updated",
            f"  Queue               : {counts.get('done', 0)} done, "
            f"{counts.get('rejected', 0)} rejected, {counts.get('failed', 0)} failed, "
            f"{counts.get('pending', 0)} pending",
        ]
        if job.get("note"):
            lines.extend(["", f"Note: {job['note']}"])
        if job.get("error"):
            lines.extend(["", f"Error: {job['error']}"])
        if verified:
            lines.extend(["", "Verified leads in this run"])
            for item in verified[:100]:
                name = item.get("name") or "(unnamed)"
                website = item.get("website") or "(no website)"
                lines.append(f"  - {name} · {website} · {item.get('employee_count') or '?'}")
            if len(verified) > 100:
                lines.append(f"  ... and {len(verified) - 100} more")
        return "\n".join(lines) + "\n"

    def export_run_summary(self, job_id: str) -> ExportResult:
        """Write the plain-text summary of one run."""
        text = self.run_summary_text(job_id)
        path = self._unique_report_path("summary", "txt")
        path.write_text(text, encoding="utf-8")
        self.store.add_export("report-summary-txt", str(path), 1)
        return ExportResult(
            kind="txt", path=path, row_count=1, scope="summary", run_id=job_id
        )

    # ------------------------------------------------------------------
    # Telegram size limits
    # ------------------------------------------------------------------
    def split_csv(self, path: Path, max_bytes: int = 40 * 1024 * 1024) -> list[Path]:
        """Split a large CSV into parts that fit Telegram's upload limit."""
        path = Path(path)
        if path.stat().st_size <= max_bytes:
            return [path]
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                return [path]
            parts: list[Path] = []
            index = 0
            current: list[list[str]] = []
            size = 0
            for row in reader:
                width = sum(len(cell) for cell in row) + len(row)
                if current and size + width > max_bytes:
                    index += 1
                    part = path.with_name(f"{path.stem}-part{index}{path.suffix}")
                    self._write_csv_part(part, header, current)
                    parts.append(part)
                    current = []
                    size = 0
                current.append(row)
                size += width
            part = path.with_name(f"{path.stem}-part1{path.suffix}")
            self._write_csv_part(part, header, current)
            parts.append(part)
        return parts

    @staticmethod
    def _write_csv_part(
        path: Path, header: list[str], rows: list[list[str]]
    ) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)

