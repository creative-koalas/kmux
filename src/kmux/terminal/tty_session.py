from pathlib import Path
import os
import asyncio
import pty
import uuid
import fcntl
import termios
import struct
import errno
import signal

from aiorwlock import RWLock

import aiofiles
from aiofiles.tempfile import TemporaryDirectory

ZSH_BLOCK_MARKER_REGISTRATION_COMMANDS = r"""
# --- minimal block markers (kmux) ---

# Hardcoded UUID (hex only)
typeset -g KMUX_BLOCK_MARKER_SALT=1b3e62c774b44f78898be928a7aa6532

# DCS wrappers
typeset -g KMUX_DCS_START=$'\x1bP'   # ESC P
typeset -g KMUX_DCS_END=$'\x1b\\'    # ESC \  (String Terminator)

kmux_preexec() {
  # Start marker: ESC P kmux;BLOCKSTART;<uuid_hex> ESC \
  print -n -- "${KMUX_DCS_START}kmux;BLOCKSTART;${KMUX_BLOCK_MARKER_SALT}${KMUX_DCS_END}"
}

kmux_precmd() {
  # End marker: ESC P kmux;BLOCKEND;<uuid_hex> ESC \
  print -n -- "${KMUX_DCS_START}kmux;BLOCKEND;${KMUX_BLOCK_MARKER_SALT}${KMUX_DCS_END}"
}

# Register hooks (idempotent-ish)
typeset -ga preexec_functions precmd_functions
(( ${preexec_functions[(Ie)kmux_preexec]} )) || preexec_functions+=(kmux_preexec)
(( ${precmd_functions[(Ie)kmux_precmd]}   )) || precmd_functions+=(kmux_precmd)
"""


class BlockPtySession:
    """
    A pty session with input/output block parsing capabilities.
    Currently, we only support `zsh`.
    Block recognition is done with `preexec` and `precmd` hooks offered by `zsh`.
    """

    def __init__(self):
        self._started = False
        self._stoppped = False

        # Receiver end for the pty session master FD
        self._rx_q: asyncio.Queue[bytes] = asyncio.Queue()
        # Sender end for the pty session master FD
        self._tx_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._chunk_to_be_written: bytes | None = None
        self._child_exited_event: asyncio.Event = asyncio.Event()
        
        self._pid: int
        self._master_fd: int
        self._output_reader_task: asyncio.Task[None]

    async def create_zsh_config_dir(self, parent_dir: Path = Path('/tmp')) -> Path:
        """Creates a zsh config directory with a .zshrc file patched with block markers.
        :param parent_dir: The parent directory to create the zsh config directory in.
        :return: The path to the zsh config directory.
        """

        zsh_config_dir = parent_dir / f'kmux_zsh_config_{uuid.uuid4()}'

        original_zshrc_path = Path(os.getenv("ZDOTDIR") or Path.home()).resolve() / '.zshrc'

        if original_zshrc_path.is_file():
            async with aiofiles.open(original_zshrc_path, mode='r') as f:
                zshrc_content = await f.read()
        else:
            zshrc_content = ""

        zshrc_content += '\n' + ZSH_BLOCK_MARKER_REGISTRATION_COMMANDS + '\n'

        zshrc_path = Path(zsh_config_dir).resolve() / '.zshrc'

        async with aiofiles.open(zshrc_path, mode='x') as f:
            await f.write(zshrc_content)

    async def start(self):
        """Starts the tty session.
        """
        
        # Create a temporary zsh config directory and start zsh process with ZDOTDIR set to it
        async with TemporaryDirectory() as zsh_config_dir:
            original_zshrc_path = Path(os.getenv("ZDOTDIR") or Path.home()).resolve() / '.zshrc'

            if original_zshrc_path.is_file():
                async with aiofiles.open(original_zshrc_path, mode='r') as f:
                    zshrc_content = await f.read()
            else:
                zshrc_content = ""

            zshrc_content += '\n' + ZSH_BLOCK_MARKER_REGISTRATION_COMMANDS + '\n'

            zshrc_path = Path(zsh_config_dir).resolve() / '.zshrc'

            async with aiofiles.open(zshrc_path, mode='x') as f:
                await f.write(zshrc_content)

            # Spawn zsh process with ZDOTDIR set to the temporary directory
            pid, master_fd = pty.fork()

            if pid == 0:
                # Child process: exec zsh (interactive)
                env = os.environ.copy()
                env['ZDOTDIR'] = str(zsh_config_dir)
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
        # Register self._on_writable_or_new_write_data to be called whenever the master file descriptor is writable
        asyncio.get_running_loop().add_writer(self._master_fd, self._on_writable_or_new_write_data)

        # Start the output reader loop
        self._output_reader_task = asyncio.create_task(self._read_output_loop())

        self._started = True
    
    def _remove_reader_and_writer(self):
        """Removes the reader and writer on the PTY master FD.

        This method is idempotent.
        """
        asyncio.get_running_loop().remove_reader(self._master_fd)
        asyncio.get_running_loop().remove_writer(self._master_fd)
    
    def _on_child_exited(self):
        # Finishes the process when the child process exits
        self._remove_reader_and_writer()
        self._child_exited_event.set()
        self._output_reader_task.cancel()

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
                        self._chunk_to_be_written = self._chunk_to_be_written[bytes_written:]
                    else:
                        self._chunk_to_be_written = None

                if self._chunk_to_be_written is None:
                    try:
                        self._chunk_to_be_written = self._tx_q.get_nowait()
                    except asyncio.QueueEmpty:
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
    
    def stop(self):
        if not self._started:
            raise RuntimeError('PTY session not started yet!')
        
        # Cancel the output reader task
        self._output_reader_task.cancel()
        
        # Gracefully close the PTY master FD
        self._remove_reader_and_writer()

        try:
            os.close(self._master_fd)
        except OSError:
            pass
        
        # Kill the child process if it's still running
        if not self._child_exited_event.is_set():
            os.kill(self._pid, signal.SIGKILL)
        
        self._stopped = True
    
    async def _read_output_loop(self):
        # TODO
        pass
    
    async def _write_bytes(self, data: bytes):
        """Write bytes to the pty session.
        """
        
        # Put the data into the write data queue
        await self._tx_q.put(data)

        # Trigger the writer to be called
        self._on_writable_or_new_write_data()
    