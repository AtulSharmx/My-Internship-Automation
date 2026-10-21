from apollo_tool.names import split_full_name
from apollo_tool.patterns import guess_email, infer_pattern
from apollo_tool.runner import title_tier


def test_split_simple():
    parts = split_full_name("John Smith")
    assert parts.first == "john"
    assert parts.last == "smith"
    assert parts.usable


def test_split_comma_and_suffix():
    parts = split_full_name("Smith, John Jr.")
    assert parts.first == "john"
    assert parts.last == "smith"


def test_split_accents_and_apostrophe():
    parts = split_full_name("José O'Connor")
    assert parts.first == "jose"
    assert parts.last == "oconnor"


def test_infer_first_last():
    match = infer_pattern("john.smith@acme.com", "John Smith")
    assert match is not None
    assert match.template == "{first}.{last}"
    assert match.confidence == "high"


def test_infer_concatenated():
    match = infer_pattern("johnsmith@acme.com", "John Smith")
    assert match is not None
    assert match.template == "{first}{last}"


def test_infer_initial_dot_last():
    match = infer_pattern("j.smith@acme.com", "John Smith")
    assert match is not None
    assert match.template == "{f}.{last}"


def test_infer_first_dot_initial():
    match = infer_pattern("john.s@acme.com", "John Smith")
    assert match is not None
    assert match.template == "{first}.{l}"


def test_infer_last_first():
    match = infer_pattern("smithj@acme.com", "John Smith")
    assert match is not None
    assert match.template == "{last}{f}"


def test_infer_first_only():
    match = infer_pattern("john@acme.com", "John Smith")
    assert match is not None
    assert match.template == "{first}"


def test_numeric_suffix_noted():
    match = infer_pattern("john.smith2@acme.com", "John Smith")
    assert match is not None
    assert match.template == "{first}.{last}"
    assert "suffix" in match.note


def test_role_address_rejected():
    assert infer_pattern("info@acme.com", "John Smith") is None


def test_masked_name_rejected():
    assert infer_pattern("jamie.f@acme.com", "Jamie F") is None


def test_accented_name():
    match = infer_pattern("jose.nunez@acme.com", "José Núñez")
    assert match is not None
    assert match.template == "{first}.{last}"


def test_middle_name_pattern():
    match = infer_pattern("mary.j.watson@acme.com", "Mary Jane Watson")
    assert match is not None
    assert match.template == "{first}.{m}.{last}"


def test_guess_email():
    assert guess_email("{first}.{last}", "Jane Doe", "acme.com") == "jane.doe@acme.com"


def test_title_tiers():
    assert title_tier("Co-Founder") == 0
    assert title_tier("Founder") == 0
    assert title_tier("Founding Designer") == 4
    assert title_tier("Chief Executive Officer") == 1
    assert title_tier("Executive Assistant to Chief Executive Officer") == 4
    assert title_tier("CTO") == 2
    assert title_tier("Head of Talent") == 3
    assert title_tier("People Operations") == 3
    assert title_tier("Head of Security") == 4
