"""The daily loop: discover fresh listings, apply with human pacing, report."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from math import inf

from . import guards, human
from .config import ROLE_LIBRARY, Config
from .models import Job, Outcome, RunSummary, RuntimeState, Status, utcnow
from .store import Store
from .wellfound import LoginRequired, Wellfound

log = logging.getLogger("wfbot.runner")

#: Statuses where something was actually submitted (a full human pause is due
#: after these; plain skips only get a short beat).
SUBMITTED_STATUSES = frozenset(
    {Status.APPLIED, Status.UNVERIFIED, Status.EXTERNAL, Status.NEEDS_REVIEW}
)


class Runner:
    def __init__(
        self,
        *,
        cfg: Config,
        store: Store,
        wf: Wellfound,
        notify,
        trigger_run: asyncio.Event,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.wf = wf
        self.notify = notify
        self.trigger = trigger_run
        self.state: RuntimeState = notify.state

    # ------------------------------------------------------------------
    # one run
    # ------------------------------------------------------------------
    async def run_once(self) -> RunSummary:
        summary = RunSummary()
        await self.notify.run_started()

        # The signed-in search is the primary discovery path, so verify the
        # session before searching. Public role pages can still be read
        # signed-out as a fallback.
        if (
            self.cfg.authenticated_search
            and not summary.stopped_reason
            and not await self._ensure_login(summary)
        ):
            summary.stopped_reason = summary.stopped_reason or "login unavailable"

        fresh = (
            []
            if summary.stopped_reason
            else await self._collect_candidates(summary)
        )
        summary.candidates = len(fresh)

        # Every application also needs the session. This is a no-op when the
        # pre-search check above already succeeded.
        if fresh and not summary.stopped_reason and not await self._ensure_login(summary):
            summary.stopped_reason = summary.stopped_reason or "login unavailable"

        if not summary.stopped_reason:
            await self._apply_candidates(fresh, summary)
            if not summary.stopped_reason and not fresh:
                summary.stopped_reason = "no new listings"

        summary.finished_at = utcnow()
        self._update_backoff(summary)
        self.store.save_run(summary)
        return summary

    async def _apply_candidates(self, fresh: list[Job], summary: RunSummary) -> None:
        """Apply to every fresh listing (``0`` means unlimited).

        Candidates arrive newest-first. Only *submitted* applications consume
        the optional per-run budget, so a batch of too-old / too-big /
        wrong-location listings cannot silently use up the slots. Page opens
        are unlimited by default too; setting ``WF_MAX_SIZE_CHECKS_PER_RUN``
        or the app caps to a positive number restores the old safety limits.

        Company size and age are only visible on the listing page, so the first
        pass defers 11-50 employee companies (``Status.DEFERRED_SIZE``, not
        recorded and therefore retried in a later run). If the pass applies to
        nothing smaller, the deferred jobs get a second pass when
        ``WF_ALLOW_SIZE_FALLBACK`` is on.
        """
        limit: float = self.cfg.max_apps_per_run or inf
        if self.cfg.max_apps_per_day > 0:
            remaining_today = self.cfg.max_apps_per_day - self.store.applied_today()
            if remaining_today <= 0:
                summary.stopped_reason = (
                    f"daily cap of {self.cfg.max_apps_per_day} applications reached"
                )
                return
            limit = min(limit, remaining_today)
        max_checks: float = self.cfg.max_size_checks_per_run or inf
        if limit != inf:
            # Even when page opens are unlimited, keep enough headroom to reach
            # the application limit after skipped listings.
            max_checks = max(max_checks, limit)
        deferred: list[Job] = []
        applied = 0
        checked = 0
        index = 0

        while (
            index < len(fresh)
            and applied < limit
            and checked < max_checks
            and not summary.stopped_reason
        ):
            job = fresh[index]
            index += 1
            checked += 1
            if self.store.paused:
                summary.stopped_reason = "paused by operator"
                break

            outcome = await self._attempt(job, role_for(job), summary, allow_fallback=False)
            if outcome.status == Status.DEFERRED_SIZE:
                log.info("deferred %s (%s)", job.title, job.company_size)
                deferred.append(job)
                await self._cool_down(
                    summary, more_to_come=index < len(fresh), submitted=False
                )
                continue

            if outcome.status in SUBMITTED_STATUSES:
                applied += 1
            await self._record_outcome(outcome, summary)
            if summary.stopped_reason:
                break
            await self._cool_down(
                summary,
                more_to_come=(applied < limit and checked < max_checks),
                submitted=outcome.status in SUBMITTED_STATUSES,
            )

        if (
            not summary.stopped_reason
            and deferred
            and applied < limit
            and self.cfg.allow_size_fallback
        ):
            log.info("%d small-company attempts, using %d fallback listing(s)",
                     applied, len(deferred))
            for job in deferred:
                if applied >= limit or summary.stopped_reason:
                    break
                if self.store.paused:
                    summary.stopped_reason = "paused by operator"
                    break

                outcome = await self._attempt(job, role_for(job), summary, allow_fallback=True)
                if outcome.status == Status.DEFERRED_SIZE:
                    continue
                if outcome.status in SUBMITTED_STATUSES:
                    applied += 1
                await self._record_outcome(outcome, summary)
                if summary.stopped_reason:
                    break
                await self._cool_down(
                    summary,
                    more_to_come=(applied < limit),
                    submitted=outcome.status in SUBMITTED_STATUSES,
                )

    async def _record_outcome(self, outcome: Outcome, summary: RunSummary) -> None:
        summary.add(outcome)
        self.store.record(outcome)
        await self.notify.outcome(outcome.status, outcome.job, outcome.detail)
        if outcome.status == Status.APPLIED:
            await self._celebrate_goal()

    async def _celebrate_goal(self) -> None:
        """Send one celebration per goal value, the moment it is reached."""
        goal = int(self.store.get_meta("goal_applied", str(self.cfg.apply_goal)) or 0)
        if goal <= 0:
            return
        done = int(self.store.counts().get(Status.APPLIED, 0))
        if done < goal or self.store.get_meta("goal_celebrated_for") == str(goal):
            return
        self.store.set_meta("goal_celebrated_for", str(goal))
        send = getattr(self.notify, "send", None)
        if send is None:
            return
        await send(
            f"\U0001f389 <b>Goal reached!</b> {done}/{goal} applications submitted.\n"
            f"Set the next target with <code>/goal {goal + 50}</code> "
            f"or see the full picture with /report."
        )

    async def _cool_down(
        self, summary: RunSummary, *, more_to_come: bool, submitted: bool = True
    ) -> None:
        """Pace between listings.

        A real submission gets the full human pause; a listing that was only
        skipped (size, location, closed) gets a short beat, so the daily pass
        spends its time on fresh internships instead of idling between skips.
        """
        if not more_to_come or summary.stopped_reason or self.store.paused:
            return
        if submitted:
            delay = human.jitter(self.cfg)
            log.info("cooling down %.1fs before the next application", delay)
        else:
            delay = random.uniform(2.0, 6.0)
        await self._sleep_interruptibly(delay)

    async def _collect_candidates(self, summary: RunSummary) -> list[Job]:
        """Find every fresh internship, newest first, then drop history."""
        found: dict[str, Job] = {}
        search_ok = False

        if self.cfg.authenticated_search:
            try:
                for job in await self.wf.search_internships():
                    found.setdefault(job.job_id, job)
                search_ok = True
                await human.pause(2.0, 5.0)
            except guards.ChallengeTimeout as exc:
                await self._handle_challenge(summary, exc)
            except LoginRequired as exc:
                await self._handle_fatal(summary, exc, where="internship search")
            except Exception as exc:  # noqa: BLE001 - fall back to public pages
                log.exception("signed-in internship search failed")
                summary.errors.append(f"internship search: {type(exc).__name__}: {exc}")
                await self.notify.error("internship search", exc)

        if summary.stopped_reason:
            return []

        # The signed-in search is authoritative and includes onsite Gurugram
        # roles. Fall back to the public role/city pages only if the search
        # itself failed (not when it legitimately returned zero listings).
        if not search_ok:
            for role in self.cfg.roles:
                if self.store.paused:
                    summary.stopped_reason = "paused by operator"
                    break
                try:
                    for job in await self.wf.search(role):
                        found.setdefault(job.job_id, job)
                    await human.pause(2.0, 5.0)
                except guards.ChallengeTimeout as exc:
                    await self._handle_challenge(summary, exc)
                    break
                except LoginRequired as exc:
                    await self._handle_fatal(summary, exc, where="login")
                    break
                except Exception as exc:  # noqa: BLE001 - one bad role must not kill the run
                    log.exception("search failed for %s", role.label)
                    summary.errors.append(f"{role.label}: {type(exc).__name__}: {exc}")
                    await self.notify.error(f"search for {role.label}", exc)

            if not summary.stopped_reason and self.cfg.location_pages:
                try:
                    # The role pages are remote-filtered, so city pages are the only
                    # way to see onsite roles (Gurugram) on Wellfound.
                    for job in await self.wf.search_locations():
                        found.setdefault(job.job_id, job)
                    await human.pause(2.0, 5.0)
                except guards.ChallengeTimeout as exc:
                    await self._handle_challenge(summary, exc)
                except LoginRequired as exc:
                    await self._handle_fatal(summary, exc, where="location search")
                except Exception as exc:  # noqa: BLE001
                    log.exception("location search failed")
                    summary.errors.append(f"location search: {type(exc).__name__}: {exc}")
                    await self.notify.error("location search", exc)

        if summary.stopped_reason:
            return []

        # Freshest first: parsed ages are authoritative. Listings whose card
        # omitted the age go last and are checked strictly on the detail page.
        # Job id only breaks ties, because it is a rough posting-order proxy.
        fresh = [
            job
            for job in found.values()
            if not self.store.is_seen(job.job_id, self.cfg.max_retries)
        ]
        fresh.sort(key=_freshness_key)
        known = sum(1 for job in fresh if job.posted_days is not None)
        log.info(
            "%d unique listings, %d not yet processed (%d with a card age)",
            len(found),
            len(fresh),
            known,
        )
        return fresh

    async def _ensure_login(self, summary: RunSummary) -> bool:
        """True when there is a usable Wellfound session (creating one if needed)."""
        try:
            await self.wf.ensure_logged_in()
            return True
        except LoginRequired as exc:
            await self._handle_fatal(summary, exc, where="login")
            return False
        except guards.ChallengeTimeout as exc:
            await self._handle_challenge(summary, exc)
            return False
        except Exception as exc:  # noqa: BLE001 - one bad check must not traceback
            log.exception("login check failed")
            summary.stopped_reason = f"login check failed: {type(exc).__name__}: {exc}"
            summary.errors.append(summary.stopped_reason)
            await self.notify.error("login check", exc)
            return False

    async def _attempt(
        self, job: Job, role, summary: RunSummary, *, allow_fallback: bool = False
    ) -> Outcome:
        try:
            return await self.wf.apply(job, role, allow_fallback=allow_fallback)
        except guards.ChallengeTimeout as exc:
            await self._handle_challenge(summary, exc)
            return Outcome(job=job, status=Status.FAILED, detail=f"challenge: {exc}"[:180])
        except LoginRequired as exc:
            await self._handle_fatal(summary, exc, where="login")
            return Outcome(job=job, status=Status.FAILED, detail=f"login: {exc}"[:180])
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            log.exception("application failed for %s", job.url)
            return Outcome(
                job=job, status=Status.FAILED, detail=f"{type(exc).__name__}: {exc}"[:180]
            )

    # ------------------------------------------------------------------
    # failure handling
    # ------------------------------------------------------------------
    async def _handle_challenge(self, summary: RunSummary, exc: guards.ChallengeTimeout) -> None:
        log.warning("challenge timeout: %s", exc)
        summary.stopped_reason = f"anti-bot challenge: {exc}"
        self.store.set_paused(True)
        await self.notify.paused_automatically(str(exc))

    async def _handle_fatal(self, summary: RunSummary, exc: Exception, *, where: str) -> None:
        log.error("fatal during %s: %s", where, exc)
        summary.stopped_reason = f"{where}: {exc}"
        summary.errors.append(str(exc))
        self.store.set_paused(True)
        await self.notify.error(where, exc)
        await self.notify.paused_automatically(f"{where} failed - needs a human")

    def _update_backoff(self, summary: RunSummary) -> None:
        if summary.failures:
            self.store.set_consecutive_failures(self.store.consecutive_failures + 1)
        else:
            self.store.set_consecutive_failures(0)
        multiplier = guards.backoff_multiplier(self.cfg, self.store.consecutive_failures)
        self.state.backoff_multiplier = multiplier
        if multiplier > 1.0:
            log.warning("backing off: x%s (consecutive failures=%s)",
                        multiplier, self.store.consecutive_failures)

    # ------------------------------------------------------------------
    # schedule
    # ------------------------------------------------------------------
    async def loop(self) -> None:
        await asyncio.sleep(random.uniform(3.0, 8.0))  # settle after boot

        if self.cfg.daily_run_clock is None:
            # Legacy interval mode still starts with a run.
            self.trigger.set()
        else:
            self._catch_up_missed_run()

        while True:
            if self.store.paused:
                self.state.next_run_at = None
                await asyncio.sleep(10)
                continue

            if not self.trigger.is_set():
                next_at = self._next_run_at()
                self.state.next_run_at = next_at
                delay = max(1.0, (next_at - datetime.now().astimezone()).total_seconds())
                self.state.backoff_multiplier = guards.backoff_multiplier(
                    self.cfg, self.store.consecutive_failures
                )
                log.info("next run at %s (in %.0fs)", next_at.strftime("%d %b %H:%M"), delay)
                await self._sleep_interruptibly(delay)
                if self.store.paused or self.trigger.is_set():
                    continue
                # Sleep is deliberately granular; if a clock adjustment woke us
                # early, go back to waiting rather than running twice.
                if datetime.now().astimezone() < next_at:
                    continue

            self.trigger.clear()
            if self.cfg.max_apps_per_day > 0 and self.store.applied_today() >= self.cfg.max_apps_per_day:
                today = datetime.now().astimezone().date().isoformat()
                if self.store.get_meta("daily_cap_notified") != today:
                    self.store.set_meta("daily_cap_notified", today)
                    log.info("daily cap reached - idling until tomorrow")
                    send = getattr(self.notify, "send", None)
                    if send is not None:
                        await send(
                            f"\U0001f6d1 <b>Daily cap reached</b> "
                            f"({self.cfg.max_apps_per_day} applications).\n"
                            f"I will idle until tomorrow; /report shows the full "
                            f"picture, /pause stops the schedule entirely."
                        )
                self.state.next_run_at = None
                await self._sleep_interruptibly(600)
                continue

            summary = await self.run_once()
            now = datetime.now().astimezone()
            if self.store.paused:
                next_at = None
            else:
                self._mark_daily_run(now)
                next_at = self._next_run_at(now)
            self.state.next_run_at = next_at
            self.state.last_status_line = summary.one_line()
            await self.notify.run_finished(summary, next_at)

    def _next_run_at(self, now: datetime | None = None) -> datetime:
        """The next fixed daily slot, or the legacy interval's next wake-up."""
        if self.cfg.daily_run_clock is not None:
            next_at = guards.next_daily_run(self.cfg, now)
            assert next_at is not None
            return next_at
        return (now or datetime.now().astimezone()) + timedelta(
            seconds=guards.next_delay(self.cfg, self.store.consecutive_failures)
        )

    def _mark_daily_run(self, now: datetime) -> None:
        """Record today's slot once a run has started, so a restart can't
        accidentally run the same evening twice."""
        slot = guards.daily_slot(self.cfg, now)
        if slot is not None and now >= slot:
            self.store.set_meta("last_scheduled_run_date", slot.date().isoformat())

    def _catch_up_missed_run(self) -> None:
        """Start once if the bot was off during today's slot (within 4 hours)."""
        now = datetime.now().astimezone()
        slot = guards.daily_slot(self.cfg, now)
        if slot is None or now < slot or now - slot > timedelta(hours=4):
            return
        today = slot.date().isoformat()
        if self.store.get_meta("last_scheduled_run_date") == today:
            return
        log.info("today's %s run was missed; starting a catch-up pass", self.cfg.daily_run_at)
        self.trigger.set()

    async def _sleep_interruptibly(self, seconds: float) -> None:
        """Sleep, but wake up immediately for /run or /pause."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            if self.trigger.is_set() or self.store.paused:
                return
            await asyncio.sleep(min(5.0, deadline - loop.time()))


def _freshness_key(job: Job) -> tuple[bool, float, int]:
    """Newest first; unknown card ages last; job id breaks ties."""
    age = job.posted_days
    return (
        age is None,
        age if age is not None else inf,
        -(int(job.job_id) if job.job_id.isdigit() else 0),
    )


def role_for(job: Job):
    return ROLE_LIBRARY.get(job.role_key) or next(iter(ROLE_LIBRARY.values()))
