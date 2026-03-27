# Claude Slack Bridge

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Slack](https://img.shields.io/badge/Slack-Socket%20Mode-4A154B?logo=slack)](https://api.slack.com/apis/socket-mode)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Compatible-orange?logo=anthropic)](https://docs.anthropic.com/en/docs/claude-code)

将 Claude Code 会话桥接到 Slack —— 通过 Slack 线程在手机上与 Claude 对话。

[English](README.md) | 中文

<p align="center">
  <img src="docs/demo.gif" alt="Claude Slack Bridge Demo" width="600">
</p>

## 工作原理

后台运行一个守护进程，通过 Socket Mode 连接 Claude Code 和 Slack：

```
Slack Thread ←→ Daemon ←→ claude --print (stdin/stdout)
       ↑                         ↑
       └── TUI hooks 同步 ───────┘
```

**双模架构：**
- **PROCESS 模式** — Slack 通过 `--print` 子进程驱动 Claude
- **TUI 同步** — hooks 将 TUI 的提示和回复同步到 Slack 线程
- **IDLE 模式** — 会话暂停，任一端可恢复

TUI 和 Slack 可以同时操作同一个会话 —— Slack 使用 `--resume --print` 与运行中的 TUI 并行。

## 功能特性

- **@提及或私信** 即可开始会话 —— 线程回复延续对话
- **TUI ↔ Slack 同步** — 提示和回复通过 hooks 同步到 Slack 线程
- **会话绑定** — `/slack-bridge:sync-on` 命令自动将 TUI 会话绑定到 Slack DM
- **流式响应** — 实时预览更新，最终结果覆盖进度消息
- **选项按钮** — Slack 中可点击的建议按钮
- **Markdown → mrkdwn** — 正确的格式转换，长消息自动分割

## 与其他方案的区别

### vs Claude Slack App（官方）

官方 Claude Slack 应用是一个独立聊天机器人，调用 Claude API 进行对话。本项目将你的**本地 Claude Code 会话**桥接到 Slack —— 拥有完整的文件系统、工具和代码库访问权限。

### vs Remote Control（官方）

[Remote Control](https://code.claude.com/docs/en/remote-control) 将 claude.ai/code 或 Claude 手机 App 连接到本地会话。概念相似，但有关键区别：

| | Remote Control | Claude Slack Bridge |
|---|---|---|
| **客户端** | claude.ai/code 或 Claude App（完整 UI） | Slack（消息界面） |
| **认证** | 需要 claude.ai Pro/Max/Team/Enterprise，不支持 API key 或 Bedrock | 支持任何 Claude Code 配置，包括 Bedrock/API key |
| **团队可见性** | 私人会话 | Slack 频道共享，团队可以跟进 |
| **集成** | 独立界面 | 融入现有 Slack 工作流（搜索、通知、@提及） |

如果你有 claude.ai 订阅，Remote Control 提供更丰富的 UI。本项目更适合 **Bedrock/API key 用户**、**团队协作场景**，或者你希望将 Claude Code 融入 Slack 工作流。

## 安装

### 作为 Claude Code 插件安装（推荐）

```bash
# 克隆
git clone https://github.com/qianheng-aws/claude-slack-bridge.git
cd claude-slack-bridge
python3 -m venv .venv
.venv/bin/pip install -e .

# 注册为 Claude Code 市场插件
claude plugins marketplace add /path/to/claude-slack-bridge
claude plugins install slack-bridge@qianheng-plugins

# 初始化配置
.venv/bin/claude-slack-bridge init
# 编辑 ~/.claude/slack-bridge/.env 填入 Slack token
```

然后在 Claude Code TUI 中：
```
/slack-bridge:sync-on    → 启动守护进程 + 绑定会话到 Slack DM
```

### 手动安装

```bash
git clone https://github.com/qianheng-aws/claude-slack-bridge.git
cd claude-slack-bridge
python3 -m venv .venv
.venv/bin/pip install -e .

# 初始化配置
.venv/bin/claude-slack-bridge init

# 编辑 ~/.claude/slack-bridge/.env:
#   SLACK_BOT_TOKEN=xoxb-...
#   SLACK_APP_TOKEN=xapp-...
```

### Slack 应用配置

1. 在 https://api.slack.com/apps 创建应用
2. 启用 **Socket Mode**（生成 `xapp-` token）
3. 添加 **Bot Token Scopes**：`app_mentions:read`、`channels:history`、`channels:read`、`chat:write`、`im:history`、`im:read`、`reactions:write`
4. **Event Subscriptions** → 订阅 bot 事件：`app_mention`、`message.channels`、`message.im`
5. **Interactivity** → 启用（用于选项按钮）
6. 安装应用到工作区，邀请 bot 进入频道

## 使用方法

### 插件命令

| 命令 | 效果 |
|------|------|
| `/slack-bridge:sync-on` | 启动守护进程 + 绑定当前会话到 Slack DM |
| `/slack-bridge:sync-off` | 静音当前会话的 TUI→Slack 同步 |
| `/slack-bridge:start-daemon` | 仅启动守护进程 |
| `/slack-bridge:stop-daemon` | 停止守护进程 |
| `/slack-bridge:status` | 显示状态和活跃会话 |
| `/slack-bridge:logs` | 查看最近的守护进程日志 |

### Slack 命令

| 命令 | 位置 | 效果 |
|------|------|------|
| `@bot <提示>` | 频道 | 新建会话 |
| `<消息>` | 私信 | 新建会话 |
| 线程回复 | 线程 | 继续会话 |
| `@bot resume <UUID>` | 频道 | 绑定 TUI 会话到线程 |
| `resume <UUID>` | 私信 | 绑定 TUI 会话到线程 |

### 工作流：TUI 为主（同步到 Slack）

在电脑前正常使用 TUI。想把 Slack 当镜像的时候 —— 不管是准备离开工位前、对话进行中、还是一开始 —— 随时 `/slack-bridge:sync-on` 就行。绑定之后一切自动同步。

```
1. 启动 TUI：          claude
2. 正常工作            （sync-on 随时都可以做，不急）
3. /slack-bridge:sync-on → 会话绑定到 Slack DM 线程
4. 去吃午饭            →  掏出手机，在 Slack 线程里继续对话
5. Claude 回复          →  同一个会话，同一个上下文，无缝衔接
6. 回到工位            →  直接继续在 TUI 工作（Slack 对话变成
                          分支对话），或 quit + `claude --resume`
                          把完整历史合并回 TUI*
```

\* *TUI 不支持运行时刷新会话历史（平台限制）。不 resume 的话，Slack 部分的对话会作为并行分支存在。*

### 工作流：Slack 为主（手机远程控制）

喜欢 [Remote Control](https://code.claude.com/docs/en/remote-control) 或 [OpenClaw](https://github.com/openclaw/openclaw)？这是同样的理念 —— **从手机远程操控 Claude Code 会话** —— 只不过通过 Slack，你的团队已经在用的工具。不需要 claude.ai 订阅，支持 Bedrock 和 API key，而且团队成员可以在频道里围观。

在 Slack 里私信或 @提及 bot，Claude Code 就会在你的开发机上运行起来。你相当于在手机上远程操控一个完整的 Claude Code 会话 —— 读文件、改代码、跑测试，全部搞定。

```
1. @bot 或 DM    →  Claude Code 会话在你的机器上启动
2. 线程中对话    →  Claude 读写文件、执行命令，流式返回结果
3. 持续对话      →  多轮交互，完整工具访问
```

回到电脑前时，每个会话头部都附带一行恢复命令：

```bash
cd /your/project && claude --resume <session-id>
```

复制粘贴即可在 TUI 中恢复完整对话上下文，再 `/slack-bridge:sync-on` 保持双向同步。

### 使用场景

**通勤写代码** —— 在手机上私信 bot："把 auth 中间件重构成 JWT"。Claude 在你的云开发机上干活，到公司时工作已经完成 —— `claude --resume` 审查和迭代。

**开会时多线程工作** —— 在 Slack 里启动一个耗时任务（"迁移数据库 schema 并更新所有测试"），趁议程间隙看看进度。Claude 在后台持续工作，不需要你盯着。

**频道共享，团队可见** —— 在项目频道 @提及 bot，整个团队都能在线程里看到 Claude 的工作过程 —— 适合做 demo、协作调试、或让队友了解进展。

**On-call 应急响应** —— 凌晨 2 点被告警叫醒？躺在床上私信 bot："查一下 /var/log/app 的错误日志，找出 root cause"。先在手机上 triage，再决定要不要爬起来。

**长时间任务** —— 从 Slack 启动一个大型重构，然后该干嘛干嘛。Slack 通知会告诉你 Claude 什么时候需要输入或者完成了。不需要维护一个终端会话。

## 配置

`~/.claude/slack-bridge/config.json`：

```json
{
  "daemon_port": 7778,
  "work_dir": "/path/to/default/cwd",
  "max_concurrent_sessions": 3,
  "session_archive_after_secs": 3600
}
```

## 架构

详见 [ARCHITECTURE.md](ARCHITECTURE.md) | [English](ARCHITECTURE.en.md)

## 测试

```bash
make test
# 或
.venv/bin/pytest tests/ -q
```

## 许可证

[MIT](LICENSE)
