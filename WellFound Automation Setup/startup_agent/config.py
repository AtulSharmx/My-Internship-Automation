"""Environment configuration for the startup discovery agent.

Secrets stay in ``.env`` (git-ignored).  This module only reads them and never
prints the Telegram token.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", encoding="utf-8", interpolate=False)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "on", "y"}:
        return True
    if raw in {"0", "false", "no", "off", "n"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _int(name: str, default: int, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not (minimum <= value <= maximum):
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 3600.0) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not (minimum <= value <= maximum):
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = _env(name, default)
    values: list[str] = []
    for line in raw.replace(";", "\n").splitlines():
        for part in line.split(","):
            text = part.strip().strip("\"'")
            if text:
                values.append(text)
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _path(name: str, default: str) -> Path:
    raw = _env(name, default)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _size_range(name: str, default: str = "1-10") -> tuple[int, int]:
    raw = _env(name, default).replace("–", "-").replace("—", "-")
    match = re.fullmatch(r"\s*(\d[\d,]*)\s*(?:-|to|:)\s*(\d[\d,]*)\s*", raw, flags=re.I)
    if match:
        low = int(match.group(1).replace(",", ""))
        high = int(match.group(2).replace(",", ""))
    elif re.fullmatch(r"\s*\d[\d,]*\s*", raw):
        low = high = int(raw.replace(",", "").strip())
    else:
        raise ConfigError(f"{name} must look like 1-10, got {raw!r}")
    if low < 1 or high < low:
        raise ConfigError(f"{name} must be an increasing positive range, got {raw!r}")
    if high > 1_000_000:
        raise ConfigError(f"{name} upper bound is too large, got {high}")
    return low, high


def _hhmm(name: str, default: str = "") -> str:
    raw = _env(name, default)
    if not raw:
        return ""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", raw)
    if not match:
        raise ConfigError(f"{name} must look like 24h HH:MM (for example 20:00 = 8:00 PM)")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"{name} must be a valid 24-hour time, got {raw!r}")
    return f"{hour:02d}:{minute:02d}"


def _domain(value: str) -> str:
    text = value.strip().lower()
    if "://" in text:
        text = urlsplit(text).hostname or ""
    text = text.split("/", 1)[0].split(":", 1)[0].strip().rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    if not text or not re.fullmatch(r"[a-z0-9.-]+", text):
        raise ConfigError(f"STARTUP_ALLOWED_DOMAINS contains an invalid domain: {value!r}")
    return text


def _domains(name: str) -> tuple[str, ...]:
    raw = _csv(name)
    return tuple(dict.fromkeys(_domain(value) for value in raw))


def _urls(name: str) -> tuple[str, ...]:
    raw = _env(name)
    if not raw:
        return ()
    # Accept commas, semicolons and newlines.  Keep the URL text itself intact.
    chunks: list[str] = []
    for line in raw.replace(";", "\n").splitlines():
        chunks.extend(part.strip() for part in line.split(","))
    urls: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        if not re.match(r"^https?://", chunk, flags=re.I):
            raise ConfigError(f"{name} entries must start with http:// or https://")
        urls.append(chunk)
    return tuple(dict.fromkeys(urls))


@dataclass(slots=True)
class Config:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "startups.db")
    import_dir: Path = field(default_factory=lambda: ROOT / "imports")
    export_dir: Path = field(default_factory=lambda: ROOT / "exports")
    log_dir: Path = field(default_factory=lambda: ROOT / "logs")
    target_size_min: int = 1
    target_size_max: int = 10
    enable_url_discovery: bool = False
    allowed_domains: tuple[str, ...] = ()
    seed_urls: tuple[str, ...] = ()
    enable_datasets: bool = False
    dataset_sources: tuple[str, ...] = ("dbpedia",)
    dataset_limit: int = 250
    enable_wellfound_browser: bool = False
    wellfound_max_companies: int = 25
    browser_profile: Path = field(default_factory=lambda: ROOT / "browser_profile")
    browser_headless: bool = False
    startup_auto_scan: bool = True
    daily_run_at: str = ""
    max_urls_per_run: int = 20
    request_timeout_seconds: float = 20.0
    max_retries: int = 2
    min_request_interval_seconds: float = 3.0
    max_page_bytes: int = 2_000_000
    user_agent: str = "StartupDiscoveryAgent/1.0 (personal research; respects robots.txt)"
    max_import_bytes: int = 10 * 1024 * 1024
    max_export_rows: int = 0
    telegram_poll_timeout: int = 30
    log_level: str = "INFO"

    # --- public Wellfound scraper (startup_agent.wfscrape) --------------
    #: Editable list of Wellfound role slugs (``/role/r/<slug>``).
    wfscrape_roles_file: Path = field(
        default_factory=lambda: ROOT / "config" / "wf_scrape_roles.txt"
    )
    wfscrape_max_pages_per_role: int = 3
    wfscrape_max_companies: int = 150
    wfscrape_http_delay_seconds: float = 1.5
    wfscrape_browser_delay_seconds: float = 2.5
    wfscrape_headless: bool = True
    #: A dedicated, *unsigned-in* browser profile.  It only ever holds the
    #: anonymous Cloudflare clearance cookie.
    wfscrape_profile: Path = field(
        default_factory=lambda: ROOT / "data" / "wfscrape_profile"
    )
    wfscrape_target_size: str = "1-10"
    wfscrape_nav_timeout_ms: int = 45_000
    wfscrape_challenge_wait_seconds: int = 30

    @classmethod
    def from_env(cls, *, require_telegram: bool = True) -> "Config":
        token = _env("TELEGRAM_BOT_TOKEN")
        chat_id = _env("TELEGRAM_CHAT_ID")
        if require_telegram and (not token or not chat_id):
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required. "
                "Run `python setup_discovery.py` first."
            )

        target_min, target_max = _size_range("STARTUP_TARGET_SIZE", "1-10")
        enable_url = _bool("STARTUP_ENABLE_URL_DISCOVERY", False)
        allowed = _domains("STARTUP_ALLOWED_DOMAINS")
        if enable_url and not allowed:
            raise ConfigError(
                "STARTUP_ENABLE_URL_DISCOVERY=true requires STARTUP_ALLOWED_DOMAINS. "
                "List only sites whose terms and robots.txt permit your use."
            )

        min_interval = _float(
            "STARTUP_MIN_REQUEST_INTERVAL_SECONDS", 3.0, minimum=0.0, maximum=300.0
        )
        if enable_url and min_interval < 1.0:
            min_interval = 1.0
        log_level = _env("STARTUP_LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("STARTUP_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")

        max_import_mb = _int("STARTUP_MAX_IMPORT_MB", 10, minimum=1, maximum=100)
        enable_datasets = _bool("STARTUP_ENABLE_DATASETS", False)
        dataset_sources = tuple(
            dict.fromkeys(
                part.strip().lower()
                for part in _csv("STARTUP_DATASET_SOURCES")
                if part.strip()
            )
        ) or ("dbpedia",)
        if enable_datasets and not allowed:
            raise ConfigError(
                "STARTUP_ENABLE_DATASETS=true requires STARTUP_ALLOWED_DOMAINS so the "
                "dataset hosts are explicitly allowed."
            )
        return cls(
            telegram_token=token,
            telegram_chat_id=chat_id,
            db_path=_path("STARTUP_DB_PATH", "data/startups.db"),
            import_dir=_path("STARTUP_IMPORT_DIR", "imports"),
            export_dir=_path("STARTUP_EXPORT_DIR", "exports"),
            log_dir=_path("STARTUP_LOG_DIR", "logs"),
            target_size_min=target_min,
            target_size_max=target_max,
            enable_url_discovery=enable_url,
            allowed_domains=allowed,
            seed_urls=_urls("STARTUP_SEED_URLS"),
            enable_datasets=enable_datasets,
            dataset_sources=dataset_sources,
            dataset_limit=_int("STARTUP_DATASET_LIMIT", 250, minimum=1, maximum=5000),
            enable_wellfound_browser=_bool("STARTUP_WELLFOUND_BROWSER", False),
            wellfound_max_companies=_int(
                "STARTUP_WELLFOUND_MAX", 25, minimum=1, maximum=200
            ),
            browser_profile=_path("STARTUP_BROWSER_PROFILE", "browser_profile"),
            browser_headless=False,
            startup_auto_scan=_bool("STARTUP_AUTO_SCAN", True),
            daily_run_at=_hhmm("STARTUP_DAILY_RUN_AT", ""),
            max_urls_per_run=_int("STARTUP_MAX_URLS_PER_RUN", 20, minimum=1, maximum=500),
            request_timeout_seconds=_float(
                "STARTUP_REQUEST_TIMEOUT_SECONDS", 20.0, minimum=5.0, maximum=120.0
            ),
            max_retries=_int("STARTUP_MAX_RETRIES", 2, minimum=0, maximum=5),
            min_request_interval_seconds=min_interval,
            max_page_bytes=_int(
                "STARTUP_MAX_PAGE_MB", 2, minimum=1, maximum=25
            )
            * 1024
            * 1024,
            user_agent=(
                _env(
                    "STARTUP_USER_AGENT",
                    "StartupDiscoveryAgent/1.0 (personal research; respects robots.txt)",
                )
                or "StartupDiscoveryAgent/1.0 (personal research; respects robots.txt)"
            )[:300],
            max_import_bytes=max_import_mb * 1024 * 1024,
            max_export_rows=_int("STARTUP_MAX_EXPORT_ROWS", 0, minimum=0, maximum=1_000_000),
            telegram_poll_timeout=_int(
                "STARTUP_TELEGRAM_POLL_TIMEOUT", 30, minimum=5, maximum=50
            ),
            log_level=log_level,
            wfscrape_roles_file=_path("WFSCRAPE_ROLES_FILE", "config/wf_scrape_roles.txt"),
            wfscrape_max_pages_per_role=_int(
                "WFSCRAPE_MAX_PAGES_PER_ROLE", 3, minimum=1, maximum=50
            ),
            wfscrape_max_companies=_int(
                "WFSCRAPE_MAX_COMPANIES", 150, minimum=1, maximum=5_000
            ),
            wfscrape_http_delay_seconds=_float(
                "WFSCRAPE_HTTP_DELAY_SECONDS", 1.5, minimum=0.0, maximum=300.0
            ),
            wfscrape_browser_delay_seconds=_float(
                "WFSCRAPE_BROWSER_DELAY_SECONDS", 2.5, minimum=0.0, maximum=300.0
            ),
            wfscrape_headless=_bool("WFSCRAPE_HEADLESS", True),
            wfscrape_profile=_path("WFSCRAPE_PROFILE", "data/wfscrape_profile"),
            wfscrape_target_size=_env("WFSCRAPE_TARGET_SIZE", "1-10") or "1-10",
            wfscrape_nav_timeout_ms=_int(
                "WFSCRAPE_NAV_TIMEOUT_MS", 45_000, minimum=5_000, maximum=120_000
            ),
            wfscrape_challenge_wait_seconds=_int(
                "WFSCRAPE_CHALLENGE_WAIT_SECONDS", 30, minimum=0, maximum=300
            ),
        )

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def target_size_label(self) -> str:
        if self.target_size_min == self.target_size_max:
            return str(self.target_size_min)
        return f"{self.target_size_min}-{self.target_size_max}"

    def ensure_dirs(self) -> None:
        for path in (self.db_path.parent, self.import_dir, self.export_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)

    def domain_allowed(self, host: str) -> bool:
        host = (host or "").lower().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return False
        for domain in self.allowed_domains:
            if host == domain or host.endswith("." + domain):
                return True
        return False

    def safe_summary(self) -> dict[str, object]:
        """Configuration detail safe to show in Telegram/logs (no secrets)."""
        return {
            "db_path": str(self.db_path),
            "import_dir": str(self.import_dir),
            "export_dir": str(self.export_dir),
            "target_size": self.target_size_label,
            "url_discovery": self.enable_url_discovery,
            "allowed_domains": list(self.allowed_domains),
            "seed_urls": len(self.seed_urls),
            "datasets": self.enable_datasets,
            "dataset_sources": list(self.dataset_sources),
            "wellfound_browser": self.enable_wellfound_browser,
            "wellfound_max": self.wellfound_max_companies,
            "auto_scan": self.startup_auto_scan,
            "daily_run_at": self.daily_run_at or "manual only",
            "wfscrape_roles_file": str(self.wfscrape_roles_file),
            "wfscrape_target_size": self.wfscrape_target_size,
            "wfscrape_max_companies": self.wfscrape_max_companies,
            "wfscrape_headless": self.wfscrape_headless,
        }
