"""Tests for the scan job manager, website verification and the control panel.

Everything here is offline: the browser is a fake collector, the website check
uses a mocked HTTP transport and Telegram calls are captured instead of sent.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

import httpx

from startup_agent.config import Config
from startup_agent.discovery import DiscoveryService
from startup_agent.export import (
    REPORT_HEADERS,
    EmptyReportError,
    ExportService,
)
from startup_agent.jobs import (
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_PAUSED,
    JOB_STOPPED,
    JOB_STOPPING,
    MODE_NEW,
    MODE_RETRY,
    JobBusy,
    JobError,
    ScanManager,
    ScanSettings,
)
from startup_agent.models import LeadStatus, WebsiteStatus
from startup_agent.sources.wellfound_browser import (
    BrowserError,
    ChallengeError,
    ProfileCard,
    ProfileData,
)
from startup_agent.store import Store
from startup_agent.telegram import (
    DOWNLOAD,
    START_SCAN,
    STOP,
    WELCOME_TEXT,
    Telegram,
)
from startup_agent.verify import WebsiteCheck, WebsiteVerifier


def _cfg(root: Path, **overrides) -> Config:
    values = {
        "db_path": root / "data" / "startups.db",
        "import_dir": root / "imports",
        "export_dir": root / "exports",
        "log_dir": root / "logs",
        "target_size_min": 1,
        "target_size_max": 10,
        "enable_wellfound_browser": True,
        "wellfound_max_companies": 25,
    }
    values.update(overrides)
    return Config(**values)


def _card(name: str, size: str, slug: str | None = None) -> ProfileCard:
    slug = slug or name.lower().replace(" ", "-")
    return ProfileCard(
        name=name,
        profile_url=f"https://wellfound.com/company/{slug}",
        size=size,
        text=f"{name} {size}",
    )


def _profile(
    name: str,
    website: str = "",
    size: str = "1-10 Employees",
    slug: str | None = None,
) -> ProfileData:
    slug = slug or name.lower().replace(" ", "-")
    return ProfileData(
        name=name,
        website=website,
        website_source="Website section",
        size=size,
        tagline=f"{name} builds things",
        profile_url=f"https://wellfound.com/company/{slug}",
    )


class FakeCollector:
    """A stand-in for the visible-browser collector."""

    def __init__(
        self,
        cfg: Config,
        *,
        cards: list[ProfileCard] | None = None,
        profiles: dict[str, ProfileData] | None = None,
        fail_start: bool = False,
        fail_profiles: tuple[str, ...] = (),
        challenge_profiles: tuple[str, ...] = (),
        gate: asyncio.Event | None = None,
    ) -> None:
        self.cfg = cfg
        self.cards = cards or []
        self.profiles = profiles or {}
        self.fail_start = fail_start
        self.fail_profiles = fail_profiles
        self.challenge_profiles = challenge_profiles
        self.gate = gate
        self.visited: list[str] = []
        self.closed = False
        self.started = False

    async def start(self) -> None:
        if self.fail_start:
            raise BrowserError("the browser profile is locked")
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def discover_profiles(self, max_profiles: int, on_card=None) -> list[ProfileCard]:
        cards = self.cards[:max_profiles]
        for card in cards:
            if on_card is not None:
                on_card(card)
        return cards

    async def inspect_profile(self, profile_url: str) -> ProfileData:
        self.visited.append(profile_url)
        if self.gate is not None:
            await self.gate.wait()
        if profile_url in self.challenge_profiles:
            raise ChallengeError("verification page")
        if profile_url in self.fail_profiles:
            raise RuntimeError("page did not load")
        try:
            return self.profiles[profile_url]
        except KeyError:  # pragma: no cover - defensive
            raise RuntimeError("unknown profile") from None


class FakeVerifier:
    """Always answers with the configured status."""

    def __init__(self, status: str = WebsiteStatus.VERIFIED) -> None:
        self.status = status
        self.checked: list[str] = []

    async def verify(self, url: str, *, company_name: str = "") -> WebsiteCheck:
        self.checked.append(url)
        if not url:
            return WebsiteCheck(WebsiteStatus.MISSING, reason="no website listed")
        return WebsiteCheck(
            self.status, final_url=url, reason=f"HTTP 200 ({self.status})", http_status=200
        )


class ScanManagerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = _cfg(
            self.root, telegram_token="123456:TEST", telegram_chat_id="42"
        )
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)
        self.service = DiscoveryService(self.cfg, self.store)
        self.collector: FakeCollector | None = None
        self.verifier = FakeVerifier()
        self.jobs = ScanManager(
            self.cfg,
            self.store,
            collector_factory=self._make_collector,
            verifier=self.verifier,  # type: ignore[arg-type]
        )

    def _make_collector(self, settings: ScanSettings) -> FakeCollector:
        self.collector = FakeCollector(
            self.cfg,
            cards=self.cards,
            profiles=self.profiles,
            fail_start=self.fail_start,
            fail_profiles=self.fail_profiles,
            challenge_profiles=self.challenge_profiles,
            gate=self.gate,
        )
        return self.collector

    cards: list[ProfileCard] = []
    profiles: dict[str, ProfileData] = {}
    fail_start = False
    fail_profiles: tuple[str, ...] = ()
    challenge_profiles: tuple[str, ...] = ()
    gate: asyncio.Event | None = None

    async def asyncTearDown(self) -> None:
        await self.jobs.close()
        await self.service.close()
        self.store.close()
        self.temp.cleanup()

    async def wait_for_status(self, job_id: str, statuses, timeout: float = 10.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            snapshot = self.jobs.snapshot(job_id)
            if snapshot.get("status") in statuses:
                return snapshot
            await asyncio.sleep(0.05)
        return self.jobs.snapshot(job_id)


# ---------------------------------------------------------------------------
# Size filter / eligibility
# ---------------------------------------------------------------------------
class StrictSizeFilterTests(ScanManagerTestCase):
    async def test_only_explicit_1_to_10_is_accepted(self) -> None:
        self.cards = [
            _card("Tiny Studio", "1-10 Employees"),
            _card("Big Corp", "11-50 Employees"),
            _card("Huge Corp", "51-200 Employees"),
            _card("Vague Corp", ""),
        ]
        self.profiles = {
            card.profile_url: _profile(
                card.name,
                website=f"https://{card.name.split()[0].lower()}.test",
                size=card.size or "12 Employees",
            )
            for card in self.cards
        }
        job = await self.jobs.start(MODE_NEW, max_profiles=10)
        snapshot = await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})
        self.assertEqual(snapshot["status"], JOB_COMPLETED)
        self.assertEqual(snapshot["eligible"], 1)
        self.assertEqual(snapshot["rejected"], 3)
        self.assertEqual(snapshot["websites_verified"], 1)
        self.assertEqual(snapshot["new_verified"], 1)

        verified = self.store.verified_companies()
        self.assertEqual([c.name for c in verified], ["Tiny Studio"])
        # A company without a verified website never reaches the export.
        for company in self.store.list_companies(limit=50):
            if company.name != "Tiny Studio":
                self.assertNotEqual(company.lead_status, LeadStatus.VERIFIED)

    async def test_profile_size_wins_over_card_size(self) -> None:
        card = _card("Mixed Co", "1-10 Employees")
        self.cards = [card]
        self.profiles = {
            card.profile_url: _profile("Mixed Co", website="https://mixed.test", size="51-200 Employees")
        }
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        snapshot = await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})
        self.assertEqual(snapshot["eligible"], 0)
        self.assertEqual(snapshot["rejected"], 1)
        self.assertEqual(self.store.verified_companies(), [])


# ---------------------------------------------------------------------------
# Website verification
# ---------------------------------------------------------------------------
class WebsiteVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def _check(self, handler, url: str = "https://acme.test") -> WebsiteCheck:
        transport = httpx.MockTransport(handler)
        verifier = WebsiteVerifier(timeout=5.0, transport=transport, check_dns=False)
        return await verifier.verify(url)

    async def test_reachable_html_page_is_verified(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, text="<html><body>Acme</body></html>"
            )

        check = await self._check(handler)
        self.assertEqual(check.status, WebsiteStatus.VERIFIED)
        self.assertTrue(check.ok)
        self.assertIn("HTTP 200", check.evidence)

    async def test_www_redirect_stays_verified(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, text="<html></html>"
            )

        check = await self._check(handler, "https://www.acme.test")
        self.assertEqual(check.status, WebsiteStatus.VERIFIED)

    async def test_other_domain_is_not_verified(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                301, headers={"location": "https://other.test/"}, content=b""
            )

        check = await self._check(handler)
        self.assertEqual(check.status, WebsiteStatus.REDIRECTED)
        self.assertFalse(check.ok)

    async def test_dead_site_is_unreachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"nope")

        check = await self._check(handler)
        self.assertEqual(check.status, WebsiteStatus.UNREACHABLE)

    async def test_social_profiles_are_rejected(self) -> None:
        check = await self._check(
            lambda request: httpx.Response(200, text="<html></html>"),
            "https://www.linkedin.com/company/acme",
        )
        self.assertEqual(check.status, WebsiteStatus.BLOCKED)

    async def test_timeout_is_unreachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow", request=request)

        check = await self._check(handler)
        self.assertEqual(check.status, WebsiteStatus.UNREACHABLE)
        self.assertIn("timed out", check.evidence)

    async def test_missing_url(self) -> None:
        verifier = WebsiteVerifier(check_dns=False)
        check = await verifier.verify("")
        self.assertEqual(check.status, WebsiteStatus.MISSING)

    async def test_private_addresses_are_refused(self) -> None:
        verifier = WebsiteVerifier(check_dns=False)
        check = await verifier.verify("http://127.0.0.1/")
        self.assertEqual(check.status, WebsiteStatus.UNREACHABLE)
        self.assertIn("private", check.reason)


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------
class JobLifecycleTests(ScanManagerTestCase):
    async def test_concurrent_scans_are_refused(self) -> None:
        self.cards = [_card("Tiny Studio", "1-10 Employees")]
        self.profiles = {
            self.cards[0].profile_url: _profile("Tiny Studio", website="https://tiny.test")
        }
        await self.jobs.start(MODE_NEW, max_profiles=5)
        with self.assertRaises(JobBusy):
            await self.jobs.start(MODE_NEW, max_profiles=5)
        await self.wait_for_status(self.jobs.active_job().job_id, {JOB_COMPLETED})

    async def test_pause_really_stops_the_worker(self) -> None:
        card_a = _card("Alpha", "1-10 Employees")
        card_b = _card("Beta", "1-10 Employees")
        self.cards = [card_a, card_b]
        self.profiles = {
            card_a.profile_url: _profile("Alpha", website="https://alpha.test"),
            card_b.profile_url: _profile("Beta", website="https://beta.test"),
        }
        gate = asyncio.Event()
        self.gate = gate
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        # Wait until the first profile is being read (it is held at the gate).
        for _ in range(300):
            if self.collector is not None and self.collector.visited:
                break
            await asyncio.sleep(0.02)
        await self.jobs.pause()
        await asyncio.sleep(0.2)
        snapshot = self.jobs.snapshot(job.job_id)
        self.assertEqual(snapshot["status"], JOB_PAUSED)
        # The worker is blocked: the second profile is not read while paused.
        self.assertEqual(len(self.collector.visited), 1)
        # Let the first profile finish; it is still written while paused.
        gate.set()
        await asyncio.sleep(0.4)
        self.assertEqual(len(self.collector.visited), 1)
        self.assertEqual(len(self.store.list_companies(limit=10)), 1)
        # Resume: the run continues with the second profile.
        await self.jobs.resume()
        snapshot = await self.wait_for_status(job.job_id, {JOB_COMPLETED})
        self.assertEqual(snapshot["status"], JOB_COMPLETED)
        self.assertEqual(len(self.collector.visited), 2)
        self.assertEqual(snapshot["new_verified"], 2)

    async def test_graceful_stop_keeps_the_data(self) -> None:
        card_a = _card("Alpha", "1-10 Employees")
        card_b = _card("Beta", "1-10 Employees")
        self.cards = [card_a, card_b]
        self.profiles = {
            card_a.profile_url: _profile("Alpha", website="https://alpha.test"),
            card_b.profile_url: _profile("Beta", website="https://beta.test"),
        }
        gate = asyncio.Event()
        self.gate = gate
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        for _ in range(300):
            if self.collector is not None and self.collector.visited:
                break
            await asyncio.sleep(0.02)
        await self.jobs.request_stop()
        snapshot = self.jobs.snapshot(job.job_id)
        self.assertIn(snapshot["status"], {JOB_STOPPING, JOB_STOPPED})
        gate.set()
        snapshot = await self.wait_for_status(job.job_id, {JOB_STOPPED, JOB_COMPLETED})
        self.assertEqual(snapshot["status"], JOB_STOPPED)
        # The record that was already read is kept.
        self.assertEqual(len(self.store.list_companies(limit=10)), 1)
        # A stopped run can be resumed later.
        self.gate = None
        await self.jobs.resume_job(job.job_id)
        snapshot = await self.wait_for_status(job.job_id, {JOB_COMPLETED})
        self.assertEqual(snapshot["status"], JOB_COMPLETED)
        self.assertEqual(len(self.store.list_companies(limit=10)), 2)

    async def test_checkpoint_survives_a_restart(self) -> None:
        card_a = _card("Alpha", "1-10 Employees")
        card_b = _card("Beta", "1-10 Employees")
        self.cards = [card_a, card_b]
        self.profiles = {
            card_a.profile_url: _profile("Alpha", website="https://alpha.test"),
            card_b.profile_url: _profile("Beta", website="https://beta.test"),
        }
        gate = asyncio.Event()
        self.gate = gate
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        for _ in range(300):
            if self.collector is not None and self.collector.visited:
                break
            await asyncio.sleep(0.02)
        # Simulate the process being killed mid-run.
        await self.jobs.close()
        snapshot = self.jobs.snapshot(job.job_id)
        self.assertEqual(snapshot["status"], JOB_INTERRUPTED)

        # A new process picks the run up again.
        second = ScanManager(
            self.cfg,
            self.store,
            collector_factory=self._make_collector,
            verifier=self.verifier,  # type: ignore[arg-type]
        )
        recovered = second.recover()
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.status, JOB_INTERRUPTED)
        gate.set()
        await second.resume_job(job.job_id)
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if second.snapshot(job.job_id)["status"] == JOB_COMPLETED:
                break
            await asyncio.sleep(0.05)
        snapshot = second.snapshot(job.job_id)
        self.assertEqual(snapshot["status"], JOB_COMPLETED)
        # Alpha was already read, so it is not read again.
        self.assertEqual(snapshot["inspected"], 2)
        self.assertEqual(len(self.store.list_companies(limit=10)), 2)
        await second.close()

    async def test_browser_failure_marks_the_run_failed(self) -> None:
        self.fail_start = True
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        snapshot = await self.wait_for_status(job.job_id, {JOB_FAILED, JOB_COMPLETED})
        self.assertEqual(snapshot["status"], JOB_FAILED)
        self.assertIn("locked", snapshot["error"])

    async def test_challenge_pauses_the_run_for_manual_help(self) -> None:
        card = _card("Guarded", "1-10 Employees")
        self.cards = [card]
        self.profiles = {card.profile_url: _profile("Guarded", website="https://guarded.test")}
        self.challenge_profiles = (card.profile_url,)
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        snapshot = await self.wait_for_status(job.job_id, {JOB_PAUSED, JOB_FAILED})
        self.assertEqual(snapshot["status"], JOB_PAUSED)
        self.assertIn("verification", snapshot["error"].lower())

    async def test_retry_limit_is_respected(self) -> None:
        card = _card("Broken", "1-10 Employees")
        self.cards = [card]
        self.profiles = {card.profile_url: _profile("Broken", website="https://broken.test")}
        self.fail_profiles = (card.profile_url,)
        settings = self.jobs.settings()
        settings.max_retries = 1
        self.jobs.save_settings(settings)
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        snapshot = await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})
        self.assertEqual(snapshot["status"], JOB_COMPLETED)
        self.assertEqual(snapshot["errors"], 2)
        self.assertEqual(snapshot["retries"], 1)
        items = self.store.scan_items(job.job_id)
        self.assertEqual(items[0]["status"], "failed")
        self.assertEqual(items[0]["attempts"], 2)

    async def test_empty_scan_finishes_cleanly(self) -> None:
        self.cards = []
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        snapshot = await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})
        self.assertEqual(snapshot["status"], JOB_COMPLETED)
        self.assertEqual(snapshot["inspected"], 0)
        self.assertEqual(snapshot["new_verified"], 0)
        exporter = ExportService(self.cfg, self.store)
        with self.assertRaises(EmptyReportError):
            exporter.export_report("latest", "xlsx")

    async def test_duplicates_are_not_reported_as_new_leads(self) -> None:
        card = _card("Alpha", "1-10 Employees")
        self.cards = [card]
        self.profiles = {card.profile_url: _profile("Alpha", website="https://alpha.test")}
        first = await self.jobs.start(MODE_NEW, max_profiles=5)
        await self.wait_for_status(first.job_id, {JOB_COMPLETED})
        second = await self.jobs.start(MODE_NEW, max_profiles=5)
        snapshot = await self.wait_for_status(second.job_id, {JOB_COMPLETED})
        self.assertEqual(snapshot["new_verified"], 0)
        self.assertEqual(snapshot["duplicates_skipped"], 1)
        self.assertEqual(len(self.store.list_companies(limit=10)), 1)
        # The original discovery date is kept.
        company = self.store.list_companies(limit=1)[0]
        self.assertTrue(company.discovered_at)

    async def test_retry_only_touches_incomplete_records(self) -> None:
        card = _card("Alpha", "1-10 Employees")
        self.cards = [card]
        self.profiles = {card.profile_url: _profile("Alpha", website="https://alpha.test")}
        first = await self.jobs.start(MODE_NEW, max_profiles=5)
        await self.wait_for_status(first.job_id, {JOB_COMPLETED})
        self.collector = None
        self.cards = []
        retry = await self.jobs.start(MODE_RETRY, max_profiles=10)
        snapshot = await self.wait_for_status(retry.job_id, {JOB_COMPLETED})
        # Nothing is incomplete, so nothing is processed.
        self.assertEqual(snapshot["inspected"], 0)

    async def test_incomplete_records_are_retried_and_recovered(self) -> None:
        card = _card("Alpha", "1-10 Employees")
        self.cards = [card]
        self.profiles = {card.profile_url: _profile("Alpha", website="https://alpha.test")}
        self.verifier = FakeVerifier(WebsiteStatus.UNREACHABLE)
        self.jobs.verifier = self.verifier  # type: ignore[assignment]
        first = await self.jobs.start(MODE_NEW, max_profiles=5)
        snapshot = await self.wait_for_status(first.job_id, {JOB_COMPLETED})
        self.assertEqual(snapshot["incomplete"], 1)
        self.assertEqual(self.store.count_incomplete(), 1)

        # The site comes back: the retry recovers the lead.
        self.verifier = FakeVerifier(WebsiteStatus.VERIFIED)
        self.jobs.verifier = self.verifier  # type: ignore[assignment]
        self.collector = None
        retry = await self.jobs.start(MODE_RETRY, max_profiles=10)
        snapshot = await self.wait_for_status(retry.job_id, {JOB_COMPLETED})
        self.assertEqual(snapshot["websites_verified"], 1)
        self.assertEqual(self.store.count_incomplete(), 0)
        self.assertEqual(len(self.store.verified_companies()), 1)

    async def test_retry_rechecks_stored_websites_without_a_browser(self) -> None:
        from startup_agent.models import Company
        from startup_agent.normalize import evaluate_company

        company = Company(
            record_id="",
            name="Dataset Co",
            website="https://dataset.test",
            employee_count="1-10",
            source_name="dbpedia",
            source_url="https://dbpedia.test/Dataset_Co",
        )
        evaluate_company(company, 1, 10)
        self.store.upsert_company(company)
        self.assertEqual(self.store.count_unverified_web(), 1)
        job = await self.jobs.start(MODE_RETRY, max_profiles=10)
        snapshot = await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})
        self.assertEqual(snapshot["status"], JOB_COMPLETED)
        self.assertEqual(snapshot["websites_verified"], 1)
        self.assertEqual(snapshot["new_verified"], 1)
        self.assertEqual(len(self.store.verified_companies()), 1)
        # The original discovery date survives the retry.
        stored = self.store.verified_companies()[0]
        self.assertTrue(stored.discovered_at)
        self.assertEqual(stored.retry_count, 1)

    async def test_repeated_errors_are_reported_once(self) -> None:
        events: list[tuple[str, str]] = []

        async def event_sink(kind: str, snapshot: dict, detail: str) -> None:
            events.append((kind, detail))

        self.jobs.event_sink = event_sink
        cards = [_card(f"Broken {index}", "1-10 Employees") for index in range(6)]
        self.cards = cards
        self.profiles = {
            card.profile_url: _profile(card.name, website="https://broken.test")
            for card in cards
        }
        self.fail_profiles = tuple(card.profile_url for card in cards)
        settings = self.jobs.settings()
        settings.max_retries = 0
        self.jobs.save_settings(settings)
        job = await self.jobs.start(MODE_NEW, max_profiles=10)
        await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})
        kinds = [kind for kind, _ in events]
        self.assertIn("errors", kinds)
        self.assertEqual(kinds.count("errors"), 1)

    async def test_a_second_process_cannot_take_the_browser(self) -> None:
        # A run that could be resumed.
        self.cards = []
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})
        self.store.update_scan_job(job.job_id, status="paused")

        # Another live process owns the browser profile.
        self.store.set_meta("browser_owner", f"other-job:{os.getppid()}")
        with self.assertRaises(JobBusy):
            await self.jobs.resume_job(job.job_id)
        with self.assertRaises(JobBusy):
            await self.jobs.start(MODE_NEW, max_profiles=5)

        # A stale owner (dead process) is ignored.
        self.store.set_meta("browser_owner", "other-job:999999")
        self.store.update_scan_job(job.job_id, status="stopped")
        new_job = await self.jobs.start(MODE_NEW, max_profiles=5)
        self.assertIsNotNone(new_job)
        await self.wait_for_status(new_job.job_id, {JOB_COMPLETED, JOB_FAILED})

    async def test_settings_are_validated_and_persisted(self) -> None:
        settings = ScanSettings()
        settings.max_profiles = 5000
        with self.assertRaises(ValueError):
            settings.validate()
        settings.max_profiles = 50
        settings.delay_seconds = 0.01
        with self.assertRaises(ValueError):
            settings.validate()
        settings.delay_seconds = 3.0
        settings.max_retries = 9
        with self.assertRaises(ValueError):
            settings.validate()
        settings.max_retries = 1
        settings.validate()
        self.jobs.save_settings(settings)
        # A fresh manager reads the same values back.
        other = ScanManager(self.cfg, self.store)
        stored = other.settings()
        self.assertEqual(stored.max_profiles, 50)
        self.assertEqual(stored.delay_seconds, 3.0)
        self.assertEqual(stored.max_retries, 1)
        self.assertEqual(stored.retry_incomplete, True)
        await other.close()

    async def test_completed_runs_cannot_be_resumed(self) -> None:
        self.cards = []
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        await self.wait_for_status(job.job_id, {JOB_COMPLETED})
        with self.assertRaises(JobError):
            await self.jobs.resume_job(job.job_id)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
class ReportTests(ScanManagerTestCase):
    async def _run_one(self) -> None:
        card = _card("Alpha", "1-10 Employees")
        self.cards = [card]
        self.profiles = {card.profile_url: _profile("Alpha", website="https://alpha.test")}
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})

    async def test_reports_use_the_same_schema(self) -> None:
        await self._run_one()
        exporter = ExportService(self.cfg, self.store)
        for kind in ("csv", "json", "xlsx"):
            result = exporter.export_report("verified", kind)
            self.assertEqual(result.row_count, 1)
        csv_result = exporter.export_report("verified", "csv")
        with csv_result.path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(list(rows[0].keys()), list(REPORT_HEADERS.values()))
        self.assertEqual(rows[0]["Startup Name"], "Alpha")
        self.assertEqual(rows[0]["Official Website"], "https://alpha.test")
        self.assertEqual(rows[0]["Employee Range"], "1-10 Employees")
        self.assertEqual(rows[0]["Exact Member Count"], "")
        self.assertEqual(rows[0]["Website Verification Status"], "verified")
        self.assertEqual(rows[0]["Processing Status"], "verified")
        self.assertIn("Wellfound", rows[0]["Source of Website Evidence"])

        json_result = exporter.export_report("verified", "json")
        payload = json.loads(json_result.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["records"][0]["Startup Name"], "Alpha"
        )
        self.assertNotIn("record_id", payload["records"][0])

    async def test_excel_report_has_filters_frozen_header_and_links(self) -> None:
        await self._run_one()
        exporter = ExportService(self.cfg, self.store)
        result = exporter.export_report("verified", "xlsx")
        with zipfile.ZipFile(result.path) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
            rels = archive.read("xl/worksheets/_rels/sheet1.xml.rels").decode("utf-8")
        self.assertIn("<autoFilter", sheet)
        self.assertIn('state="frozen"', sheet)
        self.assertIn("<hyperlinks>", sheet)
        self.assertIn("https://alpha.test", rels)
        self.assertIn("TargetMode=\"External\"", rels)
        self.assertIn("<cols>", sheet)
        for header in REPORT_HEADERS.values():
            self.assertIn(header, sheet)

    async def test_latest_run_report_only_holds_that_run(self) -> None:
        await self._run_one()
        exporter = ExportService(self.cfg, self.store)
        result = exporter.export_report("latest", "csv")
        with result.path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Startup Name"], "Alpha")

    async def test_run_history_report(self) -> None:
        await self._run_one()
        exporter = ExportService(self.cfg, self.store)
        result = exporter.export_report("runs", "csv")
        with result.path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        self.assertIn("Run ID", rows[0])
        self.assertIn("Websites verified", rows[0])

    async def test_run_summary_text(self) -> None:
        await self._run_one()
        exporter = ExportService(self.cfg, self.store)
        job_id = self.store.latest_scan_job()["job_id"]
        result = exporter.export_run_summary(job_id)
        text = result.path.read_text(encoding="utf-8")
        self.assertIn(job_id, text)
        self.assertIn("Websites verified", text)
        self.assertIn("Alpha", text)

    async def test_empty_report_does_not_write_a_file(self) -> None:
        exporter = ExportService(self.cfg, self.store)
        before = list(self.cfg.export_dir.glob("*")) if self.cfg.export_dir.exists() else []
        with self.assertRaises(EmptyReportError):
            exporter.export_report("verified", "csv")
        after = list(self.cfg.export_dir.glob("*")) if self.cfg.export_dir.exists() else []
        self.assertEqual(len(before), len(after))


# ---------------------------------------------------------------------------
# Telegram control panel
# ---------------------------------------------------------------------------
class TelegramControlPanelTests(ScanManagerTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot = Telegram(self.cfg, self.service, self.jobs)
        self.sent: list[str] = []
        self.documents: list[tuple[Path, str]] = []
        self.edits: list[tuple[int, str]] = []
        self._message_id = 100

        async def fake_send(text: str, keyboard=None, *, preview: bool = False) -> bool:
            self.sent.append(text)
            return True

        async def fake_send_with_id(text: str, keyboard=None) -> int:
            self.sent.append(text)
            self._message_id += 1
            return self._message_id

        async def fake_document(path: Path, caption: str = "") -> bool:
            self.documents.append((Path(path), caption))
            return True

        async def fake_edit(message_id: int, text: str, keyboard=None) -> bool:
            self.edits.append((message_id, text))
            return True

        self.bot.send = fake_send  # type: ignore[method-assign]
        self.bot.send_with_id = fake_send_with_id  # type: ignore[method-assign]
        self.bot.send_document = fake_document  # type: ignore[method-assign]
        self.bot.edit_message = fake_edit  # type: ignore[method-assign]
        self.bot.answer_callback = lambda callback_id, text="": asyncio.sleep(0)  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        await super().asyncTearDown()

    async def _callback(self, data: str, chat_id: int = 42) -> None:
        await self.bot._handle_callback(
            {
                "id": "cb",
                "data": data,
                "message": {"message_id": 11, "chat": {"id": chat_id}},
            }
        )

    async def test_main_keyboard_has_the_three_core_actions(self) -> None:
        keyboard = self.bot.main_keyboard()["keyboard"]
        labels = [button["text"] for row in keyboard for button in row]
        self.assertEqual(labels, ["🚀 Start Scan", "⏹ Stop", "📥 Download"])

    async def test_download_menu_has_the_two_download_actions(self) -> None:
        keyboard = self.bot.download_keyboard()["inline_keyboard"]
        labels = [button["text"] for row in keyboard for button in row]
        self.assertEqual(
            labels,
            ["📄 Download this file", "🗂 Download all past"],
        )

    async def test_start_command_sends_the_welcome_message(self) -> None:
        await self.bot._handle_command("/start")
        self.assertEqual(self.sent[-1], WELCOME_TEXT)
        await self.bot._handle_command("/menu")
        self.assertEqual(self.sent[-1], WELCOME_TEXT)

    async def test_main_buttons_are_wired_to_their_actions(self) -> None:
        calls: list[str] = []

        async def fake_start() -> None:
            calls.append("start")

        async def fake_stop() -> None:
            calls.append("stop")

        async def fake_download(message_id=None) -> None:
            calls.append("download")

        self.bot.start_scan = fake_start  # type: ignore[method-assign]
        self.bot.stop_scan = fake_stop  # type: ignore[method-assign]
        self.bot._show_download = fake_download  # type: ignore[method-assign]

        await self.bot._handle_main_button(START_SCAN)
        await self.bot._handle_main_button(STOP)
        await self.bot._handle_main_button(DOWNLOAD)
        self.assertEqual(calls, ["start", "stop", "download"])

    async def test_start_button_starts_a_scan(self) -> None:
        card = _card("Alpha", "1-10 Employees")
        self.cards = [card]
        self.profiles = {card.profile_url: _profile("Alpha", website="https://alpha.test")}
        await self.bot._handle_main_button(START_SCAN)
        job = self.jobs.latest_job()
        self.assertIsNotNone(job)
        self.assertEqual(self.sent[-1], "🏃 Running")
        await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})

    async def test_start_scan_starts_immediately(self) -> None:
        card = _card("Alpha", "1-10 Employees")
        self.cards = [card]
        self.profiles = {card.profile_url: _profile("Alpha", website="https://alpha.test")}
        await self._callback("m:scan:go")
        job = self.jobs.latest_job()
        self.assertIsNotNone(job)
        self.assertEqual(self.sent[-1], "🏃 Running")
        await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})
        self.assertEqual(self.edits[-1][1], "✅ Done")

    async def test_stop_control_reaches_the_worker(self) -> None:
        card = _card("Alpha", "1-10 Employees")
        self.cards = [card]
        self.profiles = {card.profile_url: _profile("Alpha", website="https://alpha.test")}
        gate = asyncio.Event()
        self.gate = gate
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        for _ in range(300):
            if self.collector is not None and self.collector.visited:
                break
            await asyncio.sleep(0.02)
        await self._callback("m:stop:yes")
        self.assertIn(
            self.jobs.snapshot(job.job_id)["status"],
            {JOB_STOPPING, JOB_STOPPED},
        )
        gate.set()
        snapshot = await self.wait_for_status(job.job_id, {JOB_STOPPED, JOB_COMPLETED})
        self.assertIn(snapshot["status"], {JOB_STOPPED, JOB_COMPLETED})
        words = [text for _, text in self.edits] + self.sent
        self.assertTrue(any("Stopp" in word or "Done" in word for word in words))

    async def test_live_status_is_one_word(self) -> None:
        card = _card("Alpha", "1-10 Employees")
        self.cards = [card]
        self.profiles = {card.profile_url: _profile("Alpha", website="https://alpha.test")}
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})
        self.assertEqual(self.edits[-1][1], "✅ Done")
        for text in self.sent + [edit[1] for edit in self.edits]:
            self.assertNotIn("Inspected", text)
            self.assertNotIn("\n", text)

    async def test_this_file_download_sends_a_document(self) -> None:
        card = _card("Alpha", "1-10 Employees")
        self.cards = [card]
        self.profiles = {card.profile_url: _profile("Alpha", website="https://alpha.test")}
        job = await self.jobs.start(MODE_NEW, max_profiles=5)
        await self.wait_for_status(job.job_id, {JOB_COMPLETED, JOB_FAILED})
        await self._callback("m:download:this")
        self.assertTrue(self.documents)
        path, caption = self.documents[-1]
        self.assertTrue(path.exists())
        self.assertEqual(path.suffix, ".xlsx")
        self.assertIn("row(s)", caption)

    async def test_empty_download_says_empty_and_sends_no_file(self) -> None:
        await self._callback("m:download:this")
        self.assertFalse(self.documents)
        self.assertEqual(self.sent[-1], "📭 Empty")

    async def test_all_past_download_sends_a_zip(self) -> None:
        self.cfg.export_dir.mkdir(parents=True, exist_ok=True)
        sample = self.cfg.export_dir / "verified-sample.csv"
        sample.write_text("name\nAlpha\n", encoding="utf-8")
        await self._callback("m:download:past")
        self.assertTrue(self.documents)
        path, caption = self.documents[-1]
        self.assertEqual(path.suffix, ".zip")
        self.assertIn("file(s)", caption)
        with zipfile.ZipFile(path) as archive:
            self.assertIn("verified-sample.csv", archive.namelist())

    async def test_old_buttons_just_return_to_the_small_menu(self) -> None:
        await self._callback("m:help")
        self.assertEqual(self.sent[-1], "🤖 Wellfound Scanner")
        self.assertNotIn(self.cfg.telegram_token, self.sent[-1])
        await self._callback("m:runs")
        self.assertEqual(self.sent[-1], "🤖 Wellfound Scanner")

    async def test_unauthorized_users_cannot_control_the_agent(self) -> None:
        before = len(self.edits)
        await self._callback("m:scan:go", chat_id=999)
        self.assertEqual(len(self.edits), before)
        self.assertFalse(self.sent)
        self.assertIsNone(self.jobs.active_job())
        self.assertIsNone(self.jobs.latest_job())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
