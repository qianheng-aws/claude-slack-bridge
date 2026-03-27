# Claude Slack Bridge v2 — Dual-Mode Bidirectional Architecture

## Core Concept

The daemon supports two modes simultaneously, with a single Slack thread spanning the entire session lifecycle:
- **PROCESS mode** — daemon manages a `claude --print` subprocess, Slack-driven (mobile/remote)
- **HOOK mode** — user is in local TUI, hooks sync TUI actions to Slack
- **IDLE mode** — neither active, waiting to wake up

You can `claude --resume UUID` to switch back to TUI at any time, or exit TUI to let Slack take over.

## Session State Machine

```
                    +-----------------------------+
                    |       Create Session         |
                    |  Slack @mention or hook reg   |
                    +--------------+--------------+
                                   |
                                   v
                 +--------------------------------+
        +--------|            PROCESS              |<---- Slack message (from IDLE)
        |        |     daemon runs claude --print   |
        |        |     Slack messages -> stdin       |
        |        |     stdout -> Slack thread        |
        |        +---------------+----------------+
        |                        |
        |  --print exits         |  User: claude --resume
        |  (TUI resume preempts  |  (hook registers with daemon)
        |   or idle timeout)     |
        |                        v
        |        +--------------------------------+
        |        |             HOOK                |
        |        |     TUI hooks -> daemon HTTP     |
        |        |     -> Slack thread              |
        |        |                                  |
        |        |  Slack message -> warn user       |
        |        |  "Session in TUI, use terminal"   |
        |        +---------------+----------------+
        |                        |
        |  TUI exits             |  TUI exits
        |  (stop hook or timeout)|  (stop hook or timeout)
        |                        |
        v                        v
+----------------------------------------------+
|                IDLE                            |
|  Neither active                                |
|                                                |
|  Slack message -> start --print -> PROCESS     |
|  TUI resume -> hook registers -> HOOK          |
|  Timeout -> ARCHIVED                           |
+----------------------------------------------+
```

## Architecture Overview

```
+------------------------------------------------------------------------+
|                           Cloud Desktop                                  |
|                                                                          |
|  +--------------+       +----------------------------------------------+|
|  |  Slack App    |<----->|            Bridge Daemon                     ||
|  |  Socket Mode  |       |                                              ||
|  |               |       |  +-----------+  +------------+              ||
|  |  events_api ---------->  | Msg Router |  | Session    |              ||
|  |  interactive --------->  |            |  | Manager    |              ||
|  |               |       |  | thread_ts  |  |            |              ||
|  |  <-- blocks --|       |  | -> session |  | State      |              ||
|  +--------------+       |  +-----+------+  | PROCESS    |              ||
|                          |        |         | HOOK       |              ||
|                          |        |         | IDLE       |              ||
|  +--------------+       |        |         +-----+------+              ||
|  |  HTTP API     |       |        |               |                     ||
|  |  :7778        |       |        v               v                     ||
|  |               |       |  +--------------------------------------+   ||
|  |  /hooks/* <-----------|  |         Process Pool                  |   ||
|  |  (TUI hooks)  |       |  |                                       |   ||
|  |               |       |  |  +- Session A (PROCESS) ------------+ |   ||
|  |  /sessions/* <---------|  |  | claude --print --session-id UUID  | |   ||
|  |  (mgmt API)   |       |  |  | stdin <-- Slack msgs             | |   ||
|  |               |       |  |  | stdout --> Slack thread           | |   ||
|  +--------------+       |  |  +----------------------------------+ |   ||
|                          |  |                                       |   ||
|                          |  |  +- Session B (HOOK) ---------------+ |   ||
|                          |  |  | No subprocess, TUI hooks push     | |   ||
|                          |  |  +----------------------------------+ |   ||
|                          |  |                                       |   ||
|                          |  |  +- Session C (IDLE) ---------------+ |   ||
|                          |  |  | Waiting for Slack msg or TUI      | |   ||
|                          |  |  +----------------------------------+ |   ||
|                          |  +--------------------------------------+   ||
|                          +----------------------------------------------+|
|                                                                          |
|  +--------------------------------------------------------------------+  |
|  |  Local terminal:                                                    |  |
|  |  $ claude --resume UUID-aaa  -> TUI, daemon switches to HOOK       |  |
|  |  $ exit TUI                  -> daemon switches to IDLE             |  |
|  |  Slack message arrives       -> daemon starts --print, PROCESS      |  |
|  +--------------------------------------------------------------------+  |
+------------------------------------------------------------------------+
```

## Data Flow

### 1. New Session (Slack-initiated -> PROCESS mode)

```
User in Slack: "@bridge refactor the auth module"
    |
    v
Daemon Socket Mode: app_mention
    |
    +-> Session Mgr: create session UUID-aaa, state=PROCESS
    |     +-> record channel_id + thread_ts <-> UUID-aaa
    |     +-> persist to sessions.json
    |
    +-> Process Pool: start subprocess
    |     claude --print \
    |       --session-id UUID-aaa \
    |       --name "refactor auth" \
    |       --output-format stream-json \
    |       --input-format stream-json \
    |       -p "refactor the auth module"
    |
    +-> Slack: post session header to thread
```

### 2. Streaming Response (PROCESS mode: Claude -> Slack)

```
claude stdout (stream-json):
    {"type":"assistant","content":"Let me look at..."}
    {"type":"assistant","content":"Let me look at auth"}
    ...
    v
Daemon StreamReader (throttled 1s)
    +-> chat_postMessage (first time)
    +-> chat_update (incremental updates)
    +-> chat_postMessage (tool_use -> approval buttons, PROCESS mode only)
```

### 3. Slack Reply -> Routing Decision

```
User replies in thread: "rewrite with async/await"
    |
    v
Daemon Socket Mode: events_api -> message
    |
    +-> Filter: bot_id? subtype? -> skip
    +-> Lookup: thread_ts -> session UUID-aaa
    +-> Check session.mode:
    |
    +- PROCESS -> stdin.write({"type":"user","content":"rewrite with async/await"})
    |
    +- HOOK   -> slack.post_text("Session is in TUI, please use terminal,
    |            or exit TUI to continue from Slack")
    |
    +- IDLE   -> start claude --print --resume UUID-aaa
               -> switch to PROCESS mode
               -> stdin.write(message)
```

### 4. Tool Approval (PROCESS mode)

```
claude stdout: {"type":"tool_use","name":"Bash","input":{"command":"rm -rf old/"}}
    |
    v
Daemon: check auto_approve_tools (PROCESS mode only)
    +- whitelisted -> write approved to stdin
    +- not whitelisted -> Slack approval buttons
                    |
                    User clicks Approve -> write approved to stdin
                    User clicks Reject  -> write rejected to stdin

Note: The PreToolUse hook (TUI -> Slack approval) is currently disabled due to
dual-approval conflict with CC's permission system. See Issue #4.
Approval in PROCESS mode (Slack-driven sessions) still works as described above.
```

### 5. TUI Resume (PROCESS/IDLE -> HOOK mode)

```
$ claude --resume UUID-aaa
    |
    v
TUI starts, loads session history
    |
    +-> hooks.json user_prompt hook fires
    |     -> POST http://127.0.0.1:7778/hooks/user-prompt
    |     -> daemon receives, identifies session UUID-aaa
    |     -> session.mode = HOOK
    |     -> if --print process running, send SIGTERM
    |
    +-> User actions in TUI -> hooks sync to Slack thread:
    |     user_prompt  -> Slack shows user input
    |     stop         -> Slack shows Claude response
    |
    +-> TUI exits -> stop hook fires
          -> daemon: session.mode = IDLE
```

### 6. TUI Exit -> Slack Takes Over

```
TUI exits (Ctrl+C or /exit)
    |
    +-> stop hook -> daemon: session.mode = IDLE
    |
    v
User sends message in Slack thread
    |
    +-> session.mode == IDLE
    +-> start claude --print --resume UUID-aaa
    +-> session.mode = PROCESS
    +-> stdin.write(message)
```

## Mode Detection

### PROCESS -> HOOK Switch
```
Daemon detects hook request with session_id matching existing session:
  1. Terminate --print subprocess (SIGTERM)
  2. session.mode = HOOK
  3. Subsequent hook requests processed normally
```

### HOOK -> IDLE Switch
```
Two triggers:
  1. stop hook received -> session.mode = IDLE
  2. Hook heartbeat timeout (5min no hook requests) -> session.mode = IDLE
```

### IDLE -> PROCESS Switch
```
Slack thread receives user message:
  1. Start claude --print --resume UUID
  2. session.mode = PROCESS
  3. Write message to stdin
```

## Module Design

```
src/claude_slack_bridge/
+-- config.py            # Configuration
+-- cli.py               # CLI: init/start/stop/status/hook
+-- daemon.py            # Main daemon (Socket Mode + HTTP API + process mgmt)
+-- session_manager.py   # Session lifecycle + state machine
|   +-- Three states: PROCESS / HOOK / IDLE
|   +-- thread_ts <-> UUID mapping
|   +-- Mode switching logic
|   +-- Persistence
+-- process_pool.py      # Claude --print subprocess management
+-- stream_parser.py     # stream-json output parsing
+-- slack_client.py      # Slack API wrapper
+-- slack_formatter.py   # Block Kit formatting
+-- approval.py          # Approval management (PROCESS mode)
+-- http_api.py          # Hook HTTP endpoints (legacy, HOOK mode)
+-- hooks.py             # Hook CLI entry points (TUI mode)
```

## Configuration

```json
{
  "default_channel": "claude-code",
  "max_concurrent_sessions": 3,
  "stream_throttle_ms": 1000,
  "auto_approve_tools": ["Read", "Glob", "Grep"],
  "require_approval": true,
  "approval_timeout_secs": 300,
  "idle_session_timeout_secs": 3600,
  "hook_heartbeat_timeout_secs": 300,
  "claude_args": ["--permission-mode", "default"]
}
```

## Typical Use Cases

### Scenario 1: Pure Slack (mobile/remote)
```
@bridge fix the bug -> PROCESS mode -> Slack conversation -> done
```

### Scenario 2: Slack start, TUI takeover
```
@bridge refactor -> PROCESS mode -> Slack conversation
Back at computer -> claude --resume -> HOOK mode -> TUI conversation (synced to Slack)
```

### Scenario 3: TUI start, Slack continues
```
Local: claude --session-id UUID -> HOOK mode -> TUI conversation (synced to Slack)
Leave computer -> exit TUI -> IDLE
Phone Slack reply -> PROCESS mode -> Slack continues conversation
```

### Scenario 4: Switch back and forth
```
Slack start -> PROCESS
resume TUI -> HOOK (Slack messages intercepted with warning)
exit TUI -> IDLE
Slack continues -> PROCESS
resume again -> HOOK
...
```
