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
        async with self._sessions_lock.reader:
            if len([item for item in self._session_items.values() if not item.pending_deletion]) == 0:
                return "No sessions."

            return yaml.dump([
                {
                    "id": session_id,
                    "label": session_item.label,
                    "description": session_item.description,
                    "runningCommand": session_item.session.get_current_running_command() or "(No command is currently running)",
                } for session_id, session_item in self._session_items.items()
                if not session_item.pending_deletion
            ], sort_keys=False, indent=2)
    
    async def update_session_label(self, session_id: str, label: str):
        async with self._sessions_lock.reader:
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            session_item.label = label

    async def update_session_description(self, session_id: str, description: str):
        async with self._sessions_lock.reader:
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            session_item.description = description
    
    async def execute_command(self, session_id: str, command: str, timeout_seconds: float = 5.0) -> str:
        async with self._sessions_lock.reader:
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            result = await session_item.session.execute_command(command, timeout_seconds=timeout_seconds)

            if result.status == 'finished':
                return f"""Command finished in {result.duration_seconds:.2f} seconds with the following output:
<command-output>
{result.output}
</command-output>"""
            else:
                return f"""Command timed out after {result.timeout_seconds:.2f} seconds (i.e., still running)."""
    
    async def snapshot(self, session_id: str, include_all: bool = False) -> str:
        async with self._sessions_lock.reader:
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            snapshot = await session_item.session.snapshot(include_all=include_all)

            return f"""Terminal snapshot ({'including all outputs' if include_all else 'starting from last command input'}):
<snapshot>
{snapshot}
</snapshot>"""
    
    async def send_keys(self, session_id: str, keys: str):
        async with self._sessions_lock.reader:
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            await session_item.session.send_keys(keys)
    
    async def enter_root_password(self, session_id: str):
        async with self._sessions_lock.reader:
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            await session_item.session.enter_root_password()
    
    async def delete_session(self, session_id: str):
        async with self._sessions_lock.writer:
            session_item = self._session_items.get(session_id)
            
            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")
            
            session_item.pending_deletion = True
            await session_item.session.stop()
            
            # No need to delete the session;
            # deletion is signaled by the callback invoked when the session is stopped,
            # and the session will be subsequently deleted by the custom garbage collection system
    
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
        