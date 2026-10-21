"""SQLite ledger and result storage."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import CompanyFacts, CompanyResult, Person, RunSummary


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS companies (
                    domain TEXT PRIMARY KEY,
                    wellfound_company_id TEXT,
                    company_name TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    credit_spent INTEGER NOT NULL DEFAULT 0,
                    credits_before INTEGER,
                    credits_after INTEGER,
                    attempted_at TEXT,
                    completed_at TEXT,
                    last_checked_at TEXT,
                    revealed_name TEXT,
                    revealed_email TEXT,
                    pattern TEXT,
                    pattern_note TEXT,
                    company_json TEXT,
                    notes TEXT,
                    last_run_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_companies_wfid
                    ON companies(wellfound_company_id);
                CREATE TABLE IF NOT EXISTS people (
                    domain TEXT NOT NULL,
                    person_key TEXT NOT NULL,
                    full_name TEXT,
                    name_raw TEXT,
                    masked INTEGER NOT NULL DEFAULT 0,
                    title TEXT,
                    tier INTEGER NOT NULL DEFAULT 4,
                    href TEXT,
                    location TEXT,
                    email TEXT,
                    source TEXT,
                    validation TEXT,
                    confidence TEXT,
                    reason TEXT,
                    PRIMARY KEY (domain, person_key)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    input_name TEXT,
                    limit_n INTEGER,
                    dry_run INTEGER,
                    companies_handled INTEGER DEFAULT 0,
                    credits_spent INTEGER DEFAULT 0,
                    verified INTEGER DEFAULT 0,
                    guessed INTEGER DEFAULT 0,
                    masked INTEGER DEFAULT 0,
                    skipped INTEGER DEFAULT 0,
                    errors INTEGER DEFAULT 0,
                    status TEXT,
                    output_path TEXT,
                    note TEXT
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            conn.commit()

    # ------------------------------------------------------------------ lookup
    def get_by_domain(self, domain: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM companies WHERE domain = ?", (domain,)
            ).fetchone()

    def get_by_wfid(self, wfid: str) -> sqlite3.Row | None:
        if not wfid:
            return None
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM companies WHERE wellfound_company_id = ? "
                "ORDER BY credit_spent DESC, completed_at DESC LIMIT 1",
                (wfid,),
            ).fetchone()

    def find_company(self, domain: str, wfid: str = "") -> sqlite3.Row | None:
        by_id = self.get_by_wfid(wfid)
        if by_id is not None and int(by_id["credit_spent"] or 0) > 0:
            return by_id
        by_domain = self.get_by_domain(domain)
        if by_domain is not None:
            return by_domain
        return by_id

    def get_people(self, domain: str) -> list[Person]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM people WHERE domain = ? ORDER BY tier, full_name",
                (domain,),
            ).fetchall()
        return [
            Person(
                person_key=row["person_key"],
                name_raw=row["name_raw"] or row["full_name"] or "",
                full_name=row["full_name"] or "",
                masked=bool(row["masked"]),
                title=row["title"] or "",
                tier=int(row["tier"] or 4),
                href=row["href"] or "",
                location=row["location"] or "",
                email=row["email"] or "",
                source=row["source"] or "",
                validation=row["validation"] or "",
                confidence=row["confidence"] or "",
                reason=row["reason"] or "",
            )
            for row in rows
        ]

    def result_for(self, domain: str, wfid: str = "") -> CompanyResult | None:
        row = self.find_company(domain, wfid)
        if row is None:
            return None
        facts = CompanyFacts()
        if row["company_json"]:
            try:
                facts = CompanyFacts(**json.loads(row["company_json"]))
            except Exception:  # noqa: BLE001
                facts = CompanyFacts()
        people = self.get_people(row["domain"])
        return CompanyResult(
            domain=row["domain"],
            company_name=row["company_name"] or "",
            wellfound_company_id=row["wellfound_company_id"] or "",
            status=row["status"] or "",
            email=row["revealed_email"] or "",
            email_source="verified" if row["revealed_email"] else "",
            notes=row["notes"] or "",
            revealed_name=row["revealed_name"] or "",
            pattern=row["pattern"] or "",
            pattern_note=row["pattern_note"] or "",
            credit_spent=int(row["credit_spent"] or 0),
            credits_before=row["credits_before"],
            credits_after=row["credits_after"],
            facts=facts,
            people=people,
            updated_at=row["completed_at"] or row["last_checked_at"] or "",
        )

    # ------------------------------------------------------------------ writes
    def begin_attempt(
        self,
        domain: str,
        company_name: str,
        wfid: str,
        run_id: str,
        credits_before: int | None,
    ) -> None:
        """Committed before the reveal click. If the process dies after this,
        the company is never clicked again automatically."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO companies
                    (domain, wellfound_company_id, company_name, status,
                     credit_spent, credits_before, attempted_at, last_run_id)
                VALUES (?, ?, ?, 'attempting', 1, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    wellfound_company_id = COALESCE(excluded.wellfound_company_id, companies.wellfound_company_id),
                    company_name = COALESCE(excluded.company_name, companies.company_name),
                    status = 'attempting',
                    credit_spent = 1,
                    credits_before = excluded.credits_before,
                    attempted_at = excluded.attempted_at,
                    last_run_id = excluded.last_run_id
                """,
                (domain, wfid or None, company_name, credits_before, now_iso(), run_id),
            )
            conn.commit()

    def save_people(self, domain: str, people: list[Person], conn: sqlite3.Connection | None = None) -> None:
        def _write(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM people WHERE domain = ?", (domain,))
            c.executemany(
                """
                INSERT INTO people
                    (domain, person_key, full_name, name_raw, masked, title, tier,
                     href, location, email, source, validation, confidence, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        domain,
                        person.person_key,
                        person.full_name,
                        person.name_raw,
                        1 if person.masked else 0,
                        person.title,
                        person.tier,
                        person.href,
                        person.location,
                        person.email,
                        person.source,
                        person.validation,
                        person.confidence,
                        person.reason,
                    )
                    for person in people
                ],
            )

        if conn is not None:
            _write(conn)
        else:
            with self._connect() as own:
                _write(own)
                own.commit()

    def save_result(self, result: CompanyResult, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO companies
                    (domain, wellfound_company_id, company_name, status, credit_spent,
                     credits_before, credits_after, attempted_at, completed_at,
                     last_checked_at, revealed_name, revealed_email, pattern,
                     pattern_note, company_json, notes, last_run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    wellfound_company_id = COALESCE(excluded.wellfound_company_id, companies.wellfound_company_id),
                    company_name = COALESCE(excluded.company_name, companies.company_name),
                    status = excluded.status,
                    credit_spent = MAX(companies.credit_spent, excluded.credit_spent),
                    credits_before = COALESCE(excluded.credits_before, companies.credits_before),
                    credits_after = COALESCE(excluded.credits_after, companies.credits_after),
                    attempted_at = COALESCE(excluded.attempted_at, companies.attempted_at),
                    completed_at = excluded.completed_at,
                    last_checked_at = excluded.last_checked_at,
                    revealed_name = COALESCE(excluded.revealed_name, companies.revealed_name),
                    revealed_email = COALESCE(excluded.revealed_email, companies.revealed_email),
                    pattern = COALESCE(excluded.pattern, companies.pattern),
                    pattern_note = COALESCE(excluded.pattern_note, companies.pattern_note),
                    company_json = COALESCE(excluded.company_json, companies.company_json),
                    notes = excluded.notes,
                    last_run_id = excluded.last_run_id
                """,
                (
                    result.domain,
                    result.wellfound_company_id or None,
                    result.company_name or None,
                    result.status,
                    result.credit_spent,
                    result.credits_before,
                    result.credits_after,
                    now_iso(),
                    now_iso(),
                    now_iso(),
                    result.revealed_name or None,
                    result.email or None,
                    result.pattern or None,
                    result.pattern_note or None,
                    json.dumps(asdict(result.facts)),
                    result.notes,
                    run_id,
                ),
            )
            self.save_people(result.domain, result.people, conn)
            conn.commit()

    def mark_no_candidate(
        self,
        domain: str,
        company_name: str,
        wfid: str,
        notes: str,
        facts: CompanyFacts,
        people: list[Person],
        run_id: str,
    ) -> None:
        result = CompanyResult(
            domain=domain,
            company_name=company_name,
            wellfound_company_id=wfid,
            status="no_candidate",
            notes=notes,
            facts=facts,
            people=people,
            credit_spent=0,
        )
        self.save_result(result, run_id)

    def mark_unknown(
        self,
        domain: str,
        company_name: str,
        wfid: str,
        notes: str,
        run_id: str,
        credits_before: int | None = None,
        credits_after: int | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE companies SET status = 'unknown', notes = ?, completed_at = ?,
                    credits_after = COALESCE(?, credits_after),
                    credits_before = COALESCE(credits_before, ?),
                    last_run_id = ?
                WHERE domain = ?
                """,
                (notes, now_iso(), credits_after, credits_before, run_id, domain),
            )
            conn.commit()

    def reset_domain(self, domain: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM companies WHERE domain = ?", (domain,))
            conn.execute("DELETE FROM people WHERE domain = ?", (domain,))
            conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------------- runs
    def record_run_start(self, summary: RunSummary) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs
                    (run_id, started_at, input_name, limit_n, dry_run, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.run_id,
                    summary.started_at,
                    summary.input_name,
                    summary.limit,
                    1 if summary.dry_run else 0,
                    "running",
                ),
            )
            conn.commit()

    def record_run_finish(self, summary: RunSummary) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs SET
                    finished_at = ?, companies_handled = ?, credits_spent = ?,
                    verified = ?, guessed = ?, masked = ?, skipped = ?, errors = ?,
                    status = ?, output_path = ?, note = ?
                WHERE run_id = ?
                """,
                (
                    summary.finished_at or now_iso(),
                    summary.companies_handled,
                    summary.credits_spent,
                    summary.verified,
                    summary.guessed,
                    summary.masked,
                    summary.skipped,
                    summary.errors,
                    summary.status,
                    summary.output_path,
                    summary.note,
                    summary.run_id,
                ),
            )
            conn.commit()

    def recent_runs(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def ledger_summary(self) -> dict[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS companies,
                    COALESCE(SUM(CASE WHEN credit_spent > 0 THEN 1 ELSE 0 END), 0) AS credits_used,
                    COALESCE(SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END), 0) AS processed,
                    COALESCE(SUM(CASE WHEN status = 'attempting' OR status = 'unknown' THEN 1 ELSE 0 END), 0) AS needs_review
                FROM companies
                """
            ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def domain_statuses(self) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT domain, wellfound_company_id, status, credit_spent, last_checked_at, notes "
                "FROM companies"
            ).fetchall()
        return {
            row["domain"]: {
                "wellfound_company_id": row["wellfound_company_id"] or "",
                "status": row["status"] or "",
                "credit_spent": int(row["credit_spent"] or 0),
                "last_checked_at": row["last_checked_at"] or "",
                "notes": row["notes"] or "",
            }
            for row in rows
        }

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
