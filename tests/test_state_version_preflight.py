from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kmux.native import NativeTerminalService
from kmux.terminal_server import (
    PtySessionItem,
    SessionLifecycle,
    SessionOperationError,
    TerminalServer,
)


class RecordingSubmitSession:
    def __init__(self) -> None:
        self.submit_called = False

    async def submit_command(self, command: str, timeout_seconds: float) -> object:
        self.submit_called = True
        return SimpleNamespace(
            result_type="finished",
            duration_seconds=0.01,
            command_buffer=command,
            output="ok",
        )


class RecordingKeySession:
    def __init__(self) -> None:
        self.send_keys_called = False

    async def send_keys(self, keys: str) -> None:
        self.send_keys_called = True


class RecordingRootPasswordSession:
    def __init__(self) -> None:
        self.root_password_called = False

    async def enter_root_password(self) -> None:
        self.root_password_called = True


class StateVersionApiTests(unittest.TestCase):
    def test_terminal_and_native_operations_accept_expected_state_version(self) -> None:
        operations = (
            TerminalServer.submit_command,
            TerminalServer.send_keys,
            TerminalServer.enter_root_password,
            NativeTerminalService.submit_command,
            NativeTerminalService.send_input,
            NativeTerminalService.authenticate_privilege,
        )

        for operation in operations:
            with self.subTest(operation=operation.__qualname__):
                parameter = inspect.signature(operation).parameters.get(
                    "expected_state_version"
                )
                self.assertIsNotNone(parameter)
                self.assertIsNone(parameter.default)

    def test_operation_error_rejects_unknown_error_code(self) -> None:
        with self.assertRaises(ValueError):
            SessionOperationError("SESSION_STPOPPING", "Typo in error code")


class NativeStateVersionForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_command_forwards_expected_state_version(self) -> None:
        terminal_server = SimpleNamespace(
            submit_command=AsyncMock(return_value="command result")
        )

        result = await NativeTerminalService(terminal_server).submit_command(
            "0",
            "pwd",
            wait_seconds=4.0,
            expected_state_version=7,
        )

        self.assertEqual(result["transcript"], "command result")
        terminal_server.submit_command.assert_awaited_once_with(
            session_id="0",
            command="pwd",
            timeout_seconds=4.0,
            expected_state_version=7,
        )

    async def test_send_keys_forwards_expected_state_version(self) -> None:
        terminal_server = SimpleNamespace(send_keys=AsyncMock())

        await NativeTerminalService(terminal_server).send_input(
            "0", "y\r", expected_state_version=8
        )

        terminal_server.send_keys.assert_awaited_once_with(
            session_id="0",
            keys="y\r",
            expected_state_version=8,
        )

    async def test_root_password_forwards_expected_state_version(self) -> None:
        terminal_server = SimpleNamespace(enter_root_password=AsyncMock())

        await NativeTerminalService(terminal_server).authenticate_privilege(
            "0", expected_state_version=9
        )

        terminal_server.enter_root_password.assert_awaited_once_with(
            session_id="0",
            expected_state_version=9,
        )


class StateVersionPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = TerminalServer()

    async def asyncTearDown(self) -> None:
        cleanup_task = self.server._delete_stopped_sessions_task
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

    async def test_submit_rejects_stale_version_before_pty_dispatch(self) -> None:
        session = RecordingSubmitSession()
        self.server._session_items["0"] = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.READY,
            state_version=4,
        )

        with self.assertRaises(SessionOperationError) as caught:
            await self.server.submit_command(
                "0",
                "pwd",
                expected_state_version=3,
            )

        self.assertEqual(caught.exception.code.value, "SESSION_STATE_CHANGED")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.expected_state_version, 3)
        self.assertEqual(caught.exception.current_state_version, 4)
        self.assertFalse(session.submit_called)

    async def test_submit_dispatches_when_state_version_matches(self) -> None:
        session = RecordingSubmitSession()
        self.server._session_items["0"] = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.READY,
            state_version=4,
        )

        result = await self.server.submit_command(
            "0",
            "pwd",
            expected_state_version=4,
        )

        self.assertTrue(session.submit_called)
        self.assertIn("Command finished", result)

    async def test_send_keys_without_state_version_keeps_existing_behavior(self) -> None:
        session = RecordingKeySession()
        self.server._session_items["0"] = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.READY,
            state_version=5,
        )

        await self.server.send_keys("0", "y\r")

        self.assertTrue(session.send_keys_called)

    async def test_send_keys_rejects_stale_version_before_pty_dispatch(self) -> None:
        session = RecordingKeySession()
        self.server._session_items["0"] = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.READY,
            state_version=5,
        )

        with self.assertRaises(SessionOperationError) as caught:
            await self.server.send_keys(
                "0",
                "y\r",
                expected_state_version=4,
            )

        self.assertEqual(caught.exception.code.value, "SESSION_STATE_CHANGED")
        self.assertFalse(session.send_keys_called)

    async def test_root_password_rejects_stale_version_before_dispatch(self) -> None:
        session = RecordingRootPasswordSession()
        self.server._session_items["0"] = PtySessionItem(
            session=session,
            lifecycle=SessionLifecycle.READY,
            state_version=6,
        )

        with self.assertRaises(SessionOperationError) as caught:
            await self.server.enter_root_password(
                "0",
                expected_state_version=5,
            )

        self.assertEqual(caught.exception.code.value, "SESSION_STATE_CHANGED")
        self.assertFalse(session.root_password_called)
