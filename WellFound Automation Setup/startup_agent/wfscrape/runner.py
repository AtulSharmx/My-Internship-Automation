"""Orchestrate a public Wellfound scrape and report the result.

The flow is deliberately simple and manual: nothing here is scheduled, and
nothing here is triggered by the Telegram control panel's daily run.  A scrape
happens when a human asks for one.

    python -m startup_agent.cli scrape

The run reports to Telegram when it starts, when it finishes, how many unique
companies it found and what went wrong.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Config
from ..models import Company
from ..normalize import evaluate_company, niche_from_industry
from ..store import Store
from ..verify import WebsiteVerifier
from .apollo import StartupCard, StartupProfile, size_code_for, size_label
from .discover import RoleDiscovery
from .enrich import BrowserUnavailable, ChallengeError, ProfileFetcher
from .export import row_from_company, write_scrape_files
from .roles import load_roles

log = logging.getLogger("startup_agent.wfscrape.runner")

#: The source name the records are stored under.
SOURCE_NAME = "wellfound_public"


@dataclass(slots=True)
class ScrapeSummary:
    """What one scrape found."""

    roles: list[str] = field(default_factory=list)
    pages_fetched: int = 0
    cards_seen: int = 0
    unique_companies: int = 0
    enriched: int = 0
    with_website: int = 0
    already_known: int = 0
    errors: list[str] = field(default_factory=list)
    csv_path: str = ""
    xlsx_path: str = ""
    json_path: str = ""
    elapsed_seconds: float = 0.0
    target_size: str = "1-10"

    def one_line(self) -> str:
        parts = [
            f"{self.unique_companies} unique compan{'y' if self.unique_companies == 1 else 'ies'}",
            f"{self.with_website} with a website",
        ]
        if self.already_known:
            parts.append(f"{self.already_known} already known")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return " · ".join(parts)


class ScrapeRunner:
    """Run a public Wellfound scrape."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        bot: Any | None = None,
        *,
        discovery: RoleDiscovery | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.bot = bot
        self.discovery = discovery or RoleDiscovery(cfg)

    # ------------------------------------------------------------------
    # notifications
    # ------------------------------------------------------------------
    async def _notify(self, text: str) -> None:
        if self.bot is None:
            return
        try:
            await self.bot.send(text)
        except Exception:  # noqa: BLE001 - a notification must never break a run
            log.warning("could not send the Telegram notification", exc_info=True)

    async def _notify_document(self, path: Path, caption: str) -> None:
        if self.bot is None:
            return
        try:
            await self.bot.send_document(path, caption)
        except Exception:  # noqa: BLE001
            log.warning("could not send the report", exc_info=True)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _known_with_website(self, card: StartupCard) -> Company | None:
        """The stored record for this company, when it already has a website."""
        for record_id in self.store.find_by_keys([("profile", card.profile_url)]):
            existing = self.store.get_company(record_id)
            if existing is not None and existing.website:
                return existing
        return None

    def _to_company(
        self, card: StartupCard, profile: StartupProfile
    ) -> Company:
        """Turn a scraped company into a stored record."""
        tags = list(profile.market_tags)
        website = profile.company_url
        size_text = size_label(profile.company_size or card.company_size)
        company = Company(
            record_id="",
            name=profile.name or card.name,
            website=website,
            employee_count=size_text,
            industry=", ".join(tags),
            description=profile.high_concept or card.high_concept,
            wellfound_url=card.profile_url,
            location=", ".join(profile.location_tags),
            source_url=card.profile_url,
            source_name=SOURCE_NAME,
            source_id=card.company_id,
            notes=(
                "Collected from Wellfound's public pages. Headcount, website and "
                "tags are as published by Wellfound."
            ),
            raw={
                "wellfound_company_id": card.company_id,
                "wellfound_slug": card.slug,
                "market_tags": tags,
                "location_tags": profile.location_tags,
                "badges": profile.badges or card.badges,
                "website_source": profile.website_source,
            },
        )
        evaluate_company(company, self.cfg.target_size_min, self.cfg.target_size_max)
        if tags:
            # ``evaluate_company`` derives one niche from the whole industry
            # string; a single tag is a better answer than the first alias
            # that happens to appear in the list.
            company.niche = niche_from_industry(tags[0])
        return company

    # ------------------------------------------------------------------
    # the run
    # ------------------------------------------------------------------
    async def run(
        self,
        *,
        roles: list[str] | None = None,
        size: str = "",
        max_pages_per_role: int = 0,
        max_companies: int = 0,
        headless: bool | None = None,
        profile_dir: Path | str | None = None,
        verify_websites: bool = False,
        on_progress: Callable[[str], None] | None = None,
    ) -> ScrapeSummary:
        started = time.monotonic()
        summary = ScrapeSummary(target_size=size or self.cfg.wfscrape_target_size)

        size_label_wanted = size or self.cfg.wfscrape_target_size
        try:
            size_code = size_code_for(size_label_wanted)
        except ValueError as exc:
            summary.errors.append(str(exc))
            await self._notify(f"❌ <b>Wellfound scrape failed</b>\n<code>{exc}</code>")
            return summary

        role_list = roles or load_roles(self.cfg.wfscrape_roles_file)
        pages = max_pages_per_role or self.cfg.wfscrape_max_pages_per_role
        limit = max_companies or self.cfg.wfscrape_max_companies

        def progress(message: str) -> None:
            if on_progress is not None:
                try:
                    on_progress(message)
                except Exception:  # noqa: BLE001
                    pass

        await self._notify(
            "🔎 <b>Wellfound scrape started</b>\n"
            f"Target size: <b>{size_label_wanted}</b> employees\n"
            f"Roles: <b>{len(role_list)}</b> · up to <b>{limit}</b> companies"
        )

        # --- phase 1: find the companies -------------------------------
        progress(f"Phase 1: walking {len(role_list)} role page(s)...")
        try:
            found = await self.discovery.discover(
                role_list,
                size_code=size_code,
                max_pages_per_role=pages,
                max_companies=limit,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            log.warning("the discovery phase failed", exc_info=True)
            summary.errors.append(f"discovery failed: {type(exc).__name__}: {exc}")
            await self._notify(
                "❌ <b>Wellfound scrape failed</b>\n"
                f"<code>{summary.errors[-1]}</code>"
            )
            summary.elapsed_seconds = time.monotonic() - started
            return summary
        finally:
            await self.discovery.close()
        summary.roles = list(found.roles_walked)
        summary.pages_fetched = found.pages_fetched
        summary.cards_seen = found.cards_seen
        summary.unique_companies = len(found.cards)
        summary.errors.extend(found.errors)
        progress(
            f"Phase 1: {summary.unique_companies} unique {size_label_wanted} "
            f"company(ies) from {found.pages_fetched} page(s)"
        )

        if not found.cards:
            await self._notify(
                "⚠️ <b>Wellfound scrape finished</b>\n"
                f"No {size_label_wanted}-employee companies were found.\n"
                + (f"<code>{summary.errors[0]}</code>" if summary.errors else "")
            )
            summary.elapsed_seconds = time.monotonic() - started
            return summary

        # --- phase 2: read each profile --------------------------------
        rows: list[dict[str, Any]] = []
        verifier = WebsiteVerifier() if verify_websites else None
        fetcher = ProfileFetcher(
            self.cfg, headless=headless, progress=progress, profile_dir=profile_dir
        )
        try:
            await fetcher.start()
            for index, card in enumerate(found.cards, start=1):
                known = self._known_with_website(card)
                if known is not None:
                    summary.already_known += 1
                    rows.append(row_from_company(known))
                    continue
                try:
                    profile = await fetcher.fetch(card.slug)
                except ChallengeError as exc:
                    summary.errors.append(str(exc))
                    log.warning("security check stopped the run: %s", exc)
                    break
                except BrowserUnavailable as exc:
                    summary.errors.append(str(exc))
                    log.warning("browser error: %s", exc)
                    continue
                summary.enriched += 1
                if not profile.company_url:
                    summary.errors.append(f"{card.slug}: no website published")

                company = self._to_company(card, profile)
                if verifier is not None and company.website:
                    check = await verifier.verify(
                        company.website, company_name=company.name
                    )
                    company.website_status = check.status
                    company.website_evidence = check.evidence
                    company.website_checked_at = time.strftime(
                        "%Y-%m-%dT%H:%M:%S%z"
                    )
                    company.website_final_url = check.final_url
                    company.lead_status = _lead_status(company)
                self.store.upsert_company(company)

                if company.website:
                    summary.with_website += 1
                rows.append(row_from_company(company))
                if index % 5 == 0 or index == len(found.cards):
                    progress(
                        f"Phase 2: {index}/{len(found.cards)} profile(s) read, "
                        f"{summary.with_website} with a website"
                    )
                await asyncio.sleep(self.cfg.wfscrape_browser_delay_seconds)
        finally:
            await fetcher.close()
            if verifier is not None:
                await verifier.close()

        # --- the report -------------------------------------------------
        paths = write_scrape_files(self.cfg.export_dir, rows)
        summary.csv_path = str(paths.get("csv", ""))
        summary.xlsx_path = str(paths.get("xlsx", ""))
        summary.json_path = str(paths.get("json", ""))
        for kind, path in paths.items():
            self.store.add_export(f"wf-scrape-{kind}", str(path), len(rows))
        summary.elapsed_seconds = time.monotonic() - started

        minutes = int(summary.elapsed_seconds // 60)
        seconds = int(summary.elapsed_seconds % 60)
        lines = [
            "✅ <b>Wellfound scrape finished</b>",
            "",
            f"<b>{summary.unique_companies}</b> unique companies "
            f"({size_label_wanted} employees)",
            f"<b>{summary.with_website}</b> with an official website",
            f"{summary.pages_fetched} role page(s) read · {minutes}m {seconds}s",
        ]
        if summary.already_known:
            lines.append(f"{summary.already_known} already known (skipped)")
        if summary.errors:
            lines.append("")
            lines.append(f"⚠️ {len(summary.errors)} error(s):")
            for error in summary.errors[:5]:
                lines.append(f"<i>{error}</i>")
        if summary.csv_path:
            lines.append("")
            lines.append(f"📄 {Path(summary.csv_path).name}")
        await self._notify("\n".join(lines))
        if summary.csv_path:
            await self._notify_document(
                Path(summary.csv_path),
                f"Wellfound {size_label_wanted} employee companies "
                f"({summary.unique_companies})",
            )
        progress(summary.one_line())
        return summary

def export_stored(
    cfg: Config,
    store: Store,
    *,
    source_name: str = SOURCE_NAME,
) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    """Rebuild the snapshot from the database, without scraping again.

    Returns the rows and the written files, keyed by kind.
    """
    companies = store.list_companies_by_source(source_name)
    rows = [row_from_company(company) for company in companies]
    paths = write_scrape_files(cfg.export_dir, rows)
    for kind, path in paths.items():
        store.add_export(f"wf-scrape-{kind}", str(path), len(rows))
    return rows, paths


def _lead_status(company: Company) -> str:
    """The lead state after an optional website check."""
    from ..models import LeadStatus, WebsiteStatus

    if company.size_match and company.website_status == WebsiteStatus.VERIFIED:
        return LeadStatus.VERIFIED
    if company.size_match:
        return LeadStatus.INCOMPLETE
    return LeadStatus.DISCOVERED


__all__ = ["ScrapeRunner", "ScrapeSummary", "SOURCE_NAME", "export_stored"]
