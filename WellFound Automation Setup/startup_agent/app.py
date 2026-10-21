"""Composition root: the background scan manager, the bot and the scheduler."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
from datetime import datetime, timedelta
from typing import Iterable

from .config import Config, ConfigError
from .discovery import DiscoveryService
from .jobs import MODE_NEW, MODE_NEW_RETRY, ScanManager
from .store import Store
from .telegram import Telegram

log = logging.getLogger("startup_agent")


class SecretFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self.secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        for secret in self.secrets:
            message = message.replace(secret, "<redacted>")
        record.msg = message
        record.args = ()
        return True


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-24s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(cfg.log_level)
    root.handlers.clear()

    # The agent normally runs under pythonw.exe, which has no console at all.
    # The file handler below is what matters in that case.
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(SecretFilter([cfg.telegram_token]))
        root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        cfg.log_dir / "startup_agent.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SecretFilter([cfg.telegram_token]))
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _fmt_hhmm(value: str) -> str:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        return f"{(hour % 12) or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"
    except (TypeError, ValueError):
        return value


class DailyScheduler:
    """Optional daily discovery with one startup catch-up attempt.

    Manual imports and exports always work; this only starts a discovery run at
    the configured local time.
    """

    def __init__(
        self,
        cfg: Config,
        service: DiscoveryService,
        bot: Telegram,
        jobs: ScanManager | None = None,
    ) -> None:
        self.cfg = cfg
        self.service = service
        self.bot = bot
        self.jobs = jobs
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.cfg.daily_run_at and (
            self.cfg.enable_url_discovery
            or self.cfg.enable_datasets
            or self.cfg.enable_wellfound_browser
        ):
            self._task = asyncio.create_task(self._loop(), name="daily-discovery")

    async def _loop(self) -> None:
        hour, minute = (int(part) for part in self.cfg.daily_run_at.split(":", 1))
        first = True
        while True:
            now = datetime.now()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if first:
                first = False
                if target <= now:
                    today = now.strftime("%Y-%m-%d")
                    if self.service.store.get_meta("last_daily_run_date") != today:
                        await self._run_once()
                        continue
                    target += timedelta(days=1)
            elif target <= now:
                target += timedelta(days=1)
            delay = max(1.0, (target - now).total_seconds())
            log.info(
                "next scheduled discovery: %s local", target.strftime("%d %b %Y %I:%M %p")
            )
            await asyncio.sleep(delay)
            await self._run_once()

    async def _run_once(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.service.store.get_meta("last_daily_run_date") == today:
            return
        if self.service.store.paused():
            log.info("daily discovery skipped because the agent is paused")
            return
        if self.jobs is not None and self.cfg.enable_wellfound_browser:
            settings = self.jobs.settings()
            mode = MODE_NEW_RETRY if settings.retry_incomplete else MODE_NEW
            try:
                job = await self.jobs.start(mode)
            except Exception as exc:  # noqa: BLE001 - the scheduler must keep running
                log.warning("scheduled scan could not start: %s", exc)
                await self.bot.send(
                    "❌ The scheduled scan could not start. "
                    "Open the panel for the details.",
                    self.bot.menu_keyboard(),
                )
                return
            self.service.store.set_meta("last_daily_run_date", today)
            log.info("scheduled scan started: %s", job.job_id)
            return
        try:
            summary = await self.service.run_daily()
        except Exception:  # noqa: BLE001 - the scheduler must keep running
            log.exception("scheduled discovery failed")
            await self.bot.send(
                "❌ Scheduled discovery failed. Check the log; manual import/export still works.",
                self.bot.menu_keyboard(),
            )
            return
        self.service.store.set_meta("last_daily_run_date", today)
        await self.bot.send(self.bot._run_text(summary), self.bot.back_keyboard())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


async def _auto_scan(cfg: Config, jobs: ScanManager, bot: Telegram) -> None:
    """Start the real Wellfound workflow shortly after the agent boots."""
    await asyncio.sleep(3)
    try:
        settings = jobs.settings()
        mode = MODE_NEW_RETRY if settings.retry_incomplete else MODE_NEW
        job = await jobs.start(mode)
    except Exception as exc:  # noqa: BLE001 - never take the bot down
        log.warning("the startup scan could not start: %s", exc)
        return
    log.info("startup scan started: %s", job.job_id)
    await bot.announce_started(job)


async def amain() -> int:
    try:
        cfg = Config.from_env(require_telegram=True)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    cfg.ensure_dirs()
    setup_logging(cfg)
    log.info("starting startup discovery agent")
    log.info("config: %s", cfg.safe_summary())

    store = Store(cfg.db_path)
    service = DiscoveryService(cfg, store)
    jobs = ScanManager(cfg, store)
    bot = Telegram(cfg, service, jobs)
    scheduler = DailyScheduler(cfg, service, bot, jobs)
    background: list[asyncio.Task] = []
    try:
        # A scan that was interrupted by a restart keeps its checkpoint and is
        # resumed automatically.
        recovered = jobs.recover()
        scheduler.start()
        if recovered is not None:
            try:
                await jobs.resume_job(recovered.job_id)
            except Exception as exc:  # noqa: BLE001 - the bot must still start
                log.warning("could not resume %s: %s", recovered.job_id, exc)
                await bot.announce_recovered(recovered)
            else:
                await bot.announce_resumed(recovered)
        elif cfg.enable_wellfound_browser and cfg.startup_auto_scan:
            background.append(
                asyncio.create_task(
                    _auto_scan(cfg, jobs, bot), name="startup-scan"
                )
            )
        await bot.run()
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("shutdown requested")
        return 0
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    except Exception:
        log.exception("fatal error")
        return 1
    finally:
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        await scheduler.stop()
        await jobs.close()
        await bot.close()
        await service.close()
        store.close()
