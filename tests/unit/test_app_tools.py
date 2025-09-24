import importlib
import pytest

app = importlib.import_module('kmux.app')


class FakeTerminalServer:
    def __init__(self):
        self.labels = {}
        self.descriptions = {}
        self.last_keys = None
        self.entered_root = False
        self.stopped = []

    async def create_session(self) -> str:
        return "123"

    async def list_sessions(self) -> str:
        return "session-list"

    async def update_session_label(self, session_id: str, label: str):
        self.labels[session_id] = label

    async def update_session_description(self, session_id: str, description: str):
        self.descriptions[session_id] = description

    async def execute_command(self, session_id: str, command: str, timeout_seconds: float = 5.0) -> str:
        return (
            "Command finished in 0.01 seconds with the following output:\n"
            "<command-output>\n" + command + "\n</command-output>"
        )

    async def snapshot(self, session_id: str, include_all: bool = False) -> str:
        return (
            f"Terminal snapshot ({'including all outputs' if include_all else 'starting from last command input'}):\n"
            "<snapshot>\nS\n</snapshot>"
        )

    async def send_keys(self, session_id: str, keys: str):
        self.last_keys = keys

    async def enter_root_password(self, session_id: str):
        self.entered_root = True

    async def delete_session(self, session_id: str):
        self.stopped.append(session_id)


@pytest.mark.asyncio
async def test_tools_with_fake_terminal_server(monkeypatch):
    ts = FakeTerminalServer()
    monkeypatch.setattr(app, 'terminal_server', ts, raising=False)

    # set_root_password affects module-level state
    app.set_root_password('secret')
    assert getattr(app, 'root_password', None) == 'secret'

    # create_session
    s = await app.create_session()
    assert 'Session ID' in s and '123' in s

    # list_sessions
    s = await app.list_sessions()
    assert '<sessions>' in s and 'session-list' in s

    # update label/description
    s = await app.update_session_label('1', 'dev')
    assert 'updated' in s.lower()
    s = await app.update_session_description('1', 'desc')
    assert 'updated' in s.lower()
    assert ts.labels['1'] == 'dev' and ts.descriptions['1'] == 'desc'

    # execute_command
    out = await app.execute_command('1', 'echo ok')
    assert '<command-output>' in out and 'echo ok' in out

    # snapshot
    snap = await app.snapshot('1', include_all=False)
    assert '<snapshot>' in snap

    # send_keys / enter_root_password
    s = await app.send_keys('1', 'q')
    assert 'Keys sent' in s
    s = await app.enter_root_password('1')
    assert 'Root password entered' in s

    # delete_session
    s = await app.delete_session('1')
    assert 'Session deleted' in s
