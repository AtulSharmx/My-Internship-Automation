"""Run orchestration: free collection, one reveal, pattern guesses, and Excel output."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import traceback
import uuid
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .apollo import (
    ApolloBrowser,
    ChallengeDetected,
    LoginRequired,
    PeoplePage,
    RevealCaptureError,
)
from .config import LEADERSHIP_TITLES, Settings, TALENT_TITLES
from .db import Database, now_iso
from .email_check import MXChecker, validate_email
from .excel_io import InputWorkbook, read_input, write_output
from .models import CompanyFacts, CompanyResult, InputRow, Person, RunSummary
from .names import split_full_name
from .patterns import (
    TEMPLATE_BY_DESCRIPTION,
    can_render_partial_name,
    email_domain,
    guess_email,
    infer_pattern,
    is_free_email_domain,
)

log = logging.getLogger(__name__)


class StopRequested(Exception):
    pass


class RunAlreadyActive(Exception):
    pass


@dataclass
class RunState:
    run_id: str = ""
    running: bool = False
    phase: str = "idle"
    status_text: str = "Ready"
    current_company: str = ""
    current_domain: str = ""
    current_person: str = ""
    total_rows: int = 0
    unique_companies: int = 0
    planned: int = 0
    handled: int = 0
    credits: int = 0
    verified: int = 0
    guessed: int = 0
    masked: int = 0
    people_found: int = 0
    people_covered: int = 0
    skipped: int = 0
    errors: int = 0
    limit: int = 0
    dry_run: bool = False
    hide_browser: bool = True
    stop_requested: bool = False
    browser_open: bool = False
    browser_hidden: bool = False
    login_required: bool = False
    challenge: str = ""
    started_at: str = ""
    finished_at: str = ""
    output_path: str = ""
    last_error: str = ""
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=400))

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["logs"] = list(self.logs)
        return data


def title_tier(title: str) -> int:
    value = (title or "").lower()
    if re.search(r"\bco[- ]?founder\b", value) or re.search(r"\bfounder\b", value):
        return 0
    if re.search(r"\b(ceo|chief executive officer)\b", value) and "assistant" not in value:
        return 1
    if re.search(r"\b(cto|chief technology officer)\b", value):
        return 2
    if re.search(
        r"(head of (talent|people|hr|recruiting)|talent acquisition|people operations|"
        r"people ops|recruit|human resources|\bhr\b|chief people|talent)",
        value,
    ):
        return 3
    return 4


def _merge_facts(base: CompanyFacts, extra: CompanyFacts) -> CompanyFacts:
    merged = CompanyFacts(
        employees=base.employees or extra.employees,
        industries=list(dict.fromkeys([*base.industries, *extra.industries])),
        stage=base.stage or extra.stage,
        revenue=base.revenue or extra.revenue,
        founded=base.founded or extra.founded,
        location=base.location or extra.location,
        funding=base.funding or extra.funding,
        description=base.description or extra.description,
        keywords=list(dict.fromkeys([*base.keywords, *extra.keywords])),
    )
    return merged


def _dedupe_people(people: list[Person]) -> list[Person]:
    result: list[Person] = []
    seen: set[str] = set()
    for person in people:
        if not person.person_key or person.person_key in seen:
            continue
        seen.add(person.person_key)
        person.tier = title_tier(person.title)
        result.append(person)
    return result


def select_candidate(people: list[Person]) -> tuple[Person | None, str]:
    """Choose the best available candidate, preferring verified + full name."""
    # Person.reason carries the data-tour-id from Apollo. Treat unavailable as no email.
    available = [
        p
        for p in people
        if p.reason
        and "unavailable" not in (p.reason or "").lower()
        and p.full_name
    ]
    if not available:
        return None, "Apollo showed no people with an available email"

    for person in available:
        person.tier = title_tier(person.title)
    tiers = sorted({p.tier for p in available})
    notes: list[str] = []
    for tier in tiers:
        same_tier = [p for p in available if p.tier == tier]
        full = [p for p in same_tier if not p.masked and split_full_name(p.full_name).usable]
        if not full:
            notes.append(f"tier {tier} candidates were masked")
            continue
        verified = [
            p
            for p in full
            if (p.reason or "").lower().strip() == "email-cell-verified"
        ]
        chosen = verified[0] if verified else full[0]
        if tier > 0:
            notes.append(f"fell back to title tier {tier}")
        return chosen, "; ".join(notes)
    return None, "no full-name candidate with an available email (all masked on the Free plan)"


def _rendered_email_counts(
    people: list[Person], template: str, domain: str
) -> dict[str, int]:
    """Count how many contacts would produce each address under a pattern.

    A count above one means the guess is ambiguous. Masked teammates whose
    first name (or initial combination) collides with another contact are left
    blank instead of being given a duplicate address.
    """
    counts: dict[str, int] = {}
    for person in people:
        if not person.full_name:
            continue
        email = guess_email(template, person.full_name, domain)
        if email:
            counts[email] = counts.get(email, 0) + 1
    return counts


def _skip_person(person: Person, message: str, reason: str) -> bool:
    person.email = ""
    person.source = ""
    person.validation = message
    person.confidence = "n/a"
    person.reason = reason
    return False


def _guess_from_pattern(
    person: Person,
    template: str,
    pattern_confidence: str,
    domain: str,
    mx: MXChecker,
    rendered_counts: dict[str, int],
) -> bool:
    """Fill one person's email from a company pattern.

    Returns True when an email was assigned. A masked/incomplete name is only
    guessed when the pattern can be rendered without the full last name; those
    guesses are always low confidence and are skipped when the address would be
    ambiguous (for example two masked people with the same first name).
    """
    parts = split_full_name(person.full_name)
    if not parts.first:
        return _skip_person(
            person,
            "skipped: contact has no usable first name",
            "incomplete_name",
        )

    partial = person.masked or not parts.usable
    if partial:
        if not can_render_partial_name(template, parts):
            if person.masked:
                return _skip_person(
                    person,
                    "skipped: pattern needs the full last name; Apollo masked this contact",
                    "masked_name",
                )
            return _skip_person(
                person,
                "skipped: pattern needs the full name; this contact is incomplete",
                "incomplete_name",
            )
        email = guess_email(template, person.full_name, domain)
        if not email:
            return _skip_person(
                person,
                "skipped: name could not be rendered with the pattern",
                "render_failed",
            )
        if rendered_counts.get(email, 0) > 1:
            reason = (
                "duplicate_first_name" if template == "{first}" else "duplicate_rendered_email"
            )
            message = (
                "skipped: duplicate first name; masked contact cannot be disambiguated"
                if reason == "duplicate_first_name"
                else "skipped: rendered email would collide with another contact"
            )
            return _skip_person(person, message, reason)
        validation = validate_email(email, mx)
        person.email = email
        person.source = "guessed"
        person.validation = "partial name; " + validation.validation
        person.confidence = "low"
        person.reason = "partial_name_guess"
        return True

    email = guess_email(template, person.full_name, domain)
    if not email:
        return _skip_person(
            person,
            "skipped: name could not be rendered with the pattern",
            "render_failed",
        )

    validation = validate_email(email, mx)
    person.email = email
    person.source = "guessed"
    person.validation = validation.validation
    duplicate_first = template == "{first}" and rendered_counts.get(email, 0) > 1
    if duplicate_first:
        person.validation += "; duplicate first name in team"
        person.confidence = "low"
    else:
        person.confidence = (
            "high"
            if pattern_confidence == "high" and validation.confidence == "high"
            else "low"
        )
    person.reason = "pattern_guess"
    return True


def reenrich_result(result: CompanyResult, mx: MXChecker) -> int:
    """Re-fill teammates on a saved result after the pattern engine improves.

    Older database rows already contain the revealed name/email but may have
    been written before a format was understood. This runs when output is
    rebuilt, so no new Apollo credit is spent.
    """
    if result.dry_run or not result.email or not result.revealed_name:
        return 0

    match = infer_pattern(result.email, result.revealed_name)
    if match is not None:
        template = match.template
        pattern_confidence = match.confidence
        result.pattern = match.description
        result.pattern_note = match.note
    else:
        template = TEMPLATE_BY_DESCRIPTION.get(result.pattern or "", "")
        pattern_confidence = "low"
    if not template:
        return 0

    domain = email_domain(result.email) or result.domain
    counts = _rendered_email_counts(result.people, template, domain)
    # Reserve the verified address too: a masked teammate must never be given
    # the same address as the person Apollo already revealed.
    if result.email:
        counts[result.email.lower()] = counts.get(result.email.lower(), 0) + 1
    filled = 0
    for person in result.people:
        if person.email:
            continue
        if _guess_from_pattern(person, template, pattern_confidence, domain, mx, counts):
            filled += 1
    if filled:
        note = (
            f"re-enriched {filled} teammate(s) with the stored "
            f"{result.pattern or template} pattern"
        )
        result.notes = f"{result.notes}; {note}" if result.notes else note
    return filled


class RunManager:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.browser = ApolloBrowser(settings)
        self.state = RunState()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._mx = MXChecker()
        self._input: InputWorkbook | None = None
        self._dry_results: dict[str, CompanyResult] = {}

    # ------------------------------------------------------------------ public
    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done()) or self.state.running

    async def start(
        self,
        input_path: Path,
        limit: int,
        dry_run: bool,
        hide_browser: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            if self.running:
                raise RunAlreadyActive("A run is already in progress")
            workbook = await asyncio.to_thread(read_input, input_path)
            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
            self._stop = asyncio.Event()
            self._dry_results = {}
            self.state = RunState(
                run_id=run_id,
                running=True,
                phase="starting",
                status_text="Starting",
                total_rows=len(workbook.rows),
                unique_companies=len({row.domain for row in workbook.rows if row.domain}),
                limit=limit,
                dry_run=dry_run,
                hide_browser=hide_browser,
                started_at=now_iso(),
            )
            self._log(
                f"Run {run_id} starting | input={input_path.name} | limit={limit} | "
                f"dry_run={dry_run} | hide_chrome={hide_browser}"
            )
            self._task = asyncio.create_task(
                self._run(workbook, input_path, limit, dry_run, run_id, hide_browser)
            )
            return self.state.as_dict()

    async def stop(self) -> dict[str, Any]:
        self._stop.set()
        self.state.stop_requested = True
        self.state.phase = "stopping"
        self.state.status_text = "Stopping after the current company"
        self._log("Stop requested - finishing the current company, then stopping")
        return self.state.as_dict()

    async def close_browser(self) -> None:
        await self.browser.close()
        self.state.browser_open = False
        self.state.browser_hidden = False
        self._log("Chrome closed")

    async def show_browser(self) -> bool:
        if not self.browser.is_open:
            return False
        await self.browser.show_window()
        self.state.browser_hidden = self.browser.window_hidden
        self._log("Chrome window restored")
        return True

    def status(self) -> dict[str, Any]:
        data = self.state.as_dict()
        data["browser_open"] = self.browser.is_open
        data["browser_hidden"] = self.browser.window_hidden
        data["ledger"] = self.db.ledger_summary()
        data["recent_runs"] = self.db.recent_runs(8)
        return data

    def _log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        self.state.logs.append(line)
        log.info(message)

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise StopRequested()

    def _sync_summary(self, summary: RunSummary) -> None:
        summary.companies_handled = self.state.handled
        summary.credits_spent = self.state.credits
        summary.verified = self.state.verified
        summary.guessed = self.state.guessed
        summary.masked = self.state.masked
        summary.skipped = self.state.skipped
        summary.errors = self.state.errors

    # --------------------------------------------------------------------- run
    async def _run(
        self,
        workbook: InputWorkbook,
        input_path: Path,
        limit: int,
        dry_run: bool,
        run_id: str,
        hide_browser: bool = True,
    ) -> None:
        summary = RunSummary(
            run_id=run_id,
            started_at=self.state.started_at,
            input_name=input_path.name,
            limit=limit,
            dry_run=dry_run,
            status="running",
        )
        self._input = workbook
        selected: list[InputRow] = []
        stop_reason = ""
        keep_browser = False
        output_path = self.settings.exports_dir / f"{input_path.stem}_apollo_{run_id}.xlsx"
        self.state.output_path = str(output_path)

        try:
            self.db.record_run_start(summary)
            selected = self._select_work(workbook, limit)
            self.state.planned = len(selected)
            self._log(
                f"{len(workbook.rows)} rows / {self.state.unique_companies} unique companies; "
                f"{len(selected)} selected for this run"
            )
            if not selected:
                self._log("Nothing new to process - rebuilding the workbook from the ledger")
                self.state.phase = "writing"
                self.state.status_text = "Writing workbook"
                await self._write_output(output_path)
                summary.status = "finished"
                summary.note = "No new companies selected; existing ledger results only."
                return

            self.state.phase = "browser"
            self.state.status_text = "Opening Chrome"
            self.browser.hide_window_preference = hide_browser
            await self.browser.start()
            self.state.browser_open = True
            self.state.browser_hidden = self.browser.window_hidden
            await self.browser.ensure_logged_in()
            self.state.login_required = False

            for index, row in enumerate(selected, start=1):
                self._check_stop()
                self.state.current_company = row.company_name or row.domain
                self.state.current_domain = row.domain
                self.state.current_person = ""
                self.state.status_text = f"Company {index} of {len(selected)}"
                self._log(
                    f"[{index}/{len(selected)}] {row.company_name or '(no name)'} <{row.domain}>"
                )
                try:
                    await self._process_company(row, run_id, dry_run)
                except StopRequested:
                    raise
                except ChallengeDetected as exc:
                    self.state.challenge = str(exc)
                    self.state.login_required = False
                    keep_browser = True
                    stop_reason = "Apollo verification/challenge screen"
                    await self._write_output(output_path)
                    self._sync_summary(summary)
                    raise
                except LoginRequired as exc:
                    self.state.login_required = True
                    keep_browser = True
                    stop_reason = str(exc)
                    await self._write_output(output_path)
                    self._sync_summary(summary)
                    raise
                except RevealCaptureError as exc:
                    self.state.last_error = str(exc)
                    keep_browser = True
                    stop_reason = str(exc)
                    await self._write_output(output_path)
                    self._sync_summary(summary)
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.state.errors += 1
                    self.state.last_error = f"{type(exc).__name__}: {exc}"
                    self._log(f"ERROR on {row.domain}: {self.state.last_error}")
                    log.debug(traceback.format_exc())
                    await self._write_output(output_path)
                    # Free-collection errors do not spend credits; continue with the next company.
                    continue

                await self._write_output(output_path)
                self.state.handled += 1
                self.state.status_text = f"Finished {index} of {len(selected)}"
                if index < len(selected):
                    delay = random.uniform(
                        self.settings.company_delay_min, self.settings.company_delay_max
                    )
                    self._log(f"Pausing {delay:.0f}s before the next company")
                    await asyncio.sleep(delay)

            self.state.phase = "writing"
            self.state.status_text = "Writing final workbook"
            await self._write_output(output_path)
            summary.status = "finished"
            summary.note = "Completed"
            self._log("Run complete")

        except StopRequested:
            summary.status = "stopped"
            stop_reason = "Stopped by user"
            self._log("Run stopped by user")
            await self._write_output(output_path)
        except ChallengeDetected as exc:
            summary.status = "stopped"
            keep_browser = True
            self.state.challenge = str(exc)
            stop_reason = "Apollo verification/challenge screen"
            self._log("Run stopped: challenge")
        except LoginRequired as exc:
            summary.status = "stopped"
            keep_browser = True
            self.state.login_required = True
            stop_reason = str(exc)
            self._log("Run stopped: login required")
        except RevealCaptureError as exc:
            summary.status = "error"
            keep_browser = True
            self.state.last_error = str(exc)
            stop_reason = str(exc)
            self._log("Run stopped: could not capture the revealed email")
        except Exception as exc:  # noqa: BLE001
            summary.status = "error"
            self.state.errors += 1
            self.state.last_error = f"{type(exc).__name__}: {exc}"
            stop_reason = self.state.last_error
            self._log("Run failed: " + self.state.last_error)
            log.debug(traceback.format_exc())
            try:
                await self._write_output(output_path)
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                await self._write_output(output_path)
            except Exception as exc:  # noqa: BLE001
                self._log(f"Output write failed: {exc}")
            summary.finished_at = now_iso()
            self._sync_summary(summary)
            summary.output_path = str(output_path)
            if summary.status == "running":
                summary.status = "finished"
            try:
                self.db.record_run_finish(summary)
            except Exception as exc:  # noqa: BLE001
                self._log(f"Could not record run history: {exc}")
            if not keep_browser:
                await self.browser.close()
                self._log("Chrome closed after the run")
            else:
                # Restore the window on challenge/login/capture failures so the
                # user can review or solve the screen that stopped the run.
                try:
                    await self.browser.show_window()
                except Exception as exc:  # noqa: BLE001
                    self._log(f"Could not restore Chrome window: {exc}")
            self.state.browser_open = self.browser.is_open
            self.state.browser_hidden = self.browser.window_hidden
            self.state.running = False
            self.state.phase = "done" if summary.status == "finished" else summary.status
            if summary.status == "finished":
                self.state.status_text = "Finished"
            elif summary.status == "stopped":
                self.state.status_text = f"Stopped: {stop_reason or 'run stopped'}"
            else:
                self.state.status_text = f"Error: {stop_reason or summary.status}"
            self.state.finished_at = summary.finished_at
            self.state.stop_requested = False

    def _select_work(self, workbook: InputWorkbook, limit: int) -> list[InputRow]:
        if limit <= 0:
            return []
        seen: set[str] = set()
        seen_wfid: set[str] = set()
        selected: list[InputRow] = []
        now = datetime.now(timezone.utc)
        for row in workbook.rows:
            if not row.domain or row.domain in seen:
                continue
            if row.wellfound_company_id and row.wellfound_company_id in seen_wfid:
                continue
            seen.add(row.domain)
            if row.wellfound_company_id:
                seen_wfid.add(row.wellfound_company_id)
            existing = self.db.find_company(row.domain, row.wellfound_company_id)
            if existing is not None:
                status = existing["status"]
                if status in {"processed", "attempting", "unknown"}:
                    continue
                if status == "no_candidate":
                    checked = existing["last_checked_at"]
                    if checked:
                        try:
                            age = now - datetime.fromisoformat(checked)
                            if age.days < self.settings.retry_no_candidate_days:
                                continue
                        except ValueError:
                            pass
                if status == "stopped":
                    pass
            selected.append(row)
            if len(selected) >= limit:
                break
        return selected

    async def _process_company(self, row: InputRow, run_id: str, dry_run: bool) -> None:
        domain = row.domain
        company_name = row.company_name or domain
        wfid = row.wellfound_company_id

        existing = self.db.result_for(domain, wfid)
        if existing and existing.status in {"processed", "attempting", "unknown"}:
            self._log(f"Already in ledger ({existing.status}) - reusing, no credit spent")
            return

        pages: list[PeoplePage] = []
        people: list[Person] = []
        org_id = ""
        for page_num in range(1, self.settings.people_pages + 1):
            page = await self.browser.search_people(domain, page_num)
            pages.append(page)
            if not org_id:
                org_id = page.org_id
            people.extend(page.people)
            self._log(f"free people page {page_num}: {len(page.people)} rows ({page.total_text})")
            if len(page.people) < 30:
                break
            total_match = re.search(r"of\s+([\d,]+)", page.total_text or "")
            if total_match and page_num * 30 >= int(total_match.group(1).replace(",", "")):
                break

        title_page = None
        if people:
            title_page = await self.browser.search_titles(
                domain, [*LEADERSHIP_TITLES, *TALENT_TITLES]
            )
            if not org_id:
                org_id = title_page.org_id
            people.extend(title_page.people)
            self._log(
                f"title-priority search: {len(title_page.people)} rows ({title_page.total_text})"
            )
        else:
            self._log("no people found in the domain search; skipping the title search")

        people = _dedupe_people(people)
        full_count = sum(1 for p in people if not p.masked and split_full_name(p.full_name).usable)
        masked_count = sum(1 for p in people if p.masked)
        self._log(f"collected {len(people)} unique people ({full_count} full names, {masked_count} masked)")

        candidate, candidate_note = select_candidate(people)
        if candidate:
            self.state.current_person = f"{candidate.full_name} — {candidate.title}"
            self._log(f"candidate: {candidate.full_name} | {candidate.title} | {candidate.reason}")
            if candidate_note:
                self._log(f"selection note: {candidate_note}")
        else:
            self._log(f"no revealable candidate: {candidate_note}")

        facts = CompanyFacts()
        if org_id:
            try:
                facts = await self.browser.org_facts(org_id)
            except Exception as exc:  # noqa: BLE001
                self._log(f"company page read failed (continuing): {exc}")

        if dry_run:
            if candidate and candidate.href:
                try:
                    await self.browser.goto(self._absolute_person(candidate.href))
                    await asyncio.sleep(3)
                    facts = _merge_facts(facts, await self.browser.person_facts())
                except Exception as exc:  # noqa: BLE001
                    self._log(f"profile read failed in dry run: {exc}")
            result = self._build_dry_result(row, people, candidate, candidate_note, facts)
            self._dry_results[domain] = result
            self.state.masked += result.masked_count
            self.state.people_found += len(people)
            if candidate:
                self._log(f"DRY RUN: would reveal {candidate.full_name}, no credit spent")
            else:
                self.state.skipped += 1
            return

        self._check_stop()
        if candidate is None:
            notes = f"no candidate: {candidate_note}; no credit spent"
            for person in people:
                if person.masked:
                    person.validation = "skipped: last name masked by Apollo Free plan"
                else:
                    person.validation = "skipped: no revealable candidate with an available email"
                person.confidence = "n/a"
                person.source = ""
                person.email = ""
                person.reason = "masked_name" if person.masked else "no_candidate"
            self.db.mark_no_candidate(domain, company_name, wfid, notes, facts, people, run_id)
            self.state.skipped += 1
            self.state.masked += masked_count
            self.state.people_found += len(people)
            self._log("No candidate - ledger marked no_candidate (0 credits)")
            return

        attempted = False

        async def before_click(credits_before: int | None) -> None:
            nonlocal attempted
            attempted = True
            self.db.begin_attempt(domain, company_name, wfid, run_id, credits_before)
            self.state.credits += 1  # pessimistic until the Apollo counter confirms
            self._log("Credit attempt recorded before clicking Access email")

        try:
            reveal = await self.browser.reveal_email(
                candidate.href,
                domain,
                before_click_cb=before_click,
            )
            facts = _merge_facts(facts, await self.browser.person_facts())
        except (ChallengeDetected, LoginRequired):
            if attempted:
                self.db.mark_unknown(
                    domain,
                    company_name,
                    wfid,
                    "run stopped during/after the reveal click; a credit may have been spent",
                    run_id,
                )
            raise
        except RevealCaptureError:
            if attempted:
                self.db.mark_unknown(
                    domain,
                    company_name,
                    wfid,
                    "clicked Access email but could not capture the address; manual review needed",
                    run_id,
                )
            raise

        try:
            reveal_name = reveal.full_name or candidate.full_name
            reveal_title = reveal.title or candidate.title
            pattern = infer_pattern(reveal.email, reveal_name)
            if pattern is None:
                self._log("No usable company pattern could be derived from the revealed email")
            else:
                self._log(f"pattern: {pattern.description} (example {pattern.example})")

            reveal_domain = email_domain(reveal.email) or domain
            if is_free_email_domain(reveal_domain):
                self._log("Revealed address is on a free email provider - no company-wide guesses")
                pattern = None
            elif reveal_domain and reveal_domain != domain:
                self._log(
                    f"revealed email domain is {reveal_domain}, not the input domain {domain}; "
                    "guesses use the revealed domain"
                )

            enriched_people = self._build_people(
                people,
                candidate,
                reveal,
                reveal_name,
                reveal_title,
                pattern.template if pattern else "",
                pattern.confidence if pattern else "",
                reveal_domain,
            )
            guessed = sum(1 for p in enriched_people if p.source == "guessed" and p.email)
            low = sum(1 for p in enriched_people if p.source == "guessed" and p.confidence == "low")
            partial = sum(
                1
                for p in enriched_people
                if p.reason == "partial_name_guess" and p.email
            )
            masked = sum(1 for p in enriched_people if p.masked and not p.email)
            covered = sum(1 for p in enriched_people if p.email)
            credit_delta = reveal.credit_delta
            credit_spent = 1 if not credit_delta else credit_delta

            notes = self._build_notes(
                row,
                facts,
                pattern.description if pattern else "",
                reveal_name,
                reveal.email,
                len(enriched_people),
                guessed,
                low,
                partial,
                masked,
                candidate_note,
                reveal,
            )
            if reveal_domain and reveal_domain != domain:
                notes += f"; revealed email domain {reveal_domain} differs from input domain {domain}"
            result = CompanyResult(
                domain=domain,
                company_name=company_name,
                wellfound_company_id=wfid,
                status="processed",
                email=reveal.email,
                email_source="verified",
                notes=notes,
                revealed_name=reveal_name,
                pattern=pattern.description if pattern else "",
                pattern_note=pattern.note if pattern else "no pattern derivable",
                credit_spent=credit_spent,
                credits_before=reveal.credits_before,
                credits_after=reveal.credits_after,
                facts=facts,
                people=enriched_people,
            )
            self.db.save_result(result, run_id)
        except Exception as exc:
            if attempted:
                try:
                    self.db.mark_unknown(
                        domain,
                        company_name,
                        wfid,
                        f"email was revealed but post-processing failed: {type(exc).__name__}: {exc}",
                        run_id,
                    )
                except Exception:  # noqa: BLE001
                    pass
            raise

        if credit_delta is not None:
            self.state.credits += credit_delta - 1
        self.state.guessed += guessed
        self.state.verified += 1
        self.state.masked += masked
        self.state.people_found += len(enriched_people)
        self.state.people_covered += covered
        self._log(
            f"saved: verified={reveal.email} | guessed={guessed} (low={low}, "
            f"partial={partial}) | masked={masked}"
        )

    def _absolute_person(self, href: str) -> str:
        if href.startswith("http"):
            return href
        if href.startswith("#"):
            return "https://app.apollo.io/" + href
        return "https://app.apollo.io" + (href if href.startswith("/") else "/" + href)

    def _build_people(
        self,
        people: list[Person],
        candidate: Person,
        reveal,
        reveal_name: str,
        reveal_title: str,
        template: str,
        pattern_confidence: str,
        domain: str,
    ) -> list[Person]:
        found_candidate = False
        result: list[Person] = []
        rendered_counts = (
            _rendered_email_counts(people, template, domain) if template else {}
        )
        if reveal.email:
            # The verified address is reserved even if the candidate's old
            # masked row happens to render a different local part.
            rendered_counts[reveal.email.lower()] = (
                rendered_counts.get(reveal.email.lower(), 0) + 1
            )
        for person in people:
            if person.person_key == candidate.person_key or (
                person.href and candidate.href and person.href.split("?")[0] == candidate.href.split("?")[0]
            ):
                found_candidate = True
                person = Person(
                    **{
                        **person.__dict__,
                        "full_name": reveal_name,
                        "name_raw": reveal_name,
                        "masked": False,
                        "title": reveal_title or person.title,
                        "email": reveal.email,
                        "source": "verified",
                        "validation": "real email revealed via Apollo",
                        "confidence": "high",
                        "reason": "revealed",
                    }
                )
                result.append(person)
                continue

            if not person.full_name and not person.name_raw:
                continue

            if not template:
                person.email = ""
                person.source = ""
                if person.masked:
                    person.validation = "skipped: last name masked by Apollo Free plan"
                    person.reason = "masked_name"
                elif not split_full_name(person.full_name).usable:
                    person.validation = (
                        "skipped: contact name is incomplete (single name or initial-only last name)"
                    )
                    person.reason = "incomplete_name"
                else:
                    person.validation = "skipped: no company pattern could be derived"
                    person.reason = "no_pattern"
                person.confidence = "n/a"
                result.append(person)
                continue

            if _guess_from_pattern(
                person,
                template,
                pattern_confidence,
                domain,
                self._mx,
                rendered_counts,
            ):
                result.append(person)
                continue

            if person.validation:
                # The helper already explained a duplicate-name or render failure.
                result.append(person)
                continue

            person.email = ""
            person.source = ""
            person.validation = (
                "skipped: last name masked by Apollo Free plan; this pattern needs the full last name"
                if person.masked
                else "skipped: contact name is incomplete; this pattern needs the full name"
            )
            person.confidence = "n/a"
            person.reason = "masked_name" if person.masked else "incomplete_name"
            result.append(person)

        if not found_candidate:
            result.append(
                Person(
                    person_key=candidate.person_key,
                    name_raw=reveal_name,
                    full_name=reveal_name,
                    masked=False,
                    title=reveal_title or candidate.title,
                    tier=title_tier(reveal_title or candidate.title),
                    href=candidate.href,
                    location=candidate.location,
                    email=reveal.email,
                    source="verified",
                    validation="real email revealed via Apollo",
                    confidence="high",
                    reason="revealed",
                )
            )
        return result

    def _build_dry_result(
        self,
        row: InputRow,
        people: list[Person],
        candidate: Person | None,
        candidate_note: str,
        facts: CompanyFacts,
    ) -> CompanyResult:
        candidate_text = (
            f"would reveal {candidate.full_name} ({candidate.title})"
            if candidate
            else f"no revealable candidate: {candidate_note}"
        )
        notes = (
            f"DRY RUN — {candidate_text}; no credit spent. "
            f"Collected {len(people)} people, "
            f"{sum(1 for p in people if p.masked)} masked. "
            f"Apollo: {facts.notes() or 'no company details visible'}"
        )
        for person in people:
            person.email = ""
            person.source = ""
            person.validation = "dry run: email not revealed"
            person.confidence = "n/a"
            person.reason = person.reason or "dry_run"
        people = sorted(people, key=lambda p: (p.tier, (p.full_name or "").lower()))
        return CompanyResult(
            domain=row.domain,
            company_name=row.company_name or row.domain,
            wellfound_company_id=row.wellfound_company_id,
            status="dry_run",
            notes=notes,
            facts=facts,
            people=people,
            dry_run=True,
        )

    def _build_notes(
        self,
        row: InputRow,
        facts: CompanyFacts,
        pattern: str,
        reveal_name: str,
        reveal_email: str,
        people_count: int,
        guessed: int,
        low: int,
        partial: int,
        masked: int,
        selection_note: str,
        reveal,
    ) -> str:
        bits: list[str] = []
        if pattern:
            bits.append(f"pattern {pattern} (from {reveal_name})")
        else:
            bits.append("no company-wide email pattern could be derived")
        if selection_note:
            bits.append(selection_note)
        bits.append(
            f"team {people_count}: {guessed} guessed"
            + (f" ({low} low-confidence)" if low else "")
            + (f", {partial} partial-name" if partial else "")
            + f", {masked} masked/without email"
        )
        if reveal.credit_delta is not None:
            bits.append(f"Apollo credits {reveal.credits_before} -> {reveal.credits_after}")
        if facts.notes():
            bits.append("Apollo: " + facts.notes())
        if facts.niche and not str(row.data.get("niche") or "").strip():
            bits.append(f"filled niche with '{facts.niche}'")
        if facts.employees and not str(row.data.get("team_size") or "").strip():
            bits.append(f"filled team_size with '{facts.employees}'")
        if facts.employees and str(row.data.get("team_size") or "").strip() not in {"", facts.employees}:
            bits.append(
                f"Apollo reports {facts.employees} employees (kept input team_size "
                f"'{row.data.get('team_size')}')"
            )
        return "; ".join(bits)

    async def _write_output(self, output_path: Path) -> None:
        if not self._input:
            return
        results: list[CompanyResult] = []
        seen: set[str] = set()
        for row in self._input.rows:
            if not row.domain:
                continue
            result = self._dry_results.get(row.domain)
            if result is None:
                result = self.db.result_for(row.domain, row.wellfound_company_id)
            if result is not None and result.domain not in seen:
                seen.add(result.domain)
                results.append(result)

        # Older ledger rows may predate a newly recognised pattern format.
        # Re-running the local matcher costs no Apollo credit and makes old
        # companies benefit from the improved inference.
        for result in results:
            filled = reenrich_result(result, self._mx)
            if filled:
                self._log(
                    f"re-enriched {filled} teammate(s) for {result.company_name or result.domain} "
                    "using the stored reveal (0 credits)"
                )

        statuses = self.db.domain_statuses()
        path = await asyncio.to_thread(
            write_output,
            self._input,
            results,
            output_path,
            statuses,
        )
        if str(path) != self.state.output_path:
            self.state.output_path = str(path)
        self._log(f"workbook written: {path.name}")
