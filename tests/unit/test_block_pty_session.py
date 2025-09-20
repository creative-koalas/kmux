"""
Unit tests for BlockPtySession class.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.kmux.terminal.block_pty_session import BlockPtySession, CommandStatus, InvalidOperationError
from src.kmux.terminal.pty_session import PtySessionStatus


class TestBlockPtySession:
    """Test BlockPtySession functionality."""

    @pytest.fixture
    def mock_pty_session(self):
        """Create a mock PtySession."""
        mock = AsyncMock()
        mock.status = PtySessionStatus.RUNNING
        return mock

    @pytest.fixture
    def block_pty_session(self, mock_pty_session):
        """Create a BlockPtySession instance with mocked dependencies."""
        session = BlockPtySession()
        session._pty_session = mock_pty_session
        return session

    def test_initialization(self):
        """Test BlockPtySession initialization."""
        session = BlockPtySession()
        assert session._cumulative_output == b''
        assert session._root_password is None
        assert session._current_command is None
        assert session.session_status == PtySessionStatus.NOT_STARTED

    def test_command_status_idle_when_no_markers(self):
        """Test command status is IDLE when no markers are present."""
        session = BlockPtySession()
        session._cumulative_output = b'plain output without markers'
        assert session.command_status == CommandStatus.IDLE

    def test_command_status_idle_when_editstart_last(self):
        """Test command status is IDLE when EDITSTART is the last marker."""
        session = BlockPtySession()
        session._cumulative_output = b'\x1bPkmux;EDITSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        assert session.command_status == CommandStatus.IDLE

    def test_command_status_executing_when_execstart_last(self):
        """Test command status is EXECUTING when EXECSTART is the last marker."""
        session = BlockPtySession()
        session._cumulative_output = b'\x1bPkmux;EXECSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        assert session.command_status == CommandStatus.EXECUTING

    def test_command_status_executing_when_editend_last(self):
        """Test command status is EXECUTING when EDITEND is the last marker."""
        session = BlockPtySession()
        session._cumulative_output = b'\x1bPkmux;EDITEND;1b3e62c774b44f78898be928a7aa6532\x1b\'
        assert session.command_status == CommandStatus.EXECUTING

    def test_command_status_executing_when_execend_last(self):
        """Test command status is EXECUTING when EXECEND is the last marker."""
        session = BlockPtySession()
        session._cumulative_output = b'\x1bPkmux;EXECEND;1b3e62c774b44f78898be928a7aa6532\x1b\'
        assert session.command_status == CommandStatus.EXECUTING

    @pytest.mark.asyncio
    async def test_start_session(self, block_pty_session, mock_pty_session):
        """Test starting the session."""
        await block_pty_session.start()
        mock_pty_session.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_session(self, block_pty_session, mock_pty_session):
        """Test stopping the session."""
        await block_pty_session.stop()
        mock_pty_session.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_command_success(self, block_pty_session, mock_pty_session):
        """Test successful command execution."""
        # Setup idle state
        block_pty_session._cumulative_output = b'\x1bPkmux;EDITSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        
        # Mock the finish event
        block_pty_session._current_command_finish_execution_event = asyncio.Event()
        
        # Start the test
        task = asyncio.create_task(block_pty_session.execute_command("echo hello"))
        
        # Simulate command completion
        await asyncio.sleep(0.1)
        block_pty_session._current_command_finish_execution_event.set()
        
        result = await task
        assert result.status == 'finished'
        mock_pty_session.write_bytes.assert_called()

    @pytest.mark.asyncio
    async def test_execute_command_timeout(self, block_pty_session, mock_pty_session):
        """Test command execution timeout."""
        # Setup idle state
        block_pty_session._cumulative_output = b'\x1bPkmux;EDITSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        
        # Don't set the finish event to trigger timeout
        result = await block_pty_session.execute_command("sleep 10", timeout_seconds=0.1)
        assert result.status == 'timeout'
        assert result.timeout_seconds == 0.1

    @pytest.mark.asyncio
    async def test_execute_command_invalid_state(self, block_pty_session):
        """Test executing command in invalid state raises error."""
        # Setup executing state
        block_pty_session._cumulative_output = b'\x1bPkmux;EXECSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        
        with pytest.raises(InvalidOperationError):
            await block_pty_session.execute_command("echo hello")

    @pytest.mark.asyncio
    async def test_enter_root_password_success(self, block_pty_session, mock_pty_session):
        """Test entering root password successfully."""
        # Setup executing state and root password
        block_pty_session._cumulative_output = b'\x1bPkmux;EXECSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        block_pty_session._root_password = "mypassword"
        
        await block_pty_session.enter_root_password()
        mock_pty_session.write_bytes.assert_called_with(b'mypassword\r')

    @pytest.mark.asyncio
    async def test_enter_root_password_invalid_state(self, block_pty_session):
        """Test entering root password in invalid state raises error."""
        # Setup idle state
        block_pty_session._cumulative_output = b'\x1bPkmux;EDITSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        block_pty_session._root_password = "mypassword"
        
        with pytest.raises(InvalidOperationError):
            await block_pty_session.enter_root_password()

    @pytest.mark.asyncio
    async def test_enter_root_password_no_password(self, block_pty_session):
        """Test entering root password without password set raises error."""
        # Setup executing state but no password
        block_pty_session._cumulative_output = b'\x1bPkmux;EXECSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        
        with pytest.raises(ValueError):
            await block_pty_session.enter_root_password()

    @pytest.mark.asyncio
    async def test_send_keys_success(self, block_pty_session, mock_pty_session):
        """Test sending keys successfully."""
        # Setup executing state
        block_pty_session._cumulative_output = b'\x1bPkmux;EXECSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        
        await block_pty_session.send_keys("test input")
        mock_pty_session.write_bytes.assert_called_with(b'test input')

    @pytest.mark.asyncio
    async def test_send_keys_invalid_state(self, block_pty_session):
        """Test sending keys in invalid state raises error."""
        # Setup idle state
        block_pty_session._cumulative_output = b'\x1bPkmux;EDITSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        
        with pytest.raises(InvalidOperationError):
            await block_pty_session.send_keys("test input")

    def test_get_current_running_command_executing(self, block_pty_session):
        """Test getting current running command when executing."""
        # Setup executing state with current command
        block_pty_session._cumulative_output = b'\x1bPkmux;EXECSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        block_pty_session._current_command = "echo hello"
        
        assert block_pty_session.get_current_running_command() == "echo hello"

    def test_get_current_running_command_idle(self, block_pty_session):
        """Test getting current running command when idle returns None."""
        # Setup idle state
        block_pty_session._cumulative_output = b'\x1bPkmux;EDITSTART;1b3e62c774b44f78898be928a7aa6532\x1b\'
        
        assert block_pty_session.get_current_running_command() is None

    def test_snapshot_all_output(self, block_pty_session):
        """Test snapshot with all output."""
        block_pty_session._cumulative_output = b'test output'
        result = block_pty_session.snapshot(include_all=True)
        # Mock render should return processed output
        assert "test output" in result

    def test_snapshot_recent_output(self, block_pty_session):
        """Test snapshot with recent output only."""
        block_pty_session._cumulative_output = b'old output\x1bPkmux;EXECEND;1b3e62c774b44f78898be928a7aa6532\x1b\new output'
        result = block_pty_session.snapshot(include_all=False)
        # Should only include content after last EXECEND
        assert "new output" in result

    def test_parse_output_complete_block(self):
        """Test parsing output with complete block."""
        session = BlockPtySession()
        
        # Create a complete command block
        output = (
            b'\x1bPkmux;EDITSTART;1b3e62c774b44f78898be928a7aa6532\x1b\' +
            b'echo hello' +
            b'\x1bPkmux;EDITEND;1b3e62c774b44f78898be928a7aa6532\x1b\' +
            b'\x1bPkmux;EXECSTART;1b3e62c774b44f78898be928a7aa6532\x1b\' +
            b'hello\n' +
            b'\x1bPkmux;EXECEND;1b3e62c774b44f78898be928a7aa6532\x1b\'
        )
        
        blocks = session._parse_output(output)
        assert len(blocks) == 1
        assert blocks[0].command == "echo hello"
        assert "hello" in blocks[0].output

    def test_parse_output_incomplete_block(self):
        """Test parsing output with incomplete block returns empty list."""
        session = BlockPtySession()
        
        # Incomplete block (missing EXECEND)
        output = (
            b'\x1bPkmux;EDITSTART;1b3e62c774b44f78898be928a7aa6532\x1b\' +
            b'echo hello' +
            b'\x1bPkmux;EDITEND;1b3e62c774b44f78898be928a7aa6532\x1b\' +
            b'\x1bPkmux;EXECSTART;1b3e62c774b44f78898be928a7aa6532\x1b\' +
            b'hello\n'
        )
        
        blocks = session._parse_output(output)
        assert len(blocks) == 0

    def test_render_removes_markers(self):
        """Test that render method removes all markers."""
        session = BlockPtySession()
        
        output_with_markers = (
            b'prefix\x1bPkmux;EDITSTART;1b3e62c774b44f78898be928a7aa6532\x1b\' +
            b'content\x1bPkmux;EDITEND;1b3e62c774b44f78898be928a7aa6532\x1b\' +
            b'\x1bPkmux;EXECSTART;1b3e62c774b44f78898be928a7aa6532\x1b\' +
            b'more content\x1bPkmux;EXECEND;1b3e62c774b44f78898be928a7aa6532\x1b\' +
            b'suffix'
        )
        
        rendered = session._render(output_with_markers)
        # Should not contain any marker text
        assert "kmux" not in rendered
        assert "EDITSTART" not in rendered
        assert "EDITEND" not in rendered
        assert "EXECSTART" not in rendered
        assert "EXECEND" not in rendered
        # Should contain the actual content
        assert "content" in rendered
        assert "more content" in rendered