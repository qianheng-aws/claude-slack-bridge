---
description: Sync a clean Q&A log (your prompt + each turn's final message); keep approvals
allowed-tools: [Bash]
---

Enable summary-only sync for the current session. Progress chatter (tool
status, session lifecycle, todos) and the intermediate "let me check X…"
narration are silenced. The thread stays a clean **Q&A log**: your prompt,
then the turn's **final message** — the wrap-up Claude writes right before it
stops for your input. Permission requests still ring Slack. This is the
"phone mode": you see what was asked and the answer, and can approve tools,
without the thread filling up with intermediate noise.

Use `/slack-bridge:sync-on` for full sync, `/slack-bridge:sync-ring` for
approvals-only (no answers), or `/slack-bridge:sync-off` to fully mute.

```bash
# Step 1: Ensure daemon is running
export PATH="$HOME/.local/bin:$PATH"
if ! command -v claude-slack-bridge >/dev/null 2>&1; then
    echo "❌ claude-slack-bridge not found. Run the Quick Start from the README to install it."
    exit 1
fi

if ! curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok; then
    setsid claude-slack-bridge start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
    sleep 3
    curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok && echo "✅ Daemon started" || { echo "❌ Daemon failed"; exit 1; }
else
    echo "✅ Daemon running"
fi

# Step 2: Resolve the real session_id of *this* TUI.
SESSION_ID=$("${CLAUDE_PLUGIN_ROOT}/bin/claude-slack-bridge-session-id" "$PWD" 2>/dev/null)
if [ -z "$SESSION_ID" ]; then
    CWD_ENCODED=$(echo "$PWD" | sed 's|^/||; s|/|-|g')
    SESSION_DIR="$HOME/.claude/projects/-${CWD_ENCODED}"
    [ ! -d "$SESSION_DIR" ] && SESSION_DIR=$(ls -dt $HOME/.claude/projects/-* 2>/dev/null | head -1)
    SESSION_ID=$(basename "$(ls -t "$SESSION_DIR"/*.jsonl 2>/dev/null | head -1)" .jsonl 2>/dev/null)
fi

if [ -z "$SESSION_ID" ]; then
    echo "⚠️ No session found"
    exit 0
fi

# Step 3: Bind the session so the Q&A log has a Slack thread to land in
RESULT=$(curl -s -X POST http://127.0.0.1:7778/sessions/bind \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"$SESSION_ID\", \"name\": \"TUI-${SESSION_ID:0:12}\", \"cwd\": \"$PWD\", \"tmux_pane_id\": \"${TMUX_PANE:-}\"}")
echo "$RESULT" | grep -q '"ok"' && echo "✅ Session $SESSION_ID bound to Slack" || echo "⚠️ Bind: $RESULT"

# Step 4: Set summary-only mute level
RESULT=$(curl -s -X POST "http://127.0.0.1:7778/sessions/$SESSION_ID/mute" \
  -H "Content-Type: application/json" -d '{"level": "summary"}')
echo "$RESULT" | grep -q '"ok": true' && echo "📝 Summary-only sync — prompt + final message + Slack approvals" || echo "⚠️ Failed: $RESULT"
```

Report the result.
