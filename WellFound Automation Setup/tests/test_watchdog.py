"""Tests for the silent supervisor that keeps the agent alive at logon.

Nothing here starts a browser or contacts Telegram: the supervisor is tested
with fake child processes and temporary files.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import watchdog


class TempRootsMixin(unittest.TestCase):
    """Point the watchdog at a throw-away data/log folder."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.data_dir = root / "data"
        self.log_dir = root / "logs"
        self.data_dir.mkdir()
        self.log_dir.mkdir()
        patcher = patch.multiple(
            watchdog,
            ROOT=root,
            DATA_DIR=self.data_dir,
            LOG_DIR=self.log_dir,
            LOG_FILE=self.log_dir / "watchdog.log",
            CONSOLE_FILE=self.log_dir / "agent-console.log",
            LOCK_FILE=self.data_dir / "watchdog.lock",
            PID_FILE=self.data_dir / "watchdog.pid",
            AGENT_PID_FILE=self.data_dir / "startup_agent.pid",
            STOP_FILE=self.data_dir / "stop_agent",
            AGENT_SCRIPT=root / "run.py",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)


class TestPidHelpers(TempRootsMixin):
    def test_read_pid_handles_missing_file(self) -> None:
        self.assertEqual(watchdog._read_pid(watchdog.PID_FILE), 0)

    def test_read_pid_handles_garbage(self) -> None:
        watchdog.PID_FILE.write_text("not-a-pid", encoding="utf-8")
        self.assertEqual(watchdog._read_pid(watchdog.PID_FILE), 0)

    def test_read_pid_returns_the_number(self) -> None:
        watchdog.PID_FILE.write_text(" 4321 \n", encoding="utf-8")
        self.assertEqual(watchdog._read_pid(watchdog.PID_FILE), 4321)

    def test_pid_alive_rejects_bad_input(self) -> None:
        self.assertFalse(watchdog._pid_alive(0))
        self.assertFalse(watchdog._pid_alive(-1))

    def test_pid_alive_for_this_process(self) -> None:
        self.assertTrue(watchdog._pid_alive(os.getpid()))

    def test_supervisor_pid_ignores_a_dead_process(self) -> None:
        watchdog.PID_FILE.write_text("999999", encoding="utf-8")
        self.assertEqual(watchdog._supervisor_pid(), 0)

    def test_agent_pid_reads_the_agent_pid_file(self) -> None:
        watchdog.AGENT_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        self.assertEqual(watchdog._agent_pid(), os.getpid())


class TestLocking(TempRootsMixin):
    def test_only_one_supervisor_can_hold_the_lock(self) -> None:
        first = watchdog._acquire_lock()
        self.assertIsNotNone(first)
        try:
            second = watchdog._acquire_lock()
            self.assertIsNone(second)
        finally:
            first.close()

    def test_the_lock_can_be_taken_after_it_is_released(self) -> None:
        first = watchdog._acquire_lock()
        first.close()
        second = watchdog._acquire_lock()
        self.assertIsNotNone(second)
        second.close()

    def test_the_pid_is_published_in_its_own_readable_file(self) -> None:
        handle = watchdog._acquire_lock()
        try:
            watchdog._write_pid(watchdog.PID_FILE)
            # The pid file must be readable while the lock is held.
            self.assertEqual(
                watchdog._read_pid(watchdog.PID_FILE), os.getpid()
            )
        finally:
            handle.close()


class TestStopSwitch(TempRootsMixin):
    def test_stop_creates_the_stop_file(self) -> None:
        self.assertEqual(watchdog.cmd_stop(), 0)
        self.assertTrue(watchdog.STOP_FILE.exists())

    def test_start_removes_the_stop_file(self) -> None:
        watchdog.STOP_FILE.write_text("stopped\n", encoding="utf-8")
        with patch.object(watchdog, "cmd_start", wraps=watchdog.cmd_start):
            with patch.object(watchdog.subprocess, "Popen") as popen:
                popen.return_value.pid = 4242
                with patch.object(watchdog, "_supervisor_pid", return_value=4242):
                    self.assertEqual(watchdog.cmd_start(), 0)
        self.assertFalse(watchdog.STOP_FILE.exists())


class TestSupervisorLoop(TempRootsMixin):
    """The loop must survive a dead agent and must idle while switched off."""

    def test_a_crashed_agent_is_started_again(self) -> None:
        started: list[int] = []
        exits = [1, 0]  # first crash, then a clean exit

        class FakeChild:
            def __init__(self, code: int) -> None:
                self.pid = 1234
                self._code = code

            def wait(self, timeout: float | None = None) -> int:
                return self._code

            def terminate(self) -> None:  # pragma: no cover - not reached
                raise AssertionError("unexpected terminate")

            def kill(self) -> None:  # pragma: no cover - not reached
                raise AssertionError("unexpected kill")

        def fake_spawn() -> FakeChild:
            code = exits.pop(0) if exits else 0
            started.append(1)
            return FakeChild(code)

        def fake_sleep(seconds: float) -> None:
            # Stop the loop once the agent was started a second time.
            if len(started) >= 2:
                raise KeyboardInterrupt

        with patch.object(watchdog, "_spawn_agent", side_effect=fake_spawn):
            with patch.object(watchdog.time, "sleep", side_effect=fake_sleep):
                self.assertEqual(watchdog.supervise(), 0)

        # The agent was started, it "exited", and the loop started it again.
        self.assertEqual(len(started), 2)

    def test_the_agent_is_not_started_while_switched_off(self) -> None:
        watchdog.STOP_FILE.write_text("stopped\n", encoding="utf-8")

        def fake_sleep(seconds: float) -> None:
            raise KeyboardInterrupt

        with patch.object(watchdog, "_spawn_agent") as spawn:
            with patch.object(watchdog.time, "sleep", side_effect=fake_sleep):
                self.assertEqual(watchdog.supervise(), 0)

        spawn.assert_not_called()

    def test_an_agent_that_is_already_running_is_not_started_again(self) -> None:
        watchdog.AGENT_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

        def fake_sleep(seconds: float) -> None:
            raise KeyboardInterrupt

        with patch.object(watchdog, "_spawn_agent") as spawn:
            with patch.object(watchdog.time, "sleep", side_effect=fake_sleep):
                self.assertEqual(watchdog.supervise(), 0)

        spawn.assert_not_called()


class TestTermination(TempRootsMixin):
    def test_the_whole_process_tree_is_stopped(self) -> None:
        class FakeChild:
            pid = 4321

            def terminate(self) -> None:  # pragma: no cover - not used on Windows
                raise AssertionError("taskkill should be used instead")

            def kill(self) -> None:  # pragma: no cover - not used on Windows
                raise AssertionError("taskkill should be used instead")

            def wait(self, timeout: float | None = None) -> int:
                return 0

        with patch.object(watchdog.subprocess, "run") as run:
            watchdog._terminate(FakeChild())

        if os.name == "nt":
            run.assert_called_once()
            args = run.call_args.args[0]
            self.assertIn("/T", args)
            self.assertIn("4321", args)
        else:  # pragma: no cover - POSIX fallback
            run.assert_not_called()


class TestLogging(TempRootsMixin):
    def test_log_lines_are_written_to_the_log_file(self) -> None:
        watchdog._log("a message for the log")
        text = watchdog.LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("a message for the log", text)

    def test_logging_never_raises(self) -> None:
        watchdog.LOG_FILE = self.data_dir / "no" / "such" / "dir" / "watchdog.log"
        watchdog._log("this must not raise")


class TestCommandLine(unittest.TestCase):
    def test_every_command_is_known(self) -> None:
        for command in ("run", "status", "start", "stop", "restart"):
            with patch.object(watchdog, "supervise", return_value=0):
                with patch.object(watchdog, "cmd_status", return_value=0):
                    with patch.object(watchdog, "cmd_start", return_value=0):
                        with patch.object(watchdog, "cmd_stop", return_value=0):
                            with patch.object(
                                watchdog, "cmd_restart", return_value=0
                            ):
                                self.assertEqual(
                                    watchdog.main([command]), 0
                                )

    def test_an_unknown_command_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            watchdog.main(["nonsense"])


if __name__ == "__main__":
    unittest.main()
