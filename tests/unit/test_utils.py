"""
Unit tests for utility functions.
"""
import pytest

from src.kmux.terminal.utils import render_bytes


class TestUtils:
    """Test utility functions."""

    def test_render_bytes_basic(self):
        """Test basic bytes rendering."""
        input_bytes = b'Hello World'
        result = render_bytes(input_bytes)
        assert "Hello World" in result
        assert len(result) > 0

    def test_render_bytes_empty(self):
        """Test rendering empty bytes."""
        result = render_bytes(b'')
        assert result == []

    def test_render_bytes_with_escape_codes(self):
        """Test rendering bytes with escape codes."""
        # Simple escape sequence
        input_bytes = b'\x1b[32mGreen Text\x1b[0m'
        result = render_bytes(input_bytes)
        # Should contain the text content
        assert "Green Text" in ''.join(result)

    def test_render_bytes_with_newlines(self):
        """Test rendering bytes with newlines."""
        input_bytes = b'Line 1\nLine 2\nLine 3'
        result = render_bytes(input_bytes)
        assert len(result) >= 3
        assert "Line 1" in result[0]
        assert "Line 2" in result[1]
        assert "Line 3" in result[2]

    def test_render_bytes_with_carriage_return(self):
        """Test rendering bytes with carriage returns."""
        input_bytes = b'Loading...\rDone!'
        result = render_bytes(input_bytes)
        # Carriage return should overwrite previous content
        assert "Done!" in ''.join(result)
        assert "Loading..." not in ''.join(result)

    def test_render_bytes_screen_dimensions(self):
        """Test rendering with specific screen dimensions."""
        input_bytes = b'x' * 100  # 100 characters
        result = render_bytes(input_bytes, screen_width=20, screen_height=10)
        
        # Should wrap to multiple lines
        assert len(result) > 1
        
        # Each line should be at most screen_width characters
        for line in result:
            assert len(line.rstrip()) <= 20

    def test_render_bytes_unicode(self):
        """Test rendering unicode bytes."""
        input_bytes = '你好世界'.encode('utf-8')
        result = render_bytes(input_bytes)
        # Should handle unicode characters
        assert len(result) > 0

    def test_render_bytes_complex_escape_sequences(self):
        """Test rendering with complex escape sequences."""
        # Color codes and cursor movement
        input_bytes = b'\x1b[31mRed\x1b[0m \x1b[32mGreen\x1b[0m \x1b[34mBlue\x1b[0m'
        result = render_bytes(input_bytes)
        # Should contain the text content
        rendered_text = ''.join(result)
        assert "Red" in rendered_text
        assert "Green" in rendered_text  
        assert "Blue" in rendered_text

    def test_render_bytes_long_content(self):
        """Test rendering long content with wrapping."""
        # Create a long line that should wrap
        long_text = b'A' * 50  # 50 characters
        result = render_bytes(long_text, screen_width=20, screen_height=10)
        
        # Should wrap to multiple lines
        assert len(result) == 3  # 50 chars / 20 width = 2.5 → 3 lines
        
        # Check line lengths
        assert len(result[0].rstrip()) == 20
        assert len(result[1].rstrip()) == 20
        assert len(result[2].rstrip()) == 10

    def test_render_bytes_scroll_behavior(self):
        """Test rendering with screen height limit."""
        # Create more lines than screen height
        many_lines = b'\n'.join([b'Line ' + str(i).encode() for i in range(20)])
        result = render_bytes(many_lines, screen_width=80, screen_height=5)
        
        # Should be limited to screen height
        assert len(result) == 5
        # Should show the most recent lines
        assert "Line 19" in result[-1]
        assert "Line 0" not in ''.join(result)