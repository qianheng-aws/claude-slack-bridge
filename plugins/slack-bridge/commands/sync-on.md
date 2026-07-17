---
description: Start TUI→Slack sync (starts daemon if needed, binds session, unmutes if muted)
allowed-tools: [Bash]
---

Enable TUI↔Slack sync for the current session. Run all steps:

```bash
# Step 1: Ensure daemon is running
export PATH="$HOME/.local/bin:$PATH"
if ! command -v claude-slack-bridge >/dev/null 2>&1; then
    echo "❌ claude-slack-bridge not found. Run the Quick Start from the README to install it."
    exit 1
fi

if ! curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    # Detach the daemon so it survives this shell exiting. setsid is
    # Linux-only; fall back to nohup (portable, works on macOS too).
    if command -v setsid >/dev/null 2>&1; then
        setsid claude-slack-bridge start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
    else
        nohup claude-slack-bridge start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
    fi
    sleep 3
    curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok && echo "✅ Daemon started" || { echo "❌ Daemon failed"; exit 1; }
else
    echo "✅ Daemon running"
fi

# Step 2: Resolve the real session_id of *this* TUI. Claude Code exports
# CLAUDE_CODE_SESSION_ID into every Bash tool subprocess (documented;
# updated on /clear, stable across --resume) — the authoritative source.
# The parent-pid-walk resolver is only a fallback for older CC versions.
# No mtime fallback: picking "newest jsonl" silently binds to the wrong
# session when the cwd has parallel/older sessions. Fail loudly.
SESSION_ID="${CLAUDE_CODE_SESSION_ID:-}"
if [ -z "$SESSION_ID" ]; then
    SESSION_ID=$("${CLAUDE_PLUGIN_ROOT}/bin/claude-slack-bridge-session-id" "$PWD")
fi
if [ -z "$SESSION_ID" ]; then
    echo "❌ Could not resolve this TUI's session_id."
    echo "   CLAUDE_CODE_SESSION_ID was unset and the pid-walk resolver found nothing."
    echo "   If this keeps happening, file an issue with the output of:"
    echo "     SLACK_BRIDGE_RESOLVER_DEBUG=1 \"${CLAUDE_PLUGIN_ROOT}/bin/claude-slack-bridge-session-id\" \"\$PWD\""
    exit 1
fi

# The daemon may CORRECT the id (it trusts hook activity from this tmux
# pane over our discovery) — always adopt the id it echoes back, so the
# mute call below targets the session that will actually produce events.
RESULT=$(curl -s -X POST http://127.0.0.1:7778/sessions/bind \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"$SESSION_ID\", \"name\": \"TUI-${SESSION_ID:0:12}\", \"cwd\": \"$PWD\", \"tmux_pane_id\": \"${TMUX_PANE:-}\"}")
BOUND_ID=$(echo "$RESULT" | sed -n 's/.*"session_id": *"\([^"]*\)".*/\1/p')
if [ -n "$BOUND_ID" ] && [ "$BOUND_ID" != "$SESSION_ID" ]; then
    echo "ℹ️ Daemon corrected session id: $SESSION_ID → $BOUND_ID (matched by hook activity)"
    SESSION_ID="$BOUND_ID"
fi
echo "$RESULT" | grep -q '"ok"' && echo "✅ Session $SESSION_ID bound to Slack" || echo "⚠️ Bind: $RESULT"

# Step 3: Mark session as explicitly opted-in to sync
curl -s -X POST "http://127.0.0.1:7778/sessions/$SESSION_ID/mute" \
  -H "Content-Type: application/json" -d '{"level": "sync"}' > /dev/null 2>&1
echo "🔊 TUI↔Slack sync active"
```

Report results.
