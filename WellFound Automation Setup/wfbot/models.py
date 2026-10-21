"""Domain models shared across the bot."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

JOB_PATH_RE = re.compile(r"^/jobs/(?P<id>\d+)-(?P<slug>.+)$")
COMPANY_SUFFIX_RE = re.compile(
    r"\b(?:inc|llc|ltd|limited|pvt|private|corp|corporation|co|company"
    r"|technologies|technology|tech|solutions|systems|labs|ai)\b\.?",
    re.IGNORECASE,
)


def company_key(name: str) -> str:
    """A stable, human-friendly key for company-level de-duplication.

    ``Acme Technologies Pvt Ltd`` and ``Acme`` are the same employer for our
    purposes, so legal suffixes and punctuation are dropped. The raw lowercase
    name is kept when stripping would leave nothing (a company literally named
    ``AI`` or ``Tech``).
    """
    raw = re.sub(r"\s+", " ", (name or "").strip().lower())
    text = re.sub(r"[^a-z0-9 ]+", " ", raw)
    text = COMPANY_SUFFIX_RE.sub(" ", text)
    key = re.sub(r"\s+", " ", text).strip()
    return key or raw


def display_title(title: str, company: str = "") -> str:
    """A compact listing title for chat messages.

    Wellfound often repeats the employer in the heading ("Role at Acme") and
    some scraped cards prefix fields with "Job Title:". Neither is useful under
    a message that already names the company.
    """
    text = re.sub(r"^\s*job title\s*:\s*", "", str(title or ""), flags=re.IGNORECASE)
    text = text.strip()
    name = str(company or "").strip()
    if name:
        suffix = f" at {name}"
        if text.lower().endswith(suffix.lower()):
            text = text[: -len(suffix)].strip()
    return text or "Untitled role"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Status:
    """Result of a single application attempt.

    Every status is terminal for de-duplication purposes: a job that has been
    touched once is never touched again by a later run.
    """

    APPLIED = "applied"                # submitted successfully, confirmation seen
    ALREADY_APPLIED = "already_applied"  # site reported a prior application
    EXTERNAL = "external"              # apply continues on another domain (manual follow-up)
    NEEDS_REVIEW = "needs_review"      # form asks questions the bot will not guess at
    UNVERIFIED = "unverified"          # submitted but no confirmation text appeared
    FAILED = "failed"                  # an error stopped the attempt
    SKIPPED_ROLE = "skipped_role"      # title did not match the requested role
    SKIPPED_CLOSED = "skipped_closed"  # listing no longer accepts applications
    SKIPPED_SIZE = "skipped_size"      # company is larger than the configured limit
    SKIPPED_LOCATION = "skipped_location"  # onsite somewhere we do not want
    SKIPPED_MANUAL = "skipped_manual"  # you replied "skip" to a form question
    SKIPPED_AGE = "skipped_age"        # posted too long ago to be worth applying
    SKIPPED_PAY = "skipped_pay"        # unpaid / commission-only listing
    SKIPPED_COMPANY = "skipped_company"  # already applied to this employer recently
    DEFERRED_SIZE = "deferred_size"    # 11-50 fallback: retried when nothing 1-10 applies

    ALL = (
        APPLIED,
        ALREADY_APPLIED,
        EXTERNAL,
        NEEDS_REVIEW,
        UNVERIFIED,
        FAILED,
        SKIPPED_ROLE,
        SKIPPED_CLOSED,
        SKIPPED_SIZE,
        SKIPPED_LOCATION,
        SKIPPED_MANUAL,
        SKIPPED_AGE,
        SKIPPED_PAY,
        SKIPPED_COMPANY,
        DEFERRED_SIZE,
    )

    # Counters shown in run summaries /status messages.
    COUNTED = (APPLIED, ALREADY_APPLIED, EXTERNAL, NEEDS_REVIEW, UNVERIFIED, FAILED)

    ICONS = {
        APPLIED: "\u2705",
        ALREADY_APPLIED: "\u2139\ufe0f",
        EXTERNAL: "\U0001f517",
        NEEDS_REVIEW: "\U0001f4cb",
        UNVERIFIED: "\u26a0\ufe0f",
        FAILED: "\u274c",
        SKIPPED_ROLE: "\U0001f6ab",
        SKIPPED_CLOSED: "\U0001f6d1",
        SKIPPED_SIZE: "\U0001f3e2",
        SKIPPED_LOCATION: "\U0001f4cd",
        SKIPPED_MANUAL: "\u23ed\ufe0f",
        SKIPPED_AGE: "\U0001f570\ufe0f",
        SKIPPED_PAY: "\U0001f4b8",
        SKIPPED_COMPANY: "\U0001f3e2",
        DEFERRED_SIZE: "\u23ed\ufe0f",
    }

    LABELS = {
        APPLIED: "Applied",
        ALREADY_APPLIED: "Already applied",
        EXTERNAL: "External apply",
        NEEDS_REVIEW: "Needs your review",
        UNVERIFIED: "Unverified",
        FAILED: "Failed",
        SKIPPED_ROLE: "Skipped (role mismatch)",
        SKIPPED_CLOSED: "Skipped (closed)",
        SKIPPED_SIZE: "Skipped (company too big)",
        SKIPPED_LOCATION: "Skipped (location)",
        SKIPPED_MANUAL: "Skipped (by you)",
        SKIPPED_AGE: "Skipped (too old)",
        SKIPPED_PAY: "Skipped (unpaid)",
        SKIPPED_COMPANY: "Skipped (company cooldown)",
        DEFERRED_SIZE: "Deferred (smaller company preferred)",
    }


@dataclass(slots=True)
class Job:
    job_id: str
    slug: str
    url: str
    title: str = ""
    company: str = ""
    meta: str = ""
    role_key: str = ""
    query: str = ""
    has_inline_apply: bool = False
    company_size: str = ""
    company_size_min: int = 0
    company_size_max: int = 0
    location: str = ""
    remote: bool = False
    #: "Internship" / "Full-time" as printed on the listing card.
    job_type: str = ""
    #: Salary/stipend text pulled from the listing card ("" when not shown).
    stipend: str = ""
    #: Path to the confirmation screenshot, when one was captured.
    proof_path: str = ""
    #: Age of the posting in days, read from the listing's "POSTED X AGO" line.
    #: ``None`` means the page did not say (the age filter is then permissive).
    posted_days: float | None = None

    @classmethod
    def from_href(cls, href: str, **kwargs: object) -> Job | None:
        """Build a Job from a ``/jobs/1234-some-slug`` href, else None."""
        parsed = urlparse(href or "")
        if parsed.netloc and "wellfound.com" not in parsed.netloc:
            return None
        match = JOB_PATH_RE.match(parsed.path)
        if not match:
            return None
        job_id, slug = match.group("id"), match.group("slug")
        return cls(
            job_id=job_id,
            slug=slug,
            url=f"https://wellfound.com/jobs/{job_id}-{slug}",
            **kwargs,  # type: ignore[arg-type]
        )


@dataclass(slots=True)
class Outcome:
    job: Job
    status: str
    detail: str = ""
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime = field(default_factory=utcnow)

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    @property
    def ok(self) -> bool:
        return self.status in (Status.APPLIED, Status.ALREADY_APPLIED)


@dataclass(slots=True)
class RuntimeState:
    """Mutable cross-component state shared between the scheduler and Telegram."""

    started_at: datetime = field(default_factory=utcnow)
    running: bool = False
    run_started_at: datetime | None = None
    next_run_at: datetime | None = None
    backoff_multiplier: float = 1.0
    last_status_line: str = "bot just started"


@dataclass(slots=True)
class RunSummary:
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    candidates: int = 0
    attempted: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped_reason: str = ""

    def add(self, outcome: Outcome) -> None:
        self.attempted += 1
        self.counts[outcome.status] = self.counts.get(outcome.status, 0) + 1
        self.outcomes.append(outcome)

    @property
    def applied(self) -> int:
        return self.counts.get(Status.APPLIED, 0)

    @property
    def failures(self) -> int:
        return self.counts.get(Status.FAILED, 0) + self.counts.get(Status.UNVERIFIED, 0)

    @property
    def duration(self) -> str:
        if self.finished_at is None:
            return "-"
        seconds = int((self.finished_at - self.started_at).total_seconds())
        return f"{seconds // 60}m {seconds % 60:02d}s"

    def one_line(self) -> str:
        parts = [
            f"{Status.ICONS.get(status, '\U0001f4e6')} {count} {Status.LABELS.get(status, status)}"
            for status, count in sorted(self.counts.items())
        ]
        return " \u00b7 ".join(parts) if parts else "nothing to do"
