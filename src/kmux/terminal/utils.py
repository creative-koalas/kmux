import re

# Match CSI, OSC, DCS, and other escape sequences
ANSI_ESCAPE_RE = re.compile(
    br'''
    \x1B  # ESC
    (?:   # 7-bit C1 Fe (except CSI)
        [@-Z\\-_]
    |
        \[ [0-?]* [ -/]* [@-~]       # CSI
    |
        \] .*? (?:\x07|\x1B\\)       # OSC
    |
        P .*? \x1B\\                 # DCS
    |
        [PX^_] .*? \x1B\\            # SOS/PM/APC
    )
    ''',
    re.VERBOSE | re.DOTALL,
)

def strip_ansi(data: bytes) -> str:
    """Remove ANSI escape codes and return clean text."""
    # also drop other control chars except \n, \r, \t
    no_esc = ANSI_ESCAPE_RE.sub(b'', data)
    return no_esc
