from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kmux.terminal.pty_session import PtySessionStatus
from kmux.terminal_server import (
    PtySessionItem,
    SessionLifecycle,
    SessionOperationError,
    TerminalServer,
    TerminalServerConfig,
    TollCallTimeoutError,
)


class FakeBlockPtySession:
    def __init__(self, **kwargs: object):
        self.session_initialized = False
        self._on_session_finished_callback = kwargs["on_session_finished_callback"]

    async def start(self) -> None:
        self.session_initialized = True

    @property
    def session_status(self) -> PtySessionStatus:
        return PtySessionStatus.RUNNING

    async def finish(self) -> None:
        await self._on_session_finished_callback()


class BlockingStopSession:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.stop_started = asyncio.Event()
        self.allow_stop = asyncio.Event()

    async def stop(self) -> None:
        self.stop_calls += 1
        self.stop_started.set()
        await self.allow_stop.wait()


class RecordingSubmitSession:
    def __init__(self) -> None:
        self.submit_called = False

    async def submit_command(self, *_: object, **__: object) -> None:
        self.submit_called = True
        raise AssertionError("a stopping session must not receive a command")


class RecordingKeySession:
    def __init__(self) -> None:
        self.send_keys_called = False

    async def send_keys(self, *_: object, **__: object) -> None:
        self.send_keys_called = True
        raise AssertionError("a non-ready session must not receive keys")


class RecordingRootPasswordSession:
    def __init__(self) -> None:
        self.root_password_called = False

    async def enter_root_password(self) -> None:
        self.root_password_called = True
        raise AssertionError("a non-ready session must not receive a password")


class CountingStopSession:
    def __init__(self) -> None:
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


class SlowStartingSession:
    instances: list["SlowStartingSession"] = []

    def __init__(self, **_: object) -> None:
        self.session_initialized = False
        self.stop_called = False
        self.instances.append(self)

    async def start(self) -> None:
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stop_called = True


class StartReturnsUninitializedSession:
    instances: list["StartReturnsUninitializedSession"] = []

    def __init__(self, **_: object) -> None:
        self.session_initialized = False
        self.stop_called = False
        self.instances.append(self)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.stop_called = True


class CancellableStartingSession:
    instances: list["CancellableStartingSession"] = []

    def __init__(self, **_: object) -> None:
        self.session_initialized = False
        self.stop_called = False
        self.start_started = asyncio.Event()
        self.instances.append(self)

    async def start(self) -> None:
        self.start_started.set()
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stop_called = True


class StuckStartingSession:
    instances: list["StuckStartingSession"] = []

    def __init__(self, **_: object) -> None:
        self.session_initialized = False
        self.stop_calls = 0
        self.start_started = asyncio.Event()
        self.instances.append(self)

    async def start(self) -> None:
        self.start_started.set()
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stop_calls += 1


class FailingStartSession:
    instances: list["FailingStartSession"] = []

    def __init__(self, **_: object) -> None:
        self.session_initialized = False
        self.stop_called = False
        self.instances.append(self)

    async def start(self) -> None:
        raise RuntimeError("zsh is unavailable")

    async def stop(self) -> None:
        self.stop_called = True


class FailingStartAndClosingSession:
    instances: list["FailingStartAndClosingSession"] = []

    def __init__(self, **kwargs: object) -> None:
        self.session_initialized = False
        self.stop_called = False
        self._on_session_finished_callback = kwargs["on_session_finished_callback"]
        self.callback_finished = asyncio.Event()
        self.instances.append(self)

    async def start(self) -> None:
        raise RuntimeError("zsh is unavailable")

    async def stop(self) -> None:
        self.stop_called = True
        asyncio.create_task(self._finish())

    async def _finish(self) -> None:
        await self._on_session_finished_callback()
        self.callback_finished.set()


class ClosesDuringStartSession:
    instances: list["ClosesDuringStartSession"] = []

    def __init__(self, **kwargs: object) -> None:
        self.session_initialized = False
        self.stop_called = False
        self._on_session_finished_callback = kwargs["on_session_finished_callback"]
        self.instances.append(self)

    async def start(self) -> None:
        await self._on_session_finished_callback()
        raise RuntimeError("zsh closed before marker initialization")

    async def stop(self) -> None:
        self.stop_called = True


class ReadyThenClosesDuringStartSession:
    instances: list["ReadyThenClosesDuringStartSession"] = []

    def __init__(self, **kwargs: object) -> None:
        self.session_initialized = False
        self.stop_called = False
        self._on_session_finished_callback = kwargs["on_session_finished_callback"]
        self.instances.append(self)

    @property
    def session_status(self) -> PtySessionStatus:
        return PtySessionStatus.FINISHED

    async def start(self) -> None:
        self.session_initialized = True
        await self._on_session_finished_callback()

    async def stop(self) -> None:
        self.stop_called = True


class FailingStopSession:
    def __init__(self) -> None:
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1
        raise RuntimeError("failed to stop terminal")


class FailsOnceStopSession:
    def __init__(self) -> None:
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_calls == 1:
            raise RuntimeError("first stop failed")


class UninitializedSession:
    session_initialized = False


class ReadySession:
    session_initialized = True

    def get_current_running_command(self) -> None:
        return None


class TerminalServerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = TerminalServer()

    async def asyncTearDown(self) -> None:
        await self._stop_cleanup_worker()

    async def _stop_cleanup_worker(self) -> None:
        task = self.server._delete_stopped_sessions_task
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_created_session_transitions_to_ready(self) -> None:
        with patch("kmux.terminal_server.BlockPtySession", FakeBlockPtySession):
            session_id = await self.server.create_session()

        session_item = self.server._session_items[session_id]

        self.assertEqual(session_item.lifecycle, SessionLifecycle.READY)
        self.assertEqual(session_item.state_version, 1)

    async def test_create_rejects_false_ready_session(self) -> None:
        StartReturnsUninitializedSession.instances.clear()

        with patch(
            "kmux.terminal_server.BlockPtySession",
            StartReturnsUninitializedSession,
        ):
            with self.assertRaises(SessionOperationError) as caught:
                await self.server.create_session()

        session_item = self.server._session_items["0"]
        self.assertEqual(caught.exception.code, "SESSION_START_FAILED")
        self.assertEqual(session_item.lifecycle, SessionLifecycle.FAILED)
        self.assertEqual(session_item.state_version, 1)
        self.assertTrue(StartReturnsUninitializedSession.instances[0].stop_called)

    async def test_naturally_finished_session_transitions_to_terminated(self) -> None:
        with patch("kmux.terminal_server.BlockPtySession", FakeBlockPtySession):
            session_id = await self.server.create_session()

        session_item = self.server._session_items[session_id]
        await self._stop_cleanup_worker()
        await session_item.session.finish()

        self.assertEqual(session_item.lifecycle, SessionLifecycle.TERMINATED)
        self.assertEqual(session_item.state_version, 2)

    async def test_operation_error_keeps_its_code_in_text(self) -> None:
        error = SessionOperationError(
            "SESSION_STOPPING",
            "Session 0 is being deleted.",
            retryable=True,
        )

        self.assertEqual(
            str(error),
            "SESSION_STOPPING: Session 0 is being deleted.",
        )
        self.assertTrue(error.retryable)

    async def test_tool_timeout_error_keeps_its_message_in_text(self) -> None:
        error = TollCallTimeoutError(5.0)

        self.assertEqual(str(error), "Tool call timeout after 5.0 seconds")

    async def test_delete_transitions_to_stopping_before_pty_stops(self) -> None:
        session = BlockingStopSession()
        session_item = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.READY,
            state_version=1,
        )
        self.server._session_items["0"] = session_item

        delete_task = asyncio.create_task(self.server.delete_session("0"))
        try:
            await session.stop_started.wait()

            self.assertEqual(session_item.lifecycle, SessionLifecycle.STOPPING)
            self.assertEqual(session_item.state_version, 2)
        finally:
            session.allow_stop.set()
            await delete_task

    async def test_delete_removes_failed_session_after_cleanup(self) -> None:
        session = CountingStopSession()
        self.server._session_items["0"] = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.FAILED,
            state_version=1,
        )

        await self.server.delete_session("0")

        self.assertEqual(session.stop_calls, 1)
        self.assertNotIn("0", self.server._session_items)

    async def test_submit_rejects_stopping_session_before_pty_dispatch(self) -> None:
        session = RecordingSubmitSession()
        self.server._session_items["0"] = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.STOPPING,
        )

        with self.assertRaises(SessionOperationError) as caught:
            await self.server.submit_command("0", "pwd")

        self.assertEqual(caught.exception.code, "SESSION_STOPPING")
        self.assertFalse(session.submit_called)

    async def test_send_keys_rejects_starting_session_before_pty_dispatch(self) -> None:
        session = RecordingKeySession()
        self.server._session_items["0"] = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.STARTING,
        )

        with self.assertRaises(SessionOperationError) as caught:
            await self.server.send_keys("0", "y\r")

        self.assertEqual(caught.exception.code, "SESSION_STARTING")
        self.assertFalse(session.send_keys_called)

    async def test_enter_root_password_rejects_terminated_session_before_dispatch(self) -> None:
        session = RecordingRootPasswordSession()
        self.server._session_items["0"] = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.TERMINATED,
        )

        with self.assertRaises(SessionOperationError) as caught:
            await self.server.enter_root_password("0")

        self.assertEqual(caught.exception.code, "SESSION_TERMINATED")
        self.assertFalse(session.root_password_called)

    async def test_stop_marks_and_stops_all_sessions(self) -> None:
        first_session = CountingStopSession()
        second_session = CountingStopSession()
        first_item = PtySessionItem(
            session=first_session,
            lifecycle=SessionLifecycle.READY,
            state_version=1,
        )
        second_item = PtySessionItem(
            session=second_session,
            lifecycle=SessionLifecycle.READY,
            state_version=1,
        )
        self.server._session_items = {
            "0": first_item,
            "1": second_item,
        }

        await self.server.stop()

        self.assertEqual(first_session.stop_calls, 1)
        self.assertEqual(second_session.stop_calls, 1)
        self.assertEqual(first_item.lifecycle, SessionLifecycle.STOPPING)
        self.assertEqual(second_item.lifecycle, SessionLifecycle.STOPPING)

    async def test_stop_does_not_regress_terminated_session(self) -> None:
        session = CountingStopSession()
        session_item = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.TERMINATED,
            state_version=3,
        )
        self.server._session_items["0"] = session_item

        await self.server.stop()

        self.assertEqual(session_item.lifecycle, SessionLifecycle.TERMINATED)
        self.assertEqual(session_item.state_version, 3)
        self.assertEqual(session.stop_calls, 0)

    async def test_stop_preserves_failed_session_without_stopping_it(self) -> None:
        session = CountingStopSession()
        session_item = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.FAILED,
            state_version=4,
        )
        self.server._session_items["0"] = session_item

        await self.server.stop()

        self.assertEqual(session_item.lifecycle, SessionLifecycle.FAILED)
        self.assertEqual(session_item.state_version, 4)
        self.assertEqual(session.stop_calls, 0)

    async def test_stop_governs_stuck_starting_session_without_resurrecting_ready(self) -> None:
        StuckStartingSession.instances.clear()
        server = TerminalServer(
            config=TerminalServerConfig(session_startup_timeout_seconds=None),
        )
        create_task: asyncio.Task[str] | None = None
        try:
            with patch("kmux.terminal_server.BlockPtySession", StuckStartingSession):
                create_task = asyncio.create_task(server.create_session())
                while not StuckStartingSession.instances:
                    await asyncio.sleep(0)
                session = StuckStartingSession.instances[0]
                await session.start_started.wait()

                self.assertEqual(
                    server._session_items["0"].lifecycle,
                    SessionLifecycle.STARTING,
                )
                await asyncio.wait_for(server.stop(), timeout=0.1)

            self.assertTrue(create_task.done())
            self.assertTrue(create_task.cancelled())
            self.assertEqual(session.stop_calls, 1)
            self.assertEqual(
                server._session_items["0"].lifecycle,
                SessionLifecycle.STOPPING,
            )
        finally:
            if create_task is not None and not create_task.done():
                create_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await create_task
            if not server._delete_stopped_sessions_task.done():
                server._delete_stopped_sessions_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await server._delete_stopped_sessions_task

    async def test_stop_closes_cleanup_worker_and_is_idempotent(self) -> None:
        worker = self.server._delete_stopped_sessions_task

        await self.server.stop()
        await self.server.stop()

        self.assertTrue(worker.done())
        self.assertFalse(worker.cancelled())

    async def test_stop_waits_for_every_session_before_propagating_stop_error(self) -> None:
        failing_session = FailingStopSession()
        blocking_session = BlockingStopSession()
        self.server._session_items = {
            "0": PtySessionItem(session=failing_session, lifecycle=SessionLifecycle.READY),
            "1": PtySessionItem(session=blocking_session, lifecycle=SessionLifecycle.READY),
        }

        stop_task = asyncio.create_task(self.server.stop())
        await blocking_session.stop_started.wait()
        await asyncio.sleep(0)

        self.assertFalse(stop_task.done())

        blocking_session.allow_stop.set()
        with self.assertLogs("kmux.terminal_server", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "failed to stop terminal"):
                await stop_task

        self.assertEqual(failing_session.stop_calls, 1)

    async def test_stop_retry_stops_session_after_first_stop_is_cancelled(self) -> None:
        session = BlockingStopSession()
        session_item = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.READY,
        )
        self.server._session_items["0"] = session_item

        first_stop = asyncio.create_task(self.server.stop())
        await asyncio.wait_for(session.stop_started.wait(), timeout=0.1)
        first_stop.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(first_stop, timeout=0.1)

        session.allow_stop.set()
        await asyncio.wait_for(self.server.stop(), timeout=0.1)

        self.assertEqual(session.stop_calls, 2)
        self.assertEqual(session_item.lifecycle, SessionLifecycle.STOPPING)

    async def test_stop_retry_attempts_session_again_after_stop_error(self) -> None:
        session = FailsOnceStopSession()
        session_item = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.READY,
        )
        self.server._session_items["0"] = session_item

        with self.assertLogs("kmux.terminal_server", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "first stop failed"):
                await asyncio.wait_for(self.server.stop(), timeout=0.1)

        await asyncio.wait_for(self.server.stop(), timeout=0.1)

        self.assertEqual(session.stop_calls, 2)
        self.assertEqual(session_item.lifecycle, SessionLifecycle.STOPPING)

    async def test_stop_rejects_new_session_creation_after_shutdown_begins(self) -> None:
        running_session = BlockingStopSession()
        self.server._session_items["0"] = PtySessionItem(
            session=running_session,
            lifecycle=SessionLifecycle.READY,
        )

        stop_task = asyncio.create_task(self.server.stop())
        try:
            await running_session.stop_started.wait()

            with patch("kmux.terminal_server.BlockPtySession", FakeBlockPtySession):
                with self.assertRaises(SessionOperationError) as caught:
                    await self.server.create_session()

            self.assertEqual(caught.exception.code, "SERVER_STOPPING")
        finally:
            running_session.allow_stop.set()
            await stop_task

    async def test_start_timeout_marks_session_failed_and_reports_code(self) -> None:
        SlowStartingSession.instances.clear()
        server = TerminalServer(
            config=TerminalServerConfig(session_startup_timeout_seconds=0.001),
        )
        try:
            with patch("kmux.terminal_server.BlockPtySession", SlowStartingSession):
                with self.assertLogs("kmux.terminal_server", level="WARNING"):
                    with self.assertRaises(SessionOperationError) as caught:
                        await server.create_session()

            session_item = server._session_items["0"]
            self.assertEqual(caught.exception.code, "SESSION_START_TIMEOUT")
            self.assertEqual(session_item.lifecycle, SessionLifecycle.FAILED)
            self.assertEqual(session_item.state_version, 1)
            self.assertTrue(SlowStartingSession.instances[0].stop_called)
        finally:
            server._delete_stopped_sessions_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server._delete_stopped_sessions_task

    async def test_create_cancellation_cleans_up_starting_session(self) -> None:
        CancellableStartingSession.instances.clear()
        server = TerminalServer()
        create_task: asyncio.Task[str] | None = None
        try:
            with patch(
                "kmux.terminal_server.BlockPtySession",
                CancellableStartingSession,
            ):
                create_task = asyncio.create_task(server.create_session())
                while not CancellableStartingSession.instances:
                    await asyncio.sleep(0)
                session = CancellableStartingSession.instances[0]
                await session.start_started.wait()

                create_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await create_task

            self.assertEqual(server._session_items["0"].lifecycle, SessionLifecycle.FAILED)
            self.assertTrue(session.stop_called)
        finally:
            if create_task is not None and not create_task.done():
                create_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await create_task
            server._delete_stopped_sessions_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server._delete_stopped_sessions_task

    async def test_start_failure_marks_session_failed_and_reports_code(self) -> None:
        FailingStartSession.instances.clear()
        server = TerminalServer()
        try:
            with patch("kmux.terminal_server.BlockPtySession", FailingStartSession):
                with self.assertLogs("kmux.terminal_server", level="WARNING"):
                    with self.assertRaises(SessionOperationError) as caught:
                        await server.create_session()

            session_item = server._session_items["0"]
            self.assertEqual(caught.exception.code, "SESSION_START_FAILED")
            self.assertEqual(session_item.lifecycle, SessionLifecycle.FAILED)
            self.assertEqual(session_item.state_version, 1)
            self.assertTrue(FailingStartSession.instances[0].stop_called)
        finally:
            server._delete_stopped_sessions_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server._delete_stopped_sessions_task

    async def test_start_failure_close_callback_preserves_failed_session(self) -> None:
        FailingStartAndClosingSession.instances.clear()
        server = TerminalServer()
        server._delete_stopped_sessions_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server._delete_stopped_sessions_task

        with patch(
            "kmux.terminal_server.BlockPtySession",
            FailingStartAndClosingSession,
        ):
            with self.assertLogs("kmux.terminal_server", level="WARNING"):
                with self.assertRaises(SessionOperationError):
                    await server.create_session()

        session = FailingStartAndClosingSession.instances[0]
        await session.callback_finished.wait()

        self.assertEqual(server._session_items["0"].lifecycle, SessionLifecycle.FAILED)
        self.assertTrue(server._stopped_sessions_id_queue.empty())

    async def test_close_during_start_is_retained_as_failed(self) -> None:
        ClosesDuringStartSession.instances.clear()
        await self._stop_cleanup_worker()

        with patch(
            "kmux.terminal_server.BlockPtySession",
            ClosesDuringStartSession,
        ):
            with self.assertLogs("kmux.terminal_server", level="WARNING"):
                with self.assertRaises(SessionOperationError) as caught:
                    await self.server.create_session()

        session_item = self.server._session_items["0"]
        self.assertEqual(caught.exception.code, "SESSION_START_FAILED")
        self.assertEqual(session_item.lifecycle, SessionLifecycle.FAILED)
        self.assertEqual(session_item.state_version, 1)
        self.assertTrue(ClosesDuringStartSession.instances[0].stop_called)
        self.assertTrue(self.server._stopped_sessions_id_queue.empty())

    async def test_close_after_marker_before_ready_is_retained_as_failed(self) -> None:
        ReadyThenClosesDuringStartSession.instances.clear()
        await self._stop_cleanup_worker()

        with patch(
            "kmux.terminal_server.BlockPtySession",
            ReadyThenClosesDuringStartSession,
        ):
            with self.assertLogs("kmux.terminal_server", level="WARNING"):
                with self.assertRaises(SessionOperationError) as caught:
                    await self.server.create_session()

        session_item = self.server._session_items["0"]
        self.assertEqual(caught.exception.code, "SESSION_START_FAILED")
        self.assertEqual(session_item.lifecycle, SessionLifecycle.FAILED)
        self.assertEqual(session_item.state_version, 1)
        self.assertTrue(
            ReadyThenClosesDuringStartSession.instances[0].stop_called
        )
        self.assertTrue(self.server._stopped_sessions_id_queue.empty())

    async def test_list_sessions_reports_failed_initialization(self) -> None:
        self.server._session_items["0"] = PtySessionItem(
            session=UninitializedSession(),
            lifecycle=SessionLifecycle.FAILED,
            state_version=2,
            label="broken-shell",
            description="zsh startup failed",
        )

        sessions = await self.server.list_sessions()

        self.assertIn("Session failed to initialize.", sessions)
        self.assertNotIn("Session still initializing...", sessions)
        self.assertIn("label: broken-shell", sessions)
        self.assertIn("description: zsh startup failed", sessions)
        self.assertIn("lifecycle: failed", sessions)
        self.assertIn("stateVersion: 2", sessions)

    async def test_list_sessions_reports_starting_metadata(self) -> None:
        self.server._session_items["0"] = PtySessionItem(
            session=UninitializedSession(),
            lifecycle=SessionLifecycle.STARTING,
            state_version=0,
            label="booting-shell",
        )

        sessions = await self.server.list_sessions()

        self.assertIn("Session still initializing...", sessions)
        self.assertIn("label: booting-shell", sessions)
        self.assertIn("lifecycle: starting", sessions)
        self.assertIn("stateVersion: 0", sessions)

    async def test_list_sessions_reports_lifecycle_and_state_version(self) -> None:
        self.server._session_items["0"] = PtySessionItem(
            session=ReadySession(),
            lifecycle=SessionLifecycle.READY,
            state_version=4,
        )

        sessions = await self.server.list_sessions()

        self.assertIn("lifecycle: ready", sessions)
        self.assertIn("stateVersion: 4", sessions)
