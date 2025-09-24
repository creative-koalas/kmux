import os
import re
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

pytestmark = pytest.mark.skipif(os.environ.get("KMUX_RUN_INTEGRATION") != "1", reason="integration tests only run when KMUX_RUN_INTEGRATION=1")


@pytest.mark.asyncio
async def test_mcp_end_to_end_commands_and_snapshot():
    params = StdioServerParameters(command="python", args=["-m", "kmux"])  # start our server via stdio
    async with stdio_client(params) as (read, write):
        client = ClientSession(read, write)
        await client.initialize()

        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert {"create_session", "list_sessions", "execute_command", "snapshot", "delete_session"}.issubset(names)

        # create session and extract ID from returned string
        res = await client.call_tool("create_session", {})
        assert isinstance(res.result, str)
        m = re.search(r"Session ID: (\d+)", res.result)
        assert m, f"unexpected create_session result: {res.result}"
        sid = m.group(1)

        # 1st command -> only ALFA should appear
        res = await client.call_tool("execute_command", {"session_id": sid, "command": "echo ALFA", "timeout_seconds": 8.0})
        assert isinstance(res.result, str)
        assert "ALFA" in res.result and "BETA" not in res.result

        # 2nd command -> only BETA should appear
        res = await client.call_tool("execute_command", {"session_id": sid, "command": "echo BETA", "timeout_seconds": 8.0})
        assert isinstance(res.result, str)
        assert "BETA" in res.result and "ALFA" not in res.result

        # snapshot (last block only) should include BETA and not ALFA
        res = await client.call_tool("snapshot", {"session_id": sid, "include_all": False})
        assert isinstance(res.result, str)
        assert "<snapshot>" in res.result and "BETA" in res.result and "ALFA" not in res.result

        # same-block multi-command -> both appear
        res = await client.call_tool("execute_command", {"session_id": sid, "command": "echo ONE; echo TWO", "timeout_seconds": 8.0})
        assert "ONE" in res.result and "TWO" in res.result

        # cleanup
        await client.call_tool("delete_session", {"session_id": sid})
