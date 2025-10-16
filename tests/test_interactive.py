
import os
import sys
import time
import asyncio
import pytest

# Add src to path
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
async def test_read_then_send_keys():
    async with lifespan(mcp):
        sidtxt = _extract_text(await mcp.call_tool('create_session', {}))
        sid = sidtxt.split(':')[-1].strip('.').strip()
        # zsh friendly prompt+read
        _ = _extract_text(await mcp.call_tool('execute_command', {
            'session_id': sid,
            'command': "print -n 'Enter:'; read VAR; echo OK:$VAR",
            'timeout_seconds': 0.3,
        }))
        # send input
        _ = _extract_text(await mcp.call_tool('send_keys', {'session_id': sid, 'keys': 'KMUX_VAL\r'}))
        # poll snapshot until value appears
        found = False
        for _ in range(25):
            snap = _extract_text(await mcp.call_tool('snapshot', {'session_id': sid}))
            if 'OK:KMUX_VAL' in snap:
                found = True
                break
            await asyncio.sleep(0.2)
        assert found, f'echoed value not found in snapshot: {snap}'
        await mcp.call_tool('delete_session', {'session_id': sid})


@pytest.mark.asyncio
async def test_cat_stdin_via_send_keys_and_ctrl_d():
    async with lifespan(mcp):
        sidtxt = _extract_text(await mcp.call_tool('create_session', {}))
        sid = sidtxt.split(':')[-1].strip('.').strip()
        _ = _extract_text(await mcp.call_tool('execute_command', {
            'session_id': sid,
            'command': 'cat',
            'timeout_seconds': 0.3,
        }))
        # send payload
        _ = await mcp.call_tool('send_keys', {'session_id': sid, 'keys': 'HELLO\nWORLD\n'})
        # end with Ctrl-D
        _ = await mcp.call_tool('send_keys', {'session_id': sid, 'keys': '\x04'})
        found = False
        for _ in range(25):
            snap = _extract_text(await mcp.call_tool('snapshot', {'session_id': sid}))
            if 'HELLO' in snap and 'WORLD' in snap:
                found = True
                break
            await asyncio.sleep(0.2)
        assert found, f'stdin content not found in snapshot: {snap}'
        await mcp.call_tool('delete_session', {'session_id': sid})
