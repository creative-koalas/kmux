PLUGIN_GENERAL_DOCUMENTATION = \
"""# kmux文档

## kmux简介

kmux是由创意考拉开发的、针对LLM优化的终端工具。

## 面向块的抽象和块识别机制

与终端的底层接口（pty+IO流不同），
kmux是面向块设计的，将终端上的输入输出抽象成一个一个的块，
每一次执行命令，命令对应一个输入块，而命令输出对应一个输出块
（这也包括需要交互的命令，如npx create-next-app@latest）。

例如，考虑以下终端上的内容：

```txt
trent-AORUS-15-XE4% cd /; ls
bin		   include	      media  sbin.usr-is-merged       tmpjcef-p270_scheme.tmp
bin.usr-is-merged  lib		      mnt    share		      tmpjcef-p71254_scheme.tmp
boot		   lib32	      opt    snap		      tmpjcef-p73568_scheme.tmp
cdrom		   lib64	      proc   srv		      tools
dev		   lib.usr-is-merged  root   sys		      usr
etc		   libx32	      run    tmp		      var
home		   lost+found	      sbin   tmpjcef-p140_scheme.tmp
trent-AORUS-15-XE4% ls home 
linuxbrew  postgres  trent
trent-AORUS-15-XE4%
```

kmux会自动将这些内容识别并组织成块，类似这样：

```xml
<input-block>
cd /; ls
</input-block
<output-block>
bin		   include	      media  sbin.usr-is-merged       tmpjcef-p270_scheme.tmp
bin.usr-is-merged  lib		      mnt    share		      tmpjcef-p71254_scheme.tmp
boot		   lib32	      opt    snap		      tmpjcef-p73568_scheme.tmp
cdrom		   lib64	      proc   srv		      tools
dev		   lib.usr-is-merged  root   sys		      usr
etc		   libx32	      run    tmp		      var
home		   lost+found	      sbin   tmpjcef-p140_scheme.tmp
</output-block>
<input-block>
ls home
</input-block>
<output-block>
linuxbrew  postgres  trent
</output-block>
```

这种面向块的抽象可以让LLM“按需读取终端内容”，
无需把所有终端内容都读入模型上下文；
kmux的块识别机制还能够智能识别某个命令何时完成执行，
因此支持在某个终端session中运行某个命令并在完成执行时返回命令的输出，
而无需等待固定的一段时间或返回终端上的全部内容。

由于块识别机制用到了Zsh提供的一些功能，kmux只支持创建和使用基于Zsh的终端会话。

## 交互式命令

kmux完全支持执行命令后可能需要额外输入的交互式命令
（由于一些known issues，对于“更加exotic”的TUI应用，如vim，暂不支持），
例如`sudo apt upgrade`（输入密码+输入Y确认更新）。
因此，使用kmux时，无需将所有输入想办法“打包”到一条命令里，
可以像人类一样先运行命令，在需要输入时再以发送keys的方式与命令交互。

## 对LLM友好的终端会话管理系统

kmux支持管理多个终端会话，
并可以在多个终端会话中同时运行做多个工作。
会话管理系统也是针对LLM优化的；
每个会话的ID使用从0开始依次递增的整数，而非UUID。
每个会话还附带一些可以读写的metadata，如标题和内容简介；
这些metadata是需要手动设置的，
并会在列举当前会话时被显示出来。
因此，可以通过设置这些metadata为每个会话赋予“含义”
（例如“配置xxx环境”，“调试xxx”，等等），
使得无需检查终端上的具体内容，只要看到这些metadata就大致知道每个终端会话是干什么的。
当然，也可以在会话中执行一些命令之后根据结果修改metadata，
或通过动态修改metadata实现终端复用，使用同一个终端做完一个任务后去做下一个任务。

## 小贴士

1. 遇到有命令卡在那里导致你无法执行下一个命令的情况，
先看看这个命令是不是在等输入，
等输入的话可以用send_keys方法把输入给进去
（send_keys可以用来输入任何字符，从一般的ASCII到\n\r\t等等，可以模拟换行或者按enter，
还可以用来输入Ctrl-B/C/D等等特殊字符）。
如果就是卡死了，可以善用send_keys方法，
这个方法不仅可以用来输密码，输文本，还可以用来输其他的科技狠活，
比如Ctrl-B/C/D等等（说到底这些组合键最终都是特殊字符嘛）。
可以Ctrl-C掉等等。
2. 没必要把一大堆命令组合到一块一起运行。
kmux是人性化设计的，你完全可以像人一样一次一个命令执行。

以上就是kmux使用文档的全部内容，祝使用愉快。
"""