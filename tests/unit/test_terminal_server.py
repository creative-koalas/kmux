import pytest

kmux_ts = pytest.importorskip('kmux.terminal_server')

def test_terminal_server_exports():
    public = [n for n in dir(kmux_ts) if not n.startswith('_')]
    assert len(public) > 0
