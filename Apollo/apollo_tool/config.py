"""Project configuration and paths."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

REQUIRED_COLUMNS = [
    "company_name",
    "website",
    "domain",
    "team_size",
    "niche",
    "wellfound_profile_url",
    "date_scraped",
    "wellfound_company_id",
]
ADDED_COLUMNS = ["email", "email_source", "notes"]
TEAM_COLUMNS = [
    "company",
    "domain",
    "full_name",
    "title",
    "email",
    "source",
    "syntax/mx validation",
    "confidence",
]

LEADERSHIP_TITLES = ["Founder", "Co-Founder", "CEO", "CTO"]
TALENT_TITLES = [
    "Head of Talent",
    "Head of People",
    "Head of HR",
    "Head of Recruiting",
    "Talent Acquisition",
    "People Operations",
    "Recruiter",
]


def _path_from_env(name: str, default: str) -> Path:
    raw = os.getenv(name, "").strip() or default
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    profile_dir: Path = field(default_factory=lambda: _path_from_env("APOLLO_PROFILE_DIR", "apollo_profile"))
    db_path: Path = field(default_factory=lambda: _path_from_env("APOLLO_DB_PATH", "data/apollo.db"))
    imports_dir: Path = field(default_factory=lambda: _path_from_env("APOLLO_IMPORTS_DIR", "imports"))
    uploads_dir: Path = field(default_factory=lambda: _path_from_env("APOLLO_UPLOADS_DIR", "imports/uploads"))
    exports_dir: Path = field(default_factory=lambda: _path_from_env("APOLLO_EXPORTS_DIR", "exports"))
    logs_dir: Path = field(default_factory=lambda: _path_from_env("APOLLO_LOGS_DIR", "logs"))
    headless: bool = field(default_factory=lambda: os.getenv("APOLLO_HEADLESS", "0").strip() == "1")
    people_pages: int = field(default_factory=lambda: int(os.getenv("APOLLO_PEOPLE_PAGES", "2")))
    retry_no_candidate_days: int = 7
    page_delay_min: float = 3.0
    page_delay_max: float = 6.0
    company_delay_min: float = 8.0
    company_delay_max: float = 15.0
    reveal_timeout_seconds: int = 25

    def ensure_dirs(self) -> None:
        for folder in (
            self.profile_dir,
            self.db_path.parent,
            self.imports_dir,
            self.uploads_dir,
            self.exports_dir,
            self.logs_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
