import os
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

pytestmark = pytest.mark.skipif(os.environ.get("KMUX_RUN_INTEGRATION") != "1", reason="integration tests only run when KMUX_RUN_INTEGRATION=1")


@pytest.mark.asyncio
async def test_mcp_end_to_end_list_and_create_session():
    params = StdioServerParameters(command="python", args=["-m", "kmux"])  # start our server
    async with stdio_client(params) as (read, write):
        client = ClientSession(read, write)
        await client.initialize()
        tools = await client.list_tools()
        # expect core tools present
        names = {t.name for t in tools.tools}
        assert "create_session" in names and "list_sessions" in names

        # call create_session
        res = await client.call_tool("create_session", {})
        # result payload should be a string message containing session id
        assert isinstance(res.result, str)
        assert "Session ID" in res.result

        # list sessions should include the newly created session block
        res2 = await client.call_tool("list_sessions", {})
        assert isinstance(res2.result, str)
        assert "<sessions>" in res2.result
