"""Convenience wrapper: python setup_discovery.py"""

from startup_agent import configure_console
from startup_agent.setup_wizard import main

if __name__ == "__main__":
    configure_console()
    raise SystemExit(main())
