from apollo_tool.models import Person
from apollo_tool.runner import select_candidate, title_tier


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
