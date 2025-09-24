import importlib
import pytest

app = importlib.import_module('kmux.app')


class DummyTS:
    def __init__(self, root_password=None):
        self.root_password = root_password
        self.stopped = False
    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_lifespan_initializes_and_stops(monkeypatch):
    # Replace TerminalServer with DummyTS to avoid importing real terminal layer
    monkeypatch.setattr(app, 'TerminalServer', DummyTS, raising=True)

    app.set_root_password('pw')
    # FastMCP instance is not used by lifespan; pass None safely
    async with app.lifespan(app.mcp):
        assert isinstance(getattr(app, 'terminal_server', None), DummyTS)
    # After exit, DummyTS.stop should have been awaited
    assert getattr(app, 'terminal_server', None).stopped is True
