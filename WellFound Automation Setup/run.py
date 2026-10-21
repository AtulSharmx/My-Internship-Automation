"""Entry point for the Wellfound Startup Discovery Agent.

The legacy internship bot in ``wfbot/`` is no longer imported or started by this
entry point.  Its database and code are preserved for reference.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCK_PATH = ROOT / "data" / "startup_agent.lock"
PID_PATH = ROOT / "data" / "startup_agent.pid"


def acquire_single_instance_lock(path: Path):
    """Return an open handle holding the lock, or None if another copy runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _write_pid() -> None:
    """Publish the pid in a separate file.

    The lock file itself is byte-locked while the agent runs, so it cannot be
    read from another process.  The pid therefore lives in its own file, which
    the supervisor uses to tell whether the agent is alive.
    """
    try:
        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _safe_print(message: str) -> None:
    """Print without failing when there is no console (pythonw.exe)."""
    try:
        print(message)
    except (OSError, ValueError, RuntimeError):
        pass


def main() -> int:
    from startup_agent import configure_console

    configure_console()
    lock = acquire_single_instance_lock(LOCK_PATH)
    if lock is None:
        _safe_print(
            "Another startup discovery agent is already running "
            "(see data/startup_agent.pid)."
        )
        return 1
    _write_pid()
    try:
        from startup_agent.app import amain

        return asyncio.run(amain())
    finally:
        try:
            if PID_PATH.exists() and PID_PATH.read_text(
                encoding="utf-8"
            ).strip() == str(os.getpid()):
                PID_PATH.unlink()
        except OSError:
            pass
        try:
            lock.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
