
import os
import sys
import pytest

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from kmux.app import mcp, lifespan


def _extract_text(result):
    if isinstance(result, str):
        return result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict) and 'result' in result[1]:
        return result[1]['result']
    return str(result)


def _extract_command_output(text: str) -> str:
    start = text.find('<command-output>')
    end = text.find('</command-output>')
    if start == -1 or end == -1 or end <= start:
        return ''
    payload = text[start + len('<command-output>'):end]
    return '\n'.join([ln.rstrip() for ln in payload.splitlines()]).strip()


@pytest.mark.asyncio
async def test_pwd_and_ls_contents_are_correct():
    async with lifespan(mcp):
        # Create session
        raw = await mcp.call_tool('create_session', {})
        text = _extract_text(raw)
        assert 'Session ID:' in text
        sid = text.split(':')[-1].strip('.').strip()

        # Baseline PWD
        raw = await mcp.call_tool('execute_command', {'session_id': sid, 'command': 'pwd', 'timeout_seconds': 2.0})
        base_out = _extract_command_output(_extract_text(raw))
        assert base_out

        # Create and cd into a temp folder (idempotent)
        raw = await mcp.call_tool('execute_command', {'session_id': sid, 'command': 'mkdir -p test_tmp_kmux && cd test_tmp_kmux', 'timeout_seconds': 2.0})
        _ = _extract_text(raw)

        # Check PWD is updated
        raw = await mcp.call_tool('execute_command', {'session_id': sid, 'command': 'pwd', 'timeout_seconds': 2.0})
        pwd_out = _extract_command_output(_extract_text(raw))
        assert pwd_out.endswith('test_tmp_kmux'), f"pwd did not change into test_tmp_kmux: {pwd_out!r}"

        # Create files and list
        raw = await mcp.call_tool('execute_command', {'session_id': sid, 'command': 'printf a > f1 && printf b > f2 && ls -1 | sort', 'timeout_seconds': 2.0})
        ls_out = _extract_command_output(_extract_text(raw))
        assert 'f1' in ls_out and 'f2' in ls_out, f"ls output mismatch: {ls_out!r}"
