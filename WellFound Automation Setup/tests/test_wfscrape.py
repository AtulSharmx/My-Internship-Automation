"""Tests for the public Wellfound scraper.

The suite is hermetic: no network, no browser, no live Wellfound.  The Apollo
fixtures below are trimmed copies of the real data island Wellfound ships.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from startup_agent.config import Config
from startup_agent.models import Company
from startup_agent.normalize import evaluate_company
from startup_agent.store import Store
from startup_agent.wfscrape.apollo import (
    StartupCard,
    filter_by_size,
    find_profile,
    parse_apollo_state,
    size_code_for,
    size_label,
    startup_cards,
    startup_profiles,
)
from startup_agent.wfscrape.enrich import clean_website, parse_size_text
from startup_agent.wfscrape.export import (
    SCRAPE_COLUMNS,
    SCRAPE_HEADERS,
    bare_domain,
    row_from_company,
    unique_scrape_path,
    write_scrape_csv,
)
from startup_agent.wfscrape.roles import (
    DEFAULT_ROLE_SLUGS,
    load_roles,
    parse_roles,
    role_page_url,
)
from startup_agent.wfscrape.runner import SOURCE_NAME, ScrapeRunner, export_stored


def _next_data(state: dict) -> str:
    """Wrap an Apollo state the way a Next.js page ships it."""
    payload = {"props": {"pageProps": {"apolloState": {"data": state}}}}
    return (
        "<html><head><script id=\"__NEXT_DATA__\" type=\"application/json\">"
        + json.dumps(payload)
        + "</script></head><body></body></html>"
    )


BADGE_B2B = {
    "__typename": "Badge",
    "id": "B2B-895963",
    "label": "B2B",
    "tooltip": None,
}
BADGE_STAGE = {
    "__typename": "Badge",
    "id": "COMPANY_STAGE-895963",
    "label": "Early Stage",
    "data": "early_stage",
}
TAG_ENTERPRISE = {
    "__typename": "NewTag",
    "id": "12",
    "name": "enterprise software",
    "displayName": "Enterprise Software",
}
TAG_MESSAGING = {
    "__typename": "NewTag",
    "id": "157",
    "name": "messaging",
    "displayName": "Messaging",
}

ROLE_STATE = {
    "StartupResult:895963": {
        "__typename": "StartupResult",
        "id": "895963",
        "name": "Mattermost",
        "slug": "mattermost",
        "companySize": "SIZE_51_200",
        "highConcept": "Self-hosted alternative to Slack",
        "badges": [{"__ref": "Badge:B2B-895963"}],
    },
    "StartupResult:10276427": {
        "__typename": "StartupResult",
        "id": "10276427",
        "name": "AIKOCorp",
        "slug": "aikocorp",
        "companySize": "SIZE_1_10",
        "highConcept": "Autonomous Hacking",
        "badges": [{"__ref": "Badge:COMPANY_STAGE-10276427"}],
    },
    "StartupResult:10286352": {
        "__typename": "StartupResult",
        "id": "10286352",
        "name": "Infisical",
        "slug": "infisical-3",
        "companySize": "SIZE_11_50",
        "highConcept": "Open source secrets management",
        "badges": [],
    },
    "Badge:B2B-895963": BADGE_B2B,
    "Badge:COMPANY_STAGE-10276427": BADGE_STAGE,
}

PROFILE_STATE = {
    "Startup:895963": {
        "__typename": "Startup",
        "id": "895963",
        "slug": "mattermost",
        "name": "Mattermost",
        "companySize": "SIZE_1_10",
        "companyUrl": "https://mattermost.com/",
        "highConcept": "Self-hosted alternative to Slack",
        "marketTaggings": [
            {"__ref": "NewTag:12"},
            {"__ref": "NewTag:157"},
        ],
        "locationTaggings": [{"__ref": "NewTag:1692"}],
        "badges": [{"__ref": "Badge:B2B-895963"}],
    },
    "NewTag:12": TAG_ENTERPRISE,
    "NewTag:157": TAG_MESSAGING,
    "NewTag:1692": {
        "__typename": "NewTag",
        "id": "1692",
        "name": "san francisco",
        "displayName": "San Francisco",
    },
    "Badge:B2B-895963": BADGE_B2B,
}


class ApolloParsingTests(unittest.TestCase):
    def test_parse_reads_the_next_data_island(self) -> None:
        state = parse_apollo_state(_next_data(ROLE_STATE))
        self.assertIn("StartupResult:895963", state)

    def test_parse_tolerates_a_page_without_the_island(self) -> None:
        self.assertEqual(parse_apollo_state("<html><body>nope</body></html>"), {})
        self.assertEqual(parse_apollo_state(""), {})

    def test_cards_expose_id_name_slug_and_size(self) -> None:
        cards = startup_cards(parse_apollo_state(_next_data(ROLE_STATE)))
        by_id = {card.company_id: card for card in cards}
        self.assertEqual(len(cards), 3)
        mattermost = by_id["895963"]
        self.assertEqual(mattermost.name, "Mattermost")
        self.assertEqual(mattermost.slug, "mattermost")
        self.assertEqual(mattermost.company_size, "SIZE_51_200")
        self.assertEqual(mattermost.size_label, "51-200")
        self.assertEqual(
            mattermost.profile_url, "https://wellfound.com/company/mattermost"
        )

    def test_cards_resolve_badge_labels(self) -> None:
        cards = startup_cards(parse_apollo_state(_next_data(ROLE_STATE)))
        by_id = {card.company_id: card for card in cards}
        self.assertEqual(by_id["895963"].badges, ["B2B"])
        self.assertEqual(by_id["10276427"].badges, ["Early Stage"])

    def test_size_filter_is_exact(self) -> None:
        cards = startup_cards(parse_apollo_state(_next_data(ROLE_STATE)))
        small = filter_by_size(cards, "SIZE_1_10")
        self.assertEqual([card.name for card in small], ["AIKOCorp"])

    def test_profile_carries_the_website_and_the_market_tags(self) -> None:
        state = parse_apollo_state(_next_data(PROFILE_STATE))
        profile = find_profile(state, "mattermost")
        assert profile is not None
        self.assertEqual(profile.company_url, "https://mattermost.com/")
        self.assertEqual(profile.market_tags, ["Enterprise Software", "Messaging"])
        self.assertEqual(profile.location_tags, ["San Francisco"])
        self.assertEqual(profile.badges, ["B2B"])
        self.assertEqual(profile.niche, "Enterprise Software, Messaging")

    def test_profile_is_found_by_slug(self) -> None:
        state = parse_apollo_state(_next_data(PROFILE_STATE))
        self.assertIsNotNone(find_profile(state, "mattermost"))
        self.assertIsNone(find_profile(state, "does-not-exist"))

    def test_profiles_skip_records_without_a_slug(self) -> None:
        state = dict(PROFILE_STATE)
        state["Startup:999"] = {"__typename": "Startup", "id": "999"}
        self.assertEqual(len(startup_profiles(state)), 1)

    def test_size_codes_round_trip(self) -> None:
        self.assertEqual(size_code_for("1-10"), "SIZE_1_10")
        self.assertEqual(size_label("SIZE_1_10"), "1-10")
        with self.assertRaises(ValueError):
            size_code_for("nonsense")


class WebsiteCleaningTests(unittest.TestCase):
    def test_a_bare_host_becomes_an_https_url(self) -> None:
        self.assertEqual(clean_website("aikocorp.ai"), "https://aikocorp.ai")

    def test_a_full_url_is_kept(self) -> None:
        self.assertEqual(
            clean_website("https://mattermost.com/"), "https://mattermost.com"
        )

    def test_a_social_profile_is_not_a_website(self) -> None:
        self.assertEqual(clean_website("https://www.linkedin.com/company/x"), "")

    def test_a_wellfound_link_is_not_a_website(self) -> None:
        self.assertEqual(clean_website("https://wellfound.com/company/x"), "")

    def test_nonsense_is_dropped(self) -> None:
        self.assertEqual(clean_website(""), "")
        self.assertEqual(clean_website("Visit website"), "")

    def test_visible_size_text_maps_to_the_enum(self) -> None:
        self.assertEqual(parse_size_text("1-10 Employees"), "SIZE_1_10")
        self.assertEqual(parse_size_text("11-50 Employees"), "SIZE_11_50")
        self.assertEqual(parse_size_text("no size here"), "")


class RoleListTests(unittest.TestCase):
    def test_comments_and_blank_lines_are_ignored(self) -> None:
        roles = parse_roles("# a comment\n\nsoftware-engineer\n  ai-engineer  \n")
        self.assertEqual(roles, ["software-engineer", "ai-engineer"])

    def test_duplicates_are_dropped(self) -> None:
        self.assertEqual(parse_roles("a-role\na-role\n"), ["a-role"])

    def test_a_missing_file_falls_back_to_the_defaults(self) -> None:
        roles = load_roles(Path(tempfile.gettempdir()) / "does-not-exist.txt")
        self.assertEqual(roles, list(DEFAULT_ROLE_SLUGS))

    def test_a_file_is_read_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "roles.txt"
            path.write_text("first-role\nsecond-role\n", encoding="utf-8")
            self.assertEqual(load_roles(path), ["first-role", "second-role"])

    def test_page_urls(self) -> None:
        self.assertEqual(
            role_page_url("software-engineer"),
            "https://wellfound.com/role/r/software-engineer",
        )
        self.assertEqual(
            role_page_url("software-engineer", 3),
            "https://wellfound.com/role/r/software-engineer?page=3",
        )


class CsvExportTests(unittest.TestCase):
    def test_the_csv_has_exactly_the_requested_columns(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.csv"
            rows = [
                {
                    "company_name": "Mattermost",
                    "website": "https://mattermost.com",
                    "domain": "mattermost.com",
                    "team_size": "1-10",
                    "niche": "Enterprise Software, Messaging",
                    "wellfound_profile_url": "https://wellfound.com/company/mattermost",
                    "date_scraped": "2026-10-20",
                    "wellfound_company_id": "895963",
                }
            ]
            written = write_scrape_csv(path, rows)
            self.assertEqual(written, 1)
            text = path.read_text(encoding="utf-8-sig")
            header = text.splitlines()[0]
            self.assertEqual(
                header,
                "company_name,website,domain,team_size,niche,"
                "wellfound_profile_url,date_scraped,wellfound_company_id",
            )
            self.assertIn("Mattermost", text)
            self.assertIn("https://mattermost.com", text)

    def test_the_column_contract_is_exactly_eight_and_in_order(self) -> None:
        self.assertEqual(
            SCRAPE_COLUMNS,
            (
                "company_name",
                "website",
                "domain",
                "team_size",
                "niche",
                "wellfound_profile_url",
                "date_scraped",
                "wellfound_company_id",
            ),
        )

    def test_no_pipeline_state_column_is_present(self) -> None:
        joined = " ".join(SCRAPE_COLUMNS).lower()
        for forbidden in ("status", "contacted", "stage", "lead", "verified"):
            self.assertNotIn(forbidden, joined)

    def test_a_missing_value_stays_blank(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.csv"
            write_scrape_csv(path, [{"company_name": "No site", "website": ""}])
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("No site,,", text)

    def test_the_path_never_overwrites_an_earlier_report(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first = unique_scrape_path(Path(folder), stamp="20260101-000000")
            first.write_text("x", encoding="utf-8")
            second = unique_scrape_path(Path(folder), stamp="20260101-000000")
            self.assertNotEqual(first, second)

    def test_the_headers_are_the_column_names(self) -> None:
        self.assertEqual(
            list(SCRAPE_HEADERS.values()),
            [
                "company_name",
                "website",
                "domain",
                "team_size",
                "niche",
                "wellfound_profile_url",
                "date_scraped",
                "wellfound_company_id",
            ],
        )


class BareDomainTests(unittest.TestCase):
    def test_scheme_www_and_path_are_stripped(self) -> None:
        self.assertEqual(
            bare_domain("https://www.Acme.Startup.com/jobs?x=1"), "acme.startup.com"
        )

    def test_a_bare_host_is_kept(self) -> None:
        self.assertEqual(bare_domain("acmestartup.com"), "acmestartup.com")

    def test_a_trailing_slash_does_not_matter(self) -> None:
        self.assertEqual(bare_domain("https://acmestartup.com/"), "acmestartup.com")

    def test_the_port_is_not_part_of_the_domain(self) -> None:
        self.assertEqual(bare_domain("https://acmestartup.com:8443/x"), "acmestartup.com")

    def test_nothing_in_nothing_out(self) -> None:
        self.assertEqual(bare_domain(""), "")
        self.assertEqual(bare_domain("   "), "")


class RowFromCompanyTests(unittest.TestCase):
    def _company(self) -> Company:
        company = Company(
            record_id="",
            name="Mattermost",
            website="https://www.mattermost.com/",
            employee_count="1-10",
            industry="Enterprise Software, Messaging",
            wellfound_url="https://wellfound.com/company/mattermost",
            source_name="wellfound_public",
            source_id="895963",
        )
        company.discovered_at = "2026-10-20T23:47:00+00:00"
        return company

    def test_the_row_has_the_whole_contract(self) -> None:
        row = row_from_company(self._company())
        self.assertEqual(tuple(row.keys()), SCRAPE_COLUMNS)

    def test_the_domain_is_derived_from_the_website(self) -> None:
        row = row_from_company(self._company())
        self.assertEqual(row["website"], "https://www.mattermost.com/")
        self.assertEqual(row["domain"], "mattermost.com")

    def test_the_date_is_a_plain_date(self) -> None:
        self.assertEqual(row_from_company(self._company())["date_scraped"], "2026-10-20")

    def test_the_company_id_is_the_reference_key(self) -> None:
        self.assertEqual(
            row_from_company(self._company())["wellfound_company_id"], "895963"
        )

    def test_the_company_id_falls_back_to_the_raw_payload(self) -> None:
        company = self._company()
        company.source_id = ""
        company.raw = {"wellfound_company_id": "12345"}
        self.assertEqual(row_from_company(company)["wellfound_company_id"], "12345")

    def test_the_niche_holds_the_published_tags(self) -> None:
        self.assertEqual(
            row_from_company(self._company())["niche"],
            "Enterprise Software, Messaging",
        )


class ReExportTests(unittest.TestCase):
    def test_the_snapshot_is_rebuilt_from_the_database(self) -> None:
        # Cleanup is LIFO, so the folder is registered first and closed last:
        # the database handle must be released before the folder is removed.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cfg = Config(
            db_path=root / "test.db",
            export_dir=root / "exports",
            import_dir=root / "imports",
            log_dir=root / "logs",
            target_size_min=1,
            target_size_max=10,
        )
        store = Store(cfg.db_path)
        self.addCleanup(store.close)
        company = Company(
            record_id="",
            name="Mattermost",
            website="https://mattermost.com",
            employee_count="1-10",
            industry="Enterprise Software",
            wellfound_url="https://wellfound.com/company/mattermost",
            source_url="https://wellfound.com/company/mattermost",
            source_name="wellfound_public",
            source_id="895963",
        )
        evaluate_company(company, 1, 10)
        store.upsert_company(company)

        rows, paths = export_stored(cfg, store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["domain"], "mattermost.com")
        self.assertEqual(rows[0]["wellfound_company_id"], "895963")
        self.assertEqual(set(paths), {"csv", "xlsx", "json"})
        for path in paths.values():
            self.assertTrue(path.exists())
        header = paths["csv"].read_text(encoding="utf-8-sig").splitlines()[0]
        self.assertEqual(
            header,
            "company_name,website,domain,team_size,niche,"
            "wellfound_profile_url,date_scraped,wellfound_company_id",
        )


class _FakeDiscovery:
    """A stand-in for phase 1 that returns fixed cards."""

    def __init__(self, cards: list[StartupCard]) -> None:
        self.cards = cards
        self.roles: list[str] = []

    async def discover(self, roles, **kwargs):
        from startup_agent.wfscrape.discover import DiscoveryResult

        self.roles = list(roles)
        result = DiscoveryResult(cards=list(self.cards))
        result.pages_fetched = 1
        result.cards_seen = len(self.cards)
        result.roles_walked = list(roles)
        return result

    async def close(self) -> None:
        return None


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=root / "test.db",
            export_dir=root / "exports",
            import_dir=root / "imports",
            log_dir=root / "logs",
            target_size_min=1,
            target_size_max=10,
        )
        self.store = Store(self.cfg.db_path)
        self.addCleanup(self.store.close)

    def test_a_known_company_is_not_fetched_again(self) -> None:
        card = StartupCard(
            company_id="895963",
            name="Mattermost",
            slug="mattermost",
            company_size="SIZE_1_10",
        )
        runner = ScrapeRunner(self.cfg, self.store, None, discovery=_FakeDiscovery([]))
        # Not stored yet -> nothing to reuse.
        self.assertIsNone(runner._known_with_website(card))

        company = Company(
            record_id="",
            name="Mattermost",
            website="https://mattermost.com",
            employee_count="1-10",
            wellfound_url="https://wellfound.com/company/mattermost",
            source_url="https://wellfound.com/company/mattermost",
            source_name=SOURCE_NAME,
            source_id="895963",
        )
        evaluate_company(company, 1, 10)
        self.store.upsert_company(company)

        known = runner._known_with_website(card)
        assert known is not None
        self.assertEqual(known.website, "https://mattermost.com")

    def test_records_are_stored_under_the_public_source(self) -> None:
        company = Company(
            record_id="",
            name="Mattermost",
            website="https://mattermost.com",
            employee_count="1-10",
            industry="Enterprise Software, Messaging",
            wellfound_url="https://wellfound.com/company/mattermost",
            source_url="https://wellfound.com/company/mattermost",
            source_name=SOURCE_NAME,
            source_id="895963",
        )
        evaluate_company(company, 1, 10)
        self.assertTrue(company.size_match)
        self.assertEqual(company.platform, "Wellfound")
        result = self.store.upsert_company(company)
        self.assertIn(result.action, {"inserted", "updated"})

    def test_the_same_company_is_not_stored_twice(self) -> None:
        def make() -> Company:
            company = Company(
                record_id="",
                name="Mattermost",
                website="https://mattermost.com",
                employee_count="1-10",
                wellfound_url="https://wellfound.com/company/mattermost",
                source_url="https://wellfound.com/company/mattermost",
                source_name=SOURCE_NAME,
                source_id="895963",
            )
            evaluate_company(company, 1, 10)
            return company

        self.store.upsert_company(make())
        self.store.upsert_company(make())
        self.assertEqual(self.store.counts()["total"], 1)


if __name__ == "__main__":
    unittest.main()
