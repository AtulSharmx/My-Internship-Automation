"""Shared data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Person:
    person_key: str
    name_raw: str
    full_name: str
    masked: bool
    title: str
    tier: int = 4
    href: str = ""
    location: str = ""
    email: str = ""
    source: str = ""
    validation: str = ""
    confidence: str = ""
    reason: str = ""

    @property
    def is_full_name(self) -> bool:
        return not self.masked and bool(self.full_name) and " " in self.full_name.strip()


@dataclass
class CompanyFacts:
    employees: str = ""
    industries: list[str] = field(default_factory=list)
    stage: str = ""
    revenue: str = ""
    founded: str = ""
    location: str = ""
    funding: str = ""
    description: str = ""
    keywords: list[str] = field(default_factory=list)

    @property
    def niche(self) -> str:
        if self.industries:
            return self.industries[0]
        if self.keywords:
            return self.keywords[0]
        return ""

    def notes(self) -> str:
        bits: list[str] = []
        if self.employees:
            bits.append(f"employees {self.employees}")
        if self.industries:
            bits.append("industry " + ", ".join(self.industries[:3]))
        if self.stage:
            bits.append(f"stage {self.stage}")
        if self.revenue:
            bits.append(f"revenue {self.revenue}")
        if self.founded:
            bits.append(f"founded {self.founded}")
        return "; ".join(bits)


@dataclass
class RevealResult:
    email: str
    full_name: str
    title: str
    credits_before: int | None = None
    credits_after: int | None = None
    screenshot_path: Path | None = None

    @property
    def credit_delta(self) -> int | None:
        if self.credits_before is None or self.credits_after is None:
            return None
        return max(0, self.credits_before - self.credits_after)


@dataclass
class InputRow:
    row_number: int
    values: list[Any]
    data: dict[str, Any]
    domain: str
    company_name: str
    wellfound_company_id: str


@dataclass
class InputWorkbook:
    path: Path
    sheet_name: str
    headers: list[str]
    rows: list[InputRow]
    number_formats: dict[str, str]
    column_widths: dict[str, float]


@dataclass
class CompanyResult:
    domain: str
    company_name: str
    wellfound_company_id: str
    status: str
    email: str = ""
    email_source: str = ""
    notes: str = ""
    revealed_name: str = ""
    pattern: str = ""
    pattern_note: str = ""
    credit_spent: int = 0
    credits_before: int | None = None
    credits_after: int | None = None
    facts: CompanyFacts = field(default_factory=CompanyFacts)
    people: list[Person] = field(default_factory=list)
    dry_run: bool = False
    updated_at: str = ""

    @property
    def guessed_count(self) -> int:
        return sum(1 for p in self.people if p.source == "guessed" and p.email)

    @property
    def masked_count(self) -> int:
        return sum(1 for p in self.people if p.masked)


@dataclass
class RunSummary:
    run_id: str
    started_at: str
    finished_at: str = ""
    input_name: str = ""
    limit: int = 0
    dry_run: bool = False
    companies_handled: int = 0
    credits_spent: int = 0
    verified: int = 0
    guessed: int = 0
    masked: int = 0
    skipped: int = 0
    errors: int = 0
    status: str = ""
    output_path: str = ""
    note: str = ""
