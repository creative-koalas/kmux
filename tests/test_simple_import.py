"""Simple import tests for kmux modules."""
import sys
import os

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def test_import_mcp():
    from kmux.app import mcp  # noqa: F401
    assert True


def test_import_terminal_server():
    from kmux.terminal_server import TerminalServer  # noqa: F401
    assert True


def test_import_block_pty_session():
    from kmux.terminal.block_pty_session import BlockPtySession  # noqa: F401
    assert True
