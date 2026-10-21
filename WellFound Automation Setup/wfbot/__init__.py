"""Wellfound intern-application bot.

A 24/7 automation that finds new Wellfound listings for the roles you configure
and applies for you, reporting everything to Telegram.
"""

__version__ = "1.0.0"


def configure_console() -> None:
    """Keep Unicode job titles from crashing a cp1252 Windows console.

    When output is piped (Task Scheduler, redirecting to a file) use UTF-8 so
    currency symbols survive. On a real console, keep its encoding and replace
    characters it cannot render instead of raising ``UnicodeEncodeError``.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
