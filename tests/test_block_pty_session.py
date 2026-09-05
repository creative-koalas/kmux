from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kmux.terminal.block_pty_session import BlockPtySession, InvalidOperationError, _BlockMarker, _SessionStatus
from kmux.terminal.pty_session import PtySession, PtySessionStatus


class PartiallyStartedPtySession:
    status = PtySessionStatus.NOT_STARTED
    has_open_resources = True

    def __init__(self) -> None:
        self.stop_called = False

    async def stop(self) -> None:
        self.stop_called = True


class StartedPtySession:
    status = PtySessionStatus.RUNNING
    has_open_resources = True

    def __init__(self) -> None:
        self.stop_called = False
        self.writes: list[bytes] = []
        self.command_written = asyncio.Event()

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.stop_called = True

    async def write_bytes(self, data: bytes) -> None:
        self.writes.append(data)
        if len(self.writes) == 2:
            self.command_written.set()


class BlockPtySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_waits_for_initial_edit_marker(self) -> None:
        session = BlockPtySession()
        pty_session = StartedPtySession()
        session._pty_session = pty_session
        start_task = asyncio.create_task(session.start())

        try:
            await asyncio.sleep(0)
            self.assertFalse(start_task.done())
            self.assertFalse(session.session_initialized)

            session._on_new_output(_BlockMarker.EDIT_START.value)
            await asyncio.wait_for(start_task, timeout=0.1)

            self.assertTrue(session.session_initialized)
            self.assertEqual(
                session._get_session_status(session._cumulative_output),
                _SessionStatus.AWAITING_COMMAND,
            )
        finally:
            if not start_task.done():
                start_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await start_task
            await session.stop()

    async def test_start_fails_when_pty_closes_before_initial_marker(self) -> None:
        session = BlockPtySession()
        pty_session = StartedPtySession()
        session._pty_session = pty_session
        start_task = asyncio.create_task(session.start())

        try:
            await asyncio.sleep(0)
            session._on_session_finished()

            with self.assertRaisesRegex(
                RuntimeError,
                "closed before marker initialization",
            ):
                await asyncio.wait_for(start_task, timeout=0.1)

            self.assertFalse(session.session_initialized)
        finally:
            if not start_task.done():
                start_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await start_task
            await session.stop()

    async def test_initial_output_callback_keeps_processing_until_marker(self) -> None:
        session = BlockPtySession()

        session._on_new_output(b"shell starting")
        session._on_new_output(_BlockMarker.EDIT_START.value)

        self.assertEqual(
            session._get_session_status(session._cumulative_output),
            _SessionStatus.AWAITING_COMMAND,
        )

    async def test_input_echo_does_not_signal_command_completion(self) -> None:
        session = BlockPtySession()
        session._cumulative_output = _BlockMarker.EDIT_START.value
        session._current_command_parts = ["echo still-being-entered"]
        session._last_write_time = time.monotonic() - 1

        session._on_new_output(b"echo still-being-entered")

        self.assertFalse(session._session_idle_event.is_set())

    async def test_submit_waits_for_complete_exec_marker_flow(self) -> None:
        session = BlockPtySession()
        pty_session = StartedPtySession()
        session._pty_session = pty_session
        session._cumulative_output = _BlockMarker.EDIT_START.value
        session._session_initialized = True
        submit_task = asyncio.create_task(
            session.submit_command("echo marker-flow", timeout_seconds=0.5)
        )

        try:
            await asyncio.wait_for(pty_session.command_written.wait(), timeout=0.1)

            session._on_new_output(b"echo marker-flow")
            self.assertFalse(submit_task.done())

            session._on_new_output(_BlockMarker.EDIT_END.value)
            session._on_new_output(
                _BlockMarker.EXEC_START.value + b"marker-flow\r\n"
            )
            session._on_new_output(_BlockMarker.EXEC_END.value)
            self.assertFalse(submit_task.done())

            session._on_new_output(_BlockMarker.EDIT_START.value)
            result = await asyncio.wait_for(submit_task, timeout=0.1)

            self.assertEqual(result.result_type, "finished")
            self.assertEqual(result.output, "marker-flow")
        finally:
            if not submit_task.done():
                submit_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await submit_task

    async def test_snapshot_waits_for_markers_without_serializing_behind_submit(self) -> None:
        session = BlockPtySession()
        pty = StartedPtySession()
        session._pty_session = pty
        session._cumulative_output = _BlockMarker.EDIT_START.value
        submit = asyncio.create_task(session.submit_command("echo complete"))
        await asyncio.wait_for(pty.command_written.wait(), timeout=.2)
        snapshot = asyncio.create_task(session.snapshot())
        try:
            session._on_new_output(b"echo complete" + _BlockMarker.EDIT_END.value)
            session._on_new_output(_BlockMarker.EXEC_START.value + b"partial\r\n")
            early = await session.snapshot(wait_seconds=.01)
            self.assertIn("partial", early)
            self.assertFalse(snapshot.done())
            self.assertFalse(submit.done())
            session._on_new_output(b"complete\r\n" + _BlockMarker.EXEC_END.value)
            self.assertFalse(snapshot.done())
            session._on_new_output(_BlockMarker.EDIT_START.value)
            result, output = await asyncio.wait_for(asyncio.gather(submit, snapshot), timeout=.2)
            self.assertEqual(result.result_type, "finished")
            self.assertIn("complete", output)
            self.assertFalse(pty.stop_called)
        finally:
            for task in (submit, snapshot):
                if not task.done():
                    task.cancel()
            await asyncio.gather(submit, snapshot, return_exceptions=True)

    async def test_cancelled_snapshot_leaves_command_and_other_waiters_running(self) -> None:
        session = BlockPtySession()
        pty = StartedPtySession()
        session._pty_session = pty
        session._cumulative_output = _BlockMarker.EDIT_START.value
        await session.submit_command("echo once", timeout_seconds=0)
        session._on_new_output(b"echo once" + _BlockMarker.EDIT_END.value + _BlockMarker.EXEC_START.value)
        cancelled = asyncio.create_task(session.snapshot())
        remaining = asyncio.create_task(session.snapshot())
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.assertFalse(remaining.done())
        self.assertFalse(pty.stop_called)
        self.assertEqual(len(pty.writes), 2)
        session._on_new_output(b"once\r\n" + _BlockMarker.EXEC_END.value + _BlockMarker.EDIT_START.value)
        self.assertIn("once", await asyncio.wait_for(remaining, timeout=.2))
        self.assertEqual(len(pty.writes), 2)

    async def test_snapshot_returns_on_need_for_input_or_reports_closed_session(self) -> None:
        session = BlockPtySession()
        session._cumulative_output = _BlockMarker.EDIT_START.value + _BlockMarker.EDIT_END.value
        waiting = asyncio.create_task(session.snapshot())
        await asyncio.sleep(0)
        session._on_new_output(_BlockMarker.EDIT_START.value)
        await asyncio.wait_for(waiting, timeout=.2)
        self.assertEqual(session._get_session_status(session._cumulative_output), _SessionStatus.INPUT_COMMAND)

        session._session_idle_event.clear()
        session._on_new_output(_BlockMarker.EDIT_END.value + _BlockMarker.EXEC_START.value)
        waiting = asyncio.create_task(session.snapshot())
        await asyncio.sleep(0)
        session._on_session_finished()
        with self.assertRaisesRegex(InvalidOperationError, "closed before command completion"):
            await asyncio.wait_for(waiting, timeout=.2)

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
