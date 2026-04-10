---
description: Start the Slack Bridge daemon
allowed-tools: [Bash]
---

Start the Slack Bridge daemon. Run:

```bash
# Find the project root from the installed package location
PROJECT_DIR=$(python3 -c "import claude_slack_bridge; import os; print(os.path.dirname(os.path.dirname(claude_slack_bridge.__file__)))" 2>/dev/null)
PYTHON="${PROJECT_DIR}/.venv/bin/python"
# Fallback: try the CLI directly if installed globally
[ ! -x "$PYTHON" ] && PYTHON=$(command -v python3)

if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    echo "✅ Daemon already running (PID $(cat ~/.claude/slack-bridge/daemon.pid 2>/dev/null))"
else
    setsid "$PYTHON" -m claude_slack_bridge.cli start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
    sleep 3
    if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
        echo "✅ Daemon started (PID $(cat ~/.claude/slack-bridge/daemon.pid 2>/dev/null))"
    else
        echo "❌ Failed to start. Check: tail -10 ~/.claude/slack-bridge/daemon.log"
    fi
fi
```

Report the result.
