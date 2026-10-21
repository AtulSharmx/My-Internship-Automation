"""Synthetic-fixture tests for the startup discovery pipeline.

No network access is required.  HTML/JSON/CSV fixtures are local and clearly
synthetic; they are not presented as real scraped records.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
import unittest
import zipfile

import httpx
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from startup_agent.app import SecretFilter
from startup_agent.config import Config, ConfigError
from startup_agent.discovery import DiscoveryService
from startup_agent.export import EXPORT_COLUMNS
from startup_agent.models import Company, CompanyStatus, RunSummary, SizeStatus
from startup_agent.normalize import (
    canonical_wellfound_url,
    classify_size,
    evaluate_company,
    name_key,
    normalize_url,
    parse_employee_size,
    record_id_for,
    website_key,
)
from startup_agent.setup_wizard import write_env
from startup_agent.sources.datasets import DbpediaSource
from startup_agent.sources.files import FileImportError, FileImporter
from startup_agent.sources.web import ComplianceError, WebDiscoverySource
from startup_agent.sources.wellfound_browser import (
    EXTRACT_PROFILE_JS,
    EXTRACT_STARTUPS_JS,
    _clean_profile_name,
    _clean_website,
    _looks_like_url,
)
from startup_agent.store import Store
from startup_agent.telegram import Telegram, _fmt_hhmm, status_word


SPARQL_FIXTURE = {
    "head": {"vars": ["c", "name", "e", "site", "industry", "city", "description"]},
    "results": {
        "bindings": [
            {
                "c": {"type": "uri", "value": "http://dbpedia.org/resource/Acme_Robotics"},
                "name": {"type": "literal", "value": "Acme Robotics"},
                "e": {"type": "literal", "value": "6"},
                "site": {
                    "type": "literal",
                    "value": "https://acme-robotics.example/|acme-robotics.example",
                },
                "industry": {
                    "type": "uri",
                    "value": "http://dbpedia.org/resource/Robotics",
                },
                "city": {"type": "uri", "value": "http://dbpedia.org/resource/Berlin"},
                "description": {
                    "type": "literal",
                    "value": "small robotics startup",
                },
            },
            {
                "c": {"type": "uri", "value": "http://dbpedia.org/resource/Acme_Robotics"},
                "name": {"type": "literal", "value": "Acme Robotics"},
                "e": {"type": "literal", "value": "6"},
                "site": {"type": "literal", "value": "https://acme-robotics.example/"},
                "industry": {
                    "type": "uri",
                    "value": "http://dbpedia.org/resource/Artificial_intelligence",
                },
            },
            {
                "c": {"type": "uri", "value": "http://dbpedia.org/resource/Archive_Example"},
                "name": {"type": "literal", "value": "Archive Example"},
                "e": {"type": "literal", "value": "3"},
                "site": {
                    "type": "literal",
                    "value": "https://web.archive.org/web/2024/http://old.test",
                },
            },
        ]
    },
}


def _cfg(root: Path, **overrides) -> Config:
    values = {
        "db_path": root / "data" / "startups.db",
        "import_dir": root / "imports",
        "export_dir": root / "exports",
        "log_dir": root / "logs",
        "target_size_min": 1,
        "target_size_max": 10,
    }
    values.update(overrides)
    return Config(**values)


def _write_csv(path: Path, rows: list[dict[str, str]], headers: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = headers or list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


class NormalizationTests(unittest.TestCase):
    def test_size_parsing_and_classification(self) -> None:
        cases = {
            "1-10 employees": (SizeStatus.CONFIRMED, True),
            "1–10": (SizeStatus.CONFIRMED, True),
            "5": (SizeStatus.CONFIRMED, True),
            "11-50 employees": (SizeStatus.OUTSIDE, False),
            "1-20 employees": (SizeStatus.PARTIAL, False),
            "10+ employees": (SizeStatus.PARTIAL, False),
            "fewer than 10": (SizeStatus.CONFIRMED, True),
            "self employed": (SizeStatus.CONFIRMED, True),
            "Unknown": (SizeStatus.UNKNOWN, False),
            "": (SizeStatus.UNKNOWN, False),
        }
        for text, (status, match) in cases.items():
            with self.subTest(text=text):
                size = parse_employee_size(text)
                self.assertEqual(classify_size(size, 1, 10), status)
                company = Company(record_id="", name="Test Co", employee_count=text)
                evaluate_company(company, 1, 10)
                self.assertEqual(company.size_match, match)

    def test_year_range_is_not_a_size_in_page_text(self) -> None:
        size = parse_employee_size("Founded 2019-2020. Small team.", require_context=True)
        self.assertFalse(size.known)

    def test_url_normalization_and_website_key(self) -> None:
        self.assertEqual(
            normalize_url("HTTPS://WWW.Acme.TEST/?utm_source=x&ref=abc#team"),
            "https://acme.test",
        )
        self.assertEqual(normalize_url("https://acme.test/about/"), "https://acme.test/about")
        self.assertEqual(website_key("https://www.acme.test/about"), "acme.test")
        self.assertEqual(website_key("https://www.linkedin.com/company/acme"), "")
        self.assertEqual(website_key("https://wellfound.com/company/acme"), "")

    def test_wellfound_profile_canonicalization(self) -> None:
        self.assertEqual(
            canonical_wellfound_url("https://www.wellfound.com/company/acme/jobs?utm=1"),
            "https://wellfound.com/company/acme",
        )
        self.assertEqual(canonical_wellfound_url("https://example.com/company/acme"), "")

    def test_record_id_is_stable_by_profile(self) -> None:
        first = Company(
            record_id="",
            name="Acme",
            wellfound_url="https://wellfound.com/company/acme",
        )
        second = Company(
            record_id="",
            name="Acme Inc",
            wellfound_url="https://www.wellfound.com/company/acme/",
        )
        evaluate_company(first)
        evaluate_company(second)
        self.assertEqual(record_id_for(first), record_id_for(second))
        third = Company(
            record_id="", name="Other", wellfound_url="https://wellfound.com/company/other"
        )
        evaluate_company(third)
        self.assertNotEqual(record_id_for(first), record_id_for(third))

    def test_name_key_does_not_merge_by_name_only(self) -> None:
        self.assertEqual(name_key("Acme Technologies Pvt Ltd"), "acme")
        self.assertNotEqual(name_key("Acme Labs"), name_key("Beta Labs"))

    def test_twelve_hour_time_display(self) -> None:
        self.assertEqual(_fmt_hhmm("20:00"), "8:00 PM")
        self.assertEqual(_fmt_hhmm("00:05"), "12:05 AM")
        self.assertEqual(_fmt_hhmm(""), "manual only")


class ConfigTests(unittest.TestCase):
    def test_env_config(self) -> None:
        env = {
            "STARTUP_TARGET_SIZE": "1-10",
            "STARTUP_ENABLE_URL_DISCOVERY": "true",
            "STARTUP_ALLOWED_DOMAINS": "example.com,www.example.org",
            "STARTUP_DAILY_RUN_AT": "20:00",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = Config.from_env(require_telegram=False)
        self.assertEqual(cfg.target_size_label, "1-10")
        self.assertTrue(cfg.enable_url_discovery)
        self.assertIn("example.com", cfg.allowed_domains)
        self.assertIn("example.org", cfg.allowed_domains)
        self.assertEqual(cfg.daily_run_at, "20:00")

    def test_invalid_size_rejected(self) -> None:
        with patch.dict(os.environ, {"STARTUP_TARGET_SIZE": "10-1"}, clear=False):
            with self.assertRaises(ConfigError):
                Config.from_env(require_telegram=False)

    def test_enabling_url_discovery_requires_domains(self) -> None:
        with patch.dict(
            os.environ,
            {"STARTUP_ENABLE_URL_DISCOVERY": "true", "STARTUP_ALLOWED_DOMAINS": ""},
            clear=False,
        ):
            with self.assertRaises(ConfigError):
                Config.from_env(require_telegram=False)


class ImportAndStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = _cfg(self.root)
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)
        self.service = DiscoveryService(self.cfg, self.store)

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.store.close()
        self.temp.cleanup()

    def _standard_rows(self) -> list[dict[str, str]]:
        return [
            {
                "name": "Acme",
                "website": "https://www.acme.test/?utm_source=wellfound",
                "employee_count": "1-10 employees",
                "industry": "Fintech",
                "description": "Payments for small teams",
                "wellfound_url": "https://wellfound.com/company/acme",
                "job_urls": "https://acme.test/jobs/1",
                "location": "Delhi",
                "source_url": "https://wellfound.com/company/acme",
            },
            {
                "name": "Broad",
                "website": "https://broad.test",
                "employee_count": "1-20",
                "industry": "SaaS",
                "description": "Broad range",
                "wellfound_url": "",
                "job_urls": "",
                "location": "Mumbai",
                "source_url": "https://broad.test",
            },
            {
                "name": "Big",
                "website": "https://big.test",
                "employee_count": "11-50",
                "industry": "AI",
                "description": "Bigger",
                "wellfound_url": "",
                "job_urls": "",
                "location": "Bengaluru",
                "source_url": "https://big.test",
            },
            {
                "name": "Missing",
                "website": "https://missing.test",
                "employee_count": "",
                "industry": "Health",
                "description": "No size",
                "wellfound_url": "",
                "job_urls": "",
                "location": "Pune",
                "source_url": "https://missing.test",
            },
            {
                "name": "",
                "website": "https://noname.test",
                "employee_count": "1-10",
                "industry": "",
                "description": "",
                "wellfound_url": "",
                "job_urls": "",
                "location": "",
                "source_url": "",
            },
        ]

    async def test_import_filter_missing_fields_and_normalization(self) -> None:
        path = self.root / "standard.csv"
        _write_csv(path, self._standard_rows())
        summary = await self.service.import_file(path)
        self.assertEqual(summary.inserted, 4)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(self.store.counts()["total"], 4)

        companies = {company.name: company for company in self.store.list_companies(limit=10)}
        acme = companies["Acme"]
        self.assertEqual(acme.website, "https://acme.test")
        self.assertEqual(acme.website_key, "acme.test")
        self.assertEqual(acme.size_status, SizeStatus.CONFIRMED)
        self.assertTrue(acme.size_match)
        self.assertEqual(acme.status, CompanyStatus.VALIDATED)
        self.assertEqual(acme.wellfound_url, "https://wellfound.com/company/acme")
        self.assertEqual(acme.job_urls, ["https://acme.test/jobs/1"])

        broad = companies["Broad"]
        self.assertEqual(broad.size_status, SizeStatus.PARTIAL)
        self.assertFalse(broad.size_match)
        self.assertEqual(broad.status, CompanyStatus.NEEDS_REVIEW)

        big = companies["Big"]
        self.assertEqual(big.size_status, SizeStatus.OUTSIDE)
        self.assertFalse(big.size_match)

        missing = companies["Missing"]
        self.assertEqual(missing.size_status, SizeStatus.UNKNOWN)
        self.assertFalse(missing.size_match)
        self.assertEqual(missing.status, CompanyStatus.NEEDS_REVIEW)

        counts = self.store.counts()
        self.assertEqual(counts["confirmed"], 1)
        self.assertEqual(counts["unknown_size"], 1)
        self.assertEqual(counts["needs_review"], 2)

    async def test_repeated_import_is_idempotent(self) -> None:
        path = self.root / "standard.csv"
        _write_csv(path, self._standard_rows())
        first = await self.service.import_file(path)
        second = await self.service.import_file(path)
        self.assertEqual(first.inserted, 4)
        self.assertTrue(second.already_imported)
        self.assertEqual(self.store.counts()["total"], 4)

    async def test_failed_import_is_recorded_and_not_retried_unchanged(self) -> None:
        path = self.root / "no-name.csv"
        _write_csv(
            path,
            [
                {
                    "name": "",
                    "website": "https://no-name.test",
                    "employee_count": "1-10",
                }
            ],
        )
        first = await self.service.import_file(path)
        self.assertEqual(first.failed, 1)
        self.assertEqual(self.store.counts()["total"], 0)
        self.assertIsNotNone(self.store.last_import())
        second = await self.service.import_file(path)
        self.assertTrue(second.already_imported)

    async def test_prior_export_record_id_updates_after_edit(self) -> None:
        path = self.root / "original.csv"
        _write_csv(
            path,
            [
                {
                    "name": "Acme",
                    "website": "https://acme.test",
                    "employee_count": "1-10",
                    "location": "Delhi",
                    "source_url": "https://acme.test",
                }
            ],
        )
        first = await self.service.import_file(path)
        self.assertEqual(first.inserted, 1)
        original = self.store.list_companies(limit=1)[0]

        edited = self.root / "edited.csv"
        _write_csv(
            edited,
            [
                {
                    "record_id": original.record_id,
                    "name": "Acme Labs",
                    "website": "",
                    "employee_count": "2-5",
                    "location": "Delhi",
                    "source_url": "",
                }
            ],
        )
        second = await self.service.import_file(edited)
        self.assertEqual(second.updated, 1)
        self.assertEqual(second.failed, 0)
        companies = self.store.list_companies(limit=10)
        self.assertEqual(len(companies), 1)
        self.assertEqual(companies[0].record_id, original.record_id)
        self.assertEqual(companies[0].name, "Acme Labs")
        # A size change from one confirmed range to another is a conflict
        # and moves the record to "Needs review" with size_match=False.
        self.assertEqual(companies[0].size_status, SizeStatus.UNKNOWN)
        self.assertFalse(companies[0].size_match)
        self.assertEqual(companies[0].status, CompanyStatus.NEEDS_REVIEW)

    async def test_profile_upsert_merges_job_urls_and_keeps_original_id(self) -> None:
        first_path = self.root / "one.csv"
        _write_csv(
            first_path,
            [
                {
                    "name": "Acme",
                    "website": "https://acme.test",
                    "employee_count": "1-10",
                    "industry": "Fintech",
                    "description": "Old description",
                    "wellfound_url": "https://wellfound.com/company/acme",
                    "job_urls": "https://acme.test/jobs/1",
                    "location": "Delhi",
                    "source_url": "https://wellfound.com/company/acme",
                }
            ],
        )
        second_path = self.root / "two.csv"
        _write_csv(
            second_path,
            [
                {
                    "name": "Acme",
                    "website": "https://acme.test",
                    "employee_count": "1-10",
                    "industry": "Fintech",
                    "description": "New description",
                    "wellfound_url": "https://wellfound.com/company/acme",
                    "job_urls": "https://acme.test/jobs/2",
                    "location": "Delhi",
                    "source_url": "https://wellfound.com/company/acme",
                }
            ],
        )
        first = await self.service.import_file(first_path)
        second = await self.service.import_file(second_path)
        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.updated, 1)
        self.assertEqual(self.store.counts()["total"], 1)
        company = self.store.list_companies(limit=1)[0]
        self.assertEqual(company.description, "New description")
        self.assertEqual(
            company.job_urls, ["https://acme.test/jobs/1", "https://acme.test/jobs/2"]
        )

    async def test_website_is_secondary_dedup_key(self) -> None:
        first_path = self.root / "one.csv"
        _write_csv(
            first_path,
            [
                {
                    "name": "Acme",
                    "website": "https://acme.test",
                    "employee_count": "1-10",
                    "wellfound_url": "https://wellfound.com/company/acme",
                    "source_url": "https://wellfound.com/company/acme",
                    "location": "Delhi",
                }
            ],
        )
        second_path = self.root / "two.csv"
        _write_csv(
            second_path,
            [
                {
                    "name": "Acme",
                    "website": "https://acme.test",
                    "employee_count": "1-10",
                    "wellfound_url": "https://wellfound.com/company/acme2",
                    "source_url": "https://wellfound.com/company/acme2",
                    "location": "Delhi",
                }
            ],
        )
        await self.service.import_file(first_path)
        second = await self.service.import_file(second_path)
        self.assertEqual(second.updated, 1)
        self.assertEqual(self.store.counts()["total"], 1)
        company = self.store.list_companies(limit=1)[0]
        self.assertIn("conflict", company.notes.lower())

    async def test_same_name_different_locations_are_not_merged(self) -> None:
        path = self.root / "names.csv"
        _write_csv(
            path,
            [
                {
                    "name": "Acme",
                    "website": "",
                    "employee_count": "1-10",
                    "location": "Delhi",
                    "source_url": "",
                },
                {
                    "name": "Acme",
                    "website": "",
                    "employee_count": "1-10",
                    "location": "Mumbai",
                    "source_url": "",
                },
            ],
        )
        summary = await self.service.import_file(path)
        self.assertEqual(summary.inserted, 2)
        self.assertEqual(self.store.counts()["total"], 2)
        self.assertEqual(self.store.counts()["duplicates"], 0)

    async def test_same_name_and_location_without_strong_key_is_flagged(self) -> None:
        path = self.root / "names.csv"
        _write_csv(
            path,
            [
                {
                    "name": "Acme",
                    "website": "",
                    "employee_count": "1-10",
                    "location": "Delhi",
                    "source_url": "",
                },
                {
                    "name": "Acme",
                    "website": "",
                    "employee_count": "2-5",
                    "location": "Delhi",
                    "source_url": "",
                },
            ],
        )
        summary = await self.service.import_file(path)
        self.assertEqual(summary.duplicates, 1)
        companies = self.store.list_companies(limit=10)
        self.assertEqual(len(companies), 2)
        duplicate = next(item for item in companies if item.status == CompanyStatus.DUPLICATE)
        self.assertIn(duplicate.duplicate_of, {item.record_id for item in companies})
        self.assertEqual(self.store.counts()["confirmed"], 1)

    async def test_json_wrapper_and_jsonl(self) -> None:
        json_path = self.root / "companies.json"
        json_path.write_text(
            json.dumps(
                {
                    "companies": [
                        {
                            "name": "JsonCo",
                            "website": "https://json.test",
                            "employee_count": "1-10",
                            "source_url": "https://json.test",
                            "location": "Delhi",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        summary = await self.service.import_file(json_path)
        self.assertEqual(summary.inserted, 1)
        jsonl_path = self.root / "more.jsonl"
        jsonl_path.write_text(
            json.dumps(
                {
                    "name": "LineCo",
                    "website": "https://line.test",
                    "employee_count": "11-50",
                    "source_url": "https://line.test",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        summary = await self.service.import_file(jsonl_path)
        self.assertEqual(summary.inserted, 1)
        self.assertEqual(self.store.counts()["total"], 2)

    async def test_saved_html_jsonld(self) -> None:
        html = """<!doctype html><html><head>
        <title>Acme</title><meta name="description" content="Synthetic test company">
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Organization","name":"Acme",
         "url":"https://acme.test","description":"Synthetic test company",
         "industry":"Fintech","numberOfEmployees":{"minValue":1,"maxValue":10},
         "address":{"@type":"PostalAddress","addressLocality":"Delhi","addressCountry":"India"},
         "sameAs":["https://wellfound.com/company/acme"]}
        </script></head><body><h1>Acme</h1></body></html>"""
        path = self.root / "acme.html"
        path.write_text(html, encoding="utf-8")
        summary = await self.service.import_file(path)
        self.assertEqual(summary.inserted, 1)
        company = self.store.list_companies(limit=1)[0]
        self.assertEqual(company.name, "Acme")
        self.assertEqual(company.website, "https://acme.test")
        self.assertEqual(company.size_status, SizeStatus.CONFIRMED)
        self.assertEqual(company.location, "Delhi, India")
        self.assertEqual(company.wellfound_url, "https://wellfound.com/company/acme")
        self.assertEqual(company.industry, "Fintech")

    async def test_saved_wellfound_directory_html(self) -> None:
        html = """<!doctype html><html><body>
        <div class="card">
          <a href="https://wellfound.com/company/acme">Acme</a>
          <span>1-10 employees</span>
          <a href="https://acme.test">Website</a>
        </div>
        <div class="card">
          <a href="https://wellfound.com/company/beta">Beta</a>
          <span>11-50 employees</span>
          <a href="https://beta.test">Website</a>
        </div>
        </body></html>"""
        path = self.root / "saved-wellfound.html"
        path.write_text(html, encoding="utf-8")
        summary = await self.service.import_file(path)
        self.assertEqual(summary.inserted, 2)
        companies = {company.name: company for company in self.store.list_companies(limit=10)}
        self.assertEqual(companies["Acme"].size_status, SizeStatus.CONFIRMED)
        self.assertEqual(companies["Acme"].website, "https://acme.test")
        self.assertEqual(companies["Beta"].size_status, SizeStatus.OUTSIDE)
        # A saved Wellfound page is recorded as the Wellfound platform.
        self.assertEqual(companies["Acme"].platform, "Wellfound")
        self.assertEqual(companies["Acme"].wellfound_url, "https://wellfound.com/company/acme")

        # The sheet keeps only the 1-10 company and shows the platform.
        sheet = await self.service.export_sheet("csv", platform="wellfound")
        with sheet.path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Startup Name"], "Acme")
        self.assertEqual(rows[0]["Platform"], "Wellfound")
        self.assertEqual(rows[0]["Members"], "1-10 employees")

    async def test_wellfound_login_page_is_rejected(self) -> None:
        path = self.root / "challenge.html"
        path.write_text(
            "<html><body><h1>Just a moment…</h1><p>Verify you are human</p></body></html>",
            encoding="utf-8",
        )
        summary = await self.service.import_file(path)
        self.assertEqual(summary.inserted, 0)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(self.store.counts()["total"], 0)
        self.assertTrue(summary.errors)

    async def test_xlsx_export_and_reimport(self) -> None:
        path = self.root / "standard.csv"
        _write_csv(path, self._standard_rows()[:2])
        await self.service.import_file(path)
        result = await self.service.export("xlsx")
        self.assertTrue(result.path.exists())
        with zipfile.ZipFile(result.path) as archive:
            self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
            sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("Acme", sheet)
        self.assertIn("Broad", sheet)

        another_root = self.root / "roundtrip"
        cfg = _cfg(another_root)
        cfg.ensure_dirs()
        store = Store(cfg.db_path)
        service = DiscoveryService(cfg, store)
        try:
            summary = await service.import_file(result.path)
            self.assertEqual(summary.inserted, 2)
            self.assertEqual(store.counts()["total"], 2)
        finally:
            await service.close()
            store.close()

    async def test_exports_are_timestamped_and_escape_text(self) -> None:
        path = self.root / "escape.csv"
        _write_csv(
            path,
            [
                {
                    "name": "Acme",
                    "website": "https://acme.test",
                    "employee_count": "1-10",
                    "industry": "Fintech",
                    "description": 'Payments, for "small" teams\nand more',
                    "source_url": "https://acme.test",
                    "location": "Delhi",
                }
            ],
        )
        await self.service.import_file(path)
        first = await self.service.export("csv")
        second = await self.service.export("csv")
        self.assertNotEqual(first.path, second.path)
        with first.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            row = next(reader)
        self.assertEqual(row["name"], "Acme")
        self.assertEqual(row["description"], 'Payments, for "small" teams and more')
        self.assertEqual(tuple(row.keys()), EXPORT_COLUMNS)

        json_result = await self.service.export("json")
        payload = json.loads(json_result.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["companies"][0]["name"], "Acme")
        self.assertTrue(payload["companies"][0]["size_match"])

    async def test_json_export_can_be_imported(self) -> None:
        path = self.root / "standard.csv"
        _write_csv(path, self._standard_rows()[:2])
        await self.service.import_file(path)
        exported = await self.service.export("json")
        another_root = self.root / "json-roundtrip"
        cfg = _cfg(another_root)
        cfg.ensure_dirs()
        store = Store(cfg.db_path)
        service = DiscoveryService(cfg, store)
        try:
            summary = await service.import_file(exported.path)
            self.assertEqual(summary.inserted, 2)
            self.assertEqual(store.counts()["total"], 2)
        finally:
            await service.close()
            store.close()

    async def test_queue_resumes_and_respects_attempt_limit(self) -> None:
        url = "https://example.com/startup"
        self.assertTrue(self.store.enqueue_url(url))
        self.assertEqual(len(self.store.pending_urls(10, max_attempts=2)), 1)
        row = self.store.pending_urls(10, max_attempts=2)[0]
        self.store.mark_queue(int(row["id"]), "failed", "transient", increment=True)
        self.assertEqual(len(self.store.pending_urls(10, max_attempts=2)), 1)
        row = self.store.pending_urls(10, max_attempts=2)[0]
        self.store.mark_queue(int(row["id"]), "failed", "transient", increment=True)
        self.assertEqual(len(self.store.pending_urls(10, max_attempts=2)), 0)
        self.assertEqual(self.store.queue_counts()["failed"], 1)

        blocked = "https://example.com/blocked"
        self.assertTrue(self.store.enqueue_url(blocked))
        blocked_row = self.store.queue_status(blocked)
        self.store.mark_queue(int(blocked_row["id"]), "blocked", "robots.txt", increment=True)
        self.assertFalse(self.store.enqueue_url(blocked, reset_done=True))
        self.assertEqual(self.store.queue_status(blocked)["status"], "blocked")

        done = "https://example.com/done"
        self.assertTrue(self.store.enqueue_url(done))
        done_row = self.store.queue_status(done)
        self.store.mark_queue(int(done_row["id"]), "done")
        self.assertTrue(self.store.enqueue_url(done, reset_done=True))
        self.assertEqual(self.store.queue_status(done)["status"], "pending")

    async def test_pause_and_meta(self) -> None:
        self.assertFalse(self.store.paused())
        self.store.set_paused(True)
        self.assertTrue(self.store.paused())
        self.store.set_meta("telegram_offset", "42")
        self.assertEqual(self.store.get_meta("telegram_offset"), "42")


class WebPolicyTests(unittest.TestCase):
    def test_wellfound_and_disabled_domains_are_blocked(self) -> None:
        cfg = _cfg(Path(tempfile.mkdtemp()))
        web = WebDiscoverySource(cfg)
        allowed, _, reason = web.check_url("https://wellfound.com/company/acme")
        self.assertFalse(allowed)
        self.assertIn("terms", reason.lower())
        allowed, _, reason = web.check_url("https://angel.co/company/acme")
        self.assertFalse(allowed)
        self.assertIn("terms", reason.lower())
        allowed, _, _ = web.check_url("https://example.com/startups")
        self.assertFalse(allowed)

    def test_allowlisted_domain_and_subdomain(self) -> None:
        cfg = _cfg(
            Path(tempfile.mkdtemp()),
            enable_url_discovery=True,
            allowed_domains=("example.com",),
        )
        web = WebDiscoverySource(cfg)
        allowed, url, reason = web.check_url("https://www.example.com/startups")
        self.assertTrue(allowed, reason)
        self.assertEqual(url, "https://example.com/startups")
        allowed, _, _ = web.check_url("https://sub.example.com/page")
        self.assertTrue(allowed)
        allowed, _, _ = web.check_url("https://other.test/page")
        self.assertFalse(allowed)
        allowed, _, reason = web.check_url("https://wellfound.com/company/acme")
        self.assertFalse(allowed)
        self.assertIn("terms", reason.lower())


class WebRedirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_to_non_allowlisted_host_is_not_fetched(self) -> None:
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx.Response(
                    200,
                    text="User-agent: *\nAllow: /\n",
                    headers={"content-type": "text/plain"},
                )
            if request.url.path == "/start":
                return httpx.Response(
                    302,
                    headers={"location": "https://not-allowed.test/secret"},
                )
            return httpx.Response(500)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cfg = _cfg(
            Path(tempfile.mkdtemp()),
            enable_url_discovery=True,
            allowed_domains=("allowed.test",),
            min_request_interval_seconds=0.0,
        )
        web = WebDiscoverySource(cfg, client=client)
        try:
            with self.assertRaises(ComplianceError):
                await web.fetch_companies("https://allowed.test/start")
            self.assertFalse(any("not-allowed.test" in url for url in requests))
        finally:
            await web.close()
            await client.aclose()

    async def test_redirect_to_wellfound_is_blocked_before_fetch(self) -> None:
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            return httpx.Response(
                301, headers={"location": "https://wellfound.com/company/acme"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cfg = _cfg(
            Path(tempfile.mkdtemp()),
            enable_url_discovery=True,
            allowed_domains=("allowed.test",),
            min_request_interval_seconds=0.0,
        )
        web = WebDiscoverySource(cfg, client=client)
        try:
            with self.assertRaises(ComplianceError):
                await web.fetch_companies("https://allowed.test/start")
            self.assertFalse(any("wellfound.com" in url for url in requests))
        finally:
            await web.close()
            await client.aclose()


class DatasetSourceTests(unittest.IsolatedAsyncioTestCase):
    """The DBpedia adapter is tested offline with a local fixture."""

    async def test_dbpedia_rows_become_companies_with_real_headcounts(self) -> None:
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx.Response(
                    200, text="User-agent: *\nAllow: /\n", headers={"content-type": "text/plain"}
                )
            if request.url.path == "/sparql":
                return httpx.Response(
                    200,
                    json=SPARQL_FIXTURE,
                    headers={"content-type": "application/sparql-results+json"},
                )
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cfg = _cfg(
            Path(tempfile.mkdtemp()),
            enable_datasets=True,
            allowed_domains=("dbpedia.org",),
            min_request_interval_seconds=0.0,
        )
        source = DbpediaSource(cfg, client=client)
        try:
            companies = await source.fetch(50)
        finally:
            await source.close()
            await client.aclose()

        # Two rows for the same company collapse into one record.
        self.assertEqual(len(companies), 2)
        acme = next(item for item in companies if item.name == "Acme Robotics")
        self.assertEqual(acme.employee_count, "6 employees")
        self.assertEqual(acme.website, "https://acme-robotics.example")
        self.assertEqual(acme.industry, "Robotics")
        self.assertEqual(acme.location, "Berlin")
        self.assertEqual(acme.description, "small robotics startup")
        self.assertEqual(acme.source_name, "dbpedia")
        self.assertIn("dbpedia.org/page/Acme_Robotics", acme.source_url)
        # Archive URLs are not usable company websites.
        archive = next(item for item in companies if item.name == "Archive Example")
        self.assertEqual(archive.website, "")
        # The query is bounded by the configured target range.
        queries = [
            parse_qs(urlsplit(url).query).get("query", [""])[0] for url in requested
        ]
        self.assertTrue(
            any("FILTER(?e >= 1 && ?e <= 10)" in query for query in queries),
            "the dataset query must be limited to the configured target range",
        )

    async def test_dbpedia_respects_robots_and_allowlist(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /sparql\n")
            return httpx.Response(200, json=SPARQL_FIXTURE)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cfg = _cfg(
            Path(tempfile.mkdtemp()),
            enable_datasets=True,
            allowed_domains=("dbpedia.org",),
            min_request_interval_seconds=0.0,
        )
        source = DbpediaSource(cfg, client=client)
        try:
            with self.assertRaises(ComplianceError):
                await source.fetch(10)
        finally:
            await source.close()
            await client.aclose()

        # A host that is not allow-listed is refused before any request.
        client2 = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cfg2 = _cfg(
            Path(tempfile.mkdtemp()),
            enable_datasets=True,
            allowed_domains=("example.org",),
            min_request_interval_seconds=0.0,
        )
        source2 = DbpediaSource(cfg2, client=client2)
        try:
            with self.assertRaises(ComplianceError):
                await source2.fetch(10)
        finally:
            await source2.close()
            await client2.aclose()

    async def test_dataset_discovery_stores_records(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            return httpx.Response(200, json=SPARQL_FIXTURE)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        root = Path(tempfile.mkdtemp())
        cfg = _cfg(
            root,
            enable_datasets=True,
            allowed_domains=("dbpedia.org",),
            min_request_interval_seconds=0.0,
        )
        cfg.ensure_dirs()
        store = Store(cfg.db_path)
        source = DbpediaSource(cfg, client=client)
        service = DiscoveryService(cfg, store, datasets=[source])
        try:
            summary = await service.discover_datasets()
            self.assertEqual(summary.inserted, 2)
            self.assertEqual(summary.failed, 0)
            self.assertEqual(store.counts()["confirmed"], 2)
            # Running it again must not create duplicate records.
            second = await service.discover_datasets()
            self.assertEqual(second.inserted, 0)
            self.assertEqual(store.counts()["total"], 2)
        finally:
            await service.close()
            store.close()
            await client.aclose()


class SheetExportTests(unittest.IsolatedAsyncioTestCase):
    """The compact sheet: Startup Name, Website, Platform, Members."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = _cfg(self.root)
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)
        self.service = DiscoveryService(self.cfg, self.store)

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.store.close()
        self.temp.cleanup()

    async def test_sheet_has_four_columns_and_only_target_sizes(self) -> None:
        path = self.root / "companies.csv"
        _write_csv(
            path,
            [
                {
                    "name": "Tiny Studio",
                    "website": "https://tiny.test",
                    "employee_count": "1-10",
                    "wellfound_url": "https://wellfound.com/company/tiny-studio",
                    "source_name": "wellfound_saved",
                    "source_url": "https://wellfound.com/company/tiny-studio",
                    "location": "Pune",
                },
                {
                    "name": "Big Corp",
                    "website": "https://big.test",
                    "employee_count": "11-50",
                    "source_name": "wellfound_saved",
                    "source_url": "https://wellfound.com/company/big-corp",
                },
                {
                    "name": "Unknown Size",
                    "website": "https://unknown.test",
                    "employee_count": "",
                    "source_name": "file:manual",
                    "source_url": "https://unknown.test",
                },
            ],
        )
        await self.service.import_file(path)
        result = await self.service.export_sheet("csv", platform="wellfound")
        self.assertEqual(result.row_count, 1)
        with result.path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(
            list(rows[0].keys()), ["Startup Name", "Website", "Platform", "Members"]
        )
        self.assertEqual(rows[0]["Startup Name"], "Tiny Studio")
        self.assertEqual(rows[0]["Website"], "https://tiny.test")
        self.assertEqual(rows[0]["Platform"], "Wellfound")
        self.assertEqual(rows[0]["Members"], "1-10")

        # 11-50 and unknown sizes must never reach the sheet.
        every = await self.service.export_sheet("csv")
        self.assertEqual(every.row_count, 1)

    async def test_sheet_xlsx_has_four_columns(self) -> None:
        path = self.root / "companies.csv"
        _write_csv(
            path,
            [
                {
                    "name": "Tiny Studio",
                    "website": "https://tiny.test",
                    "employee_count": "4 employees",
                    "source_name": "dbpedia",
                    "source_url": "https://dbpedia.org/page/Tiny_Studio",
                }
            ],
        )
        await self.service.import_file(path)
        result = await self.service.export_sheet("xlsx")
        self.assertEqual(result.row_count, 1)
        with zipfile.ZipFile(result.path) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("Startup Name", sheet)
        self.assertIn("Members", sheet)
        self.assertNotIn("record_id", sheet)

    def test_platform_names(self) -> None:
        self.assertEqual(
            Company(record_id="x", name="A", source_name="wellfound_saved").platform,
            "Wellfound",
        )
        self.assertEqual(
            Company(record_id="x", name="A", source_name="dbpedia").platform, "DBpedia"
        )
        self.assertEqual(
            Company(record_id="x", name="A", source_name="file:companies").platform,
            "Manual import",
        )
        self.assertEqual(Company(record_id="x", name="A").platform, "Unknown")


class WellfoundBrowserTests(unittest.IsolatedAsyncioTestCase):
    """The browser collector's extraction logic, tested against local fixtures."""

    def test_clean_website_accepts_bare_hosts_and_rejects_directories(self) -> None:
        self.assertEqual(
            _clean_website("www.getcerta.com/"), "https://getcerta.com"
        )
        self.assertEqual(
            _clean_website("https://acme.test/jobs"), "https://acme.test/jobs"
        )
        self.assertEqual(_clean_website(""), "")
        self.assertEqual(_clean_website("linkedin.com"), "")

    def test_profile_names_are_cleaned(self) -> None:
        self.assertEqual(
            _clean_profile_name("Langslide Careers - Insights and Opportunities"),
            "Langslide",
        )
        self.assertEqual(_clean_profile_name("Certa"), "Certa")
        self.assertTrue(_looks_like_url("https://nvisionglobaladvisory.com/"))
        self.assertFalse(_looks_like_url("PMNGLOBAL.INFO"))

    async def test_extraction_scripts_read_cards_and_profiles(self) -> None:
        listing = """
        <html><body>
          <div data-test="StartupResult">
            <h2>Acme Robotics</h2>
            <span>1-10 Employees</span>
            <a href="/company/acme-robotics">Acme Robotics</a>
          </div>
          <div data-test="StartupResult">
            <h2>Big Corp</h2>
            <span>51-200 Employees</span>
            <a href="/company/big-corp">Big Corp</a>
          </div>
        </body></html>
        """
        profile = """
        <html><body>
          <h1>Acme Robotics</h1>
          <h3>Procurement platform</h3>
          <p>51-200 Employees</p>
          <dl><dd>Website</dd><dt><div><ul>
            <button class="styles_websiteLink___Rnfc">www.acme.test/</button>
          </ul></div></dt></dl>
        </body></html>
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:  # pragma: no cover - depends on the machine
            self.skipTest("playwright is not installed")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(listing)
            cards = await page.evaluate(EXTRACT_STARTUPS_JS)
            self.assertEqual(len(cards), 2)
            self.assertEqual(cards[0]["name"], "Acme Robotics")
            self.assertEqual(cards[0]["size"], "1-10 Employees")
            self.assertEqual(cards[0]["href"], "/company/acme-robotics")
            self.assertEqual(cards[1]["size"], "51-200 Employees")

            await page.set_content(profile)
            data = await page.evaluate(EXTRACT_PROFILE_JS)
            self.assertIn("51-200", data["size"])
            self.assertEqual(data["website"], "www.acme.test/")
            await browser.close()

    async def test_service_stores_collected_companies(self) -> None:
        class FakeCollector:
            def __init__(self, cfg, *, progress=None) -> None:
                self.cfg = cfg

            async def start(self) -> None:
                return None

            async def close(self) -> None:
                return None

            async def collect(self, limit: int):
                return [
                    Company(
                        record_id="",
                        name="Acme Robotics",
                        website="https://acme.test",
                        employee_count="1-10 Employees",
                        wellfound_url="https://wellfound.com/company/acme-robotics",
                        source_url="https://wellfound.com/company/acme-robotics",
                        source_name="wellfound",
                        source_id="acme-robotics",
                    ),
                    Company(
                        record_id="",
                        name="Big Corp",
                        website="https://big.test",
                        employee_count="51-200 Employees",
                        wellfound_url="https://wellfound.com/company/big-corp",
                        source_url="https://wellfound.com/company/big-corp",
                        source_name="wellfound",
                        source_id="big-corp",
                    ),
                ]

        root = Path(tempfile.mkdtemp())
        cfg = _cfg(
            root,
            enable_wellfound_browser=True,
            wellfound_max_companies=10,
        )
        cfg.ensure_dirs()
        store = Store(cfg.db_path)
        service = DiscoveryService(cfg, store)
        with patch(
            "startup_agent.sources.wellfound_browser.WellfoundBrowserCollector",
            FakeCollector,
        ):
            summary = await service.collect_wellfound()
        try:
            self.assertEqual(summary.inserted, 2)
            self.assertEqual(summary.failed, 0)
            # Only the 1-10 company is a confirmed target match.
            self.assertEqual(store.counts()["confirmed"], 1)
            sheet = await service.export_sheet("csv", platform="wellfound")
            with sheet.path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Startup Name"], "Acme Robotics")
            self.assertEqual(rows[0]["Website"], "https://acme.test")
            self.assertEqual(rows[0]["Platform"], "Wellfound")
            self.assertEqual(rows[0]["Members"], "1-10 Employees")
        finally:
            await service.close()
            store.close()


class TelegramPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = _cfg(self.root, telegram_token="123456:TEST", telegram_chat_id="42")
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)
        self.service = DiscoveryService(self.cfg, self.store)
        self.bot = Telegram(self.cfg, self.service)

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        await self.service.close()
        self.store.close()
        self.temp.cleanup()

    async def test_main_keyboard_has_three_actions(self) -> None:
        keyboard = self.bot.main_keyboard()["keyboard"]
        labels = [button["text"] for row in keyboard for button in row]
        self.assertEqual(labels, ["🚀 Start Scan", "⏹ Stop", "📥 Download"])

    async def test_download_menu_has_two_options(self) -> None:
        keyboard = self.bot.download_keyboard()["inline_keyboard"]
        labels = [button["text"] for row in keyboard for button in row]
        self.assertEqual(
            labels,
            ["📄 Download this file", "🗂 Download all past"],
        )

    async def test_download_this_file_contains_all_data(self) -> None:
        path = self.root / "one.csv"
        _write_csv(
            path,
            [
                {
                    "name": "Acme",
                    "website": "https://acme.test",
                    "employee_count": "1-10",
                    "industry": "Fintech",
                    "location": "Delhi",
                    "source_url": "https://acme.test",
                },
                {
                    "name": "Beta",
                    "website": "",
                    "employee_count": "1-10",
                    "industry": "Robotics",
                    "location": "Pune",
                    "source_url": "https://wellfound.com/company/beta",
                },
            ],
        )
        await self.service.import_file(path)
        result = self.service.exporter.export_report("all", "xlsx")
        self.assertEqual(result.row_count, 2)
        self.assertTrue(result.path.exists())

    async def test_callback_routing_is_authorized_and_offline(self) -> None:
        edits: list[tuple[int, str]] = []
        answers: list[tuple[str, str]] = []
        sent: list[str] = []
        downloaded: list[str] = []

        async def fake_edit(message_id: int, text: str, keyboard=None) -> bool:
            edits.append((message_id, text))
            return True

        async def fake_send(text: str, keyboard=None, *, preview: bool = False) -> bool:
            sent.append(text)
            return True

        async def fake_answer(callback_id: str, text: str = "") -> None:
            answers.append((callback_id, text))

        async def fake_this() -> None:
            downloaded.append("this")

        async def fake_past() -> None:
            downloaded.append("past")

        self.bot.edit_message = fake_edit  # type: ignore[method-assign]
        self.bot.send = fake_send  # type: ignore[method-assign]
        self.bot.answer_callback = fake_answer  # type: ignore[method-assign]
        self.bot.send_this_file = fake_this  # type: ignore[method-assign]
        self.bot.send_all_past = fake_past  # type: ignore[method-assign]

        async def callback(data: str, chat_id: int = 42) -> None:
            await self.bot._handle_callback(
                {
                    "id": "cb1",
                    "data": data,
                    "message": {"message_id": 9, "chat": {"id": chat_id}},
                }
            )

        await callback("m:download")
        self.assertIn("Download", edits[-1][1])
        await callback("m:download:this")
        await callback("m:download:past")
        self.assertEqual(downloaded, ["this", "past"])
        await callback("m:reports")
        self.assertIn("Download", edits[-1][1])
        await callback("m:menu")
        self.assertEqual(sent[-1], "🤖 Wellfound Scanner")
        await callback("m:settings")
        self.assertEqual(sent[-1], "🤖 Wellfound Scanner")
        before = len(edits) + len(sent)
        await callback("m:menu", chat_id=999)
        self.assertEqual(len(edits) + len(sent), before)
        self.assertTrue(answers)

    async def test_authorization_and_result_formatting(self) -> None:
        self.assertTrue(self.bot._authorized({"id": 42}))
        self.assertFalse(self.bot._authorized({"id": 99}))
        summary = RunSummary(kind="url")
        summary.finish()
        self.assertEqual(self.bot._run_text(summary), "✅ Done")
        self.assertEqual(status_word("running"), "🏃 Running")
        self.assertEqual(status_word("completed"), "✅ Done")

    async def test_secret_filter(self) -> None:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="token=%s",
            args=("123456:TEST",),
            exc_info=None,
        )
        SecretFilter(["123456:TEST"]).filter(record)
        self.assertNotIn("123456:TEST", record.getMessage())
        self.assertIn("<redacted>", record.getMessage())


class SetupWizardTests(unittest.TestCase):
    def test_write_env_preserves_legacy_keys_and_comments(self) -> None:
        root = Path(tempfile.mkdtemp())
        env = root / ".env"
        env.write_text(
            "# keep this comment\nWF_EMAIL=legacy@example.test\nSTARTUP_TARGET_SIZE=1-5\n",
            encoding="utf-8",
        )
        write_env(
            {
                "STARTUP_TARGET_SIZE": "1-10",
                "TELEGRAM_CHAT_ID": "42",
                "TEST_QUOTED": "value # with hash",
            },
            path=env,
        )
        text = env.read_text(encoding="utf-8")
        self.assertIn("# keep this comment", text)
        self.assertIn("WF_EMAIL=legacy@example.test", text)
        self.assertIn("STARTUP_TARGET_SIZE=1-10", text)
        self.assertIn("TELEGRAM_CHAT_ID=42", text)
        self.assertIn('TEST_QUOTED="value # with hash"', text)


class ParserSafetyTests(unittest.TestCase):
    def test_file_importer_rejects_unsupported_and_missing_names(self) -> None:
        root = Path(tempfile.mkdtemp())
        importer = FileImporter()
        bad = root / "bad.txt"
        bad.write_text("hello", encoding="utf-8")
        with self.assertRaises(FileImportError):
            importer.parse(bad)
        no_name = root / "no-name.csv"
        _write_csv(no_name, [{"website": "https://x.test"}])
        batch = importer.parse(no_name)
        self.assertEqual(len(batch.companies), 0)
        self.assertEqual(len(batch.errors), 1)

    def test_html_parser_does_not_execute_scripts(self) -> None:
        from startup_agent.sources.extract import parse_page

        page = parse_page(
            "<html><head><script>window.alert('no')</script></head>"
            "<body><h1>Acme</h1><p>1-10 employees</p></body></html>",
            url="https://acme.test",
        )
        self.assertEqual(page.h1, "Acme")
        self.assertIn("1-10", page.text)
        self.assertNotIn("window.alert", page.text)


if __name__ == "__main__":
    unittest.main()
