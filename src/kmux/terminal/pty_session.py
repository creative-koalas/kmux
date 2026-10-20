"""
TODO: Possible resource leaks on unsuccessful startup
"""

from pathlib import Path
import os
import asyncio
import pty
from typing import Callable
import fcntl
import termios
import struct
import errno
import signal
import psutil
import logging
from enum import Enum
import uuid
import shutil

import aiofiles


logger = logging.getLogger(__name__)


PROCESS_TERMINATION_GRACE_SECONDS = 2.0
"""How long stopping a session waits after SIGHUP/SIGTERM before SIGKILL."""

_PROCESS_KILL_CONFIRM_SECONDS = 1.0
"""How long stopping a session waits for SIGKILL to take effect."""

_PROCESS_EXIT_POLL_SECONDS = 0.05


class PtySessionStatus(Enum):
    NOT_STARTED = 'not_started'
    RUNNING = 'running'
    FINISHED = 'finished'


class PtySessionClosedError(RuntimeError):
    """Raised when writing to a PTY session that is stopping or has closed."""


def _session_members(session_id: int) -> list[tuple[int, int, bool]]:
    """Lists ``(pid, pgid, is_zombie)`` for every process in a POSIX session."""
    members: list[tuple[int, int, bool]] = []
    for pid in psutil.pids():
        try:
            if os.getsid(pid) != session_id:
                continue
            pgid = os.getpgid(pid)
            is_zombie = psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        except (OSError, psutil.Error):
            # Exited while we looked, or not ours to inspect.
            continue
        members.append((pid, pgid, is_zombie))
    return members


def _signal_process_groups(process_groups: set[int], signum: int) -> None:
    # Never signal init or our own group, whatever the enumeration returned.
    own_process_group = os.getpgrp()
    for pgid in sorted(process_groups):
        if pgid <= 1 or pgid == own_process_group:
            continue
        try:
            os.killpg(pgid, signum)
        except (ProcessLookupError, PermissionError):
            pass


class PtySession:
    """
    A primitive zsh pty session.
    Currently, we only support `zsh`.
    """

    def __init__(
        self,
        zshrc_patch: str = "",
        on_new_output_callback: Callable[[bytes], None] = lambda _: None,
        on_session_closed_callback: Callable[[], None] = lambda: None,
        screen_width: int = 80,
        screen_height: int = 24,
        termination_grace_seconds: float = PROCESS_TERMINATION_GRACE_SECONDS,
    ):
        """Creates a PtySession object.
        Notice that this method does not start the pty session;
        it only allocates a Python object.

        :param zshrc_patch: The patch to append to the .zshrc file during startup.
        :param on_new_output_callback: The callback to call when new output is received.
        :param on_session_closed_callback: The callback to call when the session is closed.
        This callback is called exactly once, as soon as the terminal is
        closed (either normally or explictly by calling the `stop` method);
        `stop` may still be terminating the session's processes then.
        :param screen_width: The width of the terminal screen.
        :param screen_height: The height of the terminal screen.
        :param termination_grace_seconds: How long `stop` lets the session's
        processes handle SIGHUP/SIGTERM before it sends SIGKILL.
        """

        self._started = False
        self._finished = False
        self._stop_task: asyncio.Task[None] | None = None
        self._screen_width = screen_width
        self._screen_height = screen_height
        self._termination_grace_seconds = termination_grace_seconds

        # Receiver end for the pty session master FD
        self._rx_q: asyncio.Queue[bytes] = asyncio.Queue()
        # Sender end for the pty session master FD
        self._tx_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._chunk_to_be_written: bytes | None = None
        self._child_exited_event: asyncio.Event = asyncio.Event()

        self._pid: int | None = None
        # Start time of `_pid`, so stop never mistakes a reused PID for the shell.
        self._pid_create_time: float | None = None
        self._master_fd: int | None = None
        self._output_reader_task: asyncio.Task[None] | None = None
        self._close_on_child_exit_task: asyncio.Task[None] | None = None
        self._reap_child_task: asyncio.Task[None] | None = None

        self._zshrc_patch: str = zshrc_patch
        self._on_new_output_callback: Callable[[bytes], None] = on_new_output_callback
        self._on_session_closed_callback: Callable[[], None] = on_session_closed_callback
    
    @property
    def status(self) -> PtySessionStatus:
        if self._finished:
            return PtySessionStatus.FINISHED
        elif not self._started:
            return PtySessionStatus.NOT_STARTED
        else:
            return PtySessionStatus.RUNNING

    @property
    def has_open_resources(self) -> bool:
        """Whether startup has allocated a PTY process or master file descriptor."""
        return self._pid is not None or self._master_fd is not None
    
    async def stop(self):
        """Closes the terminal and terminates every process in its session.

        Every process group in the shell's POSIX session (the shell, the
        foreground job and background, nohup or disowned jobs) gets SIGHUP,
        then SIGTERM and SIGCONT; whatever still runs after
        `termination_grace_seconds` gets SIGKILL. Processes that left the
        session with setsid() (e.g. daemons) are not reached.

        Concurrent and repeated calls share one stop, and each returns once
        it is complete. Cancelling a caller does not cancel the stop.
        """
        if self._stop_task is None:
            if self._finished:
                return
            if not self._started and not self.has_open_resources:
                raise RuntimeError("PTY session not started yet!")
            self._stop_task = asyncio.create_task(self._stop())
        await asyncio.shield(self._stop_task)

    async def write_bytes(self, data: bytes):
        """Writes bytes to the pty session."""
        await self._write_bytes(data)
    

    async def start(self):
        """Starts the tty session."""
        
        if self._started:
            raise RuntimeError("PTY session already started!")

        # Create a temporary zsh config directory and start zsh process with ZDOTDIR set to it
        # Spawn zsh process with ZDOTDIR set to the temporary directory
        tmp_zshrc_directory = Path(f'/tmp/kmux_{uuid.uuid4().hex}').resolve()
        tmp_zshrc_directory.mkdir(parents=True, exist_ok=False)

        try:
            await self._configure_zshrc(tmp_zshrc_directory)

            pid, master_fd = pty.fork()

            if pid == 0:
                # Child process: exec zsh (interactive)
                env = os.environ.copy()
                env["ZDOTDIR"] = str(tmp_zshrc_directory)
                env["NO_GLOBAL_RCS"] = "1"
                os.execvpe("zsh", ["zsh", "-i"], env)
            else:
                # Parent process: store handles
                self._pid = pid
                self._master_fd = master_fd
                try:
                    self._pid_create_time = psutil.Process(pid).create_time()
                except psutil.Error:
                    self._pid_create_time = None

                # Make master FD non-blocking
                flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                # Optionally set initial window size (rows, cols)
                #    You can expose this as an API; here we set a sane default.
                rows, cols = self._screen_height, self._screen_width
                fcntl.ioctl(
                    master_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )

            # Guaranteed to be in the parent process if we're at this point
            # Register self._on_readable to be called whenever the master file descriptor is readable
            asyncio.get_running_loop().add_reader(self._master_fd, self._on_readable)

            # Start the output reader loop
            self._output_reader_task = asyncio.create_task(self._read_output_loop())
            self._close_on_child_exit_task = asyncio.create_task(
                self.close_on_child_exit_loop()
            )

            self._started = True
        finally:
            # Wait for a period of time and then remove the temporary zsh config directory
            async def wait_and_delete_zshrc_directory():
                # TODO: This could be brittle; what if it takes more than 10 seconds for the child to start?
                await asyncio.sleep(10)
                shutil.rmtree(tmp_zshrc_directory, ignore_errors=True)
            
            asyncio.create_task(wait_and_delete_zshrc_directory())

    def _remove_reader_and_writer(self):
        """Removes the reader and writer on the PTY master FD.

        This method is idempotent.
        """
        if self._master_fd is None:
            return

        asyncio.get_running_loop().remove_reader(self._master_fd)
        asyncio.get_running_loop().remove_writer(self._master_fd)

    def _on_child_exited(self):
        # Stops the PTY session when the child process exits
        self._child_exited_event.set()

    def _enable_writer(self):
        asyncio.get_running_loop().add_writer(
            self._master_fd, self._on_writable_or_new_write_data
        )

    def _disable_writer(self):
        asyncio.get_running_loop().remove_writer(self._master_fd)

    def _on_readable(self):
        # Called by event loop when PTY master is readable
        try:
            while True:
                chunk = os.read(self._master_fd, 65536)
                if not chunk:
                    break

                # Guaranteed success since the queue is created with infinite size
                self._rx_q.put_nowait(chunk)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return

            # EIO: slave closed (child exited)
            if e.errno == errno.EIO:
                self._on_child_exited()
                return
            else:
                raise

    def _on_writable_or_new_write_data(self):
        # Called by the event loop when PTY master is writable
        while True:
            try:
                if self._chunk_to_be_written:
                    bytes_written = os.write(self._master_fd, self._chunk_to_be_written)
                    if bytes_written < len(self._chunk_to_be_written):
                        self._chunk_to_be_written = self._chunk_to_be_written[
                            bytes_written:
                        ]
                    else:
                        self._chunk_to_be_written = None

                if self._chunk_to_be_written is None:
                    try:
                        self._chunk_to_be_written = self._tx_q.get_nowait()
                    except asyncio.QueueEmpty:
                        # No more write content (for now); disable writer to avoid busy waiting
                        self._disable_writer()
                        break
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return

                # EIO: slave closed (child exited)
                if e.errno == errno.EIO:
                    self._on_child_exited()
                    return
                else:
                    raise

    def _shell_session_id(self) -> int | None:
        """Returns the shell's POSIX session ID, or None when it cannot be trusted.

        `pty.fork` makes the shell a session leader, so every process started
        in this terminal shares the shell's PID as its session ID unless it
        called setsid() itself. The shell stays unreaped until `_stop`
        finishes, so its PID (and with it the session ID) cannot be reused by
        another process while we signal; the start-time check covers anything
        else reaping it.
        """
        shell_pid = self._pid
        if shell_pid is None:
            return None
        try:
            if os.getsid(shell_pid) != shell_pid:
                return None
            if self._pid_create_time is not None \
                and psutil.Process(shell_pid).create_time() != self._pid_create_time:
                return None
        except (OSError, psutil.Error):
            return None
        return shell_pid

    def _session_process_groups(self, session_id: int) -> set[int]:
        process_groups = {pgid for _, pgid, _ in _session_members(session_id)}
        # The terminal's foreground job, from the TTY itself, in case it was
        # missed while enumerating processes.
        if self._master_fd is not None:
            try:
                foreground_pgid = os.tcgetpgrp(self._master_fd)
                if os.getsid(foreground_pgid) == session_id:
                    process_groups.add(foreground_pgid)
            except OSError:
                pass
        return process_groups

    @staticmethod
    async def _wait_for_session_exit(session_id: int, timeout: float) -> list[int]:
        """Waits for the session's processes to exit; returns those still running."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            running = [
                pid for pid, _, is_zombie in _session_members(session_id)
                if not is_zombie
            ]
            if not running or loop.time() >= deadline:
                return running
            await asyncio.sleep(_PROCESS_EXIT_POLL_SECONDS)

    def _kill_session(self, session_id: int) -> None:
        remaining = _session_members(session_id)
        survivors = [pid for pid, _, is_zombie in remaining if not is_zombie]
        if survivors:
            logger.warning(
                'Killing %d terminal process(es) still running %.1f seconds '
                'after SIGHUP/SIGTERM: %s',
                len(survivors),
                self._termination_grace_seconds,
                survivors,
            )
            _signal_process_groups(
                {pgid for _, pgid, _ in remaining}, signal.SIGKILL
            )

    async def _stop(self):
        # Cancel the output reader task
        if self._output_reader_task is not None:
            self._output_reader_task.cancel()

        # Explicit shutdown removes the FD before the child-exit watcher can
        # observe it, so that watcher must not remain pending forever.
        if self._close_on_child_exit_task is not None \
            and self._close_on_child_exit_task is not asyncio.current_task():
            self._close_on_child_exit_task.cancel()

        # Terminate the whole terminal session, not only the shell: background
        # jobs and the foreground job each run in their own process group, and
        # jobs started with nohup or disowned never get the shell's SIGHUP.
        # Only process groups inside the shell's POSIX session are signalled
        # (a process group never spans sessions), so processes that left the
        # session with setsid() are not reached.
        session_id = self._shell_session_id()
        process_groups: set[int] = set()
        if session_id is not None:
            process_groups = self._session_process_groups(session_id)
            _signal_process_groups(process_groups, signal.SIGHUP)

        # Gracefully close the PTY master FD; this also hangs up the terminal.
        if self._master_fd is not None:
            self._remove_reader_and_writer()

            try:
                os.close(self._master_fd)
            except OSError:
                pass
            finally:
                self._master_fd = None

        if process_groups:
            _signal_process_groups(process_groups, signal.SIGTERM)
            # Stopped jobs only act on SIGTERM once continued.
            _signal_process_groups(process_groups, signal.SIGCONT)

        # The terminal is gone: release everything waiting on it now, rather
        # than after the grace period below.
        self._finished = True
        self._on_session_closed_callback()

        child_pid = self._pid
        try:
            if session_id is not None:
                await self._wait_for_session_exit(
                    session_id, self._termination_grace_seconds
                )
        finally:
            try:
                if session_id is not None:
                    self._kill_session(session_id)

                # Kill the child process if it's still around.
                if child_pid is not None and psutil.pid_exists(child_pid):
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        # The child can exit after `pid_exists` but before `kill`.
                        pass

                # Return only once the session's processes are gone, so a
                # completed stop means they no longer run.
                if session_id is not None:
                    still_running = await self._wait_for_session_exit(
                        session_id, _PROCESS_KILL_CONFIRM_SECONDS
                    )
                    if still_running:
                        logger.error(
                            'Terminal process(es) still running after SIGKILL: %s',
                            still_running,
                        )
            finally:
                self._pid = None
                if child_pid is not None:
                    # Reaping may wait for an uninterruptible child; do not
                    # make a terminal stop wait on that.
                    self._reap_child_task = asyncio.create_task(
                        self._reap_child(child_pid)
                    )
                    self._reap_child_task.add_done_callback(
                        self._consume_reap_child_result
                    )

    async def _reap_child(self, pid: int) -> None:
        """Wait for a child process without blocking the event loop."""
        while True:
            try:
                reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, ProcessLookupError):
                # The child may have been reaped elsewhere or exited before kill.
                return

            if reaped_pid == pid:
                return

            await asyncio.sleep(0.01)

    @staticmethod
    def _consume_reap_child_result(task: asyncio.Task[None]) -> None:
        """Retrieve background reaper failures so asyncio never reports them late."""
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            logger.error(
                "Unexpected error while reaping terminal child process: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _read_output_loop(self):
        while True:
            chunk = await self._rx_q.get()
            self._on_new_output_callback(chunk)

    async def close_on_child_exit_loop(self):
        await self._child_exited_event.wait()
        await self.stop()

    async def _write_bytes(self, data: bytes):
        """Write bytes to the pty session."""

        if self._stop_task is not None or self._finished or self._master_fd is None:
            raise PtySessionClosedError("The terminal session is closed.")

        # Put the data into the write data queue
        await self._tx_q.put(data)

        # Register writer callback
        self._enable_writer()
    
    async def _configure_zshrc(self, directory: Path):
        original_zshrc_path = (
            Path(os.getenv("ZDOTDIR") or Path.home()).resolve() / ".zshrc"
        )

        if original_zshrc_path.is_file():
            async with aiofiles.open(original_zshrc_path, mode="r") as f:
                zshrc_content = await f.read()
        else:
            zshrc_content = ""

        zshrc_content += "\n" + self._zshrc_patch + "\n"

        zshrc_path = directory.resolve() / ".zshrc"

        async with aiofiles.open(zshrc_path, mode="x") as f:
            await f.write(zshrc_content)
