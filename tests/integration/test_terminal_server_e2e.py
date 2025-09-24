import pytest

from kmux.terminal_server import TerminalServer


@pytest.mark.asyncio
async def test_e2e_zsh_echo_and_snapshot():
    """End-to-end test using real zsh: create session, run a command, snapshot, and cleanup."""
    ts = TerminalServer(root_password=None)

    # Create session
    session_id = await ts.create_session()
    assert isinstance(session_id, str) and session_id.isdigit()

    # Run a simple command and assert output contains expected text
    out = await ts.execute_command(session_id=session_id, command="echo KMUX_OK", timeout_seconds=8.0)
    assert "KMUX_OK" in out, f"Unexpected output: {out}"

    # Take a snapshot (last command output segment)
    snap = await ts.snapshot(session_id=session_id, include_all=False)
    assert "<snapshot>" in snap

    # Delete session and stop the server
    await ts.delete_session(session_id)
    await ts.stop()
