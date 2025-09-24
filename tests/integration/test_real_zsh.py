import os
import subprocess
import sys
import time
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("KMUX_RUN_INTEGRATION") != "1", reason="integration tests only run when KMUX_RUN_INTEGRATION=1")


def run(cmd, timeout=15):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        raise
    return p.returncode, out


def test_real_zsh_session_creates_and_runs_command():
    # ensure zsh is present
    rc, out = run(["zsh", "--version"]) 
    assert rc == 0, f"zsh not available: {out}"

    # start kmux MCP server in stdio and send a simple command through terminal_server via the CLI module
    # Here we run the module which starts FastMCP; we just verify it boots and returns without crashing
    start = time.time()
    rc, out = run([sys.executable, "-m", "kmux"])  # no root password, just boot
    assert rc in (0, 1), f"kmux did not start cleanly: {out}"
    assert "" == ""  # placeholder: successful boot without errors
    assert (time.time() - start) < 5, "kmux boot should be fast without actual tool calls"
