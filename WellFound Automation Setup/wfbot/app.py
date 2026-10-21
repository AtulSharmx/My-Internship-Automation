"""Composition root: build every component and run them together."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers

from playwright.async_api import async_playwright

from .browser import open_context
from .config import Config, ConfigError
from .guards import ChallengeTimeout
from .runner import Runner
from .store import Store
from .telegram import Telegram, esc
from .wellfound import LoginRequired, Wellfound

log = logging.getLogger("wfbot")


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-18s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(cfg.log_level)

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        cfg.log_dir / "wfbot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_telegram(cfg: Config, store: Store, trigger: asyncio.Event) -> Telegram:
    tg = Telegram(cfg)
    tg.bind(store=store, trigger_run=trigger)
    tg.command("start")(tg.cmd_start)
    tg.command("help")(tg.cmd_help)
    tg.command("status")(tg.cmd_status)
    tg.command("info")(tg.cmd_info)
    tg.command("report")(tg.cmd_report)
    tg.command("last_applied")(tg.cmd_last_applied)
    tg.command("applications")(tg.cmd_last_applied)
    tg.command("errors")(tg.cmd_errors)
    tg.command("sync")(tg.cmd_sync)
    tg.command("responses")(tg.cmd_responses)
    tg.command("answers")(tg.cmd_answers)
    tg.command("forget")(tg.cmd_forget)
    tg.command("roles")(tg.cmd_roles)
    tg.command("goal")(tg.cmd_goal)
    tg.command("pause")(tg.cmd_pause)
    tg.command("resume")(tg.cmd_resume)
    tg.command("run")(tg.cmd_run)
    tg.command("cancel")(tg.cmd_cancel)
    return tg


async def amain() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2

    setup_logging(cfg)
    if cfg.daily_run_clock is not None:
        cadence = f"daily at {cfg.daily_run_at}"
    elif cfg.interval_minutes <= 0:
        cadence = f"continuous ({cfg.continuous_gap_seconds}s gap)"
    else:
        cadence = f"{cfg.interval_minutes}min"
    log.info("starting | roles=%s | cadence=%s | max_age=%s | search=%s | headless=%s",
             cfg.role_labels, cadence, cfg.freshness_label,
             "signed-in /jobs" if cfg.authenticated_search else "public pages",
             cfg.headless)

    store = Store(cfg.db_path)
    trigger = asyncio.Event()
    tg = build_telegram(cfg, store, trigger)
    await tg.setup_commands()

    try:
        async with async_playwright() as playwright:
            context = await open_context(playwright, cfg)
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(cfg.action_timeout_ms)

            wf = Wellfound(page, cfg, context, tg, store)
            tg._wf = wf
            try:
                await wf.ensure_logged_in()
            except (LoginRequired, ChallengeTimeout) as exc:
                await tg.send(
                    "\U0001f534 <b>Wellfound bot stopped at login</b>\n"
                    f"<i>{esc(str(exc))}</i>\n"
                    "Sign in once with <code>python setup_wizard.py --login</code> "
                    "(or solve the check in the browser window), then start the bot again."
                )
                log.error("cannot start without a Wellfound session: %s", exc)
                return 2

            history_note = ""
            if cfg.reconcile_applications:
                try:
                    sync = await wf.sync_applications()
                    if sync["imported"] or sync["updated"]:
                        history_note = (
                            f"History: {sync['imported']} manual application(s) "
                            f"imported \u00b7 {sync['updated']} history row(s) updated"
                        )
                except Exception as exc:  # noqa: BLE001 - never block startup
                    log.warning("application history sync failed: %s", exc)

            profile_note = ""
            if cfg.profile_preflight:
                try:
                    warnings = await wf.profile_warnings()
                    if warnings:
                        profile_note = "Profile warning: " + "; ".join(warnings)
                except Exception as exc:  # noqa: BLE001 - advisory only
                    log.warning("profile preflight failed: %s", exc)

            runner = Runner(
                cfg=cfg, store=store, wf=wf, notify=tg, trigger_run=trigger
            )

            await tg.startup_notice(
                history_note=history_note,
                profile_note=profile_note,
            )

            tasks = [
                asyncio.create_task(tg.run_updates(), name="telegram-updates"),
                asyncio.create_task(tg.daily_report_loop(), name="daily-report"),
                asyncio.create_task(runner.loop(), name="scheduler"),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await context.close()
    except asyncio.CancelledError:
        log.info("shutdown requested")
    finally:
        await tg.close()
        store.close()
        log.info("stopped")
    return 0
