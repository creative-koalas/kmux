# kmux

[English](./README.md)

专为AI打造的终端MCP工具，由创意考拉打造。

## 什么是 kmux？

kmux（名字来自 “koala” + “tmux”）
是一个专为大语言模型（LLM）设计的终端 MCP 服务器。
换句话说，它是一个 AI 用的终端模拟器。
把它接入你的 LLM，你的 LLM 就可以使用终端，
做诸如写代码、安装软件等事情。

## 安装与使用

目前，kmux 仅支持 Zsh。
在使用 kmux 前，请确保已安装 Zsh 且能通过 `$PATH` 找到它。

安装 kmux，运行：

```bash
pip install -i https://test.pypi.org/simple/ kmux
```

启动 kmux，运行：

```bash
python -m kmux --root-password <your_root_password>
```

如果不想让 LLM 拥有 root 权限，可以省略密码参数，例如：

```bash
python -m kmux
```

在 Claude Code 中：

```bash
# 如果不想让 AI 拥有 root 权限，省略 --root-password 参数
claude mcp add kmux -- python -m kmux --root-password <your_root_password>
```

在 Claude Desktop 中，可以将 kmux 添加到配置文件的 `mcpServers` 字段，例如：

```json
{
  "mcpServers": {
    ... (其他 MCP 服务器)
    "kmux": {
      "command": "python",
      "args": [
        "-m",
        "kmux",
        "--root-password", // 如果不想让 AI 拥有 root 权限，请省略
        "<your-root-password>", // 如果不想让 AI 拥有 root 权限，请省略
      ]
    }
  }
}
```

如果你是第一次在 Claude Desktop 添加 MCP 服务器，请访问
[此页面](https://modelcontextprotocol.io/docs/develop/connect-local-servers)。

## kmux 与其他终端 MCP 有何不同？

**kmux 是首个专为 LLM 设计和定制的终端工具。**

其他终端 MCP 服务器对 LLM 来说只是“能用”；
而 kmux 是“好用”。

kmux 的 “LLM 用户体验” 基于 “AI 人体工学” 的理念，
这一概念由创意考拉CEO [Trent Fellbootman](https://x.com/TFellbootman) 于 2023 年提出。

基于 Transformer 的 LLM 与人类在感知与交互方式上存在差异；
kmux 的设计使 LLM 使用终端变得自然。

### 面向块的设计

**与其他仅提供读写终端数据方式的 MCP 不同，
kmux 将终端的输入与输出组织成块（block）。**
这种面向块的设计正是 [warp](https://www.warp.dev/)
（目前最流行的非系统自带终端模拟器之一）在初期发布时脱颖而出的原因。

来看以下命令与输出：

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

作为人类，你能轻松看出这里有 4 个命令及其对应的 4 个输出。
但程序上分割这些命令/输出对却并不容易。
目前，大多数终端 MCP 服务器只是把终端中的所有内容当作一个巨大的数据块处理。

这种方式会带来一系列问题：

1. 很难单独获取某条（通常是当前的）命令输出。
   为避免遗漏，最直接的方法是把所有输出都传给 LLM。
   这会极大增加 LLM 的上下文负担。
   不幸的是，这正是大多数 MCP 服务器的做法。
2. 难以判断命令何时结束执行。
   很多 MCP 服务器只是等待固定时间再返回所有终端输出。

如果你在 warp 出现前就用过终端，你会发现这些问题在人类终端中也存在。
但它们长期被忽视，因为：

1. 人类可以滚动终端查找对应的命令和输出。看到提示符，就知道上个命令结束了。
2. 人类能自然地多任务：短命令等一会就好，长命令则切到别处再回来查看。

**kmux 通过实现块识别机制解决了这些问题，
能够在终端内容出现时，实时地分割和识别命令/输出块。**

带来的好处：

* 通过块识别，LLM 可以有选择地读取特定命令的输出；
  **只读需要的部分，而不是整个终端。**
  不再污染模型上下文。
* 增量式的块分割让我们可以精确判断命令何时完成；
  不再需要固定等待时间，LLM 可以即时获取结果。

#### 块识别是如何实现的？

块识别机制主要依赖于 zsh 的 hook（因此依赖 zsh）。
zsh 在以下时机提供 hook：

* 命令输入开始
* 命令输入结束
* 命令输出开始
* 命令输出结束

kmux 借助这些 hook 在终端输出中注入标记，
标记命令/输出块的开始和结束。
这些标记以 ANSI 转义序列形式注入，对人类和 LLM 不可见，
但可用于程序化识别。

由于这些 hook 与 shell 相关，而 `zsh` 提供了最直接的接口，
且语法几乎与最流行的 bash 相同，
我们目前选择依赖 `zsh`。

### 语义化会话管理

kmux 另一个体现 AI 人体工学的特性是语义化会话管理系统。
简单来说，kmux 支持为每个 shell 会话附加标签（标题）和描述（摘要）。
它们可以用来标注某个会话的用途。

当 LLM 列出会话时，会显示会话的标签、描述、ID，以及正在运行的命令。
**这样 LLM 无需查看完整输出，就能理解每个会话的状态。**

这很有用，因为人类能直接通过屏幕和标题了解会话上下文，
但 LLM 想获取这些元数据，必须通过 MCP 工具。
因此，让列举会话的MCP工具不仅返回会话 ID，还返回元数据，
能让 LLM 快速了解每个会话的情况，从而更好地决定下一步操作。
