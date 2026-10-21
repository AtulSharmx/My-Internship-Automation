"""Discovery orchestration: local imports, permitted URL queue and exports."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Iterable

from .config import Config
from .export import ExportResult, ExportService
from .models import ImportSummary, RunSummary
from .normalize import evaluate_company
from .sources.datasets import DatasetSource, build_dataset_sources
from .sources.files import FileImportError, FileImporter, SUPPORTED_SUFFIXES
from .sources.web import ComplianceError, SourceError, WebDiscoverySource
from .store import Store

log = logging.getLogger("startup_agent.discovery")


class DiscoveryService:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        *,
        importer: FileImporter | None = None,
        web: WebDiscoverySource | None = None,
        exporter: ExportService | None = None,
        datasets: list[DatasetSource] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.importer = importer or FileImporter(max_bytes=cfg.max_import_bytes)
        self.web = web or WebDiscoverySource(cfg)
        self.exporter = exporter or ExportService(cfg, store)
        self.datasets = (
            datasets
            if datasets is not None
            else (build_dataset_sources(cfg) if cfg.enable_datasets else [])
        )
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------
    async def import_file(
        self, path: Path, *, source_name: str = "", force: bool = False
    ) -> ImportSummary:
        path = Path(path)
        summary = ImportSummary(
            file_name=path.name,
            format=path.suffix.lower().lstrip("."),
        )
        if not path.exists() or not path.is_file():
            summary.failed = 1
            summary.errors.append("file not found")
            return summary
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            summary.failed = 1
            summary.errors.append(f"unsupported file type: {path.suffix or '(none)'}")
            return summary
        try:
            if path.stat().st_size > self.cfg.max_import_bytes:
                summary.failed = 1
                summary.errors.append(
                    f"file is larger than {self.cfg.max_import_bytes // 1024} KB"
                )
                return summary
            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            summary.failed = 1
            summary.errors.append(f"could not read file: {exc}")
            return summary
        summary.file_hash = file_hash
        if not force and self.store.import_seen(file_hash):
            summary.already_imported = True
            return summary

        async with self.lock:
            try:
                batch = self.importer.parse(path, source_name=source_name)
            except FileImportError as exc:
                summary.failed = 1
                summary.errors.append(str(exc))
                self.store.record_import(summary, errors=summary.errors)
                log.warning("import failed for %s: %s", path.name, exc)
                return summary

            summary.format = batch.format or summary.format
            summary.total_rows = batch.total_rows or (len(batch.companies) + len(batch.errors))
            error_rows = [
                (str(error.row), error.message, error.raw) for error in batch.errors
            ]
            for error in batch.errors:
                summary.failed += 1
                if len(summary.errors) < 50:
                    summary.errors.append(f"row {error.row}: {error.message}")

            for company in batch.companies:
                try:
                    evaluate_company(
                        company, self.cfg.target_size_min, self.cfg.target_size_max
                    )
                    result = self.store.upsert_company(
                        company,
                        source_name=company.source_name,
                        source_url=company.source_url,
                        source_id=company.source_id,
                        raw=company.raw,
                    )
                    if result.action == "inserted":
                        summary.inserted += 1
                    elif result.action == "updated":
                        summary.updated += 1
                    elif result.action == "duplicate":
                        summary.duplicates += 1
                    else:
                        summary.skipped += 1
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the file
                    summary.failed += 1
                    if len(summary.errors) < 50:
                        summary.errors.append(f"{company.name or 'row'}: {type(exc).__name__}: {exc}")
                    log.exception("failed to store imported company %r", company.name)

            # Record failed/empty imports too: the status panel must retain
            # failed-to-process history, and an unchanged bad file should not be
            # retried on every Sync forever.
            self.store.record_import(
                summary, errors=summary.errors, error_rows=error_rows
            )
            log.info(
                "import %s: %s new, %s updated, %s duplicate(s), %s failed",
                path.name,
                summary.inserted,
                summary.updated,
                summary.duplicates,
                summary.failed,
            )
            return summary

    async def sync_imports(self) -> list[ImportSummary]:
        """Import new/changed files from the local imports directory."""
        self.cfg.import_dir.mkdir(parents=True, exist_ok=True)
        summaries: list[ImportSummary] = []
        for path in sorted(self.cfg.import_dir.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.name.lower() in {"readme.md", "readme.txt"}:
                continue
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            try:
                summary = await self.import_file(path, source_name=path.stem)
            except Exception as exc:  # noqa: BLE001 - keep scanning other files
                summary = ImportSummary(file_name=path.name, failed=1)
                summary.errors.append(f"{type(exc).__name__}: {exc}")
            if not summary.already_imported:
                summaries.append(summary)
        return summaries

    # ------------------------------------------------------------------
    # Permitted URL discovery
    # ------------------------------------------------------------------
    async def discover(
        self,
        urls: Iterable[str] | None = None,
        *,
        source_name: str = "web",
    ) -> RunSummary:
        summary = RunSummary(kind="url")
        if self.store.paused():
            summary.failed = 1
            summary.errors.append("Discovery is paused. Tap Resume first.")
            summary.finish()
            self.store.record_run(summary)
            return summary
        if not self.cfg.enable_url_discovery:
            summary.failed = 1
            summary.errors.append(
                "URL discovery is disabled. Use Import Data, or explicitly enable "
                "STARTUP_ENABLE_URL_DISCOVERY with allowed domains."
            )
            summary.finish()
            self.store.record_run(summary)
            return summary

        requested = list(urls if urls is not None else self.cfg.seed_urls)
        if not requested:
            summary.failed = 1
            summary.errors.append(
                "No URLs configured. Add STARTUP_SEED_URLS or send/import a URL list."
            )
            summary.finish()
            self.store.record_run(summary)
            return summary
        requested = requested[: self.cfg.max_urls_per_run]

        async with self.lock:
            max_attempts = self.cfg.max_retries + 1
            for value in requested:
                allowed, url, reason = self.web.check_url(value)
                if not allowed:
                    summary.failed += 1
                    summary.errors.append(f"{value}: {reason}")
                    continue
                self.store.enqueue_url(url, source_name, reset_done=True)
                queue_row = self.store.queue_status(url)
                if queue_row is not None:
                    queue_status = str(queue_row["status"])
                    attempts = int(queue_row["attempts"] or 0)
                    if queue_status == "blocked":
                        summary.failed += 1
                        summary.errors.append(
                            f"{url}: previously blocked by the source policy; "
                            "choose a permitted manual import instead"
                        )
                        continue
                    if attempts >= max_attempts:
                        summary.failed += 1
                        summary.errors.append(
                            f"{url}: retry limit reached ({attempts}/{max_attempts})"
                        )
                        continue

            rows = self.store.pending_urls(
                max_attempts=max_attempts, limit=self.cfg.max_urls_per_run
            )
            if not rows:
                if not summary.errors:
                    summary.note = "No URL was queued for processing."
                summary.finish()
                self.store.record_run(summary)
                return summary
            stop = False
            for row in rows:
                if stop:
                    break
                url = row["url"]
                try:
                    companies = await self.web.fetch_companies(
                        url, source_name=row["source_name"] or source_name
                    )
                except ComplianceError as exc:
                    self.store.mark_queue(
                        int(row["id"]), "blocked", str(exc), increment=True
                    )
                    summary.failed += 1
                    summary.errors.append(f"{url}: {exc}")
                    summary.note = "Stopped after the source denied access or robots.txt disallowed it."
                    stop = True
                    log.warning("URL discovery stopped: %s", exc)
                    continue
                except SourceError as exc:
                    self.store.mark_queue(
                        int(row["id"]), "failed", str(exc), increment=True
                    )
                    summary.failed += 1
                    summary.errors.append(f"{url}: {exc}")
                    log.warning("URL failed: %s: %s", url, exc)
                    continue
                except Exception as exc:  # noqa: BLE001 - keep queue resumable
                    self.store.mark_queue(
                        int(row["id"]), "failed", f"{type(exc).__name__}: {exc}", increment=True
                    )
                    summary.failed += 1
                    summary.errors.append(f"{url}: {type(exc).__name__}: {exc}")
                    log.exception("unexpected URL discovery error")
                    continue

                summary.discovered += len(companies)
                if not companies:
                    summary.skipped += 1
                    self.store.mark_queue(int(row["id"]), "done", "no company fields found")
                    continue
                for company in companies:
                    try:
                        evaluate_company(
                            company, self.cfg.target_size_min, self.cfg.target_size_max
                        )
                        result = self.store.upsert_company(
                            company,
                            source_name=company.source_name,
                            source_url=company.source_url,
                            source_id=company.source_id,
                            raw=company.raw,
                        )
                        if result.action == "inserted":
                            summary.inserted += 1
                        elif result.action == "updated":
                            summary.updated += 1
                        elif result.action == "duplicate":
                            summary.duplicates += 1
                        else:
                            summary.skipped += 1
                    except Exception as exc:  # noqa: BLE001 - one record failure
                        summary.failed += 1
                        if len(summary.errors) < 50:
                            summary.errors.append(
                                f"{company.name or url}: {type(exc).__name__}: {exc}"
                            )
                        log.exception("failed to store discovered company %r", company.name)
                self.store.mark_queue(int(row["id"]), "done")
            if not summary.errors and not summary.discovered:
                summary.note = "No company records were collected."
            summary.finish()
            self.store.record_run(summary)
            return summary

    async def discover_urls(self, urls: Iterable[str], *, source_name: str = "web") -> RunSummary:
        return await self.discover(urls, source_name=source_name)

    async def discover_datasets(
        self, source_key: str = "", limit: int = 0
    ) -> RunSummary:
        """Query permitted open datasets (DBpedia) for real company records."""
        summary = RunSummary(kind="dataset")
        if self.store.paused():
            summary.failed = 1
            summary.errors.append("Discovery is paused. Tap Resume first.")
            summary.finish()
            self.store.record_run(summary)
            return summary
        if not self.cfg.enable_datasets:
            summary.failed = 1
            summary.errors.append(
                "Dataset discovery is disabled. Set STARTUP_ENABLE_DATASETS=true and "
                "allow-list the dataset host to use it."
            )
            summary.finish()
            self.store.record_run(summary)
            return summary
        sources = [
            source
            for source in self.datasets
            if not source_key or source.key == source_key
        ]
        if not sources:
            summary.failed = 1
            summary.errors.append(f"No dataset source named {source_key!r} is configured.")
            summary.finish()
            self.store.record_run(summary)
            return summary

        max_rows = limit or self.cfg.dataset_limit
        async with self.lock:
            for source in sources:
                try:
                    companies = await source.fetch(max_rows)
                except ComplianceError as exc:
                    summary.failed += 1
                    summary.errors.append(f"{source.label}: {exc}")
                    summary.note = (
                        "Stopped after the source denied access or its robots.txt "
                        "disallowed the query."
                    )
                    log.warning("dataset discovery stopped: %s", exc)
                    break
                except SourceError as exc:
                    summary.failed += 1
                    summary.errors.append(f"{source.label}: {exc}")
                    log.warning("dataset %s failed: %s", source.key, exc)
                    continue
                except Exception as exc:  # noqa: BLE001 - keep the agent alive
                    summary.failed += 1
                    summary.errors.append(f"{source.label}: {type(exc).__name__}: {exc}")
                    log.exception("unexpected dataset error")
                    continue

                summary.discovered += len(companies)
                for company in companies:
                    try:
                        evaluate_company(
                            company, self.cfg.target_size_min, self.cfg.target_size_max
                        )
                        result = self.store.upsert_company(
                            company,
                            source_name=company.source_name,
                            source_url=company.source_url,
                            source_id=company.source_id,
                            raw=company.raw,
                        )
                        if result.action == "inserted":
                            summary.inserted += 1
                        elif result.action == "updated":
                            summary.updated += 1
                        elif result.action == "duplicate":
                            summary.duplicates += 1
                        else:
                            summary.skipped += 1
                    except Exception as exc:  # noqa: BLE001 - one record must not stop the run
                        summary.failed += 1
                        if len(summary.errors) < 50:
                            summary.errors.append(
                                f"{company.name or 'record'}: {type(exc).__name__}: {exc}"
                            )
                        log.exception("failed to store dataset company %r", company.name)
                if not summary.errors and not summary.discovered:
                    summary.note = "No company records were collected from the dataset."
            summary.finish()
            self.store.record_run(summary)
            return summary

    async def collect_wellfound(
        self, max_companies: int = 0, *, progress: object = None
    ) -> RunSummary:
        """Collect 1-10 employee startups from Wellfound through the browser.

        This drives the user's own visible, signed-in browser session.  It
        never bypasses a login, a CAPTCHA or robots rules - if a challenge
        appears the run stops and says so.
        """
        summary = RunSummary(kind="wellfound")
        if self.store.paused():
            summary.failed = 1
            summary.errors.append("Discovery is paused. Tap Resume first.")
            summary.finish()
            self.store.record_run(summary)
            return summary
        if not self.cfg.enable_wellfound_browser:
            summary.failed = 1
            summary.errors.append(
                "The Wellfound browser collector is off. Set "
                "STARTUP_WELLFOUND_BROWSER=true to use it."
            )
            summary.finish()
            self.store.record_run(summary)
            return summary

        from .sources.wellfound_browser import (
            BrowserError,
            ChallengeError,
            WellfoundBrowserCollector,
        )

        limit = max_companies or self.cfg.wellfound_max_companies
        collector = WellfoundBrowserCollector(
            self.cfg, progress=progress  # type: ignore[arg-type]
        )
        try:
            await collector.start()
        except BrowserError as exc:
            summary.failed = 1
            summary.errors.append(str(exc))
            summary.finish()
            self.store.record_run(summary)
            return summary
        except Exception as exc:  # noqa: BLE001 - e locked profile is a normal case
            summary.failed = 1
            summary.errors.append(
                f"the browser could not be opened ({type(exc).__name__}). "
                "Close any Chrome window that is using the browser profile and try again."
            )
            log.warning("wellfound browser could not start: %s", exc)
            summary.finish()
            self.store.record_run(summary)
            return summary

        try:
            companies = await collector.collect(limit)
        except ChallengeError as exc:
            summary.failed = 1
            summary.errors.append(str(exc))
            summary.note = "Stopped at a verification page - it is never bypassed."
            summary.finish()
            self.store.record_run(summary)
            return summary
        except BrowserError as exc:
            summary.failed = 1
            summary.errors.append(str(exc))
            summary.finish()
            self.store.record_run(summary)
            return summary
        except Exception as exc:  # noqa: BLE001 - keep the agent alive
            summary.failed = 1
            summary.errors.append(f"{type(exc).__name__}: {exc}")
            log.exception("wellfound browser collection failed")
            summary.finish()
            self.store.record_run(summary)
            return summary
        finally:
            await collector.close()

        summary.discovered = len(companies)
        async with self.lock:
            for company in companies:
                try:
                    evaluate_company(
                        company, self.cfg.target_size_min, self.cfg.target_size_max
                    )
                    result = self.store.upsert_company(
                        company,
                        source_name=company.source_name,
                        source_url=company.source_url,
                        source_id=company.source_id,
                        raw=company.raw,
                    )
                    if result.action == "inserted":
                        summary.inserted += 1
                    elif result.action == "updated":
                        summary.updated += 1
                    elif result.action == "duplicate":
                        summary.duplicates += 1
                    else:
                        summary.skipped += 1
                except Exception as exc:  # noqa: BLE001 - one record must not stop the run
                    summary.failed += 1
                    if len(summary.errors) < 50:
                        summary.errors.append(
                            f"{company.name or 'record'}: {type(exc).__name__}: {exc}"
                        )
                    log.exception("failed to store wellfound company %r", company.name)
            if not summary.errors and not summary.discovered:
                summary.note = "No company records were collected."
            summary.finish()
            self.store.record_run(summary)
            return summary

    async def run_daily(self) -> RunSummary:
        """The scheduled run: the browser collector, the datasets, then the URLs."""
        combined: RunSummary | None = None
        if self.cfg.enable_wellfound_browser:
            combined = await self.collect_wellfound()
        if self.cfg.enable_datasets:
            dataset_summary = await self.discover_datasets()
            combined = self._merge_summaries(combined, dataset_summary)
        if self.cfg.enable_url_discovery and self.cfg.seed_urls:
            url_summary = await self.discover(self.cfg.seed_urls)
            combined = self._merge_summaries(combined, url_summary)
        if combined is not None:
            return combined
        return await self.discover(self.cfg.seed_urls)

    @staticmethod
    def _merge_summaries(
        first: RunSummary | None, second: RunSummary
    ) -> RunSummary:
        """Combine two run summaries into one report."""
        if first is None:
            return second
        first.discovered += second.discovered
        first.inserted += second.inserted
        first.updated += second.updated
        first.duplicates += second.duplicates
        first.skipped += second.skipped
        first.failed += second.failed
        first.errors.extend(second.errors)
        if second.note:
            first.note = second.note
        if second.finished_at:
            first.finished_at = second.finished_at
        return first

    # ------------------------------------------------------------------
    # Exports
    # ------------------------------------------------------------------
    async def export(self, kind: str, *, confirmed_only: bool = False) -> ExportResult:
        async with self.lock:
            return self.exporter.export(kind, confirmed_only=confirmed_only)

    async def export_all(self, *, confirmed_only: bool = False) -> list[ExportResult]:
        async with self.lock:
            return self.exporter.export_all(confirmed_only=confirmed_only)

    async def export_sheet(self, kind: str, *, platform: str = "") -> ExportResult:
        """The compact sheet: name, website, platform and members (1-10 only)."""
        async with self.lock:
            return self.exporter.export_sheet(kind, platform=platform)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        last_run = self.store.last_run()
        last_import = self.store.last_import()
        return {
            "paused": self.store.paused(),
            "counts": self.store.counts(),
            "queue": self.store.queue_counts(),
            "last_run": dict(last_run) if last_run else None,
            "last_import": dict(last_import) if last_import else None,
            "config": self.cfg.safe_summary(),
            "db_path": str(self.cfg.db_path),
            "exports": self.store.export_count(),
        }

    def pause(self) -> None:
        self.store.set_paused(True)

    def resume(self) -> None:
        self.store.set_paused(False)

    async def close(self) -> None:
        await self.web.close()
        for source in self.datasets:
            await source.close()
