"""Run the Telegram agent with ``python -m startup_agent``."""

from __future__ import annotations

import asyncio

from . import configure_console
from .app import amain


def main() -> int:
    configure_console()
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
