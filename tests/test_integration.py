"""Integration tests for kmux MCP server."""
import asyncio
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


class TestIntegration:
    """Integration tests for the MCP server."""

    @pytest.mark.asyncio
    async def test_mcp_tools_available(self):
        """Test that all expected MCP tools are available."""
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
    async def test_session_workflow(self):
        """Test complete session workflow via MCP tools."""
        # Ensure MCP lifespan is active so terminal_server is initialized
        async with lifespan(mcp):
            # Create session
            raw = await mcp.call_tool('create_session', {})
            session_id = _extract_text(raw)
            assert "New zsh session created" in session_id
            
            # Extract session ID
            session_id_num = session_id.split(":")[-1].strip().strip('.')
            
            # Update metadata
            await mcp.call_tool('update_session_label', {
                'session_id': session_id_num,
                'label': 'Integration Test Session'
            })
            
            await mcp.call_tool('update_session_description', {
                'session_id': session_id_num,
                'description': 'Test session for integration testing'
            })
            
            # Execute command
            raw = await mcp.call_tool('execute_command', {
                'session_id': session_id_num,
                'command': 'pwd'
            })
            result = _extract_text(raw)
            
            assert "Command finished" in result
            assert "<command-output>" in result
            
            # Take snapshot
            raw = await mcp.call_tool('snapshot', {
                'session_id': session_id_num
            })
            snapshot = _extract_text(raw)
            
            assert "Terminal snapshot" in snapshot
            
            # Delete session
            raw = await mcp.call_tool('delete_session', {
                'session_id': session_id_num
            })
            delete_result = _extract_text(raw)
            
            assert "Session deleted" in delete_result
