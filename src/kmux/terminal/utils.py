import logging

import pyte


logger = logging.getLogger(__name__)


def _line_to_text(line: pyte.screens.StaticDefaultDict[int, pyte.screens.Char]) -> str:
    # history stores lines as lists of Char; extract .data
    return "".join(line[key].data for key in sorted(line.keys()))


def render_bytes(data: bytes, screen_width: int = 80, screen_height: int = 24) -> list[str]:
    """Renders bytes as a terminal screen.

    :param data: The bytes to render.
    :param screen_width: The width of the terminal screen.
    :param screen_height: The height of the terminal screen.
    :return: The rendered screen. Each item is a row.
    """
    
    # FIXME: Is this way of counting lines robust enough?
    lines = max(len(data.split(b'\n')), len(data.split(b'\r')), len(data.split(b'\r\n')), screen_height) + 100

    screen = pyte.HistoryScreen(columns=screen_width, lines=screen_height, history=lines)
    stream = pyte.Stream(screen)
    
    
    stream.feed(data.decode(errors='ignore'))
    
    top = [_line_to_text(line) for line in screen.history.top]
    current = list(screen.display)
    bottom = [_line_to_text(line) for line in screen.history.bottom]
    
    return top + current + bottom
