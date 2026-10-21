"""Email syntax and MX validation."""

from __future__ import annotations

import re
from dataclasses import dataclass

import dns.exception
import dns.resolver

EMAIL_RE = re.compile(
    r"^(?=.{1,254}$)(?=.{1,64}@)[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)


@dataclass(frozen=True)
class MXResult:
    status: str
    detail: str
    records: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmailValidation:
    validation: str
    confidence: str
    mx_status: str

    @property
    def ok(self) -> bool:
        return self.mx_status == "ok"


class MXChecker:
    def __init__(self, lifetime: float = 6.0) -> None:
        self.lifetime = lifetime
        self._cache: dict[str, MXResult] = {}

    def check(self, domain: str) -> MXResult:
        domain = (domain or "").lower().strip(".")
        if not domain:
            return MXResult("invalid_domain", "empty domain")
        if domain in self._cache:
            return self._cache[domain]

        resolver = dns.resolver.Resolver(configure=True)
        resolver.lifetime = self.lifetime
        resolver.timeout = min(4.0, self.lifetime)

        try:
            answers = resolver.resolve(domain, "MX")
            records = sorted(
                (
                    (int(rdata.preference), str(rdata.exchange).rstrip("."))
                    for rdata in answers
                ),
                key=lambda item: item[0],
            )
            result = MXResult(
                "ok",
                f"{len(records)} MX record(s)",
                tuple(exchange for _, exchange in records),
            )
        except dns.resolver.NXDOMAIN:
            result = MXResult("domain_not_found", "domain does not exist")
        except (dns.resolver.NoAnswer, dns.resolver.NoMetaqueries):
            try:
                resolver.resolve(domain, "A")
                result = MXResult("no_mx_record", "domain resolves but has no MX record")
            except Exception:  # noqa: BLE001
                result = MXResult("no_mx_record", "no MX record")
        except (dns.resolver.LifetimeTimeout, dns.resolver.NoNameservers) as exc:
            result = MXResult("mx_lookup_failed", f"DNS lookup failed: {type(exc).__name__}")
        except dns.exception.DNSException as exc:
            result = MXResult("mx_lookup_failed", f"DNS error: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001
            result = MXResult("mx_lookup_failed", f"lookup error: {type(exc).__name__}")

        self._cache[domain] = result
        return result


def validate_syntax(email: str) -> bool:
    return bool(email and EMAIL_RE.match(email))


def validate_email(email: str, checker: MXChecker) -> EmailValidation:
    if not validate_syntax(email):
        return EmailValidation("invalid_syntax", "low", "not_checked")
    domain = email.rsplit("@", 1)[1]
    mx = checker.check(domain)
    if mx.status == "ok":
        return EmailValidation("syntax_ok; mx_ok", "high", mx.status)
    if mx.status == "no_mx_record":
        return EmailValidation("syntax_ok; no_mx_record", "low", mx.status)
    if mx.status == "domain_not_found":
        return EmailValidation("syntax_ok; domain_not_found", "low", mx.status)
    return EmailValidation("syntax_ok; mx_lookup_failed", "low", mx.status)
