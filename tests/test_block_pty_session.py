from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch


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
            patch(
                "kmux.terminal.pty_session.asyncio.to_thread",
                new_callable=AsyncMock,
            ),
        ):
            await session.stop()

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
            patch(
                "kmux.terminal.pty_session.asyncio.to_thread",
                new_callable=AsyncMock,
            ),
        ):
            await session.stop()

        on_closed.assert_called_once()
        self.assertEqual(session.status, PtySessionStatus.FINISHED)

    async def test_stop_reaps_child_process_without_blocking_event_loop(self) -> None:
        session = PtySession()
        session._started = True
        session._pid = 999_999
        session._master_fd = 42
        reaping_called = asyncio.Event()

        async def record_reaping(*_: object) -> tuple[int, int]:
            reaping_called.set()
            return 999_999, 0

        with (
            patch.object(session, "_remove_reader_and_writer"),
            patch("kmux.terminal.pty_session.os.close"),
            patch("kmux.terminal.pty_session.psutil.pid_exists", return_value=True),
            patch("kmux.terminal.pty_session.os.kill"),
            patch(
                "kmux.terminal.pty_session.asyncio.to_thread",
                new_callable=AsyncMock,
                side_effect=record_reaping,
            ) as to_thread,
        ):
            await session.stop()

            await asyncio.wait_for(reaping_called.wait(), timeout=0.1)
            to_thread.assert_awaited_once_with(os.waitpid, 999_999, 0)


class PtySessionAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_does_not_wait_for_child_reaping(self) -> None:
        session = PtySession()
        session._started = True
        session._pid = 999_999
        session._master_fd = 42
        reaping_started = asyncio.Event()
        allow_reaping_to_finish = asyncio.Event()

        async def wait_for_reaping(*_: object) -> tuple[int, int]:
            reaping_started.set()
            await allow_reaping_to_finish.wait()
            return 999_999, 0

        stop_task: asyncio.Task[None] | None = None
        try:
            with (
                patch.object(session, "_remove_reader_and_writer"),
                patch("kmux.terminal.pty_session.os.close"),
                patch("kmux.terminal.pty_session.psutil.pid_exists", return_value=False),
                patch(
                    "kmux.terminal.pty_session.asyncio.to_thread",
                    new_callable=AsyncMock,
                    side_effect=wait_for_reaping,
                ),
            ):
                stop_task = asyncio.create_task(session.stop())
                await reaping_started.wait()

                self.assertTrue(stop_task.done())
        finally:
            allow_reaping_to_finish.set()
            if stop_task is not None:
                await stop_task
            await asyncio.sleep(0)

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
                patch(
                    "kmux.terminal.pty_session.asyncio.to_thread",
                    new_callable=AsyncMock,
                ),
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
