import os, sys, asyncio, pytest
sys.path.insert(0, os.path.abspath('src'))
from kmux.terminal.block_pty_session import BlockPtySession

@pytest.mark.asyncio
async def test_block_session_detects_blocks():
    s = BlockPtySession()
    await s.start()
    try:
        # run simple command; output should be captured in a block
        res = await s.execute_command('printf "one\n"; printf "two\n"', timeout_seconds=3.0)
        assert res.status == 'finished'
        assert res.output is not None
        assert 'one' in res.output
        assert 'two' in res.output
    finally:
        await s.stop()
