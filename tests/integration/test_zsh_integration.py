"""
Integration tests for BlockPtySession with real zsh sessions.
Tests actual terminal behavior, block recognition, and concurrency.
"""
import asyncio
import pytest
import time
from src.kmux.terminal.block_pty_session import BlockPtySession


class TestBlockPtySessionIntegration:
    """Integration tests with real zsh sessions."""

    @pytest.fixture
    async def session(self):
        """Create a real BlockPtySession for testing."""
        session = BlockPtySession()
        await session.start()
        yield session
        await session.stop()

    @pytest.mark.asyncio
    async def test_basic_command_execution(self, session):
        """Test basic command execution with real zsh."""
        # Wait for zsh to initialize
        await asyncio.sleep(1.0)
        
        # Execute a simple command
        result = await session.execute_command("echo hello")
        
        assert result.status == 'finished'
        assert "hello" in result.output
        assert result.duration_seconds is not None

    @pytest.mark.asyncio
    async def test_command_with_output(self, session):
        """Test command that produces output."""
        await asyncio.sleep(1.0)
        
        result = await session.execute_command("pwd")
        
        assert result.status == 'finished'
        assert "/" in result.output  # Should contain some path

    @pytest.mark.asyncio
    async def test_multiple_commands(self, session):
        """Test executing multiple commands in sequence."""
        await asyncio.sleep(1.0)
        
        # First command
        result1 = await session.execute_command("echo first")
        assert result1.status == 'finished'
        assert "first" in result1.output
        
        # Second command  
        result2 = await session.execute_command("echo second")
        assert result2.status == 'finished'
        assert "second" in result2.output

    @pytest.mark.asyncio
    async def test_command_timeout(self, session):
        """Test command timeout handling."""
        await asyncio.sleep(1.0)
        
        # This should timeout
        result = await session.execute_command("sleep 2", timeout_seconds=0.5)
        
        assert result.status == 'timeout'
        assert result.timeout_seconds == 0.5
        assert result.output is None

    @pytest.mark.asyncio
    async def test_environment_persistence(self, session):
        """Test that environment variables persist between commands."""
        await asyncio.sleep(1.0)
        
        # Set environment variable
        result1 = await session.execute_command("export TEST_VAR=integration_test")
        assert result1.status == 'finished'
        
        # Read environment variable
        result2 = await session.execute_command("echo $TEST_VAR")
        assert result2.status == 'finished'
        assert "integration_test" in result2.output

    @pytest.mark.asyncio
    async def test_current_directory_persistence(self, session):
        """Test that current directory persists between commands."""
        await asyncio.sleep(1.0)
        
        # Change to tmp directory
        result1 = await session.execute_command("cd /tmp")
        assert result1.status == 'finished'
        
        # Check current directory
        result2 = await session.execute_command("pwd")
        assert result2.status == 'finished'
        assert "/tmp" in result2.output

    @pytest.mark.asyncio
    async def test_command_status_detection(self, session):
        """Test that command status detection works correctly."""
        await asyncio.sleep(1.0)
        
        # Should be idle initially
        assert session.command_status.name == 'IDLE'
        
        # Start a command and check status changes
        async def check_status_during_execution():
            # Start a slow command
            execution_task = asyncio.create_task(
                session.execute_command("sleep 0.5 && echo done")
            )
            
            # Give it time to start
            await asyncio.sleep(0.1)
            
            # Should be executing now
            assert session.command_status.name == 'EXECUTING'
            
            # Wait for completion
            result = await execution_task
            assert result.status == 'finished'
            assert "done" in result.output
            
            # Should be idle again
            assert session.command_status.name == 'IDLE'
        
        await check_status_during_execution()

    @pytest.mark.asyncio
    async def test_snapshot_functionality(self, session):
        """Test snapshot functionality with real output."""
        await asyncio.sleep(1.0)
        
        # Execute a command
        await session.execute_command("echo snapshot_test")
        
        # Take snapshot
        snapshot = await session.snapshot()
        
        # Should contain the command output
        assert "snapshot_test" in snapshot

    @pytest.mark.asyncio
    async def test_concurrent_sessions(self):
        """Test creating multiple concurrent sessions."""
        sessions = []
        
        try:
            # Create multiple sessions
            for i in range(3):
                session = BlockPtySession()
                await session.start()
                sessions.append(session)
                await asyncio.sleep(0.1)
            
            # Test each session independently
            for i, session in enumerate(sessions):
                result = await session.execute_command(f"echo session_{i}")
                assert result.status == 'finished'
                assert f"session_{i}" in result.output
                
        finally:
            # Clean up all sessions
            for session in sessions:
                await session.stop()

    @pytest.mark.asyncio
    async def test_block_recognition_accuracy(self, session):
        """Test that block recognition works accurately with real zsh output."""
        await asyncio.sleep(1.0)
        
        # Execute a command that produces specific output pattern
        result = await session.execute_command("echo -e 'line1\\nline2\\nline3'")
        
        assert result.status == 'finished'
        # Should properly capture multi-line output
        assert "line1" in result.output
        assert "line2" in result.output  
        assert "line3" in result.output

    @pytest.mark.asyncio
    async def test_error_command_handling(self, session):
        """Test handling of failing commands."""
        await asyncio.sleep(1.0)
        
        # Command that will fail
        result = await session.execute_command("false")
        
        # Should still complete (not timeout)
        assert result.status == 'finished'
        # Exit code should be non-zero but we get the output
        assert result.output is not None

    @pytest.mark.asyncio
    async def test_complex_command_sequence(self, session):
        """Test complex sequence of commands."""
        await asyncio.sleep(1.0)
        
        commands = [
            "echo 'Starting test sequence'",
            "mkdir -p test_dir",
            "cd test_dir",
            "touch test_file.txt", 
            "ls -la",
            "cd ..",
            "rm -rf test_dir",
            "echo 'Sequence completed'"
        ]
        
        for cmd in commands:
            result = await session.execute_command(cmd)
            assert result.status == 'finished', f"Command failed: {cmd}"
            # Brief pause between commands
            await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_session_cleanup(self, session):
        """Test that session cleans up properly."""
        await asyncio.sleep(1.0)
        
        # Use the session
        result = await session.execute_command("echo cleanup_test")
        assert result.status == 'finished'
        
        # Stop the session
        await session.stop()
        
        # Session should be in stopped state
        # Note: We can't easily test internal state after stop, but no error is good