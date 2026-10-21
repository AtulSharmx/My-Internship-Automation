"""Wellfound Startup Discovery Agent.

The package is intentionally independent of the legacy ``wfbot`` internship
application bot.  It discovers startup records from user-provided/imported data
and from explicitly allow-listed public URLs, stores them in SQLite, and exposes
a small Telegram control panel.
"""

from __future__ import annotations

import sys

__version__ = "1.0.0"


def configure_console() -> None:
    """Make emoji and non-ASCII output safe on a Windows console.

    Under ``pythonw.exe`` there is no console at all and the streams are
    ``None``; that is not an error, the agent simply logs to its log file.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
