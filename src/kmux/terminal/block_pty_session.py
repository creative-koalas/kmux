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
class _BlockMarker(Enum):
    EDIT_START = b'\x1bPkmux;EDITSTART;1b3e62c774b44f78898be928a7aa6532\x1b\\'
    EDIT_END   = b'\x1bPkmux;EDITEND;1b3e62c774b44f78898be928a7aa6532\x1b\\'
    EXEC_START = b'\x1bPkmux;EXECSTART;1b3e62c774b44f78898be928a7aa6532\x1b\\'
    EXEC_END   = b'\x1bPkmux;EXECEND;1b3e62c774b44f78898be928a7aa6532\x1b\\'


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


class _SessionStatus(Enum):
    EXECUTING = 'executing'
    """A command is currently being executed; last marker is EXECSTART."""
    AWAITING_COMMAND = 'awaiting_command'
    """Awaiting new command; last two markers are EXECEND and EDITSTART."""
    INPUT_COMMAND = 'input_command'
    """Currently inputting command; last two markers are EDITEND and EDITSTART."""
    TRANSIENT_ZSH_PROCESSING = 'parsing_command'
    """Transient state, Zsh is currently processing; last marker is EDITEND or EXECEND"""
    
    _NO_MARKERS = '_no_markers'
    """No block markers found in the output buffer; likely that the session has not received any output yet."""
    
    @property
    def status_string(self) -> str:
        if self == _SessionStatus.EXECUTING:
            return 'executing command'
        elif self == _SessionStatus.AWAITING_COMMAND:
            return 'awaiting new command'
        elif self == _SessionStatus.INPUT_COMMAND:
            return 'awaiting additional input for current incomplete (likely multi-line) command'
        elif self == _SessionStatus.TRANSIENT_ZSH_PROCESSING:
            return 'waiting for Zsh bookkeeping work to complete (should finish automatically shortly)'
        else:
            raise RuntimeError(f'Error: Invalid session status: {self}')


def _extract_markers(cumulative_output: bytes) -> list[_BlockMarker]:
    """
    Extracts the markers from the cumulative buffer and returns them in order.

    :param cumulative_output: The cumulative output from the pty session.
    :return: The markers in order.
    """

    matches = set()

    for marker in _BlockMarker:
        start = 0
        
        while (index := cumulative_output.find(marker.value, start)) != -1:
            matches.add((index, marker))
            start = index + len(marker.value)
    
    matches = [x[1] for x in sorted(matches, key=lambda x: x[0])]

    return matches

class InvalidOperationError(Exception):
    """Raised when an invalid operation is performed."""

    def __init__(self, message: str):
        super().__init__(message)


class CommandSubmissionResult(BaseModel):
    result_type: Literal['finished', 'timeout', 'command_incomplete']
    """
    The type of the command submission result.
    
    - finished: Command finished executing and exited.
    - timeout: Command did not exit within the specified timeout.
    - command_incomplete: Submitted command is incomplete (likely multi-line);
    more input is required before command buffer can be parsed.
    """
    output: str | None
    """The command output (applies only to `finished` and `timeout` result)"""
    command_buffer: str | None
    """The current incomplete command buffer (applies only to `finished`, `timeout` and `command_incomplete` result)"""
    duration_seconds: float | None = None
    """The time it took to execute the command (applies only to `finished` result)"""
    timeout_seconds: float | None = None
    """The timeout duration (applies only to `timeout` result)"""


class _CommandBlock(BaseModel):
    command_parts: list[bytes]
    output: str | None

    @property
    def combined_command(self) -> bytes:
        return b''.join(self.command_parts)


class _ParseState(Enum):
    WAIT_EDIT_START = 'wait_edit_start'
    WAIT_EDIT_END = 'wait_edit_end'
    WAIT_EXEC_START_OR_NEXT_EDIT = 'wait_exec_start_or_next_edit'
    WAIT_EXEC_END = 'wait_exec_end'


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
        self._session_idle_event = asyncio.Event()
        self._session_finished_event = asyncio.Event()
        self._on_session_finished_callback = on_session_finished_callback
        self._watch_session_finished_task: asyncio.Task

        self._session_initialized = False

        # FIXME: Remove this
        self._current_command_parts: list[str] | None = None

    @property
    def session_status(self) -> PtySessionStatus:
        return self._pty_session.status
    
    @property
    def session_initialized(self) -> bool:
        """Whether the session is initialized (i.e., `.zshrc` sourced successfully) and ready to go.
        """
        return self._session_initialized

    async def start(self):
        # FIXME: This does not account for the case where `start` is called by multiple callers simultaneously;
        # Need to add another flag
        if self._session_initialized:
            raise RuntimeError("Session already initialized!")
        
        self._watch_session_finished_task = asyncio.create_task(self._watch_session_finished_loop())
        await self._pty_session.start()

        self._session_initialized = True
    
    async def stop(self):
        self._pty_session.stop()

    async def enter_root_password(self):
        async with self._tool_lock:
            if self._get_session_status(self._cumulative_output) != _SessionStatus.EXECUTING:
                raise InvalidOperationError("This method is available only when a command is running, presumably awaiting password input!")
            if self._root_password is None:
                raise ValueError("Root privilege not enabled on this zsh session!")
            await self._pty_session.write_bytes(self._root_password.encode() + b'\r')

    async def send_keys(self, keys: str):
        async with self._tool_lock:
            if self._get_session_status(self._cumulative_output) != _SessionStatus.EXECUTING:
                raise InvalidOperationError("This method is available only when a command is running!")
            await self._pty_session.write_bytes(keys.encode())

    async def submit_command(self, command: str, timeout_seconds: float = 5.0) -> CommandSubmissionResult:
        async with self._tool_lock:
            session_status = self._get_session_status(self._cumulative_output)
            if session_status not in { _SessionStatus.AWAITING_COMMAND, _SessionStatus.INPUT_COMMAND }:
                raise InvalidOperationError(
                    "This method is available only when the zsh session is awaiting command input; "
                    f"right now the session is {session_status.status_string}."
                )
                
            if session_status == _SessionStatus.AWAITING_COMMAND:
                assert self._current_command_parts is None, "Potential bug: session state is AWAITING_COMMAND but `current_command_parts` is not None"
                self._current_command_parts = [command]
            elif session_status == _SessionStatus.INPUT_COMMAND:
                assert len(self._current_command_parts) > 0, "Potential bug: session state is INPUT_COMMAND but `current_command_parts` is empty"
                self._current_command_parts.append(command)
            
            combined_command_buffer = '\n'.join(self._current_command_parts)

            self._session_idle_event.clear()
            # TODO: Remove this clear junk line?
            await self._pty_session.write_bytes(b'\x08' * 1000)  # clear junk
            start_time = datetime.now(UTC)
            
            # Use bracketed paste mode to ensure correct behavior when command contains multiple commands
            await self._pty_session.write_bytes(EDIT_START_BRACKET_CODE + command.encode() + EDIT_END_BRACKET_CODE + b'\r')

            self._current_command = command

            try:
                await asyncio.wait_for(self._session_idle_event.wait(), timeout=timeout_seconds)
                end_time = datetime.now(UTC)
                duration = (end_time - start_time).total_seconds()

                last_block = self._parse_output(self._cumulative_output)[-1]

                if last_block.output is None:
                    # Incomplete command; command is not executed
                    return CommandSubmissionResult(
                        result_type='command_incomplete',
                        output=None,
                        command_buffer=combined_command_buffer,
                        duration_seconds=duration,
                        timeout_seconds=None
                    )
                else:
                    # Command successfully submitted and sent for execution
                    # TODO: Could there be a case where `last_block.output` is not `None` but the command has not finished executing?
                    return CommandSubmissionResult(
                        result_type='finished',
                        output=last_block.output,
                        command_buffer=combined_command_buffer,
                        duration_seconds=duration,
                        timeout_seconds=None
                    )
            except asyncio.TimeoutError:
                # Command timed out
                # TODO: Does it work for the case where it's the parsing by Zsh that timed out?
                last_block = self._parse_output(self._cumulative_output)[-1]
                
                return CommandSubmissionResult(
                    result_type='timeout',
                    output=last_block.output,
                    command_buffer=combined_command_buffer,
                    duration_seconds=None,
                    timeout_seconds=timeout_seconds
                )
    
    async def snapshot(self, include_all: bool = False) -> str:
        """
        Returns a snapshot of the current state of the pty session.
        
        By default, it only returns the output including and after the last command;
        if include_all is True, it returns the all terminal output starting from terminal startup.
        """
        
        cumulative_output = self._cumulative_output
        if include_all:
            return self._render(cumulative_output)
        
        session_status = self._get_session_status(cumulative_output)
        
        # TODO: Did we handle all the possible cases gracefully?
        if session_status in { _SessionStatus.EXECUTING, _SessionStatus.INPUT_COMMAND }:
            # Render everything after the last EXEC_END marker
            # There's a command currently executing
            last_exec_end_index = cumulative_output.rfind(_BlockMarker.EXEC_END.value)
            return self._render(
                cumulative_output[
                    last_exec_end_index + len(_BlockMarker.EXEC_END.value) if last_exec_end_index != -1 else 0:
                ]
            )
        elif session_status == _SessionStatus.AWAITING_COMMAND:
            # Render everything after the second-to-last EXEC_END marker
            last_exec_end_index = cumulative_output.rfind(_BlockMarker.EXEC_END.value)
            if last_exec_end_index == -1:
                last_exec_end_index = len(cumulative_output)
            render_start = cumulative_output.rfind(_BlockMarker.EXEC_END.value, 0, last_exec_end_index)
            if render_start == -1:
                render_start = 0
            else:
                render_start += len(_BlockMarker.EXEC_END.value)
            
            return self._render(cumulative_output[render_start:])
        else:
            raise NotImplementedError(f'Error: Invalid command status for `snapshot`: {session_status}')
    
    def get_current_running_command(self) -> str | None:
        """
        Returns the currently running command.

        :return: The currently running command, or None if no command is running.
        """

        # TODO: Well, we just can't seem to get the bytes between edit start & end markers to render correctly,.
        # so we're falling back to using a manually managed variable.
        # Of course, this could pose some robustness issues,
        # but given that `send_keys` is denied when there is no running command,
        # this method should work fine in most cases.
        session_status = self._get_session_status(self._cumulative_output)

        if session_status == _SessionStatus.EXECUTING:
            assert self._current_command_parts is not None and len(self._current_command_parts) > 0, \
                "Potential bug: session status is EXECUTING but `current_command_parts` is not a non-empty list"
            return '\n'.join(self._current_command_parts)
        else:
            return None
    
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
            .replace(_BlockMarker.EDIT_START.value, b'') \
            .replace(_BlockMarker.EDIT_END.value, b'') \
            .replace(_BlockMarker.EXEC_START.value, b'') \
            .replace(_BlockMarker.EXEC_END.value, b'')
        
        content = '\n'.join(s.rstrip() for s in render_bytes(data, screen_width=self._screen_width, screen_height=self._screen_height))

        # FIXME: This would remove the deliberately added leading and trailing blank lines and spaces in the original bytes as well
        return content.rstrip()
    
    def _on_new_output(self, data: bytes):
        old_cumulative_output = self._cumulative_output
        self._cumulative_output += data

        # Wake terminal idle waiters when terminal becomes idle
        if (not self._is_session_idle(old_cumulative_output)) \
            and self._is_session_idle(self._cumulative_output):
            # If new state is AWAITING_COMMAND, clear command buffer
            if self._get_session_status(self._cumulative_output) == _SessionStatus.AWAITING_COMMAND:
                self._current_command_parts = None
                
            self._session_idle_event.set()
    
    @staticmethod
    def _is_session_idle(cumulative_output: bytes) -> _SessionStatus:
        # _NO_MARKERS is considered "not idle", since the session is not ready to accept inputs at this point;
        # this is the expected design choice.
        return BlockPtySession._get_session_status(cumulative_output) in { _SessionStatus.AWAITING_COMMAND, _SessionStatus.INPUT_COMMAND }

    @staticmethod
    def _get_session_status(cumulative_output: bytes) -> _SessionStatus:
        markers = _extract_markers(cumulative_output)
        
        if len(markers) >= 2:
            last_markers = (markers[-2], markers[-1])
        elif len(markers) >= 1:
            last_markers = (None, markers[-1])
        else:
            last_markers = (None, None)
        
        if last_markers == (None, None):
            return _SessionStatus._NO_MARKERS
        
        if last_markers[-1] == _BlockMarker.EXEC_START:
            return _SessionStatus.EXECUTING
        elif last_markers == (_BlockMarker.EXEC_END, _BlockMarker.EDIT_START) \
            or last_markers == (None, _BlockMarker.EDIT_START):
            return _SessionStatus.AWAITING_COMMAND
        elif last_markers == (_BlockMarker.EDIT_END, _BlockMarker.EDIT_START):
            return _SessionStatus.INPUT_COMMAND
        elif last_markers[-1] in { _BlockMarker.EDIT_END, _BlockMarker.EXEC_END }:
            return _SessionStatus.TRANSIENT_ZSH_PROCESSING
        else:
            error_message = f'Potential bug: unexpected state, last two markers are {last_markers}'
            logger.error(error_message)
            
            raise RuntimeError(error_message)

    def _parse_output(self, output: bytes) -> list[_CommandBlock]:
        blocks: list[_CommandBlock] = []
        cursor = 0
        state = _ParseState.WAIT_EDIT_START
        command_parts: list[bytes] = []
        iteration = 0

        while cursor < len(output):
            iteration += 1

            # FIXME: Remove this after we confirm that _parse_output works correctly
            logger.debug(f"_parse_output iter={iteration}: state={state}, cursor={cursor}")

            if state == _ParseState.WAIT_EDIT_START:
                edit_start = output.find(_BlockMarker.EDIT_START.value, cursor)
                if edit_start == -1:
                    break

                cursor = edit_start + len(_BlockMarker.EDIT_START.value)
                state = _ParseState.WAIT_EDIT_END

            elif state == _ParseState.WAIT_EDIT_END:
                edit_end = output.find(_BlockMarker.EDIT_END.value, cursor)
                if edit_end == -1:
                    break

                next_edit_start = output.find(_BlockMarker.EDIT_START.value, cursor)
                next_exec_start = output.find(_BlockMarker.EXEC_START.value, cursor)
                next_exec_end = output.find(_BlockMarker.EXEC_END.value, cursor)
                assert next_edit_start == -1 or next_edit_start >= edit_end, "Detected nested EDITSTART before EDITEND"
                assert next_exec_start == -1 or next_exec_start >= edit_end, "Detected EXECSTART before EDITEND"
                assert next_exec_end == -1 or next_exec_end >= edit_end, "Detected EXECEND before EDITEND"

                command_parts.append(output[cursor:edit_end])
                cursor = edit_end + len(_BlockMarker.EDIT_END.value)
                state = _ParseState.WAIT_EXEC_START_OR_NEXT_EDIT

            elif state == _ParseState.WAIT_EXEC_START_OR_NEXT_EDIT:
                assert len(command_parts) > 0, "There must be command input before seeking EXECSTART"
                exec_start = output.find(_BlockMarker.EXEC_START.value, cursor)
                next_edit_start = output.find(_BlockMarker.EDIT_START.value, cursor)

                if exec_start == -1 and next_edit_start == -1:
                    # Need more data to determine outcome.
                    break

                elif next_edit_start != -1 and (exec_start == -1 or next_edit_start < exec_start):
                    # Next marker is EDIT_START; this is a multi-part command
                    cursor = next_edit_start
                    state = _ParseState.WAIT_EDIT_START
                elif exec_start != -1 and (next_edit_start == -1 or exec_start < next_edit_start):
                    # Next marker is EXEC_START
                    cursor = exec_start + len(_BlockMarker.EXEC_START.value)
                    state = _ParseState.WAIT_EXEC_END
                else:
                    raise RuntimeError(f'Invalid state transition: exec_start={exec_start}, next_edit_start={next_edit_start}')

            elif state == _ParseState.WAIT_EXEC_END:
                assert len(command_parts) > 0, "There must be command input before capturing EXEC output"
                exec_end = output.find(_BlockMarker.EXEC_END.value, cursor)
                if exec_end == -1:
                    break

                next_edit_start = output.find(_BlockMarker.EDIT_START.value, cursor)
                next_edit_end = output.find(_BlockMarker.EDIT_END.value, cursor)
                next_exec_start = output.find(_BlockMarker.EXEC_START.value, cursor)
                assert next_edit_start == -1 or next_edit_start >= exec_end, "Detected EDITSTART before EXECEND"
                assert next_edit_end == -1 or next_edit_end >= exec_end, "Detected EDITEND before EXECEND"
                assert next_exec_start == -1 or next_exec_start >= exec_end, "Detected nested EXECSTART before EXECEND"

                command_output = output[cursor:exec_end]
                blocks.append(
                    _CommandBlock(
                        command_parts=command_parts,
                        output=self._render(command_output),
                    )
                )
                
                # This marks the end of this command-output pair; clear command parts
                command_parts = []
                cursor = exec_end + len(_BlockMarker.EXEC_END.value)
                state = _ParseState.WAIT_EDIT_START

            else:
                raise RuntimeError(f"Unknown parse state: {state}")
        
        if state == _ParseState.WAIT_EDIT_END:
            # Waiting for edit end; currently entering command (includes carrying on from a previous incomplete command)
            if len(command_parts) > 0:
                # Currently awaiting additional input from an incomplete command;
                # Add that command as a block
                blocks.append(
                    _CommandBlock(
                        command_parts=command_parts,
                        output=None
                    )
                )
        elif state == _ParseState.WAIT_EXEC_END:
            # Waiting for completion of a currently running command;
            # Add that command as a block
            blocks.append(
                _CommandBlock(
                    command_parts=command_parts,
                    output=self._render(output[cursor:])
                )
            )

        return blocks

    def _on_session_finished(self):
        self._session_finished_event.set()
