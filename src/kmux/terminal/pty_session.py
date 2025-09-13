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


class PtySessionStatus(Enum):
    NOT_STARTED = 'not_started'
    RUNNING = 'running'
    FINISHED = 'finished'


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
    ):
        """Creates a PtySession object.
        Notice that this method does not start the pty session;
        it only allocates a Python object.

        :param zshrc_patch: The patch to append to the .zshrc file during startup.
        :param on_new_output_callback: The callback to call when new output is received.
        :param on_session_closed_callback: The callback to call when the session is closed.
        This callback is called exactly once after the session is closed
        (either normally or explictly closed by calling the `stop` method)
        and the resources are released.
        """

        self._started = False
        self._finished = False

        # Receiver end for the pty session master FD
        self._rx_q: asyncio.Queue[bytes] = asyncio.Queue()
        # Sender end for the pty session master FD
        self._tx_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._chunk_to_be_written: bytes | None = None
        self._child_exited_event: asyncio.Event = asyncio.Event()

        self._pid: int
        self._master_fd: int
        self._output_reader_task: asyncio.Task[None]
        self._close_on_child_exit_task: asyncio.Task[None]

        self._zshrc_patch: str = zshrc_patch
        self._on_new_output_callback: Callable[[bytes], None] = on_new_output_callback
        self._on_session_closed_callback: Callable[[], None] = on_session_closed_callback
    
    @property
    def status(self) -> PtySessionStatus:
        if not self._started:
            return PtySessionStatus.NOT_STARTED
        elif not self._finished:
            return PtySessionStatus.RUNNING
        else:
            return PtySessionStatus.FINISHED
    
    def stop(self):
        self._stop()
    
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
                os.execvpe("zsh", ["zsh", "-i"], env)
            else:
                # Parent process: store handles
                self._pid = pid
                self._master_fd = master_fd

                # Make master FD non-blocking
                flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                # Optionally set initial window size (rows, cols)
                #    You can expose this as an API; here we set a sane default.
                rows, cols = 40, 120
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

    def _stop(self):
        if self._finished:
            # Already finished; return
            return

        if not self._started:
            raise RuntimeError("PTY session not started yet!")

        # Cancel the output reader task
        self._output_reader_task.cancel()

        # Gracefully close the PTY master FD
        self._remove_reader_and_writer()

        try:
            os.close(self._master_fd)
        except OSError:
            pass

        # Kill the child process if it's still around
        if psutil.pid_exists(self._pid):
            os.kill(self._pid, signal.SIGKILL)

        self._finished = True
        self._on_session_closed_callback()

    async def _read_output_loop(self):
        while True:
            chunk = await self._rx_q.get()
            self._on_new_output_callback(chunk)

    async def close_on_child_exit_loop(self):
        await self._child_exited_event.wait()
        self._stop()

    async def _write_bytes(self, data: bytes):
        """Write bytes to the pty session."""

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
