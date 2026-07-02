---
description: Start the Slack Bridge daemon
allowed-tools: [Bash]
---

Start the Slack Bridge daemon. Run:

```bash
export PATH="$HOME/.local/bin:$PATH"
if ! command -v claude-slack-bridge >/dev/null 2>&1; then
    echo "❌ claude-slack-bridge not found. Run the Quick Start from the README to install it."
    exit 1
fi

if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    echo "✅ Daemon already running (PID $(cat ~/.claude/slack-bridge/daemon.pid 2>/dev/null))"
else
    # Detach the daemon so it survives this shell exiting. setsid is
    # Linux-only; fall back to nohup (portable, works on macOS too).
    if command -v setsid >/dev/null 2>&1; then
        setsid claude-slack-bridge start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
    else
        nohup claude-slack-bridge start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
    fi
    sleep 3
    if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
        echo "✅ Daemon started (PID $(cat ~/.claude/slack-bridge/daemon.pid 2>/dev/null))"
    else
        echo "❌ Failed to start. Check: tail -10 ~/.claude/slack-bridge/daemon.log"
    fi
fi
```

Report the result.
