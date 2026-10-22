"""Company email-pattern inference and rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .names import NameParts, split_full_name, split_local_part

ROLE_LOCAL_PARTS = {
    "info",
    "hello",
    "contact",
    "sales",
    "support",
    "admin",
    "team",
    "careers",
    "jobs",
    "hr",
    "recruiting",
    "people",
    "office",
    "billing",
    "accounts",
    "marketing",
    "press",
    "legal",
    "privacy",
    "security",
    "noreply",
    "no-reply",
    "webmaster",
    "postmaster",
    "enquiries",
    "inquiries",
    "help",
    "service",
    "mail",
}

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "protonmail.com",
    "proton.me",
    "gmx.com",
    "gmx.net",
    "mail.com",
    "mail.ru",
    "yandex.com",
    "yandex.ru",
    "zoho.com",
    "qq.com",
    "163.com",
    "126.com",
}


@dataclass(frozen=True)
class PatternSpec:
    template: str
    needs_middle: bool = False

    @property
    def description(self) -> str:
        return self.template.replace("{", "").replace("}", "")


# Ordered most-specific first. Exact string matches win over canonical matches,
# so the order mainly matters for ties.
PATTERNS: tuple[PatternSpec, ...] = (
    PatternSpec("{first}.{middle}.{last}", needs_middle=True),
    PatternSpec("{first}_{middle}_{last}", needs_middle=True),
    PatternSpec("{first}{middle}{last}", needs_middle=True),
    PatternSpec("{first}.{m}.{last}", needs_middle=True),
    PatternSpec("{first}{m}{last}", needs_middle=True),
    PatternSpec("{f}.{m}.{last}", needs_middle=True),
    PatternSpec("{first}.{last}"),
    PatternSpec("{first}_{last}"),
    PatternSpec("{first}-{last}"),
    PatternSpec("{first}{last}"),
    PatternSpec("{last}.{first}"),
    PatternSpec("{last}_{first}"),
    PatternSpec("{last}{first}"),
    PatternSpec("{f}.{last}"),
    PatternSpec("{f}_{last}"),
    PatternSpec("{f}-{last}"),
    PatternSpec("{f}{last}"),
    PatternSpec("{first}.{l}"),
    PatternSpec("{first}_{l}"),
    PatternSpec("{first}{l}"),
    # Real-world formats that use the last initial before the first name.
    # Example: Mohammed Halabi -> hmohammed@albaraka.bh
    PatternSpec("{l}{first}"),
    PatternSpec("{l}.{first}"),
    PatternSpec("{l}_{first}"),
    PatternSpec("{l}-{first}"),
    PatternSpec("{last}.{f}"),
    PatternSpec("{last}{f}"),
    PatternSpec("{first}"),
    PatternSpec("{last}"),
)

# Exact matches in these formats are common enough to keep high confidence.
# Anything else is still accepted, but deliberately marked low confidence.
COMMON_TEMPLATES = {
    "{first}.{last}",
    "{first}_{last}",
    "{first}-{last}",
    "{first}{last}",
    "{f}.{last}",
    "{f}_{last}",
    "{f}-{last}",
    "{f}{last}",
    "{first}.{l}",
    "{first}_{l}",
    "{first}{l}",
    "{first}",
}

# Formats that need a full last name (or a middle name) cannot be rendered for
# a masked name such as "Dylan F". Formats that only need first name/initial
# can still produce a safe, clearly-labelled partial guess.
PARTIAL_NAME_BLOCKERS = ("{last}", "{middle}", "{m}")

# The workbook stores the human-readable description (for example "first.last").
# This mapping lets older saved results be re-enriched without a new Apollo run.
TEMPLATE_BY_DESCRIPTION = {spec.description: spec.template for spec in PATTERNS}


@dataclass(frozen=True)
class PatternMatch:
    template: str
    example: str
    confidence: str
    note: str

    @property
    def description(self) -> str:
        return self.template.replace("{", "").replace("}", "")


def canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def is_role_address(local: str) -> bool:
    return local.lower().strip() in ROLE_LOCAL_PARTS


def _values(parts: NameParts) -> dict[str, str]:
    return {
        "first": parts.first,
        "last": parts.last,
        "middle": parts.middle,
        "f": parts.first_initial,
        "l": parts.last_initial,
        "m": parts.middle_initial,
    }


def render_pattern(template: str, parts: NameParts) -> str | None:
    """Render a pattern, allowing partial names when the pattern permits it.

    A masked Apollo name ("Dylan F") only exposes the first name and last
    initial. Patterns such as ``{first}`` or ``{first}{l}`` can still be
    rendered from that information; patterns that need the full last name
    cannot. Single-name contacts are handled the same way.
    """
    if not parts.first:
        return None
    if any(blocker in template for blocker in PARTIAL_NAME_BLOCKERS):
        if not parts.usable:
            return None
    if "{l}" in template and not parts.last:
        return None
    values = _values(parts)
    if "{middle}" in template or "{m}" in template:
        if not parts.middle:
            return None
    try:
        rendered = template.format(**values)
    except (KeyError, IndexError):
        return None
    rendered = rendered.strip().lower()
    rendered = re.sub(r"[-_.]{2,}", lambda m: m.group(0)[0], rendered)
    if not rendered or rendered.startswith((".", "-", "_")) or rendered.endswith((".", "-", "_")):
        return None
    if len(rendered) > 64:
        return None
    return rendered


def can_render_partial_name(template: str, parts: NameParts) -> bool:
    """True when a masked/incomplete name still has everything the pattern needs."""
    if not template or not parts.first:
        return False
    if any(blocker in template for blocker in PARTIAL_NAME_BLOCKERS):
        return False
    if "{l}" in template and not parts.last:
        return False
    return render_pattern(template, parts) is not None


def guess_email(template: str, full_name: str, domain: str) -> str | None:
    parts = split_full_name(full_name)
    local = render_pattern(template, parts)
    if not local or not domain:
        return None
    return f"{local}@{domain.lower().strip('.')}"


def infer_pattern(email: str, full_name: str) -> PatternMatch | None:
    if not email or "@" not in email:
        return None
    local_raw, domain = email.rsplit("@", 1)
    if not local_raw or not domain:
        return None
    if is_role_address(local_raw):
        return None

    base, suffix = split_local_part(local_raw)
    if not base:
        return None
    parts = split_full_name(full_name)
    if not parts.usable:
        return None

    best: tuple[int, PatternSpec, str, bool] | None = None
    for spec in PATTERNS:
        rendered = render_pattern(spec.template, parts)
        if not rendered:
            continue
        if base == rendered:
            score = 1000 + len(rendered)
            candidate = (score, spec, rendered, True)
        elif canonical(base) == canonical(rendered):
            score = 500 + len(rendered)
            candidate = (score, spec, rendered, False)
        else:
            continue
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is None:
        return None

    _, spec, rendered, exact = best
    note = "separator confirmed by the revealed address" if exact else (
        "punctuation was stripped in the revealed address; separator inferred"
    )
    if exact and spec.template not in COMMON_TEMPLATES:
        note += "; unusual format, so guesses from it stay low confidence"
    if suffix:
        note += f"; revealed address has numeric suffix '{suffix}', which was excluded from the pattern"
    return PatternMatch(
        template=spec.template,
        example=f"{rendered}@{domain.lower()}",
        confidence="high" if exact and spec.template in COMMON_TEMPLATES else "low",
        note=note,
    )


def is_free_email_domain(domain: str) -> bool:
    return (domain or "").lower().strip(".") in FREE_EMAIL_DOMAINS


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[1].lower().strip(".") if "@" in email else ""
