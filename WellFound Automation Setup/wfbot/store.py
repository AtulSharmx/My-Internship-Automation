"""SQLite persistence: application history, run log and small key/value state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Job, Outcome, RunSummary, Status, company_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    job_id     TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL,
    company    TEXT DEFAULT '',
    role_key   TEXT DEFAULT '',
    query      TEXT DEFAULT '',
    status     TEXT NOT NULL,
    detail     TEXT DEFAULT '',
    applied_at TEXT NOT NULL,
    attempts   INTEGER DEFAULT 1,
    response   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_applications_applied_at ON applications(applied_at);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    candidates  INTEGER DEFAULT 0,
    attempted   INTEGER DEFAULT 0,
    applied     INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    note        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    question_key TEXT UNIQUE NOT NULL,
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add Phase 2 columns to databases created by older versions."""
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(applications)")
        }
        if "attempts" not in columns:
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN attempts INTEGER DEFAULT 1"
            )
        if "response" not in columns:
            self.conn.execute(
                "ALTER TABLE applications ADD COLUMN response TEXT DEFAULT ''"
            )

    # -- applications ---------------------------------------------------
    def is_seen(self, job_id: str, retry_failed_under: int = 0) -> bool:
        """True when this job must not be opened again.

        ``FAILED`` is the only retryable status: an error may be transient
        (page timeout, network). ``retry_failed_under`` is the number of
        retries allowed after the first attempt; a submitted/unverified job is
        never retried because it may already be a real application.
        """
        row = self.conn.execute(
            "SELECT status, attempts FROM applications WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return False
        if row["status"] == Status.FAILED and int(row["attempts"] or 1) <= retry_failed_under:
            return False
        return True

    def record(self, outcome: Outcome) -> None:
        job = outcome.job
        self.conn.execute(
            """
            INSERT INTO applications
                (job_id, url, title, company, role_key, query, status, detail, applied_at,
                 attempts, response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '')
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                detail = excluded.detail,
                applied_at = excluded.applied_at,
                attempts = applications.attempts + 1
            """,
            (
                job.job_id,
                job.url,
                job.title,
                job.company,
                job.role_key,
                job.query,
                outcome.status,
                outcome.detail,
                _iso(outcome.finished_at),
            ),
        )
        self.conn.commit()

    def company_recent(self, company: str, days: int) -> bool:
        """True if this employer had a real application within ``days`` days.

        Company names on Wellfound vary ("Acme", "Acme Technologies Pvt Ltd"),
        so both sides are reduced to a ``company_key``. A company cooldown stops
        the bot from sending three applications to the same tiny startup.
        """
        if days <= 0 or not (company or "").strip():
            return False
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=days))
        rows = self.conn.execute(
            "SELECT DISTINCT company FROM applications"
            " WHERE status IN (?, ?, ?, ?) AND applied_at >= ? AND company != ''",
            (
                Status.APPLIED,
                Status.UNVERIFIED,
                Status.EXTERNAL,
                Status.ALREADY_APPLIED,
                cutoff,
            ),
        ).fetchall()
        wanted = company_key(company)
        if not wanted:
            return False
        return any(company_key(row["company"]) == wanted for row in rows)

    def sync_application(
        self,
        job: Job,
        *,
        status: str = Status.ALREADY_APPLIED,
        detail: str = "found on /jobs/applications",
        response: str = "",
        applied_at: datetime | None = None,
    ) -> tuple[bool, bool]:
        """Import one row from Wellfound's applications page.

        Returns ``(imported, response_updated)``. An existing bot record keeps
        its status; only its response field is refreshed. Manual applications
        that the bot never saw are inserted as ``already_applied`` so they are
        never applied to again.
        """
        row = self.conn.execute(
            "SELECT company, title, response, url, role_key"
            " FROM applications WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        if row is not None:
            changed = False
            if response and response != row["response"]:
                self.conn.execute(
                    "UPDATE applications SET response = ? WHERE job_id = ?",
                    (response, job.job_id),
                )
                changed = True
            if job.company and not row["company"]:
                self.conn.execute(
                    "UPDATE applications SET company = ? WHERE job_id = ?",
                    (job.company, job.job_id),
                )
                changed = True
            if job.title and not row["title"]:
                self.conn.execute(
                    "UPDATE applications SET title = ? WHERE job_id = ?",
                    (job.title, job.job_id),
                )
                changed = True
            if job.role_key and not row["role_key"]:
                self.conn.execute(
                    "UPDATE applications SET role_key = ? WHERE job_id = ?",
                    (job.role_key, job.job_id),
                )
                changed = True
            old_url = str(row["url"] or "")
            if (
                job.url
                and job.url != old_url
                and old_url.rstrip("/").endswith("/" + job.job_id)
            ):
                self.conn.execute(
                    "UPDATE applications SET url = ? WHERE job_id = ?",
                    (job.url, job.job_id),
                )
                changed = True
            if changed:
                self.conn.commit()
            return False, changed

        self.conn.execute(
            """
            INSERT INTO applications
                (job_id, url, title, company, role_key, query, status, detail,
                 applied_at, attempts, response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                job.job_id,
                job.url,
                job.title or job.slug,
                job.company,
                job.role_key,
                job.query or "applications page",
                status,
                detail,
                _iso(applied_at),
                response,
            ),
        )
        self.conn.commit()
        return True, False

    def response_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT response, COUNT(*) AS n FROM applications"
            " WHERE response != '' GROUP BY response ORDER BY n DESC"
        ).fetchall()
        return {str(row["response"]): int(row["n"]) for row in rows}

    def counts(self, *, since_hours: int | None = None) -> dict[str, int]:
        if since_hours is None:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
            )
        else:
            cutoff = _iso(datetime.now(timezone.utc) - timedelta(hours=since_hours))
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM applications"
                " WHERE applied_at >= ? GROUP BY status",
                (cutoff,),
            )
        return {row["status"]: row["n"] for row in rows.fetchall()}

    @property
    def total(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM applications").fetchone()
        return int(row["n"])

    def last_applied(self, limit: int = 5) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT * FROM applications
            WHERE status IN (?, ?)
            ORDER BY applied_at DESC LIMIT ?
            """,
            (Status.APPLIED, Status.ALREADY_APPLIED, limit),
        ).fetchall()

    def recent(self, statuses: tuple[str, ...], limit: int = 5) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in statuses)
        return self.conn.execute(
            f"SELECT * FROM applications WHERE status IN ({placeholders})"
            " ORDER BY applied_at DESC LIMIT ?",  # noqa: S608 - placeholders only
            (*statuses, limit),
        ).fetchall()

    # -- answer bank ------------------------------------------------------
    def get_answer(self, question_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT answer FROM answers WHERE question_key = ?", (question_key,)
        ).fetchone()
        return row["answer"] if row else None

    def save_answer(self, question_key: str, question: str, answer: str) -> None:
        if not question_key:
            return
        self.conn.execute(
            """
            INSERT INTO answers (question_key, question, answer, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(question_key) DO UPDATE SET
                question = excluded.question,
                answer = excluded.answer,
                updated_at = excluded.updated_at
            """,
            (question_key, question, answer, _iso()),
        )
        self.conn.commit()

    def list_answers(self, limit: int = 25) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM answers ORDER BY updated_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def forget_answer(self, answer_id: int) -> bool:
        cursor = self.conn.execute("DELETE FROM answers WHERE id = ?", (answer_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    # -- reporting --------------------------------------------------------
    def set_goal(self, goal: int, deadline: str = "") -> None:
        self.set_meta("goal_applied", str(max(0, int(goal))))
        self.set_meta("goal_deadline", deadline)

    def applied_today(self, now: datetime | None = None) -> int:
        local_now = (now or datetime.now().astimezone()).astimezone()
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM applications WHERE status = ? AND applied_at >= ?",
            (Status.APPLIED, _iso(start.astimezone(timezone.utc))),
        ).fetchone()
        return int(row["n"])

    def report(
        self,
        *,
        now: datetime | None = None,
        goal: int = 0,
        deadline: str = "",
        daily_cap: int = 0,
    ) -> dict:
        """Everything /report needs, computed in one place.

        All stored timestamps are UTC; day boundaries are local so "today"
        means the operator's today, not UTC's.
        """
        local_now = (now or datetime.now().astimezone()).astimezone()
        utc_now = local_now.astimezone(timezone.utc)
        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = utc_now - timedelta(days=7)
        day_starts = [today_start - timedelta(days=offset) for offset in range(6, -1, -1)]

        def counts_since(cutoff: datetime | None) -> dict[str, int]:
            if cutoff is None:
                rows = self.conn.execute(
                    "SELECT status, COUNT(*) n FROM applications GROUP BY status"
                )
            else:
                rows = self.conn.execute(
                    "SELECT status, COUNT(*) n FROM applications"
                    " WHERE applied_at >= ? GROUP BY status",
                    (_iso(cutoff),),
                )
            return {row["status"]: row["n"] for row in rows.fetchall()}

        def applied(counts: dict[str, int]) -> int:
            return int(counts.get(Status.APPLIED, 0))

        def role_counts(cutoff: datetime | None) -> dict[str, int]:
            if cutoff is None:
                rows = self.conn.execute(
                    "SELECT role_key, COUNT(*) n FROM applications"
                    " WHERE status = ? GROUP BY role_key ORDER BY n DESC",
                    (Status.APPLIED,),
                )
            else:
                rows = self.conn.execute(
                    "SELECT role_key, COUNT(*) n FROM applications"
                    " WHERE status = ? AND applied_at >= ?"
                    " GROUP BY role_key ORDER BY n DESC",
                    (Status.APPLIED, _iso(cutoff)),
                )
            return {(row["role_key"] or "unknown"): row["n"] for row in rows.fetchall()}

        today_counts = counts_since(today_start.astimezone(timezone.utc))
        h48_counts = counts_since(utc_now - timedelta(hours=48))
        week_counts = counts_since(week_start)
        all_counts = counts_since(None)

        daily: list[dict] = []
        for start in day_starts:
            end = start + timedelta(days=1)
            row = self.conn.execute(
                "SELECT COUNT(*) n FROM applications"
                " WHERE status = ? AND applied_at >= ? AND applied_at < ?",
                (
                    Status.APPLIED,
                    _iso(start.astimezone(timezone.utc)),
                    _iso(end.astimezone(timezone.utc)),
                ),
            ).fetchone()
            daily.append(
                {
                    "date": start.date().isoformat(),
                    "label": start.strftime("%a %d %b"),
                    "count": int(row["n"]),
                }
            )

        top_companies = [
            (row["company"], int(row["n"]))
            for row in self.conn.execute(
                "SELECT company, COUNT(*) n FROM applications"
                " WHERE status = ? AND company != ''"
                " GROUP BY company ORDER BY n DESC, company LIMIT 5",
                (Status.APPLIED,),
            ).fetchall()
        ]

        first_row = self.conn.execute(
            "SELECT MIN(applied_at) AS first FROM applications WHERE status = ?",
            (Status.APPLIED,),
        ).fetchone()
        first_at = None
        if first_row and first_row["first"]:
            try:
                first_at = datetime.fromisoformat(first_row["first"]).astimezone(
                    timezone.utc
                )
            except ValueError:
                first_at = None

        applied_all = applied(all_counts)
        days_since_first = (
            max(1, (utc_now - first_at).days + 1) if first_at else 0
        )
        avg_per_day = applied_all / days_since_first if days_since_first else 0.0

        goal_value = int(self.get_meta("goal_applied", str(goal)) or 0)
        deadline_value = (self.get_meta("goal_deadline", deadline) or "").strip()
        days_left = None
        required_per_day = None
        projected_date = None
        deadline_date = None
        if goal_value > 0:
            remaining = max(0, goal_value - applied_all)
            if deadline_value:
                try:
                    deadline_date = datetime.strptime(deadline_value, "%Y-%m-%d").date()
                    days_left = (deadline_date - local_now.date()).days
                    required_per_day = remaining / max(1, days_left)
                except ValueError:
                    deadline_date = None
            if avg_per_day > 0 and remaining > 0:
                projected_date = (
                    local_now.date() + timedelta(days=int(-(-remaining // avg_per_day)))
                )

        submitted = applied_all + int(all_counts.get(Status.UNVERIFIED, 0))
        confirmed = applied_all
        unconfirmed = int(all_counts.get(Status.UNVERIFIED, 0))

        streak = 0
        for day in reversed(daily):
            if day["count"] > 0:
                streak += 1
            else:
                break
        best = max(daily, key=lambda day: day["count"], default=None)
        best_day = (
            {"label": best["label"], "count": best["count"]}
            if best and best["count"] > 0
            else {"label": "", "count": 0}
        )

        return {
            "now": local_now,
            "daily_cap": daily_cap,
            "streak": streak,
            "best_day": best_day,
            "today": {"counts": today_counts, "applied": applied(today_counts)},
            "last_48h": {"counts": h48_counts, "applied": applied(h48_counts)},
            "week": {"counts": week_counts, "applied": applied(week_counts)},
            "all": {"counts": all_counts, "applied": applied_all},
            "roles": {
                "today": role_counts(today_start.astimezone(timezone.utc)),
                "week": role_counts(week_start),
                "all": role_counts(None),
            },
            "daily": daily,
            "top_companies": top_companies,
            "first_at": first_at,
            "days_since_first": days_since_first,
            "avg_per_day": avg_per_day,
            "goal": goal_value,
            "deadline": deadline_value,
            "days_left": days_left,
            "required_per_day": required_per_day,
            "projected_date": projected_date,
            "responses": self.response_counts(),
            "submitted": submitted,
            "confirmed": confirmed,
            "unconfirmed": unconfirmed,
            "success_rate": (confirmed / submitted * 100) if submitted else 0.0,
        }

    # -- runs -------------------------------------------------------------
    def save_run(self, summary: RunSummary) -> None:
        note = summary.stopped_reason or summary.errors[:1]
        if isinstance(note, list):
            note = "; ".join(note)
        self.conn.execute(
            """
            INSERT INTO runs (started_at, finished_at, candidates, attempted, applied, failed, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso(summary.started_at),
                _iso(summary.finished_at) if summary.finished_at else None,
                summary.candidates,
                summary.attempted,
                summary.applied,
                summary.failures,
                str(note),
            ),
        )
        self.conn.commit()

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # -- meta ---------------------------------------------------------------
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- convenience state ----------------------------------------------------
    @property
    def paused(self) -> bool:
        return self.get_meta("paused", "0") == "1"

    def set_paused(self, value: bool) -> None:
        self.set_meta("paused", "1" if value else "0")

    @property
    def consecutive_failures(self) -> int:
        return int(self.get_meta("consecutive_failures", "0") or 0)

    def set_consecutive_failures(self, value: int) -> None:
        self.set_meta("consecutive_failures", str(max(0, value)))

    def close(self) -> None:
        self.conn.close()
