"""
Test to reproduce specific bugs in block recognition algorithm.
"""
import asyncio
import pytest
from src.kmux.terminal.block_pty_session import BlockPtySession


class TestBugReproduction:
    """Tests to reproduce and verify fixes for specific bugs."""

    @pytest.fixture
    async def session(self):
        """Create a real BlockPtySession for bug testing."""
        session = BlockPtySession()
        await session.start()
        # Wait for zsh to fully initialize
        await asyncio.sleep(2.0)
        yield session
        await session.stop()

    @pytest.mark.asyncio
    async def test_block_recognition_after_complete_command(self, session):
        """
        Test for bug: Complete EDITSTART-EDITEND block should be recognized as IDLE, not EXECUTING.
        
        This reproduces the core algorithm bug where a complete command block
        was incorrectly identified as still executing.
        """
        # Execute a complete command
        result = await session.execute_command("echo bug_test")
        assert result.status == 'finished'
        assert "bug_test" in result.output
        
        # After command completion, should be IDLE, not EXECUTING
        # This is the critical test for the block recognition bug
        assert session.command_status.name == 'IDLE', (
            f"Expected IDLE after command completion, got {session.command_status.name}. "
            f"This indicates the block recognition algorithm bug!"
        )

    @pytest.mark.asyncio
    async def test_consecutive_commands_status(self, session):
        """Test status transitions between consecutive commands."""
        # First command
        result1 = await session.execute_command("echo first")
        assert result1.status == 'finished'
        
        # Should be IDLE before next command
        assert session.command_status.name == 'IDLE'
        
        # Second command
        result2 = await session.execute_command("echo second") 
        assert result2.status == 'finished'
        
        # Should be IDLE again
        assert session.command_status.name == 'IDLE'

    @pytest.mark.asyncio
    async def test_marker_parsing_accuracy(self, session):
        """Test that markers are properly parsed and removed from output."""
        result = await session.execute_command("echo marker_test")
        assert result.status == 'finished'
        
        # Output should not contain raw marker text
        output = result.output
        assert "kmux" not in output, f"Output contains raw markers: {output}"
        assert "EDITSTART" not in output, f"Output contains EDITSTART: {output}"
        assert "EDITEND" not in output, f"Output contains EDITEND: {output}"
        assert "EXECSTART" not in output, f"Output contains EXECSTART: {output}"
        assert "EXECEND" not in output, f"Output contains EXECEND: {output}"
        
        # Should contain the actual command output
        assert "marker_test" in output

    @pytest.mark.asyncio
    async def test_partial_output_handling(self, session):
        """Test handling of partial/incomplete marker sequences."""
        # This tests edge cases where markers might be incomplete
        # due to output buffering or other issues
        
        # Multiple quick commands to stress test
        for i in range(5):
            result = await session.execute_command(f"echo quick_{i}")
            assert result.status == 'finished'
            assert f"quick_{i}" in result.output
            await asyncio.sleep(0.05)  # Very short delay

    @pytest.mark.asyncio
    async def test_session_recovery(self, session):
        """Test that session recovers properly from various states."""
        # Test timeout recovery
        timeout_result = await session.execute_command("sleep 1", timeout_seconds=0.1)
        assert timeout_result.status == 'timeout'
        
        # After timeout, should still be able to execute normal commands
        normal_result = await session.execute_command("echo recovery_test")
        assert normal_result.status == 'finished'
        assert "recovery_test" in normal_result.output
        
        # Should be IDLE state
        assert session.command_status.name == 'IDLE'

    @pytest.mark.asyncio
    async def test_unicode_and_special_chars(self, session):
        """Test handling of unicode and special characters."""
        test_cases = [
            "echo 'hello world'",
            "echo '测试中文'",
            "echo 'special!@#$%^&*()'",
            "echo 'multi\\nline\\noutput'",
        ]
        
        for cmd in test_cases:
            result = await session.execute_command(cmd)
            assert result.status == 'finished', f"Command failed: {cmd}"
            # Should not crash on any of these inputs

    @pytest.mark.asyncio  
    async def test_get_current_command_accuracy(self, session):
        """Test accuracy of get_current_running_command()."""
        # Should be None when idle
        assert session.get_current_running_command() is None
        
        # Start a command and check
        execution_task = asyncio.create_task(
            session.execute_command("sleep 0.3")
        )
        
        # Wait for command to start
        await asyncio.sleep(0.1)
        
        # Should return the current command
        current_cmd = session.get_current_running_command()
        assert current_cmd == "sleep 0.3", f"Expected 'sleep 0.3', got '{current_cmd}'"
        
        # Wait for completion
        result = await execution_task
        assert result.status == 'finished'
        
        # Should be None again
        assert session.get_current_running_command() is None