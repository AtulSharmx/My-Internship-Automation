"""Browser lifecycle: one persistent, consistently-configured Chromium.

Design choices worth knowing:

* A **persistent profile** keeps the Wellfound session cookies between runs so
  the bot logs in once (ideally manually, via ``setup_wizard.py --login``) and
  reuses them for weeks.
* The user agent, viewport, locale and timezone stay **identical every run**.
  Rotating fingerprints per session is itself a strong bot signal - one
  believable, stable identity is quieter than five different ones.
"""

from __future__ import annotations

from playwright.async_api import BrowserContext, Playwright

from .config import Config

DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
NAVIGATION_TIMEOUT_MS = 45_000


async def open_context(playwright: Playwright, cfg: Config) -> BrowserContext:
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)

    kwargs: dict = {
        "user_data_dir": str(cfg.profile_dir),
        "headless": cfg.headless,
        "viewport": DEFAULT_VIEWPORT,
        "locale": "en-US",
        "timezone_id": cfg.timezone,
        "accept_downloads": False,
        "args": ["--disable-session-crashed-bubble", "--hide-scrollbars"],
    }
    if cfg.browser_channel:
        # "chrome" or "msedge": a real branded browser is the least
        # distinguishable option if you have one installed.
        kwargs["channel"] = cfg.browser_channel
    if cfg.user_agent:
        kwargs["user_agent"] = cfg.user_agent

    context = await playwright.chromium.launch_persistent_context(**kwargs)
    context.set_default_timeout(cfg.action_timeout_ms)
    context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    return context
