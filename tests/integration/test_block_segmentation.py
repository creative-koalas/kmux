import os
import shutil
import pytest

from kmux.terminal_server import TerminalServer

pytestmark = pytest.mark.skipif(os.environ.get("KMUX_RUN_INTEGRATION") != "1", reason="integration tests only run when KMUX_RUN_INTEGRATION=1")


@pytest.mark.asyncio
async def test_block_segmentation_for_sequential_commands():
    if shutil.which("zsh") is None:
        pytest.skip("zsh not installed")

    ts = TerminalServer(root_password=None)
    sid = await ts.create_session()

    out1 = await ts.execute_command(session_id=sid, command="echo ONE", timeout_seconds=8.0)
    assert "ONE" in out1 and "TWO" not in out1

    out2 = await ts.execute_command(session_id=sid, command="echo TWO", timeout_seconds=8.0)
    # should only contain output for the second command
    assert "TWO" in out2 and "ONE" not in out2

    snap = await ts.snapshot(session_id=sid, include_all=False)
    # snapshot should reflect last command output only
    assert "<snapshot>" in snap and "TWO" in snap and "ONE" not in snap

    await ts.delete_session(sid)
    await ts.stop()


@pytest.mark.asyncio
async def test_block_segmentation_multi_command_in_one_block():
    if shutil.which("zsh") is None:
        pytest.skip("zsh not installed")

    ts = TerminalServer(root_password=None)
    sid = await ts.create_session()

    out = await ts.execute_command(session_id=sid, command="echo ALPHA; echo BETA", timeout_seconds=8.0)
    # both ALPHA and BETA should be present since they are part of the same command block
    assert "ALPHA" in out and "BETA" in out

    out_next = await ts.execute_command(session_id=sid, command="echo GAMMA", timeout_seconds=8.0)
    # next block should not contain ALPHA/BETA
    assert "GAMMA" in out_next and "ALPHA" not in out_next and "BETA" not in out_next

    await ts.delete_session(sid)
    await ts.stop()


@pytest.mark.asyncio
async def test_timeout_behavior():
    if shutil.which("zsh") is None:
        pytest.skip("zsh not installed")

    ts = TerminalServer(root_password=None)
    sid = await ts.create_session()

    out = await ts.execute_command(session_id=sid, command="sleep 2", timeout_seconds=0.2)
    assert "timed out" in out.lower()

    await ts.delete_session(sid)
    await ts.stop()
