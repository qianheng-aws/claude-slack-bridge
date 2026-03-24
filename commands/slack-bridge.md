---
description: Start the Slack Bridge daemon in the background
allowed-tools: [Bash]
---

Start the Slack Bridge daemon. Run this exact command and report the result:

```bash
BRIDGE_DIR="$(dirname "$(dirname "$(readlink -f "$(which claude-slack-bridge 2>/dev/null || echo /workplace/qianheng/claude-slack-bridge/.venv/bin/claude-slack-bridge)")")")"
export LD_LIBRARY_PATH="${BRIDGE_DIR}/../MeshClaw/build/MeshClaw/MeshClaw-1.0/AL2_x86_64/DEV.STD.PTHREAD/build/private/tmp/brazil-path/build.libfarm/python3.10/lib:${LD_LIBRARY_PATH:-}"

# Check if already running
if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    echo "✅ Slack Bridge daemon is already running"
    curl -s http://127.0.0.1:7778/sessions | python3 -m json.tool
    exit 0
fi

# Start daemon
cd /workplace/qianheng/claude-slack-bridge
setsid .venv/bin/python -m claude_slack_bridge.cli start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
sleep 3

if curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    echo "✅ Slack Bridge daemon started"
else
    echo "❌ Failed to start daemon"
    tail -5 ~/.claude/slack-bridge/daemon.log
fi
```

Report whether the daemon started successfully.
