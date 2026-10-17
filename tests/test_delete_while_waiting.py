"""Real zsh sessions: deleting a session must not wait for other waiters.

These tests reproduce the STG deadlock: a snapshot waiting (``wait_seconds``)
on a running command held the registry read lock, ``delete_session`` waited
for the write lock before stopping the PTY, and the command could only end
once the PTY was stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kmux.terminal_server import (
    SessionNotFoundError,
    SessionOperationCode,
    SessionOperationError,
    TerminalServer,
)

DELETE_BUDGET_SECONDS = 5.0
SNAPSHOT_RELEASE_SECONDS = 2.0
PROCESS_EXIT_SECONDS = 3.0


def _is_running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def _wait_for_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _is_running(pid):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _read_pid(path: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(FileNotFoundError, ValueError):
            return int(path.read_text().strip())
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} was not written")


@unittest.skipUnless(shutil.which("zsh"), "requires zsh")
class DeleteWhileWaitingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="kmux-delete-test-")
        self.tmp = Path(self._tmp.name)
        # Start zsh with an empty user .zshrc so the test does not depend on
        # the developer's shell configuration.
        zdotdir = self.tmp / "zdotdir"
        zdotdir.mkdir()
        self._env = patch.dict(os.environ, {"ZDOTDIR": str(zdotdir)})
        self._env.start()
        self.server = TerminalServer()
        self.pids: list[int] = []
        self.tasks: list[asyncio.Task[object]] = []

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.server.stop(), timeout=10)
        for pid in self.pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        self._env.stop()
        self._tmp.cleanup()

    async def _start_foreground_command(self, session_id: str) -> int:
        """Run a long command in the foreground and return its PID."""
        pid_file = self.tmp / f"fg-{session_id}.pid"
        result = await self.server.submit_command(
            session_id,
            f"sh -c 'echo $$ > {pid_file}; exec sleep 60'",
            timeout_seconds=0.5,
        )
        self.assertIn("still running", result)
        pid = await _read_pid(pid_file)
        self.pids.append(pid)
        self.assertTrue(_is_running(pid))
        return pid

    async def _start_waiting_snapshot(self, session_id: str) -> asyncio.Task[str]:
        snapshot = asyncio.create_task(
            self.server.snapshot(session_id, wait_seconds=30)
        )
        self.tasks.append(snapshot)
        # Let the snapshot reach its wait for the command to finish.
        await asyncio.sleep(0.3)
        self.assertFalse(snapshot.done())
        return snapshot

    async def test_delete_stops_command_a_snapshot_is_waiting_on(self) -> None:
        session_id = await self.server.create_session()
        command_pid = await self._start_foreground_command(session_id)
        snapshot = await self._start_waiting_snapshot(session_id)

        started = time.monotonic()
        await asyncio.wait_for(
            self.server.delete_session(session_id),
            timeout=DELETE_BUDGET_SECONDS,
        )
        delete_seconds = time.monotonic() - started

        # The waiting snapshot is released with a lifecycle error instead of
        # waiting for the command (or wait_seconds) to run out.
        with self.assertRaises(SessionOperationError) as caught:
            await asyncio.wait_for(snapshot, timeout=SNAPSHOT_RELEASE_SECONDS)
        self.assertEqual(
            caught.exception.code, SessionOperationCode.SESSION_TERMINATED
        )
        self.assertLess(delete_seconds, DELETE_BUDGET_SECONDS)
        self.assertTrue(await _wait_for_exit(command_pid, PROCESS_EXIT_SECONDS))
        self.assertNotIn(session_id, self.server._session_items)
        self.assertEqual(await self.server.list_sessions(), "No sessions.")

    async def test_delete_kills_background_jobs(self) -> None:
        session_id = await self.server.create_session()
        pid_file = self.tmp / "bg.pid"
        result = await self.server.submit_command(
            session_id,
            f"sleep 300 & echo $! > {pid_file}",
            timeout_seconds=5,
        )
        self.assertIn("Command finished", result)
        background_pid = await _read_pid(pid_file)
        self.pids.append(background_pid)
        self.assertTrue(_is_running(background_pid))

        await asyncio.wait_for(
            self.server.delete_session(session_id),
            timeout=DELETE_BUDGET_SECONDS,
        )

        self.assertTrue(
            await _wait_for_exit(background_pid, PROCESS_EXIT_SECONDS)
        )

    async def test_delete_kills_nohup_and_disowned_jobs(self) -> None:
        # zsh forwards SIGHUP only to jobs it still owns, and nohup ignores
        # it; neither job is in the terminal's foreground process group.
        session_id = await self.server.create_session()
        nohup_file = self.tmp / "nohup.pid"
        disowned_file = self.tmp / "disowned.pid"
        for command in (
            f"nohup sleep 300 >/dev/null 2>&1 & echo $! > {nohup_file}",
            f"sleep 300 &! echo $! > {disowned_file}",
        ):
            result = await self.server.submit_command(
                session_id, command, timeout_seconds=5
            )
            self.assertIn("Command finished", result)
        job_pids = [await _read_pid(nohup_file), await _read_pid(disowned_file)]
        self.pids.extend(job_pids)

        await asyncio.wait_for(
            self.server.delete_session(session_id),
            timeout=DELETE_BUDGET_SECONDS,
        )

        for pid in job_pids:
            self.assertTrue(await _wait_for_exit(pid, PROCESS_EXIT_SECONDS))

    async def test_delete_kills_jobs_that_ignore_hangup_and_terminate(self) -> None:
        session_id = await self.server.create_session()
        pid_file = self.tmp / "stubborn.pid"
        result = await self.server.submit_command(
            session_id,
            f"sh -c 'trap \"\" HUP TERM; echo $$ > {pid_file}; "
            f"while :; do sleep 1; done' &",
            timeout_seconds=5,
        )
        self.assertIn("Command finished", result)
        stubborn_pid = await _read_pid(pid_file)
        self.pids.append(stubborn_pid)

        await asyncio.wait_for(
            self.server.delete_session(session_id),
            timeout=DELETE_BUDGET_SECONDS,
        )

        self.assertTrue(await _wait_for_exit(stubborn_pid, PROCESS_EXIT_SECONDS))

    async def test_delete_leaves_processes_outside_the_session_running(self) -> None:
        outsider = await asyncio.create_subprocess_exec("sleep", "30")
        self.pids.append(outsider.pid)
        other_session_id = await self.server.create_session()
        other_command_pid = await self._start_foreground_command(other_session_id)
        session_id = await self.server.create_session()
        await self._start_foreground_command(session_id)

        await asyncio.wait_for(
            self.server.delete_session(session_id),
            timeout=DELETE_BUDGET_SECONDS,
        )
        await asyncio.sleep(0.5)

        self.assertIsNone(outsider.returncode)
        self.assertTrue(_is_running(outsider.pid))
        self.assertTrue(_is_running(other_command_pid))
        outsider.kill()
        await outsider.wait()

    async def test_waiting_snapshot_does_not_stall_other_sessions(self) -> None:
        busy_session_id = await self.server.create_session()
        await self._start_foreground_command(busy_session_id)
        await self._start_waiting_snapshot(busy_session_id)

        # create_session takes the registry write lock; it used to wait for
        # the snapshot, and every later terminal call queued behind it.
        other_session_id = await asyncio.wait_for(
            self.server.create_session(),
            timeout=DELETE_BUDGET_SECONDS,
        )
        result = await asyncio.wait_for(
            self.server.submit_command(
                other_session_id, "echo other-session-ok", timeout_seconds=5
            ),
            timeout=DELETE_BUDGET_SECONDS,
        )
        self.assertIn("other-session-ok", result)
        await asyncio.wait_for(
            self.server.delete_session(other_session_id),
            timeout=DELETE_BUDGET_SECONDS,
        )

    async def test_operations_racing_delete_fail_cleanly(self) -> None:
        session_id = await self.server.create_session()
        await self._start_foreground_command(session_id)
        snapshot = await self._start_waiting_snapshot(session_id)

        delete = asyncio.create_task(self.server.delete_session(session_id))
        second_delete = asyncio.create_task(self.server.delete_session(session_id))
        await asyncio.sleep(0)
        racing = await asyncio.gather(
            self.server.submit_command(session_id, "echo late", timeout_seconds=1),
            self.server.send_keys(session_id, "\x03"),
            self.server.snapshot(session_id, wait_seconds=1),
            return_exceptions=True,
        )

        await asyncio.wait_for(
            asyncio.gather(delete, second_delete),
            timeout=DELETE_BUDGET_SECONDS,
        )
        snapshot_result = (
            await asyncio.wait_for(
                asyncio.gather(snapshot, return_exceptions=True),
                timeout=SNAPSHOT_RELEASE_SECONDS,
            )
        )[0]

        # Operations arriving mid-delete are rejected (STOPPING, or TERMINATED
        # once the close callback ran); the snapshot that was already waiting
        # is cut short with TERMINATED.
        for result in racing:
            self.assertIsInstance(result, SessionOperationError)
            self.assertIn(
                result.code,
                {
                    SessionOperationCode.SESSION_STOPPING,
                    SessionOperationCode.SESSION_TERMINATED,
                },
            )
        self.assertIsInstance(snapshot_result, SessionOperationError)
        self.assertEqual(
            snapshot_result.code, SessionOperationCode.SESSION_TERMINATED
        )
        self.assertNotIn(session_id, self.server._session_items)
        with self.assertRaises(SessionNotFoundError):
            await self.server.snapshot(session_id, wait_seconds=1)


if __name__ == "__main__":
    unittest.main()
