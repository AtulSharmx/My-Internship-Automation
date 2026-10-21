"""Data models for the startup discovery pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


class CompanyStatus:
    """Lifecycle/quality state of a discovered company record."""

    DISCOVERED = "discovered"
    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    DUPLICATE = "duplicate"
    FAILED = "failed"

    ALL = (DISCOVERED, VALIDATED, NEEDS_REVIEW, DUPLICATE, FAILED)
    LABELS = {
        DISCOVERED: "Discovered",
        VALIDATED: "Validated",
        NEEDS_REVIEW: "Needs review",
        DUPLICATE: "Possible duplicate",
        FAILED: "Failed to process",
    }
    ICONS = {
        DISCOVERED: "🆕",
        VALIDATED: "✅",
        NEEDS_REVIEW: "🕵️",
        DUPLICATE: "♻️",
        FAILED: "❌",
    }


class WebsiteStatus:
    """Independent reachability/ownership check of a company website."""

    NOT_CHECKED = "not_checked"
    VERIFIED = "verified"
    REDIRECTED = "redirected"
    UNREACHABLE = "unreachable"
    MISSING = "missing"
    BLOCKED = "blocked"

    ALL = (NOT_CHECKED, VERIFIED, REDIRECTED, UNREACHABLE, MISSING, BLOCKED)
    LABELS = {
        NOT_CHECKED: "Not checked",
        VERIFIED: "Verified",
        REDIRECTED: "Redirects elsewhere",
        UNREACHABLE: "Unreachable",
        MISSING: "No website listed",
        BLOCKED: "Blocked / not an official site",
    }
    ICONS = {
        NOT_CHECKED: "❔",
        VERIFIED: "✅",
        REDIRECTED: "↪️",
        UNREACHABLE: "⚠️",
        MISSING: "🚫",
        BLOCKED: "⛔",
    }


class LeadStatus:
    """The per-lead processing state requested by the control panel."""

    DISCOVERED = "discovered"
    PROCESSING = "processing"
    VERIFIED = "verified"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    SKIPPED_DUPLICATE = "skipped_duplicate"

    ALL = (DISCOVERED, PROCESSING, VERIFIED, INCOMPLETE, FAILED, SKIPPED_DUPLICATE)
    LABELS = {
        DISCOVERED: "Discovered",
        PROCESSING: "Processing",
        VERIFIED: "Verified",
        INCOMPLETE: "Incomplete",
        FAILED: "Failed",
        SKIPPED_DUPLICATE: "Skipped duplicate",
    }
    ICONS = {
        DISCOVERED: "🆕",
        PROCESSING: "⏳",
        VERIFIED: "✅",
        INCOMPLETE: "🕵️",
        FAILED: "❌",
        SKIPPED_DUPLICATE: "♻️",
    }


def derive_lead_status(company: "Company") -> str:
    """Compute the control-panel state from the record's evidence.

    Only a confirmed 1-10 size *and* a verified official website is
    ``verified``.  A confirmed lead without that evidence is ``incomplete`` so
    it can be retried; it never appears in a verified export.
    """
    if company.duplicate_of or company.status == CompanyStatus.DUPLICATE:
        return LeadStatus.SKIPPED_DUPLICATE
    if company.status == CompanyStatus.FAILED:
        return LeadStatus.FAILED
    if company.size_match and company.website_status == WebsiteStatus.VERIFIED:
        return LeadStatus.VERIFIED
    if company.size_match:
        return LeadStatus.INCOMPLETE
    return LeadStatus.DISCOVERED


class SizeStatus:
    """How confidently a company's head-count fits the target range."""

    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    OUTSIDE = "outside"
    UNKNOWN = "unknown"

    ALL = (CONFIRMED, PARTIAL, OUTSIDE, UNKNOWN)
    LABELS = {
        CONFIRMED: "Confirmed in range",
        PARTIAL: "Partly overlaps range",
        OUTSIDE: "Outside range",
        UNKNOWN: "Unknown",
    }
    ICONS = {
        CONFIRMED: "🎯",
        PARTIAL: "〰️",
        OUTSIDE: "🏢",
        UNKNOWN: "❔",
    }


#: Human name of the platform a record came from.
PLATFORM_NAMES = {
    "wellfound": "Wellfound",
    "wellfound_saved": "Wellfound",
    "wellfound_public": "Wellfound",
    "angel": "AngelList",
    "angel_saved": "AngelList",
    "dbpedia": "DBpedia",
    "wikipedia": "Wikipedia",
    "web": "Web",
    "manual": "Manual import",
    "file": "Manual import",
}


@dataclass(frozen=True)
class SizeInfo:
    """A parsed employee-size value.

    ``minimum``/``maximum`` are ``None`` when the source did not say.  A single
    number is represented by equal minimum and maximum.  ``10+`` has a minimum
    but no maximum.
    """

    raw: str = ""
    minimum: int | None = None
    maximum: int | None = None
    label: str = ""

    @property
    def known(self) -> bool:
        return self.minimum is not None or self.maximum is not None


@dataclass(slots=True)
class Company:
    """One normalized company/startup record.

    The record is deliberately source-agnostic.  ``source_name``/``source_url``
    describe where the first/primary copy came from, while
    ``company_sources`` in the database retains every source seen for the
    record.
    """

    record_id: str
    name: str
    website: str = ""
    website_key: str = ""
    employee_count: str = ""
    employee_min: int | None = None
    employee_max: int | None = None
    size_status: str = SizeStatus.UNKNOWN
    size_match: bool = False
    industry: str = ""
    niche: str = ""
    description: str = ""
    wellfound_url: str = ""
    job_urls: list[str] = field(default_factory=list)
    location: str = ""
    source_url: str = ""
    source_name: str = ""
    source_id: str = ""
    discovered_at: str = field(default_factory=iso_now)
    last_checked_at: str = field(default_factory=iso_now)
    status: str = CompanyStatus.DISCOVERED
    validation_status: str = ""
    completeness: int = 0
    duplicate_of: str = ""
    notes: str = ""
    name_key: str = ""
    website_status: str = WebsiteStatus.NOT_CHECKED
    website_evidence: str = ""
    website_checked_at: str = ""
    website_final_url: str = ""
    member_count_exact: int | None = None
    lead_status: str = LeadStatus.DISCOVERED
    retry_count: int = 0
    last_run_id: str = ""
    discovered_run_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def platform(self) -> str:
        """The platform a record came from (Wellfound, DBpedia, manual import…)."""
        source = (self.source_name or "").strip()
        if not source:
            return "Unknown"
        key = source.split(":", 1)[0].strip().lower()
        return PLATFORM_NAMES.get(key, source)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly representation (job URLs stay a list)."""
        return {
            "record_id": self.record_id,
            "name": self.name,
            "website": self.website,
            "platform": self.platform,
            "employee_count": self.employee_count,
            "employee_min": self.employee_min,
            "employee_max": self.employee_max,
            "size_status": self.size_status,
            "size_match": self.size_match,
            "industry": self.industry,
            "niche": self.niche,
            "description": self.description,
            "wellfound_url": self.wellfound_url,
            "job_urls": list(self.job_urls),
            "location": self.location,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "source_id": self.source_id,
            "discovered_at": self.discovered_at,
            "last_checked_at": self.last_checked_at,
            "status": self.status,
            "validation_status": self.validation_status,
            "completeness": self.completeness,
            "duplicate_of": self.duplicate_of,
            "notes": self.notes,
            "website_status": self.website_status,
            "website_evidence": self.website_evidence,
            "website_checked_at": self.website_checked_at,
            "website_final_url": self.website_final_url,
            "member_count_exact": self.member_count_exact,
            "lead_status": self.lead_status,
            "retry_count": self.retry_count,
            "last_run_id": self.last_run_id,
            "discovered_run_id": self.discovered_run_id,
        }

    def to_report_row(self) -> dict[str, Any]:
        """The consistent verified/incomplete report schema."""
        return {
            "name": self.name,
            "website": self.website,
            "employee_range": self.employee_count,
            "member_count_exact": (
                "" if self.member_count_exact is None else self.member_count_exact
            ),
            "wellfound_url": self.wellfound_url,
            "website_status": self.website_status,
            "discovered_at": self.discovered_at,
            "last_checked_at": self.last_checked_at,
            "website_evidence": self.website_evidence,
            "lead_status": self.lead_status,
        }

    def to_export_row(self) -> dict[str, Any]:
        """Flat row used by CSV and Excel exports."""
        row = self.to_dict()
        row["job_urls"] = json.dumps(list(self.job_urls), ensure_ascii=False)
        row["size_match"] = "yes" if self.size_match else "no"
        return row


@dataclass(slots=True)
class ImportErrorRow:
    row: int | str
    message: str
    raw: str = ""


@dataclass(slots=True)
class ImportBatch:
    companies: list[Company] = field(default_factory=list)
    errors: list[ImportErrorRow] = field(default_factory=list)
    format: str = ""
    file_name: str = ""
    file_hash: str = ""
    total_rows: int = 0


@dataclass(slots=True)
class ImportSummary:
    file_name: str
    format: str = ""
    total_rows: int = 0
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    already_imported: bool = False
    file_hash: str = ""

    def one_line(self) -> str:
        if self.already_imported:
            return f"{self.file_name}: already imported (unchanged)"
        parts = [
            f"{self.inserted} new",
            f"{self.updated} updated",
        ]
        if self.duplicates:
            parts.append(f"{self.duplicates} possible duplicate(s)")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.failed:
            parts.append(f"{self.failed} failed")
        return f"{self.file_name}: " + ", ".join(parts)


@dataclass(slots=True)
class RunSummary:
    kind: str
    started_at: str = field(default_factory=iso_now)
    finished_at: str = ""
    discovered: int = 0
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    note: str = ""

    def finish(self) -> "RunSummary":
        self.finished_at = iso_now()
        return self

    def one_line(self) -> str:
        parts = [
            f"{self.inserted} new",
            f"{self.updated} updated",
            f"{self.duplicates} duplicate(s)",
            f"{self.skipped} skipped",
            f"{self.failed} failed",
        ]
        return " · ".join(parts)
