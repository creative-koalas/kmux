from dataclasses import dataclass
import asyncio
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


class TollCallTimeoutError(Exception):
    
    def __init__(self, timeout_seconds: float, message: str | None = None):
        self.timeout_seconds = timeout_seconds
        self.message = message or f"Tool call timeout after {timeout_seconds} seconds"


@dataclass
class PtySessionItem:
    session: BlockPtySession
    label: str | None = None
    description: str | None = None
    pending_deletion: bool = False


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

        # This lock only ensures no race conditions on the dictionary itself,
        # but not on individual session items.
        self._sessions_lock = RWLock()

        self._config = config
        self._root_password = root_password

        self._stopped_sessions_id_queue: asyncio.Queue[str] = asyncio.Queue()
        self._delete_stopped_sessions_task = asyncio.create_task(self._delete_stopped_sessions_loop())
    
    async def create_session(self) -> str:
        """
        Creates a new PTY session,
        returning its ID.

        :return: The ID of the new session.
        """
        async with self._sessions_lock.writer:
            session_id = str(self._next_session_id)
            self._next_session_id += 1
            
            session_item = PtySessionItem(
                session=None,
            )

            async def signal_deletion():
                session_item.pending_deletion = True
                await self._stopped_sessions_id_queue.put(session_id)
            
            session = BlockPtySession(
                root_password=self._root_password,
                on_session_finished_callback=signal_deletion
            )

            session_item.session = session

            self._session_items[session_id] = session_item

            try:
                await asyncio.wait_for(session.start(), timeout=self._config.session_startup_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(f'Zsh session {session_id} failed to initialize within {self._config.session_startup_timeout_seconds} seconds; initialization job moved to background.')

        return session_id
    
    async def list_sessions(self) -> str:
        async def lock_guarded_job():
            if len([item for item in self._session_items.values() if not item.pending_deletion]) == 0:
                return "No sessions."

            return yaml.dump([
                {
                    "id": session_id,
                    "metadata": {
                        "label": session_item.label,
                        "description": session_item.description,
                        "runningCommand": session_item.session.get_current_running_command() or "(No command is currently running)"
                    } if session_item.session.session_initialized else "(Session still initializing...)"
                } for session_id, session_item in self._session_items.items()
                if not session_item.pending_deletion
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
    
    async def execute_command(self, session_id: str, command: str, timeout_seconds: float = 5.0) -> str:
        async with self._sessions_lock.reader:
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            # TODO: Parameterize this?
            tool_call_timeout = timeout_seconds + 1
            
            try:
                result = await asyncio.wait_for(session_item.session.execute_command(command, timeout_seconds=timeout_seconds), timeout=tool_call_timeout)
            except asyncio.TimeoutError:
                logger.warning(f'`BlockPtySession.execute_command` timeout after {tool_call_timeout} seconds (command execution timeout was {timeout_seconds} seconds)')
                return f"Tool call itself timeout after {tool_call_timeout} seconds. Command may or may not have been submitted to the terminal session; consider coming back and checking this terminal session later."

            if result.status == 'finished':
                return f"""Command finished in {result.duration_seconds:.2f} seconds with the following output:
<command-output>
{result.output}
</command-output>"""
            else:
                return f"""Command is still running after {result.timeout_seconds:.2f} seconds;
this could mean the command is doing blocking operations (e.g., disk reading, downloading)
or is awaiting input (e.g., password, confirmation).

Current command output:

<command-output>
{result.output}
</command-output>

It is recommended to use `snapshot` on this session later to see command status,
and use `send_keys` or `enter_root_password` to interact with the command if necessary.
You cannot execute another command on this session until the current command finishes or get terminated."""
    
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
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            await session_item.session.send_keys(keys)

        async with self._sessions_lock.reader:
            try:
                return await asyncio.wait_for(lock_guarded_job(), timeout=self._config.general_tool_call_timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning(f'`send_keys` timeout after {self._config.general_tool_call_timeout_seconds} seconds')
                raise TollCallTimeoutError(self._config.general_tool_call_timeout_seconds)
    
    async def enter_root_password(self, session_id: str):
        async def lock_guarded_job():
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
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
            
            session_item.pending_deletion = True
            await session_item.session.stop()
            
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

                del self._session_items[session_id]
    
    async def stop(self):
        """Stops the server.
        Stops all current terminal sessions.
        """

        await asyncio.gather(session_item.session.stop() for session_item in self._session_items.values())
        