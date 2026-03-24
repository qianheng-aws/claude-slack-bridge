# Claude Slack Bridge

Bridge Claude Code sessions to Slack — chat with Claude from your phone via Slack threads.

## How it works

A daemon runs in the background, connecting Claude Code's `--print` mode to Slack via Socket Mode:

```
Slack Thread ←→ Daemon ←→ claude --print (stdin/stdout)
```

**Dual-mode architecture:**
- **PROCESS mode** — Slack drives Claude via `--print` subprocess
- **HOOK mode** — TUI drives Claude, hooks sync to Slack
- **IDLE mode** — session paused, either side can resume

## Features

- **@mention** in any channel to start a session
- **DM** the bot directly (no @mention needed)
- **Thread replies** continue the conversation (multi-turn)
- **Tool notifications** — see Bash, Read, Write calls in real-time
- **Streaming responses** — live updates as Claude types
- **OPTIONS buttons** — clickable suggestion buttons in Slack
- **👀 reaction** — instant acknowledgment when message received
- **`resume <session-id>`** — bind a TUI session to a Slack thread
- **Session header** — one-click copy `cd /path && claude --resume <UUID>`
- **Markdown → mrkdwn** — proper formatting in Slack
- **Long message splitting** — auto-split at ~3800 chars
- **`yolo off`** — disable auto-approve in thread

## Install

```bash
# Clone and setup
git clone <repo-url>
cd claude-slack-bridge
python3 -m venv .venv
.venv/bin/pip install -e .

# Initialize config
.venv/bin/claude-slack-bridge init

# Edit ~/.claude/slack-bridge/.env with your Slack tokens:
#   SLACK_BOT_TOKEN=xoxb-...
#   SLACK_APP_TOKEN=xapp-...
```

### Slack App Setup

1. Create app at https://api.slack.com/apps
2. Enable **Socket Mode** (generates `xapp-` token)
3. Add **Bot Token Scopes**: `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`, `im:history`, `reactions:write`
4. **Event Subscriptions** → Subscribe to bot events: `app_mention`, `message.channels`, `message.im`
5. **Interactivity** → Enable (for OPTIONS buttons)
6. Install app to workspace, invite bot to channels

## Usage

### Start daemon

```bash
.venv/bin/claude-slack-bridge start
```

### As Claude Code plugin

```
/slack-bridge        # start daemon
/slack-bridge-stop   # stop daemon
/slack-bridge-status # show status
```

### Slack commands

| Command | Where | Effect |
|---------|-------|--------|
| `@bot <prompt>` | Channel | New session |
| `<message>` | DM | New session |
| Reply in thread | Thread | Continue session |
| `@bot resume <UUID>` | Channel | Bind TUI session to thread |
| `resume <UUID>` | DM | Bind TUI session to thread |
| `yolo off` | Thread | Disable auto-approve |

### Switch between TUI and Slack

**Slack → TUI:** Copy the resume command from session header
```bash
cd /path && claude --resume <UUID>
```

**TUI → Slack:** Send in Slack
```
@bot resume <UUID>
```

## Config

`~/.claude/slack-bridge/config.json`:

```json
{
  "daemon_port": 7778,
  "work_dir": "/path/to/default/cwd",
  "claude_args": ["--tools", "Bash,Read,Write,Edit,Glob,Grep"],
  "require_approval": false,
  "auto_approve_tools": ["Read", "Glob", "Grep"],
  "approval_timeout_secs": 300
}
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full dual-mode state machine design.

## Tests

```bash
.venv/bin/pytest tests/ -q
```
