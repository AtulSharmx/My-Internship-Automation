"""Local command-line interface (no Telegram required).

Examples::

    python -m startup_agent.cli init
    python -m startup_agent.cli import imports/companies.csv
    python -m startup_agent.cli discover --seeds
    python -m startup_agent.cli export --format all
    python -m startup_agent.cli stats
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import Config, ConfigError
from .discovery import DiscoveryService
from .export import EmptyReportError
from .jobs import ScanManager, ScanSettings
from .sources.files import SUPPORTED_SUFFIXES
from .store import Store
from .wfscrape.roles import load_roles


def _build() -> tuple[Config, Store, DiscoveryService]:
    cfg = Config.from_env(require_telegram=False)
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    return cfg, store, DiscoveryService(cfg, store)


def _print_summary(summary) -> None:
    print(summary.one_line())
    if getattr(summary, "already_imported", False):
        print("  (same content hash as an earlier import; no duplicate records)")
    for error in getattr(summary, "errors", [])[:15]:
        print(f"  ! {error}")


async def _cmd_import(args: argparse.Namespace) -> int:
    cfg, store, service = _build()
    try:
        path = Path(args.path)
        paths = sorted(path.iterdir()) if path.is_dir() else [path]
        summaries = []
        for item in paths:
            if not item.is_file() or item.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            summary = await service.import_file(item, source_name="")
            summaries.append(summary)
            _print_summary(summary)
        if not summaries:
            print("No supported files found.")
            return 1
        failed = sum(summary.failed for summary in summaries)
        inserted = sum(summary.inserted for summary in summaries)
        updated = sum(summary.updated for summary in summaries)
        print(f"\nDone: {inserted} new, {updated} updated, {failed} failed.")
        return 1 if (failed and not (inserted or updated)) else 0
    finally:
        await service.close()
        store.close()


async def _cmd_discover(args: argparse.Namespace) -> int:
    cfg, store, service = _build()
    try:
        urls = args.url or (list(cfg.seed_urls) if args.seeds else None)
        want_datasets = args.datasets or (
            not urls and not args.seeds and cfg.enable_datasets
        )
        totals = {"inserted": 0, "updated": 0, "failed": 0, "discovered": 0}
        exit_code = 1
        if want_datasets:
            dataset_summary = await service.discover_datasets(
                args.dataset_source or "", args.dataset_limit or 0
            )
            _print_summary(dataset_summary)
            print(
                f"Dataset: seen {dataset_summary.discovered}; "
                f"new {dataset_summary.inserted}; updated {dataset_summary.updated}; "
                f"failed {dataset_summary.failed}."
            )
            for error in dataset_summary.errors[:10]:
                print(f"  ! {error}")
            totals["inserted"] += dataset_summary.inserted
            totals["updated"] += dataset_summary.updated
            totals["failed"] += dataset_summary.failed
            totals["discovered"] += dataset_summary.discovered
            if dataset_summary.inserted or dataset_summary.updated:
                exit_code = 0
        if urls is not None or args.seeds or (not want_datasets and cfg.seed_urls):
            url_summary = await service.discover(urls)
            _print_summary(url_summary)
            print(
                f"URLs: seen {url_summary.discovered}; new {url_summary.inserted}; "
                f"updated {url_summary.updated}; failed {url_summary.failed}."
            )
            for error in url_summary.errors[:10]:
                print(f"  ! {error}")
            totals["inserted"] += url_summary.inserted
            totals["updated"] += url_summary.updated
            totals["failed"] += url_summary.failed
            totals["discovered"] += url_summary.discovered
            if url_summary.inserted or url_summary.updated:
                exit_code = 0
        print(
            f"\nDone: {totals['inserted']} new, {totals['updated']} updated, "
            f"{totals['failed']} failed, {totals['discovered']} records seen."
        )
        return exit_code
    finally:
        await service.close()
        store.close()


async def _cmd_export(args: argparse.Namespace) -> int:
    cfg, store, service = _build()
    try:
        if args.format == "all":
            results = await service.export_all(confirmed_only=args.confirmed_only)
        else:
            results = [
                await service.export(args.format, confirmed_only=args.confirmed_only)
            ]
        for result in results:
            print(f"{result.kind.upper()}: {result.row_count} rows -> {result.path}")
        return 0
    finally:
        await service.close()
        store.close()


async def _cmd_sheet(args: argparse.Namespace) -> int:
    cfg, store, service = _build()
    try:
        result = await service.export_sheet(args.format, platform=args.platform)
        print(f"{result.kind.upper()}: {result.row_count} rows -> {result.path}")
        if not result.row_count:
            print("  (no confirmed target-size company matches that platform)")
        return 0
    finally:
        await service.close()
        store.close()


async def _cmd_collect(args: argparse.Namespace) -> int:
    cfg, store, service = _build()
    try:
        async def progress(update) -> None:
            print(f"  {update.message}")

        summary = await service.collect_wellfound(
            args.max, progress=progress if args.verbose else None
        )
        print(summary.one_line())
        if summary.note:
            print(f"  {summary.note}")
        for error in summary.errors[:10]:
            print(f"  ! {error}")
        print(
            f"Done: {summary.inserted} new, {summary.updated} updated, "
            f"{summary.duplicates} duplicate(s), {summary.failed} failed."
        )
        return 0 if (summary.inserted or summary.updated) else 1
    finally:
        await service.close()
        store.close()


async def _cmd_scrape(args: argparse.Namespace) -> int:
    """Scrape Wellfound's public pages for target-sized companies."""
    from .telegram import Telegram
    from .wfscrape.runner import ScrapeRunner

    cfg = Config.from_env(require_telegram=False)
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    bot = None
    service = None
    if cfg.telegram_token and cfg.telegram_chat_id:
        try:
            service = DiscoveryService(cfg, store)
            bot = Telegram(cfg, service)
        except Exception as exc:  # noqa: BLE001 - the scrape still runs
            print(f"  (Telegram unavailable: {exc})")
    runner = ScrapeRunner(cfg, store, bot)
    roles = load_roles(args.roles) if args.roles else None

    def progress(message: str) -> None:
        print(f"  {message}")

    try:
        summary = await runner.run(
            roles=roles,
            size=args.size,
            max_pages_per_role=args.pages,
            max_companies=args.max,
            headless=False if args.headed else None,
            profile_dir=args.profile or None,
            verify_websites=args.verify,
            on_progress=progress,
        )
    finally:
        if bot is not None:
            await bot.close()
        if service is not None:
            await service.close()
        store.close()

    print("")
    print(summary.one_line())
    if summary.csv_path:
        print(f"CSV: {summary.csv_path}")
    for error in summary.errors[:10]:
        print(f"  ! {error}")
    return 0 if summary.unique_companies else 1


async def _cmd_scrape_prime(args: argparse.Namespace) -> int:
    """Clear Wellfound's security check once, in a visible browser."""
    from .wfscrape.enrich import BrowserUnavailable, ProfileFetcher

    cfg = Config.from_env(require_telegram=False)

    def progress(message: str) -> None:
        print(f"  {message}")

    fetcher = ProfileFetcher(
        cfg, headless=False, profile_dir=args.profile or None, progress=progress
    )
    try:
        await fetcher.start()
    except BrowserUnavailable as exc:
        print(str(exc))
        return 1
    try:
        ok = await fetcher.prime(args.slug)
    except BrowserUnavailable as exc:
        print(str(exc))
        return 1
    finally:
        await fetcher.close()
    if ok:
        print(f"Security check cleared. The profile at {cfg.wfscrape_profile} is ready.")
        print("Every later run can stay headless.")
        return 0
    print(
        "The security check did not clear. Close the browser, wait a moment and "
        "run this command again - or point --profile at a profile that already "
        "has a clearance cookie."
    )
    return 1


async def _cmd_scrape_export(args: argparse.Namespace) -> int:
    """Rebuild the snapshot from the database - no scraping."""
    from .wfscrape.runner import SOURCE_NAME, export_stored

    cfg = Config.from_env(require_telegram=False)
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    try:
        rows, paths = export_stored(cfg, store, source_name=args.source or SOURCE_NAME)
    finally:
        store.close()

    if not rows:
        print("No stored records for that source - nothing was written.")
        return 1
    print(f"{len(rows)} record(s) exported:")
    for kind in ("csv", "xlsx", "json"):
        path = paths.get(kind)
        if path:
            print(f"  {kind.upper():4} {path}")
    return 0


async def _cmd_stats(args: argparse.Namespace) -> int:
    cfg, store, service = _build()
    try:
        data = service.status()
        counts = data["counts"]
        print(f"Database: {cfg.db_path}")
        print(
            f"Total {counts['total']} | target {counts['confirmed']} | "
            f"validated {counts['validated']} | review {counts['needs_review']} | "
            f"duplicates {counts['duplicates']} | failed {counts['failed']} | "
            f"unknown size {counts['unknown_size']}"
        )
        print(f"Queue: {data['queue']} | paused: {data['paused']} | exports: {data['exports']}")
        last_run = data["last_run"]
        if last_run:
            print(
                f"Last run: {last_run.get('kind')} {last_run.get('started_at')} | "
                f"new {last_run.get('inserted')} updated {last_run.get('updated')} "
                f"failed {last_run.get('failed')}"
            )
        last_import = data["last_import"]
        if last_import:
            print(
                f"Last import: {last_import.get('file_name')} {last_import.get('imported_at')} | "
                f"new {last_import.get('inserted')} updated {last_import.get('updated')}"
            )
        print(f"Integrity: {store.integrity_check()}")
        return 0
    finally:
        await service.close()
        store.close()


async def _cmd_scan(args: argparse.Namespace) -> int:
    """Run a real scan in the foreground and print the live counters."""
    cfg = Config.from_env(require_telegram=False)
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    jobs = ScanManager(cfg, store)
    try:
        async def progress(snapshot: dict) -> None:
            counts = snapshot.get("item_counts") or {}
            finished = sum(
                int(counts.get(key) or 0)
                for key in ("done", "rejected", "failed", "skipped_duplicate")
            )
            total = int(counts.get("total") or 0)
            print(
                f"  [{snapshot.get('status')}] {finished}/{total} inspected · "
                f"eligible {snapshot.get('eligible')} · verified "
                f"{snapshot.get('websites_verified')} · incomplete "
                f"{snapshot.get('incomplete')} · errors {snapshot.get('errors')}"
            )

        async def event(kind: str, snapshot: dict, detail: str) -> None:
            print(f"  >> {kind}: {snapshot.get('job_id')} {detail}")

        jobs.progress_sink = progress
        jobs.event_sink = event
        mode = args.mode
        job = await jobs.start(mode, max_profiles=args.max or 0)
        print(f"Run {job.job_id} started ({mode}). Ctrl+C to pause and stop.")
        while True:
            await asyncio.sleep(1.0)
            snapshot = jobs.snapshot(job.job_id)
            if snapshot.get("status") in (
                "completed",
                "failed",
                "stopped",
                "interrupted",
            ):
                break
        snapshot = jobs.snapshot(job.job_id)
        print(
            f"Done: inspected {snapshot.get('inspected')} · eligible "
            f"{snapshot.get('eligible')} · verified {snapshot.get('websites_verified')} "
            f"· new verified {snapshot.get('new_verified')} · errors "
            f"{snapshot.get('errors')}"
        )
        if snapshot.get("error"):
            print(f"  ! {snapshot['error']}")
        return 0 if snapshot.get("status") == "completed" else 1
    finally:
        await jobs.close()
        store.close()


async def _cmd_runs(args: argparse.Namespace) -> int:
    cfg, store, service = _build()
    try:
        rows = store.list_scan_jobs(limit=args.limit)
        if not rows:
            print("No scans recorded yet.")
            return 0
        for row in rows:
            print(
                f"{row['job_id']}  {row['status']:<12} {row['started_at']}  "
                f"inspected {row['inspected']}  verified {row['websites_verified']}  "
                f"new {row['new_verified']}  errors {row['errors']}"
            )
        return 0
    finally:
        await service.close()
        store.close()


async def _cmd_report(args: argparse.Namespace) -> int:
    cfg, store, service = _build()
    try:
        exporter = service.exporter
        try:
            result = exporter.export_report(args.scope, args.format)
        except EmptyReportError as exc:
            print(f"Nothing to export: {exc}")
            return 1
        print(f"{result.kind.upper()}: {result.row_count} rows -> {result.path}")
        return 0
    finally:
        await service.close()
        store.close()


async def _cmd_settings(args: argparse.Namespace) -> int:
    cfg, store, service = _build()
    try:
        jobs = ScanManager(cfg, store)
        settings = jobs.settings()
        if args.set:
            key, _, value = args.set.partition("=")
            if not value:
                print("Use --set key=value (e.g. --set max_profiles=50)")
                return 2
            current = settings.to_dict()
            try:
                if key in {"retry_incomplete", "auto_report", "notifications"}:
                    current[key] = value.lower() in {"1", "true", "yes", "on"}
                elif key == "delay_seconds":
                    current[key] = float(value)
                elif key == "max_profiles":
                    current[key] = int(value)
                elif key == "max_retries":
                    current[key] = int(value)
                elif key == "nav_timeout_ms":
                    current[key] = int(value)
                else:
                    print(f"Unknown setting: {key}")
                    return 2
                jobs.save_settings(ScanSettings.from_dict(current))
            except (TypeError, ValueError) as exc:
                print(f"Invalid value: {exc}")
                return 2
            settings = jobs.settings()
        for key, value in settings.to_dict().items():
            print(f"{key} = {value}")
        return 0
    finally:
        await service.close()
        store.close()


def _cmd_init(args: argparse.Namespace) -> int:
    cfg = Config.from_env(require_telegram=False)
    cfg.ensure_dirs()
    template = Path(__file__).resolve().parents[1] / "templates" / "companies_template.csv"
    print(f"Created folders under {cfg.import_dir.parent}")
    print(f"CSV template: {template}")
    print(f"Database: {cfg.db_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Wellfound Startup Discovery Agent — local CLI"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="show debug logging on the console"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="create the data, imports and exports folders")
    init_parser.set_defaults(func=_cmd_init)

    import_parser = sub.add_parser("import", help="import a file or a folder")
    import_parser.add_argument("path")
    import_parser.set_defaults(func=_cmd_import, coro=True)

    discover_parser = sub.add_parser("discover", help="run permitted discovery")
    discover_parser.add_argument("--url", action="append", help="one URL; repeatable")
    discover_parser.add_argument("--seeds", action="store_true", help="use STARTUP_SEED_URLS")
    discover_parser.add_argument(
        "--datasets",
        action="store_true",
        help="query permitted open datasets (DBpedia) for real company records",
    )
    discover_parser.add_argument(
        "--dataset-source", default="", help="only run this dataset source (e.g. dbpedia)"
    )
    discover_parser.add_argument(
        "--dataset-limit", type=int, default=0, help="max rows per dataset query"
    )
    discover_parser.set_defaults(func=_cmd_discover, coro=True)

    export_parser = sub.add_parser("export", help="create timestamped exports")
    export_parser.add_argument(
        "--format", choices=("csv", "json", "xlsx", "all"), default="all"
    )
    export_parser.add_argument("--confirmed-only", action="store_true")
    export_parser.set_defaults(func=_cmd_export, coro=True)

    stats_parser = sub.add_parser("stats", help="show counts and integrity")
    stats_parser.set_defaults(func=_cmd_stats, coro=True)

    sheet_parser = sub.add_parser(
        "sheet", help="compact sheet: startup name, website, platform, members"
    )
    sheet_parser.add_argument(
        "--platform",
        default="",
        help="only rows from this platform (wellfound, dbpedia, manual import)",
    )
    sheet_parser.add_argument("--format", choices=("csv", "xlsx"), default="xlsx")
    sheet_parser.set_defaults(func=_cmd_sheet, coro=True)

    collect_parser = sub.add_parser(
        "collect",
        help="collect 1-10 employee startups from Wellfound in your own browser",
    )
    collect_parser.add_argument(
        "--max", type=int, default=0, help="maximum companies to visit (0 = config)"
    )
    collect_parser.set_defaults(func=_cmd_collect, coro=True)

    scan_parser = sub.add_parser(
        "scan", help="run a real scan in the background worker (same code as Telegram)"
    )
    scan_parser.add_argument(
        "--max", type=int, default=0, help="maximum profiles to inspect (0 = settings)"
    )
    scan_parser.add_argument(
        "--mode",
        choices=("new", "new_retry", "retry"),
        default="new",
        help="new companies only, new + retry incomplete, or retry incomplete only",
    )
    scan_parser.set_defaults(func=_cmd_scan, coro=True)

    runs_parser = sub.add_parser("runs", help="list the recent scan runs")
    runs_parser.add_argument("--limit", type=int, default=10)
    runs_parser.set_defaults(func=_cmd_runs, coro=True)

    report_parser = sub.add_parser("report", help="write a report file")
    report_parser.add_argument(
        "--scope", choices=("latest", "verified", "incomplete", "runs"), default="latest"
    )
    report_parser.add_argument(
        "--format", choices=("csv", "xlsx", "json", "txt"), default="xlsx"
    )
    report_parser.set_defaults(func=_cmd_report, coro=True)

    settings_parser = sub.add_parser("settings", help="show or change scan settings")
    settings_parser.add_argument(
        "--set", default="", help="change one setting, e.g. max_profiles=50"
    )
    settings_parser.set_defaults(func=_cmd_settings, coro=True)

    scrape_parser = sub.add_parser(
        "scrape",
        help="scrape Wellfound's public pages for target-sized companies (manual)",
    )
    scrape_parser.add_argument(
        "--roles", default="", help="path to the role list file (default: config/wf_scrape_roles.txt)"
    )
    scrape_parser.add_argument(
        "--pages", type=int, default=0, help="pages to read per role (0 = config)"
    )
    scrape_parser.add_argument(
        "--max", type=int, default=0, help="maximum companies to collect (0 = config)"
    )
    scrape_parser.add_argument(
        "--size", default="", help="company size to keep, e.g. 1-10 (0 = config)"
    )
    scrape_parser.add_argument(
        "--headed", action="store_true", help="show the browser window while running"
    )
    scrape_parser.add_argument(
        "--profile",
        default="",
        help="browser profile to use (default: data/wfscrape_profile)",
    )
    scrape_parser.add_argument(
        "--verify",
        action="store_true",
        help="also check each website with one polite HTTP request",
    )
    scrape_parser.set_defaults(func=_cmd_scrape, coro=True)

    prime_parser = sub.add_parser(
        "scrape-prime",
        help="clear Wellfound's security check once in a visible browser",
    )
    prime_parser.add_argument(
        "--slug", default="mattermost", help="company page used to test the profile"
    )
    prime_parser.add_argument(
        "--profile",
        default="",
        help="browser profile to warm up (default: data/wfscrape_profile)",
    )
    prime_parser.set_defaults(func=_cmd_scrape_prime, coro=True)

    export_parser = sub.add_parser(
        "scrape-export",
        help="re-export the snapshot from the database (no scraping)",
    )
    export_parser.add_argument(
        "--source",
        default="",
        help="source name to export (default: wellfound_public)",
    )
    export_parser.set_defaults(func=_cmd_scrape_export, coro=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        if getattr(args, "coro", False):
            return asyncio.run(args.func(args))
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
