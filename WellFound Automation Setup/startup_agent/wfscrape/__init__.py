"""Public Wellfound startup scraper.

This package collects **public** Wellfound pages - no account, no login, no
session - and writes one row per company.

How it works
------------
Wellfound renders its pages server-side and ships the complete result set as a
Next.js ``__NEXT_DATA__`` script that carries an Apollo normalized cache.  That
cache is the very data the page renders from, so it is both more complete and
far more stable than reading visible HTML text.

Two phases:

1. :mod:`startup_agent.wfscrape.discover` walks the public role landing pages
   (``/role/r/<slug>?page=N``) over plain HTTP and reads the company id, name,
   slug and published team size from the cache.
2. :mod:`startup_agent.wfscrape.enrich` opens each company profile page
   (``/company/<slug>``) in a browser and reads the company's own external
   website and its market/industry tags from the same cache.

Nothing here logs in, solves a challenge, spoofs a fingerprint or rotates a
user agent.  A security check stops the run and is reported.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
