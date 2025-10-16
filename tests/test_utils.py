import os
import sys
import pytest

# Ensure src on path
sys.path.insert(0, os.path.abspath('src'))

from kmux.terminal.utils import render_bytes


@pytest.mark.parametrize(
    'payload,expect_substrings',
    [
        (b'hello\nworld\n', ['hello', 'world']),
        (b"\x1b[31mred\x1b[0m\n", ['red']),
    ],
)
def test_render_bytes_basic(payload, expect_substrings):
    lines = render_bytes(payload, screen_width=80, screen_height=5)
    joined = "\n".join(lines)
    for s in expect_substrings:
        assert s in joined
