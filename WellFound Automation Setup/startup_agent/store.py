"""SQLite persistence for company records, imports, runs and the URL queue."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import (
    Company,
    CompanyStatus,
    ImportSummary,
    LeadStatus,
    RunSummary,
    SizeStatus,
    WebsiteStatus,
    derive_lead_status,
    utcnow,
)
from .normalize import completeness_score, identity_keys


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
    record_id        TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    name_key         TEXT NOT NULL DEFAULT '',
    website          TEXT NOT NULL DEFAULT '',
    website_key      TEXT NOT NULL DEFAULT '',
    employee_count   TEXT NOT NULL DEFAULT '',
    employee_min     INTEGER,
    employee_max     INTEGER,
    size_status      TEXT NOT NULL DEFAULT 'unknown',
    size_match       INTEGER NOT NULL DEFAULT 0,
    industry         TEXT NOT NULL DEFAULT '',
    niche            TEXT NOT NULL DEFAULT '',
    description      TEXT NOT NULL DEFAULT '',
    wellfound_url    TEXT NOT NULL DEFAULT '',
    job_urls         TEXT NOT NULL DEFAULT '[]',
    location         TEXT NOT NULL DEFAULT '',
    source_url       TEXT NOT NULL DEFAULT '',
    source_name      TEXT NOT NULL DEFAULT '',
    source_id        TEXT NOT NULL DEFAULT '',
    discovered_at    TEXT NOT NULL,
    last_checked_at  TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'discovered',
    validation_status TEXT NOT NULL DEFAULT '',
    completeness     INTEGER NOT NULL DEFAULT 0,
    duplicate_of     TEXT NOT NULL DEFAULT '',
    notes            TEXT NOT NULL DEFAULT '',
    website_status   TEXT NOT NULL DEFAULT 'not_checked',
    website_evidence TEXT NOT NULL DEFAULT '',
    website_checked_at TEXT NOT NULL DEFAULT '',
    website_final_url TEXT NOT NULL DEFAULT '',
    member_count_exact INTEGER,
    lead_status      TEXT NOT NULL DEFAULT 'discovered',
    retry_count      INTEGER NOT NULL DEFAULT 0,
    last_run_id      TEXT NOT NULL DEFAULT '',
    discovered_run_id TEXT NOT NULL DEFAULT '',
    raw_json         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_companies_size ON companies(size_match DESC, completeness DESC);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status);
CREATE INDEX IF NOT EXISTS idx_companies_name_key ON companies(name_key);
CREATE INDEX IF NOT EXISTS idx_companies_website_key ON companies(website_key);
CREATE INDEX IF NOT EXISTS idx_companies_discovered ON companies(discovered_at);

CREATE TABLE IF NOT EXISTS company_keys (
    key_type   TEXT NOT NULL,
    key_value  TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key_type, key_value),
    FOREIGN KEY (record_id) REFERENCES companies(record_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_company_keys_record ON company_keys(record_id);

CREATE TABLE IF NOT EXISTS company_sources (
    record_id   TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url  TEXT NOT NULL,
    source_id   TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    raw_json    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (record_id, source_name, source_url),
    FOREIGN KEY (record_id) REFERENCES companies(record_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name   TEXT NOT NULL,
    file_hash   TEXT NOT NULL,
    source_format TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    total_rows  INTEGER NOT NULL DEFAULT 0,
    inserted    INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    duplicates  INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    notes       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_imports_hash ON imports(file_hash);
CREATE INDEX IF NOT EXISTS idx_imports_time ON imports(imported_at);

CREATE TABLE IF NOT EXISTS import_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name   TEXT NOT NULL,
    file_hash   TEXT NOT NULL,
    row_ref     TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL,
    raw         TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    discovered  INTEGER NOT NULL DEFAULT 0,
    inserted    INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    duplicates  INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    errors      TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_time ON runs(started_at);

CREATE TABLE IF NOT EXISTS queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL UNIQUE,
    source_name TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, attempts);

CREATE TABLE IF NOT EXISTS exports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    row_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_jobs (
    job_id        TEXT PRIMARY KEY,
    mode          TEXT NOT NULL DEFAULT 'new',
    status        TEXT NOT NULL DEFAULT 'queued',
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL,
    max_profiles  INTEGER NOT NULL DEFAULT 25,
    settings_json TEXT NOT NULL DEFAULT '{}',
    plan_ready    INTEGER NOT NULL DEFAULT 0,
    discovered    INTEGER NOT NULL DEFAULT 0,
    inspected     INTEGER NOT NULL DEFAULT 0,
    eligible      INTEGER NOT NULL DEFAULT 0,
    rejected      INTEGER NOT NULL DEFAULT 0,
    duplicates_skipped INTEGER NOT NULL DEFAULT 0,
    websites_verified INTEGER NOT NULL DEFAULT 0,
    incomplete    INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    retries       INTEGER NOT NULL DEFAULT 0,
    new_verified  INTEGER NOT NULL DEFAULT 0,
    inserted      INTEGER NOT NULL DEFAULT 0,
    updated       INTEGER NOT NULL DEFAULT 0,
    recovered     INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    last_profile  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scan_jobs_status ON scan_jobs(status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_scan_jobs_started ON scan_jobs(started_at DESC);

CREATE TABLE IF NOT EXISTS scan_items (
    job_id        TEXT NOT NULL,
    profile_url   TEXT NOT NULL,
    record_id     TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    card_size     TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL DEFAULT 'discovery',
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    website       TEXT NOT NULL DEFAULT '',
    website_status TEXT NOT NULL DEFAULT 'not_checked',
    employee_count TEXT NOT NULL DEFAULT '',
    last_error    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (job_id, profile_url),
    FOREIGN KEY (job_id) REFERENCES scan_jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_scan_items_status ON scan_items(job_id, status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


COMPANY_FIELDS = (
    "record_id",
    "name",
    "name_key",
    "website",
    "website_key",
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
    "source_url",
    "source_name",
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
    "raw_json",
)


@dataclass(slots=True)
class UpsertResult:
    action: str
    record_id: str
    reason: str = ""


def _iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat(timespec="seconds")


def _json_load(text: str, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _json_dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _append_note(existing: str, note: str) -> str:
    note = str(note or "").strip()
    if not note:
        return existing
    if note.lower() in (existing or "").lower():
        return existing
    combined = f"{existing}\n{note}".strip() if existing else note
    return combined[:2000]


def _company_values(company: Company) -> tuple[Any, ...]:
    return (
        company.record_id,
        company.name,
        company.name_key,
        company.website,
        company.website_key,
        company.employee_count,
        company.employee_min,
        company.employee_max,
        company.size_status,
        1 if company.size_match else 0,
        company.industry,
        company.niche,
        company.description,
        company.wellfound_url,
        _json_dump(company.job_urls),
        company.location,
        company.source_url,
        company.source_name,
        company.source_id,
        company.discovered_at,
        company.last_checked_at,
        company.status,
        company.validation_status,
        int(company.completeness),
        company.duplicate_of,
        company.notes,
        company.website_status,
        company.website_evidence,
        company.website_checked_at,
        company.website_final_url,
        company.member_count_exact,
        company.lead_status,
        int(company.retry_count or 0),
        company.last_run_id,
        company.discovered_run_id,
        _json_dump(company.raw),
    )


def _row_to_company(row: sqlite3.Row) -> Company:
    return Company(
        record_id=row["record_id"],
        name=row["name"],
        name_key=row["name_key"] or "",
        website=row["website"] or "",
        website_key=row["website_key"] or "",
        employee_count=row["employee_count"] or "",
        employee_min=row["employee_min"],
        employee_max=row["employee_max"],
        size_status=row["size_status"] or SizeStatus.UNKNOWN,
        size_match=bool(row["size_match"]),
        industry=row["industry"] or "",
        niche=row["niche"] or "",
        description=row["description"] or "",
        wellfound_url=row["wellfound_url"] or "",
        job_urls=list(_json_load(row["job_urls"], [])),
        location=row["location"] or "",
        source_url=row["source_url"] or "",
        source_name=row["source_name"] or "",
        source_id=row["source_id"] or "",
        discovered_at=row["discovered_at"] or "",
        last_checked_at=row["last_checked_at"] or "",
        status=row["status"] or CompanyStatus.DISCOVERED,
        validation_status=row["validation_status"] or "",
        completeness=int(row["completeness"] or 0),
        duplicate_of=row["duplicate_of"] or "",
        notes=row["notes"] or "",
        website_status=row["website_status"] or WebsiteStatus.NOT_CHECKED,
        website_evidence=row["website_evidence"] or "",
        website_checked_at=row["website_checked_at"] or "",
        website_final_url=row["website_final_url"] or "",
        member_count_exact=row["member_count_exact"],
        lead_status=row["lead_status"] or LeadStatus.DISCOVERED,
        retry_count=int(row["retry_count"] or 0),
        last_run_id=row["last_run_id"] or "",
        discovered_run_id=row["discovered_run_id"] or "",
        raw=dict(_json_load(row["raw_json"], {})),
    )


class Store:
    """One connection guarded by a re-entrant lock.

    The bot is single-process/single-loop, but the lock also makes CLI and test
    helpers safe when they share an instance.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.conn:
            self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        added_lead_status = False
        with self.conn:
            columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(companies)").fetchall()
            }
            additions = {
                "name_key": "TEXT NOT NULL DEFAULT ''",
                "website_key": "TEXT NOT NULL DEFAULT ''",
                "size_status": "TEXT NOT NULL DEFAULT 'unknown'",
                "size_match": "INTEGER NOT NULL DEFAULT 0",
                "niche": "TEXT NOT NULL DEFAULT ''",
                "duplicate_of": "TEXT NOT NULL DEFAULT ''",
                "raw_json": "TEXT NOT NULL DEFAULT '{}'",
                "website_status": "TEXT NOT NULL DEFAULT 'not_checked'",
                "website_evidence": "TEXT NOT NULL DEFAULT ''",
                "website_checked_at": "TEXT NOT NULL DEFAULT ''",
                "website_final_url": "TEXT NOT NULL DEFAULT ''",
                "member_count_exact": "INTEGER",
                "lead_status": "TEXT NOT NULL DEFAULT 'discovered'",
                "retry_count": "INTEGER NOT NULL DEFAULT 0",
                "last_run_id": "TEXT NOT NULL DEFAULT ''",
                "discovered_run_id": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE companies ADD COLUMN {name} {definition}")
                    if name == "lead_status":
                        added_lead_status = True
            if added_lead_status:
                # Backfill honestly: a confirmed 1-10 lead without a verified
                # website is incomplete, not verified.
                self.conn.execute(
                    "UPDATE companies SET lead_status = CASE "
                    "WHEN duplicate_of <> '' OR status = 'duplicate' THEN 'skipped_duplicate' "
                    "WHEN status = 'failed' THEN 'failed' "
                    "WHEN size_match = 1 AND website_status = 'verified' THEN 'verified' "
                    "WHEN size_match = 1 THEN 'incomplete' "
                    "ELSE 'discovered' END"
                )
            # These indexes need the new columns, so they come after the
            # migration.
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_companies_lead_status "
                "ON companies(lead_status, size_match)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_companies_website_status "
                "ON companies(website_status)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_companies_run ON companies(last_run_id)"
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_company(self, record_id: str) -> Company | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM companies WHERE record_id = ?", (record_id,)
            ).fetchone()
        return _row_to_company(row) if row else None

    def find_by_keys(self, keys: Iterable[tuple[str, str]]) -> list[str]:
        pairs = [(str(kind), str(value)) for kind, value in keys if kind and value]
        if not pairs:
            return []
        placeholders = ",".join("(?,?)" for _ in pairs)
        params: list[str] = []
        for kind, value in pairs:
            params.extend((kind, value))
        sql = (
            "SELECT DISTINCT record_id FROM company_keys "
            f"WHERE (key_type, key_value) IN ({placeholders})"
        )
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            if not rows:
                return []
            ids = [row["record_id"] for row in rows]
            marks = ",".join("?" for _ in ids)
            order = self.conn.execute(
                f"SELECT record_id FROM companies WHERE record_id IN ({marks}) "
                "ORDER BY discovered_at ASC, record_id ASC",
                ids,
            ).fetchall()
        return [row["record_id"] for row in order]

    def key_owner(self, key_type: str, key_value: str) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT record_id FROM company_keys WHERE key_type = ? AND key_value = ?",
                (key_type, key_value),
            ).fetchone()
        return row["record_id"] if row else ""

    def find_weak_duplicate(self, name_key: str, location: str, *, exclude: str = "") -> Company | None:
        """A possible duplicate, never an automatic merge.

        Both a normalized name and a non-empty location are required.  A name
        alone is deliberately not enough to merge two companies.
        """
        name_key = (name_key or "").strip()
        location = (location or "").strip()
        if not name_key or not location:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM companies WHERE name_key = ? "
                "AND lower(location) = lower(?) AND record_id <> ? "
                "AND status <> ? ORDER BY discovered_at ASC LIMIT 1",
                (name_key, location, exclude or "", CompanyStatus.DUPLICATE),
            ).fetchone()
        return _row_to_company(row) if row else None

    def list_companies(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        only_confirmed: bool = False,
        status: str = "",
        search: str = "",
    ) -> list[Company]:
        clauses: list[str] = []
        params: list[Any] = []
        if only_confirmed:
            clauses.append(
                "size_match = 1 AND status NOT IN ('duplicate', 'failed')"
            )
        if status:
            clauses.append("status = ?")
            params.append(status)
        if search:
            clauses.append("(name LIKE ? OR industry LIKE ? OR description LIKE ?)")
            term = f"%{search}%"
            params.extend([term, term, term])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT * FROM companies"
            f"{where} ORDER BY size_match DESC, completeness DESC, discovered_at DESC, name ASC "
            "LIMIT ? OFFSET ?"
        )
        params.extend([max(1, limit), max(0, offset)])
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_company(row) for row in rows]

    def list_companies_by_source(
        self,
        source_name: str,
        *,
        limit: int = 5000,
        offset: int = 0,
    ) -> list[Company]:
        """Every record stored under one source, oldest first.

        Used to re-export a previous run without scraping again.
        """
        wanted = str(source_name or "").strip()
        if not wanted:
            return []
        sql = (
            "SELECT * FROM companies WHERE source_name = ? "
            "ORDER BY discovered_at ASC, name ASC LIMIT ? OFFSET ?"
        )
        with self._lock:
            rows = self.conn.execute(
                sql, (wanted, max(1, int(limit)), max(0, int(offset)))
            ).fetchall()
        return [_row_to_company(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN size_match = 1 AND status NOT IN (?, ?) "
                "THEN 1 ELSE 0 END), 0) AS confirmed, "
                "COALESCE(SUM(status = ?), 0) AS needs_review, "
                "COALESCE(SUM(status = ?), 0) AS duplicates, "
                "COALESCE(SUM(status = ?), 0) AS failed, "
                "COALESCE(SUM(status = ?), 0) AS validated, "
                "COALESCE(SUM(size_status = ?), 0) AS unknown_size "
                "FROM companies",
                (
                    CompanyStatus.DUPLICATE,
                    CompanyStatus.FAILED,
                    CompanyStatus.NEEDS_REVIEW,
                    CompanyStatus.DUPLICATE,
                    CompanyStatus.FAILED,
                    CompanyStatus.VALIDATED,
                    SizeStatus.UNKNOWN,
                ),
            ).fetchone()
            midnight = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            today = self.conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE discovered_at >= ?",
                (midnight.isoformat(timespec="seconds"),),
            ).fetchone()
        lead = self.counts_by_lead_status()
        return {
            "total": int(row["total"] or 0),
            "confirmed": int(row["confirmed"] or 0),
            "needs_review": int(row["needs_review"] or 0),
            "duplicates": int(row["duplicates"] or 0),
            "failed": int(row["failed"] or 0),
            "validated": int(row["validated"] or 0),
            "unknown_size": int(row["unknown_size"] or 0),
            "today": int(today["n"] or 0),
            "verified": int(lead.get(LeadStatus.VERIFIED, 0)),
            "incomplete": int(lead.get(LeadStatus.INCOMPLETE, 0)),
            "skipped_duplicate": int(lead.get(LeadStatus.SKIPPED_DUPLICATE, 0)),
        }

    def last_import(self) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM imports ORDER BY imported_at DESC, id DESC LIMIT 1"
            ).fetchone()

    def recent_imports(self, limit: int = 5) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM imports ORDER BY imported_at DESC, id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()

    def import_seen(self, file_hash: str) -> bool:
        if not file_hash:
            return False
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM imports WHERE file_hash = ? LIMIT 1", (file_hash,)
            ).fetchone()
        return bool(row)

    def last_run(self, kind: str = "") -> sqlite3.Row | None:
        with self._lock:
            if kind:
                return self.conn.execute(
                    "SELECT * FROM runs WHERE kind = ? ORDER BY started_at DESC, id DESC LIMIT 1",
                    (kind,),
                ).fetchone()
            return self.conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 1"
            ).fetchone()

    def recent_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()

    def export_count(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM exports").fetchone()
        return int(row["n"] or 0)

    def recent_exports(self, limit: int = 5) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM exports ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()

    # ------------------------------------------------------------------
    # Company upsert
    # ------------------------------------------------------------------
    def upsert_company(
        self,
        company: Company,
        *,
        source_name: str = "",
        source_url: str = "",
        source_id: str = "",
        raw: dict[str, Any] | None = None,
    ) -> UpsertResult:
        if not company.name:
            return UpsertResult("skipped", company.record_id, "missing company name")
        if not company.record_id:
            from .normalize import record_id_for

            company.record_id = record_id_for(company)
        company.source_name = company.source_name or source_name
        company.source_url = company.source_url or source_url
        company.source_id = company.source_id or source_id
        company.raw = raw if raw is not None else company.raw
        company.lead_status = derive_lead_status(company)

        keys = identity_keys(company)
        with self._lock, self.conn:
            matches = self.find_by_keys(keys)
            # A record_id from a prior export is also an explicit identity.
            # This avoids an IntegrityError when an edited export keeps the old
            # id but changes its website/name fields.
            if company.record_id and self.get_company(company.record_id) is not None:
                if company.record_id not in matches:
                    matches.append(company.record_id)
                    def _sort_key(rid):
                        c = self.get_company(rid)
                        return (c.discovered_at or "") if c else ""
                    matches.sort(key=_sort_key)
            if matches:
                primary = matches[0]
                existing = self.get_company(primary)
                if existing is None:
                    return UpsertResult("failed", primary, "identity matched a missing record")
                conflict = len(matches) > 1
                merged = self._merge(existing, company, conflict=conflict)
                self._update_company(merged)
                key_conflicts = self._add_keys(merged.record_id, merged)
                self._add_source(merged, raw)
                reason = ""
                if conflict:
                    reason = "multiple existing records matched; merged into the oldest"
                    merged.notes = _append_note(
                        merged.notes,
                        "Identity conflict: " + ", ".join(matches[1:]) + "; not auto-merged.",
                    )
                    self._update_company(merged)
                if key_conflicts:
                    reason = (reason + "; " if reason else "") + "; ".join(key_conflicts)
                action = "duplicate" if merged.status == CompanyStatus.DUPLICATE else "updated"
                return UpsertResult(action, merged.record_id, reason)

            strong = any(kind in {"profile", "source", "website"} for kind, _ in keys)
            if not strong:
                weak = self.find_weak_duplicate(
                    company.name_key or "", company.location, exclude=company.record_id
                )
                if weak is not None:
                    company.duplicate_of = weak.record_id
                    company.status = CompanyStatus.DUPLICATE
                    company.validation_status = CompanyStatus.NEEDS_REVIEW
                    company.notes = _append_note(
                        company.notes,
                        f"Possible duplicate of {weak.record_id} ({weak.name}); "
                        "not merged automatically because no shared profile URL, "
                        "source id or website was available.",
                    )
                    self._insert_company(company)
                    self._add_keys(company.record_id, company)
                    self._add_source(company, raw)
                    return UpsertResult("duplicate", company.record_id, "possible duplicate")

            self._insert_company(company)
            self._add_keys(company.record_id, company)
            self._add_source(company, raw)
            return UpsertResult("inserted", company.record_id)

    def _insert_company(self, company: Company) -> None:
        columns = ", ".join(COMPANY_FIELDS)
        placeholders = ", ".join("?" for _ in COMPANY_FIELDS)
        self.conn.execute(
            f"INSERT INTO companies ({columns}) VALUES ({placeholders})",
            _company_values(company),
        )

    def _update_company(self, company: Company) -> None:
        fields = [name for name in COMPANY_FIELDS if name not in {"record_id", "discovered_at"}]

        def value_for(name: str) -> Any:
            if name == "job_urls":
                return _json_dump(company.job_urls)
            if name == "raw_json":
                return _json_dump(company.raw)
            if name == "size_match":
                return 1 if company.size_match else 0
            return getattr(company, name)

        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [value_for(name) for name in fields]
        values.append(company.record_id)
        self.conn.execute(
            f"UPDATE companies SET {assignments} WHERE record_id = ?", values
        )

    def _add_keys(self, record_id: str, company: Company) -> list[str]:
        conflicts: list[str] = []
        now = _iso()
        for key_type, key_value in identity_keys(company):
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO company_keys (key_type, key_value, record_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key_type, key_value, record_id, now),
            )
            if cursor.rowcount == 0:
                owner = self.key_owner(key_type, key_value)
                if owner and owner != record_id:
                    conflicts.append(f"{key_type} key already owned by {owner}")
        return conflicts

    def _add_source(self, company: Company, raw: dict[str, Any] | None) -> None:
        now = _iso()
        raw_json = _json_dump(raw if raw is not None else company.raw)[:20000]
        self.conn.execute(
            "INSERT INTO company_sources "
            "(record_id, source_name, source_url, source_id, first_seen, last_seen, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(record_id, source_name, source_url) DO UPDATE SET "
            "source_id = excluded.source_id, last_seen = excluded.last_seen, "
            "raw_json = excluded.raw_json",
            (
                company.record_id,
                company.source_name or "unknown",
                company.source_url or "",
                company.source_id or "",
                company.discovered_at or now,
                now,
                raw_json,
            ),
        )

    def _merge(self, existing: Company, incoming: Company, *, conflict: bool = False) -> Company:
        merged = copy.copy(existing)
        merged.job_urls = list(dict.fromkeys([*existing.job_urls, *incoming.job_urls]))
        merged.raw = incoming.raw or existing.raw
        merged.last_checked_at = incoming.last_checked_at or _iso()
        merged.discovered_at = existing.discovered_at or incoming.discovered_at

        if incoming.name:
            if existing.name and incoming.name_key and incoming.name_key != existing.name_key:
                merged.notes = _append_note(
                    merged.notes, f"Name changed from {existing.name!r} to {incoming.name!r}."
                )
            merged.name = incoming.name
            merged.name_key = incoming.name_key or existing.name_key

        if incoming.website:
            if not existing.website:
                merged.website = incoming.website
                merged.website_key = incoming.website_key
            elif incoming.website_key and incoming.website_key != existing.website_key:
                merged.notes = _append_note(
                    merged.notes,
                    f"Website conflict: kept {existing.website}, source also lists {incoming.website}.",
                )
            elif not existing.website_key:
                merged.website_key = incoming.website_key

        size_conflict = False
        if incoming.employee_count:
            if not existing.employee_count:
                merged.employee_count = incoming.employee_count
                merged.employee_min = incoming.employee_min
                merged.employee_max = incoming.employee_max
                merged.size_status = incoming.size_status
                merged.size_match = incoming.size_match
            elif (
                existing.size_status not in {SizeStatus.UNKNOWN, SizeStatus.PARTIAL}
                and incoming.size_status not in {SizeStatus.UNKNOWN}
                and (
                    existing.employee_min != incoming.employee_min
                    or existing.employee_max != incoming.employee_max
                )
            ):
                size_conflict = True
                merged.notes = _append_note(
                    merged.notes,
                    f"Size conflict: kept {existing.employee_count}, source also lists "
                    f"{incoming.employee_count}.",
                )
                merged.size_status = SizeStatus.UNKNOWN
                merged.size_match = False
            elif existing.size_status in {SizeStatus.UNKNOWN, SizeStatus.PARTIAL} and incoming.size_status != SizeStatus.UNKNOWN:
                merged.employee_count = incoming.employee_count
                merged.employee_min = incoming.employee_min
                merged.employee_max = incoming.employee_max
                merged.size_status = incoming.size_status
                merged.size_match = incoming.size_match
            elif (
                existing.employee_min == incoming.employee_min
                and existing.employee_max == incoming.employee_max
                and incoming.employee_count != existing.employee_count
            ):
                # Same range, better wording ("1 employee" vs "1 employees").
                merged.employee_count = incoming.employee_count
                merged.size_status = incoming.size_status
                merged.size_match = incoming.size_match

        if incoming.industry:
            merged.industry = incoming.industry
            merged.niche = incoming.niche
        if incoming.description:
            merged.description = incoming.description
        if incoming.wellfound_url:
            if not existing.wellfound_url:
                merged.wellfound_url = incoming.wellfound_url
            elif incoming.wellfound_url != existing.wellfound_url:
                merged.notes = _append_note(
                    merged.notes,
                    f"Wellfound profile conflict: kept {existing.wellfound_url}, "
                    f"source also lists {incoming.wellfound_url}.",
                )
        if incoming.location:
            if not existing.location or existing.location.lower() in {"unknown", "not available"}:
                merged.location = incoming.location
            elif incoming.location.lower() != existing.location.lower():
                merged.notes = _append_note(
                    merged.notes,
                    f"Location conflict: kept {existing.location}, source also lists {incoming.location}.",
                )
        if not existing.source_url and incoming.source_url:
            merged.source_url = incoming.source_url
        if not existing.source_name and incoming.source_name:
            merged.source_name = incoming.source_name
        if not existing.source_id and incoming.source_id:
            merged.source_id = incoming.source_id
        if not existing.duplicate_of and incoming.duplicate_of:
            merged.duplicate_of = incoming.duplicate_of

        merged.completeness = completeness_score(merged)
        if size_conflict:
            merged.validation_status = CompanyStatus.NEEDS_REVIEW
            merged.status = CompanyStatus.NEEDS_REVIEW
        elif existing.status == CompanyStatus.DUPLICATE:
            merged.status = CompanyStatus.DUPLICATE
            merged.validation_status = CompanyStatus.NEEDS_REVIEW
        elif existing.status == CompanyStatus.DISCOVERED:
            merged.status = incoming.status
            merged.validation_status = incoming.validation_status
        elif existing.status == CompanyStatus.NEEDS_REVIEW and incoming.status == CompanyStatus.VALIDATED:
            merged.status = CompanyStatus.VALIDATED
            merged.validation_status = CompanyStatus.VALIDATED
        elif existing.status == CompanyStatus.VALIDATED:
            merged.status = CompanyStatus.VALIDATED
            merged.validation_status = existing.validation_status or incoming.validation_status
        else:
            merged.status = existing.status
            merged.validation_status = existing.validation_status or incoming.validation_status

        if conflict:
            merged.notes = _append_note(
                merged.notes, "Multiple identity keys matched different records; review needed."
            )
            merged.status = CompanyStatus.NEEDS_REVIEW
            merged.validation_status = CompanyStatus.NEEDS_REVIEW
            merged.size_status = SizeStatus.UNKNOWN
            merged.size_match = False

        # Website verification evidence.  A previously verified site is never
        # downgraded by a later not-checked/partial observation.
        if incoming.website_status == WebsiteStatus.VERIFIED and incoming.website:
            if not merged.website:
                merged.website = incoming.website
                merged.website_key = incoming.website_key
            if merged.website_key == incoming.website_key:
                merged.website_status = WebsiteStatus.VERIFIED
                merged.website_evidence = incoming.website_evidence
                merged.website_checked_at = incoming.website_checked_at
                merged.website_final_url = incoming.website_final_url
        elif existing.website_status == WebsiteStatus.VERIFIED and (
            not incoming.website or incoming.website_key == existing.website_key
        ):
            pass
        elif incoming.website_status and incoming.website_status != WebsiteStatus.NOT_CHECKED:
            merged.website_status = incoming.website_status
            merged.website_evidence = incoming.website_evidence or existing.website_evidence
            merged.website_checked_at = (
                incoming.website_checked_at or existing.website_checked_at
            )
            merged.website_final_url = incoming.website_final_url or existing.website_final_url

        if incoming.member_count_exact is not None:
            merged.member_count_exact = incoming.member_count_exact
        if incoming.last_run_id:
            merged.last_run_id = incoming.last_run_id
        if not merged.discovered_run_id and incoming.discovered_run_id:
            merged.discovered_run_id = incoming.discovered_run_id
        if incoming.retry_count:
            merged.retry_count = max(existing.retry_count, incoming.retry_count)
        merged.lead_status = derive_lead_status(merged)
        return merged

    # ------------------------------------------------------------------
    # Scan jobs / checkpoints
    # ------------------------------------------------------------------
    _JOB_SET_FIELDS = {
        "mode",
        "status",
        "finished_at",
        "max_profiles",
        "settings_json",
        "plan_ready",
        "note",
        "error",
        "last_profile",
        "recovered",
        "discovered",
        "duplicates_skipped",
    }
    _JOB_BUMP_FIELDS = {
        "discovered",
        "inspected",
        "eligible",
        "rejected",
        "duplicates_skipped",
        "websites_verified",
        "incomplete",
        "errors",
        "retries",
        "new_verified",
        "inserted",
        "updated",
    }

    def create_scan_job(
        self,
        job_id: str,
        *,
        mode: str,
        max_profiles: int,
        settings_json: str,
    ) -> None:
        now = _iso()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO scan_jobs "
                "(job_id, mode, status, started_at, updated_at, max_profiles, settings_json) "
                "VALUES (?, ?, 'running', ?, ?, ?, ?)",
                (job_id, mode, now, now, int(max_profiles), settings_json),
            )

    def get_scan_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM scan_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def latest_scan_job(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM scan_jobs ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def list_scan_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM scan_jobs ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_scan_job(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM scan_jobs WHERE status IN ('running', 'paused', 'stopping') "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def unfinished_scan_job(self) -> dict[str, Any] | None:
        """The newest run that still has work (or was interrupted)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM scan_jobs WHERE status IN "
                "('running', 'paused', 'stopping', 'interrupted') "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def resumable_scan_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM scan_jobs WHERE status IN "
                "('paused', 'stopped', 'interrupted', 'failed') "
                "ORDER BY started_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_scan_job(self, job_id: str, **fields: Any) -> None:
        values: dict[str, Any] = {
            key: value for key, value in fields.items() if key in self._JOB_SET_FIELDS
        }
        values["updated_at"] = _iso()
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = list(values.values())
        params.append(job_id)
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE scan_jobs SET {assignments} WHERE job_id = ?", params
            )

    def bump_scan_job(self, job_id: str, **increments: int) -> None:
        values = {
            key: int(value)
            for key, value in increments.items()
            if key in self._JOB_BUMP_FIELDS and int(value)
        }
        values["updated_at"] = _iso()
        assignments = ", ".join(
            f"{key} = {key} + ?" if key != "updated_at" else "updated_at = ?"
            for key in values
        )
        params = list(values.values())
        params.append(job_id)
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE scan_jobs SET {assignments} WHERE job_id = ?", params
            )

    def add_scan_items(self, job_id: str, items: Iterable[dict[str, Any]]) -> int:
        now = _iso()
        added = 0
        with self._lock, self.conn:
            for item in items:
                profile = str(item.get("profile_url") or "").strip()
                if not profile:
                    continue
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO scan_items "
                    "(job_id, profile_url, record_id, name, card_size, origin, status, "
                    "attempts, website, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        job_id,
                        profile,
                        str(item.get("record_id") or "")[:64],
                        str(item.get("name") or "")[:200],
                        str(item.get("card_size") or "")[:100],
                        str(item.get("origin") or "discovery")[:20],
                        str(item.get("status") or "pending")[:24],
                        str(item.get("website") or "")[:500],
                        now,
                    ),
                )
                added += int(cursor.rowcount or 0)
        return added

    def scan_items(
        self, job_id: str, status: str = ""
    ) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT * FROM scan_items WHERE job_id = ? AND status = ? "
                    "ORDER BY rowid ASC",
                    (job_id, status),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM scan_items WHERE job_id = ? ORDER BY rowid ASC",
                    (job_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def next_pending_scan_item(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM scan_items WHERE job_id = ? AND status = 'pending' "
                "ORDER BY rowid ASC LIMIT 1",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def claim_scan_item(self, job_id: str, profile_url: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE scan_items SET status = 'processing', attempts = attempts + 1, "
                "updated_at = ? WHERE job_id = ? AND profile_url = ?",
                (_iso(), job_id, profile_url),
            )

    def finish_scan_item(
        self,
        job_id: str,
        profile_url: str,
        *,
        status: str,
        website: str = "",
        website_status: str = "",
        employee_count: str = "",
        last_error: str = "",
    ) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE scan_items SET status = ?, website = ?, website_status = ?, "
                "employee_count = ?, last_error = ?, updated_at = ? "
                "WHERE job_id = ? AND profile_url = ?",
                (
                    status,
                    website,
                    website_status,
                    employee_count,
                    str(last_error)[:2000],
                    _iso(),
                    job_id,
                    profile_url,
                ),
            )

    def reset_processing_items(self, job_id: str) -> int:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE scan_items SET status = 'pending', updated_at = ? "
                "WHERE job_id = ? AND status = 'processing'",
                (_iso(), job_id),
            )
        return int(cursor.rowcount or 0)

    def scan_item_counts(self, job_id: str) -> dict[str, int]:
        counts = {
            "pending": 0,
            "processing": 0,
            "done": 0,
            "rejected": 0,
            "failed": 0,
            "skipped_duplicate": 0,
            "total": 0,
        }
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM scan_items WHERE job_id = ? "
                "GROUP BY status",
                (job_id,),
            ).fetchall()
        for row in rows:
            key = str(row["status"])
            counts[key] = int(row["n"] or 0)
            counts["total"] += int(row["n"] or 0)
        return counts

    # ------------------------------------------------------------------
    # Report queries
    # ------------------------------------------------------------------
    def company_by_wellfound_url(self, url: str) -> Company | None:
        if not url:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM companies WHERE wellfound_url = ? LIMIT 1", (url,)
            ).fetchone()
        return _row_to_company(row) if row else None

    def incomplete_companies(self, limit: int = 50) -> list[Company]:
        """Confirmed 1-10 leads that still lack a verified website."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM companies WHERE size_match = 1 "
                "AND status NOT IN ('duplicate', 'failed') "
                "AND lead_status <> 'verified' "
                "AND website_status <> 'verified' "
                "AND wellfound_url <> '' "
                "ORDER BY CASE website_status WHEN 'missing' THEN 0 "
                "WHEN 'unreachable' THEN 1 ELSE 2 END, "
                "last_checked_at ASC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [_row_to_company(row) for row in rows]

    def count_incomplete(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE size_match = 1 "
                "AND status NOT IN ('duplicate', 'failed') "
                "AND lead_status <> 'verified' AND website_status <> 'verified' "
                "AND wellfound_url <> ''"
            ).fetchone()
        return int(row["n"] or 0)

    def count_unverified_web(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE size_match = 1 "
                "AND status NOT IN ('duplicate', 'failed') "
                "AND lead_status <> 'verified' AND website_status <> 'verified' "
                "AND website <> '' AND wellfound_url = ''"
            ).fetchone()
        return int(row["n"] or 0)

    def incomplete_report_companies(self, limit: int = 0) -> list[Company]:
        """Every confirmed-size lead that is not verified yet."""
        sql = (
            "SELECT * FROM companies WHERE size_match = 1 "
            "AND status NOT IN ('duplicate', 'failed') "
            "AND lead_status <> 'verified' AND website_status <> 'verified' "
            "ORDER BY name COLLATE NOCASE ASC"
        )
        params: list[Any] = []
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_company(row) for row in rows]

    def unverified_web_companies(self, limit: int = 0) -> list[Company]:
        """Confirmed-size leads that only need their stored website re-checked."""
        sql = (
            "SELECT * FROM companies WHERE size_match = 1 "
            "AND status NOT IN ('duplicate', 'failed') "
            "AND lead_status <> 'verified' AND website_status <> 'verified' "
            "AND website <> '' AND wellfound_url = '' "
            "ORDER BY last_checked_at ASC, name COLLATE NOCASE ASC"
        )
        params: list[Any] = []
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_company(row) for row in rows]

    def verified_companies(
        self, run_id: str = "", limit: int = 0
    ) -> list[Company]:
        clauses = [
            "size_match = 1",
            "status NOT IN ('duplicate', 'failed')",
            "lead_status = 'verified'",
            "website_status = 'verified'",
        ]
        params: list[Any] = []
        if run_id:
            clauses.append("last_run_id = ?")
            params.append(run_id)
        sql = "SELECT * FROM companies WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name COLLATE NOCASE ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_company(row) for row in rows]

    def companies_for_run(
        self, run_id: str, lead_status: str = "", limit: int = 0
    ) -> list[Company]:
        clauses = ["last_run_id = ?"]
        params: list[Any] = [run_id]
        if lead_status:
            clauses.append("lead_status = ?")
            params.append(lead_status)
        sql = "SELECT * FROM companies WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name COLLATE NOCASE ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_company(row) for row in rows]

    def counts_by_lead_status(self) -> dict[str, int]:
        result = {status: 0 for status in LeadStatus.ALL}
        with self._lock:
            rows = self.conn.execute(
                "SELECT lead_status, COUNT(*) AS n FROM companies GROUP BY lead_status"
            ).fetchall()
        for row in rows:
            result[str(row["lead_status"])] = int(row["n"] or 0)
        return result

    # ------------------------------------------------------------------
    # Import/run bookkeeping
    # ------------------------------------------------------------------
    def record_import(
        self,
        summary: ImportSummary,
        *,
        errors: Iterable[str] = (),
        error_rows: Iterable[tuple[str, str, str]] = (),
    ) -> None:
        now = _iso()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO imports "
                "(file_name, file_hash, source_format, imported_at, total_rows, inserted, "
                "updated, duplicates, skipped, failed, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary.file_name,
                    summary.file_hash,
                    summary.format,
                    now,
                    summary.total_rows,
                    summary.inserted,
                    summary.updated,
                    summary.duplicates,
                    summary.skipped,
                    summary.failed,
                    "\n".join(summary.errors or list(errors))[:4000],
                ),
            )
            for row_ref, message, raw in error_rows:
                self.conn.execute(
                    "INSERT INTO import_errors "
                    "(file_name, file_hash, row_ref, message, raw, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        summary.file_name,
                        summary.file_hash,
                        str(row_ref)[:80],
                        str(message)[:1000],
                        str(raw)[:2000],
                        now,
                    ),
                )

    def record_run(self, summary: RunSummary) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO runs "
                "(kind, started_at, finished_at, discovered, inserted, updated, duplicates, "
                "skipped, failed, errors, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary.kind,
                    summary.started_at,
                    summary.finished_at,
                    summary.discovered,
                    summary.inserted,
                    summary.updated,
                    summary.duplicates,
                    summary.skipped,
                    summary.failed,
                    "\n".join(summary.errors)[:4000],
                    summary.note[:2000],
                ),
            )

    def add_export(self, kind: str, path: str, row_count: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO exports (kind, path, created_at, row_count) VALUES (?, ?, ?, ?)",
                (kind, path, _iso(), int(row_count)),
            )

    # ------------------------------------------------------------------
    # URL queue
    # ------------------------------------------------------------------
    def enqueue_url(self, url: str, source_name: str = "", *, reset_done: bool = False) -> bool:
        now = _iso()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id, status FROM queue WHERE url = ?", (url,)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO queue (url, source_name, status, attempts, last_error, "
                    "created_at, updated_at) VALUES (?, ?, 'pending', 0, '', ?, ?)",
                    (url, source_name, now, now),
                )
                return True
            status = str(row["status"])
            if reset_done and status == "done":
                self.conn.execute(
                    "UPDATE queue SET status = 'pending', attempts = 0, last_error = '', "
                    "source_name = ?, updated_at = ? WHERE url = ?",
                    (source_name, now, url),
                )
                return True
            # Failed URLs stay failed but remain eligible through
            # pending_urls() until their attempt limit is reached. A blocked
            # URL stays blocked: repeatedly bypassing an access denial is not
            # a retry strategy.
            self.conn.execute(
                "UPDATE queue SET source_name = ?, updated_at = ? WHERE url = ?",
                (source_name, now, url),
            )
        return False

    def pending_urls(self, limit: int, max_attempts: int = 2) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM queue WHERE status IN ('pending', 'failed') "
                "AND attempts < ? ORDER BY id ASC LIMIT ?",
                (max_attempts, max(1, limit)),
            ).fetchall()

    def mark_queue(self, queue_id: int, status: str, error: str = "", *, increment: bool = False) -> None:
        with self._lock, self.conn:
            if increment:
                self.conn.execute(
                    "UPDATE queue SET status = ?, attempts = attempts + 1, last_error = ?, "
                    "updated_at = ? WHERE id = ?",
                    (status, str(error)[:2000], _iso(), queue_id),
                )
            else:
                self.conn.execute(
                    "UPDATE queue SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                    (status, str(error)[:2000], _iso(), queue_id),
                )

    def queue_status(self, url: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute("SELECT * FROM queue WHERE url = ?", (url,)).fetchone()

    def queue_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM queue GROUP BY status"
            ).fetchall()
        counts = {"pending": 0, "done": 0, "failed": 0, "blocked": 0}
        for row in rows:
            counts[row["status"]] = int(row["n"] or 0)
        return counts

    # ------------------------------------------------------------------
    # Small key/value state
    # ------------------------------------------------------------------
    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def paused(self) -> bool:
        return self.get_meta("paused", "0") == "1"

    def set_paused(self, value: bool) -> None:
        self.set_meta("paused", "1" if value else "0")

    def integrity_check(self) -> str:
        with self._lock:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
            finally:
                self.conn.close()

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    def count_since(self, hours: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0, hours))
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE discovered_at >= ?",
                (cutoff.isoformat(timespec="seconds"),),
            ).fetchone()
        return int(row["n"] or 0)
