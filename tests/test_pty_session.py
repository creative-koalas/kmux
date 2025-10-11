import asyncio
import pytest

from kmux.terminal.pty_session import PtySession, PtySessionStatus

pytestmark = pytest.mark.asyncio

async def test_pty_session_start_stop():
    s = PtySession()
    assert s.status == PtySessionStatus.NOT_STARTED
    await s.start()
    assert s.status == PtySessionStatus.RUNNING
    # Give it a moment to fully initialize
    await asyncio.sleep(0.2)
    s.stop()
    assert s.status == PtySessionStatus.FINISHED
