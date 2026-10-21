"""Silent supervisor that keeps the discovery agent alive.

Windows Task Scheduler starts this file with ``pythonw.exe`` at every logon.
``pythonw.exe`` has no console at all, so nothing flashes on screen and no
terminal is ever opened: switching the laptop on is the only step needed.

The supervisor

* starts ``run.py`` (the Telegram discovery agent) as a child process,
* restarts it whenever it stops, so a crash never leaves the bot offline,
* keeps exactly one supervisor alive through a lock file,
* idles (without exiting) while the agent is switched off, so the scheduled
  task stays quiet instead of thrashing,
* writes every decision to ``logs/watchdog.log``.

Command line::

    python watchdog.py            # run the supervisor (this is what the task starts)
    python watchdog.py status     # report what is running right now
    python watchdog.py start      # switch the agent on and resume the supervisor
    python watchdog.py stop       # switch the agent off (it resumes on ``start``)
    python watchdog.py restart    # stop and start again
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"

LOG_FILE = LOG_DIR / "watchdog.log"
CONSOLE_FILE = LOG_DIR / "agent-console.log"
LOCK_FILE = DATA_DIR / "watchdog.lock"
PID_FILE = DATA_DIR / "watchdog.pid"
AGENT_PID_FILE = DATA_DIR / "startup_agent.pid"
STOP_FILE = DATA_DIR / "stop_agent"
AGENT_SCRIPT = ROOT / "run.py"

# Windows creation flags: never show a console window.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

RESTART_DELAY_SECONDS = 15
CLEAN_EXIT_DELAY_SECONDS = 60
POLL_SECONDS = 2.0
IDLE_SECONDS = 5.0
LOG_MAX_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _log(message: str) -> None:
    """Append a line to ``logs/watchdog.log`` (best effort)."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {message}"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_log()
        with LOG_FILE.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except (OSError, ValueError, RuntimeError):
        pass


def _rotate_log() -> None:
    """Keep the supervisor log small: one backup file is enough."""
    try:
        if not LOG_FILE.exists() or LOG_FILE.stat().st_size < LOG_MAX_BYTES:
            return
        backup = LOG_FILE.with_suffix(".log.1")
        if backup.exists():
            backup.unlink()
        LOG_FILE.replace(backup)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """True when a process with that id is still running.

    On Windows a terminated process can stay in the process table while a
    handle to it is open, so the exit code is checked as well.
    """
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows specific
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(code)
            ):
                return True
            return code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:  # pragma: no cover - POSIX fallback
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _python_exe() -> Path:
    """The interpreter used to run the agent (console-free when possible)."""
    candidates = (
        ROOT / ".venv" / "Scripts" / "pythonw.exe",
        ROOT / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    fallback = Path(sys.executable)
    sibling = fallback.with_name("pythonw.exe")
    if sibling.exists():
        return sibling
    return fallback


def _acquire_lock() -> "object | None":
    """Hold an exclusive lock so only one supervisor ever runs."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(LOCK_FILE, "a+", encoding="utf-8")
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - POSIX fallback
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _write_pid(path: Path) -> None:
    """Publish a pid in its own file.

    The lock file is byte-locked while it is held, so it cannot be read from
    another process.  The pid therefore lives in a separate, readable file.
    """
    try:
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The agent process
# ---------------------------------------------------------------------------
def _spawn_agent() -> subprocess.Popen | None:
    """Start ``run.py`` in a hidden child process."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        console = open(CONSOLE_FILE, "a", encoding="utf-8", errors="replace")
    except OSError:
        console = subprocess.DEVNULL
    try:
        return subprocess.Popen(
            [str(_python_exe()), str(AGENT_SCRIPT)],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=console,
            stderr=console,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
    except OSError as exc:
        _log(f"the agent could not be started: {exc}")
        return None
    finally:
        if console is not subprocess.DEVNULL:
            try:
                console.close()
            except OSError:
                pass


def _terminate(child: subprocess.Popen) -> None:
    """Ask the agent to stop, then force it.

    The venv interpreter starts the real worker as a child process, so the
    whole tree is stopped instead of the wrapper alone.
    """
    if os.name == "nt":  # pragma: no cover - Windows specific
        try:
            subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:  # pragma: no cover - POSIX fallback
        try:
            child.terminate()
        except OSError:
            pass
    try:
        child.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        child.kill()
        child.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _wait_for_exit(child: subprocess.Popen) -> int | None:
    """Wait for the child, honouring the stop file.  None means "stopped"."""
    while True:
        try:
            return child.wait(timeout=POLL_SECONDS)
        except subprocess.TimeoutExpired:
            if STOP_FILE.exists():
                _log("stop file found; the agent is being shut down")
                _terminate(child)
                return None


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------
def supervise() -> int:
    lock = _acquire_lock()
    if lock is None:
        _log("another supervisor is already running; nothing to do")
        return 0
    _log(f"supervisor started (pid {os.getpid()})")
    _write_pid(PID_FILE)
    waiting_for = 0
    try:
        while True:
            if STOP_FILE.exists():
                _log("the agent is switched off; waiting for it to be switched on")
                waiting_for = 0
                time.sleep(IDLE_SECONDS)
                continue

            running = _read_pid(AGENT_PID_FILE)
            if running and _pid_alive(running):
                # An agent from an earlier session is still alive; let it work.
                if running != waiting_for:
                    _log(f"an agent is already running (pid {running}); waiting")
                    waiting_for = running
                time.sleep(POLL_SECONDS * 5)
                continue
            waiting_for = 0

            child = _spawn_agent()
            if child is None:
                time.sleep(RESTART_DELAY_SECONDS)
                continue

            _log(f"the agent started (pid {child.pid})")
            code = _wait_for_exit(child)
            if code is None:
                continue  # stopped on purpose
            _log(f"the agent exited with code {code}")
            delay = CLEAN_EXIT_DELAY_SECONDS if code == 0 else RESTART_DELAY_SECONDS
            _log(f"the agent is restarted in {delay}s")
            time.sleep(delay)
    except KeyboardInterrupt:  # pragma: no cover - manual interruption
        _log("supervisor interrupted")
        return 0
    finally:
        try:
            if PID_FILE.exists() and _read_pid(PID_FILE) == os.getpid():
                PID_FILE.unlink()
        except OSError:
            pass
        try:
            lock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def _supervisor_pid() -> int:
    pid = _read_pid(PID_FILE)
    return pid if pid and _pid_alive(pid) else 0


def _agent_pid() -> int:
    pid = _read_pid(AGENT_PID_FILE)
    return pid if pid and _pid_alive(pid) else 0


def cmd_status() -> int:
    supervisor = _supervisor_pid()
    agent = _agent_pid()
    stopped = STOP_FILE.exists()
    print("Wellfound agent status")
    print(f"  supervisor : {'running (pid %s)' % supervisor if supervisor else 'not running'}")
    print(f"  agent     : {'running (pid %s)' % agent if agent else 'not running'}")
    print(f"  switched  : {'off' if stopped else 'on'}")
    print(f"  log       : {LOG_FILE}")
    if LOG_FILE.exists():
        print("  last log lines:")
        try:
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines[-8:]:
            print(f"    {line}")
    return 0


def cmd_start() -> int:
    if STOP_FILE.exists():
        try:
            STOP_FILE.unlink()
        except OSError as exc:
            print(f"the stop file could not be removed: {exc}")
            return 1
        _log("the agent was switched on")

    if _supervisor_pid():
        print("the supervisor is already running")
        return 0

    try:
        PID_FILE.unlink()
    except OSError:
        pass

    python = _python_exe()
    try:
        subprocess.Popen(
            [str(python), str(Path(__file__).resolve())],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as exc:
        print(f"the supervisor could not be started: {exc}")
        return 1
    for _ in range(20):
        time.sleep(0.5)
        if _supervisor_pid():
            print("the supervisor is running")
            return 0
    print("the supervisor did not come up; see logs/watchdog.log")
    return 1


def cmd_stop() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STOP_FILE.write_text("stopped\n", encoding="utf-8")
    except OSError as exc:
        print(f"the stop file could not be written: {exc}")
        return 1
    _log("the agent was switched off")
    print("the agent is switched off; it comes back with `watchdog.py start`")
    return 0


def cmd_restart() -> int:
    cmd_stop()
    time.sleep(2)
    return cmd_start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="watchdog.py", description="Keep the discovery agent alive."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "status", "start", "stop", "restart"),
        help="what to do (default: run the supervisor)",
    )
    args = parser.parse_args(argv)
    if args.command == "run":
        return supervise()
    if args.command == "status":
        return cmd_status()
    if args.command == "start":
        return cmd_start()
    if args.command == "stop":
        return cmd_stop()
    return cmd_restart()


if __name__ == "__main__":
    raise SystemExit(main())
