"""Wellfound automation: session handling, search, and the application flow.

Grounded in the live markup of wellfound.com (checked while building):

* listings link to ``/jobs/{id}-{slug}``. Discovery uses the signed-in ``/jobs``
  search (Internship + 1-10 employees, Most recent) as its primary feed and
  falls back to public role/city pages (``/role/r/<slug>``) only if that search
  errors.
* the application form opens in a modal that carries
  ``data-test="JobApplicationModal--SubmitButton"``
* anti-bot is Cloudflare **Turnstile**, raised through a modal overlay
* signed-out visitors always see ``Log In`` / ``Sign Up`` in the nav - that is
  how the bot tells an expired session from a live one (cookie *names* are not
  a reliable signal: analytics cookies such as ``rl_session`` exist for
  anonymous visitors too)

Everything that can move (selector names, form shapes) is centralised in this
module so when Wellfound ships a redesign you only edit one file.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeout,
)

from . import guards, human
from .config import Config, Role
from .models import Job, Outcome, Status, display_title, utcnow

log = logging.getLogger("wfbot.wellfound")

APPLY_NAME_RE = re.compile(
    r"^\s*(apply now|apply|start (?:your )?application|submit application)\s*$",
    re.IGNORECASE,
)
SUBMIT_NAME_RE = re.compile(
    r"^\s*(submit (?:your )?application|send (?:my )?application|submit|send)\s*$",
    re.IGNORECASE,
)
EXTERNAL_APPLY_RE = re.compile(
    r"apply (?:on|at|to) .{0,30}(?:company|employer|site)|apply externally|go to (?:the )?site",
    re.IGNORECASE,
)
CONFIRMATION_RE = re.compile(
    r"application (?:was |have been )?(?:sent|submitted|received|completed)"
    r"|you(?:'|’)?ve applied"
    r"|thanks for (?:your )?applying"
    r"|application is (?:now )?on its way",
    re.IGNORECASE,
)
ALREADY_RE = re.compile(
    r"you(?:'|’)?ve already applied|already applied to|application pending|withdraw (?:your )?application",
    re.IGNORECASE,
)
#: The listing itself flips to a "✓ Applied" badge once the submit lands.
APPLIED_STATE_RE = re.compile(r"^\s*(?:\u2713\s*)?applied\s*$", re.IGNORECASE)
CLOSED_RE = re.compile(
    r"no longer accepting applications|this (?:role|job|position) (?:has been )?closed"
    r"|position (?:is )?no longer (?:available|open)|job (?:is )?no longer",
    re.IGNORECASE,
)
#: Wellfound keeps duplicate postings around with a "(Clone)" title/slug suffix.
#: Applying to both would look like spam to the same employer.
CLONE_RE = re.compile(r"\(\s*clone\s*\)|(?:^|[-_])clone$", re.IGNORECASE)
#: Wellfound's application history: each card carries Pending / Accepted /
#: Not Accepted. Used to track responses and to import manual applications.
APPLICATIONS_URL = "https://wellfound.com/jobs/applications"
ARCHIVED_APPLICATIONS_URL = "https://wellfound.com/jobs/applications/archived"
PROFILE_URL = "https://wellfound.com/profile/edit"
APPLICATION_PATH_RE = re.compile(
    r"^/jobs/applications/(?P<application_id>\d+)-(?P<job_id>\d+)"
)
#: The applications page wraps each row in ``/jobs/applications/{id}-{job_id}``;
#: the card's own text is "Company\\nTitle\\nStatus\\nDate".
EXTRACT_APPLICATIONS_JS = """
(anchors) => anchors.map((a) => ({
  href: a.getAttribute('href') || '',
  text: (a.innerText || '').trim(),
}))
"""
APPLICATION_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
APPLICATION_RELATIVE_DATE_RE = re.compile(
    r"\b(?:(\d+)\s+)?(day|days|week|weeks|month|months)\s+ago\b", re.IGNORECASE
)
APPLICATION_MONTH_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b",
    re.IGNORECASE,
)
RESPONSE_RE = re.compile(
    r"\b(not accepted|accepted|pending|rejected|declined)\b", re.IGNORECASE
)
PROFILE_WARNING_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\byour profile is incomplete\b", re.IGNORECASE),
        "profile is marked incomplete",
    ),
    (
        re.compile(
            r"\b(?:add|upload) (?:your )?resume\b|\bresume (?:is )?missing\b",
            re.IGNORECASE,
        ),
        "no resume is attached to the profile",
    ),
    (
        re.compile(r"\badd (?:a )?(?:bio|headline)\b", re.IGNORECASE),
        "bio/headline is empty",
    ),
)


def parse_response(text: str) -> str:
    """Normalise an applications-page status: pending / accepted / not accepted."""
    match = RESPONSE_RE.search(text or "")
    if not match:
        return ""
    value = match.group(1).lower()
    return "not accepted" if value in {"rejected", "declined"} else value


def slugify_title(title: str) -> str:
    """Build the public URL slug Wellfound expects for a job title."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def job_from_application_href(href: str, title: str = "") -> Job | None:
    """Turn ``/jobs/applications/{application_id}-{job_id}`` into a Job.

    The applications page does not link to the public job URL, but the numeric
    tail of its href is the job id and the card carries the title. Wellfound
    requires a slug after the id (``/jobs/{id}-{slug}``), so use the slugified
    title when it is available.
    """
    match = APPLICATION_PATH_RE.match(urlparse(href).path)
    if match is None:
        return None
    job_id = match.group("job_id")
    slug = slugify_title(title)
    return Job(
        job_id=job_id,
        slug=slug or job_id,
        url=(
            f"https://wellfound.com/jobs/{job_id}-{slug}"
            if slug
            else f"https://wellfound.com/jobs/{job_id}"
        ),
        title=title,
        query="applications page",
    )


def parse_application_date(
    text: str, *, now: datetime | None = None
) -> datetime | None:
    """Parse the date column of an applications-page card.

    Handles ``YESTERDAY``, ``TODAY``, ``3 DAYS AGO``, ``2 WEEKS AGO`` and
    ``OCT 7``. Returning None means "unknown"; the caller treats that as now.
    """
    now = now or datetime.now(timezone.utc)
    value = (text or "").strip()
    if re.search(r"\btoday\b", value, re.IGNORECASE):
        return now
    if re.search(r"\byesterday\b", value, re.IGNORECASE):
        return now - timedelta(days=1)
    match = APPLICATION_RELATIVE_DATE_RE.search(value)
    if match:
        count = int(match.group(1) or 1)
        unit = match.group(2).lower()
        if unit.startswith("week"):
            days = count * 7
        elif unit.startswith("month"):
            days = count * 30
        else:
            days = count
        return now - timedelta(days=days)
    match = APPLICATION_MONTH_DATE_RE.search(value)
    if match:
        month = APPLICATION_MONTHS[match.group(1).lower()[:3]]
        day = int(match.group(2))
        try:
            parsed = datetime(now.year, month, day, 12, tzinfo=timezone.utc)
        except ValueError:
            return None
        if parsed > now + timedelta(days=1):
            parsed = parsed.replace(year=now.year - 1)
        return parsed
    return None


SUBMIT_ERROR_RE = re.compile(
    r"something went wrong|please try again|error submitting|could not (?:be )?sent",
    re.IGNORECASE,
)
LOGIN_ERROR_RE = re.compile(
    r"invalid (?:email|password|credentials)|incorrect password|couldn.t find (?:an )?account",
    re.IGNORECASE,
)
MAGIC_LINK_RE = re.compile(
    r"check your (?:email|inbox)|we sent you|one[- ]time (?:code|link)|magic link|verification code",
    re.IGNORECASE,
)

#: Company head-count shown on the job page, e.g. "1-10 Employees",
#: "11-50 Employees", "1001+ Employees". Used to prefer tiny startups.
COMPANY_SIZE_RANGE_RE = re.compile(
    r"([\d,]{1,7})\s*[-\u2013\u2014]\s*([\d,]{1,7})\s*employees?", re.IGNORECASE
)
COMPANY_SIZE_PLUS_RE = re.compile(r"([\d,]{1,7})\s*\+\s*employees?", re.IGNORECASE)
COMPANY_SIZE_SINGLE_RE = re.compile(
    r"(?<![\d,-\u2013\u2014])([\d,]{1,7})\s*employees?", re.IGNORECASE
)


def parse_company_size(text: str) -> tuple[str, int, int] | None:
    """Pull ``("11-50", 11, 50)`` out of a job page, or None when not shown."""
    if not text:
        return None
    match = COMPANY_SIZE_RANGE_RE.search(text)
    if match:
        low = int(match.group(1).replace(",", ""))
        high = int(match.group(2).replace(",", ""))
        return f"{low}-{high}", low, high
    match = COMPANY_SIZE_PLUS_RE.search(text)
    if match:
        low = int(match.group(1).replace(",", ""))
        return f"{low}+", low, 10**9
    match = COMPANY_SIZE_SINGLE_RE.search(text)
    if match:
        count = int(match.group(1).replace(",", ""))
        return str(count), count, count
    return None


#: The freshness stamp in the listing header, verified live on Wellfound:
#: "POSTED 2 HOURS AGO", "POSTED 3 WEEKS AGO", "POSTED 1 YEAR AGO".
POSTED_AGE_RE = re.compile(
    r"\bposted\s+"
    r"(?:(?P<amount>\d+)\s*\+?\s*(?P<unit>minute|hour|day|week|month|year)s?\s+ago"
    r"|(?P<phrase>just\s+now|today|yesterday))",
    re.IGNORECASE,
)
_AGE_UNIT_DAYS = {
    "minute": 1.0 / 1440,
    "hour": 1.0 / 24,
    "day": 1.0,
    "week": 7.0,
    "month": 30.0,
    "year": 365.0,
}


def parse_posted_age(text: str) -> float | None:
    """How many days ago the listing was posted, or ``None`` when unstated.

    Only the top of the page is searched: the stamp sits next to Save/Apply,
    while the description below can contain unrelated "…ago" phrases.
    """
    if not text:
        return None
    match = POSTED_AGE_RE.search(text[:4000])
    if not match:
        return None
    return _match_age_days(match)


def format_age(days: float) -> str:
    """A short human age: ``45 min`` / ``6 h`` / ``3 d`` / ``2 mo``."""
    if days < 1.0 / 24:
        return f"{max(1, round(days * 1440))} min"
    if days < 1.0:
        return f"{round(days * 24)} h"
    if days < 30:
        return f"{round(days)} d"
    return f"{max(1, round(days / 30))} mo"


#: Listing cards print the age without the word "posted":
#: ``today``, ``yesterday``, ``2 hours ago``, ``3 weeks ago``, ``1 month ago``.
#: Parsing this *before* opening the detail page saves a page load and keeps the
#: bot from crawling weeks-old leftovers.
CARD_AGE_RE = re.compile(
    r"\b(?:(?P<amount>\d+)\s*\+?\s*(?P<unit>minute|hour|day|week|month|year)s?\s+ago"
    r"|(?P<phrase>just\s+now|today|yesterday))\b",
    re.IGNORECASE,
)


def _match_age_days(match: re.Match[str]) -> float:
    phrase = (match.group("phrase") or "").lower()
    if phrase:
        return 0.0 if "yesterday" not in phrase else 1.0
    amount = int(match.group("amount"))
    return amount * _AGE_UNIT_DAYS[match.group("unit").lower()]


def parse_card_age(text: str) -> float | None:
    """Age of a listing card ("today", "2 weeks ago"), or ``None``."""
    if not text:
        return None
    match = CARD_AGE_RE.search(text)
    return _match_age_days(match) if match else None


#: "Internship" / "Full-time" shown on the card, next to the title. "intern"
#: alone is deliberately absent: it is part of the title, not a job type.
JOB_TYPE_RE = re.compile(
    r"\b(internship|full[\s-]?time|part[\s-]?time|contract|co-?op)\b",
    re.IGNORECASE,
)


def parse_job_type(text: str) -> str:
    match = JOB_TYPE_RE.search(text or "")
    if not match:
        return ""
    found = match.group(1).lower()
    if found.startswith("intern"):
        return "Internship"
    if found.startswith("full"):
        return "Full-time"
    if found.startswith("part"):
        return "Part-time"
    return found.title()


#: Listings that cost the applicant money (or pay nothing) are not worth an
#: application. "Equity only" is included: it is not a stipend.
UNPAID_RE = re.compile(
    r"\bunpaid\b|\bno stipend\b|\bcommission[\s-]?only\b|\bequity[\s-]?only\b"
    r"|\btraining fee\b|\bsecurity deposit\b|\bpay to (?:join|apply)\b",
    re.IGNORECASE,
)


def is_unpaid(*parts: str) -> bool:
    """True for unpaid / commission-only / equity-only listings.

    A card that shows equity (``0.1% - 0.5%``) but no money at all is an
    equity-only role - it pays nothing, so it is treated like "unpaid".
    """
    text = " ".join(part or "" for part in parts)
    if UNPAID_RE.search(text):
        return True
    return bool(EQUITY_RE.search(text)) and not MONEY_RE.search(text)


#: A money amount or a salary range on the card, e.g. "₹2.4L", "$150k – $280k".
MONEY_RE = re.compile(
    r"(?:₹|Rs\.?|INR|\$|USD|€|EUR|£|GBP)\s?[\d][\d,.]*\s?[kKlL]?"
    r"(?:\s*(?:-|–|—|to)\s*(?:₹|Rs\.?|INR|\$|USD|€|EUR|£|GBP)?\s?"
    r"[\d][\d,.]*\s?[kKlL]?)?",
    re.IGNORECASE,
)
EQUITY_RE = re.compile(
    r"\d+(?:\.\d+)?%\s*(?:-|–|—|to)\s*\d+(?:\.\d+)?%", re.IGNORECASE
)


def parse_stipend(text: str) -> str:
    """First salary/equity amount on a listing card ("" when not shown)."""
    if not text:
        return ""
    match = MONEY_RE.search(text)
    if match:
        return match.group(0).strip(" •|")
    match = EQUITY_RE.search(text)
    return match.group(0).strip(" •|") if match else ""


#: Work mode + area as printed on a card: ``Remote only • Everywhere``,
#: ``Remote • San Francisco``, ``In office • Gurugram``, ``Hybrid • Pune``.
#: The area stops before the experience/age/trailing controls.
CARD_LOCATION_RE = re.compile(
    r"\b(Remote only|Remote|In office|Hybrid)\s*[•·|]\s*(.+?)"
    r"(?=\s+(?:\d+\s+years?\s+of\s+exp|Save|Apply"
    r"|\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago|today|yesterday)\b|$)",
    re.IGNORECASE,
)

#: Onsite/hybrid cities we are willing to commute to.
CARD_ONSITE_OK_RE = re.compile(r"\b(?:gurugram|gurgaon)\b", re.IGNORECASE)


def parse_card_location(meta: str) -> tuple[str, str]:
    """Return ``(work_mode, area)`` from a listing card, or ``("", "")``."""
    match = CARD_LOCATION_RE.search(meta or "")
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip(" •|,;")


def card_location_verdict(meta: str) -> tuple[str, str] | None:
    """Skip onsite/hybrid cards outside Gurugram before opening the page.

    Remote cards are left alone: only the detail page (and Wellfound's own
    location/timezone warning) can tell whether India is actually supported.
    """
    mode, area = parse_card_location(meta)
    if not mode:
        return None
    if mode.lower() in {"in office", "hybrid"}:
        if not CARD_ONSITE_OK_RE.search(area):
            return mode, area
    return None


#: Wellfound's own application modal warns when the role cannot hire from the
#: profile's location/timezone. Applying anyway produces a dead application.
LOCATION_MISMATCH_RE = re.compile(
    r"does not support the locations? or timezones?"
    r"|job does not support (?:the )?locations?"
    r"|(?:locations?|timezones?) (?:is|are) not supported",
    re.IGNORECASE,
)

#: Questions that are about *this* employer/role (or ask for a long, original
#: answer) are never reused from the answer bank; a generic saved answer would
#: be worse than asking. ``discover``/``hear about`` catch the common
#: employer-specific "How did you find us?" radio question.
JOB_SPECIFIC_RE = re.compile(
    r"\b(?:why|cover letter|motivat|excited|this (?:role|company|position|job)"
    r"|about you|introduce yourself|pitch|tell us|discover|hear about|bullets?"
    r"|examples?|achievement|portfolio|work sample|assignment|projects?"
    r"|interests?|passion|favorite)\b",
    re.IGNORECASE,
)

#: Replies that mean "do not apply to this one".
SKIP_WORDS = frozenset({"skip", "/skip", "cancel", "/cancel"})


def answer_key(label: str) -> str:
    """Stable lookup key for a form question ("LinkedIn URL*" -> "linkedin url")."""
    text = re.sub(r"[^a-z0-9]+", " ", (label or "").lower().replace("*", " "))
    return re.sub(r"\s+", " ", text).strip()[:160]


#: Job-page sections that carry location. The rendered text has no spaces
#: between labels ("Job LocationKolkata • BengaluruHires remotely inEverywhere"),
#: so each capture stops at the next known label.
_LOCATION_STOP = (
    r"(?:Hires remotely in|Remote Work Policy|Visa Sponsorship|Relocation|Skills"
    r"|About the job|$)"
)
JOB_LOCATION_RE = re.compile(
    r"Job Location\s*(.{0,180}?)\s*" + _LOCATION_STOP, re.IGNORECASE | re.DOTALL
)
REMOTE_POLICY_RE = re.compile(
    r"Remote Work Policy\s*(.{0,120}?)\s*" + _LOCATION_STOP, re.IGNORECASE | re.DOTALL
)
HIRES_REMOTE_RE = re.compile(
    r"Hires remotely in\s*(.{0,120}?)\s*(?:Visa Sponsorship|Relocation|Skills"
    r"|About the job|Remote Work Policy|Job Location|$)",
    re.IGNORECASE | re.DOTALL,
)


def parse_location(text: str) -> tuple[str, str]:
    """Return ``(location_display, hires_remotely_text)`` from a job page."""
    compact = re.sub(r"\s+", " ", text or " ")
    parts: list[str] = []
    match = JOB_LOCATION_RE.search(compact)
    if match:
        parts.append(match.group(1).strip(" •"))
    match = REMOTE_POLICY_RE.search(compact)
    if match and match.group(1).strip():
        parts.append(match.group(1).strip(" •"))
    hires = ""
    match = HIRES_REMOTE_RE.search(compact)
    if match:
        hires = match.group(1).strip(" •")
    location = " • ".join(part for part in parts if part)
    return location[:180], hires[:120]

#: Nav/overlay elements Wellfound only renders to signed-out visitors.
#: Observed on ``/jobs``, ``/role/r/<slug>`` and job detail pages alike.
SIGNED_OUT_MARKERS: tuple[str, ...] = (
    'a[href="/login"]',
    'a[href*="/jobs/signup"]',
    'button:has-text("Log In")',
    'button:has-text("Sign Up")',
)

#: Extracts listing rows from a results page in one pass.
#:
#: The card's full text carries the job type, salary, location and age (for
#: example "Full Stack Intern | Internship | ₹2.4L | Remote • India | today"),
#: so it is kept as ``meta`` and parsed in Python. That lets discovery drop
#: stale/unpaid listings before spending a page load on them.
EXTRACT_ROWS_JS = """
(anchors) => anchors.map((a) => {
  const href = a.getAttribute('href') || '';
  const title = (a.textContent || '').trim();
  const row = a.closest('div.mb-2, li, article, div[class*="min-h-"], div[data-test*="Job"]')
    || (a.parentElement && a.parentElement.parentElement);
  let meta = '';
  let company = '';
  if (row) {
    const metaEl = row.querySelector('div[class*="text-sm"]');
    const metaText = metaEl ? (metaEl.textContent || '').trim() : '';
    const companyEl = row.querySelector('a[href*="/company/"]');
    company = companyEl ? (companyEl.textContent || '').trim() : '';
    // Older listing rows print "Company \u2022 Salary \u2022 Location" in one div.
    if (!company && metaText) company = metaText.split('\\u2022')[0].trim();
    const flat = (row.innerText || '').replace(/\\s+/g, ' ').trim();
    meta = flat || metaText;
    if (!meta) {
      meta = (metaText || title).replace(/\\s+/g, ' ').trim();
    }
    // Keep the card readable; the age/salary/location sit in the first line.
    meta = meta.slice(0, 320);
  }
  const applyBtn = row ? row.querySelector('[data-test="JobApplicationApplyButton"]') : null;
  return {
    href,
    title,
    company,
    meta,
    has_apply: !!applyBtn,
    apply_disabled: applyBtn ? !!applyBtn.disabled : false,
  };
})
"""

#: Extracts the signed-in ``/jobs`` search results. Each startup card carries
#: the company and head-count once, followed by one or more job links. Most of
#: the visible card text is already the useful metadata (title, location,
#: compensation, "Posted 1 week ago"), so it is joined into ``meta``.
EXTRACT_SEARCH_ROWS_JS = r"""
() => {
  const rows = [];
  const cards = document.querySelectorAll('[data-test="StartupResult"]');
  for (const card of cards) {
    const company = (card.querySelector('h2')?.innerText || '').trim();
    let companySize = '';
    for (const span of card.querySelectorAll('span')) {
      const text = (span.innerText || '').trim();
      if (/^\d[\d,]*\s*(?:-\s*\d[\d,]*|\+)?\s+Employees?$/i.test(text)) {
        companySize = text;
        break;
      }
    }
    for (const anchor of card.querySelectorAll('a[href^="/jobs/"]')) {
      const titleEl = anchor.querySelector('span[class*="title"]');
      const title = (titleEl?.innerText || '').trim();
      if (!title) continue;
      const locations = (anchor.querySelector('span[class*="locations"]')?.innerText || '').trim();
      const compensation = (anchor.querySelector('span[class*="compensation"]')?.innerText || '').trim();
      const tags = (anchor.querySelector('div[class*="tags"]')?.innerText || '').trim();
      rows.push({
        href: anchor.getAttribute('href') || '',
        title,
        company,
        company_size: companySize,
        meta: [title, locations, compensation, tags].filter(Boolean).join(' | '),
        has_apply: true,
        apply_disabled: false,
      });
    }
  }
  return rows;
}
"""

#: Required-but-empty form controls that a human would have to answer.
#: Each field is tagged with ``data-wfbot-field`` so the answer can be typed
#: back into the exact control the question was about.
#:
#: Live Wellfound marks employer questions as required with a visual ``*`` in
#: the label, but **not** with the HTML ``required`` attribute (verified on a
#: real modal: all seven fields had ``required=false``). So the selector must
#: also include every ``customQuestionAnswers[...]`` control, or the bot would
#: submit an incomplete application (or record a misleading UNVERIFIED).
UNANSWERED_JS = r"""
() => {
  const candidates = Array.from(document.querySelectorAll(
    '[name^="customQuestionAnswers"], input[required], textarea[required], '
    + 'select[required], [aria-required="true"]'
  ));
  const isCustom = (el) => /customQuestionAnswers/i.test(el.name || '');
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    if (rect.width > 0 || rect.height > 0) return true;
    // Radios/checkboxes are often visually hidden behind a styled label.
    const label = el.closest('label');
    if (label) {
      const lr = label.getBoundingClientRect();
      return lr.width > 0 || lr.height > 0;
    }
    return false;
  };
  const textOf = (el) => (el.innerText || el.textContent || '')
    .replace(/\s+/g, ' ').trim();
  const labelFor = (el) => {
    let text = el.getAttribute('aria-label') || '';
    if (!text) {
      const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
      text = ids.map((id) => {
        const node = document.getElementById(id);
        return node ? textOf(node) : '';
      }).join(' ');
    }
    if (!text && el.id) {
      const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (label) text = textOf(label);
    }
    if (!text) {
      const label = el.closest('label');
      if (label) text = textOf(label);
    }
    if (!text) text = el.getAttribute('placeholder') || '';
    if (!text) text = el.name || el.id || '';
    return text.slice(0, 240);
  };
  const cleanLabel = (text) => (text || '')
    .replace(/\s*\*\s*$/, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 240);
  const groupLabel = (el) => {
    const fieldset = el.closest('fieldset');
    if (fieldset) {
      const legend = fieldset.querySelector('legend');
      if (legend) return textOf(legend).slice(0, 240);
    }
    const group = el.closest('[role="radiogroup"], [role="group"]');
    if (group) {
      const label = group.getAttribute('aria-label');
      if (label) return label.slice(0, 240);
      const labelledby = group.getAttribute('aria-labelledby');
      if (labelledby) {
        const text = labelledby.split(/\s+/).map((id) => {
          const node = document.getElementById(id);
          return node ? textOf(node) : '';
        }).join(' ');
        if (text.trim()) return text.trim().slice(0, 240);
      }
    }
    // Custom questions render the question text just before the control group.
    let node = el;
    for (let depth = 0; depth < 6 && node; depth += 1, node = node.parentElement) {
      let prev = node.previousElementSibling;
      while (prev) {
        const text = textOf(prev);
        if (text.length >= 8 && text.length <= 300) return text;
        prev = prev.previousElementSibling;
      }
      const parent = node.parentElement;
      if (parent && parent.firstElementChild && parent.firstElementChild !== node) {
        const first = parent.firstElementChild;
        const text = textOf(first);
        if (first.tagName === 'LABEL' && text.length >= 8 && text.length <= 300) {
          return text;
        }
      }
    }
    return labelFor(el);
  };
  const isOptional = (label) => /\boptional\b/i.test(label || '');
  const isRequired = (el, rawLabel) => {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    if (isCustom(el)) return !isOptional(rawLabel);
    return /\*\s*$/.test((rawLabel || '').trim());
  };
  const out = [];
  let key = 0;
  const add = (el, type, label, extra) => {
    el.setAttribute('data-wfbot-field', String(key));
    out.push(Object.assign({
      key: String(key),
      type,
      label: cleanLabel(label) || el.name || 'field',
      name: el.name || '',
      tag: el.tagName.toLowerCase(),
    }, extra || {}));
    key += 1;
  };
  const seenRadio = new Set();
  for (const el of candidates) {
    if (!visible(el)) continue;
    // A disabled application form usually means Wellfound has already decided
    // this job cannot hire from the profile's location/timezone. Asking for
    // answers we cannot type would only waste the operator's time.
    if (el.disabled) continue;
    const type = (el.type || '').toLowerCase();
    const tag = el.tagName.toLowerCase();
    if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'search') continue;
    if (el.name === 'search') continue;
    const rawLabel = labelFor(el);
    if (!isRequired(el, rawLabel)) continue;
    if (isOptional(rawLabel)) continue;

    if (type === 'file') {
      add(el, type, rawLabel || 'resume upload');
      continue;
    }
    if (type === 'radio') {
      const group = el.name || ('id:' + (el.id || key));
      if (seenRadio.has(group)) continue;
      seenRadio.add(group);
      const options = el.name
        ? Array.from(document.querySelectorAll(
            'input[type="radio"][name="' + CSS.escape(el.name) + '"]'
          ))
        : [el];
      if (!options.some((option) => option.checked)) {
        add(el, type, groupLabel(el) || rawLabel, {
          name: el.name || '',
          options: options.map((option) => labelFor(option)).filter(Boolean),
        });
      }
      continue;
    }
    if (type === 'checkbox') {
      if (!el.checked) add(el, type, rawLabel);
      continue;
    }
    if (tag === 'select') {
      if (!(el.value || '').trim()) {
        add(el, 'select', rawLabel, {
          options: Array.from(el.options)
            .map((option) => (option.text || '').trim())
            .filter(Boolean),
        });
      }
      continue;
    }
    if (!(el.value || '').trim()) {
      const label = isCustom(el) && /customQuestionAnswers/i.test(rawLabel)
        ? groupLabel(el)
        : rawLabel;
      add(el, type || tag, label);
    }
  }
  const resumeHint = !!document.querySelector(
    '[data-test*="resume" i], [class*="resume" i], [class*="Resume"]'
  );
  return out.filter((item) => !(item.type === 'file' && resumeHint));
}
"""


class LoginRequired(RuntimeError):
    """No usable session and no way to finish login without the human."""


class Wellfound:
    def __init__(self, page: Page, cfg: Config, context: BrowserContext, notify, store=None) -> None:
        self.page = page
        self.context = context
        self.cfg = cfg
        self.notify = notify
        #: Optional Store, used for the answer bank and proof path persistence.
        self.store = store

    # ------------------------------------------------------------------
    # navigation + session
    # ------------------------------------------------------------------
    async def _goto(self, url: str, *, where: str) -> None:
        log.info("navigate [%s] %s", where, url)
        await self.page.goto(url, wait_until="domcontentloaded")
        await guards.maybe_resolve(self.page, self.cfg, self.notify, where)

    async def _body_text(self, limit: int = 6000) -> str:
        try:
            text = await self.page.locator("body").inner_text(timeout=5000)
        except PlaywrightError:
            return ""
        return (text or "")[:limit]

    async def is_logged_in(self) -> bool:
        """True when the current page shows a signed-in Wellfound session.

        Detection is navigation-based, not cookie-based: anonymous visitors get
        cookies whose names contain "session" (``rl_session``), so a cookie
        heuristic reports a session that does not exist. Signed-out pages -
        ``/jobs``, ``/role/r/...`` and job details alike - always render
        ``Log In`` / ``Sign Up`` controls in the nav.
        """
        try:
            if "wellfound.com" not in urlparse(self.page.url).netloc:
                await self._goto(f"{self.cfg.base_url}/jobs", where="session check")
            # Auth screens never count as a sign-in, even though their submit
            # control is an <input> rather than a "Log In" button.
            if urlparse(self.page.url).path.startswith(("/login", "/signup", "/auth")):
                return False
            for marker in SIGNED_OUT_MARKERS:
                locator = self.page.locator(marker)
                count = await locator.count()
                for index in range(count):
                    if await locator.nth(index).is_visible():
                        log.debug("signed-out marker present: %s", marker)
                        return False
        except PlaywrightError:
            return False
        return True

    async def _tell(self, text: str) -> None:
        """Best-effort operator message (probe's console notifier has no send)."""
        send = getattr(self.notify, "send", None)
        if send is not None:
            await send(text)

    async def ensure_logged_in(self, force: bool = False) -> None:
        """Reuse the stored session; fall back to credential login; else ask."""
        if not force and await self.is_logged_in():
            log.info("existing Wellfound session found - skipping login")
            return
        if not (self.cfg.wf_email and self.cfg.wf_password):
            await self._tell(
                "\U0001f510 <b>Wellfound sign-in needed</b>\n"
                "There is no usable stored session and WF_EMAIL/WF_PASSWORD are "
                "not set. Sign in in the open browser window within 5 minutes, "
                "or run <code>python setup_wizard.py --login</code> and restart "
                "the bot."
            )
            await self._wait_for_manual_login(seconds=300)
            return
        await self._login()

    async def _wait_for_manual_login(self, seconds: int) -> None:
        """Give the operator time to sign in by hand in the headed window."""
        await self.page.goto(f"{self.cfg.base_url}/login", wait_until="domcontentloaded")
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            await human.pause(2.0, 4.0)
            if await self.is_logged_in():
                await self._tell("\u2705 Signed in to Wellfound - continuing.")
                return
        raise LoginRequired(f"manual login not completed within {seconds}s")

    async def _login(self) -> None:
        cfg = self.cfg
        await self._goto(f"{cfg.base_url}/login", where="login page")
        log.info("attempting credential login for %s", cfg.wf_email)

        email_field = self.page.locator('input[type="email"], input[name*="email" i]').first
        try:
            await email_field.wait_for(state="visible", timeout=8000)
        except PlaywrightTimeout:
            if await guards.challenge_present(self.page):
                await guards.resolve_challenge(self.page, cfg, self.notify, "login page")
                return await self._login()
            await self._tell(
                "\U0001f510 <b>Wellfound wants a manual sign-in</b>\n"
                "The login page has no email/password form (it may only offer "
                "Google or an email code). Finish signing in in the open browser "
                "window - I will wait 5 minutes."
            )
            await self._wait_for_manual_login(seconds=300)
            return

        await human.type_like_human(email_field, cfg.wf_email)
        await human.pause(0.4, 1.0)

        password_field = self.page.locator('input[type="password"]').first
        has_password = await password_field.count() > 0
        if has_password:
            await human.type_like_human(password_field, cfg.wf_password)
            await human.pause(0.3, 0.8)

        submit = self.page.get_by_role(
            "button", name=re.compile(r"log in|sign in|continue|next", re.IGNORECASE)
        ).first
        if await submit.count():
            await human.click_like_human(self.page, submit)
        elif has_password:
            await password_field.press("Enter")

        await human.pause(1.5, 3.0)
        await guards.maybe_resolve(self.page, cfg, self.notify, "after login submit")

        body = await self._body_text()
        if LOGIN_ERROR_RE.search(body):
            raise LoginRequired("Wellfound rejected the configured credentials")

        deadline = asyncio.get_running_loop().time() + 25
        while asyncio.get_running_loop().time() < deadline:
            if await self.is_logged_in():
                log.info("login succeeded")
                return
            if MAGIC_LINK_RE.search(await self._body_text()):
                await self._tell(
                    "\U0001f510 <b>Wellfound sent a login code</b>\n"
                    "Complete the email code / magic link in the open browser "
                    "window - I will wait 5 minutes."
                )
                await self._wait_for_manual_login(seconds=300)
                return
            await human.pause(1.0, 2.0)

        raise LoginRequired("the login form did not produce a signed-in session")

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------
    async def search_internships(self) -> list[Job]:
        """One signed-in search for every fresh internship, newest first.

        Wellfound's public role pages are ranked for full-time roles and expose
        almost no internships. The signed-in ``/jobs`` search has a real
        ``jobType=internship`` filter, which is the only reliable feed. The
        search is intentionally broad (no keyword), then the configured role
        matchers decide which titles belong to us.

        The active search is cleared and rebuilt on every run, so a stale saved
        search (for example a narrow "web devloper" keyword) cannot hide new
        internships. Location is still enforced by Wellfound from the profile's
        location (Gurgaon), which covers remote roles that accept India plus
        onsite roles in Gurugram.
        """
        if self.cfg.search_url_template:
            return []

        await self._goto(f"{self.cfg.base_url}/jobs", where="daily internship search")
        if urlparse(self.page.url).path.startswith(("/login", "/signup", "/auth")):
            await self.ensure_logged_in()
            await self._goto(
                f"{self.cfg.base_url}/jobs", where="daily internship search after login"
            )
        if not await self.is_logged_in():
            raise LoginRequired("the internship search needs a signed-in session")

        try:
            await self.page.wait_for_selector('[data-test="SearchBar"]', timeout=15_000)
        except PlaywrightTimeout:
            log.warning("search bar did not render before filters")
        await human.pause(1.0, 1.8)

        # Start from a blank role/keyword search. The saved search's title chip
        # is a react-select; Backspace removes its selected values and leaves
        # the profile's location rule in force.
        await self._clear_search_roles()

        await self._open_and_apply_search_filters()
        await self._sort_search_by_newest()
        await self.page.evaluate("window.scrollTo(0, 0)")
        await human.pause(1.0, 2.0)

        try:
            await self.page.wait_for_selector(
                '[data-test="StartupResult"]', timeout=12_000
            )
        except PlaywrightTimeout:
            log.info("internship search rendered no startup cards")

        raw = await self._extract_search_rows()
        seen: set[str] = set()
        jobs: list[Job] = []
        for role in self.cfg.roles:
            jobs.extend(self._to_jobs(raw, role, seen))
        log.info(
            "signed-in internship search -> %d cards, %d matching/fresh listing(s)",
            len(raw),
            len(jobs),
        )
        return jobs

    async def _clear_search_roles(self) -> None:
        """Remove saved role/keyword chips so every internship can appear.

        The title chip is a react-select. Clicking it opens a focused input;
        each Backspace removes one selected value. The chip disappears as soon
        as the first value is removed, so using ``chip.count()`` as the loop
        condition leaves quoted keyword tokens behind. Keep pressing while the
        select input still owns focus instead.
        """
        chip = self.page.locator('[data-test="SearchBar-RoleSelect-FocusButton"]')
        if not await chip.count():
            return
        try:
            await chip.first.click()
            await human.pause(0.8, 1.5)
            for _ in range(10):
                if not await self._select_input_focused():
                    break
                await self.page.keyboard.press("Backspace")
                await human.pause(0.35, 0.7)
            await self.page.keyboard.press("Escape")
            await human.pause(1.5, 2.5)
        except PlaywrightError as exc:
            log.warning("could not clear saved search roles: %s", exc)
            return

        chip = self.page.locator('[data-test="SearchBar-RoleSelect-FocusButton"]')
        if await chip.count():
            text = " ".join((await chip.first.inner_text()).split())
            if text and text.lower() not in {"add a job title", "add a role"}:
                log.warning("saved search title chip still reads %r", text)

    async def _select_input_focused(self) -> bool:
        """True when the role combobox input owns keyboard focus."""
        try:
            return bool(
                await self.page.evaluate(
                    """() => {
                        const el = document.activeElement;
                        return !!el && el.tagName === 'INPUT' &&
                            (el.getAttribute('role') === 'combobox' ||
                             el.getAttribute('aria-autocomplete') === 'list');
                    }"""
                )
            )
        except PlaywrightError:
            return False

    async def _extract_search_rows(self) -> list[dict]:
        """Extract cards while gently scrolling, de-duplicated by URL.

        Newest-first ordering means the 24-hour window is at the top, but the
        page lazy-renders a handful of cards at a time. A dozen short scrolls
        covers a normal day's internships without walking the whole result set.
        """
        rows: dict[str, dict] = {}
        stalled = 0
        for _ in range(12):
            current = await self.page.evaluate(EXTRACT_SEARCH_ROWS_JS)
            before = len(rows)
            for row in current:
                href = str(row.get("href") or "")
                if href:
                    rows.setdefault(href, row)
            stalled = stalled + 1 if len(rows) == before else 0
            if stalled >= 3:
                break
            # window.scrollBy works even when the pointer is over the header,
            # where mouse.wheel would scroll the wrong element.
            await self.page.evaluate("window.scrollBy(0, 1800)")
            await human.pause(1.0, 1.8)
        return list(rows.values())

    async def _open_and_apply_search_filters(self) -> None:
        """Make the result set exactly Internship + 1-10 employees."""
        if await self._search_filters_active():
            return

        filters = self.page.locator(
            '[data-test="SearchBar-ToggleFilterControlPanelButton"]'
        )
        try:
            await filters.first.wait_for(state="visible", timeout=15_000)
        except PlaywrightTimeout:
            filters = self.page.get_by_role("button", name="Filters", exact=True)
            try:
                await filters.first.wait_for(state="visible", timeout=5_000)
            except PlaywrightTimeout:
                raise RuntimeError("Wellfound search has no Filters button") from None

        internship = self.page.locator(
            'label[for="form-input--jobTypes--internship"]'
        )
        drawer_open = await internship.count() and await internship.first.is_visible()
        for _ in range(2):
            if drawer_open:
                break
            try:
                await filters.first.click()
                await human.pause(1.5, 2.5)
            except PlaywrightError as exc:
                log.warning("could not open search filters: %s", exc)
                break
            drawer_open = await internship.count() and await internship.first.is_visible()
        if not drawer_open:
            raise RuntimeError("Wellfound search filter drawer did not open")

        # Give React a moment to hydrate the checkbox state before touching it.
        await human.pause(1.2, 2.0)
        await self._uncheck_other_options(
            "form-input--jobTypes--", "form-input--jobTypes--internship"
        )
        await self._uncheck_other_options(
            "form-input--companySizes--", "form-input--companySizes--SIZE_1_10"
        )
        self._require_checked(
            await self._check_search_filter("form-input--jobTypes--internship"),
            "Internship",
        )
        self._require_checked(
            await self._check_search_filter("form-input--companySizes--SIZE_1_10"),
            "1-10 employees",
        )

        view = self.page.get_by_role("button", name="View results", exact=False)
        if not await view.count():
            raise RuntimeError("search filters have no View results button")
        try:
            await view.first.click()
            await human.pause(3.0, 5.0)
        except PlaywrightError as exc:
            raise RuntimeError(f"could not apply search filters: {exc}") from exc

        if not await self._search_filters_active():
            raise RuntimeError("search filters did not stick after View results")

    async def _search_filters_active(self) -> bool:
        """True only when Internship and *just* 1-10 employees are active."""
        labels = await self.page.evaluate(
            """() => Array.from(document.querySelectorAll(
                 '[data-test^="ActiveFilterSummary-FilterButton--"]'
               )).map((el) => el.getAttribute('aria-label') || '')"""
        )
        # The filter drawer repeats the active-filter summary, so compare a set
        # rather than a list and ignore duplicates.
        size_labels = {
            label for label in labels if "employees" in label.lower()
        }
        return (
            "Clear Internship" in labels
            and size_labels == {"Clear 1-10 employees"}
        )

    async def _uncheck_other_options(self, prefix: str, keep_id: str) -> None:
        """Uncheck every filter input in a group except ``keep_id``."""
        inputs = self.page.locator(f'input[id^="{prefix}"]')
        for index in range(await inputs.count()):
            checkbox = inputs.nth(index)
            filter_id = await checkbox.get_attribute("id") or ""
            if filter_id == keep_id:
                continue
            try:
                if await checkbox.is_checked():
                    label = self.page.locator(f'label[for="{filter_id}"]')
                    if await label.count():
                        # DOM click bypasses the sticky footer that can cover a
                        # label lower down in the filter drawer.
                        await label.first.evaluate("el => el.click()")
                        await human.pause(0.5, 0.9)
            except PlaywrightError:
                continue

    @staticmethod
    def _require_checked(ok: bool, label: str) -> None:
        if not ok:
            raise RuntimeError(f"could not select the {label} search filter")

    async def _check_search_filter(self, filter_id: str) -> bool:
        label = self.page.locator(f'label[for="{filter_id}"]')
        if not await label.count():
            log.warning("search filter %s not found", filter_id)
            return False
        checkbox = self.page.locator(f'input[id="{filter_id}"]')
        try:
            # Reading the checkbox a beat after opening the drawer avoids a
            # hydration race where React briefly reports the old state.
            await human.pause(0.5, 0.9)
            await checkbox.first.wait_for(state="attached", timeout=5_000)
            if await checkbox.first.is_checked():
                log.info("search filter already on: %s", filter_id)
                return True
            for attempt in range(2):
                log.info("switching on search filter: %s (attempt %d)", filter_id, attempt + 1)
                # A DOM click bypasses the sticky footer that can cover labels
                # lower down in the filter drawer.
                await label.first.evaluate("el => el.click()")
                await human.pause(0.9, 1.4)
                if await checkbox.first.is_checked():
                    log.info("search filter %s checked=True", filter_id)
                    return True
            log.warning("search filter %s did not switch on", filter_id)
            return False
        except PlaywrightError as exc:
            log.warning("could not set search filter %s: %s", filter_id, exc)
            return False

    async def _sort_search_by_newest(self) -> None:
        """Choose the \"Most recent\" sort order (default is Recommended)."""
        current = self.page.get_by_role("button", name="Most recent", exact=True)
        if await current.count():
            return
        recommended = self.page.get_by_role("button", name="Recommended", exact=True)
        if not await recommended.count():
            log.debug("search sort control not found")
            return
        try:
            await recommended.first.click()
            await human.pause(0.4, 0.9)
            option = self.page.get_by_text("Most recent", exact=True)
            if await option.count():
                await option.first.click()
                await human.pause(2.0, 4.0)
        except PlaywrightError as exc:
            log.warning("could not switch to newest-first: %s", exc)

    async def search(self, role: Role) -> list[Job]:
        """Fallback listings for one role from public role pages.

        Only used when the signed-in ``/jobs`` search errors. It walks the
        role's public ``/role/r/<slug>`` pages and their ``?page=``
        continuation; those pages need no session, so discovery keeps working
        when the stored login expires - only the application step needs it.
        """
        jobs: list[Job] = []
        seen: set[str] = set()
        for start_url in self.cfg.search_urls(role):
            jobs.extend(await self._scan_pages(start_url, role, seen))
        log.info("query %r -> %d matching listings", role.query, len(jobs))
        return jobs

    async def search_locations(self) -> list[Job]:
        """Onsite listings from the public city pages, matched to any role.

        The role pages are remote-filtered by Wellfound, so an onsite job in
        Gurugram only appears here. Each city page is read once per run and the
        rows are matched against every configured role.
        """
        jobs: list[Job] = []
        seen: set[str] = set()
        for start_url in self.cfg.location_pages:
            jobs.extend(await self._scan_location_pages(start_url, seen))
        log.info("location pages -> %d matching listings", len(jobs))
        return jobs

    async def _scan_location_pages(self, start_url: str, seen: set[str]) -> list[Job]:
        jobs: list[Job] = []
        visited: set[str] = set()
        empty_streak = 0
        page_number = 0
        url = start_url
        while url and url not in visited and page_number < self.cfg.max_pages_per_role:
            if page_number:
                await human.pause(2.0, 5.0)
            visited.add(url)
            page_number += 1
            await self._goto(url, where=f"location p{page_number}")

            try:
                await self.page.wait_for_selector('a[href^="/jobs/"]', timeout=15_000)
            except PlaywrightTimeout:
                log.warning("no job links rendered at %s", url)

            await human.scroll_gently(self.page, self.cfg.scroll_rounds)
            await human.pause(0.4, 1.0)
            raw = await self.page.eval_on_selector_all('a[href^="/jobs/"]', EXTRACT_ROWS_JS)

            found: list[Job] = []
            for role in self.cfg.roles:
                found.extend(self._to_jobs(raw, role, seen))
            jobs.extend(found)
            empty_streak = 0 if found else empty_streak + 1
            if empty_streak >= 2:
                log.info("%s: two pages without a match - moving on", start_url)
                break
            url = await self._next_page_url()
        return jobs

    async def _scan_pages(self, start_url: str, role: Role, seen: set[str]) -> list[Job]:
        """Walk ``start_url`` and its "Next page" link, up to the configured cap."""
        jobs: list[Job] = []
        visited: set[str] = set()
        empty_streak = 0
        page_number = 0
        url = start_url
        while url and url not in visited and page_number < self.cfg.max_pages_per_role:
            if page_number:
                await human.pause(2.0, 5.0)  # same rhythm as a person clicking through
            visited.add(url)
            page_number += 1
            await self._goto(url, where=f"{role.label} p{page_number}")

            try:
                await self.page.wait_for_selector('a[href^="/jobs/"]', timeout=15_000)
            except PlaywrightTimeout:
                log.warning("no job links rendered at %s", url)

            await human.scroll_gently(self.page, self.cfg.scroll_rounds)
            await human.pause(0.4, 1.0)

            raw = await self.page.eval_on_selector_all('a[href^="/jobs/"]', EXTRACT_ROWS_JS)
            found = self._to_jobs(raw, role, seen)
            jobs.extend(found)
            empty_streak = 0 if found else empty_streak + 1
            if empty_streak >= 2:
                log.info("%s: two pages without a match - moving on", start_url)
                break
            url = await self._next_page_url()
        return jobs

    async def _next_page_url(self) -> str:
        """Absolute URL of the listing's "Next page" control, or ""."""
        try:
            link = self.page.locator('a[aria-label*="next page" i]')
            if not await link.count():
                return ""
            href = (await link.first.get_attribute("href") or "").strip()
        except PlaywrightError:
            return ""
        if not href:
            return ""
        return href if href.startswith("http") else f"{self.cfg.base_url}{href}"

    def _to_jobs(self, raw: list[dict], role: Role, seen: set[str]) -> list[Job]:
        """Turn extracted rows into role-matching, de-duplicated jobs.

        The listing card already carries the job type, salary and posting age,
        so obviously stale or unpaid listings are dropped here - before a detail
        page is opened. Anything the card does not state stays permissive and is
        checked again on the detail page.
        """
        jobs: list[Job] = []
        for row in raw:
            job = Job.from_href(row.get("href", ""))
            if job is None or job.job_id in seen:
                continue
            title = row.get("title") or ""
            if not title or not JOB_TITLE_OK(title):
                continue
            if CLONE_RE.search(title) or CLONE_RE.search(job.slug):
                log.debug("skip (duplicate 'clone' posting): %s", title)
                continue
            job.title = title
            job.company = (row.get("company") or "").strip()
            job.meta = (row.get("meta") or "").strip()
            card_size = parse_company_size(row.get("company_size") or "")
            if card_size is not None:
                job.company_size, job.company_size_min, job.company_size_max = card_size
                _display, low, high = card_size
                preferred = (
                    low >= self.cfg.company_size_min
                    and high <= self.cfg.company_size_max
                )
                fallback = (
                    self.cfg.allow_size_fallback
                    and high <= self.cfg.company_size_fallback_max
                )
                if not preferred and not fallback:
                    log.debug(
                        "skip (card company size %s): %s", _display, title
                    )
                    continue
            job.role_key = role.key
            job.query = role.query
            job.has_inline_apply = bool(row.get("has_apply")) and not row.get("apply_disabled")
            job.job_type = parse_job_type(job.meta)
            job.stipend = parse_stipend(job.meta)
            card_mode, card_area = parse_card_location(job.meta)
            if card_mode:
                job.remote = card_mode.lower().startswith("remote")
                if card_area:
                    job.location = card_area
            card_age = parse_card_age(job.meta)
            if card_age is not None:
                job.posted_days = card_age
            if not role.matches(job.title, self.cfg.negative_keywords):
                log.debug("skip (role mismatch): %s", job.title)
                continue
            if self.cfg.skip_unpaid and is_unpaid(job.title, job.meta):
                log.info("skip (unpaid/commission-only): %s", job.title)
                continue
            loc_verdict = card_location_verdict(job.meta)
            if loc_verdict is not None:
                log.info(
                    "skip (onsite outside Gurugram): %s (%s, %s)",
                    job.title,
                    loc_verdict[0],
                    loc_verdict[1],
                )
                continue
            # Unknown card age stays permissive here; the detail page is the
            # authoritative stamp. A *known* stale card is dropped immediately.
            age_verdict = self._age_verdict(job, require_known=False)
            if age_verdict is not None:
                log.info(
                    "skip (stale listing card): %s (%s)",
                    job.title,
                    age_verdict[1],
                )
                continue
            seen.add(job.job_id)
            jobs.append(job)
        return jobs

    # ------------------------------------------------------------------
    # application
    # ------------------------------------------------------------------
    async def sync_applications(self) -> dict[str, int]:
        """Import manual applications and refresh response statuses.

        Reads both the Ongoing and Archived tabs of Wellfound's
        ``/jobs/applications`` history. Jobs the bot has never seen are inserted
        as ``already_applied`` so it will not duplicate a manual application;
        existing rows only get their ``response`` field refreshed (Pending /
        Accepted / Not Accepted). The date shown on each card is preserved as
        the application date, so an old archived application does not trigger a
        fresh company cooldown.
        """
        result = {"found": 0, "imported": 0, "updated": 0}
        if self.store is None:
            return result
        seen: set[str] = set()
        await self._sync_application_page(APPLICATIONS_URL, "ongoing", seen, result)
        await self._sync_application_page(
            ARCHIVED_APPLICATIONS_URL, "archived", seen, result
        )
        log.info(
            "applications pages: %d found, %d imported, %d history row(s) updated",
            result["found"], result["imported"], result["updated"],
        )
        return result

    async def _sync_application_page(
        self,
        url: str,
        label: str,
        seen: set[str],
        result: dict[str, int],
    ) -> None:
        await self._goto(url, where=f"applications page ({label})")
        if urlparse(self.page.url).path.startswith(("/login", "/signup", "/auth")):
            await self.ensure_logged_in()
            await self._goto(url, where=f"applications page ({label}) after login")
        try:
            await self.page.wait_for_selector(
                'a[href^="/jobs/applications/"]:not([href$="/archived"])',
                timeout=10000,
            )
        except PlaywrightTimeout:
            log.info("no application rows on the %s page", label)
            return
        try:
            rows = await self.page.eval_on_selector_all(
                'a[href^="/jobs/applications/"]:not([href$="/archived"])',
                EXTRACT_APPLICATIONS_JS,
            )
        except PlaywrightError:
            log.exception("could not read the %s applications page", label)
            return

        for row in rows:
            href = str(row.get("href") or "")
            lines = [
                line.strip()
                for line in str(row.get("text") or "").splitlines()
                if line.strip()
            ]
            company = lines[0] if lines else ""
            title = lines[1] if len(lines) > 1 else ""
            tail = " ".join(lines[2:]) if len(lines) > 2 else ""
            job = job_from_application_href(href, title)
            if job is None or job.job_id in seen:
                continue
            seen.add(job.job_id)
            if company and len(lines) > 1:
                job.company = company
            if not job.title:
                job.title = job.slug
            response = parse_response(tail)
            applied_at = parse_application_date(tail)
            for role in self.cfg.roles:
                if role.matches(job.title, self.cfg.negative_keywords):
                    job.role_key = role.key
                    break
            imported, updated = self.store.sync_application(
                job,
                status=Status.ALREADY_APPLIED,
                detail=(
                    f"found on /jobs/applications ({label}; "
                    f"{response or 'status unknown'})"
                ),
                response=response,
                applied_at=applied_at,
            )
            result["found"] += 1
            result["imported"] += int(imported)
            result["updated"] += int(updated)

    async def profile_warnings(self) -> list[str]:
        """Read the profile page and return conservative completeness warnings.

        These never block an application; they are a heads-up at startup so a
        missing resume or empty headline does not silently weaken every form.
        """
        await self._goto(PROFILE_URL, where="profile preflight")
        body = await self._body_text(limit=12000)
        return [
            message
            for pattern, message in PROFILE_WARNING_PATTERNS
            if pattern.search(body)
        ]

    async def apply(self, job: Job, role: Role, *, allow_fallback: bool = False) -> Outcome:
        started = utcnow()
        page = self.page

        try:
            await self._goto(job.url, where=f"open: {job.title}")
            if urlparse(page.url).path.startswith("/login") or not await self.is_logged_in():
                # Session expired mid-run (or was never there): re-authenticate
                # before touching anything - a signed-out visitor is shown a
                # sign-up form where the application form should be.
                await self.ensure_logged_in()
                await self._goto(job.url, where="reopen after login")

            await self._enrich(job)
            if not role.matches(job.title, self.cfg.negative_keywords):
                return self._finish(job, Status.SKIPPED_ROLE, "title does not match role", started)
            company_verdict = self._company_verdict(job)
            if company_verdict is not None:
                status, detail = company_verdict
                return self._finish(job, status, detail, started)

            body = await self._body_text()
            if CLOSED_RE.search(body):
                return self._finish(job, Status.SKIPPED_CLOSED, "listing is closed", started)
            if ALREADY_RE.search(body) or await self._applied_state_visible():
                return self._finish(job, Status.ALREADY_APPLIED, "already in your history", started)

            # Freshness first: a listing that has been sitting for weeks is not
            # worth one of the few application slots, even if it just showed up
            # in the search results for the first time.
            detail_age = parse_posted_age(body)
            if detail_age is not None:
                job.posted_days = detail_age
                log.info("posted %s ago: %s", format_age(job.posted_days), job.title)
            elif job.posted_days is not None:
                log.info(
                    "detail had no age stamp; using card age %s: %s",
                    format_age(job.posted_days),
                    job.title,
                )
            age_verdict = self._age_verdict(job)
            if age_verdict is not None:
                status, detail = age_verdict
                return self._finish(job, status, detail, started)

            job.location, hires_remote = parse_location(body)
            job.remote = self._is_remote(job.location, hires_remote)
            location_verdict = self._location_verdict(job)
            if location_verdict is not None:
                status, detail = location_verdict
                return self._finish(job, status, detail, started)

            size = parse_company_size(body)
            if size is not None:
                job.company_size, job.company_size_min, job.company_size_max = size
                verdict = self._size_verdict(size, allow_fallback=allow_fallback)
                if verdict is not None:
                    status, detail = verdict
                    return self._finish(job, status, detail, started)

            control, kind = await self._find_apply_control()
            if control is None:
                # No Apply button can also mean *we already applied* (the button
                # is replaced by an "Applied" state), which is not a failure.
                if await self._applied_state_visible():
                    return self._finish(
                        job, Status.ALREADY_APPLIED, "listing already shows Applied", started
                    )
                external = await self._find_external_apply()
                if external:
                    return self._finish(
                        job, Status.EXTERNAL, f"apply continues off-platform ({external})", started
                    )
                if kind == "disabled":
                    return self._finish(
                        job, Status.SKIPPED_CLOSED, "apply control is disabled (listing closed?)", started
                    )
                return self._finish(job, Status.FAILED, "no apply control found on page", started)

            if kind == "external":
                target = await self._click_external(control)
                return self._finish(
                    job, Status.EXTERNAL, f"apply continues off-platform ({target})", started
                )

            tabs_before = len(self.context.pages)
            await human.click_like_human(page, control)
            await human.pause(1.2, 2.6)
            await guards.maybe_resolve(page, self.cfg, self.notify, f"apply: {job.title}")

            # Did the click open a new tab (external ATS) or navigate away?
            external_tab = await self._drain_external_tab(tabs_before)
            if external_tab:
                return self._finish(
                    job, Status.EXTERNAL, f"apply continues off-platform ({external_tab})", started
                )
            external_url = await self._drained_external_url()
            if external_url:
                return self._finish(
                    job, Status.EXTERNAL, f"apply continues off-platform ({external_url})", started
                )

            if not await self.is_logged_in():
                # Wellfound answered the apply click with a sign-up wall
                # ("Complete the fields below or Log in with your account to
                # apply"). Filling it would create a second account, so stop.
                raise LoginRequired("Wellfound asked for a sign-in before the application form")

            status, detail = await self._complete_form(job)
            if status == Status.APPLIED:
                await self._capture_proof(job)
            return self._finish(job, status, detail, started)

        except guards.ChallengeTimeout as exc:
            raise exc  # the scheduler handles pausing
        except PlaywrightError as exc:
            log.exception("automation error on %s", job.url)
            return self._finish(job, Status.FAILED, f"{type(exc).__name__}: {exc}"[:180], started)

    def _company_verdict(self, job: Job) -> tuple[str, str] | None:
        """Skip another role at a company we applied to recently.

        The company name is only reliable after ``_enrich`` has read the detail
        page. A cooldown of 0 disables the check; an unknown company stays
        permissive rather than silently dropping a listing.
        """
        days = self.cfg.company_cooldown_days
        if days <= 0 or self.store is None or not (job.company or "").strip():
            return None
        try:
            if self.store.company_recent(job.company, days):
                return (
                    Status.SKIPPED_COMPANY,
                    f"already applied to {job.company} in the last {days} days",
                )
        except Exception:  # noqa: BLE001 - a DB hiccup must not stop an application
            log.exception("company cooldown check failed")
        return None

    def _age_verdict(
        self, job: Job, *, require_known: bool | None = None
    ) -> tuple[str, str] | None:
        """Skip a listing outside the freshness window.

        ``require_known`` defaults to ``WF_REQUIRE_POSTED_AGE``. Card discovery
        passes ``False`` so an unreadable card stamp can still be checked on the
        detail page; the detail-page gate then passes ``True`` for the strict
        24-hour search. The comparison is inclusive at the boundary: a listing
        shown as "1 day ago" is rejected for a 24-hour window because it may be
        nearly 48 hours old.
        """
        max_days = self.cfg.max_job_age_days
        if max_days <= 0:
            return None
        strict = self.cfg.require_posted_age if require_known is None else require_known
        limit = f"{max_days * 24:g}h" if max_days <= 1 else f"{max_days:g} days"
        if job.posted_days is None:
            if strict:
                return (
                    Status.SKIPPED_AGE,
                    f"posting age unknown (limit {limit})",
                )
            return None
        if job.posted_days >= max_days:
            return (
                Status.SKIPPED_AGE,
                f"posted {format_age(job.posted_days)} ago (limit {limit})",
            )
        return None

    def _size_verdict(
        self, size: tuple[str, int, int], *, allow_fallback: bool
    ) -> tuple[str, str] | None:
        """Apply / defer / skip a listing based on its company head-count.

        Preferred band comes from ``WF_COMPANY_SIZE_MIN/MAX`` (default 1-10).
        The ``WF_COMPANY_SIZE_FALLBACK_MAX`` band (default 11-50) is only used
        after the run has tried every smaller company and applied to none.
        ``None`` means "carry on and apply".
        """
        cfg = self.cfg
        display, low, high = size
        if low >= cfg.company_size_min and high <= cfg.company_size_max:
            return None
        if high <= cfg.company_size_fallback_max:
            if not cfg.allow_size_fallback:
                return (
                    Status.SKIPPED_SIZE,
                    f"company size {display} outside {cfg.company_size_min}-{cfg.company_size_max}",
                )
            if allow_fallback:
                return None
            return (
                Status.DEFERRED_SIZE,
                f"company size {display}; waiting for a {cfg.company_size_min}-{cfg.company_size_max} company",
            )
        return (
            Status.SKIPPED_SIZE,
            f"company size {display} above the {cfg.company_size_fallback_max} limit",
        )

    def _is_remote(self, location: str, hires_remote: str) -> bool:
        """Decide whether a listing is remote work we are allowed to take.

        Wellfound says either ``Remote Work Policy: Remote only`` or
        ``Hires remotely in <region>``. When the region is named we honour it
        (``WF_ACCEPT_REMOTE_REGIONS``): a "remote" role that only hires in the
        United States is not useful from India. When no region is named but the
        page says remote, we take it at face value.
        """
        if not self.cfg.accept_remote:
            return False
        if hires_remote:
            lowered = hires_remote.lower()
            return any(
                region.lower() in lowered for region in self.cfg.accept_remote_regions
            )
        return re.search(r"\bremote\b", location, re.IGNORECASE) is not None

    def _location_verdict(self, job: Job) -> tuple[str, str] | None:
        """Skip onsite roles outside the accepted cities; ``None`` = apply."""
        if job.remote:
            return None
        text = job.location.lower()
        for city in self.cfg.accept_locations:
            if city.lower() in text:
                return None
        if not job.location:
            # No location section rendered at all: do not throw the listing away.
            return None
        accepted = ", ".join(self.cfg.accept_locations)
        return (
            Status.SKIPPED_LOCATION,
            f"onsite in {job.location}; not remote and not in {accepted}",
        )

    async def _enrich(self, job: Job) -> None:
        """Pull canonical title/company from the detail page."""
        try:
            heading = self.page.locator("h1").first
            if await heading.count():
                text = (await heading.inner_text(timeout=3000)).strip()
                if text:
                    job.title = text.split("\n")[0].strip()
        except PlaywrightError:
            pass
        if not job.company:
            try:
                link = self.page.locator('a[href*="/company/"]').first
                if await link.count():
                    text = (await link.inner_text(timeout=3000)).strip()
                    if text:
                        job.company = text
            except PlaywrightError:
                pass
        if not job.company and " at " in job.title:
            # Detail pages often render the heading as "Role at Company".
            job.company = job.title.rsplit(" at ", 1)[1].strip()

    async def _applied_state_visible(self) -> bool:
        """True when the page now shows the "✓ Applied" badge/control.

        Wellfound does not always print a confirmation sentence; on success the
        Apply button is simply replaced by an "Applied" state. Checking that is
        what turns a real submission from UNVERIFIED into APPLIED.
        """
        page = self.page
        # Only a real button counts: the signed-in nav also contains an
        # "Applied" link (data-test="ItemApplied" -> /jobs/applications), and
        # matching that would mark every visited listing as applied.
        for locator in (
            page.get_by_role("button", name=APPLIED_STATE_RE),
            page.locator('button[data-test*="Applied" i]'),
        ):
            try:
                count = await locator.count()
                for index in range(min(count, 5)):
                    if await locator.nth(index).is_visible():
                        return True
            except PlaywrightError:
                continue
        return False

    async def _find_apply_control(self):
        """Return (locator, kind) with kind ``internal``/``external``, or (None, reason)."""
        page = self.page
        candidates = (
            # The detail page's own Apply button carries a hand-written onclick
            # that opens the application modal - unambiguous, so try it first.
            page.locator('button[onclick*="autoOpenApplication"]').first,
            page.locator('[data-test="JobApplicationApplyButton"]').first,
            page.get_by_role("button", name=APPLY_NAME_RE).first,
            page.get_by_role("link", name=APPLY_NAME_RE).first,
        )
        for locator in candidates:
            try:
                if await locator.count() == 0:
                    continue
                if not await locator.is_visible():
                    continue
                if await locator.is_disabled():
                    log.info("apply control present but disabled")
                    return None, "disabled"
                href = ""
                try:
                    href = (await locator.get_attribute("href")) or ""
                except PlaywrightError:
                    pass
                if href.startswith("http") and "wellfound.com" not in href:
                    return locator, "external"
                return locator, "internal"
            except PlaywrightError:
                continue
        return None, "none"

    async def _find_external_apply(self) -> str:
        """Some listings only link out to the company's ATS."""
        try:
            links = self.page.locator("a[href^='http']")
            count = await links.count()
            for index in range(min(count, 40)):
                link = links.nth(index)
                try:
                    text = (await link.inner_text(timeout=1500)).strip()
                    href = await link.get_attribute("href") or ""
                except PlaywrightError:
                    continue
                if not href or "wellfound.com" in href:
                    continue
                if EXTERNAL_APPLY_RE.search(text) or APPLY_NAME_RE.match(text):
                    return urlparse(href).netloc
        except PlaywrightError:
            pass
        return ""

    async def _click_external(self, locator) -> str:
        """Click an off-site apply link and report where it went."""
        page = self.page
        tabs_before = len(self.context.pages)
        await human.click_like_human(page, locator)
        await human.pause(1.2, 2.4)

        tab = await self._drain_external_tab(tabs_before)
        if tab:
            return tab
        current = urlparse(page.url)
        if current.netloc and "wellfound.com" not in current.netloc:
            return current.netloc
        return "external site"

    async def _drain_external_tab(self, tabs_before: int) -> str:
        """Close any tab opened after ``tabs_before``; return its host if off-site."""
        try:
            pages = self.context.pages
        except PlaywrightError:
            return ""
        if len(pages) <= tabs_before:
            return ""
        host = ""
        for extra in pages[tabs_before:]:
            try:
                url = extra.url
                await extra.close()
            except PlaywrightError:
                continue
            parsed = urlparse(url)
            if parsed.netloc and "wellfound.com" not in parsed.netloc:
                host = parsed.netloc
        return host

    async def _drained_external_url(self) -> str:
        """If the current tab left wellfound.com, report where it went."""
        current = urlparse(self.page.url)
        if current.netloc and "wellfound.com" not in current.netloc:
            return current.netloc
        return ""

    # -- form completion ------------------------------------------------
    async def _complete_form(self, job: Job) -> tuple[str, str]:
        page = self.page
        await human.pause(1.0, 2.2)

        body = await self._body_text()
        if CONFIRMATION_RE.search(body) or await self._applied_state_visible():
            return Status.APPLIED, "confirmation shown immediately"
        if LOCATION_MISMATCH_RE.search(body):
            # Wellfound's own modal says this role cannot hire from the
            # profile's location/timezone. Filling it would be a dead
            # application, no matter how good the listing looks.
            return (
                Status.SKIPPED_LOCATION,
                "job does not support your location/timezone",
            )

        await self._fill_cover_note()

        if self.cfg.ask_questions:
            # First fill everything the answer bank already knows (no Telegram
            # noise), then ask for the rest in a single batched message. Each
            # pass re-reads the form, because filling a field can re-render it.
            for _ in range(6):
                unanswered = await page.evaluate(UNANSWERED_JS)
                if not unanswered:
                    break

                banked = 0
                for field in unanswered:
                    answer = self._bank_lookup(field, job)
                    if answer is None:
                        continue
                    filled, _ = await self._fill_answer(field, answer)
                    if filled:
                        banked += 1
                if banked:
                    log.info("answer bank filled %d field(s) for %s", banked, job.title)
                    await human.pause(0.4, 1.0)
                    continue

                answers = await self._ask_fields(job, unanswered)
                if answers is None:
                    labels = ", ".join(
                        str(item.get("label", "?")) for item in unanswered[:4]
                    )
                    more = "" if len(unanswered) <= 4 else f" (+{len(unanswered) - 4} more)"
                    return Status.NEEDS_REVIEW, f"no complete reply for: {labels}{more}"
                if answers == "skip":
                    return Status.SKIPPED_MANUAL, "you chose to skip this application"
                for field, answer in zip(unanswered, answers):
                    filled, detail = await self._fill_answer(field, answer)
                    if not filled:
                        return Status.NEEDS_REVIEW, detail
                    self._bank_save(field, answer, job)
                await human.pause(0.5, 1.2)

        unanswered = await page.evaluate(UNANSWERED_JS)
        if unanswered:
            labels = ", ".join(str(item.get("label", "?")) for item in unanswered[:4])
            more = "" if len(unanswered) <= 4 else f" (+{len(unanswered) - 4} more)"
            return (
                Status.NEEDS_REVIEW,
                f"{len(unanswered)} required field(s) need a human answer: {labels}{more}",
            )

        submit = await self._find_submit()
        if submit is None:
            if await self._find_external_apply():
                return Status.EXTERNAL, "form hands off to an external ATS"
            return Status.NEEDS_REVIEW, "could not find a submit control"

        await human.click_like_human(page, submit)
        await human.pause(1.5, 3.0)
        await guards.maybe_resolve(page, self.cfg, self.notify, "submit")

        external = await self._drained_external_url()
        if external:
            return Status.EXTERNAL, f"submit redirected off-platform ({external})"

        result = await self._wait_for_confirmation()
        return result

    # -- answer bank ----------------------------------------------------
    @staticmethod
    def _mentions_company(label: str, job: Job) -> bool:
        """True when a question names this employer ("How did you discover X?")."""
        company = (getattr(job, "company", "") or "").strip().lower()
        if len(company) < 3:
            return False
        return re.search(
            rf"(?<![a-z0-9]){re.escape(company)}(?![a-z0-9])", label.lower()
        ) is not None

    def _bank_lookup(self, field: dict, job: Job) -> str | None:
        """Saved answer for this exact question, or ``None``."""
        if not self.cfg.answer_bank or self.store is None:
            return None
        label = str(field.get("label") or "").strip()
        if not label or JOB_SPECIFIC_RE.search(label):
            return None
        if self._mentions_company(label, job):
            return None
        # Consent/privacy checkboxes are a deliberate human decision every time.
        if (field.get("type") or "").lower() in {"file", "checkbox"}:
            return None
        try:
            return self.store.get_answer(answer_key(label))
        except Exception:  # noqa: BLE001 - a broken bank must not stop an application
            log.exception("answer bank lookup failed")
            return None

    def _bank_save(self, field: dict, answer: str, job: Job) -> None:
        """Remember a reusable answer so the next application does not ask."""
        if not self.cfg.answer_bank or self.store is None:
            return
        label = str(field.get("label") or "").strip()
        value = (answer or "").strip()
        if not label or not value or JOB_SPECIFIC_RE.search(label):
            return
        if self._mentions_company(label, job):
            return
        # Long, original prose is job-specific even when the label looks static.
        if len(value) > 240:
            return
        # Consent/privacy checkboxes are a deliberate human decision every time.
        if (field.get("type") or "").lower() in {"file", "checkbox"}:
            return
        try:
            self.store.save_answer(answer_key(label), label, value)
        except Exception:  # noqa: BLE001
            log.exception("answer bank save failed")

    @staticmethod
    def _is_free_text(field: dict) -> bool:
        """True for a long/original answer that should be asked on its own."""
        if (field.get("type") or "").lower() == "textarea":
            return True
        return JOB_SPECIFIC_RE.search(str(field.get("label") or "")) is not None

    @staticmethod
    def _is_skip(answer: str) -> bool:
        return (answer or "").strip().lower() in SKIP_WORDS

    async def _ask_fields(self, job: Job, fields: list[dict]) -> list[str] | str | None:
        """Ask for all remaining fields, batching only the short ones.

        Short questions (URLs, numbers, radios) go out as one numbered Telegram
        message. Long/free-text questions are asked one at a time so a
        multi-line answer is not mangled by the one-line-per-question parser.
        Returns answers in the same order as ``fields``, ``"skip"``, or ``None``.
        """
        if len(fields) == 1:
            answer = await self._ask_field(job, fields[0])
            if answer is None:
                return None
            return "skip" if self._is_skip(answer) else [answer]

        short = [field for field in fields if not self._is_free_text(field)]
        long = [field for field in fields if self._is_free_text(field)]
        answers: dict[int, str] = {}

        if len(short) == 1:
            answer = await self._ask_field(job, short[0])
            if answer is None:
                return None
            if self._is_skip(answer):
                return "skip"
            answers[id(short[0])] = answer
        elif short:
            batch = await self._ask_batch(job, short)
            if batch is None:
                return None
            if batch == "skip":
                return "skip"
            for field, answer in zip(short, batch):
                answers[id(field)] = answer

        for field in long:
            answer = await self._ask_field(job, field)
            if answer is None:
                return None
            if self._is_skip(answer):
                return "skip"
            answers[id(field)] = answer

        return [answers[id(field)] for field in fields]

    async def _ask_batch(self, job: Job, fields: list[dict]) -> list[str] | str | None:
        """One numbered Telegram message for several short questions."""
        ask = getattr(self.notify, "ask", None)
        if ask is None:
            return None
        lines = [
            "\u2753 <b>Your answers are needed</b>",
            f"<b>{html.escape(job.company or 'Unknown company')}</b> \u2014 "
            f"<i>{html.escape(display_title(job.title, job.company))}</i>",
            html.escape(job.url),
            "",
            f"Reply in <b>one message</b> with {len(fields)} line(s), "
            "one answer per line (or tap <b>Skip application</b>):",
        ]
        for index, field in enumerate(fields, start=1):
            label = str(field.get("label") or field.get("name") or "field")
            lines.append(f"{index}. <b>{html.escape(label)}</b>")
            options = field.get("options") or []
            if options:
                lines.append(
                    "   Options: " + html.escape(", ".join(str(o) for o in options[:12]))
                )
            if (field.get("type") or "").lower() == "file":
                lines.append("   <i>needs a file upload - reply skip</i>")

        for _ in range(2):
            reply = await ask(
                "\n".join(lines), timeout=self.cfg.question_timeout_seconds
            )
            if reply is None:
                return None
            if self._is_skip(reply):
                return "skip"
            answers = []
            for line in reply.splitlines():
                # Strip "1. " / "2) " numbering, but only when the punctuation
                # is followed by whitespace: an answer like "3.5" must survive.
                text = re.sub(r"^\s*\d+\s*[.)]\s+", "", line).strip()
                if text:
                    answers.append(text)
            if len(answers) == len(fields):
                return answers
            lines = [
                f"\u2753 <b>Please resend {len(fields)} answer(s)</b>",
                f"I received {len(answers)} line(s). One line per question, in order:",
            ]
            for index, field in enumerate(fields, start=1):
                label = str(field.get("label") or field.get("name") or "field")
                lines.append(f"{index}. {html.escape(label)}")
        return None

    async def _capture_proof(self, job: Job) -> None:
        """Screenshot the confirmation so the operator has visual proof."""
        if not self.cfg.screenshot_proof:
            return
        try:
            self.cfg.proof_dir.mkdir(parents=True, exist_ok=True)
            path = self.cfg.proof_dir / f"{job.job_id}.png"
            await self.page.screenshot(path=str(path), full_page=False)
            job.proof_path = str(path)
            log.info("saved confirmation proof: %s", path)
        except (PlaywrightError, OSError) as exc:
            log.warning("could not save confirmation proof: %s", exc)


    async def _ask_field(self, job: Job, field: dict) -> str | None:
        """Send one form field to Telegram and wait for the operator's answer."""
        ask = getattr(self.notify, "ask", None)
        if ask is None:
            return None
        label = str(field.get("label") or field.get("name") or "field")
        lines = [
            "\u2753 <b>Your answer is needed</b>",
            f"<b>{html.escape(job.company or 'Unknown company')}</b> \u2014 "
            f"<i>{html.escape(display_title(job.title, job.company))}</i>",
            html.escape(job.url),
            "",
            f"Field: <b>{html.escape(label)}</b>",
        ]
        options = field.get("options") or []
        if options:
            lines.append("Options: " + html.escape(", ".join(str(o) for o in options[:12])))
        if (field.get("type") or "").lower() == "file":
            lines.append("This field needs a file upload - tap <b>Skip application</b>.")
        else:
            lines.append(
                "Reply with the answer, or tap <b>Skip application</b> to skip."
            )
        return await ask(
            "\n".join(lines), timeout=self.cfg.question_timeout_seconds
        )

    async def _fill_answer(self, field: dict, answer: str) -> tuple[bool, str]:
        page = self.page
        key = str(field.get("key", ""))
        ftype = (field.get("type") or "").lower()
        locator = page.locator(f'[data-wfbot-field="{key}"]').first
        try:
            if await locator.count() == 0:
                # React can re-render the form after a previous fill, dropping
                # our temporary data-wfbot-field tag. The control's name is
                # stable, so fall back to it before giving up.
                name = str(field.get("name") or "")
                if name:
                    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
                    locator = page.locator(f'[name="{escaped}"]').first
            if await locator.count() == 0:
                return False, "the form changed before I could fill the answer"
            if ftype == "file":
                return False, "that field needs a file upload; apply manually"
            if ftype == "checkbox":
                yes = answer.strip().lower() in {
                    "yes", "y", "true", "1", "agree", "ok", "sure",
                }
                if not await self._set_choice(locator, yes):
                    return False, f"could not set the checkbox for {field.get('label', 'field')}"
                return True, "answered"
            if ftype in {"select", "select-one", "select-multiple"}:
                return await self._select_answer(locator, field, answer)
            if ftype == "radio":
                return await self._radio_answer(field, answer)
            await locator.fill("")
            await human.type_like_human(locator, answer)
            return True, "answered"
        except PlaywrightError as exc:
            return False, f"could not fill {field.get('label', 'field')}: {type(exc).__name__}"

    async def _select_answer(self, locator, field: dict, answer: str) -> tuple[bool, str]:
        options = [str(item) for item in (field.get("options") or [])]
        wanted = answer.strip().lower()
        match = next((item for item in options if item.lower() == wanted), None)
        if match is None:
            match = next((item for item in options if wanted in item.lower()), None)
        candidates = [match] if match else []
        candidates.append(answer.strip())
        for candidate in candidates:
            if not candidate:
                continue
            for kwargs in ({"label": candidate}, {"value": candidate}):
                try:
                    await locator.select_option(**kwargs)
                    return True, "answered"
                except PlaywrightError:
                    continue
        listed = ", ".join(options[:8]) or "none"
        return False, f"could not match '{answer}' to these options: {listed}"

    async def _set_choice(self, target, checked: bool) -> bool:
        """Check/uncheck a radio/checkbox whose real input may be visually hidden.

        Wellfound styles radios as a visible ``<label>`` over a 0x0, opacity-0
        input. Playwright's ``check()`` waits for the input to be actionable and
        times out, so click the visible label first and fall back to a DOM click.
        """
        if await target.is_checked() == checked:
            return True
        rid = (await target.get_attribute("id")) or ""
        candidates = [target.locator("xpath=ancestor::label[1]")]
        if rid:
            candidates.append(self.page.locator(f'label[for="{rid}"]'))
        for candidate in candidates:
            try:
                if await candidate.count() == 0:
                    continue
                label = candidate.first
                if not await label.is_visible():
                    continue
                await label.click()
                if await target.is_checked() == checked:
                    return True
            except PlaywrightError:
                continue
        try:
            await target.evaluate("el => el.click()")
        except PlaywrightError:
            pass
        if await target.is_checked() == checked:
            return True
        try:
            if checked:
                await target.check(force=True)
            else:
                await target.uncheck(force=True)
        except PlaywrightError:
            return False
        return await target.is_checked() == checked

    async def _radio_answer(self, field: dict, answer: str) -> tuple[bool, str]:
        name = str(field.get("name") or "")
        if not name:
            return False, "could not identify the radio group"
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        radios = self.page.locator(f'input[type="radio"][name="{escaped}"]')
        count = await radios.count()
        wanted = answer.strip().lower()
        labels: list[str] = []
        target = None
        for index in range(count):
            radio = radios.nth(index)
            try:
                label = await radio.evaluate(
                    """el => {
                        const l = el.closest('label') || (
                            el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')
                        );
                        return (l ? l.innerText : (el.getAttribute('aria-label') || el.value || '')).trim();
                    }"""
                )
            except PlaywrightError:
                label = ""
            labels.append(label)
            if label and (label.lower() == wanted or wanted in label.lower()):
                target = radio
                break
        if target is None:
            for index in range(count):
                radio = radios.nth(index)
                value = (await radio.get_attribute("value")) or ""
                if value and (value.lower() == wanted or wanted in value.lower()):
                    target = radio
                    break
        if target is None:
            listed = ", ".join(item for item in labels if item) or "none"
            return False, f"could not match '{answer}' to these options: {listed}"
        if not await self._set_choice(target, True):
            return False, f"could not select '{answer}' in the radio group"
        return True, "answered"

    async def _fill_cover_note(self) -> None:
        """Type the optional cover note into the *note* field only.

        The previous version ended with a bare ``textarea`` selector, which on
        a real application typed the note into the first custom question
        ("Please add up to three bullets..."). A cover note must never leak into
        an employer's question box.
        """
        if not self.cfg.cover_note:
            return
        page = self.page
        for selector in (
            'textarea[name="userNote"]',
            'textarea[name*="cover" i]',
            'textarea[placeholder*="cover letter" i]',
            'textarea[placeholder*="message to" i]',
            'textarea[placeholder*="note to" i]',
        ):
            try:
                fields = page.locator(selector)
                count = await fields.count()
            except PlaywrightError:
                continue
            for index in range(count):
                field = fields.nth(index)
                try:
                    if not await field.is_visible():
                        continue
                    name = (await field.get_attribute("name")) or ""
                    if name.startswith("customQuestionAnswers"):
                        continue
                    if (await field.input_value(timeout=1500)).strip():
                        continue
                except PlaywrightError:
                    continue
                log.info("typing cover note into %s", selector)
                await human.type_like_human(field, self.cfg.cover_note)
                return

    async def _find_submit(self):
        page = self.page
        for locator in (
            # Verified on a live custom-question modal (2026-10): the submit
            # control is "Send application" with this data-test. The old
            # QuickApplyModal name no longer exists.
            page.locator('[data-test="JobApplicationModal--SubmitButton"]').first,
            page.locator('[data-test="QuickApplyModal--SubmitButton"]').first,
            page.get_by_role("button", name=SUBMIT_NAME_RE).first,
            page.locator('button[type="submit"]').last,
            page.locator('[data-test*="submit" i]').first,
        ):
            try:
                if await locator.count() == 0 or not await locator.is_visible():
                    continue
                if await locator.is_disabled():
                    continue
                return locator
            except PlaywrightError:
                continue
        return None

    async def _wait_for_confirmation(self, timeout: float = 18.0) -> tuple[str, str]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_text = ""
        while loop.time() < deadline:
            last_text = await self._body_text()
            if CONFIRMATION_RE.search(last_text):
                return Status.APPLIED, "confirmation shown"
            if await self._applied_state_visible():
                return Status.APPLIED, "listing now shows Applied"
            if SUBMIT_ERROR_RE.search(last_text):
                return Status.UNVERIFIED, "site reported an error after submit"
            await human.pause(1.0, 2.0)

        # Last resort: Wellfound sometimes only paints the "Applied" state after
        # a fresh load, which is why a real submission could look unverified.
        try:
            await self.page.reload(wait_until="domcontentloaded")
            await human.pause(1.0, 2.0)
            if await self._applied_state_visible():
                return Status.APPLIED, "listing shows Applied after reload"
            if CONFIRMATION_RE.search(await self._body_text()):
                return Status.APPLIED, "confirmation shown after reload"
        except PlaywrightError:
            pass

        return (
            Status.UNVERIFIED,
            "submitted, but no confirmation appeared - check the link",
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _finish(job: Job, status: str, detail: str, started) -> Outcome:
        return Outcome(
            job=job, status=status, detail=detail, started_at=started, finished_at=utcnow()
        )

    async def close(self) -> None:
        try:
            await self.context.close()
        except PlaywrightError:
            pass


def JOB_TITLE_OK(title: str) -> bool:
    """Filter obvious non-job anchors that also link to /jobs/..."""
    lowered = title.lower()
    if len(title) < 4 or len(title) > 140:
        return False
    return not any(
        token in lowered for token in ("sign up", "create profile", "view all", "log in")
    )
