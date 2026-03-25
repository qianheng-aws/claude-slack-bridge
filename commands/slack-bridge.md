---
description: Start the Slack Bridge daemon and bind current session to a Slack DM thread
allowed-tools: [Bash]
---

Start the Slack Bridge daemon and bind the current TUI session to Slack. Run these commands in order:

## Step 1: Start daemon (if not running)

```bash
cd /workplace/qianheng/claude-slack-bridge
export LD_LIBRARY_PATH=/workplace/qianheng/MeshClaw/build/MeshClaw/MeshClaw-1.0/AL2_x86_64/DEV.STD.PTHREAD/build/private/tmp/brazil-path/build.libfarm/python3.10/lib:${LD_LIBRARY_PATH:-}

if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    echo "✅ Daemon already running"
else
    setsid .venv/bin/python -m claude_slack_bridge.cli start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
    sleep 3
    if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
        echo "✅ Daemon started"
    else
        echo "❌ Failed to start daemon"
        tail -5 ~/.claude/slack-bridge/daemon.log
        exit 1
    fi
fi
```

## Step 2: Bind current session to Slack DM

```bash
# Get current session ID from environment
SESSION_ID="${CLAUDE_SESSION_ID:-}"
if [ -z "$SESSION_ID" ]; then
    echo "⚠️ No session ID found. Daemon is running but session not bound to Slack."
    exit 0
fi

CWD="$(pwd)"
RESULT=$(curl -s -X POST http://127.0.0.1:7778/sessions/bind \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"$SESSION_ID\", \"name\": \"TUI Session\", \"cwd\": \"$CWD\"}")

if echo "$RESULT" | grep -q '"ok"'; then
    echo "✅ Session bound to Slack DM thread"
    echo "   Session: $SESSION_ID"
    echo "   TUI messages will sync to Slack"
else
    echo "⚠️ Daemon running but failed to bind session: $RESULT"
fi
```

Report the results of both steps.
