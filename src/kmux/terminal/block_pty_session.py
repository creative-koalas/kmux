import asyncio
from enum import Enum
from typing import Literal, Callable, Coroutine
from datetime import datetime, UTC
import logging

from pydantic import BaseModel

from .pty_session import PtySession, PtySessionStatus
from .utils import render_bytes


logger = logging.getLogger()

# === Markers injected by zsh hooks ===
EDITSTART_MARKER = b'\x1bPkmux;EDITSTART;1b3e62c774b44f78898be928a7aa6532\x1b\\'
EDITEND_MARKER   = b'\x1bPkmux;EDITEND;1b3e62c774b44f78898be928a7aa6532\x1b\\'
EXECSTART_MARKER = b'\x1bPkmux;EXECSTART;1b3e62c774b44f78898be928a7aa6532\x1b\\'
EXECEND_MARKER   = b'\x1bPkmux;EXECEND;1b3e62c774b44f78898be928a7aa6532\x1b\\'
EDIT_START_BRACKET_CODE = b'\x1b[200~'
EDIT_END_BRACKET_CODE = b'\x1b[201~'

ZSH_BLOCK_MARKER_REGISTRATION_COMMANDS = r"""
# --- kmux block markers ---

# Hardcoded UUID (hex only)
typeset -g KMUX_BLOCK_MARKER_SALT=1b3e62c774b44f78898be928a7aa6532

# DCS wrappers
typeset -g KMUX_DCS_START=$'\x1bP'   # ESC P
typeset -g KMUX_DCS_END=$'\x1b\\'    # ESC \ (String Terminator)

# Track exec state
typeset -gi KMUX_EXEC_OPEN=0

kmux_preexec() {
  if (( ! KMUX_EXEC_OPEN )); then
    print -n -- "${KMUX_DCS_START}kmux;EXECSTART;${KMUX_BLOCK_MARKER_SALT}${KMUX_DCS_END}"
    KMUX_EXEC_OPEN=1
  fi
}

kmux_precmd() {
  if (( KMUX_EXEC_OPEN )); then
    print -n -- "${KMUX_DCS_START}kmux;EXECEND;${KMUX_BLOCK_MARKER_SALT}${KMUX_DCS_END}"
    KMUX_EXEC_OPEN=0
  fi
}

kmux_line_init() {
  print -n -- "${KMUX_DCS_START}kmux;EDITSTART;${KMUX_BLOCK_MARKER_SALT}${KMUX_DCS_END}"
}

kmux_line_finish() {
  print -n -- "${KMUX_DCS_START}kmux;EDITEND;${KMUX_BLOCK_MARKER_SALT}${KMUX_DCS_END}"
}

# Register hooks
typeset -ga preexec_functions precmd_functions
(( ${preexec_functions[(Ie)kmux_preexec]} )) || preexec_functions+=(kmux_preexec)
(( ${precmd_functions[(Ie)kmux_precmd]}   )) || precmd_functions+=(kmux_precmd)

autoload -Uz add-zle-hook-widget
add-zle-hook-widget zle-line-init kmux_line_init
add-zle-hook-widget zle-line-finish kmux_line_finish
"""


class CommandStatus(Enum):
    EXECUTING = 'executing'
    IDLE = 'idle'


class InvalidOperationError(Exception):
    """Raised when an invalid operation is performed."""

    def __init__(self, message: str):
        super().__init__(message)


class CommandExecutionResult(BaseModel):
    status: Literal['finished', 'timeout']
    output: str | None
    duration_seconds: float | None = None
    timeout_seconds: float | None = None


class CommandBlock(BaseModel):
    command: str
    output: str


class BlockPtySession:
    """
    A pty session with strict block recognition support.
    Blocks are delimited as:
      EDITSTART ... EDITEND   (user input phase)
      EXECSTART ... EXECEND   (execution phase)
    """

    def __init__(
        self,
        root_password: str | None = None,
        on_session_finished_callback: Callable[[], Coroutine[None, None, None]] | None = None,
        screen_width: int = 80,
        screen_height: int = 24,
    ):
        """Creates a BlockPtySession object.
        Note that this method does not start the pty session;
        it only allocates a Python object.

        :param root_password: The root password to use for the pty session.
        :param on_session_finished_callback: The callback to call when the session is finished.
        This callback is called exactly once after the session is finished
        (either normally or explictly closed by calling the `stop` method)
        and the resources are released.
        """
        
        self._pty_session = PtySession(
            zshrc_patch=ZSH_BLOCK_MARKER_REGISTRATION_COMMANDS,
            on_new_output_callback=self._on_new_output,
            on_session_closed_callback=self._on_session_finished,
            screen_width=screen_width,
            screen_height=screen_height,
        )
        
        self._screen_width = screen_width
        self._screen_height = screen_height

        self._cumulative_output: bytes = b''
        self._root_password = root_password
        self._tool_lock = asyncio.Lock()
        self._current_command_finish_execution_event = asyncio.Event()
        self._session_finished_event = asyncio.Event()
        self._on_session_finished_callback = on_session_finished_callback
        self._watch_session_finished_task: asyncio.Task

        self._current_command: str | None = None

    @property
    def session_status(self) -> PtySessionStatus:
        return self._pty_session.status
    
    @property
    def command_status(self) -> CommandStatus:
        return self._get_command_status(self._cumulative_output)

    async def start(self):
        self._watch_session_finished_task = asyncio.create_task(self._watch_session_finished_loop())
        await self._pty_session.start()
    
    async def stop(self):
        self._pty_session.stop()

    async def enter_root_password(self):
        async with self._tool_lock:
            if self._get_command_status(self._cumulative_output) != CommandStatus.EXECUTING:
                raise InvalidOperationError("This method is available only when a command is running, presumably awaiting password input!")
            if self._root_password is None:
                raise ValueError("Root privilege not enabled on this zsh session!")
            await self._pty_session.write_bytes(self._root_password.encode() + b'\r')

    async def send_keys(self, keys: str):
        async with self._tool_lock:
            if self._get_command_status(self._cumulative_output) != CommandStatus.EXECUTING:
                raise InvalidOperationError("This method is available only when a command is running!")
            await self._pty_session.write_bytes(keys.encode())

    async def execute_command(self, command: str, timeout_seconds: float = 5.0) -> CommandExecutionResult:
        async with self._tool_lock:
            if self._get_command_status(self._cumulative_output) != CommandStatus.IDLE:
                raise InvalidOperationError("This method is available only when the zsh session is awaiting command input!")

            self._current_command_finish_execution_event.clear()
            await self._pty_session.write_bytes(b'\x08' * 1000)  # clear junk
            start_time = datetime.now(UTC)
            
            # Use bracketed paste mode to ensure correct behavior when command contains multiple commands
            await self._pty_session.write_bytes(EDIT_START_BRACKET_CODE + command.encode() + EDIT_END_BRACKET_CODE + b'\r')

            self._current_command = command

            try:
                await asyncio.wait_for(self._current_command_finish_execution_event.wait(), timeout=timeout_seconds)
                end_time = datetime.now(UTC)
                duration = (end_time - start_time).total_seconds()

                output = self._parse_output(self._cumulative_output)[-1].output
                
                return CommandExecutionResult(status='finished', output=output, duration_seconds=duration, timeout_seconds=None)
            except asyncio.TimeoutError:
                return CommandExecutionResult(status='timeout', output=None, duration_seconds=None, timeout_seconds=timeout_seconds)
    
    async def snapshot(self, include_all: bool = False) -> str:
        """
        Returns a snapshot of the current state of the pty session.
        
        By default, it only returns the output including and after the last command;
        if include_all is True, it returns the all terminal output starting from terminal startup.
        """
        
        async with self._tool_lock:
            if include_all:
                return self._render(self._cumulative_output)
            
            current_output = self._cumulative_output
            
            command_status = self._get_command_status(current_output)
            if command_status == CommandStatus.EXECUTING:
                last_exec_end_index = current_output.rfind(EXECEND_MARKER)
                return self._render(
                    current_output[
                        last_exec_end_index + len(EXECEND_MARKER) if last_exec_end_index != -1 else 0:
                    ]
                )
            elif command_status == CommandStatus.IDLE:
                # Find the second to last EDITSTART marker
                second_to_last_exec_end_index = current_output.rfind(EXECEND_MARKER, 0, current_output.rfind(EXECEND_MARKER))
                return self._render(
                    current_output[
                        second_to_last_exec_end_index + len(EXECEND_MARKER) if second_to_last_exec_end_index != -1 else 0:
                    ]
                )
            else:
                raise Exception(f'Invalid command status: {command_status}')
    
    def get_current_running_command(self) -> str | None:
        """
        Returns the currently running command.

        :return: The currently running command, or None if no command is running.
        """

        # Finds the currently running command.
        current_output = self._cumulative_output
        
        if self._get_command_status(current_output) != CommandStatus.EXECUTING:
            return None
        
        # TODO: Well, we just can't seem to get the bytes between edit start & end markers to render correctly,.
        # so we're falling back to using a manually managed variable.
        # Of course, this could pose some robustness issues,
        # but given that `send_keys` is denied when there is no running command,
        # this method should work fine in most cases.
        return self._current_command
        
        # command_start = current_output.rfind(EDITSTART_MARKER) + len(EDITSTART_MARKER)
        # command_end = current_output.rfind(EDITEND_MARKER)

        # return self._render(current_output[command_start:command_end])
    
    async def _watch_session_finished_loop(self):
        await self._session_finished_event.wait()

        if self._on_session_finished_callback is not None:
            await self._on_session_finished_callback()
    
    def _render(self, data: bytes) -> str:
        """Renders bytes into human-readable terminal screen.

        :param data: The bytes to render.
        :return: The rendered screen.
        """

        # TODO: Potential performance issue:
        # Right now we're instantiating a new pyte.HistoryScreen each time we call this method.
        
        data = data \
            .replace(EDITSTART_MARKER, b'') \
            .replace(EDITEND_MARKER, b'') \
            .replace(EXECSTART_MARKER, b'') \
            .replace(EXECEND_MARKER, b'')
        
        content = '\n'.join(s.rstrip() for s in render_bytes(data, screen_width=self._screen_width, screen_height=self._screen_height))

        # FIXME: This would remove the deliberately added leading and trailing blank lines and spaces in the original bytes as well
        return content.rstrip()
    
    def _on_new_output(self, data: bytes):
        old_cumulative_output = self._cumulative_output
        self._cumulative_output += data

        # Wake execution waiter when command execution finishes
        if self._get_command_status(old_cumulative_output) == CommandStatus.EXECUTING \
            and self._get_command_status(self._cumulative_output) == CommandStatus.IDLE:
            self._current_command_finish_execution_event.set()

    def _get_command_status(self, cumulative_output: bytes) -> CommandStatus:
        # Look for all markers; if EDITSTART is the last marker, it's idle.
        last_edit_start_index = cumulative_output.rfind(EDITSTART_MARKER)
        last_edit_end_index = cumulative_output.rfind(EDITEND_MARKER)
        last_exec_start_index = cumulative_output.rfind(EXECSTART_MARKER)
        last_exec_end_index   = cumulative_output.rfind(EXECEND_MARKER)

        if last_edit_start_index == max(last_edit_start_index, last_edit_end_index, last_exec_start_index, last_exec_end_index):
            return CommandStatus.IDLE
        else:
            return CommandStatus.EXECUTING

    def _parse_output(self, output: bytes) -> list[CommandBlock]:
        blocks: list[CommandBlock] = []
        
        while True:
            command_start = output.find(EDITSTART_MARKER)
            command_end = output.find(EDITEND_MARKER)
            output_start = output.find(EXECSTART_MARKER)
            output_end = output.find(EXECEND_MARKER)

            if command_start == -1 or command_end == -1 or output_start == -1 or output_end == -1:
                # Incomplete block
                break

            command_start += len(EDITSTART_MARKER)
            output_start += len(EXECSTART_MARKER)
            
            assert command_start <= command_end \
                and command_end <= output_start \
                and output_start <= output_end, "Error: Invalid marker order!"

            command = output[command_start:command_end]
            command_output = output[output_start:output_end]

            output = output[output_end + len(EXECEND_MARKER):]

            blocks.append(
                CommandBlock(
                    command=self._render(command),
                    output=self._render(command_output),
                )
            )

        return blocks

    def _on_session_finished(self):
        self._session_finished_event.set()
