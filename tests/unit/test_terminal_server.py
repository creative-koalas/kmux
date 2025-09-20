"""
Unit tests for terminal server functionality.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.kmux.terminal.terminal_server import TerminalServer


class TestTerminalServer:
    """Test TerminalServer functionality."""

    @pytest.fixture
    def mock_block_pty_session(self):
        """Create a mock BlockPtySession."""
        mock = AsyncMock()
        mock.session_status = "RUNNING"
        return mock

    @pytest.fixture
    def terminal_server(self, mock_block_pty_session):
        """Create a TerminalServer instance with mocked dependencies."""
        server = TerminalServer()
        # Mock the session creation to return our mock
        server._create_session = MagicMock(return_value=mock_block_pty_session)
        return server

    @pytest.mark.asyncio
    async def test_create_session(self, terminal_server, mock_block_pty_session):
        """Test session creation."""
        session_id = "test-session"
        
        session = await terminal_server.create_session(session_id)
        assert session is mock_block_pty_session
        terminal_server._create_session.assert_called_once()
        assert session_id in terminal_server._sessions

    @pytest.mark.asyncio
    async def test_create_session_duplicate_id(self, terminal_server, mock_block_pty_session):
        """Test creating session with duplicate ID raises error."""
        session_id = "test-session"
        
        # Create first session
        await terminal_server.create_session(session_id)
        
        # Try to create duplicate
        with pytest.raises(ValueError, match="Session already exists"):
            await terminal_server.create_session(session_id)

    @pytest.mark.asyncio
    async def test_get_session_exists(self, terminal_server, mock_block_pty_session):
        """Test getting an existing session."""
        session_id = "test-session"
        await terminal_server.create_session(session_id)
        
        session = terminal_server.get_session(session_id)
        assert session is mock_block_pty_session

    @pytest.mark.asyncio
    async def test_get_session_not_exists(self, terminal_server):
        """Test getting a non-existent session returns None."""
        session = terminal_server.get_session("non-existent")
        assert session is None

    @pytest.mark.asyncio
    async def test_delete_session(self, terminal_server, mock_block_pty_session):
        """Test deleting a session."""
        session_id = "test-session"
        await terminal_server.create_session(session_id)
        
        # Verify session exists
        assert session_id in terminal_server._sessions
        
        # Delete session
        await terminal_server.delete_session(session_id)
        
        # Verify session is gone
        assert session_id not in terminal_server._sessions
        mock_block_pty_session.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_session_not_exists(self, terminal_server):
        """Test deleting a non-existent session does nothing."""
        # Should not raise error
        await terminal_server.delete_session("non-existent")

    @pytest.mark.asyncio
    async def test_list_sessions(self, terminal_server, mock_block_pty_session):
        """Test listing all sessions."""
        # Create multiple sessions
        await terminal_server.create_session("session1")
        await terminal_server.create_session("session2")
        
        sessions = terminal_server.list_sessions()
        assert len(sessions) == 2
        assert "session1" in sessions
        assert "session2" in sessions

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self, terminal_server):
        """Test listing sessions when none exist."""
        sessions = terminal_server.list_sessions()
        assert len(sessions) == 0

    @pytest.mark.asyncio
    async def test_session_cleanup_on_stop(self, terminal_server, mock_block_pty_session):
        """Test that sessions are cleaned up when server stops."""
        session_id = "test-session"
        await terminal_server.create_session(session_id)
        
        # Verify session exists
        assert session_id in terminal_server._sessions
        
        # Stop server
        await terminal_server.stop()
        
        # Verify all sessions were stopped
        mock_block_pty_session.stop.assert_called_once()
        assert len(terminal_server._sessions) == 0

    @pytest.mark.asyncio
    async def test_execute_command_on_session(self, terminal_server, mock_block_pty_session):
        """Test executing command on a session."""
        session_id = "test-session"
        await terminal_server.create_session(session_id)
        
        # Mock the execute_command result
        mock_result = MagicMock()
        mock_result.status = "finished"
        mock_result.output = "hello world"
        mock_block_pty_session.execute_command.return_value = mock_result
        
        result = await terminal_server.execute_command(session_id, "echo hello")
        
        assert result.status == "finished"
        assert result.output == "hello world"
        mock_block_pty_session.execute_command.assert_called_once_with("echo hello", 5.0)

    @pytest.mark.asyncio
    async def test_execute_command_custom_timeout(self, terminal_server, mock_block_pty_session):
        """Test executing command with custom timeout."""
        session_id = "test-session"
        await terminal_server.create_session(session_id)
        
        # Mock the execute_command result
        mock_result = MagicMock()
        mock_block_pty_session.execute_command.return_value = mock_result
        
        await terminal_server.execute_command(session_id, "sleep 10", timeout_seconds=2.0)
        
        mock_block_pty_session.execute_command.assert_called_once_with("sleep 10", 2.0)

    @pytest.mark.asyncio
    async def test_execute_command_nonexistent_session(self, terminal_server):
        """Test executing command on non-existent session raises error."""
        with pytest.raises(ValueError, match="Session not found"):
            await terminal_server.execute_command("non-existent", "echo hello")

    @pytest.mark.asyncio
    async def test_snapshot_session(self, terminal_server, mock_block_pty_session):
        """Test taking snapshot of a session."""
        session_id = "test-session"
        await terminal_server.create_session(session_id)
        
        # Mock the snapshot result
        mock_block_pty_session.snapshot.return_value = "snapshot content"
        
        result = await terminal_server.snapshot(session_id)
        
        assert result == "snapshot content"
        mock_block_pty_session.snapshot.assert_called_once_with(False)

    @pytest.mark.asyncio
    async def test_snapshot_session_full(self, terminal_server, mock_block_pty_session):
        """Test taking full snapshot of a session."""
        session_id = "test-session"
        await terminal_server.create_session(session_id)
        
        # Mock the snapshot result
        mock_block_pty_session.snapshot.return_value = "full snapshot content"
        
        result = await terminal_server.snapshot(session_id, include_all=True)
        
        assert result == "full snapshot content"
        mock_block_pty_session.snapshot.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_snapshot_nonexistent_session(self, terminal_server):
        """Test taking snapshot of non-existent session raises error."""
        with pytest.raises(ValueError, match="Session not found"):
            await terminal_server.snapshot("non-existent")

    @pytest.mark.asyncio
    async def test_get_current_command(self, terminal_server, mock_block_pty_session):
        """Test getting current command from session."""
        session_id = "test-session"
        await terminal_server.create_session(session_id)
        
        # Mock the get_current_running_command result
        mock_block_pty_session.get_current_running_command.return_value = "sleep 10"
        
        result = terminal_server.get_current_command(session_id)
        
        assert result == "sleep 10"
        mock_block_pty_session.get_current_running_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_current_command_nonexistent_session(self, terminal_server):
        """Test getting current command from non-existent session returns None."""
        result = terminal_server.get_current_command("non-existent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_current_command_no_command(self, terminal_server, mock_block_pty_session):
        """Test getting current command when no command is running."""
        session_id = "test-session"
        await terminal_server.create_session(session_id)
        
        # Mock no command running
        mock_block_pty_session.get_current_running_command.return_value = None
        
        result = terminal_server.get_current_command(session_id)
        assert result is None