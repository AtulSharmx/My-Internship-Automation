"""Offline smoke tests - no network, no browser.

    python -m unittest discover -s tests

Covers the logic that is easy to break while editing: config parsing, the
SQLite store, de-duplication, notification rendering and the backoff maths.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import setup_wizard  # noqa: E402
from wfbot import guards, human  # noqa: E402
from wfbot.config import (  # noqa: E402
    ROLE_LIBRARY,
    ROOT,
    Config,
    ConfigError,
    negative_title_hit,
)
from wfbot.models import (  # noqa: E402
    Job,
    Outcome,
    RunSummary,
    RuntimeState,
    Status,
    company_key,
    display_title,
    utcnow,
)
from wfbot.runner import Runner  # noqa: E402
from wfbot.store import Store  # noqa: E402
from wfbot.telegram import (  # noqa: E402
    BTN_APPS,
    BTN_MORE,
    BTN_PAUSE_ACTION,
    BTN_RESUME_ACTION,
    BTN_RUN,
    BTN_STATUS,
    BTN_SYNC,
    Telegram,
    esc,
)
from wfbot.wellfound import (  # noqa: E402
    APPLIED_STATE_RE,
    JOB_SPECIFIC_RE,
    LOCATION_MISMATCH_RE,
    LoginRequired,
    answer_key,
    card_location_verdict,
    format_age,
    is_unpaid,
    job_from_application_href,
    parse_application_date,
    parse_card_age,
    parse_card_location,
    parse_company_size,
    parse_job_type,
    parse_location,
    parse_posted_age,
    parse_response,
    parse_stipend,
    slugify_title,
)


def make_config(**overrides) -> Config:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
        "TELEGRAM_CHAT_ID": "-1001234567890",
        "WF_ROLES": "fullstack,backend,web",
        # Most runner tests use the public-page fakes. The signed-in search path
        # has its own focused test below.
        "WF_USE_SEARCH_PAGE": "false",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    with patch.dict(os.environ, env, clear=True):
        return Config.from_env()


class SetupWizardTest(unittest.TestCase):
    def test_env_values_roundtrip_without_truncation(self) -> None:
        values = {
            "TELEGRAM_BOT_TOKEN": "123456:TESTTOKEN",
            "TELEGRAM_CHAT_ID": "-1001234567890",
            "WF_PASSWORD": 'p@ss w#rd "$HOME" \\ end',
            "WF_COVER_NOTE": "Hi # there\n$HOME is literal",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            with patch.object(setup_wizard, "ENV_PATH", path):
                setup_wizard.write_env(values)
                parsed = setup_wizard.read_env()
        for key, value in values.items():
            self.assertEqual(parsed[key], value)


class ConfigTest(unittest.TestCase):
    def test_roles_selected(self) -> None:
        cfg = make_config()
        self.assertEqual([r.key for r in cfg.roles], ["fullstack", "backend", "web"])

    def test_unknown_role_rejected(self) -> None:
        os.environ["WF_ROLES"] = "fullstack,astronaut"
        try:
            with self.assertRaises(ConfigError):
                Config.from_env()
        finally:
            os.environ.pop("WF_ROLES", None)

    def test_interactive_question_settings(self) -> None:
        cfg = make_config(WF_ASK_QUESTIONS="false", WF_QUESTION_TIMEOUT_SECONDS="120")
        self.assertFalse(cfg.ask_questions)
        self.assertEqual(cfg.question_timeout_seconds, 120)
        # Defaults: ask by default, 15 minute timeout, no application cap.
        default = make_config()
        self.assertTrue(default.ask_questions)
        self.assertEqual(default.question_timeout_seconds, 900)
        self.assertEqual(default.max_apps_per_day, 0)
        self.assertEqual(default.max_apps_per_run, 0)
        self.assertFalse(default.allow_size_fallback)
        self.assertEqual(default.daily_run_at, "20:00")
        self.assertEqual(default.max_job_age_days, 1.0)
        self.assertTrue(default.require_posted_age)

    def test_quality_filter_settings(self) -> None:
        cfg = make_config()
        self.assertFalse(cfg.skip_unpaid)  # paid and unpaid are both welcome
        self.assertTrue(cfg.answer_bank)
        self.assertTrue(cfg.screenshot_proof)
        self.assertIn("marketing", cfg.negative_keywords)
        self.assertEqual(cfg.proof_dir, ROOT / "data" / "proof")

        on = make_config(WF_SKIP_UNPAID="true")
        self.assertTrue(on.skip_unpaid)

        off = make_config(
            WF_SKIP_UNPAID="false",
            WF_ANSWER_BANK="false",
            WF_SCREENSHOT_PROOF="false",
            WF_NEGATIVE_KEYWORDS="none",
        )
        self.assertFalse(off.skip_unpaid)
        self.assertFalse(off.answer_bank)
        self.assertFalse(off.screenshot_proof)
        self.assertEqual(off.negative_keywords, ())

    def test_phase2_settings(self) -> None:
        cfg = make_config()
        self.assertEqual(cfg.company_cooldown_days, 30)
        self.assertEqual(cfg.max_retries, 2)
        self.assertTrue(cfg.reconcile_applications)
        self.assertTrue(cfg.profile_preflight)

        custom = make_config(
            WF_COMPANY_COOLDOWN_DAYS="0",
            WF_MAX_RETRIES="0",
            WF_RECONCILE_APPLICATIONS="false",
            WF_PROFILE_PREFLIGHT="false",
        )
        self.assertEqual(custom.company_cooldown_days, 0)
        self.assertEqual(custom.max_retries, 0)
        self.assertFalse(custom.reconcile_applications)
        self.assertFalse(custom.profile_preflight)

    def test_report_hours_parsing(self) -> None:
        cfg = make_config(WF_DAILY_REPORT_HOURS="7,19")
        self.assertEqual(cfg.daily_report_hours, (7, 19))
        default = make_config(WF_DAILY_REPORT_HOURS="")
        self.assertEqual(default.daily_report_hours, (20,))
        legacy = make_config(WF_DAILY_REPORT_HOURS="", WF_DAILY_REPORT_HOUR="21")
        self.assertEqual(legacy.daily_report_hours, (21,))
        with self.assertRaises(ConfigError):
            make_config(WF_DAILY_REPORT_HOURS="25")

    def test_missing_telegram_rejected_unless_optional(self) -> None:
        saved = {k: os.environ.pop(k, None) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
        try:
            with self.assertRaises(ConfigError):
                Config.from_env()
            cfg = Config.from_env(require_telegram=False)
            self.assertEqual(cfg.telegram_token, "")
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_search_urls(self) -> None:
        cfg = make_config()
        # Fallback discovery uses public role pages - never /search.
        self.assertEqual(
            cfg.search_urls(ROLE_LIBRARY["backend"]),
            [
                "https://wellfound.com/role/r/backend-developer",
                "https://wellfound.com/role/r/backend-engineer",
            ],
        )
        self.assertEqual(cfg.search_url(ROLE_LIBRARY["backend"]),
                         "https://wellfound.com/role/r/backend-developer")
        cfg.search_url_template = "https://wellfound.com/jobs?query={query}"
        urls = cfg.search_urls(ROLE_LIBRARY["backend"])
        self.assertEqual(len(urls), 1)
        self.assertIn("query=Backend", urls[0])

    def test_every_role_has_role_pages(self) -> None:
        for key, role in ROLE_LIBRARY.items():
            self.assertTrue(role.slugs, f"{key} has no listing page")
            for slug in role.slugs:
                self.assertNotIn("?", slug)
                self.assertEqual(slug, slug.strip().lower())

    def test_role_matching(self) -> None:
        backend = ROLE_LIBRARY["backend"]
        self.assertTrue(backend.matches("Backend Developer Intern (Remote)"))
        self.assertTrue(backend.matches("Intern, Back-end Engineering"))
        self.assertTrue(backend.matches("Backend Engineering Co-op"))
        self.assertTrue(backend.matches("Backend Engineering Apprenticeship"))
        self.assertFalse(backend.matches("Product Design Intern"))
        self.assertFalse(backend.matches("Backend Engineer"))  # full-time is never a match
        self.assertFalse(backend.matches("Internal Tools Backend Engineer"))  # not an internship
        self.assertTrue(ROLE_LIBRARY["fullstack"].matches("Full-Stack Engineering Intern"))
        self.assertTrue(ROLE_LIBRARY["web"].matches("Frontend Web Developer Intern"))

    def test_negative_keywords_reject_non_engineering_titles(self) -> None:
        negatives = make_config().negative_keywords
        self.assertFalse(
            ROLE_LIBRARY["mobile"].matches("Mobile Marketing Intern", negatives)
        )
        self.assertFalse(
            ROLE_LIBRARY["fullstack"].matches("AI-First Marketing Intern", negatives)
        )
        self.assertFalse(
            ROLE_LIBRARY["web"].matches(
                "AI-Assisted Content Writing Intern", negatives
            )
        )
        self.assertFalse(
            ROLE_LIBRARY["web"].matches("Frontend Design Intern", negatives)
        )
        self.assertFalse(
            ROLE_LIBRARY["mobile"].matches("Mobile Growth Intern", negatives)
        )
        # Real engineering intern titles still pass.
        self.assertTrue(
            ROLE_LIBRARY["mobile"].matches("React Native Developer Intern", negatives)
        )
        self.assertTrue(
            ROLE_LIBRARY["fullstack"].matches("MERN Stack Developer Intern", negatives)
        )
        # Word boundaries: "sales" must not veto "Salesforce".
        self.assertIsNone(
            negative_title_hit("Salesforce Developer Intern", negatives)
        )
        self.assertEqual(negative_title_hit("Sales Intern", negatives), "sales")

    def test_empty_paths_fall_back_to_project_defaults(self) -> None:
        cfg = make_config(WF_PROFILE_DIR="", WF_DB_PATH="", WF_LOG_DIR="")
        self.assertEqual(cfg.profile_dir, ROOT / "browser_profile")
        self.assertEqual(cfg.db_path, ROOT / "data" / "applications.db")
        self.assertEqual(cfg.log_dir, ROOT / "logs")

    def test_relative_paths_are_anchored_to_project_root(self) -> None:
        cfg = make_config(WF_PROFILE_DIR="profiles/work", WF_DB_PATH="tmp/test.db")
        self.assertEqual(cfg.profile_dir, ROOT / "profiles" / "work")
        self.assertEqual(cfg.db_path, ROOT / "tmp" / "test.db")


class ModelTest(unittest.TestCase):
    def test_display_title_cleans_scraped_prefix_and_company(self) -> None:
        self.assertEqual(
            display_title("Frontend Intern at Acme", "Acme"), "Frontend Intern"
        )
        self.assertEqual(
            display_title("Job Title: Backend Intern at Acme", "Acme"),
            "Backend Intern",
        )
        self.assertEqual(display_title("", "Acme"), "Untitled role")

    def test_job_from_href(self) -> None:
        job = Job.from_href("/jobs/4866642-planning-executive-strategy-operational-planning")
        assert job is not None
        self.assertEqual(job.job_id, "4866642")
        self.assertEqual(job.slug, "planning-executive-strategy-operational-planning")
        self.assertEqual(
            job.url,
            "https://wellfound.com/jobs/4866642-planning-executive-strategy-operational-planning",
        )

    def test_job_from_absolute_href_and_query(self) -> None:
        job = Job.from_href("https://wellfound.com/jobs/7-web-intern?autoOpenApplication=true")
        assert job is not None
        self.assertEqual(job.job_id, "7")
        self.assertEqual(job.slug, "web-intern")
        self.assertIsNone(Job.from_href("https://example.com/jobs/1-elsewhere"))
        relative = Job.from_href("/jobs/8-web-intern?ref=listing")
        assert relative is not None
        self.assertEqual(relative.slug, "web-intern")

    def test_job_from_non_job_href(self) -> None:
        self.assertIsNone(Job.from_href("/jobs"))
        self.assertIsNone(Job.from_href("/jobs/signup?after_sign_in=%2Fjobs"))
        self.assertIsNone(Job.from_href("/company/paralleldots/jobs"))

    def test_company_key_normalizes_legal_suffixes(self) -> None:
        self.assertEqual(company_key("Acme Technologies Pvt Ltd"), "acme")
        self.assertEqual(company_key("ACME, Inc."), "acme")
        self.assertEqual(company_key("Heather AI"), "heather")
        self.assertEqual(company_key("Beta Labs"), "beta")
        self.assertEqual(company_key("AI"), "ai")  # never becomes empty
        self.assertEqual(company_key(""), "")

    def test_summary_counters(self) -> None:
        summary = RunSummary()
        job = Job(job_id="1", slug="s", url="u", title="Backend Developer Intern")
        summary.add(Outcome(job=job, status=Status.APPLIED))
        summary.add(Outcome(job=job, status=Status.FAILED, detail="boom"))
        summary.finished_at = summary.started_at + timedelta(seconds=125)
        self.assertEqual(summary.applied, 1)
        self.assertEqual(summary.failures, 1)
        self.assertEqual(summary.duration, "2m 05s")
        self.assertIn("Applied", summary.one_line())


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.job = Job(
            job_id="42", slug="backend-intern", url="https://wellfound.com/jobs/42-backend-intern",
            title="Backend Developer Intern", company="Acme", role_key="backend",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_record_and_dedupe(self) -> None:
        self.assertFalse(self.store.is_seen("42"))
        self.store.record(Outcome(job=self.job, status=Status.APPLIED, detail="ok"))
        self.assertTrue(self.store.is_seen("42"))
        self.assertEqual(self.store.total, 1)
        self.assertEqual(self.store.counts()[Status.APPLIED], 1)

    def test_record_is_idempotent(self) -> None:
        self.store.record(Outcome(job=self.job, status=Status.FAILED, detail="first"))
        self.store.record(Outcome(job=self.job, status=Status.APPLIED, detail="second"))
        self.assertEqual(self.store.total, 1)
        row = self.store.last_applied(1)[0]
        self.assertEqual(row["status"], Status.APPLIED)
        self.assertEqual(row["detail"], "second")

    def test_failed_jobs_retry_until_cap(self) -> None:
        self.store.record(Outcome(job=self.job, status=Status.FAILED, detail="boom"))
        self.assertFalse(self.store.is_seen("42", 2))  # attempt 1 -> retry
        self.store.record(Outcome(job=self.job, status=Status.FAILED, detail="boom"))
        self.assertFalse(self.store.is_seen("42", 2))  # attempt 2 -> retry
        self.store.record(Outcome(job=self.job, status=Status.FAILED, detail="boom"))
        self.assertTrue(self.store.is_seen("42", 2))  # attempt 3 -> give up
        # With retries disabled a failure is terminal immediately.
        self.assertTrue(self.store.is_seen("42", 0))

    def test_company_cooldown_normalizes_legal_suffixes(self) -> None:
        company_job = Job(
            job_id="43",
            slug="fs-intern",
            url="https://wellfound.com/jobs/43-fs-intern",
            title="Full Stack Intern",
            company="Acme Technologies Pvt Ltd",
            role_key="fullstack",
        )
        self.store.record(Outcome(job=company_job, status=Status.APPLIED))
        self.assertTrue(self.store.company_recent("Acme", 30))
        self.assertTrue(self.store.company_recent("ACME, Inc.", 30))
        self.assertFalse(self.store.company_recent("Beta", 30))
        self.assertFalse(self.store.company_recent("Acme", 0))

    def test_company_cooldown_verdict(self) -> None:
        from wfbot.wellfound import Wellfound

        self.store.record(
            Outcome(
                job=Job(
                    job_id="44",
                    slug="web-intern",
                    url="https://wellfound.com/jobs/44-web-intern",
                    title="Web Intern",
                    company="Acme Technologies",
                ),
                status=Status.APPLIED,
            )
        )
        wf = Wellfound(  # type: ignore[arg-type]
            None, make_config(WF_COMPANY_COOLDOWN_DAYS="30"), None, None, self.store
        )
        verdict = wf._company_verdict(
            Job(
                job_id="45",
                slug="web-intern-2",
                url="https://wellfound.com/jobs/45-web-intern-2",
                title="Web Intern",
                company="Acme",
            )
        )
        assert verdict is not None
        self.assertEqual(verdict[0], Status.SKIPPED_COMPANY)

        off = Wellfound(  # type: ignore[arg-type]
            None, make_config(WF_COMPANY_COOLDOWN_DAYS="0"), None, None, self.store
        )
        self.assertIsNone(
            off._company_verdict(
                Job(
                    job_id="45",
                    slug="web-intern-2",
                    url="https://wellfound.com/jobs/45-web-intern-2",
                    title="Web Intern",
                    company="Acme",
                )
            )
        )

    def test_sync_application_imports_manual_and_tracks_responses(self) -> None:
        manual = Job(
            job_id="500",
            slug="mern-intern",
            url="https://wellfound.com/jobs/500-mern-intern",
            title="MERN Intern",
            company="Manual Co",
            role_key="fullstack",
        )
        imported, updated = self.store.sync_application(manual, response="pending")
        self.assertTrue(imported)
        self.assertFalse(updated)
        self.assertTrue(self.store.is_seen("500"))
        self.assertEqual(self.store.response_counts(), {"pending": 1})

        imported, updated = self.store.sync_application(manual, response="accepted")
        self.assertFalse(imported)
        self.assertTrue(updated)
        self.assertEqual(self.store.response_counts(), {"accepted": 1})

        # A bot record keeps its status; only the response is refreshed.
        self.store.record(Outcome(job=self.job, status=Status.APPLIED))
        imported, updated = self.store.sync_application(
            self.job, response="not accepted"
        )
        self.assertFalse(imported)
        self.assertTrue(updated)
        row = self.store.conn.execute(
            "SELECT status, response FROM applications WHERE job_id = '42'"
        ).fetchone()
        assert row is not None
        self.assertEqual(row["status"], Status.APPLIED)
        self.assertEqual(row["response"], "not accepted")

        data = self.store.report()
        self.assertEqual(data["responses"]["not accepted"], 1)

        # An older row with no company (the title-only fallback) gets backfilled,
        # so company-level de-duplication can work on it too.
        blank = Job(
            job_id="501",
            slug="blank-intern",
            url="https://wellfound.com/jobs/501-blank-intern",
            title="",
            company="",
        )
        self.store.record(Outcome(job=blank, status=Status.APPLIED))
        filled = Job(
            job_id="501",
            slug="blank-intern",
            url="https://wellfound.com/jobs/501-blank-intern",
            title="Filled Intern",
            company="Filled Co",
        )
        imported, updated = self.store.sync_application(filled)
        self.assertFalse(imported)
        self.assertTrue(updated)
        row = self.store.conn.execute(
            "SELECT company, title FROM applications WHERE job_id = '501'"
        ).fetchone()
        assert row is not None
        self.assertEqual(row["company"], "Filled Co")
        self.assertEqual(row["title"], "Filled Intern")

        # The applications page has no public slug, so imported rows start with
        # a bare /jobs/{id} URL; a later sync upgrades it to a working link.
        bare = Job(
            job_id="502",
            slug="502",
            url="https://wellfound.com/jobs/502",
            title="",
        )
        self.store.record(Outcome(job=bare, status=Status.ALREADY_APPLIED))
        proper = Job(
            job_id="502",
            slug="web-intern",
            url="https://wellfound.com/jobs/502-web-intern",
            title="Web Intern",
        )
        _, updated = self.store.sync_application(proper)
        self.assertTrue(updated)
        row = self.store.conn.execute(
            "SELECT url FROM applications WHERE job_id = '502'"
        ).fetchone()
        assert row is not None
        self.assertEqual(row["url"], "https://wellfound.com/jobs/502-web-intern")

    def test_pause_and_backoff_meta(self) -> None:
        self.assertFalse(self.store.paused)
        self.store.set_paused(True)
        self.assertTrue(self.store.paused)
        self.store.set_paused(False)
        self.store.set_consecutive_failures(3)
        self.assertEqual(self.store.consecutive_failures, 3)

    def test_run_log(self) -> None:
        summary = RunSummary()
        summary.add(Outcome(job=self.job, status=Status.APPLIED))
        summary.finished_at = utcnow()
        summary.stopped_reason = "no new listings"
        self.store.save_run(summary)
        row = self.store.last_run()
        assert row is not None
        self.assertEqual(row["applied"], 1)
        self.assertEqual(row["note"], "no new listings")

    def test_report_windows_roles_and_goal(self) -> None:
        now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        records = [
            ("100", Status.APPLIED, 0, "fullstack", "Acme", 0),
            ("101", Status.APPLIED, 2, "web", "Acme", 0),
            ("102", Status.APPLIED, 5, "fullstack", "Beta", 0),
            ("103", Status.APPLIED, 26, "web", "Beta", 0),
            ("104", Status.APPLIED, 27, "fullstack", "Gamma", 0),
            ("105", Status.APPLIED, 72, "web", "Gamma", 0),
            ("106", Status.APPLIED, 74, "fullstack", "Delta", 0),
            ("107", Status.APPLIED, 240, "web", "Old", 0),
            ("108", Status.UNVERIFIED, 1, "web", "Maybe", 0),
            ("109", Status.FAILED, 50, "fullstack", "Oops", 0),
            ("110", Status.SKIPPED_SIZE, 1, "", "Big", 0),
            ("111", Status.ALREADY_APPLIED, 30, "web", "Before", 0),
        ]
        for job_id, status, hours_ago, role_key, company, _ in records:
            job = Job(
                job_id=job_id,
                slug=f"job-{job_id}",
                url=f"https://wellfound.com/jobs/{job_id}",
                title="Intern",
                company=company,
                role_key=role_key,
            )
            self.store.record(
                Outcome(
                    job=job,
                    status=status,
                    finished_at=now - timedelta(hours=hours_ago),
                )
            )

        self.assertEqual(self.store.applied_today(now=now), 3)

        data = self.store.report(
            now=now, goal=10, deadline="2026-10-31", daily_cap=40
        )
        self.assertEqual(data["today"]["applied"], 3)
        self.assertEqual(data["last_48h"]["applied"], 5)
        self.assertEqual(data["week"]["applied"], 7)
        self.assertEqual(data["all"]["applied"], 8)
        self.assertEqual(sum(day["count"] for day in data["daily"]), 7)
        self.assertEqual(data["roles"]["all"]["fullstack"], 4)
        self.assertEqual(data["roles"]["all"]["web"], 4)
        self.assertEqual(data["roles"]["today"]["fullstack"], 2)
        self.assertEqual(data["top_companies"][0], ("Acme", 2))
        self.assertEqual(data["submitted"], 9)  # 8 applied + 1 unverified
        self.assertEqual(data["confirmed"], 8)
        self.assertEqual(data["unconfirmed"], 1)
        self.assertAlmostEqual(data["success_rate"], 8 / 9 * 100, places=4)
        self.assertEqual(data["goal"], 10)
        self.assertEqual(data["days_left"], 12)
        self.assertGreater(data["required_per_day"], 0)
        self.assertEqual(data["projected_date"], now.date() + timedelta(days=3))
        self.assertEqual(data["daily_cap"], 40)
        self.assertEqual(data["streak"], 2)
        self.assertEqual(data["best_day"]["count"], 3)

    def test_answer_bank_roundtrip(self) -> None:
        self.assertIsNone(self.store.get_answer("linkedin url"))
        self.store.save_answer("linkedin url", "LinkedIn URL*", "https://linkedin.com/in/me")
        self.assertEqual(
            self.store.get_answer("linkedin url"), "https://linkedin.com/in/me"
        )
        # Saving the same question updates instead of duplicating.
        self.store.save_answer("linkedin url", "LinkedIn URL", "https://linkedin.com/in/new")
        rows = self.store.list_answers()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["answer"], "https://linkedin.com/in/new")
        self.assertTrue(self.store.forget_answer(rows[0]["id"]))
        self.assertIsNone(self.store.get_answer("linkedin url"))
        self.assertFalse(self.store.forget_answer(rows[0]["id"]))

    def test_bank_skips_company_specific_long_and_original_answers(self) -> None:
        from wfbot.wellfound import Wellfound

        wf = Wellfound(None, make_config(), None, None, self.store)  # type: ignore[arg-type]
        job = Job(
            job_id="77", slug="fs-intern", url="https://wellfound.com/jobs/77-fs-intern",
            title="Full Stack Intern", company="Infisical",
        )
        company_q = {
            "label": "How did you discover Infisical?",
            "type": "radio",
            "name": "customQuestionAnswers[1][jobListingQuestionOptionId]",
        }
        wf._bank_save(company_q, "GitHub", job)
        self.assertIsNone(self.store.get_answer(answer_key("How did you discover Infisical?")))

        wf._bank_save({"label": "LinkedIn URL", "type": "text"}, "https://linkedin.com/in/me", job)
        self.assertEqual(self.store.get_answer("linkedin url"), "https://linkedin.com/in/me")

        wf._bank_save({"label": "Primary expertise", "type": "text"}, "x" * 300, job)
        self.assertIsNone(self.store.get_answer("primary expertise"))

        consent = {"label": "I agree to the privacy policy", "type": "checkbox"}
        wf._bank_save(consent, "Yes", job)
        self.assertIsNone(self.store.get_answer("i agree to the privacy policy"))
        self.assertIsNone(wf._bank_lookup(consent, job))

        # A banked answer is reused, but a company-named question never is.
        self.assertEqual(
            wf._bank_lookup({"label": "LinkedIn URL", "type": "text"}, job),
            "https://linkedin.com/in/me",
        )
        self.assertIsNone(wf._bank_lookup(company_q, job))

    def test_goal_can_be_persisted(self) -> None:
        self.store.set_goal(250, "2027-01-31")
        self.assertEqual(self.store.get_meta("goal_applied"), "250")
        data = self.store.report(goal=100, deadline="2026-12-31")
        self.assertEqual(data["goal"], 250)
        self.assertEqual(data["deadline"], "2027-01-31")


class DiscoveryTest(unittest.TestCase):
    """The row -> Job filter, which is where a bad match would cost an application."""

    def setUp(self) -> None:
        from wfbot.wellfound import Wellfound

        self.wf = Wellfound(  # type: ignore[arg-type]
            None,
            make_config(WF_ALLOW_SIZE_FALLBACK="true", WF_SKIP_UNPAID="true"),
            None,
            None,
        )

    def test_rows_are_filtered_and_deduped(self) -> None:
        rows = [
            {
                "href": "/jobs/1-backend-intern",
                "title": "Backend Developer Intern",
                "company": "Acme",
                "meta": "$1k - $2k",
                "has_apply": True,
                "apply_disabled": False,
            },
            {"href": "/jobs/1-backend-intern", "title": "Backend Developer Intern"},  # duplicate
            {"href": "/jobs/2-backend-engineer", "title": "Backend Engineer"},  # full time
            {"href": "/jobs/5-backend-developer-intern-clone",
             "title": "Backend Developer Intern (Clone)"},  # duplicate posting
            {"href": "/jobs/signup?after_sign_in=%2Fjobs", "title": "Sign up"},  # not a job
            {"href": "/jobs/3-web-intern", "title": "Web Developer Intern"},  # other role
            {"href": "/jobs/4-login", "title": "Log in"},  # nav noise
        ]
        jobs = self.wf._to_jobs(rows, ROLE_LIBRARY["backend"], set())
        self.assertEqual([job.job_id for job in jobs], ["1"])
        self.assertEqual(jobs[0].company, "Acme")
        self.assertEqual(jobs[0].meta, "$1k - $2k")
        self.assertEqual(jobs[0].role_key, "backend")
        self.assertTrue(jobs[0].has_inline_apply)

    def test_company_falls_back_to_meta_when_row_has_no_company_link(self) -> None:
        rows = [
            {
                "href": "/jobs/9-web-intern",
                "title": "Web Developer Intern",
                "company": "",
                "meta": "Acme Rocket \u2022 $40k \u2022 Remote",
            }
        ]
        jobs = self.wf._to_jobs(rows, ROLE_LIBRARY["web"], set())
        # company extraction happens in the page JS; here we only prove the row
        # survives with its metadata intact when the company field is empty.
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "")
        self.assertIn("Acme Rocket", jobs[0].meta)

    def test_card_age_and_stipend_parsing(self) -> None:
        self.assertAlmostEqual(parse_card_age("today"), 0.0)
        self.assertAlmostEqual(parse_card_age("yesterday"), 1.0)
        self.assertAlmostEqual(parse_card_age("2 weeks ago"), 14.0)
        self.assertAlmostEqual(parse_card_age("1 month ago"), 30.0)
        # "2 years of exp" is not an age and must stay permissive.
        self.assertIsNone(parse_card_age("2 years of exp"))
        self.assertEqual(parse_stipend("Full Stack Intern | $150k \u2013 $280k"), "$150k \u2013 $280k")
        self.assertEqual(parse_stipend("Web Intern | \u20b92.4L | Remote"), "\u20b92.4L")
        self.assertEqual(parse_stipend("no salary shown"), "")
        self.assertEqual(parse_job_type("Web Intern | Internship | Remote"), "Internship")
        self.assertEqual(parse_job_type("Web Intern | Full-time"), "Full-time")
        self.assertTrue(is_unpaid("AI ML Engineer Intern (Unpaid)"))
        self.assertTrue(is_unpaid("Sales Intern | commission-only"))
        self.assertTrue(is_unpaid("Web Intern | 0.1% \u2013 0.5%"))  # equity only
        self.assertFalse(is_unpaid("Full Stack Intern | \u20b920k/month"))
        self.assertFalse(is_unpaid("Web Intern | \u20b920k | 0.1% \u2013 0.5%"))
        self.assertEqual(
            parse_card_location("Web Intern | In office \u2022 San Francisco | today"),
            ("In office", "San Francisco"),
        )
        self.assertEqual(
            parse_card_location("Web Intern | Remote only \u2022 Everywhere | today"),
            ("Remote only", "Everywhere"),
        )
        self.assertIsNotNone(
            card_location_verdict("Web Intern | In office \u2022 San Francisco | today")
        )
        self.assertIsNone(
            card_location_verdict("Web Intern | In office \u2022 Gurugram | today")
        )
        self.assertIsNone(
            card_location_verdict("Web Intern | Remote \u2022 San Francisco | today")
        )

    def test_card_prefilter_drops_stale_and_unpaid_before_opening(self) -> None:
        rows = [
            {
                "href": "/jobs/1-web-intern",
                "title": "Web Developer Intern",
                "company": "Fresh",
                "meta": "Web Developer Intern | Internship | \u20b920k | Remote \u2022 India | today",
            },
            {
                "href": "/jobs/2-web-intern",
                "title": "Web Developer Intern",
                "company": "Stale",
                "meta": "Web Developer Intern | Internship | \u20b920k | Remote \u2022 India | 2 months ago",
            },
            {
                "href": "/jobs/3-web-intern",
                "title": "Web Developer Intern",
                "company": "Unpaid",
                "meta": "Web Developer Intern | Unpaid | Remote \u2022 India | today",
            },
            {
                "href": "/jobs/4-web-intern",
                "title": "Web Developer Intern",
                "company": "SF onsite",
                "meta": "Web Developer Intern | Internship | \u20b920k | In office \u2022 San Francisco | today",
            },
            {
                "href": "/jobs/5-web-intern",
                "title": "Web Developer Intern",
                "company": "Gurugram onsite",
                "meta": "Web Developer Intern | Internship | \u20b920k | In office \u2022 Gurugram | today",
            },
        ]
        jobs = self.wf._to_jobs(rows, ROLE_LIBRARY["web"], set())
        self.assertEqual([job.job_id for job in jobs], ["1", "5"])
        self.assertEqual(jobs[0].job_type, "Internship")
        self.assertEqual(jobs[0].stipend, "\u20b920k")
        self.assertAlmostEqual(jobs[0].posted_days, 0.0)

    def test_unpaid_listings_are_kept_when_filter_is_off(self) -> None:
        from wfbot.wellfound import Wellfound

        wf = Wellfound(  # type: ignore[arg-type]
            None, make_config(WF_SKIP_UNPAID="false"), None, None
        )
        rows = [
            {
                "href": "/jobs/3-web-intern",
                "title": "Web Developer Intern",
                "company": "Unpaid",
                "meta": "Web Developer Intern | Unpaid | Remote \u2022 India | today",
            }
        ]
        jobs = wf._to_jobs(rows, ROLE_LIBRARY["web"], set())
        self.assertEqual([job.job_id for job in jobs], ["3"])

    def test_search_cards_filter_size_and_strict_age(self) -> None:
        from wfbot.wellfound import Wellfound

        wf = Wellfound(  # type: ignore[arg-type]
            None,
            make_config(WF_ALLOW_SIZE_FALLBACK="false", WF_MAX_JOB_AGE_DAYS="1"),
            None,
            None,
        )
        rows = [
            {
                "href": "/jobs/1-web-intern",
                "title": "Web Developer Intern",
                "company": "Tiny",
                "company_size": "1-10 Employees",
                "meta": "Web Developer Intern | Remote only India | Posted today",
            },
            {
                "href": "/jobs/2-web-intern",
                "title": "Web Developer Intern",
                "company": "Too big",
                "company_size": "11-50 Employees",
                "meta": "Web Developer Intern | Remote only India | Posted today",
            },
            {
                "href": "/jobs/3-web-intern",
                "title": "Web Developer Intern",
                "company": "Yesterday",
                "company_size": "1-10 Employees",
                "meta": "Web Developer Intern | Remote only India | Posted yesterday",
            },
            {
                "href": "/jobs/4-web-intern",
                "title": "Web Developer Intern",
                "company": "No card size",
                "meta": "Web Developer Intern | Remote only India | Posted 2 hours ago",
            },
        ]
        jobs = wf._to_jobs(rows, ROLE_LIBRARY["web"], set())
        self.assertEqual([job.job_id for job in jobs], ["1", "4"])
        self.assertEqual(jobs[0].company_size, "1-10")

    def test_one_row_can_still_match_a_later_role(self) -> None:
        rows = [
            {
                "href": "/jobs/7-web-intern",
                "title": "Web Developer Intern",
                "meta": "Web Developer Intern | Remote \u2022 India | today",
            }
        ]
        seen: set[str] = set()
        self.assertEqual(self.wf._to_jobs(rows, ROLE_LIBRARY["backend"], seen), [])
        jobs = self.wf._to_jobs(rows, ROLE_LIBRARY["web"], seen)
        self.assertEqual([job.job_id for job in jobs], ["7"])

    def test_parse_response_statuses(self) -> None:
        self.assertEqual(parse_response("Pending"), "pending")
        self.assertEqual(parse_response("Application accepted"), "accepted")
        self.assertEqual(parse_response("Not Accepted"), "not accepted")
        self.assertEqual(parse_response("Rejected"), "not accepted")
        self.assertEqual(parse_response("Applied 3 days ago"), "")

    def test_application_href_and_history_date(self) -> None:
        job = job_from_application_href(
            "/jobs/applications/988097316-4829680", "Web Developer Intern"
        )
        assert job is not None
        self.assertEqual(job.job_id, "4829680")
        self.assertEqual(
            job.url, "https://wellfound.com/jobs/4829680-web-developer-intern"
        )
        self.assertEqual(slugify_title("Full-Stack Engineer Intern (Python, Golang, React & AWS)"),
                         "full-stack-engineer-intern-python-golang-react-aws")
        self.assertIsNone(job_from_application_href("/jobs/applications/archive"))
        self.assertIsNone(job_from_application_href("/jobs/4829680-web-intern"))

        now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            parse_application_date("Pending YESTERDAY", now=now),
            now - timedelta(days=1),
        )
        self.assertEqual(
            parse_application_date("Not Accepted OCT 7", now=now),
            datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parse_application_date("3 days ago", now=now),
            now - timedelta(days=3),
        )
        self.assertEqual(
            parse_application_date("2 weeks ago", now=now),
            now - timedelta(days=14),
        )
        self.assertIsNone(parse_application_date("no date here", now=now))

    def test_batch_questions_split_long_answers(self) -> None:
        from wfbot.wellfound import Wellfound

        class FakeAsk:
            def __init__(self, replies: list[str]) -> None:
                self.replies = list(replies)
                self.prompts: list[str] = []

            async def ask(self, text: str, *, timeout=None):  # noqa: ANN001
                self.prompts.append(text)
                return self.replies.pop(0) if self.replies else None

        notify = FakeAsk(["1. https://linkedin.com/in/me\n2. 3.5\n3. No", "one\ntwo\nthree"])
        wf = Wellfound(  # type: ignore[arg-type]
            None, make_config(WF_ANSWER_BANK="false"), None, notify
        )
        job = Job(
            job_id="1", slug="x", url="https://wellfound.com/jobs/1-x",
            title="Full Stack Intern", company="Acme",
        )
        fields = [
            {"key": "0", "type": "text", "label": "LinkedIn URL"},
            {"key": "1", "type": "number", "label": "Years of experience"},
            {"key": "2", "type": "radio", "label": "Americas?", "options": ["Yes", "No"]},
            {"key": "3", "type": "textarea", "label": "Three bullets about you"},
        ]
        answers = asyncio.run(wf._ask_fields(job, fields))
        self.assertEqual(
            answers, ["https://linkedin.com/in/me", "3.5", "No", "one\ntwo\nthree"]
        )
        self.assertEqual(len(notify.prompts), 2)
        self.assertIn("LinkedIn URL", notify.prompts[0])
        self.assertIn("Options: Yes, No", notify.prompts[0])
        # The long free-text question is asked on its own, not in the batch.
        self.assertNotIn("Three bullets", notify.prompts[0])
        self.assertIn("Three bullets", notify.prompts[1])

        skip_notify = FakeAsk(["skip"])
        wf_skip = Wellfound(  # type: ignore[arg-type]
            None, make_config(WF_ANSWER_BANK="false"), None, skip_notify
        )
        self.assertEqual(
            asyncio.run(wf_skip._ask_fields(job, [fields[0], fields[1]])), "skip"
        )

    def test_location_mismatch_and_job_specific_questions(self) -> None:
        self.assertIsNotNone(
            LOCATION_MISMATCH_RE.search(
                "This job does not support the locations or timezones for "
                "remote work on your profile."
            )
        )
        self.assertIsNone(LOCATION_MISMATCH_RE.search("Remote only in India"))
        self.assertIsNotNone(JOB_SPECIFIC_RE.search("Why do you want to join us?"))
        self.assertIsNone(JOB_SPECIFIC_RE.search("GitHub/GitLab URL"))
        self.assertEqual(answer_key("LinkedIn URL*"), "linkedin url")
        self.assertEqual(answer_key("  GitHub/GitLab (or equivalent) URL* "), "github gitlab or equivalent url")

    def test_applied_state_regex(self) -> None:
        self.assertTrue(APPLIED_STATE_RE.match("Applied"))
        self.assertTrue(APPLIED_STATE_RE.match("\u2713 Applied"))
        self.assertFalse(APPLIED_STATE_RE.match("Applied AI solutions"))
        self.assertFalse(APPLIED_STATE_RE.match("Apply"))

    def test_parse_company_size(self) -> None:
        self.assertEqual(parse_company_size("About the company Acme 1-10 Employees Austin"),
                         ("1-10", 1, 10))
        self.assertEqual(parse_company_size("11\u201350 Employees"), ("11-50", 11, 50))
        self.assertEqual(parse_company_size("1001+ Employees"), ("1001+", 1001, 10**9))
        self.assertEqual(parse_company_size("25 Employees"), ("25", 25, 25))
        self.assertIsNone(parse_company_size("no head-count here"))

    def test_company_size_verdict_prefers_tiny_then_falls_back(self) -> None:
        self.assertIsNone(self.wf._size_verdict(("1-10", 1, 10), allow_fallback=False))
        deferred = self.wf._size_verdict(("11-50", 11, 50), allow_fallback=False)
        self.assertIsNotNone(deferred)
        self.assertEqual(deferred[0], Status.DEFERRED_SIZE)
        self.assertIsNone(self.wf._size_verdict(("11-50", 11, 50), allow_fallback=True))
        skipped = self.wf._size_verdict(("51-200", 51, 200), allow_fallback=True)
        self.assertIsNotNone(skipped)
        self.assertEqual(skipped[0], Status.SKIPPED_SIZE)

    def test_company_size_fallback_can_be_disabled(self) -> None:
        from wfbot.wellfound import Wellfound

        wf = Wellfound(None, make_config(WF_ALLOW_SIZE_FALLBACK="false"), None, None)  # type: ignore[arg-type]
        verdict = wf._size_verdict(("11-50", 11, 50), allow_fallback=False)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict[0], Status.SKIPPED_SIZE)

    def test_parse_location_reads_job_page_sections(self) -> None:
        body = (
            "Job LocationKolkata \u2022 BengaluruHires remotely inEverywhere"
            "Visa SponsorshipNot AvailableRelocationAllowedSkillsDeveloper"
        )
        location, hires = parse_location(body)
        self.assertIn("Kolkata", location)
        self.assertIn("Bengaluru", location)
        self.assertEqual(hires, "Everywhere")

    def test_parse_location_reads_remote_work_policy(self) -> None:
        body = (
            "Job LocationGurugramRemote Work PolicyOnsite or remote"
            "Hires remotely inIndiaVisa SponsorshipNot AvailableSkillsJavascript"
        )
        location, hires = parse_location(body)
        self.assertIn("Gurugram", location)
        self.assertIn("Onsite or remote", location)
        self.assertEqual(hires, "India")

    def test_remote_detection_honours_regions(self) -> None:
        # Everywhere/India count as remote, a US-only remote role does not.
        self.assertTrue(self.wf._is_remote("", "Everywhere"))
        self.assertTrue(self.wf._is_remote("", "India"))
        self.assertFalse(self.wf._is_remote("Remote only \u2022 United States", "United States"))
        # No region named: the word "remote" on the page is enough.
        self.assertTrue(self.wf._is_remote("Bengaluru \u2022 Remote Work PolicyOnsite or remote", ""))

    def test_location_verdict_allows_remote_and_gurugram_only(self) -> None:
        job = Job(job_id="1", slug="web-intern", url="https://wellfound.com/jobs/1", title="Web Developer Intern")
        job.location = "Gurugram"
        job.remote = False
        self.assertIsNone(self.wf._location_verdict(job))

        job.remote = True
        job.location = "Bangalore"
        self.assertIsNone(self.wf._location_verdict(job))

        job.remote = False
        job.location = "Bangalore"
        verdict = self.wf._location_verdict(job)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict[0], Status.SKIPPED_LOCATION)

        # Unknown location is not thrown away.
        job.location = ""
        self.assertIsNone(self.wf._location_verdict(job))

    def test_parse_posted_age_reads_live_wellfound_wording(self) -> None:
        # Formats captured from real Wellfound listing headers.
        self.assertEqual(parse_posted_age("X RECRUITER RECENTLY ACTIVE POSTED 2 HOURS AGO Save Apply"), 2 / 24)
        cases = {
            "POSTED 30 MINUTES AGO": 30 / 1440,
            "POSTED 1 WEEK AGO": 7.0,
            "POSTED 3 WEEKS AGO": 21.0,
            "POSTED 2 MONTHS AGO": 60.0,
            "POSTED 4 YEARS AGO": 1460.0,
            "POSTED 30+ DAYS AGO": 30.0,
            "Posted just now": 0.0,
            "POSTED TODAY": 0.0,
            "POSTED YESTERDAY": 1.0,
        }
        for text, expected in cases.items():
            self.assertAlmostEqual(parse_posted_age(text), expected, places=6, msg=text)
        # No stamp (or nothing usable) stays permissive.
        self.assertIsNone(parse_posted_age("Full Stack Developer Intern"))
        self.assertIsNone(parse_posted_age(""))

    def test_age_verdict_enforces_strict_24_hour_window(self) -> None:
        job = Job(job_id="1", slug="fs-intern", url="https://wellfound.com/jobs/1", title="Full Stack Developer Intern")
        # Unknown age is permissive during card extraction, but strict on the
        # detail page so the 24-hour promise is not based on a guess.
        job.posted_days = None
        self.assertIsNone(self.wf._age_verdict(job, require_known=False))
        unknown = self.wf._age_verdict(job)
        self.assertIsNotNone(unknown)
        self.assertEqual(unknown[0], Status.SKIPPED_AGE)
        self.assertIn("unknown", unknown[1])

        job.posted_days = 0.5
        self.assertIsNone(self.wf._age_verdict(job))

        # "1 day ago" is outside a strict 24-hour window: the real age could be
        # almost 48 hours.
        job.posted_days = 1.0
        verdict = self.wf._age_verdict(job)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict[0], Status.SKIPPED_AGE)

        job.posted_days = 0.99
        self.assertIsNone(self.wf._age_verdict(job))

    def test_format_age_is_short(self) -> None:
        self.assertEqual(format_age(0.02), "29 min")
        self.assertEqual(format_age(0.25), "6 h")
        self.assertEqual(format_age(3), "3 d")
        self.assertEqual(format_age(14), "14 d")
        self.assertEqual(format_age(60), "2 mo")

    def test_new_role_families(self) -> None:
        vibe = ROLE_LIBRARY["vibe"]
        self.assertTrue(vibe.matches("Vibe Coding Intern (AI-Assisted Web Development)"))
        self.assertFalse(vibe.matches("Vibe Coding Lead"))  # full-time is never a match

        mern = ROLE_LIBRARY["mern"]
        self.assertTrue(mern.matches("MERN Stack Developer Intern"))
        self.assertFalse(mern.matches("Lead AI-First MERN Fullstack Engineer"))

        mobile = ROLE_LIBRARY["mobile"]
        self.assertTrue(mobile.matches("Full Stack Mobile App Developer Intern"))
        self.assertFalse(mobile.matches("Senior Mobile Engineer"))

        # The broadened web role also catches AI-assisted intern titles.
        self.assertTrue(ROLE_LIBRARY["web"].matches("AI-Assisted Web Development Intern"))


class TelegramAskTest(unittest.IsolatedAsyncioTestCase):
    async def test_ask_returns_the_reply(self) -> None:
        tg = Telegram(make_config())
        sent: list[str] = []

        async def fake_send(text: str, preview: bool = False, keyboard=None) -> None:
            sent.append(text)

        async def no_typing() -> None:
            return None

        tg.send = fake_send  # type: ignore[assignment]
        tg._typing = no_typing  # type: ignore[assignment]
        task = asyncio.create_task(tg.ask("Question?", timeout=5))
        await asyncio.sleep(0.05)
        pending = tg._pending_question
        self.assertIsNotNone(pending)
        assert pending is not None
        pending.set_result("six months")
        self.assertEqual(await task, "six months")
        self.assertIsNone(tg._pending_question)
        self.assertEqual(sent[0], "Question?")

    async def test_ask_times_out_with_none(self) -> None:
        tg = Telegram(make_config())

        async def fake_send(text: str, preview: bool = False, keyboard=None) -> None:
            return None

        async def no_typing() -> None:
            return None

        tg.send = fake_send  # type: ignore[assignment]
        tg._typing = no_typing  # type: ignore[assignment]
        self.assertIsNone(await tg.ask("Question?", timeout=0.01))
        self.assertIsNone(tg._pending_question)

    async def test_skip_button_only_matches_current_question(self) -> None:
        tg = Telegram(make_config())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()
        tg._pending_question = future
        tg._pending_question_token = "2"

        async def no_answer(callback_id: str, text: str = "") -> None:
            return None

        tg._answer_callback = no_answer  # type: ignore[method-assign]
        message = {"chat": {"id": int(tg.cfg.telegram_chat_id)}, "message_id": 10}

        await tg._handle_callback(
            {"id": "1", "data": "q:skip:1", "message": message}
        )
        self.assertFalse(future.done())
        await tg._handle_callback(
            {"id": "2", "data": "q:skip:2", "message": message}
        )
        self.assertTrue(future.done())
        self.assertEqual(future.result(), "skip")

    async def test_panel_callback_routes_to_action(self) -> None:
        tg = Telegram(make_config())
        answers: list[tuple[str, str]] = []
        actions: list[tuple[str, int | None]] = []

        async def fake_answer(callback_id: str, text: str = "") -> None:
            answers.append((callback_id, text))

        async def fake_action(action: str, message_id: int | None = None) -> None:
            actions.append((action, message_id))

        tg._answer_callback = fake_answer  # type: ignore[method-assign]
        tg._panel_action = fake_action  # type: ignore[method-assign]
        message = {
            "chat": {"id": int(tg.cfg.telegram_chat_id)},
            "message_id": 42,
        }
        await tg._handle_callback(
            {"id": "cb-1", "data": "p:apps", "message": message}
        )
        self.assertEqual(answers, [("cb-1", "")])
        self.assertEqual(actions, [("apps", 42)])

        await tg._handle_callback(
            {"id": "cb-2", "data": "p:status", "message": {**message, "chat": {"id": 999}}}
        )
        self.assertEqual(answers, [("cb-1", ""), ("cb-2", "")])
        self.assertEqual(actions, [("apps", 42)])


class GuardsTest(unittest.TestCase):
    def test_backoff_grows_then_caps(self) -> None:
        cfg = make_config(WF_MAX_CONSECUTIVE_FAILURES=3, WF_BACKOFF_MAX_MULTIPLIER=6)
        self.assertEqual(guards.backoff_multiplier(cfg, 0), 1.0)
        self.assertEqual(guards.backoff_multiplier(cfg, 2), 1.0)
        self.assertEqual(guards.backoff_multiplier(cfg, 3), 1.6)
        self.assertEqual(guards.backoff_multiplier(cfg, 10), 6.0)

    def test_next_delay_respects_interval_and_jitter(self) -> None:
        cfg = make_config(
            WF_INTERVAL_MINUTES=60, WF_INTERVAL_JITTER_SECONDS=240, WF_MAX_CONSECUTIVE_FAILURES=99
        )
        for _ in range(50):
            delay = guards.next_delay(cfg, 0)
            self.assertGreaterEqual(delay, 3600)
            self.assertLessEqual(delay, 3840)

    def test_next_delay_continuous_mode_is_short(self) -> None:
        cfg = make_config(
            WF_INTERVAL_MINUTES=0,
            WF_CONTINUOUS_GAP_SECONDS=90,
            WF_MAX_CONSECUTIVE_FAILURES=99,
        )
        for _ in range(50):
            delay = guards.next_delay(cfg, 0)
            self.assertGreaterEqual(delay, 90)
            self.assertLessEqual(delay, 180)

    def test_continuous_mode_backs_off_when_broken(self) -> None:
        cfg = make_config(
            WF_INTERVAL_MINUTES=0,
            WF_CONTINUOUS_GAP_SECONDS=60,
            WF_MAX_CONSECUTIVE_FAILURES=3,
            WF_BACKOFF_MAX_MULTIPLIER=6,
        )
        self.assertGreaterEqual(guards.next_delay(cfg, 5), 60 * 1.6)

    def test_human_jitter_within_bounds(self) -> None:
        cfg = make_config(WF_MIN_DELAY_SECONDS=45, WF_MAX_DELAY_SECONDS=150)
        delays = [human.jitter(cfg) for _ in range(200)]
        self.assertGreaterEqual(min(delays), 45)
        self.assertLessEqual(max(delays), 150)

    def test_next_daily_run_is_strictly_in_the_future(self) -> None:
        cfg = make_config(WF_DAILY_RUN_AT="20:00")
        before = datetime(2026, 10, 20, 19, 15, tzinfo=timezone.utc)
        self.assertEqual(
            guards.next_daily_run(cfg, before),
            datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc),
        )
        at_slot = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(
            guards.next_daily_run(cfg, at_slot),
            datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc),
        )
        after = datetime(2026, 10, 20, 20, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(
            guards.next_daily_run(cfg, after),
            datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc),
        )

    def test_daily_schedule_can_be_disabled_for_interval_mode(self) -> None:
        cfg = make_config(WF_DAILY_RUN_AT="")
        self.assertIsNone(cfg.daily_run_clock)
        self.assertIsNone(guards.next_daily_run(cfg))


class RunnerTest(unittest.TestCase):
    """A fatal login error must stop the run, not message once per listing."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

        class Notifier:
            def __init__(self) -> None:
                self.state = RuntimeState()
                self.paused: list[str] = []

            async def run_started(self) -> None: ...
            async def outcome(self, status, job, detail=""): ...
            async def error(self, where, exc): ...
            async def send(self, text, *, preview=False): self.paused.append(text)
            async def paused_automatically(self, reason): self.paused.append(reason)

        self.notify = Notifier()

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_daily_cap_stops_applications(self) -> None:
        job = Job(
            job_id="cap", slug="cap", url="https://wellfound.com/jobs/cap",
            title="Backend Developer Intern", role_key="backend",
        )
        self.store.record(Outcome(job=job, status=Status.APPLIED))

        class FakeWellfound:
            calls = 0

            async def apply(self, job, role, *, allow_fallback=False):
                self.calls += 1
                return Outcome(job=job, status=Status.APPLIED)

        wf = FakeWellfound()
        runner = Runner(
            cfg=make_config(WF_ROLES="backend", WF_MAX_APPS_PER_DAY="1"),
            store=self.store,
            wf=wf,  # type: ignore[arg-type]
            notify=self.notify,
            trigger_run=asyncio.Event(),
        )
        summary = RunSummary()
        asyncio.run(runner._apply_candidates([job], summary))
        self.assertEqual(wf.calls, 0)
        self.assertIn("daily cap", summary.stopped_reason)

    def test_daily_catch_up_only_within_four_hours(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))

        class FixedDateTime(datetime):
            fixed = datetime(2026, 10, 20, 21, 0, tzinfo=ist)

            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return cls.fixed.astimezone(tz) if tz else cls.fixed

        class FakeWellfound:
            pass

        runner = Runner(
            cfg=make_config(WF_ROLES="backend", WF_DAILY_RUN_AT="20:00"),
            store=self.store,
            wf=FakeWellfound(),  # type: ignore[arg-type]
            notify=self.notify,
            trigger_run=asyncio.Event(),
        )
        with patch("wfbot.runner.datetime", FixedDateTime):
            runner._catch_up_missed_run()
            self.assertTrue(runner.trigger.is_set())

            runner.trigger.clear()
            self.store.set_meta("last_scheduled_run_date", "2026-10-20")
            runner._catch_up_missed_run()
            self.assertFalse(runner.trigger.is_set())

        FixedDateTime.fixed = datetime(2026, 10, 21, 1, 0, tzinfo=ist)
        with patch("wfbot.runner.datetime", FixedDateTime):
            runner._catch_up_missed_run()
            self.assertFalse(runner.trigger.is_set())

    def test_next_run_at_uses_the_daily_clock(self) -> None:
        class FakeWellfound:
            pass

        runner = Runner(
            cfg=make_config(WF_ROLES="backend", WF_DAILY_RUN_AT="20:00"),
            store=self.store,
            wf=FakeWellfound(),  # type: ignore[arg-type]
            notify=self.notify,
            trigger_run=asyncio.Event(),
        )
        now = datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc)
        self.assertEqual(
            runner._next_run_at(now),
            datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc),
        )

    def test_login_failure_breaks_application_loop(self) -> None:
        jobs = [
            Job(job_id="1", slug="a", url="https://wellfound.com/jobs/1-a",
                title="Backend Developer Intern", role_key="backend"),
            Job(job_id="2", slug="b", url="https://wellfound.com/jobs/2-b",
                title="Backend Developer Intern", role_key="backend"),
        ]

        class FakeWellfound:
            calls = 0

            async def search(self, role):
                return jobs

            async def search_locations(self):
                return []

            async def ensure_logged_in(self):
                return None

            async def apply(self, job, role, *, allow_fallback=False):
                self.calls += 1
                raise LoginRequired("session gone")

        wf = FakeWellfound()
        runner = Runner(
            cfg=make_config(WF_ROLES="backend"),
            store=self.store,
            wf=wf,  # type: ignore[arg-type]
            notify=self.notify,
            trigger_run=asyncio.Event(),
        )

        async def no_pause(*args, **kwargs):
            return None

        with patch("wfbot.runner.human.pause", no_pause):
            with self.assertLogs("wfbot.runner", level="ERROR"):
                summary = asyncio.run(runner.run_once())

        self.assertEqual(wf.calls, 1)
        self.assertTrue(self.store.paused)
        self.assertIn("login", summary.stopped_reason)
        self.assertEqual(len(self.notify.paused), 1)

    def test_candidates_are_ordered_by_newest_job_id(self) -> None:
        ids = ["4751410", "4824501", "3392132", "4617746"]
        jobs = [
            Job(job_id=i, slug=f"s{i}", url=f"https://wellfound.com/jobs/{i}-s",
                title="Full Stack Developer Intern", role_key="fullstack")
            for i in ids
        ]

        class FakeWellfound:
            async def search(self, role):
                return jobs

            async def search_locations(self):
                return []

        runner = Runner(
            cfg=make_config(WF_ROLES="fullstack"),
            store=self.store,
            wf=FakeWellfound(),  # type: ignore[arg-type]
            notify=self.notify,
            trigger_run=asyncio.Event(),
        )

        async def no_pause(*args, **kwargs):
            return None

        with patch("wfbot.runner.human.pause", no_pause):
            fresh = asyncio.run(runner._collect_candidates(RunSummary()))

        self.assertEqual(
            [job.job_id for job in fresh],
            ["4824501", "4751410", "4617746", "3392132"],
        )

    def test_signed_in_search_candidates_are_ordered_by_parsed_age(self) -> None:
        specs = [("1", 5.0), ("2", 0.1), ("3", None), ("4", 0.75)]
        jobs = [
            Job(
                job_id=job_id,
                slug=f"s{job_id}",
                url=f"https://wellfound.com/jobs/{job_id}-s",
                title="Full Stack Developer Intern",
                role_key="fullstack",
                posted_days=age,
            )
            for job_id, age in specs
        ]

        class FakeWellfound:
            def __init__(self) -> None:
                self.public_calls = 0

            async def search_internships(self):
                return jobs

            async def search(self, role):
                self.public_calls += 1
                return []

            async def search_locations(self):
                self.public_calls += 1
                return []

        wf = FakeWellfound()
        runner = Runner(
            cfg=make_config(WF_ROLES="fullstack", WF_USE_SEARCH_PAGE="true"),
            store=self.store,
            wf=wf,  # type: ignore[arg-type]
            notify=self.notify,
            trigger_run=asyncio.Event(),
        )

        async def no_pause(*args, **kwargs):
            return None

        with patch("wfbot.runner.human.pause", no_pause):
            fresh = asyncio.run(runner._collect_candidates(RunSummary()))

        self.assertEqual([job.job_id for job in fresh], ["2", "4", "1", "3"])
        self.assertEqual(wf.public_calls, 0)

    def test_signed_in_search_falls_back_to_public_pages_on_error(self) -> None:
        fallback = Job(
            job_id="8",
            slug="fallback",
            url="https://wellfound.com/jobs/8-fallback",
            title="Full Stack Developer Intern",
            role_key="fullstack",
            posted_days=0.2,
        )

        class FakeWellfound:
            async def search_internships(self):
                raise RuntimeError("search markup changed")

            async def search(self, role):
                return [fallback]

            async def search_locations(self):
                return []

        runner = Runner(
            cfg=make_config(WF_ROLES="fullstack", WF_USE_SEARCH_PAGE="true"),
            store=self.store,
            wf=FakeWellfound(),  # type: ignore[arg-type]
            notify=self.notify,
            trigger_run=asyncio.Event(),
        )

        async def no_pause(*args, **kwargs):
            return None

        with patch("wfbot.runner.human.pause", no_pause):
            with self.assertLogs("wfbot.runner", level="ERROR"):
                fresh = asyncio.run(runner._collect_candidates(RunSummary()))
        self.assertEqual([job.job_id for job in fresh], ["8"])

    def test_zero_caps_mean_unlimited(self) -> None:
        jobs = [
            Job(
                job_id=str(100 + i),
                slug=f"j{i}",
                url=f"https://wellfound.com/jobs/{100 + i}-j",
                title="Full Stack Developer Intern",
                role_key="fullstack",
            )
            for i in range(6)
        ]

        class FakeWellfound:
            def __init__(self) -> None:
                self.calls = 0

            async def apply(self, job, role, *, allow_fallback=False):
                self.calls += 1
                return Outcome(job=job, status=Status.APPLIED)

        wf = FakeWellfound()
        runner = Runner(
            cfg=make_config(
                WF_ROLES="fullstack",
                WF_MAX_APPS_PER_RUN="0",
                WF_MAX_APPS_PER_DAY="0",
                WF_MAX_SIZE_CHECKS_PER_RUN="0",
            ),
            store=self.store,
            wf=wf,  # type: ignore[arg-type]
            notify=self.notify,
            trigger_run=asyncio.Event(),
        )

        async def no_cooldown(*args, **kwargs):
            return None

        with patch.object(Runner, "_cool_down", no_cooldown):
            summary = RunSummary()
            asyncio.run(runner._apply_candidates(jobs, summary))

        self.assertEqual(summary.applied, 6)
        self.assertEqual(wf.calls, 6)

    def test_stale_skips_do_not_use_up_application_slots(self) -> None:
        jobs = [
            Job(job_id=str(500 - i), slug=f"j{i}", url=f"https://wellfound.com/jobs/{500 - i}-j",
                title="Full Stack Developer Intern", role_key="fullstack")
            for i in range(6)
        ]

        class FakeWellfound:
            def __init__(self) -> None:
                self.opened: list[str] = []

            async def apply(self, job, role, *, allow_fallback=False):
                self.opened.append(job.job_id)
                if len(self.opened) <= 3:  # three stale leftovers first
                    return Outcome(job=job, status=Status.SKIPPED_AGE, detail="posted 2 mo ago")
                return Outcome(job=job, status=Status.APPLIED, detail="confirmed")

        wf = FakeWellfound()
        runner = Runner(
            cfg=make_config(
                WF_ROLES="fullstack", WF_MAX_APPS_PER_RUN="2", WF_MAX_SIZE_CHECKS_PER_RUN="10"
            ),
            store=self.store,
            wf=wf,  # type: ignore[arg-type]
            notify=self.notify,
            trigger_run=asyncio.Event(),
        )

        async def no_cooldown(*args, **kwargs):
            return None

        with patch.object(Runner, "_cool_down", no_cooldown):
            summary = RunSummary()
            asyncio.run(runner._apply_candidates(jobs, summary))

        # 3 skips then 2 applications: the stale ones cost page opens, not slots.
        self.assertEqual(summary.applied, 2)
        self.assertEqual(len(wf.opened), 5)


class TelegramRenderTest(unittest.TestCase):
    """Commands must render without touching the network."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.tg = Telegram(make_config())
        self.tg.bind(store=self.store, trigger_run=asyncio.Event())
        self.sent: list[str] = []

        async def capture(text: str, *, preview: bool = False, keyboard=None) -> None:
            self.sent.append(text)

        async def no_typing() -> None:
            return None

        self.tg.send = capture  # type: ignore[method-assign] - no network in tests
        self.tg._typing = no_typing  # type: ignore[method-assign]
        for name in ("start", "help", "status", "last_applied", "errors", "sync", "responses", "answers", "forget", "roles", "pause", "resume", "run"):
            self.tg.command(name)(getattr(self.tg, f"cmd_{name}"))
        self.tg.command("applications")(self.tg.cmd_last_applied)
        self.job = Job(
            job_id="7", slug="web-intern", url="https://wellfound.com/jobs/7-web-intern",
            title="Web Developer Intern", company="<Evil & Co>", role_key="web",
        )

    def tearDown(self) -> None:
        asyncio.run(self.tg.close())
        self.store.close()
        self.tmp.cleanup()

    def test_status_when_empty(self) -> None:
        text = asyncio.run(self.tg.cmd_status(""))
        self.assertIn("Wellfound Bot", text)
        self.assertIn("Bot Status", text)
        self.assertIn("Idle", text)
        self.assertIn("Today's Activity", text)
        self.assertIn("Applied <b>0</b> today", text)
        self.assertIn("Application History", text)
        self.assertIn("No applications recorded yet.", text)
        self.assertIn("Last Application", text)
        self.assertIn("None yet.", text)

    def test_main_keyboard_and_panel(self) -> None:
        keyboard = self.tg.main_keyboard()
        labels = [button["text"] for row in keyboard["keyboard"] for button in row]
        self.assertEqual(
            labels,
            [BTN_STATUS, BTN_APPS, BTN_RUN, BTN_PAUSE_ACTION, BTN_SYNC, BTN_MORE],
        )
        self.assertTrue(keyboard["is_persistent"])
        self.assertTrue(keyboard["resize_keyboard"])

        self.store.set_paused(True)
        paused_labels = [
            button["text"]
            for row in self.tg.main_keyboard()["keyboard"]
            for button in row
        ]
        self.assertEqual(paused_labels[3], BTN_RESUME_ACTION)
        self.store.set_paused(False)

        text, inline = self.tg._panel_main()
        self.assertIn("Wellfound Bot", text)
        self.assertIn("Applied <b>0</b> today", text)
        # The persistent reply keyboard is the only place for the six main
        # actions; the Status panel must not duplicate them inline.
        self.assertIsNone(inline)

    def test_panel_apps_links_and_daily_summary(self) -> None:
        self.store.record(Outcome(job=self.job, status=Status.APPLIED))
        text, keyboard = self.tg._panel_apps()
        self.assertIn("Web Developer Intern", text)
        self.assertIn('href="https://wellfound.com/jobs/7-web-intern"', text)
        self.assertIn("Refresh", str(keyboard))

        status, inline = self.tg._panel_main()
        self.assertIn("Today's Activity", status)
        self.assertIn("Applied <b>1</b> today", status)
        self.assertIn("Application History", status)
        self.assertIn("Last Application", status)
        self.assertIn("Web Developer Intern", status)
        self.assertIsNone(inline)

        summary = self.tg._daily_summary()
        self.assertIn("Daily summary", summary)
        self.assertIn("Today <b>1</b>", summary)
        self.assertIn("Confirmed <b>1</b>", summary)

    def test_main_button_dispatch_is_tolerant_of_variation_selectors(self) -> None:
        calls: list[str] = []

        async def fake_action(action: str, message_id: int | None = None) -> None:
            calls.append(action)

        self.tg._panel_action = fake_action  # type: ignore[method-assign]
        self.assertTrue(asyncio.run(self.tg._handle_main_button(BTN_STATUS)))
        self.assertTrue(
            asyncio.run(self.tg._handle_main_button(BTN_STATUS.replace("\ufe0f", "")))
        )
        self.assertTrue(asyncio.run(self.tg._handle_main_button(BTN_PAUSE_ACTION)))
        self.assertTrue(asyncio.run(self.tg._handle_main_button(BTN_RESUME_ACTION)))
        self.assertFalse(asyncio.run(self.tg._handle_main_button("Status")))
        self.assertEqual(calls, ["status", "status", "toggle", "toggle"])

    def test_last_applied_escapes_html(self) -> None:
        self.store.record(Outcome(job=self.job, status=Status.APPLIED))
        text = asyncio.run(self.tg.cmd_last_applied("5"))
        self.assertIn("&lt;Evil &amp; Co&gt;", text)
        self.assertNotIn("<Evil", text)

    def test_unknown_command_reply(self) -> None:
        asyncio.run(self.tg._dispatch("/nonsense"))
        self.assertTrue(self.sent and "Unknown command" in self.sent[-1])

    def test_known_command_dispatch(self) -> None:
        asyncio.run(self.tg._dispatch("/status"))
        self.assertIn("Wellfound Bot", self.sent[-1])
        asyncio.run(self.tg._dispatch("/status@WellfoundBot"))
        self.assertIn("Wellfound Bot", self.sent[-1])

    def test_argument_parsing(self) -> None:
        self.store.record(Outcome(job=self.job, status=Status.APPLIED))
        asyncio.run(self.tg._dispatch("/last_applied 1"))
        self.assertIn("Web Developer Intern", self.sent[-1])

    def test_skips_are_quiet_but_applies_are_messaged(self) -> None:
        job = Job(
            job_id="9", slug="fs-intern", url="https://wellfound.com/jobs/9-fs-intern",
            title="Full Stack Developer Intern", company="Acme", role_key="fullstack",
            location="Remote in India", remote=True, company_size="1-10",
            job_type="Internship", stipend="\u20b925k",
        )
        asyncio.run(self.tg.outcome(Status.SKIPPED_SIZE, job, "company too big"))
        asyncio.run(self.tg.outcome(Status.ALREADY_APPLIED, job, "seen before"))
        self.assertEqual(self.sent, [])

        asyncio.run(self.tg.outcome(Status.APPLIED, job, "confirmed after reload"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Applied", self.sent[-1])
        self.assertIn("Acme", self.sent[-1])
        self.assertIn("1-10 employees", self.sent[-1])
        self.assertIn("Internship", self.sent[-1])
        self.assertIn("\u20b925k", self.sent[-1])

    def test_quiet_run_is_silent_and_busy_run_reports(self) -> None:
        quiet = RunSummary()
        asyncio.run(self.tg.run_finished(quiet, None))
        self.assertEqual(self.sent, [])

        busy = RunSummary()
        busy.add(Outcome(job=self.job, status=Status.APPLIED))
        busy.finished_at = utcnow()
        asyncio.run(self.tg.run_finished(busy, None))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Run finished", self.sent[-1])

    def test_info_command_shows_timings(self) -> None:
        text = asyncio.run(self.tg.cmd_info(""))
        self.assertIn("BOT INFO", text)
        self.assertIn("Last run", text)
        self.assertIn("Next run", text)
        self.assertIn("Schedule", text)

    def test_report_and_goal_commands(self) -> None:
        text = asyncio.run(self.tg.cmd_report(""))
        self.assertIn("WELLFOUND REPORT", text)
        self.assertIn("Today", text)
        self.assertNotIn("daily cap", text)
        self.assertIn("Goal", text)
        self.assertLess(len(text), 4096)

        goal = asyncio.run(self.tg.cmd_goal("50 2026-12-31"))
        self.assertIn("Goal set", goal)
        self.assertEqual(self.store.get_meta("goal_applied"), "50")
        shown = asyncio.run(self.tg.cmd_goal(""))
        self.assertIn("50", shown)
        cleared = asyncio.run(self.tg.cmd_goal("off"))
        self.assertIn("cleared", cleared)
        self.assertEqual(self.store.get_meta("goal_applied"), "0")

    def test_answers_and_forget_commands(self) -> None:
        self.assertIn("No saved answers", asyncio.run(self.tg.cmd_answers("")))
        self.store.save_answer("linkedin url", "LinkedIn URL", "https://linkedin.com/in/me")
        shown = asyncio.run(self.tg.cmd_answers(""))
        self.assertIn("LinkedIn URL", shown)
        self.assertIn("linkedin.com/in/me", shown)
        answer_id = self.store.list_answers()[0]["id"]
        self.assertIn("Forgot", asyncio.run(self.tg.cmd_forget(str(answer_id))))
        self.assertIn("No saved answers", asyncio.run(self.tg.cmd_answers("")))
        self.assertIn("Usage", asyncio.run(self.tg.cmd_forget("nope")))

    def test_help_and_errors_and_roles(self) -> None:
        start_text = asyncio.run(self.tg.cmd_start(""))
        self.assertIn("Choose an option below", start_text)
        help_text = asyncio.run(self.tg.cmd_help(""))
        self.assertIn("Pause / Resume", help_text)
        self.assertIn("Skip application", help_text)
        self.assertIn("No errors", asyncio.run(self.tg.cmd_errors("")))
        self.assertIn("Web Developer Intern", asyncio.run(self.tg.cmd_roles("")))

    def test_startup_notice_only_when_needed(self) -> None:
        asyncio.run(self.tg.startup_notice())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Wellfound Bot is online", self.sent[0])

        asyncio.run(self.tg.startup_notice())
        self.assertEqual(len(self.sent), 1)

        asyncio.run(self.tg.startup_notice(history_note="History: 1 imported"))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("History: 1 imported", self.sent[1])
        self.assertNotIn("is online", self.sent[1])

    def test_sync_and_response_commands(self) -> None:
        self.assertIn("not ready", asyncio.run(self.tg.cmd_sync("")))

        class FakeWF:
            async def sync_applications(self):
                return {"found": 4, "imported": 2, "updated": 1}

        self.tg._wf = FakeWF()
        sync = asyncio.run(self.tg.cmd_sync(""))
        self.assertIn("2 imported", sync)
        self.assertIn("1 updated", sync)

        self.assertIn("Nothing recorded yet", asyncio.run(self.tg.cmd_responses("")))
        manual = Job(
            job_id="55",
            slug="mern-intern",
            url="https://wellfound.com/jobs/55-mern-intern",
            title="MERN Intern",
            company="Manual Co",
        )
        self.store.sync_application(manual, response="pending")
        self.assertIn("Pending <b>1</b>", asyncio.run(self.tg.cmd_responses("")))
        self.assertIn("Responses", asyncio.run(self.tg.cmd_report("")))

    def test_pause_resume(self) -> None:
        asyncio.run(self.tg.cmd_pause(""))
        self.assertTrue(self.store.paused)
        asyncio.run(self.tg.cmd_resume(""))
        self.assertFalse(self.store.paused)

    def test_run_trigger(self) -> None:
        self.assertFalse(self.tg._trigger_run.is_set())
        asyncio.run(self.tg.cmd_run(""))
        self.assertTrue(self.tg._trigger_run.is_set())

    def test_run_refused_while_paused(self) -> None:
        self.store.set_paused(True)
        reply = asyncio.run(self.tg.cmd_run(""))
        self.assertIn("paused", reply.lower())
        self.assertFalse(self.tg._trigger_run.is_set())

    def test_times_use_12_hour_clock(self) -> None:
        self.assertEqual(
            self.tg._clock(datetime(2026, 10, 20, 20, 0)), "8:00 PM"
        )
        self.assertEqual(
            self.tg._clock(datetime(2026, 10, 21, 8, 5)), "8:05 AM"
        )
        self.assertEqual(self.tg._clock_text("20:00"), "8:00 PM")

        now = datetime.now().astimezone()
        next_run = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        self.tg.state.next_run_at = next_run
        text = asyncio.run(self.tg.cmd_status(""))
        self.assertIn("8:00 PM", text)
        self.assertNotIn("20:00", text)

        info = asyncio.run(self.tg.cmd_info(""))
        self.assertIn("8:00 PM local", info)

        today_at_2005 = now.replace(hour=20, minute=5, second=0, microsecond=0)
        self.assertEqual(self.tg._when(today_at_2005.isoformat()), "8:05 PM")

    def test_esc(self) -> None:
        self.assertEqual(esc("a<b>&c"), "a&lt;b&gt;&amp;c")


if __name__ == "__main__":
    unittest.main(verbosity=2)
