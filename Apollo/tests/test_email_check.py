from apollo_tool.email_check import MXChecker, MXResult, validate_email, validate_syntax


class FakeChecker:
    def __init__(self, result):
        self.result = result

    def check(self, domain):
        return self.result


def test_syntax():
    assert validate_syntax("jane.doe@acme.com")
    assert validate_syntax("jane+tag@sub.acme.co.uk")
    assert not validate_syntax("jane@")
    assert not validate_syntax("@acme.com")
    assert not validate_syntax("jane doe@acme.com")
    assert not validate_syntax("jane@acme")


def test_validate_with_mx_ok():
    result = validate_email("jane.doe@acme.com", FakeChecker(MXResult("ok", "1 MX")))
    assert result.validation == "syntax_ok; mx_ok"
    assert result.confidence == "high"


def test_validate_no_mx_is_low():
    result = validate_email("jane.doe@acme.com", FakeChecker(MXResult("no_mx_record", "none")))
    assert "no_mx_record" in result.validation
    assert result.confidence == "low"


def test_validate_invalid_syntax():
    result = validate_email("not-an-email", MXChecker())
    assert result.validation == "invalid_syntax"
    assert result.confidence == "low"
