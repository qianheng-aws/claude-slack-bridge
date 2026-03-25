.PHONY: install test start stop status logs

install:
	python3 -m venv .venv
	.venv/bin/pip install -e .

test:
	.venv/bin/python -m pytest tests/ -q

start:
	@curl -s http://127.0.0.1:7778/health 2>/dev/null | grep -q ok && echo "Already running" && exit 0 || true
	setsid .venv/bin/python -m claude_slack_bridge.cli start >> ~/.claude/slack-bridge/daemon.log 2>&1 < /dev/null &
	@sleep 2 && curl -s http://127.0.0.1:7778/health | grep -q ok && echo "✅ Started" || echo "❌ Failed"

stop:
	@kill $$(cat ~/.claude/slack-bridge/daemon.pid 2>/dev/null) 2>/dev/null; echo "✅ Stopped"

status:
	@curl -s http://127.0.0.1:7778/health 2>/dev/null && echo "" && curl -s http://127.0.0.1:7778/sessions | python3 -m json.tool || echo "Not running"

logs:
	tail -50 ~/.claude/slack-bridge/daemon.log
