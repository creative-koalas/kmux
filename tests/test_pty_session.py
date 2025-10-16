import os, sys, asyncio, shutil, pytest
sys.path.insert(0, os.path.abspath('src'))
from kmux.terminal.pty_session import PtySession

zsh_available = shutil.which('zsh') is not None
pytestmark = pytest.mark.skipif(not zsh_available, reason='zsh not available')

@pytest.mark.asyncio
async def test_pty_session_runs_simple_command():
    s = PtySession()
    await s.start()
    try:
        await s.write_bytes(b'echo KMUX_OK\r')
        # give it a moment to produce output
        await asyncio.sleep(0.3)
        assert s.status.name in {'RUNNING', 'FINISHED'}
    finally:
        s.stop()
