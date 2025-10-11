import asyncio
import pytest
import pytest_asyncio

from kmux.terminal_server import TerminalServer, TerminalServerConfig
from kmux.terminal.block_pty_session import InvalidOperationError

pytestmark = pytest.mark.asyncio

@pytest_asyncio.fixture
async def server():
    # Use default config; keep timeouts small enough for tests
    s = TerminalServer(config=TerminalServerConfig())
    try:
        yield s
    finally:
        await s.stop()

async def test_create_and_list_sessions(server: TerminalServer):
    sid = await server.create_session()
    assert isinstance(sid, str)
    listed = await server.list_sessions()
    assert sid in listed

async def test_execute_and_snapshot(server: TerminalServer):
    sid = await server.create_session()
    out = await server.execute_command(session_id=sid, command="echo TS-hello", timeout_seconds=5.0)
    assert "Command finished" in out
    snap = await server.snapshot(session_id=sid, include_all=False)
    assert "TS-hello" in snap

async def test_send_keys_invalid_when_idle(server: TerminalServer):
    sid = await server.create_session()
    with pytest.raises(InvalidOperationError):
        await server.send_keys(session_id=sid, keys="abc")

async def test_delete_session(server: TerminalServer):
    sid = await server.create_session()
    await server.delete_session(session_id=sid)
    listed = await server.list_sessions()
    # Either no sessions, or at least the deleted id should not appear
    assert sid not in listed
