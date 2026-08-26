from __future__ import annotations

from typing import Any

import yaml

from .terminal_server import SessionOperationError, TerminalServer


class TerminalError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class NativeTerminalService:
    """Structured terminal operations backed by KMUX's PTY engine."""

    def __init__(self, terminal: TerminalServer) -> None:
        self._terminal = terminal

    async def close(self) -> None:
        await self._terminal.stop()

    async def create_session(self) -> dict[str, Any]:
        try:
            session_id = await self._terminal.create_session()
        except Exception as exc:
            raise _terminal_error(exc) from exc
        return {"session_id": session_id, "lifecycle": "ready", "state_version": 1}

    async def list_sessions(self) -> list[dict[str, Any]]:
        try:
            raw = await self._terminal.list_sessions()
        except Exception as exc:
            raise _terminal_error(exc) from exc
        if raw.strip() == "No sessions.":
            return []
        decoded = yaml.safe_load(raw)
        if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
            raise TerminalError("INVALID_TERMINAL_STATE", "KMUX returned invalid session state.")
        return decoded

    async def update_session(
        self,
        session_id: str,
        *,
        label: str | None,
        description: str | None,
        update_label: bool,
        update_description: bool,
    ) -> dict[str, Any]:
        try:
            if update_label:
                await self._terminal.update_session_label(session_id, label or "")
            if update_description:
                await self._terminal.update_session_description(session_id, description or "")
        except Exception as exc:
            raise _terminal_error(exc) from exc
        return {
            "session_id": session_id,
            "label": label if update_label else None,
            "description": description if update_description else None,
        }

    async def delete_session(self, session_id: str) -> dict[str, str]:
        try:
            await self._terminal.delete_session(session_id)
        except Exception as exc:
            raise _terminal_error(exc) from exc
        return {"session_id": session_id}

    async def submit_command(
        self,
        session_id: str,
        command: str,
        *,
        wait_seconds: float,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        if not command.strip():
            raise TerminalError("INVALID_COMMAND", "command is required.")
        if wait_seconds < 0 or wait_seconds > 10:
            raise TerminalError("INVALID_TIMEOUT", "wait_seconds must be between 0 and 10.")
        try:
            transcript = await self._terminal.submit_command(
                session_id=session_id,
                command=command,
                timeout_seconds=wait_seconds,
                expected_state_version=expected_state_version,
            )
        except Exception as exc:
            raise _terminal_error(exc) from exc
        status = "running" if "still running" in transcript else "incomplete" if "incomplete" in transcript else "completed"
        return {
            "session_id": session_id,
            "status": status,
            "transcript": transcript,
        }

    async def snapshot(self, session_id: str, *, include_all: bool) -> dict[str, Any]:
        try:
            transcript = await self._terminal.snapshot(session_id, include_all=include_all)
        except Exception as exc:
            raise _terminal_error(exc) from exc
        return {
            "session_id": session_id,
            "include_all": include_all,
            "transcript": transcript,
        }

    async def send_input(
        self,
        session_id: str,
        data: str,
        *,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        if not data:
            raise TerminalError("INVALID_INPUT", "input data is required.")
        try:
            await self._terminal.send_keys(
                session_id=session_id,
                keys=data,
                expected_state_version=expected_state_version,
            )
        except Exception as exc:
            raise _terminal_error(exc) from exc
        return {"session_id": session_id, "bytes_sent": len(data.encode("utf-8"))}

    async def authenticate_privilege(
        self,
        session_id: str,
        *,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        try:
            await self._terminal.enter_root_password(
                session_id=session_id,
                expected_state_version=expected_state_version,
            )
        except Exception as exc:
            raise _terminal_error(exc) from exc
        return {"session_id": session_id, "credential_submitted": True}


def _terminal_error(error: Exception) -> TerminalError:
    if isinstance(error, SessionOperationError):
        return TerminalError(error.code.value, str(error), retryable=error.retryable)
    return TerminalError(type(error).__name__.upper(), str(error) or "Terminal operation failed.")
