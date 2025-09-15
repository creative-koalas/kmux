# kmux

Terminal MCP server of the AI, for the AI, but by Creative Koalas.

## What is kmux?

kmux (name comes from "koala" + "tmux")
is a terminal MCP server engineered for Large Language Models (LLMs).
I.e., it's a terminal emulator for AI.
Add this to your LLM and your LLM will be able to take advantage of terminals
and do things like writing code, installing software, etc.

## Install & Usage

Currently, kmux only supports Zsh.
Make sure you have Zsh installed and "findable" (i.e., in your `$PATH`)
before using kmux.

To install kmux, run:

```bash
pip install -i https://test.pypi.org/simple/ kmux
```

To start kmux, run:

```bash
python -m kmux --root-password <your_root_password>
```

Or, if you don't want the LLM to have root privilege, just omit the password, like this:

```bash
python -m kmux
```

For Claude Code:

```bash
# Omit the --root-password argument if you don't want the AI to be root
claude mcp add kmux -- python -m kmux --root-password <your_root_password>
```

For Claude Desktop, add kmux to the `mcpServers` field of the configuration file, like this:

```json
{
  "mcpServers": {
    ... (other MCP servers)
    "kmux": {
      "command": "python",
      "args": [
        "-m",
        "kmux",
        "--root-password", // Omit this if you don't want AI to be root
        "<your-root-password>", // Omit this if you don't want AI to be root
      ]
    }
  }
}
```

Visit [this page](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
if it's your first time adding MCP server to Claude Desktop.

## How does kmux differ from other terminal MCPs out there?

**kmux is the first terminal tool specifically engineered and tailored for LLMs.**

Other terminal MCP servers are "usable" for LLM;
kmux is "useful".

The "LLM user experience" of kmux is designed around the idea of "AI ergonomics",
a concept proposed in 2023 by [Trent Fellbootman](https://x.com/TFellbootman), CEO of Creative Koalas.

Transformer-based LLMs and humans have different ways of preceiving and interacting with the environment;
kmux is designed to make it natural for LLMs to use terminals.

### Block-oriented design

**While other terminal MCP servers just provide a way to read/write data from a terminal,
kmux organizes the input & output of a terminal into blocks.**
Such block-oriented design is also what made [warp](https://www.warp.dev/)
(probably the most popular terminal emulator that doesn't come with the OS nowadays)
stand out in its initial release.

Consider the following commands and outputs:

```bash
$ ls
file1.txt
file2.txt
file3.txt
$ cat file1.txt
This is the content of file1.txt.
$ cat file2.txt
This is the content of file2.txt.
$ cat file3.txt
This is the content of file3.txt.
```

As a human, you can easily see that there are 4 commands and 4 respective outputs
in the example above.
However, separating those command/output pairs programmatically is not an easy feat,
and currently, most, if not all, terminal MCP servers
just treat everything in a terminal as a single, large chunk of data.

Such an approach raises a series of problems:

1. There is no easy way to see the output of a specific (usually the current) command.
If you want to avoid omitting anything useful,
the most straightforward way is to include everything in the terminal output
when the LLM reads from the terminal.
That would blow up the LLM context,
but unfortunately, this is what most terminal MCP servers do.
2. It is hard to tell when a command has finished executing.
A lot of terminal MCP servers just wait a certain amount of time before
returning everything in the terminal
when the LLM wants to execute a command and see its output.

If you've been using terminals before warp,
you may realize that these problems also exist in terminals for humans.
However, such problems were ignored for a long time
because:

1. Humans can just scroll around the terminal to find the commands and their outputs.
When we see a command prompt,
we know that the last command is finished and the next one starts.
2. Humans naturally multi-task.
For simple commands, we just wait in front of the terminal until we see the next command prompt;
for long-running commands, we just switch to something else and go back to check again later.

**kmux solves those challenges by implementing a block-recognition mechanism
that allows programmatically and incrementally segmenting and recognizing
command/output blocks as new content appears on the terminal.**

Such a mechanism solves the problems mentioned above:

- With block recognition,
LLM can selectively read the outputs of any specifically command
(usually the currently running command or the last executed command);
**you only read what you need, instead of everything in the terminal.**
No more blowing up the model context.
- Incrementally segmenting output blocks allows us to know precisely when a command has finished executing.
No more waiting for a fixed amount of time;
LLM can just execute a command and get its output when it's done.

#### How is block recognition implemented?

For those interested, here's an overview of how block recognition is implemented.

The block recognition mechanism largely relies on zsh hooks
(hence the zsh dependency).
Basically, zsh has certain hooks that gets called when:

- Command input beings
- Command input ends
- Command output begins
- Command output ends

By utilizing those hooks,
kmux injects certain markers into the terminal output to indicate the beginning and the end of a command/output block.
These markers are injected as ANSI escape sequences;
they are invisible to humans and LLMs
but allow us to programmatically identify the beginning/end of each command/output block.

Since these hooks are shell-specific,
and `zsh` provides the most straightforward hook interface
while also being almost identical to bash (the most popular shell) in syntax,
we choose to stick with `zsh` at present.

### Semantic session management

Another kmux feature designed for AI ergonomics is its semantic session management system.
Basically, kmux supports attaching a label (title) and a description (summary)
to each shell session.
These can be used to mark what a shell session is for.

When the LLM list the shell sessions,
these labels & descriptions along with the session ID and the command currently running in each session are shown to the LLM,
**so that the LLM knows what is going on in each shell session
without the need to see the full outputs.**

This is useful because while humans would just look at the terminal screen and title,
the only way for LLMs to get "metadata" of a terminal session is through a specific MCP tool.
Hence, making list_session return not only the session IDs but also the metadata
would make it much easier for LLMs to quickly know what is going on in each session
and help them to decide what to do next.
