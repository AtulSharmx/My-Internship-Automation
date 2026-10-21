"""Configuration: environment variables, target roles and tuning knobs.

All secrets live in ``.env`` (never committed - see ``.gitignore``).
Run ``python setup_wizard.py`` to generate it interactively.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", encoding="utf-8", interpolate=False)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


#: Words that make a listing an internship (or a co-op) rather than a real job.
#: Word boundaries matter: a plain ``"intern" in title`` check would also match
#: "internal" and "international".
INTERN_RE = re.compile(
    r"\b(?:intern(?:ship)?s?|co-?ops?|apprentices?(?:hip)?s?)\b", re.IGNORECASE
)

#: Non-engineering titles that the broad role keywords would otherwise catch.
#: Live examples that slipped through before: "Mobile Marketing Intern" matched
#: the mobile role, and "AI-First Marketing Intern" matched four roles at once.
#: Matching uses word boundaries, so "Salesforce" is not rejected by "sales".
DEFAULT_NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "marketing",
    "sales",
    "growth",
    "content",
    "copywriting",
    "copywriter",
    "social media",
    "seo",
    "human resources",
    "hr",
    "recruiter",
    "talent acquisition",
    "business development",
    "operations",
    "finance",
    "accounting",
    "customer support",
    "customer success",
    "graphic design",
    "design intern",
    "designer",
    "ui/ux",
    "ui design",
    "ux design",
    "product design",
    "video editing",
    "video editor",
    "virtual assistant",
    "data entry",
)


def negative_title_hit(title: str, negatives: tuple[str, ...]) -> str | None:
    """Return the negative keyword present in ``title``, else ``None``."""
    lowered = title.lower()
    for word in negatives:
        token = word.strip().lower()
        if token and re.search(
            rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered
        ):
            return token
    return None


@dataclass(frozen=True, slots=True)
class Role:
    key: str
    label: str
    query: str
    keywords: tuple[str, ...]
    #: Public Wellfound listing pages (``/role/r/<slug>``) used as a fallback
    #: when the signed-in ``/jobs`` search errors. They are crawl-friendly and
    #: still contain some intern listings, but the primary search has the real
    #: Internship + company-size filters.
    slugs: tuple[str, ...] = ()

    def matches(self, title: str, negative_keywords: tuple[str, ...] = ()) -> bool:
        """True only for internship-shaped titles in this role.

        Wellfound's search is fuzzy, so a "Backend Developer Intern" query can
        return full-time roles - and we must never spend an application slot on
        those. ``negative_keywords`` removes non-engineering titles that the
        broad role keywords would otherwise accept ("Mobile Marketing Intern").
        """
        lowered = title.lower()
        if negative_title_hit(title, negative_keywords):
            return False
        if not any(keyword in lowered for keyword in self.keywords):
            return False
        return INTERN_RE.search(title) is not None


#: Roles the bot knows how to hunt for. ``WF_ROLES`` selects a subset.
ROLE_LIBRARY: dict[str, Role] = {
    "fullstack": Role(
        key="fullstack",
        label="Full Stack Developer Intern",
        query="Full Stack Developer Intern",
        keywords=(
            "full stack", "fullstack", "full-stack", "mern",
            "ai-first", "ai first", "ai-assisted", "ai assisted",
            "mobile app", "mobile developer",
        ),
        slugs=("full-stack-engineer", "full-stack-developer"),
    ),
    "backend": Role(
        key="backend",
        label="Backend Developer Intern",
        query="Backend Developer Intern",
        keywords=("backend", "back-end", "back end", "server side", "server-side"),
        slugs=("backend-developer", "backend-engineer"),
    ),
    "web": Role(
        key="web",
        label="Web Developer Intern",
        query="Web Developer Intern",
        keywords=(
            "web developer", "web engineer", "web dev", "frontend", "front-end",
            "front end", "vibe cod", "ai-assisted", "ai assisted", "ai-first", "ai first",
        ),
        slugs=("web-developer", "frontend-engineer"),
    ),
    "vibe": Role(
        key="vibe",
        label="Vibe Coding / AI-Assisted Web Intern",
        query="Vibe Coding Intern",
        keywords=(
            "vibe cod", "ai-assisted", "ai assisted", "ai-first", "ai first",
            "ai engineer", "ai developer", "ai web", "ai-powered", "ai powered",
        ),
        slugs=("ai-engineer",),
    ),
    "mern": Role(
        key="mern",
        label="MERN / AI-First Full Stack Intern",
        query="MERN Stack Developer Intern",
        keywords=("mern", "ai-first", "ai first"),
        slugs=("mern-stack-developer",),
    ),
    "mobile": Role(
        key="mobile",
        label="Full Stack Mobile / Web Intern",
        query="Full Stack Mobile App Developer Intern",
        keywords=(
            "mobile", "react native", "flutter", "android", "ios",
            "full stack mobile", "full-stack mobile",
        ),
        slugs=("mobile-engineer", "react-developer"),
    ),
}

DEFAULT_ROLE_KEYS = ("fullstack", "web", "vibe", "mern", "mobile")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name)
    text = default if raw is None or not raw.strip() else raw
    return tuple(item.strip() for item in text.split(",") if item.strip())


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _hhmm(name: str, default: str) -> str:
    """Parse a local ``HH:MM`` time, or ``""`` to disable a fixed schedule."""
    raw = os.getenv(name)
    raw = (default if raw is None else raw).strip()
    if not raw:
        return ""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        raise ConfigError(
            f"{name} must look like 24h HH:MM (for example 20:00 = 8:00 PM), "
            f"got {raw!r}"
        )
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"{name} must be a valid 24-hour time, got {raw!r}")
    return f"{hour:02d}:{minute:02d}"


def _path(name: str, default: Path) -> Path:
    """A filesystem path from the environment.

    An unset *or empty* variable means "use the default". ``.env.example``
    deliberately leaves these blank, and ``Path("")`` is the current directory,
    which would otherwise turn the browser profile and SQLite path into ``.``.
    Relative paths are anchored to the project root so Task Scheduler's working
    directory cannot silently change where session data lives.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else ROOT / path


@dataclass(slots=True)
class Config:
    # --- Telegram -------------------------------------------------------
    telegram_token: str
    telegram_chat_id: str

    # --- Wellfound credentials (optional if the browser profile is already signed in)
    wf_email: str
    wf_password: str

    # --- Roles ----------------------------------------------------------
    roles: tuple[Role, ...]

    # --- Browser --------------------------------------------------------
    headless: bool
    browser_channel: str          # "", "chrome" or "msedge"
    timezone: str
    profile_dir: Path
    user_agent: str               # "" -> keep the browser's own (stable fingerprint)

    # --- Cadence --------------------------------------------------------
    daily_run_at: str              # local HH:MM, e.g. "20:00"; "" uses interval mode
    interval_minutes: int          # legacy/fallback mode: 0 = continuous
    continuous_gap_seconds: int    # short gap between runs when continuous
    interval_jitter_seconds: int
    min_delay_seconds: int
    max_delay_seconds: int
    max_apps_per_run: int          # 0 = unlimited
    max_apps_per_day: int          # 0 = unlimited

    # --- Resilience -----------------------------------------------------
    challenge_wait_seconds: int
    max_consecutive_failures: int
    backoff_max_multiplier: int
    action_timeout_ms: int

    # --- Behaviour ------------------------------------------------------
    search_url_template: str      # "" -> the role's public /role/r/... pages
    use_search_page: bool         # use the signed-in /jobs Internship search first
    cover_note: str
    scroll_rounds: int
    max_pages_per_role: int       # listing pages scanned per role page slug
    negative_keywords: tuple[str, ...]  # non-engineering words that veto a title

    # --- Quality filters --------------------------------------------------
    skip_unpaid: bool             # skip "unpaid"/"commission only" listings
    answer_bank: bool             # remember form answers and reuse them
    screenshot_proof: bool        # capture a screenshot of each confirmation
    proof_dir: Path               # where those screenshots are stored
    company_cooldown_days: int    # skip other roles at a company applied to recently
    max_retries: int              # retry failed applications this many times
    reconcile_applications: bool  # import manual applications from /jobs/applications
    profile_preflight: bool       # warn about an incomplete profile at startup

    # --- Company size filter ---------------------------------------------
    company_size_min: int         # preferred band, e.g. 1-10 employees
    company_size_max: int
    company_size_fallback_max: int  # 11-50 fallback, used only when nothing smaller applies
    allow_size_fallback: bool
    max_size_checks_per_run: int   # cap on detail pages opened just to read company size

    # --- Freshness filter --------------------------------------------------
    #: Skip listings older than this many days (read from "POSTED X AGO").
    #: 0 disables the filter. Freshest listings are always attempted first.
    max_job_age_days: float
    #: When true, a listing whose age cannot be read is skipped rather than
    #: treated as fresh. The daily 24-hour window needs this to be honest.
    require_posted_age: bool

    # --- Location filter ---------------------------------------------------
    accept_remote: bool
    accept_locations: tuple[str, ...]  # onsite cities we will still apply to
    accept_remote_regions: tuple[str, ...]  # "hires remotely in" regions that count
    location_pages: tuple[str, ...]  # public city pages scanned for onsite roles

    # --- Interactive questions ---------------------------------------------
    ask_questions: bool            # ask in Telegram when a form needs an answer
    question_timeout_seconds: int  # how long to wait for the reply

    # --- Reporting ---------------------------------------------------------
    apply_goal: int                # target number of applications (0 = no goal)
    goal_deadline: str             # ISO date (YYYY-MM-DD) or ""
    daily_report: bool              # send the compact summary automatically
    daily_report_hours: tuple[int, ...]  # local hours 0-23, e.g. (20,)

    # --- Storage --------------------------------------------------------
    db_path: Path
    log_dir: Path
    log_level: str

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, *, require_telegram: bool = True) -> Config:
        missing = [name for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.getenv(name)]
        if missing and require_telegram:
            raise ConfigError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ".  Run `python setup_wizard.py` to create your .env file."
            )

        role_keys = [
            key.strip().lower()
            for key in os.getenv("WF_ROLES", ",".join(DEFAULT_ROLE_KEYS)).split(",")
            if key.strip()
        ]
        unknown = [key for key in role_keys if key not in ROLE_LIBRARY]
        if unknown:
            raise ConfigError(
                f"Unknown role(s) {unknown}. Available: {', '.join(ROLE_LIBRARY)}"
            )
        roles = tuple(ROLE_LIBRARY[key] for key in role_keys) or tuple(
            ROLE_LIBRARY[key] for key in DEFAULT_ROLE_KEYS
        )

        min_delay = _int("WF_MIN_DELAY_SECONDS", 45, minimum=5)
        max_delay = _int("WF_MAX_DELAY_SECONDS", 150, minimum=min_delay)
        daily_run_at = _hhmm("WF_DAILY_RUN_AT", "20:00")

        # Reports can fire one or more times a day: "20" = 8pm, "8,20" = twice.
        # WF_DAILY_REPORT_HOUR (single) is still honoured for old configs.
        raw_hours = _csv("WF_DAILY_REPORT_HOURS", "")
        if not raw_hours:
            legacy = os.getenv("WF_DAILY_REPORT_HOUR", "").strip()
            raw_hours = (legacy,) if legacy else ("20",)
        try:
            report_hours = tuple(sorted({int(value) for value in raw_hours}))
        except ValueError as exc:
            raise ConfigError(
                f"WF_DAILY_REPORT_HOURS must be comma-separated hours 0-23 (got "
                f"{','.join(raw_hours)})"
            ) from exc
        if any(hour < 0 or hour > 23 for hour in report_hours):
            raise ConfigError("WF_DAILY_REPORT_HOURS values must be between 0 and 23")

        size_max = _int("WF_COMPANY_SIZE_MAX", 10, minimum=1)
        size_fallback = _int("WF_COMPANY_SIZE_FALLBACK_MAX", 50, minimum=1)
        if size_fallback < size_max:
            raise ConfigError(
                "WF_COMPANY_SIZE_FALLBACK_MAX must be >= WF_COMPANY_SIZE_MAX "
                f"(got {size_fallback} and {size_max})"
            )

        raw_negative = (os.getenv("WF_NEGATIVE_KEYWORDS") or "").strip()
        negative_keywords = (
            ()
            if raw_negative.lower() in {"none", "off", "0"}
            else _csv("WF_NEGATIVE_KEYWORDS", ",".join(DEFAULT_NEGATIVE_KEYWORDS))
        )

        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=str(os.getenv("TELEGRAM_CHAT_ID", "")).strip(),
            wf_email=os.getenv("WF_EMAIL", "").strip(),
            wf_password=os.getenv("WF_PASSWORD", ""),
            roles=roles,
            headless=_bool("WF_HEADLESS", False),
            browser_channel=os.getenv("WF_BROWSER_CHANNEL", "").strip().lower(),
            timezone=os.getenv("WF_TIMEZONE", "America/New_York").strip() or "America/New_York",
            profile_dir=_path("WF_PROFILE_DIR", ROOT / "browser_profile"),
            user_agent=os.getenv("WF_USER_AGENT", "").strip(),
            daily_run_at=daily_run_at,
            interval_minutes=_int("WF_INTERVAL_MINUTES", 60, minimum=0),
            continuous_gap_seconds=_int("WF_CONTINUOUS_GAP_SECONDS", 60, minimum=5),
            interval_jitter_seconds=_int("WF_INTERVAL_JITTER_SECONDS", 240),
            min_delay_seconds=min_delay,
            max_delay_seconds=max_delay,
            max_apps_per_run=_int("WF_MAX_APPS_PER_RUN", 0, minimum=0),
            max_apps_per_day=_int("WF_MAX_APPS_PER_DAY", 0, minimum=0),
            challenge_wait_seconds=_int("WF_CHALLENGE_WAIT_SECONDS", 240, minimum=30),
            max_consecutive_failures=_int("WF_MAX_CONSECUTIVE_FAILURES", 3, minimum=1),
            backoff_max_multiplier=_int("WF_BACKOFF_MAX_MULTIPLIER", 6, minimum=1),
            action_timeout_ms=_int("WF_ACTION_TIMEOUT_MS", 20000, minimum=2000),
            search_url_template=os.getenv("WF_SEARCH_URL_TEMPLATE", "").strip(),
            use_search_page=_bool("WF_USE_SEARCH_PAGE", True),
            cover_note=os.getenv("WF_COVER_NOTE", "").strip(),
            scroll_rounds=_int("WF_SCROLL_ROUNDS", 2),
            max_pages_per_role=_int("WF_MAX_PAGES_PER_ROLE", 2, minimum=1),
            negative_keywords=negative_keywords,
            skip_unpaid=_bool("WF_SKIP_UNPAID", False),
            answer_bank=_bool("WF_ANSWER_BANK", True),
            screenshot_proof=_bool("WF_SCREENSHOT_PROOF", True),
            proof_dir=_path("WF_PROOF_DIR", ROOT / "data" / "proof"),
            company_cooldown_days=_int("WF_COMPANY_COOLDOWN_DAYS", 30),
            max_retries=_int("WF_MAX_RETRIES", 2),
            reconcile_applications=_bool("WF_RECONCILE_APPLICATIONS", True),
            profile_preflight=_bool("WF_PROFILE_PREFLIGHT", True),
            company_size_min=_int("WF_COMPANY_SIZE_MIN", 1, minimum=1),
            company_size_max=size_max,
            company_size_fallback_max=size_fallback,
            allow_size_fallback=_bool("WF_ALLOW_SIZE_FALLBACK", False),
            max_size_checks_per_run=_int("WF_MAX_SIZE_CHECKS_PER_RUN", 0, minimum=0),
            max_job_age_days=_float("WF_MAX_JOB_AGE_DAYS", 1.0, minimum=0.0),
            require_posted_age=_bool("WF_REQUIRE_POSTED_AGE", True),
            accept_remote=_bool("WF_ACCEPT_REMOTE", True),
            accept_locations=_csv("WF_ACCEPT_LOCATIONS", "Gurugram,Gurgaon"),
            accept_remote_regions=_csv(
                "WF_ACCEPT_REMOTE_REGIONS", "Everywhere,Anywhere,Worldwide,India"
            ),
            location_pages=_csv(
                "WF_LOCATION_PAGES", "https://wellfound.com/location/gurgaon"
            ),
            ask_questions=_bool("WF_ASK_QUESTIONS", True),
            question_timeout_seconds=_int("WF_QUESTION_TIMEOUT_SECONDS", 900, minimum=60),
            apply_goal=_int("WF_APPLY_GOAL", 100, minimum=0),
            goal_deadline=os.getenv("WF_GOAL_DEADLINE", "").strip(),
            daily_report=_bool("WF_DAILY_REPORT", True),
            daily_report_hours=report_hours,
            db_path=_path("WF_DB_PATH", ROOT / "data" / "applications.db"),
            log_dir=_path("WF_LOG_DIR", ROOT / "logs"),
            log_level=os.getenv("WF_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )

    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        return "https://wellfound.com"

    @property
    def daily_run_clock(self) -> time | None:
        """The fixed local run time, or ``None`` when interval mode is active."""
        if not self.daily_run_at:
            return None
        hour, minute = (int(part) for part in self.daily_run_at.split(":"))
        return time(hour, minute)

    @property
    def authenticated_search(self) -> bool:
        """True when the signed-in ``/jobs`` search should drive discovery."""
        return self.use_search_page and not self.search_url_template

    @property
    def freshness_label(self) -> str:
        """Compact human label for the freshness window (``24h``, ``7 days``)."""
        if self.max_job_age_days <= 0:
            return "any age"
        if self.max_job_age_days <= 1:
            return f"{self.max_job_age_days * 24:g}h"
        return f"{self.max_job_age_days:g} days"

    def search_urls(self, role: Role) -> list[str]:
        """Fallback listing pages to walk for ``role``.

        By default these are Wellfound's public role pages (``/role/r/<slug>``),
        which a signed-out visitor can read and ``robots.txt`` explicitly
        allows. The primary discovery path is the signed-in ``/jobs`` search;
        these pages are only used when that search errors.

        ``WF_SEARCH_URL_TEMPLATE`` overrides this: it is formatted with
        ``query=`` and used as the single results URL for every role.
        """
        if self.search_url_template:
            return [self.search_url_template.format(query=role.query)]
        if not role.slugs:
            return [f"{self.base_url}/jobs"]
        return [f"{self.base_url}/role/r/{slug}" for slug in role.slugs]

    def search_url(self, role: Role) -> str:
        """First listing page for ``role`` (used by logs and ``tools/probe.py``)."""
        return self.search_urls(role)[0]

    @property
    def role_labels(self) -> str:
        return ", ".join(role.label for role in self.roles)
