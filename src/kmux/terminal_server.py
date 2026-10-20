from collections.abc import Awaitable
from dataclasses import dataclass
import asyncio
from enum import Enum
import logging
import uuid
from pydantic import BaseModel

from aiorwlock import RWLock
import yaml

from .terminal.block_pty_session import BlockPtySession, PtySessionStatus


logger = logging.getLogger(__name__)


# The prefix keeps IDs non-numeric, so YAML, JSON and other loosely typed
# consumers always read them as strings.
_SESSION_ID_PREFIX = 't_'


def _new_session_id() -> str:
    """Returns a new, opaque terminal session ID.

    IDs carry 122 random bits from `uuid.uuid4()` instead of a per-process
    counter, so they do not repeat across server restarts or between server
    instances. A client that remembers an ID from an earlier server process
    cannot reach a different session that later receives the same ID.
    """
    return f'{_SESSION_ID_PREFIX}{uuid.uuid4().hex}'


class TerminalServerConfig(BaseModel):
    session_startup_timeout_seconds: float | None = 10.0
    """The timeout for session startup and initialization.
    If None, the server will wait indefinitely until the session is initialized,
    without holding the global sessions lock."""
    
    general_tool_call_timeout_seconds: float | None = 5.0
    """The generic timeout for tool calls. This applies to all tool calls except `create_session` and `execute_command`."""


class SessionLifecycle(str, Enum):
    STARTING = 'starting'
    READY = 'ready'
    STOPPING = 'stopping'
    TERMINATED = 'terminated'
    FAILED = 'failed'


class SessionOperationCode(str, Enum):
    SERVER_STOPPING = 'SERVER_STOPPING'
    SESSION_NOT_FOUND = 'SESSION_NOT_FOUND'
    SESSION_STATE_CHANGED = 'SESSION_STATE_CHANGED'
    SESSION_STARTING = 'SESSION_STARTING'
    SESSION_STOPPING = 'SESSION_STOPPING'
    SESSION_TERMINATED = 'SESSION_TERMINATED'
    SESSION_FAILED = 'SESSION_FAILED'
    SESSION_START_TIMEOUT = 'SESSION_START_TIMEOUT'
    SESSION_START_FAILED = 'SESSION_START_FAILED'


class SessionOperationError(Exception):
    def __init__(
        self,
        code: SessionOperationCode | str,
        message: str,
        *,
        retryable: bool = False,
        expected_state_version: int | None = None,
        current_state_version: int | None = None,
    ):
        self.code = SessionOperationCode(code)
        self.retryable = retryable
        self.expected_state_version = expected_state_version
        self.current_state_version = current_state_version
        super().__init__(f'{self.code.value}: {message}')


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
    startup_task: asyncio.Task[None] | None = None
    shutdown_stop_pending: bool = False


class SessionNotFoundError(Exception):
    def __init__(self, message: str):
        super().__init__(message)


_LIFECYCLE_OPERATION_ERRORS = {
    SessionLifecycle.STARTING: (
        SessionOperationCode.SESSION_STARTING,
        'is still starting.',
        True,
    ),
    SessionLifecycle.STOPPING: (
        SessionOperationCode.SESSION_STOPPING,
        'is being deleted.',
        True,
    ),
    SessionLifecycle.TERMINATED: (
        SessionOperationCode.SESSION_TERMINATED,
        'has terminated.',
        False,
    ),
    SessionLifecycle.FAILED: (
        SessionOperationCode.SESSION_FAILED,
        'failed to start.',
        False,
    ),
}


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
        self._is_stopping = False
        self._stop_lock = asyncio.Lock()

        # This lock only ensures no race conditions on the dictionary itself,
        # but not on individual session items. Never hold it while waiting on
        # a PTY: aiorwlock makes every new reader wait behind a waiting
        # writer, so one long holder stalls every session in the workspace.
        # Lifecycle checks and transitions are synchronous (no await between
        # check and update), so they are atomic on the event loop.
        self._sessions_lock = RWLock()

        self._config = config
        self._root_password = root_password

        self._cleanup_worker_sentinel = object()
        self._cleanup_worker_closing = False
        self._cleanup_worker_sentinel_enqueued = False
        self._stopped_sessions_id_queue: asyncio.Queue[str | object] = asyncio.Queue()
        self._delete_stopped_sessions_task = asyncio.create_task(self._delete_stopped_sessions_loop())

    @staticmethod
    def _transition_session(
        session_item: PtySessionItem,
        lifecycle: SessionLifecycle,
    ) -> None:
        if session_item.lifecycle != lifecycle:
            session_item.lifecycle = lifecycle
            session_item.state_version += 1

    def _require_ready_session(
        self,
        session_id: str,
        expected_state_version: int | None = None,
    ) -> PtySessionItem:
        session_item = self._session_items.get(session_id)

        if not session_item:
            raise SessionOperationError(
                SessionOperationCode.SESSION_NOT_FOUND,
                f"Session {session_id} not found!",
            )

        if expected_state_version is not None \
            and session_item.state_version != expected_state_version:
            raise SessionOperationError(
                SessionOperationCode.SESSION_STATE_CHANGED,
                f"Session {session_id} state changed from version "
                f"{expected_state_version} to {session_item.state_version}; "
                "refresh session state and retry.",
                retryable=True,
                expected_state_version=expected_state_version,
                current_state_version=session_item.state_version,
            )

        if session_item.lifecycle != SessionLifecycle.READY:
            raise self._lifecycle_error(session_id, session_item.lifecycle)

        return session_item

    @staticmethod
    def _lifecycle_error(
        session_id: str,
        lifecycle: SessionLifecycle,
    ) -> SessionOperationError:
        code, message_suffix, retryable = _LIFECYCLE_OPERATION_ERRORS[lifecycle]
        return SessionOperationError(
            code,
            f"Session {session_id} {message_suffix}",
            retryable=retryable,
        )

    @staticmethod
    def _stopped_session_error(
        session_id: str,
        session_item: PtySessionItem,
    ) -> SessionOperationError | None:
        """The lifecycle error for an operation whose session stopped under it.

        An operation cut short because the terminal closed (deleted or exited)
        reports SESSION_TERMINATED. Returns None while the session is still
        live, so the original error is reported.
        """
        lifecycle = session_item.lifecycle
        if lifecycle != SessionLifecycle.FAILED \
            and session_item.session.session_status == PtySessionStatus.FINISHED:
            lifecycle = SessionLifecycle.TERMINATED
        elif lifecycle not in {
            SessionLifecycle.STOPPING,
            SessionLifecycle.TERMINATED,
            SessionLifecycle.FAILED,
        }:
            return None

        return TerminalServer._lifecycle_error(session_id, lifecycle)

    async def _fail_session_start(
        self,
        session_id: str,
        session_item: PtySessionItem,
        session: BlockPtySession,
    ) -> None:
        async with self._sessions_lock.writer:
            current_session_item = self._session_items.get(session_id)
            if current_session_item is not session_item \
                or session_item.lifecycle != SessionLifecycle.STARTING:
                return
            self._transition_session(session_item, SessionLifecycle.FAILED)

        try:
            await session.stop()
        except Exception:
            logger.exception(
                f'Failed to stop Zsh session {session_id} during startup cleanup.'
            )


    async def create_session(self) -> str:
        """
        Creates a new PTY session,
        returning its ID.

        :return: The ID of the new session: an opaque string that is not
        reused, even after the server restarts.
        """
        async with self._sessions_lock.writer:
            if self._is_stopping:
                raise SessionOperationError(
                    SessionOperationCode.SERVER_STOPPING,
                    'Terminal server is stopping and cannot create sessions.',
                )

            session_id = _new_session_id()
            
            async def signal_deletion():
                async with self._sessions_lock.writer:
                    current_session_item = self._session_items.get(session_id)

                    if current_session_item is None:
                        return

                    # Startup owns failures until READY. In particular, a PTY
                    # closing before its initial marker must be retained as
                    # FAILED rather than queued for terminated-session cleanup.
                    if current_session_item.lifecycle in {
                        SessionLifecycle.STARTING,
                        SessionLifecycle.FAILED,
                    }:
                        return

                    self._transition_session(
                        current_session_item,
                        SessionLifecycle.TERMINATED,
                    )
                    if not self._cleanup_worker_closing \
                        and not self._delete_stopped_sessions_task.done():
                        await self._stopped_sessions_id_queue.put(session_id)
            
            session = BlockPtySession(
                root_password=self._root_password,
                on_session_finished_callback=signal_deletion
            )

            session_item = PtySessionItem(session=session)

            self._session_items[session_id] = session_item
            startup_task = asyncio.create_task(session.start())
            session_item.startup_task = startup_task

        try:
            await asyncio.wait_for(
                startup_task,
                timeout=self._config.session_startup_timeout_seconds,
            )

            if not session.session_initialized:
                raise RuntimeError(
                    f'Zsh session {session_id} startup completed before marker initialization.'
                )

            async with self._sessions_lock.writer:
                current_session_item = self._session_items.get(session_id)
                if current_session_item is session_item \
                    and session_item.lifecycle == SessionLifecycle.STARTING \
                    and not self._is_stopping:
                    if session.session_status != PtySessionStatus.RUNNING:
                        raise RuntimeError(
                            f'Zsh session {session_id} closed before READY transition.'
                        )
                    self._transition_session(session_item, SessionLifecycle.READY)
                    return session_id

                raise SessionOperationError(
                    SessionOperationCode.SERVER_STOPPING,
                    'Terminal server stopped while the session was starting.',
                )
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
                SessionOperationCode.SESSION_START_TIMEOUT,
                f'Session {session_id} did not initialize within '
                f'{self._config.session_startup_timeout_seconds} seconds.',
            ) from error
        except SessionOperationError:
            raise
        except Exception as error:
            await self._fail_session_start(session_id, session_item, session)
            logger.warning(
                f'Zsh session {session_id} failed to initialize: {error}'
            )
            raise SessionOperationError(
                SessionOperationCode.SESSION_START_FAILED,
                f'Session {session_id} failed to initialize.',
            ) from error
    
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
    
    async def submit_command(
        self,
        session_id: str,
        command: str,
        timeout_seconds: float = 330.0,
        expected_state_version: int | None = None,
    ) -> str:
        async with self._sessions_lock.reader:
            session_item = self._require_ready_session(
                session_id,
                expected_state_version,
            )

        # The command wait runs outside the registry lock; deleting the
        # session ends it with the session's lifecycle error.
        # TODO: Parameterize this?
        tool_call_timeout = timeout_seconds + 1

        try:
            result = await asyncio.wait_for(session_item.session.submit_command(command, timeout_seconds=timeout_seconds), timeout=tool_call_timeout)
        except asyncio.TimeoutError:
            logger.warning(f'`BlockPtySession.execute_command` timeout after {tool_call_timeout} seconds (command execution timeout was {timeout_seconds} seconds)')
            return f"Tool call itself timeout after {tool_call_timeout} seconds. Command may or may not have been submitted to the terminal session; consider coming back and checking this terminal session later."
        except Exception as error:
            stopped_error = self._stopped_session_error(session_id, session_item)
            if stopped_error is not None:
                raise stopped_error from error
            raise

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
    
    async def _run_session_operation[T](
        self,
        operation_name: str,
        session_id: str,
        session_item: PtySessionItem,
        operation: Awaitable[T],
        timeout: float | None,
    ) -> T:
        """Runs a PTY operation outside the registry lock.

        A session stopped under the operation (e.g. deleted while a snapshot
        waits) ends it with the session's lifecycle error.
        """
        try:
            return await asyncio.wait_for(operation, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f'`{operation_name}` timeout after {timeout} seconds')
            raise TollCallTimeoutError(timeout)
        except Exception as error:
            stopped_error = self._stopped_session_error(session_id, session_item)
            if stopped_error is not None:
                raise stopped_error from error
            raise

    async def snapshot(
        self, session_id: str, include_all: bool = False, wait_seconds: float = 330.0
    ) -> str:
        async with self._sessions_lock.reader:
            session_item = self._session_items.get(session_id)

            if not session_item:
                raise SessionNotFoundError(f"Session {session_id} not found!")

            if session_item.lifecycle in {
                SessionLifecycle.STOPPING,
                SessionLifecycle.TERMINATED,
            }:
                raise self._lifecycle_error(session_id, session_item.lifecycle)

        # Waiting for the command happens outside the registry lock, so a
        # delete never queues behind it; the delete ends this wait instead.
        snapshot = await self._run_session_operation(
            'snapshot',
            session_id,
            session_item,
            session_item.session.snapshot(
                include_all=include_all, wait_seconds=wait_seconds
            ),
            timeout=wait_seconds + 1,
        )

        return f"""Terminal snapshot ({'including all outputs' if include_all else 'starting from last command input'}):
<snapshot>
{snapshot}
</snapshot>"""
    
    async def send_keys(
        self,
        session_id: str,
        keys: str,
        expected_state_version: int | None = None,
    ):
        async with self._sessions_lock.reader:
            session_item = self._require_ready_session(
                session_id,
                expected_state_version,
            )

        await self._run_session_operation(
            'send_keys',
            session_id,
            session_item,
            session_item.session.send_keys(keys),
            timeout=self._config.general_tool_call_timeout_seconds,
        )
    
    async def enter_root_password(
        self,
        session_id: str,
        expected_state_version: int | None = None,
    ):
        async with self._sessions_lock.reader:
            session_item = self._require_ready_session(
                session_id,
                expected_state_version,
            )

        await self._run_session_operation(
            'enter_root_password',
            session_id,
            session_item,
            session_item.session.enter_root_password(),
            timeout=self._config.general_tool_call_timeout_seconds,
        )
    
    async def delete_session(self, session_id: str):
        """Stops a session's terminal and processes, then removes the session.

        Stopping never waits for the registry lock or for other operations on
        the session: a snapshot or command still waiting on it ends with
        SESSION_TERMINATED once the terminal is closed, and operations that
        arrive meanwhile are rejected with SESSION_STOPPING (or
        SESSION_TERMINATED). Concurrent deletes of one session share the same
        stop. See `PtySession.stop` for which processes are terminated.
        """
        timeout = self._config.general_tool_call_timeout_seconds

        session_item = self._session_items.get(session_id)

        if not session_item:
            raise SessionNotFoundError(f"Session {session_id} not found!")

        if session_item.lifecycle != SessionLifecycle.TERMINATED:
            self._transition_session(session_item, SessionLifecycle.STOPPING)

        try:
            await asyncio.wait_for(
                asyncio.shield(session_item.session.stop()),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f'`delete_session` timeout after {timeout} seconds while stopping session {session_id}')
            raise TollCallTimeoutError(timeout)

        if not await self._remove_session(session_id, session_item, timeout):
            # The session is stopped; only its registry entry remains, and it
            # is hidden from `list_sessions`. Let the cleanup worker remove it
            # rather than keep the caller waiting.
            logger.warning(
                f'`delete_session` stopped session {session_id} but could not '
                f'take the registry lock within {timeout} seconds; '
                'deferring its removal to the cleanup worker'
            )
            self._enqueue_stopped_session(session_id)

    async def _remove_session(
        self,
        session_id: str,
        session_item: PtySessionItem,
        timeout: float | None,
    ) -> bool:
        """Removes a stopped session; returns False if the lock timed out."""
        try:
            async with asyncio.timeout(timeout):
                await self._sessions_lock.writer.acquire()
        except TimeoutError:
            return False

        try:
            if self._session_items.get(session_id) is session_item:
                self._transition_session(session_item, SessionLifecycle.TERMINATED)
                del self._session_items[session_id]
        finally:
            self._sessions_lock.writer.release()

        return True

    def _enqueue_stopped_session(self, session_id: str) -> None:
        if not self._cleanup_worker_closing \
            and not self._delete_stopped_sessions_task.done():
            self._stopped_sessions_id_queue.put_nowait(session_id)
    
    async def _delete_stopped_sessions_loop(self):
        while True:
            session_id = await self._stopped_sessions_id_queue.get()
            if session_id is self._cleanup_worker_sentinel:
                return

            session_item = self._session_items.get(session_id)
            if session_item is None:
                # Already removed, e.g. by `delete_session`.
                continue

            if session_item.session.session_status != PtySessionStatus.FINISHED:
                logger.warning(f'Attempting to delete session {session_id} which is not finished, force stopping it; notice that this is not expected behavior (possible bug)!')
                await session_item.session.stop()

            await self._remove_session(session_id, session_item, timeout=None)
    
    async def stop(self):
        """Stops the server.
        Stops all current terminal sessions.
        """

        async with self._stop_lock:
            async with self._sessions_lock.writer:
                self._is_stopping = True
                self._cleanup_worker_closing = True
                session_items = [
                    session_item for session_item in self._session_items.values()
                    if session_item.lifecycle in {SessionLifecycle.STARTING, SessionLifecycle.READY}
                    or session_item.lifecycle == SessionLifecycle.STOPPING
                    and session_item.shutdown_stop_pending
                ]

                for session_item in session_items:
                    self._transition_session(session_item, SessionLifecycle.STOPPING)
                    session_item.shutdown_stop_pending = True
                    if session_item.startup_task is not None \
                        and not session_item.startup_task.done():
                        session_item.startup_task.cancel()

            if not self._delete_stopped_sessions_task.done():
                if not self._cleanup_worker_sentinel_enqueued:
                    self._stopped_sessions_id_queue.put_nowait(
                        self._cleanup_worker_sentinel
                    )
                    self._cleanup_worker_sentinel_enqueued = True
                await asyncio.shield(self._delete_stopped_sessions_task)

            stop_tasks = [
                asyncio.create_task(session_item.session.stop())
                for session_item in session_items
            ]
            try:
                stop_results = await asyncio.gather(
                    *stop_tasks,
                    return_exceptions=True,
                )
            except asyncio.CancelledError:
                stop_results = await asyncio.gather(
                    *stop_tasks,
                    return_exceptions=True,
                )
                for session_item, result in zip(session_items, stop_results):
                    if not isinstance(result, BaseException):
                        session_item.shutdown_stop_pending = False
                raise

            for session_item, result in zip(session_items, stop_results):
                if not isinstance(result, BaseException):
                    session_item.shutdown_stop_pending = False

            stop_errors = [
                result for result in stop_results
                if isinstance(result, BaseException)
            ]

            for error in stop_errors:
                logger.error('Failed to stop terminal session: %s', error)

            if stop_errors:
                raise stop_errors[0]
