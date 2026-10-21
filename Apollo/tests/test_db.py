from apollo_tool.db import Database
from apollo_tool.models import CompanyFacts, CompanyResult, Person


def make_result(domain="acme.com", wfid="123", status="processed"):
    return CompanyResult(
        domain=domain,
        company_name="Acme",
        wellfound_company_id=wfid,
        status=status,
        email="jane.doe@acme.com" if status == "processed" else "",
        email_source="verified" if status == "processed" else "",
        notes="pattern first.last",
        revealed_name="Jane Doe",
        pattern="first.last",
        credit_spent=1,
        credits_before=75,
        credits_after=74,
        facts=CompanyFacts(employees="40", industries=["Software"]),
        people=[
            Person(
                person_key="p1",
                name_raw="Jane Doe",
                full_name="Jane Doe",
                masked=False,
                title="Co-Founder",
                tier=0,
                email="jane.doe@acme.com",
                source="verified",
                validation="real email revealed via Apollo",
                confidence="high",
            ),
            Person(
                person_key="p2",
                name_raw="John Smith",
                full_name="John Smith",
                masked=False,
                title="CTO",
                tier=2,
                email="john.smith@acme.com",
                source="guessed",
                validation="syntax_ok; mx_ok",
                confidence="high",
            ),
        ],
    )


def test_attempt_then_result(tmp_path):
    db = Database(tmp_path / "test.db")
    assert db.result_for("acme.com") is None
    db.begin_attempt("acme.com", "Acme", "123", "run1", 75)
    row = db.get_by_domain("acme.com")
    assert row["status"] == "attempting"
    assert row["credit_spent"] == 1
    db.save_result(make_result(), "run1")
    result = db.result_for("acme.com")
    assert result is not None
    assert result.status == "processed"
    assert result.email == "jane.doe@acme.com"
    assert len(result.people) == 2
    assert result.people[0].source == "verified"
    assert db.ledger_summary()["credits_used"] == 1


def test_wfid_lookup(tmp_path):
    db = Database(tmp_path / "test.db")
    db.save_result(make_result(domain="old.com", wfid="999"), "run1")
    found = db.find_company("new.com", "999")
    assert found is not None
    assert found["domain"] == "old.com"


def test_reset(tmp_path):
    db = Database(tmp_path / "test.db")
    db.save_result(make_result(), "run1")
    assert db.reset_domain("acme.com")
    assert db.result_for("acme.com") is None


def test_persistence(tmp_path):
    path = tmp_path / "test.db"
    db = Database(path)
    db.save_result(make_result(), "run1")
    db2 = Database(path)
    result = db2.result_for("acme.com")
    assert result is not None
    assert result.revealed_name == "Jane Doe"
