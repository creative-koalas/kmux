import pytest
import pytest_asyncio

from kmux.terminal.block_pty_session import BlockPtySession, CommandStatus, InvalidOperationError

pytestmark = pytest.mark.asyncio

@pytest_asyncio.fixture
async def session():
    s = BlockPtySession()
    await s.start()
    try:
        yield s
    finally:
        await s.stop()

async def test_session_initialized(session):
    assert session.session_initialized is True
    assert session.command_status in (CommandStatus.IDLE, CommandStatus.EXECUTING)

async def test_execute_echo(session):
    result = await session.execute_command("echo hello-kmux", timeout_seconds=5.0)
    assert result.status == 'finished'
    assert result.output is not None
    assert "hello-kmux" in result.output

async def test_snapshot_after_command(session):
    # run a command
    await session.execute_command("printf kmux-snap\n", timeout_seconds=5.0)
    snap = await session.snapshot(include_all=False)
    assert "kmux-snap" in snap

async def test_send_keys_invalid_when_idle(session):
    # When idle, send_keys should raise
    with pytest.raises(InvalidOperationError):
        await session.send_keys("abc")
