"""Anti-bot challenge handling and adaptive backoff.

Policy note: this module *detects* Cloudflare Turnstile / CAPTCHA checks and
hands them to the human operator (with an automatic resume once solved). It
deliberately does not attempt to bypass or auto-solve them - that would be a
Terms-of-Service violation and is also far less reliable than a person
clicking the box once in a while.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timedelta

from .config import Config
from .human import pause

#: Frames / containers used by the challenge providers Wellfound and its
#: applicant-flow partner sites embed.
CHALLENGE_SELECTORS: tuple[str, ...] = (
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="turnstile"]',
    'iframe[src*="hcaptcha.com"]',
    'iframe[src*="google.com/recaptcha"]',
    'iframe[src*="recaptcha.net"]',
    'iframe[src*="geetest.com"]',
    "iframe[title*='verification' i]",
    ".g-recaptcha",
    "#turnstile_widget:not(:empty)",
)

#: Visible copy shown while a challenge is in progress.
CHALLENGE_TEXT = re.compile(
    r"one more step before you proceed"
    r"|verify (?:that )?you are (?:a )?human"
    r"|checking (?:your )?browser"
    r"|just a moment\.\.\."
    r"|confirm you are not a robot"
    r"|complete the security check"
    r"|are you a robot"
    r"|waiting for (?:www\.)?wellfound\.com",
    re.IGNORECASE,
)


class ChallengeTimeout(RuntimeError):
    """The operator did not clear a challenge within the allowed window."""


async def challenge_present(page) -> str | None:
    """Return a short description of the challenge on the page, else None."""
    for selector in CHALLENGE_SELECTORS:
        try:
            locator = page.locator(selector)
            if await locator.count() == 0:
                continue
            first = locator.first
            if await first.is_visible():
                return selector
        except Exception:  # noqa: BLE001 - page may be navigating underneath us
            continue

    try:
        text = await page.locator("body").inner_text(timeout=3000)
    except Exception:  # noqa: BLE001
        return None
    match = CHALLENGE_TEXT.search(text or "")
    if match and len(text) < 1500:
        # Long pages only match incidentally; a challenge page is short.
        return match.group(0)
    return None


async def resolve_challenge(page, cfg: Config, notify, where: str) -> None:
    """Wait for the operator to clear a challenge, or raise ``ChallengeTimeout``.

    The browser runs headed by default precisely so this is possible: Telegram
    gets a ping, the human clicks the checkbox, the bot continues by itself.
    """
    kind = await challenge_present(page)
    if not kind:
        return

    await notify.challenge(where, page.url, cfg.challenge_wait_seconds)
    deadline = asyncio.get_running_loop().time() + cfg.challenge_wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        await pause(1.5, 3.0)
        if await challenge_present(page) is None:
            await notify.challenge_cleared()
            return
    raise ChallengeTimeout(
        f"Challenge ({kind}) still present after {cfg.challenge_wait_seconds}s at {where}"
    )


async def maybe_resolve(page, cfg: Config, notify, where: str) -> None:
    """No-op fast path used after every navigation."""
    if await challenge_present(page):
        await resolve_challenge(page, cfg, notify, where)


def backoff_multiplier(cfg: Config, consecutive_failures: int) -> float:
    """How much longer to wait between runs once things start going wrong."""
    if consecutive_failures < cfg.max_consecutive_failures:
        return 1.0
    extra = consecutive_failures - cfg.max_consecutive_failures + 1
    return float(min(cfg.backoff_max_multiplier, round(1.6**extra, 2)))


def daily_slot(cfg: Config, now: datetime | None = None) -> datetime | None:
    """Today's local ``WF_DAILY_RUN_AT`` occurrence, or ``None``."""
    clock = cfg.daily_run_clock
    if clock is None:
        return None
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    return current.replace(
        hour=clock.hour, minute=clock.minute, second=0, microsecond=0
    )


def next_daily_run(cfg: Config, now: datetime | None = None) -> datetime | None:
    """The next local occurrence of ``WF_DAILY_RUN_AT``, or ``None``.

    The comparison is strictly in the future: waking a fraction of a second
    after the scheduled minute advances the next run to tomorrow instead of
    running twice.
    """
    candidate = daily_slot(cfg, now)
    if candidate is None:
        return None
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def next_delay(cfg: Config, consecutive_failures: int) -> float:
    """Seconds until the next run.

    ``WF_INTERVAL_MINUTES=0`` means continuous mode: only a short, jittered gap
    between runs instead of the legacy interval cadence. Either way the wait is
    scaled by the adaptive backoff so a broken session cannot hammer the site.
    The fixed daily schedule does not use this function.
    """
    if cfg.interval_minutes <= 0:
        gap = cfg.continuous_gap_seconds
        base = random.uniform(gap, gap * 2)
        return max(5.0, base) * backoff_multiplier(cfg, consecutive_failures)
    base = cfg.interval_minutes * 60 + random.uniform(0, cfg.interval_jitter_seconds)
    return base * backoff_multiplier(cfg, consecutive_failures)
