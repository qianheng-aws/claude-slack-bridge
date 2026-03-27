# Claude Slack Bridge

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Slack](https://img.shields.io/badge/Slack-Socket%20Mode-4A154B?logo=slack)](https://api.slack.com/apis/socket-mode)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Compatible-orange?logo=anthropic)](https://docs.anthropic.com/en/docs/claude-code)

Bridge Claude Code sessions to Slack — chat with Claude from your phone via Slack threads.

English | [中文](README.zh.md)

<p align="center">
  <img src="docs/demo.gif" alt="Claude Slack Bridge Demo" width="600">
</p>

## How it works

A daemon runs in the background, connecting Claude Code to Slack via Socket Mode:

```
Slack Thread ←→ Daemon ←→ claude --print (stdin/stdout)
       ↑                         ↑
       └── TUI hooks sync ───────┘
```

**Dual-mode architecture:**
- **PROCESS mode** — Slack drives Claude via `--print` subprocess
- **TUI sync** — hooks sync TUI prompts & responses to Slack thread
- **IDLE mode** — session paused, either side can resume

TUI and Slack can operate on the same session simultaneously — Slack uses `--resume --print` alongside the running TUI.

## Features

- **@mention or DM** to start a session — thread replies continue the conversation
- **TUI ↔ Slack sync** — prompts and responses sync to a Slack thread via hooks
- **Session binding** — `/slack-bridge:sync-on` command auto-binds TUI session to Slack DM
- **Streaming responses** — live preview updates, final result overwrites progress
- **OPTIONS buttons** — clickable suggestion buttons in Slack
- **Markdown → mrkdwn** — proper formatting, long messages auto-split

## How is this different?

### vs Claude Slack App (official)

The official Claude Slack app is a standalone chatbot that calls the Claude API. This project bridges your **local Claude Code session** to Slack — with full access to your filesystem, tools, and codebase.

### vs Remote Control (official)

[Remote Control](https://code.claude.com/docs/en/remote-control) connects claude.ai/code or the Claude mobile app to a local session. It's similar in concept but differs in key ways:

| | Remote Control | Claude Slack Bridge |
|---|---|---|
| **Client** | claude.ai/code or Claude app (full UI) | Slack (message-based) |
| **Auth** | Requires claude.ai Pro/Max/Team/Enterprise — no API key or Bedrock | Works with any Claude Code setup including Bedrock/API keys |
| **Team visibility** | Private session | Shared in Slack channels — team can follow along |
| **Integration** | Standalone interface | Fits into existing Slack workflows (search, notifications, @mentions) |

If you have a claude.ai subscription, Remote Control gives a richer UI. This project is better for **Bedrock/API key users**, **team visibility**, or when you want Claude Code woven into your Slack workflow.

## Install

### As Claude Code Plugin (recommended)

```bash
# Clone
git clone https://github.com/qianheng-aws/claude-slack-bridge.git
cd claude-slack-bridge
python3 -m venv .venv
.venv/bin/pip install -e .

# Register as Claude Code marketplace
claude plugins marketplace add /path/to/claude-slack-bridge
claude plugins install slack-bridge@qianheng-plugins

# Initialize config
.venv/bin/claude-slack-bridge init
# Edit ~/.claude/slack-bridge/.env with your Slack tokens
```

Then in Claude Code TUI:
```
/slack-bridge:sync-on    → start daemon + bind session to Slack DM
```

### Manual Setup

```bash
git clone https://github.com/qianheng-aws/claude-slack-bridge.git
cd claude-slack-bridge
python3 -m venv .venv
.venv/bin/pip install -e .

# Initialize config
.venv/bin/claude-slack-bridge init

# Edit ~/.claude/slack-bridge/.env:
#   SLACK_BOT_TOKEN=xoxb-...
#   SLACK_APP_TOKEN=xapp-...
```

### Slack App Setup

1. Create app at https://api.slack.com/apps
2. Enable **Socket Mode** (generates `xapp-` token)
3. Add **Bot Token Scopes**: `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`, `im:history`, `im:read`, `reactions:write`
4. **Event Subscriptions** → Subscribe to bot events: `app_mention`, `message.channels`, `message.im`
5. **Interactivity** → Enable (for OPTIONS buttons)
6. Install app to workspace, invite bot to channels

## Usage

### Plugin Commands

| Command | Effect |
|---------|--------|
| `/slack-bridge:sync-on` | Start daemon + bind current session to Slack DM |
| `/slack-bridge:sync-off` | Mute TUI→Slack sync for current session |
| `/slack-bridge:start-daemon` | Start daemon only |
| `/slack-bridge:stop-daemon` | Stop daemon |
| `/slack-bridge:status` | Show status and active sessions |
| `/slack-bridge:logs` | View recent daemon logs |

### Slack Commands

| Command | Where | Effect |
|---------|-------|--------|
| `@bot <prompt>` | Channel | New session |
| `<message>` | DM | New session |
| Reply in thread | Thread | Continue session |
| `@bot resume <UUID>` | Channel | Bind TUI session to thread |
| `resume <UUID>` | DM | Bind TUI session to thread |

### Makefile Shortcuts

```bash
make install   # setup venv and install
make test      # run tests
make start     # start daemon
make stop      # stop daemon
make status    # health check + sessions
make logs      # tail daemon log
```

### Workflow: TUI-first (sync to Slack)

Working at your desk? Use TUI as usual. Whenever you want Slack as a mirror — before stepping away, mid-conversation, or right from the start — just run `/slack-bridge:sync-on`. From that point on, everything syncs.

```
1. Start TUI:          claude
2. Work as usual       (sync-on can happen anytime — now, later, whenever)
3. /slack-bridge:sync-on → session binds to a Slack DM thread
4. Leave for lunch     →  pull out your phone, reply in the Slack thread
5. Claude responds     →  same session, same context, no interruption
6. Back at desk        →  keep working in TUI (Slack chat becomes a
                          side conversation), or quit + `claude --resume`
                          to merge the full history back into TUI*
```

\* *TUI does not live-reload session history — a platform limitation. Without resume, the Slack portion lives as a parallel branch of the conversation.*

### Workflow: Slack-first (remote control from your phone)

Love [Remote Control](https://code.claude.com/docs/en/remote-control) or [OpenClaw](https://github.com/openclaw/openclaw)? This is the same idea — **remote-control a Claude Code session from your phone** — but through Slack, the tool your team already lives in. No claude.ai subscription needed, works with Bedrock and API keys, and your team can watch along in the channel.

DM or @mention the bot from Slack, and Claude Code starts running on your dev machine. You're now remotely controlling a full Claude Code session — reading files, editing code, running tests — all from your phone.

```
1. @bot or DM     →  Claude Code session starts on your machine
2. Chat in thread →  Claude reads, edits, runs tests — streams results back
3. Keep chatting  →  multi-turn conversation, full tool access
```

When you're back at your computer, every session header includes a one-liner to resume in TUI:

```bash
cd /your/project && claude --resume <session-id>
```

Copy-paste it, and you have the full conversation context locally — then `/slack-bridge:sync-on` to keep both sides in sync.

### Use cases

**Commute coding** — DM the bot from your phone: "refactor the auth middleware to use JWT". Claude works on your cloud dev machine. By the time you arrive, the work is done — `claude --resume` to review and iterate.

**Meeting multitasking** — Kick off a long task in Slack ("migrate the database schema and update all tests"), check progress between agenda items. Claude keeps working while you're in the meeting.

**Shared channel for team visibility** — @mention the bot in a project channel. The whole team sees Claude's work in the thread — great for demos, pair debugging, or keeping teammates in the loop.

**On-call incident response** — Get paged at 2am? DM the bot from your phone: "check the error logs in /var/log/app and find the root cause". Triage from bed before deciding whether to get up.

**Long-running tasks** — Start a large refactor from Slack, go about your day. Slack notifications tell you when Claude needs input or finishes. No terminal session to keep alive.

## Config

`~/.claude/slack-bridge/config.json` (see `config.json.example`):

```json
{
  "daemon_port": 7778,
  "work_dir": "/path/to/default/cwd",
  "claude_args": ["--tools", "Bash,Read,Write,Edit,Glob,Grep"],
  "max_concurrent_sessions": 3,
  "session_archive_after_secs": 3600
}
```

## Architecture

See [ARCHITECTURE.en.md](ARCHITECTURE.en.md) | [ARCHITECTURE.md (中文)](ARCHITECTURE.md) for the full dual-mode state machine design.

## Tests

```bash
make test
# or
.venv/bin/pytest tests/ -q
```

## License

[MIT](LICENSE)
