"""Human-shaped interaction helpers: jittered pauses, paced typing, real mouse paths.

Everything the bot clicks or types goes through here so timing never looks
metronomic and the pointer never teleports between coordinates.
"""

from __future__ import annotations

import asyncio
import random

from playwright.async_api import Error as PlaywrightError

from .config import Config

#: Last pointer position per page (id(page) -> (x, y)), so movement chains.
_POINTERS: dict[int, tuple[float, float]] = {}


def jitter(cfg: Config) -> float:
    """Random pause between two applications (seconds)."""
    low = float(cfg.min_delay_seconds)
    high = float(max(cfg.max_delay_seconds, cfg.min_delay_seconds))
    # Triangular distribution: clusters around the middle, occasional long beat.
    return random.triangular(low, high, (low + high) / 2)


async def pause(low: float = 0.4, high: float = 1.4) -> None:
    """Short think-time between actions."""
    await asyncio.sleep(random.uniform(low, high))


async def type_like_human(locator, text: str) -> None:
    """Type with per-keystroke variance instead of a single instant ``fill()``.

    Short fields (emails, answers) are typed key by key with a realistic,
    uneven cadence. Long blobs (cover notes) are filled in one go - nobody
    visibly types 500 characters without a single mistake anyway.
    """
    if not text:
        return
    try:
        await locator.click(timeout=2_000)
    except PlaywrightError:
        # react-select style inputs are 1px wide with the placeholder painted
        # on top, so a pointer click is intercepted. Focusing works anyway.
        await locator.focus()
    if len(text) > 80:
        await locator.fill(text)
        await pause(0.2, 0.5)
        return
    for char in text:
        await locator.type(char, delay=max(25.0, random.gauss(78.0, 26.0)))
        if random.random() < 0.05:
            await asyncio.sleep(random.uniform(0.15, 0.5))  # hesitation


async def move_pointer(page, locator) -> None:
    """Slide the pointer to an element in a straight-ish line, then hover.

    The origin is the last known pointer position for this page, so repeated
    movements chain together instead of each one starting from (0, 0).
    """
    try:
        box = await locator.bounding_box()
    except Exception:  # noqa: BLE001 - element may vanish mid-flight
        return
    if not box:
        return
    target_x = box["x"] + random.uniform(box["width"] * 0.25, box["width"] * 0.75)
    target_y = box["y"] + random.uniform(box["height"] * 0.3, box["height"] * 0.7)
    key = id(page)
    start_x, start_y = _POINTERS.get(key, (0.0, 0.0))
    await page.mouse.move(start_x, start_y)
    await page.mouse.move(target_x, target_y, steps=random.randint(8, 22))
    _POINTERS[key] = (target_x, target_y)


async def click_like_human(page, locator) -> None:
    """Hover, settle for a beat, then click."""
    await move_pointer(page, locator)
    await pause(0.12, 0.35)
    await locator.click()


async def scroll_gently(page, rounds: int) -> None:
    """Scroll a results page the way a person skims it."""
    for _ in range(max(0, rounds)):
        await page.mouse.wheel(0, random.randint(500, 900))
        await pause(0.6, 1.4)
        if random.random() < 0.4:
            await page.mouse.wheel(0, -random.randint(80, 240))
            await pause(0.4, 0.9)
