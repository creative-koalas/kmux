from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kmux.terminal.block_pty_session import BlockPtySession
from kmux.terminal.pty_session import PtySession, PtySessionStatus


class PartiallyStartedPtySession:
    status = PtySessionStatus.NOT_STARTED
    has_open_resources = True

    def __init__(self) -> None:
        self.stop_called = False

    async def stop(self) -> None:
        self.stop_called = True


class BlockPtySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_before_start_is_safe(self) -> None:
        session = BlockPtySession()

        await session.stop()

        self.assertFalse(session.session_initialized)

    async def test_stop_releases_partially_started_pty_resources(self) -> None:
        session = BlockPtySession()
        partial_pty_session = PartiallyStartedPtySession()
        session._pty_session = partial_pty_session

        await session.stop()

        self.assertTrue(partial_pty_session.stop_called)


class PtySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_releases_resources_from_partial_startup(self) -> None:
        on_closed = Mock()
        session = PtySession(on_session_closed_callback=on_closed)
        session._pid = 999_999
        session._master_fd = 42

        with (
            patch.object(session, "_remove_reader_and_writer") as remove_reader,
            patch("kmux.terminal.pty_session.os.close") as close_fd,
            patch("kmux.terminal.pty_session.psutil.pid_exists", return_value=False),
        ):
            await session.stop()
            await session._reap_child_task

        remove_reader.assert_called_once()
        close_fd.assert_called_once_with(42)
        on_closed.assert_called_once()
        self.assertEqual(session.status, PtySessionStatus.FINISHED)
        self.assertFalse(session.has_open_resources)

    async def test_stop_finishes_when_child_exits_before_kill(self) -> None:
        on_closed = Mock()
        session = PtySession(on_session_closed_callback=on_closed)
        session._started = True
        session._pid = 999_999
        session._master_fd = 42

        with (
            patch.object(session, "_remove_reader_and_writer"),
            patch("kmux.terminal.pty_session.os.close"),
            patch("kmux.terminal.pty_session.psutil.pid_exists", return_value=True),
            patch(
                "kmux.terminal.pty_session.os.kill",
                side_effect=ProcessLookupError,
            ),
        ):
            await session.stop()
            await session._reap_child_task

        on_closed.assert_called_once()
        self.assertEqual(session.status, PtySessionStatus.FINISHED)

    async def test_reaper_polls_waitpid_without_blocking_thread(self) -> None:
        session = PtySession()
        waitpid_results = [(0, 0), (999_999, 0)]

        with patch(
            "kmux.terminal.pty_session.os.waitpid",
            side_effect=waitpid_results,
        ) as waitpid:
            await session._reap_child(999_999)

        self.assertEqual(
            waitpid.call_args_list,
            [
                unittest.mock.call(999_999, os.WNOHANG),
                unittest.mock.call(999_999, os.WNOHANG),
            ],
        )
        self.assertNotIn("asyncio.to_thread", inspect.getsource(PtySession._reap_child))

    async def test_reaper_consumes_expected_os_errors(self) -> None:
        session = PtySession()

        for error in (ChildProcessError(), ProcessLookupError()):
            with patch("kmux.terminal.pty_session.os.waitpid", side_effect=error):
                await session._reap_child(999_999)


class PtySessionAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_does_not_wait_for_child_reaping(self) -> None:
        session = PtySession()
        session._started = True
        session._pid = 999_999
        session._master_fd = 42
        with (
            patch.object(session, "_remove_reader_and_writer"),
            patch("kmux.terminal.pty_session.os.close"),
            patch("kmux.terminal.pty_session.psutil.pid_exists", return_value=False),
            patch("kmux.terminal.pty_session.os.waitpid", return_value=(0, 0)),
        ):
            await session.stop()

            self.assertTrue(session._reap_child_task)
            self.assertFalse(session._reap_child_task.done())
            session._reap_child_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await session._reap_child_task

    async def test_stop_cancels_child_exit_watcher(self) -> None:
        session = PtySession()
        session._started = True
        session._pid = 999_999
        session._master_fd = 42
        output_reader_task = asyncio.create_task(asyncio.Event().wait())
        child_exit_task = asyncio.create_task(asyncio.Event().wait())
        session._output_reader_task = output_reader_task
        session._close_on_child_exit_task = child_exit_task

        try:
            with (
                patch.object(session, "_remove_reader_and_writer"),
                patch("kmux.terminal.pty_session.os.close"),
                patch("kmux.terminal.pty_session.psutil.pid_exists", return_value=False),
            ):
                await session.stop()

            await asyncio.sleep(0)
            self.assertTrue(child_exit_task.cancelled())
        finally:
            output_reader_task.cancel()
            child_exit_task.cancel()
            await asyncio.gather(
                output_reader_task,
                child_exit_task,
                return_exceptions=True,
            )

    async def test_stop_reaps_real_child_and_closes_real_fd(self) -> None:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            os.close(write_fd)
            os.pause()
            os._exit(0)

        session = PtySession()
        session._started = True
        session._pid = pid
        session._master_fd = read_fd
        os.close(write_fd)

        try:
            await session.stop()
            await asyncio.wait_for(session._reap_child_task, timeout=1.0)

            with self.assertRaises(OSError):
                os.fstat(read_fd)
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, 9)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
            with contextlib.suppress(OSError):
                os.close(read_fd)
