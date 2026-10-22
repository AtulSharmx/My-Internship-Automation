from apollo_tool.email_check import MXResult
from apollo_tool.models import CompanyFacts, CompanyResult, Person
from apollo_tool.runner import (
    _guess_from_pattern,
    _rendered_email_counts,
    reenrich_result,
    select_candidate,
    title_tier,
)


class _GoodMX:
    def check(self, domain):
        return MXResult("ok", "1 MX record(s)", (f"mx.{domain}",))


def person(name, title, reason="email-cell-verified", masked=False, key=None):
    return Person(
        person_key=key or name,
        name_raw=name,
        full_name=name,
        masked=masked,
        title=title,
        reason=reason,
    )


def test_title_tiers():
    assert title_tier("Co-Founder") == 0
    assert title_tier("Founder & CEO") == 0
    assert title_tier("Chief Executive Officer") == 1
    assert title_tier("Executive Assistant to the CEO") == 4
    assert title_tier("CTO") == 2
    assert title_tier("Head of Talent") == 3
    assert title_tier("People Operations") == 3
    assert title_tier("Head of Security") == 4


def test_candidate_prefers_top_tier_and_verified():
    people = [
        person("Tess Talent", "Head of Talent"),
        person("Cora CTO", "CTO"),
        person("Cara CEO", "Chief Executive Officer", reason="email-cell-unverified"),
        person("Fiona Founder", "Founder", reason="email-cell-unverified"),
        person("Vera Verified", "Co-Founder"),
    ]
    chosen, note = select_candidate(people)
    assert chosen is not None
    assert chosen.full_name == "Vera Verified"
    assert note == ""


def test_candidate_skips_masked_and_unavailable_in_top_tier():
    people = [
        person("Mia M ------", "Co-Founder", masked=True),
        person("Una Unavailable", "Co-Founder", reason="email-cell-unavailable"),
        person("Cara CEO", "CEO"),
    ]
    chosen, note = select_candidate(people)
    assert chosen is not None
    assert chosen.full_name == "Cara CEO"
    assert "tier 0 candidates were masked" in note
    assert "fell back to title tier 1" in note


def test_candidate_none_when_no_available_email():
    people = [
        person("A One", "Founder", reason="email-cell-unavailable"),
        person("B Two", "CEO", reason=""),
    ]
    chosen, note = select_candidate(people)
    assert chosen is None
    assert "no people with an available email" in note


def masked_person(name, title="Engineer", key=None):
    return Person(
        person_key=key or name,
        name_raw=f"{name} ------",
        full_name=name,
        masked=True,
        title=title,
        reason="email-cell-verified",
    )


def test_rendered_email_counts_detect_collisions():
    people = [
        person("Ahmed Ali", "Engineer"),
        masked_person("Ahmed A", key="masked-1"),
        masked_person("Sara B", key="masked-2"),
    ]
    counts = _rendered_email_counts(people, "{l}{first}", "albaraka.bh")
    # Both Ahmed rows render to aahmed@..., so a masked guess would be ambiguous.
    assert counts["aahmed@albaraka.bh"] == 2
    assert counts["bsara@albaraka.bh"] == 1


def test_masked_name_can_use_first_name_and_last_initial_pattern():
    person_row = masked_person("Ahmed A")
    counts = _rendered_email_counts([person_row], "{l}{first}", "albaraka.bh")
    filled = _guess_from_pattern(
        person_row,
        "{l}{first}",
        "low",
        "albaraka.bh",
        _GoodMX(),
        counts,
    )
    assert filled is True
    assert person_row.email == "aahmed@albaraka.bh"
    assert person_row.source == "guessed"
    assert person_row.confidence == "low"
    assert person_row.reason == "partial_name_guess"
    assert "partial name" in person_row.validation


def test_masked_name_skips_duplicate_rendered_address():
    people = [
        masked_person("Ahmed A", key="one"),
        masked_person("Ahmed A", key="two"),
    ]
    counts = _rendered_email_counts(people, "{l}{first}", "albaraka.bh")
    filled = _guess_from_pattern(
        people[0],
        "{l}{first}",
        "low",
        "albaraka.bh",
        _GoodMX(),
        counts,
    )
    assert filled is False
    assert people[0].email == ""
    assert people[0].reason == "duplicate_rendered_email"
    assert "collide" in people[0].validation


def test_masked_name_needing_full_last_name_is_explained():
    person_row = masked_person("Ahmed A")
    filled = _guess_from_pattern(
        person_row,
        "{first}.{last}",
        "high",
        "albaraka.bh",
        _GoodMX(),
        {},
    )
    assert filled is False
    assert person_row.email == ""
    assert person_row.reason == "masked_name"
    assert "needs the full last name" in person_row.validation


def test_reenrich_saved_result_uses_stored_reveal_without_credits():
    result = CompanyResult(
        domain="albaraka.bh",
        company_name="Al Baraka",
        wellfound_company_id="",
        status="processed",
        email="hmohammed@albaraka.bh",
        email_source="verified",
        revealed_name="Mohammed Halabi",
        pattern="",
        credit_spent=1,
        facts=CompanyFacts(employees="70"),
        people=[
            Person(
                person_key="verified",
                name_raw="Mohammed Halabi",
                full_name="Mohammed Halabi",
                masked=False,
                title="Manager",
                email="hmohammed@albaraka.bh",
                source="verified",
                validation="real email revealed via Apollo",
                confidence="high",
                reason="revealed",
            ),
            masked_person("Ahmed A", key="ahmed"),
            masked_person("Sara B", key="sara"),
        ],
    )
    filled = reenrich_result(result, _GoodMX())
    assert filled == 2
    assert result.people[1].email == "aahmed@albaraka.bh"
    assert result.people[2].email == "bsara@albaraka.bh"
    assert result.people[1].confidence == "low"
    assert result.pattern == "lfirst"
    assert "re-enriched 2" in result.notes
    assert result.credit_spent == 1
