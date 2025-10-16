
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
async def test_pipeline_and_grep():
    async with lifespan(mcp):
        sid_txt = _extract_text(await mcp.call_tool('create_session', {}))
        sid = sid_txt.split(':')[-1].strip('.').strip()
        raw = await mcp.call_tool('execute_command', {
            'session_id': sid,
            'command': "printf 'a\nb\nc\n' | grep b",
            'timeout_seconds': 2.0,
        })
        out = _extract_command_output(_extract_text(raw))
        assert out == 'b', f'pipeline grep output mismatch: {out!r}'
        await mcp.call_tool('delete_session', {'session_id': sid})


@pytest.mark.asyncio
async def test_redirection_and_cat():
    async with lifespan(mcp):
        sid_txt = _extract_text(await mcp.call_tool('create_session', {}))
        sid = sid_txt.split(':')[-1].strip('.').strip()
        raw = await mcp.call_tool('execute_command', {
            'session_id': sid,
            'command': "echo hi > tfile && cat tfile",
            'timeout_seconds': 2.0,
        })
        out = _extract_command_output(_extract_text(raw))
        assert out.splitlines()[-1].strip() == 'hi'
        await mcp.call_tool('delete_session', {'session_id': sid})


@pytest.mark.asyncio
async def test_long_output_contains_tail():
    async with lifespan(mcp):
        sid_txt = _extract_text(await mcp.call_tool('create_session', {}))
        sid = sid_txt.split(':')[-1].strip('.').strip()
        # Generate 200 lines; ensure last line is present
        raw = await mcp.call_tool('execute_command', {
            'session_id': sid,
            'command': "python - <<'PP'\nfor i in range(1,201):\n    print(i)\nPP",
            'timeout_seconds': 4.0,
        })
        out = _extract_command_output(_extract_text(raw))
        assert out.splitlines()[-1].strip().endswith('200'), f'long output tail missing: {out.splitlines()[-5:]}'
        await mcp.call_tool('delete_session', {'session_id': sid})
