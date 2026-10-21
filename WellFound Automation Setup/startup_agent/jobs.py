"""Background scan jobs: persistent state, checkpoints, pause/resume/stop.

The Telegram bot never runs a scan inside a request handler.  It starts a job
here; the job owns the browser, writes every result to SQLite as it goes and
keeps a checkpoint so a pause, a stop, a crash or a restart never loses work.

Job lifecycle
    queued -> running -> (paused <-> running) -> stopping -> stopped
                            \\-> completed
                            \\-> failed          (a browser/network failure)
    any running/stopping job that is found at start-up becomes
    ``interrupted``: its checkpoint is intact and it can be resumed.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .config import Config
from .models import Company, LeadStatus, WebsiteStatus, iso_now
from .normalize import evaluate_company, name_key, parse_employee_size
from .store import Store
from .verify import WebsiteCheck, WebsiteVerifier

log = logging.getLogger("startup_agent.jobs")

JOB_RUNNING = "running"
JOB_PAUSED = "paused"
JOB_STOPPING = "stopping"
JOB_STOPPED = "stopped"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_INTERRUPTED = "interrupted"

ACTIVE_STATUSES = (JOB_RUNNING, JOB_PAUSED, JOB_STOPPING)
RESUMABLE_STATUSES = (JOB_PAUSED, JOB_STOPPED, JOB_INTERRUPTED, JOB_FAILED)
TERMINAL_STATUSES = (JOB_COMPLETED, JOB_STOPPED, JOB_FAILED)

JOB_LABELS = {
    JOB_RUNNING: "🟢 Running",
    JOB_PAUSED: "⏸ Paused",
    JOB_STOPPING: "🟠 Stopping",
    JOB_STOPPED: "⏹ Stopped",
    JOB_COMPLETED: "✅ Completed",
    JOB_FAILED: "❌ Failed",
    JOB_INTERRUPTED: "⚠️ Interrupted",
}

MODE_NEW = "new"
MODE_NEW_RETRY = "new_retry"
MODE_RETRY = "retry"
MODE_LABELS = {
    MODE_NEW: "New companies only",
    MODE_NEW_RETRY: "New companies + retry incomplete",
    MODE_RETRY: "Retry incomplete only",
}


class JobBusy(RuntimeError):
    """Another scan is already running; starting a second one is refused."""


class JobError(RuntimeError):
    """The scan could not be started at all."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ScanSettings:
    """Persisted, validated scan settings (stored in the ``meta`` table)."""

    max_profiles: int = 25
    delay_seconds: float = 2.0
    max_retries: int = 2
    nav_timeout_ms: int = 45_000
    retry_incomplete: bool = True
    auto_report: bool = False
    notifications: bool = True

    MAX_PROFILES_RANGE = (1, 200)
    DELAY_RANGE = (0.5, 60.0)
    RETRIES_RANGE = (0, 5)
    TIMEOUT_RANGE = (10_000, 180_000)

    def validate(self) -> "ScanSettings":
        """Raise ``ValueError`` for anything out of range."""
        if not isinstance(self.max_profiles, int) or isinstance(self.max_profiles, bool):
            raise ValueError("Maximum profiles per run must be a whole number.")
        low, high = self.MAX_PROFILES_RANGE
        if not (low <= self.max_profiles <= high):
            raise ValueError(
                f"Maximum profiles per run must be between {low} and {high}."
            )
        try:
            delay = float(self.delay_seconds)
        except (TypeError, ValueError):
            raise ValueError("Delay between profile visits must be a number.") from None
        low, high = self.DELAY_RANGE
        if not (low <= delay <= high):
            raise ValueError(
                f"Delay between profile visits must be between {low:g} and {high:g} seconds."
            )
        self.delay_seconds = delay
        if not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool):
            raise ValueError("Maximum retry attempts must be a whole number.")
        low, high = self.RETRIES_RANGE
        if not (low <= self.max_retries <= high):
            raise ValueError(
                f"Maximum retry attempts must be between {low} and {high}."
            )
        try:
            timeout = int(self.nav_timeout_ms)
        except (TypeError, ValueError):
            raise ValueError("Page navigation timeout must be a number of milliseconds.") from None
        low, high = self.TIMEOUT_RANGE
        if not (low <= timeout <= high):
            raise ValueError(
                f"Page navigation timeout must be between {low // 1000} and {high // 1000} seconds."
            )
        self.nav_timeout_ms = timeout
        self.retry_incomplete = bool(self.retry_incomplete)
        self.auto_report = bool(self.auto_report)
        self.notifications = bool(self.notifications)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_profiles": int(self.max_profiles),
            "delay_seconds": float(self.delay_seconds),
            "max_retries": int(self.max_retries),
            "nav_timeout_ms": int(self.nav_timeout_ms),
            "retry_incomplete": bool(self.retry_incomplete),
            "auto_report": bool(self.auto_report),
            "notifications": bool(self.notifications),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ScanSettings":
        data = data or {}
        defaults = cls()
        try:
            max_profiles = int(data.get("max_profiles", defaults.max_profiles))
        except (TypeError, ValueError):
            max_profiles = defaults.max_profiles
        try:
            delay = float(data.get("delay_seconds", defaults.delay_seconds))
        except (TypeError, ValueError):
            delay = defaults.delay_seconds
        try:
            retries = int(data.get("max_retries", defaults.max_retries))
        except (TypeError, ValueError):
            retries = defaults.max_retries
        try:
            timeout = int(data.get("nav_timeout_ms", defaults.nav_timeout_ms))
        except (TypeError, ValueError):
            timeout = defaults.nav_timeout_ms
        return cls(
            max_profiles=max_profiles,
            delay_seconds=delay,
            max_retries=retries,
            nav_timeout_ms=timeout,
            retry_incomplete=bool(data.get("retry_incomplete", defaults.retry_incomplete)),
            auto_report=bool(data.get("auto_report", defaults.auto_report)),
            notifications=bool(data.get("notifications", defaults.notifications)),
        )

    @classmethod
    def load(cls, store: Store, cfg: Config | None = None) -> "ScanSettings":
        raw = store.get_meta("scan_settings", "")
        if raw:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                data = {}
            if isinstance(data, dict):
                return cls.from_dict(data)
        defaults = cls()
        if cfg is not None:
            defaults.max_profiles = max(
                cls.MAX_PROFILES_RANGE[0],
                min(cls.MAX_PROFILES_RANGE[1], int(cfg.wellfound_max_companies)),
            )
        return defaults

    def save(self, store: Store) -> None:
        store.set_meta("scan_settings", json.dumps(self.to_dict()))


# ---------------------------------------------------------------------------
# Job record
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ScanJob:
    job_id: str
    mode: str = MODE_NEW
    status: str = JOB_RUNNING
    started_at: str = ""
    finished_at: str = ""
    updated_at: str = ""
    max_profiles: int = 25
    settings: dict[str, Any] = field(default_factory=dict)
    plan_ready: bool = False
    discovered: int = 0
    inspected: int = 0
    eligible: int = 0
    rejected: int = 0
    duplicates_skipped: int = 0
    websites_verified: int = 0
    incomplete: int = 0
    errors: int = 0
    retries: int = 0
    new_verified: int = 0
    inserted: int = 0
    updated: int = 0
    recovered: int = 0
    note: str = ""
    error: str = ""
    last_profile: str = ""
    item_counts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict[str, Any] | None) -> "ScanJob | None":
        if not row:
            return None
        try:
            settings = json.loads(row.get("settings_json") or "{}")
        except (TypeError, ValueError):
            settings = {}
        return cls(
            job_id=str(row.get("job_id") or ""),
            mode=str(row.get("mode") or MODE_NEW),
            status=str(row.get("status") or JOB_RUNNING),
            started_at=str(row.get("started_at") or ""),
            finished_at=str(row.get("finished_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            max_profiles=int(row.get("max_profiles") or 0),
            settings=settings if isinstance(settings, dict) else {},
            plan_ready=bool(row.get("plan_ready")),
            discovered=int(row.get("discovered") or 0),
            inspected=int(row.get("inspected") or 0),
            eligible=int(row.get("eligible") or 0),
            rejected=int(row.get("rejected") or 0),
            duplicates_skipped=int(row.get("duplicates_skipped") or 0),
            websites_verified=int(row.get("websites_verified") or 0),
            incomplete=int(row.get("incomplete") or 0),
            errors=int(row.get("errors") or 0),
            retries=int(row.get("retries") or 0),
            new_verified=int(row.get("new_verified") or 0),
            inserted=int(row.get("inserted") or 0),
            updated=int(row.get("updated") or 0),
            recovered=int(row.get("recovered") or 0),
            note=str(row.get("note") or ""),
            error=str(row.get("error") or ""),
            last_profile=str(row.get("last_profile") or ""),
        )

    @property
    def label(self) -> str:
        return JOB_LABELS.get(self.status, self.status)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def resumable(self) -> bool:
        return self.status in RESUMABLE_STATUSES

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode)

    def elapsed_seconds(self) -> float:
        start = self.started_at
        end = self.finished_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            begin = datetime.fromisoformat(start.replace("Z", "+00:00"))
            finish = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, (finish - begin).total_seconds())

    def remaining(self) -> int:
        counts = self.item_counts or {}
        total = int(counts.get("total") or 0)
        done = sum(
            int(counts.get(key) or 0)
            for key in ("done", "rejected", "failed", "skipped_duplicate")
        )
        return max(0, total - done)

    def eta_seconds(self) -> float | None:
        """A rough remaining time, only when the numbers support one."""
        remaining = self.remaining()
        if remaining <= 0 or self.inspected <= 0 or self.status != JOB_RUNNING:
            return None
        per_item = self.elapsed_seconds() / max(1, self.inspected)
        estimate = per_item * remaining
        if estimate <= 0 or estimate > 24 * 3600:
            return None
        return estimate


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    """True when a process with that id is still *running*.

    On Windows a terminated process can stay in the process table while a
    handle to it is still open, so the exit code is checked as well.
    """
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows specific
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(code)
            ):
                return True
            return code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:  # pragma: no cover - POSIX
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class ScanManager:
    """Owns the browser and runs one scan job at a time."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        *,
        collector_factory: Callable[[ScanSettings], Any] | None = None,
        verifier: WebsiteVerifier | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self._collector_factory = collector_factory or self._default_collector
        self.verifier = verifier or WebsiteVerifier(
            timeout=min(30.0, max(5.0, float(cfg.request_timeout_seconds)))
        )
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._stop_flag = False
        self._rate_limit_reported = False
        self._errors_reported = False
        self.progress_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self.event_sink: Callable[[str, dict[str, Any], str], Awaitable[None]] | None = None

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def settings(self) -> ScanSettings:
        return ScanSettings.load(self.store, self.cfg)

    def save_settings(self, settings: ScanSettings) -> ScanSettings:
        settings.validate()
        settings.save(self.store)
        return settings

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def get_job(self, job_id: str) -> ScanJob | None:
        return ScanJob.from_row(self.store.get_scan_job(job_id))

    def active_job(self) -> ScanJob | None:
        return ScanJob.from_row(self.store.active_scan_job())

    def latest_job(self) -> ScanJob | None:
        return ScanJob.from_row(self.store.latest_scan_job())

    def list_jobs(self, limit: int = 8) -> list[ScanJob]:
        return [
            job
            for job in (
                ScanJob.from_row(row) for row in self.store.list_scan_jobs(limit)
            )
            if job is not None
        ]

    def snapshot(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            return {}
        job.item_counts = self.store.scan_item_counts(job_id)
        return {
            "job_id": job.job_id,
            "mode": job.mode,
            "status": job.status,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "updated_at": job.updated_at,
            "max_profiles": job.max_profiles,
            "discovered": job.discovered,
            "inspected": job.inspected,
            "eligible": job.eligible,
            "rejected": job.rejected,
            "duplicates_skipped": job.duplicates_skipped,
            "websites_verified": job.websites_verified,
            "incomplete": job.incomplete,
            "errors": job.errors,
            "retries": job.retries,
            "new_verified": job.new_verified,
            "inserted": job.inserted,
            "updated": job.updated,
            "note": job.note,
            "error": job.error,
            "last_profile": job.last_profile,
            "item_counts": job.item_counts,
        }

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    async def start(
        self,
        mode: str = MODE_NEW,
        settings: ScanSettings | None = None,
        *,
        max_profiles: int = 0,
    ) -> ScanJob:
        """Start a background scan.  Raises ``JobBusy`` if one is active."""
        async with self._lock:
            active = self.store.active_scan_job()
            if active is not None:
                raise JobBusy(
                    f"Run {active['job_id']} is already {active['status']}. "
                    "Stop or pause it before starting another."
                )
            if self._task is not None and not self._task.done():
                raise JobBusy("A scan is already running.")
            owner_job, owner_pid = self._other_browser_owner()
            if owner_pid:
                raise JobBusy(
                    "Another agent process (pid "
                    f"{owner_pid}) is driving the browser profile. Stop it "
                    "before starting a new scan."
                )
            if not self.cfg.enable_wellfound_browser:
                raise JobError(
                    "The Wellfound browser collector is off. Set "
                    "STARTUP_WELLFOUND_BROWSER=true and restart the agent."
                )
            current = ScanSettings.from_dict((settings or self.settings()).to_dict())
            if max_profiles:
                current.max_profiles = int(max_profiles)
            current.validate()
            job_id = self._new_job_id()
            self.store.create_scan_job(
                job_id,
                mode=mode,
                max_profiles=current.max_profiles,
                settings_json=json.dumps(current.to_dict()),
            )
            self._stop_flag = False
            self._task = asyncio.create_task(self._run(job_id), name=f"scan-{job_id}")
            return self.get_job(job_id)  # type: ignore[return-value]

    async def resume_job(self, job_id: str) -> ScanJob:
        """Resume an unfinished run from its checkpoint."""
        async with self._lock:
            job = self.get_job(job_id)
            if job is None:
                raise JobError(f"Run {job_id} was not found.")
            if job.status == JOB_COMPLETED:
                raise JobError(
                    f"Run {job_id} is already complete - start a new scan instead."
                )
            if not job.resumable:
                raise JobError(f"Run {job_id} is {job.status} and cannot be resumed.")
            owner_job, owner_pid = self._other_browser_owner()
            if owner_pid:
                raise JobBusy(
                    "Another agent process (pid "
                    f"{owner_pid}) is driving the browser profile. Stop it "
                    "before resuming a run here."
                )
            self.store.reset_processing_items(job_id)
            self.store.update_scan_job(
                job_id, status=JOB_RUNNING, error="", note="Resumed from the checkpoint."
            )
            if self._task is None or self._task.done():
                self._stop_flag = False
                self._task = asyncio.create_task(self._run(job_id), name=f"scan-{job_id}")
            return self.get_job(job_id)  # type: ignore[return-value]

    async def pause(self) -> ScanJob | None:
        """Pause the running scan.  The worker stops at the next safe point."""
        job = self.store.active_scan_job()
        if job is None:
            return None
        job_id = str(job["job_id"])
        if job["status"] == JOB_PAUSED:
            return self.get_job(job_id)
        if job["status"] == JOB_STOPPING:
            return self.get_job(job_id)
        self.store.update_scan_job(
            job_id,
            status=JOB_PAUSED,
            note=(
                "Paused by the owner. The profile that was open was finished, the "
                "checkpoint was saved and the browser was kept open."
            ),
        )
        await self._progress(job_id)
        return self.get_job(job_id)

    async def resume(self) -> ScanJob | None:
        """Resume the active run, or the newest unfinished one."""
        job = self.store.active_scan_job()
        if job is None:
            resumable = self.store.resumable_scan_jobs()
            if not resumable:
                return None
            job = resumable[0]
        return await self.resume_job(str(job["job_id"]))

    async def request_stop(self) -> ScanJob | None:
        """Ask the worker to stop.  The run is saved and marked ``stopped``."""
        job = self.store.active_scan_job()
        if job is None:
            return None
        job_id = str(job["job_id"])
        self._stop_flag = True
        if job["status"] == JOB_PAUSED:
            # Nothing is in flight, so the run can be closed immediately.
            self.store.update_scan_job(
                job_id,
                status=JOB_STOPPED,
                finished_at=iso_now(),
                note="Stopped while paused. Every record collected so far was kept.",
            )
            await self._progress(job_id)
            await self._event("stopped", self.snapshot(job_id), "")
            return self.get_job(job_id)
        self.store.update_scan_job(
            job_id,
            status=JOB_STOPPING,
            note="Stopping: the profile that is open is finished, then the run is saved.",
        )
        await self._progress(job_id)
        return self.get_job(job_id)

    def recover(self) -> ScanJob | None:
        """Mark a job that was interrupted by a restart as resumable."""
        active = self.store.unfinished_scan_job()
        if active is None:
            self.store.set_meta("browser_owner", "")
            return None
        job_id = str(active["job_id"])
        self.store.reset_processing_items(job_id)
        self.store.update_scan_job(
            job_id,
            status=JOB_INTERRUPTED,
            recovered=1,
            note=(
                "The application restarted during this scan. The checkpoint is "
                "intact - resume to continue where it stopped."
            ),
        )
        log.info("recovered interrupted scan %s", job_id)
        return self.get_job(job_id)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _default_collector(self, settings: ScanSettings):
        from .sources.wellfound_browser import WellfoundBrowserCollector

        delay = max(0.5, float(settings.delay_seconds))
        return WellfoundBrowserCollector(
            self.cfg,
            nav_timeout_ms=settings.nav_timeout_ms,
            min_delay=round(delay * 0.6, 2),
            max_delay=round(delay * 1.4, 2),
        )

    def _new_job_id(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return f"S-{stamp}-{secrets.token_hex(2)}"

    # ------------------------------------------------------------------
    # Browser ownership
    # ------------------------------------------------------------------
    def _browser_owner(self) -> tuple[str, int]:
        """The job id and pid of the process that owns the browser."""
        raw = self.store.get_meta("browser_owner", "")
        if not raw:
            return "", 0
        job_id, _, pid = raw.partition(":")
        try:
            return job_id, int(pid or 0)
        except (TypeError, ValueError):
            return job_id, 0

    def _other_browser_owner(self) -> tuple[str, int]:
        """Another *live* process is driving the browser profile."""
        job_id, pid = self._browser_owner()
        if not pid or pid == os.getpid():
            return "", 0
        if not _pid_alive(pid):
            # The owner died; the lock is stale.
            self.store.set_meta("browser_owner", "")
            return "", 0
        return job_id, pid

    def _claim_browser(self, job_id: str) -> None:
        self.store.set_meta("browser_owner", f"{job_id}:{os.getpid()}")

    def _release_browser(self, job_id: str) -> None:
        owner, _ = self._browser_owner()
        if not owner or owner == job_id:
            self.store.set_meta("browser_owner", "")

    async def _progress(self, job_id: str) -> None:
        if self.progress_sink is None:
            return
        try:
            await self.progress_sink(self.snapshot(job_id))
        except Exception:  # noqa: BLE001 - progress must never break a run
            log.debug("progress sink failed", exc_info=True)

    async def _event(self, kind: str, job: dict[str, Any], detail: str) -> None:
        if self.event_sink is None:
            return
        try:
            await self.event_sink(kind, job, detail)
        except Exception:  # noqa: BLE001 - notifications must never break a run
            log.debug("event sink failed", exc_info=True)

    async def _run(self, job_id: str) -> None:
        from .sources.wellfound_browser import BrowserError, ChallengeError

        job = self.get_job(job_id)
        if job is None:
            return
        settings = ScanSettings.from_dict(job.settings)
        collector = None
        try:
            self.store.update_scan_job(job_id, status=JOB_RUNNING, error="")
            self._claim_browser(job_id)
            await self._progress(job_id)

            collector = self._collector_factory(settings)
            await collector.start()
            await self._progress(job_id)

            if not job.plan_ready:
                finished = await self._plan(job, collector, settings)
                if finished:
                    return

            await self._process_items(job, collector, settings)

            stopped = self._stop_flag or (
                (self.store.get_scan_job(job_id) or {}).get("status") == JOB_STOPPING
            )
            if stopped:
                self.store.update_scan_job(
                    job_id,
                    status=JOB_STOPPED,
                    finished_at=iso_now(),
                    note=(
                        "Stopped by the owner. Every record collected so far was "
                        "saved and the run can be resumed."
                    ),
                )
                await self._progress(job_id)
                await self._event("stopped", self.snapshot(job_id), "")
                return

            counts = self.store.scan_item_counts(job_id)
            note = "Finished."
            if not counts.get("total"):
                note = (
                    "No profiles matched the 1-10 employees filter, so nothing "
                    "was inspected."
                )
            elif counts.get("failed"):
                note = (
                    f"Finished with {counts['failed']} profile(s) that could not "
                    "be read. They stay on the retry list."
                )
            self.store.update_scan_job(
                job_id, status=JOB_COMPLETED, finished_at=iso_now(), note=note
            )
            await self._progress(job_id)
            await self._event("completed", self.snapshot(job_id), note)
        except ChallengeError as exc:
            log.warning("scan %s needs manual help: %s", job_id, exc)
            self.store.update_scan_job(
                job_id,
                status=JOB_PAUSED,
                error=str(exc),
                note=(
                    "Paused at a verification/sign-in page. Complete it in the "
                    "open browser window, then resume the run."
                ),
            )
            await self._progress(job_id)
            await self._event("auth", self.snapshot(job_id), str(exc))
        except BrowserError as exc:
            log.warning("scan %s failed: %s", job_id, exc)
            self.store.update_scan_job(
                job_id,
                status=JOB_FAILED,
                error=str(exc),
                finished_at=iso_now(),
                note="The browser could not be driven. The checkpoint was kept.",
            )
            await self._progress(job_id)
            await self._event("failed", self.snapshot(job_id), str(exc))
        except asyncio.CancelledError:
            self.store.update_scan_job(
                job_id,
                status=JOB_INTERRUPTED,
                note="The application stopped during this scan. The checkpoint is intact.",
            )
            log.info("scan %s interrupted by shutdown", job_id)
            raise
        except Exception as exc:  # noqa: BLE001 - the agent must stay alive
            log.exception("scan %s failed unexpectedly", job_id)
            self.store.update_scan_job(
                job_id,
                status=JOB_FAILED,
                error=f"{type(exc).__name__}: {exc}",
                finished_at=iso_now(),
                note="The run stopped on an unexpected error. The checkpoint was kept.",
            )
            await self._progress(job_id)
            await self._event("failed", self.snapshot(job_id), f"{type(exc).__name__}: {exc}")
        finally:
            if collector is not None:
                try:
                    await collector.close()
                except Exception:  # noqa: BLE001
                    log.debug("closing the collector failed", exc_info=True)
            self._release_browser(job_id)
            self._task = None

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    async def _plan(
        self, job: ScanJob, collector, settings: ScanSettings
    ) -> bool:
        """Build the work queue: discovery cards and/or retry candidates.

        Returns True when the run is already finished (nothing to inspect).
        """
        job_id = job.job_id
        items: list[dict[str, Any]] = []
        skipped = 0
        discovered = 0

        retry_rows: list[Company] = []
        web_rows: list[Company] = []
        if job.mode in (MODE_RETRY, MODE_NEW_RETRY):
            retry_rows = self.store.incomplete_companies(limit=settings.max_profiles)
            web_rows = self.store.unverified_web_companies(limit=settings.max_profiles)

        new_budget = settings.max_profiles
        if job.mode == MODE_NEW_RETRY:
            retry_budget = max(1, settings.max_profiles // 2)
            new_budget = max(0, settings.max_profiles - retry_budget)
        elif job.mode == MODE_RETRY:
            new_budget = 0

        if job.mode in (MODE_RETRY, MODE_NEW_RETRY):
            candidates: list[tuple[str, Company]] = [("retry", row) for row in retry_rows]
            candidates.extend(("verify", row) for row in web_rows)
            if job.mode == MODE_NEW_RETRY:
                retry_budget = min(len(candidates), max(1, settings.max_profiles // 2))
            else:
                retry_budget = min(len(candidates), settings.max_profiles)
            for kind, row in candidates[:retry_budget]:
                if kind == "retry":
                    items.append(
                        {
                            "profile_url": row.wellfound_url,
                            "record_id": row.record_id,
                            "name": row.name,
                            "card_size": row.employee_count,
                            "website": row.website,
                            "origin": "retry",
                            "status": "pending",
                        }
                    )
                else:
                    # No Wellfound profile: the stored website is simply
                    # checked again, no browser needed.
                    items.append(
                        {
                            "profile_url": f"verify:{row.record_id}",
                            "record_id": row.record_id,
                            "name": row.name,
                            "card_size": row.employee_count,
                            "website": row.website,
                            "origin": "verify",
                            "status": "pending",
                        }
                    )

        if new_budget > 0:
            cards = await collector.discover_profiles(new_budget)
            discovered = len(cards)
            for card in cards:
                existing = self.store.company_by_wellfound_url(card.profile_url)
                if existing is not None and existing.lead_status == LeadStatus.VERIFIED:
                    skipped += 1
                    items.append(
                        {
                            "profile_url": card.profile_url,
                            "name": card.name,
                            "card_size": card.size,
                            "origin": "discovery",
                            "status": "skipped_duplicate",
                        }
                    )
                    continue
                items.append(
                    {
                        "profile_url": card.profile_url,
                        "name": card.name,
                        "card_size": card.size,
                        "origin": "discovery",
                        "status": "pending",
                    }
                )

        if job.mode == MODE_RETRY and not items:
            note = "There are no incomplete leads left to retry."
            self.store.update_scan_job(
                job_id,
                plan_ready=1,
                discovered=discovered,
                note=note,
                status=JOB_COMPLETED,
                finished_at=iso_now(),
            )
            await self._progress(job_id)
            await self._event("completed", self.snapshot(job_id), note)
            return True

        items = items[: settings.max_profiles]
        self.store.add_scan_items(job_id, items)
        self.store.update_scan_job(
            job_id,
            plan_ready=1,
            discovered=discovered,
            duplicates_skipped=skipped,
        )
        await self._progress(job_id)
        return False

    # ------------------------------------------------------------------
    # Item processing
    # ------------------------------------------------------------------
    async def _process_items(
        self, job: ScanJob, collector, settings: ScanSettings
    ) -> None:
        job_id = job.job_id
        while True:
            if self._stop_flag:
                return
            await self._wait_while_paused(job_id)
            if self._stop_flag:
                return
            row = (self.store.get_scan_job(job_id) or {}).get("status")
            if row in (JOB_STOPPING, JOB_STOPPED):
                return
            item = self.store.next_pending_scan_item(job_id)
            if item is None:
                return
            self.store.claim_scan_item(job_id, str(item["profile_url"]))
            self.store.update_scan_job(job_id, last_profile=str(item["profile_url"]))
            await self._progress(job_id)
            await self._process_item(job, item, collector, settings)
            await self._progress(job_id)

    async def _wait_while_paused(self, job_id: str) -> None:
        """Block the worker while the run is paused (this is the real pause)."""
        while not self._stop_flag:
            row = self.store.get_scan_job(job_id)
            if row is None:
                return
            status = str(row.get("status") or "")
            if status != JOB_PAUSED:
                return
            await asyncio.sleep(0.4)

    async def _process_item(
        self, job: ScanJob, item: dict[str, Any], collector, settings: ScanSettings
    ) -> None:
        from .sources.wellfound_browser import ChallengeError

        job_id = job.job_id
        profile_url = str(item["profile_url"])
        card_name = str(item.get("name") or "")
        card_size = str(item.get("card_size") or "")
        origin = str(item.get("origin") or "discovery")
        attempts = int(item.get("attempts") or 0) + 1

        if origin == "verify":
            await self._verify_stored_website(job, item, settings)
            return

        try:
            data = await collector.inspect_profile(profile_url)
        except ChallengeError:
            raise
        except Exception as exc:  # noqa: BLE001 - one page must not kill the run
            message = f"{type(exc).__name__}: {exc}"
            if attempts <= settings.max_retries:
                self.store.finish_scan_item(
                    job_id, profile_url, status="pending", last_error=message
                )
                self.store.bump_scan_job(job_id, errors=1, retries=1)
                await self._progress(job_id)
                await asyncio.sleep(min(5.0, 1.5 * attempts))
                return
            self.store.finish_scan_item(
                job_id, profile_url, status="failed", last_error=message
            )
            self.store.bump_scan_job(job_id, errors=1)
            await self._maybe_warn_errors(job_id)
            await self._progress(job_id)
            return

        self.store.bump_scan_job(job_id, inspected=1)

        size_text = str(getattr(data, "size", "") or "")
        if not parse_employee_size(size_text).known:
            size_text = card_size
        website = str(getattr(data, "website", "") or "")
        # The card heading is the authoritative company name; the profile
        # title carries a "careers" suffix, so it is only a fallback.
        name = _clean_profile_name(card_name)
        if not name or _looks_like_url(name):
            cleaned = _clean_profile_name(str(getattr(data, "name", "") or ""))
            if cleaned and not _looks_like_url(cleaned):
                name = cleaned
        if not name or _looks_like_url(name):
            host = _host_of(website)
            name = host or card_name
        if not name:
            self.store.finish_scan_item(
                job_id, profile_url, status="failed", last_error="no company name found"
            )
            self.store.bump_scan_job(job_id, errors=1)
            return

        existing = self.store.company_by_wellfound_url(profile_url)
        company = Company(
            record_id="",
            name=name,
            website=website,
            employee_count=size_text,
            industry="",
            description=str(getattr(data, "tagline", "") or ""),
            wellfound_url=profile_url,
            location="",
            source_url=profile_url,
            source_name="wellfound",
            source_id=profile_url.rstrip("/").split("/")[-1],
            last_run_id=job_id,
            discovered_run_id=(existing.discovered_run_id if existing else "") or job_id,
            retry_count=(existing.retry_count if existing else 0)
            + (1 if origin == "retry" else 0),
            notes=(
                "Collected from the Wellfound company profile in your own browser "
                "session; headcount and website are as published there."
            ),
            raw={
                "wellfound_profile": profile_url,
                "card_size": card_size,
                "website_source": str(getattr(data, "website_source", "") or ""),
                "origin": origin,
            },
        )
        company.name_key = name_key(company.name)
        evaluate_company(company, self.cfg.target_size_min, self.cfg.target_size_max)

        if not company.size_match:
            # Explicitly outside the range, or missing/ambiguous: rejected.
            company.website_status = WebsiteStatus.NOT_CHECKED
            company.lead_status = LeadStatus.DISCOVERED
            self._save_company(company, profile_url)
            self.store.bump_scan_job(job_id, rejected=1)
            self.store.finish_scan_item(
                job_id,
                profile_url,
                status="rejected",
                employee_count=company.employee_count,
            )
            return

        check: WebsiteCheck
        if company.website:
            check = await self.verifier.verify(company.website, company_name=company.name)
        else:
            check = WebsiteCheck(WebsiteStatus.MISSING, reason="no website listed")
        company.website_status = check.status
        company.website_final_url = check.final_url
        company.website_evidence = f"Wellfound company profile · {check.evidence}"
        company.website_checked_at = iso_now()
        company.lead_status = (
            LeadStatus.VERIFIED if check.ok else LeadStatus.INCOMPLETE
        )

        was_verified = bool(
            existing is not None and existing.lead_status == LeadStatus.VERIFIED
        )
        action = self._save_company(company, profile_url)
        if action == "inserted":
            self.store.bump_scan_job(job_id, inserted=1)
        elif action == "updated":
            self.store.bump_scan_job(job_id, updated=1)
        elif action == "duplicate":
            self.store.bump_scan_job(job_id, duplicates_skipped=1)

        self.store.bump_scan_job(job_id, eligible=1)
        if check.ok:
            self.store.bump_scan_job(job_id, websites_verified=1)
            if not was_verified:
                self.store.bump_scan_job(job_id, new_verified=1)
        else:
            self.store.bump_scan_job(job_id, incomplete=1)
            if "429" in str(check.reason) and not self._rate_limit_reported:
                self._rate_limit_reported = True
                await self._event(
                    "rate_limit",
                    self.snapshot(job_id),
                    "A website answered with HTTP 429 (too many requests). "
                    "The run keeps going at a slower pace.",
                )

        self.store.finish_scan_item(
            job_id,
            profile_url,
            status="done",
            website=company.website,
            website_status=company.website_status,
            employee_count=company.employee_count,
        )

    async def _maybe_warn_errors(self, job_id: str) -> None:
        """Tell the owner once when a run keeps hitting errors."""
        if self._errors_reported:
            return
        row = self.store.get_scan_job(job_id) or {}
        errors = int(row.get("errors") or 0)
        if errors < 5:
            return
        self._errors_reported = True
        await self._event(
            "errors",
            self.snapshot(job_id),
            f"{errors} profiles could not be read in a row. The run continues, "
            "but the failures stay on the retry list.",
        )

    async def _verify_stored_website(
        self, job: ScanJob, item: dict[str, Any], settings: ScanSettings
    ) -> None:
        """Re-check a stored website that has no Wellfound profile to visit."""
        job_id = job.job_id
        record_id = str(item.get("record_id") or "")
        profile_url = str(item.get("profile_url") or "")
        record = self.store.get_company(record_id)
        if record is None:
            self.store.finish_scan_item(
                job_id, profile_url, status="failed", last_error="record not found"
            )
            self.store.bump_scan_job(job_id, errors=1)
            return
        if not record.website:
            self.store.finish_scan_item(
                job_id, profile_url, status="failed", last_error="no website to check"
            )
            self.store.bump_scan_job(job_id, errors=1, incomplete=1)
            return
        check = await self.verifier.verify(record.website, company_name=record.name)
        company = copy.copy(record)
        company.website_status = check.status
        company.website_final_url = check.final_url
        company.website_evidence = f"{record.source_name or 'stored record'} · {check.evidence}"
        company.website_checked_at = iso_now()
        company.lead_status = (
            LeadStatus.VERIFIED if check.ok else LeadStatus.INCOMPLETE
        )
        company.last_run_id = job_id
        company.retry_count = record.retry_count + 1
        was_verified = record.lead_status == LeadStatus.VERIFIED
        action = self._save_company(company, record.wellfound_url or record.source_url)
        if action == "inserted":
            self.store.bump_scan_job(job_id, inserted=1)
        elif action == "updated":
            self.store.bump_scan_job(job_id, updated=1)
        self.store.bump_scan_job(job_id, inspected=1, eligible=1)
        if check.ok:
            self.store.bump_scan_job(job_id, websites_verified=1)
            if not was_verified:
                self.store.bump_scan_job(job_id, new_verified=1)
        else:
            self.store.bump_scan_job(job_id, incomplete=1)
        self.store.finish_scan_item(
            job_id,
            profile_url,
            status="done",
            website=company.website,
            website_status=company.website_status,
            employee_count=company.employee_count,
        )

    def _save_company(self, company: Company, profile_url: str) -> str:
        result = self.store.upsert_company(
            company,
            source_name="wellfound",
            source_url=profile_url,
            source_id=profile_url.rstrip("/").split("/")[-1],
            raw=company.raw,
        )
        return result.action

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


class _PlanComplete(Exception):
    """Kept for backwards compatibility; planning now returns a flag."""


def _looks_like_url(value: str) -> bool:
    text = (value or "").strip().lower()
    if text.startswith(("http://", "https://", "www.")):
        return True
    return "/" in text and "." in text


def _clean_profile_name(value: str) -> str:
    """'Langslide Careers - Insights' -> 'Langslide'."""
    text = (value or "").strip()
    parts = re.split(r"\s+careers\b", text, flags=re.IGNORECASE)
    cleaned = parts[0].strip(" -–—|·")[:200]
    return cleaned or text[:200]


def _host_of(url: str) -> str:
    if not url:
        return ""
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":", 1)[0]
