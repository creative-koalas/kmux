from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from .terminal_server import TerminalServer


terminal_server: TerminalServer
root_password: str | None = None


def set_root_password(password: str | None):
    global root_password
    root_password = password


@asynccontextmanager
async def lifespan(app: FastMCP):
    global terminal_server
    global root_password

    terminal_server = TerminalServer(root_password=root_password)
    yield
    await terminal_server.stop()


mcp = FastMCP(lifespan=lifespan)


@mcp.tool()
async def create_session() -> str:
    """Creates a new zsh session,
    returning its session ID.

    :return: The ID of the new session.
    """

    try:
        session_id = await terminal_server.create_session()
        return f"""New zsh session created. Session ID: {session_id}."""
    except Exception as e:
        return f"""Failed to create new zsh session. Error: "{e}"."""

@mcp.tool()
async def list_sessions() -> str:
    """Lists all currently active zsh sessions."""
    try:
        return f"""Current active zsh sessions:

<sessions>
{await terminal_server.list_sessions()}
</sessions>"""
    except Exception as e:
        return f"""Failed to list active zsh sessions. Error: "{e}"."""

@mcp.tool()
async def update_session_label(session_id: str, label: str) -> str:
    """
    Updates the label of a zsh session.
    
    :param session_id: The ID of the zsh session to update label.
    :param label: The new label for the zsh session.
    """
    try:
        await terminal_server.update_session_label(session_id=session_id, label=label)
        return """Session label updated."""
    except Exception as e:
        return f"""Failed to update session label. Error: "{e}"."""
    
@mcp.tool()
async def update_session_description(session_id: str, description: str) -> str:
    """
    Updates the description of a zsh session.
    
    :param session_id: The ID of the zsh session to update description.
    :param description: The new description for the zsh session.
    """
    try:
        await terminal_server.update_session_description(session_id=session_id, description=description)
        return """Session description updated."""
    except Exception as e:
        return f"""Failed to update session description. Error: "{e}"."""


@mcp.tool()
async def execute_command(session_id: str, command: str, timeout_seconds: float = 5.0) -> str:
    """
    Executes a command in a zsh session.
    This tool is only available when the zsh session is awaiting command input.
    
    :param session_id: The ID of the zsh session to execute command.
    :param command: The command to execute.
    :param timeout_seconds: The timeout in seconds for the command to execute.
    """
    try:
        return await terminal_server.execute_command(session_id=session_id, command=command, timeout_seconds=timeout_seconds)
    except Exception as e:
        return f"""Failed to execute command. Error: "{e}"."""

@mcp.tool()
async def send_keys(session_id: str, keys: str) -> str:
    """
    Sends keys to a zsh session.
    This is useful with e.g., interactive CLI tools like `vim` or `npx create-next-app@latest ...`,
    or terminating a running command with Ctrl-C.
    This tool is only available when there is a running command in the zsh session, presumably awaiting input.
    
    :param session_id: The ID of the zsh session to send keys.
    :param keys: The keys to send.
    """

    try:
        await terminal_server.send_keys(session_id=session_id, keys=keys)
        return """Keys sent to terminal session; it may take a few seconds for the running command to process them."""
    except Exception as e:
        return f"""Failed to send keys. Error: "{e}"."""


@mcp.tool()
async def enter_root_password(session_id: str) -> str:
    """
    Enters the root password for a zsh session,
    i.e., simulates typing password and pressing Enter.
    This tool is only available when there is a running command in the zsh session, presumably awaiting password input.
    For this tool to be available, root privilege must also be enabled on the zsh session.
    
    :param session_id: The ID of the zsh session to enter root password.
    """

    try:
        await terminal_server.enter_root_password(session_id=session_id)
        return """Root password entered."""
    except Exception as e:
        return f"""Failed to enter root password. Error: "{e}"."""


@mcp.tool()
async def snapshot(session_id: str, include_all: bool = False) -> str:
    """
    Returns a snapshot of the current state of the pty session.
    
    By default, it only returns the output including and after the last command;
    if include_all is True, it returns the all terminal output starting from terminal startup.

    :param session_id: The ID of the zsh session to take snapshot.
    :param include_all: Whether to include all terminal output starting from terminal startup.
    """
    try:
        return repr(await terminal_server.snapshot(session_id=session_id, include_all=include_all))
    except Exception as e:
        return f"""Failed to take snapshot. Error: "{e}"."""


@mcp.tool()
async def delete_session(session_id: str) -> str:
    """
    Deletes a zsh session.
    
    :param session_id: The ID of the zsh session to delete.
    """

    try:
        await terminal_server.delete_session(session_id=session_id)
        return """Session deleted."""
    except Exception as e:
        return f"""Failed to delete session. Error: "{e}"."""
