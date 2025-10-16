"""Integration tests for kmux MCP server with strict output checks."""
import pytest
import sys
import os

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from kmux.app import mcp, lifespan
from mcp.types import TextContent


def _extract_text(result):
    # FastMCP may return either a plain string or (contents, meta) tuple.
    if isinstance(result, str):
        return result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict) and 'result' in result[1]:
        return result[1]['result']
    # Or a list of TextContent
    if isinstance(result, list) and result and isinstance(result[0], TextContent):
        return result[0].text
    return str(result)


def _extract_command_output(text: str) -> str:
    start = text.find('<command-output>')
    end = text.find('</command-output>')
    if start == -1 or end == -1 or end <= start:
        return ''
    payload = text[start + len('<command-output>'):end]
    # normalize newlines and trim
    return '\n'.join([ln.rstrip() for ln in payload.splitlines()]).strip()


class TestIntegration:
    """Integration tests for the MCP server."""

    @pytest.mark.asyncio
    async def test_mcp_tools_available(self):
        tools = await mcp.list_tools()
        tool_names = [tool.name for tool in tools]
        expected_tools = [
            'create_session',
            'list_sessions',
            'update_session_label',
            'update_session_description',
            'execute_command',
            'send_keys',
            'enter_root_password',
            'snapshot',
            'delete_session'
        ]
        for tool_name in expected_tools:
            assert tool_name in tool_names, f"Tool {tool_name} not found in available tools"

    @pytest.mark.asyncio
    async def test_session_workflow_with_output_checks(self):
        """Complete workflow with strong output verification (pwd content)."""
        async with lifespan(mcp):
            # Create session
            raw = await mcp.call_tool('create_session', {})
            session_id_text = _extract_text(raw)
            assert "Session ID:" in session_id_text
            session_id = session_id_text.split(":")[-1].strip().strip('.')

            # Execute deterministic command and verify output
            raw = await mcp.call_tool('execute_command', {
                'session_id': session_id,
                'command': 'pwd',
                'timeout_seconds': 2.0,
            })
            result_text = _extract_text(raw)
            assert 'Command finished' in result_text
            out = _extract_command_output(result_text)
            assert out, f"no command output found in: {result_text}"
            expected_cwd = os.getcwd()
            assert expected_cwd in out, f"pwd output mismatch: expected to contain {expected_cwd}, got: {out!r}"

            # Also verify a multi-command output contains both tokens
            raw = await mcp.call_tool('execute_command', {
                'session_id': session_id,
                'command': 'printf "one\n"; printf "two\n"',
                'timeout_seconds': 2.0,
            })
            result_text = _extract_text(raw)
            out2 = _extract_command_output(result_text)
            assert 'one' in out2 and 'two' in out2

            # Snapshot
            snap_text = _extract_text(await mcp.call_tool('snapshot', {'session_id': session_id}))
            assert 'Terminal snapshot' in snap_text

            # Delete session
            del_text = _extract_text(await mcp.call_tool('delete_session', {'session_id': session_id}))
            assert 'Session deleted' in del_text

    @pytest.mark.asyncio
    async def test_error_paths_and_timeout_with_output(self):
        async with lifespan(mcp):
            # Create session
            raw = await mcp.call_tool('create_session', {})
            text = _extract_text(raw)
            assert 'Session ID:' in text
            sid = text.split(':')[-1].strip('.').strip()

            # Execute a simple command and verify content
            raw = await mcp.call_tool('execute_command', {
                'session_id': sid,
                'command': 'echo E2E_OK',
                'timeout_seconds': 2.0,
            })
            out = _extract_text(raw)
            assert 'Command finished' in out
            assert 'E2E_OK' in _extract_command_output(out)

            # Call send_keys when idle (should fail gracefully)
            raw = await mcp.call_tool('send_keys', {'session_id': sid, 'keys': 'A'})
            msg = _extract_text(raw)
            assert 'Failed to send keys' in msg

            # Execute with very small timeout to hit timeout path
            raw = await mcp.call_tool('execute_command', {
                'session_id': sid,
                'command': 'sleep 1',
                'timeout_seconds': 0.2,
            })
            tout = _extract_text(raw)
            assert 'Command timed out after' in tout

            # Snapshot and delete
            snap = _extract_text(await mcp.call_tool('snapshot', {'session_id': sid}))
            assert 'Terminal snapshot' in snap
            delmsg = _extract_text(await mcp.call_tool('delete_session', {'session_id': sid}))
            assert 'Session deleted' in delmsg
