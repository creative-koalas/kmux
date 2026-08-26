from __future__ import annotations

from typing import Any

import pytest

from kmux.native import NativeTerminalService, TerminalError


class FakeTerminal:
    async def create_session(self) -> str:
        return "7"

    async def list_sessions(self) -> str:
        return "- id: '7'\n  metadata:\n    lifecycle: ready\n    stateVersion: 1\n"

    async def submit_command(self, **kwargs: Any) -> str:
        return f"Command finished: {kwargs['command']}"

    async def snapshot(self, _session_id: str, *, include_all: bool) -> str:
        return "all" if include_all else "latest"

    async def update_session_label(self, _session_id: str, _label: str) -> None: ...

    async def update_session_description(self, _session_id: str, _description: str) -> None: ...

    async def delete_session(self, _session_id: str) -> None: ...

    async def send_keys(self, **_kwargs: Any) -> None: ...

    async def enter_root_password(self, **_kwargs: Any) -> None: ...

    async def stop(self) -> None: ...


@pytest.mark.asyncio
async def test_native_terminal_returns_structured_session_and_command_results() -> None:
    service = NativeTerminalService(FakeTerminal())  # type: ignore[arg-type]
    assert (await service.create_session())["session_id"] == "7"
    assert (await service.list_sessions())[0]["metadata"]["stateVersion"] == 1
    result = await service.submit_command(
        "7",
        "pwd",
        wait_seconds=5,
        expected_state_version=1,
    )
    assert result["status"] == "completed"
    assert "pwd" in result["transcript"]


@pytest.mark.asyncio
async def test_native_terminal_rejects_unbounded_wait() -> None:
    service = NativeTerminalService(FakeTerminal())  # type: ignore[arg-type]
    with pytest.raises(TerminalError, match="between 0 and 10"):
        await service.submit_command(
            "7",
            "sleep 100",
            wait_seconds=60,
            expected_state_version=None,
        )
