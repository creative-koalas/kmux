from dataclasses import dataclass
import asyncio
from enum import Enum
import logging
from pydantic import BaseModel

from aiorwlock import RWLock
import yaml

from .terminal.block_pty_session import BlockPtySession, PtySessionStatus


logger = logging.getLogger(__name__)


class TerminalServerConfig(BaseModel):
    session_startup_timeout_seconds: float | None = 10.0
    """The timeout for session startup and initialization.
    If None, the server will wait and hold the lock indefinitely until the session is initialized
    (this is not recommended)."""
    
    general_tool_call_timeout_seconds: float | None = 5.0
    """The generic timeout for tool calls. This applies to all tool calls except `create_session` and `execute_command`."""


class SessionLifecycle(str, Enum):
    STARTING = 'starting'
    READY = 'ready'
    STOPPING = 'stopping'
    TERMINATED = 'terminated'
    FAILED = 'failed'


class SessionOperationError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.retryable = retryable


class TollCallTimeoutError(Exception):
    
    def __init__(self, timeout_seconds: float, message: str | None = None):
        self.timeout_seconds = timeout_seconds
        self.message = message or f"Tool call timeout after {timeout_seconds} seconds"
        super().__init__(self.message)


@dataclass
class PtySessionItem:
    session: BlockPtySession
    lifecycle: SessionLifecycle = SessionLifecycle.STARTING
    state_version: int = 0
    label: str | None = None
    description: str | None = None


class SessionNotFoundError(Exception):
    def __init__(self, message: str):
        super().__init__(message)


class TerminalServer:
    
    def __init__(
        self,
        config: TerminalServerConfig = TerminalServerConfig(),
        root_password: str | None = None
    ):
        """Creates a TerminalServer object.

        :param root_password: The root password to use for the pty session.
        If None, root privilege will not be enabled.
        """

        self._session_items: dict[str, PtySessionItem] = {}
        self._next_session_id = 0
        self._is_stopping = False

        # This lock only ensures no race conditions on the dictionary itself,
        # but not on individual session items.
        self._sessions_lock = RWLock()

        self._config = config
        self._root_password = root_password

        self._stopped_sessions_id_queue: asyncio.Queue[str] = asyncio.Queue()
        self._delete_stopped_sessions_task = asyncio.create_task(self._delete_stopped_sessions_loop())

    @staticmethod
    def _transition_session(
        session_item: PtySessionItem,
        lifecycle: SessionLifecycle,
    ) -> None:
        if session_item.lifecycle != lifecycle:
            session_item.lifecycle = lifecycle
            session_item.state_version += 1

    def _require_ready_session(self, session_id: str) -> PtySessionItem:
        session_item = self._session_items.get(session_id)

        if not session_item:
            raise SessionNotFoundError(f"Session {session_id} not found!")

        if session_item.lifecycle == SessionLifecycle.STARTING:
            raise SessionOperationError(
                'SESSION_STARTING',
                f"Session {session_id} is still starting.",
                retryable=True,
            )

        if session_item.lifecycle == SessionLifecycle.STOPPING:
            raise SessionOperationError(
                'SESSION_STOPPING',
                f"Session {session_id} is being deleted.",
                retryable=True,
            )

        if session_item.lifecycle == SessionLifecycle.TERMINATED:
            raise SessionOperationError(
                'SESSION_TERMINATED',
                f"Session {session_id} has terminated.",
            )

        if session_item.lifecycle == SessionLifecycle.FAILED:
            raise SessionOperationError(
                'SESSION_FAILED',
                f"Session {session_id} failed to start.",
            )

        return session_item

    async def _fail_session_start(
        self,
        session_id: str,
        session_item: PtySessionItem,
        session: BlockPtySession,
    ) -> None:
        try:
            await session.stop()
        except Exception:
            logger.exception(
                f'Failed to stop Zsh session {session_id} during startup cleanup.'
            )

        self._transition_session(session_item, SessionLifecycle.FAILED)
    
    async def create_session(self) -> str:
        """
        Creates a new PTY session,
        returning its ID.

        :return: The ID of the new session.
        """
        async with self._sessions_lock.writer:
            if self._is_stopping:
                raise SessionOperationError(
                    'SERVER_STOPPING',
                    'Terminal server is stopping and cannot create sessions.',
                )

            session_id = str(self._next_session_id)
            self._next_session_id += 1
            
            session_item = PtySessionItem(
                session=None,
            )

            async def signal_deletion():
                async with self._sessions_lock.writer:
                    current_session_item = self._session_items.get(session_id)

                    if current_session_item is None:
                        return

                    # A startup failure is intentionally retained so callers can
                    # inspect it. Its cleanup stop also emits this callback.
                    if current_session_item.lifecycle == SessionLifecycle.FAILED:
                        return

                    self._transition_session(
                        current_session_item,
                        SessionLifecycle.TERMINATED,
                    )
                    await self._stopped_sessions_id_queue.put(session_id)
            
            session = BlockPtySession(
                root_password=self._root_password,
                on_session_finished_callback=signal_deletion
            )

            session_item.session = session

            self._session_items[session_id] = session_item

            try:
                await asyncio.wait_for(session.start(), timeout=self._config.session_startup_timeout_seconds)
                self._transition_session(session_item, SessionLifecycle.READY)
            except asyncio.CancelledError:
                await self._fail_session_start(session_id, session_item, session)
                raise
            except asyncio.TimeoutError as error:
                await self._fail_session_start(session_id, session_item, session)
                logger.warning(
                    f'Zsh session {session_id} failed to initialize within '
                    f'{self._config.session_startup_timeout_seconds} seconds.'
                )
                raise SessionOperationError(
                    'SESSION_START_TIMEOUT',
                    f'Session {session_id} did not initialize within '
                    f'{self._config.session_startup_timeout_seconds} seconds.',
                ) from error
            except Exception as error:
                await self._fail_session_start(session_id, session_item, session)
                logger.warning(
                    f'Zsh session {session_id} failed to initialize: {error}'
                )
                raise SessionOperationError(
                    'SESSION_START_FAILED',
                    f'Session {session_id} failed to initialize.',
                ) from error

        return session_id
    
    async def list_sessions(self) -> str:
        async def lock_guarded_job():
            if len([
                item for item in self._session_items.values()
                if item.lifecycle not in {SessionLifecycle.STOPPING, SessionLifecycle.TERMINATED}
            ]) == 0:
                return "No sessions."

            return yaml.dump([
                {
                    "id": session_id,
                    "metadata": {
                        "label": session_item.label,
                        "description": session_item.description,
                        "lifecycle": session_item.lifecycle.value,
                        "stateVersion": session_item.state_version,
                        "runningCommand": (
                            "(Session failed to initialize.)"
                            if session_item.lifecycle == SessionLifecycle.FAILED
                            else session_item.session.get_current_running_command()
                            or "(No command is currently running)"
                            if session_item.session.session_initialized
                            else "(Session still initializing...)"
                        ),
                    },
                } for session_id, session_item in self._session_items.items()
                if session_item.lifecycle not in {SessionLifecycle.STOPPING, SessionLifecycle.TERMINATED}
            ], sort_keys=False, indent=2)

        async with self._sessions_lock.reader:
            try:
                return await asyncio.wait_for(lock_guarded_job(), timeout=self._config.general_tool_call_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(f'`list_sessions` timeout after {self._config.general_tool_call_timeout_seconds} seconds')
                raise TollCallTimeoutError(self._config.general_tool_call_timeout_seconds)
    
    async def update_session_label(self, session_id: str, label: str):
        async def lock_guarded_job():
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            session_item.label = label

        async with self._sessions_lock.reader:
            try:
                return await asyncio.wait_for(lock_guarded_job(), timeout=self._config.general_tool_call_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(f'`update_session_label` timeout after {self._config.general_tool_call_timeout_seconds} seconds')
                raise TollCallTimeoutError(self._config.general_tool_call_timeout_seconds)

    async def update_session_description(self, session_id: str, description: str):
        async def lock_guarded_job():
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            session_item.description = description

        async with self._sessions_lock.reader:
            try:
                return await asyncio.wait_for(lock_guarded_job(), timeout=self._config.general_tool_call_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(f'`update_session_description` timed out after {self._config.general_tool_call_timeout_seconds} seconds')
                raise TollCallTimeoutError(self._config.general_tool_call_timeout_seconds)
    
    async def submit_command(self, session_id: str, command: str, timeout_seconds: float = 5.0) -> str:
        async with self._sessions_lock.reader:
            session_item = self._require_ready_session(session_id)
            
            # TODO: Parameterize this?
            tool_call_timeout = timeout_seconds + 1
            
            try:
                result = await asyncio.wait_for(session_item.session.submit_command(command, timeout_seconds=timeout_seconds), timeout=tool_call_timeout)
            except asyncio.TimeoutError:
                logger.warning(f'`BlockPtySession.execute_command` timeout after {tool_call_timeout} seconds (command execution timeout was {timeout_seconds} seconds)')
                return f"Tool call itself timeout after {tool_call_timeout} seconds. Command may or may not have been submitted to the terminal session; consider coming back and checking this terminal session later."

            if result.result_type == 'finished':
                return f"""Command finished in {result.duration_seconds:.2f} seconds.

Executed command buffer:
<command>
{result.command_buffer}
</command>

Command output:
<command-output>
{result.output}
</command-output>"""
            elif result.result_type == 'timeout':
                return f"""Command is still running after {result.timeout_seconds:.2f} seconds;
this could mean the command is doing blocking operations (e.g., disk reading, downloading)
or is awaiting input (e.g., password, confirmation).

Currently executing command buffer:
<command>
{result.command_buffer}
</command>

Current command output:

<command-output>
{result.output}
</command-output>

It is recommended to use `snapshot` on this session later to see command status,
and use `send_keys` or `enter_root_password` to interact with the command if necessary.
You cannot execute another command on this session until the current command finishes or get terminated."""
            elif result.result_type == 'command_incomplete':
                return f"""Current command buffer is incomplete for parsing and execution;
call `submit_command` again to complete the command and submit for execution.

Current command buffer:

<command>
{result.command_buffer}
</command>
"""
    
    async def snapshot(self, session_id: str, include_all: bool = False) -> str:
        
        async def lock_guarded_job():
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            snapshot = await session_item.session.snapshot(include_all=include_all)

            return f"""Terminal snapshot ({'including all outputs' if include_all else 'starting from last command input'}):
<snapshot>
{snapshot}
</snapshot>"""

        async with self._sessions_lock.reader:
            try:
                return await asyncio.wait_for(lock_guarded_job(), timeout=self._config.general_tool_call_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(f'`snapshot` timeout after {self._config.general_tool_call_timeout_seconds} seconds')
                raise TollCallTimeoutError(self._config.general_tool_call_timeout_seconds)
    
    async def send_keys(self, session_id: str, keys: str):
        async def lock_guarded_job():
            session_item = self._require_ready_session(session_id)
            
            await session_item.session.send_keys(keys)

        async with self._sessions_lock.reader:
            try:
                return await asyncio.wait_for(lock_guarded_job(), timeout=self._config.general_tool_call_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(f'`send_keys` timeout after {self._config.general_tool_call_timeout_seconds} seconds')
                raise TollCallTimeoutError(self._config.general_tool_call_timeout_seconds)
    
    async def enter_root_password(self, session_id: str):
        async def lock_guarded_job():
            session_item = self._require_ready_session(session_id)
            
            await session_item.session.enter_root_password()

        async with self._sessions_lock.reader:
            try:
                return await asyncio.wait_for(lock_guarded_job(), timeout=self._config.general_tool_call_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(f'`enter_root_password` timeout after {self._config.general_tool_call_timeout_seconds} seconds')
                raise TollCallTimeoutError(self._config.general_tool_call_timeout_seconds)
    
    async def delete_session(self, session_id: str):
        async def lock_guarded_job():
            session_item = self._session_items.get(session_id)
            
            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            was_failed = session_item.lifecycle == SessionLifecycle.FAILED
            self._transition_session(session_item, SessionLifecycle.STOPPING)
            await session_item.session.stop()

            if was_failed:
                # Startup cleanup already consumed this session's close callback
                # to preserve FAILED for diagnostics, so delete it explicitly.
                self._transition_session(session_item, SessionLifecycle.TERMINATED)
                del self._session_items[session_id]
                return
            
            # No need to delete the session;
            # deletion is signaled by the callback invoked when the session is stopped,
            # and the session will be subsequently deleted by the custom garbage collection system
        
        async with self._sessions_lock.writer:
            try:
                return await asyncio.wait_for(lock_guarded_job(), timeout=self._config.general_tool_call_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(f'`delete_session` timeout after {self._config.general_tool_call_timeout_seconds} seconds')
                raise TollCallTimeoutError(self._config.general_tool_call_timeout_seconds)
    
    async def _delete_stopped_sessions_loop(self):
        while True:
            session_id = await self._stopped_sessions_id_queue.get()

            async with self._sessions_lock.writer:
                if session_id not in self._session_items:
                    logger.warning(f'Session with ID {session_id} not found, skipping deletion')
                    continue
                
                if self._session_items[session_id].session.session_status != PtySessionStatus.FINISHED:
                    logger.warning(f'Attempting to delete session {session_id} which is not finished, force stopping it; notice that this is not expected behavior (possible bug)!')
                    await self._session_items[session_id].session.stop()

                self._transition_session(
                    self._session_items[session_id],
                    SessionLifecycle.TERMINATED,
                )

                del self._session_items[session_id]
    
    async def stop(self):
        """Stops the server.
        Stops all current terminal sessions.
        """

        async with self._sessions_lock.writer:
            self._is_stopping = True
            session_items = [
                session_item for session_item in self._session_items.values()
                if session_item.lifecycle != SessionLifecycle.TERMINATED
            ]

            for session_item in session_items:
                self._transition_session(session_item, SessionLifecycle.STOPPING)

        stop_results = await asyncio.gather(
            *(session_item.session.stop() for session_item in session_items),
            return_exceptions=True,
        )
        stop_errors = [
            result for result in stop_results
            if isinstance(result, BaseException)
        ]

        for error in stop_errors:
            logger.error('Failed to stop terminal session: %s', error)

        if stop_errors:
            raise stop_errors[0]
