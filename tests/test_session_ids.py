from __future__ import annotations

import re
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kmux.native import NativeTerminalService
from kmux.terminal.pty_session import PtySessionStatus
from kmux.terminal_server import (
    SessionLifecycle,
    SessionOperationError,
    TerminalServer,
)

SESSION_ID_PATTERN = re.compile(r"t_[0-9a-f]{32}")


class FakeBlockPtySession:
    def __init__(self, **_: object) -> None:
        self.session_initialized = False
        self.submitted_commands: list[str] = []

    async def start(self) -> None:
        self.session_initialized = True

    async def stop(self) -> None:
        pass

    @property
    def session_status(self) -> PtySessionStatus:
        return PtySessionStatus.RUNNING

    def get_current_running_command(self) -> None:
        return None

    async def submit_command(self, command: str, **_: object) -> None:
        self.submitted_commands.append(command)
        raise AssertionError("a stale session ID must not reach another session")


class SessionIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_ids_are_unique_across_server_restarts(self) -> None:
        with patch("kmux.terminal_server.BlockPtySession", FakeBlockPtySession):
            first_server = TerminalServer()
            try:
                first_ids = [await first_server.create_session() for _ in range(3)]
            finally:
                await first_server.stop()

            # A new server instance stands in for a restarted process.
            second_server = TerminalServer()
            try:
                second_ids = [await second_server.create_session() for _ in range(3)]

                self.assertEqual(len(set(first_ids)), 3)
                self.assertEqual(len(set(second_ids)), 3)
                self.assertTrue(set(first_ids).isdisjoint(second_ids))

                # An ID kept from before the restart must not address a
                # session created after it.
                with self.assertRaises(SessionOperationError) as caught:
                    await second_server.submit_command(first_ids[0], "pwd")
                self.assertEqual(caught.exception.code, "SESSION_NOT_FOUND")
                for session_item in second_server._session_items.values():
                    self.assertEqual(session_item.session.submitted_commands, [])
            finally:
                await second_server.stop()

    async def test_session_ids_are_prefixed_hex_strings(self) -> None:
        with patch("kmux.terminal_server.BlockPtySession", FakeBlockPtySession):
            server = TerminalServer()
            try:
                session_id = await server.create_session()
            finally:
                await server.stop()

        self.assertIsInstance(session_id, str)
        self.assertRegex(session_id, SESSION_ID_PATTERN)
        self.assertEqual(len(session_id), 34)

    async def test_listed_session_id_stays_a_string_when_hex_is_all_digits(self) -> None:
        digits_only = uuid.UUID("12345678-1234-4234-8234-123456789012")

        with (
            patch("kmux.terminal_server.BlockPtySession", FakeBlockPtySession),
            patch("kmux.terminal_server.uuid.uuid4", return_value=digits_only),
        ):
            server = TerminalServer()
            try:
                session_id = await server.create_session()
                raw = await server.list_sessions()
                listed = await NativeTerminalService(server).list_sessions()
            finally:
                await server.stop()

        self.assertEqual(session_id, "t_12345678123442348234123456789012")
        self.assertEqual(yaml.safe_load(raw)[0]["id"], session_id)
        self.assertEqual(listed[0]["id"], session_id)
        self.assertEqual(
            listed[0]["metadata"]["lifecycle"],
            SessionLifecycle.READY.value,
        )


if __name__ == "__main__":
    unittest.main()
