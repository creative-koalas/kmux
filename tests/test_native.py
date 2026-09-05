from __future__ import annotations

from typing import Any

import pytest

from kmux.native import NativeTerminalService, TerminalError


class FakeTerminal:
    last_wait: float | None = None

    async def create_session(self) -> str:
        return "7"

    async def list_sessions(self) -> str:
        return "- id: '7'\n  metadata:\n    lifecycle: ready\n    stateVersion: 1\n"

    async def submit_command(self, **kwargs: Any) -> str:
        self.last_wait = kwargs["timeout_seconds"]
        return f"Command finished: {kwargs['command']}"

    async def snapshot(self, _session_id: str, *, include_all: bool, wait_seconds: float) -> str:
        self.last_wait = wait_seconds
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
@pytest.mark.parametrize("wait_seconds", [-1, 331, float("inf"), float("nan")])
async def test_native_terminal_rejects_unbounded_wait(wait_seconds: float) -> None:
    service = NativeTerminalService(FakeTerminal())  # type: ignore[arg-type]
    with pytest.raises(TerminalError, match="between 0 and 330"):
        await service.submit_command(
            "7",
            "sleep 100",
            wait_seconds=wait_seconds,
            expected_state_version=None,
        )
    with pytest.raises(TerminalError, match="between 0 and 330"):
        await service.snapshot("7", include_all=False, wait_seconds=wait_seconds)


@pytest.mark.asyncio
async def test_native_default_wait_and_explicit_early_return_propagate() -> None:
    terminal = FakeTerminal()
    service = NativeTerminalService(terminal)  # type: ignore[arg-type]
    await service.submit_command("7", "true")
    assert terminal.last_wait == 330
    await service.snapshot("7", include_all=False)
    assert terminal.last_wait == 330
    for wait_seconds in (0, .5, 330):
        await service.submit_command("7", "true", wait_seconds=wait_seconds)
        assert terminal.last_wait == wait_seconds
        await service.snapshot("7", include_all=True, wait_seconds=wait_seconds)
        assert terminal.last_wait == wait_seconds
