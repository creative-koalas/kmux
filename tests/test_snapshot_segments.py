
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath('src'))
from kmux.terminal.block_pty_session import BlockPtySession

zsh_available = __import__('shutil').which('zsh') is not None
pytestmark = pytest.mark.skipif(not zsh_available, reason='zsh not available')

@pytest.mark.asyncio
async def test_snapshot_only_contains_last_command_segment():
    s = BlockPtySession()
    await s.start()
    try:
        res1 = await s.execute_command('printf "ONE\n"', timeout_seconds=2.0)
        assert res1.status == 'finished'
        snap1 = await s.snapshot(include_all=False)
        assert 'ONE' in snap1
        
        res2 = await s.execute_command('printf "TWO\n"', timeout_seconds=2.0)
        assert res2.status == 'finished'
        snap2 = await s.snapshot(include_all=False)
        # After second command, snapshot should focus on the most recent window;
        # ensure it contains TWO and does not contain ONE to prove segmentation works
        assert 'TWO' in snap2
        assert 'ONE' not in snap2
    finally:
        await s.stop()
