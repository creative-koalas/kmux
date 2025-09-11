from pathlib import Path
import os

import aiofiles
from aiofiles.tempfile import TemporaryDirectory

ZSH_BLOCK_MARKER_REGISTRATION_COMMANDS = \
"""
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
        # TODO
        pass

    async def initialize(self):
        """Initializes the tty session.
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
            # TODO
    
    async def _read_all(self) -> bytes:
        """Reads all the bytes
        """

        # TODO
    
    async def _write_bytes(self, bytes: bytes):
        """Write bytes to the pty session.
        """
        # TODO
