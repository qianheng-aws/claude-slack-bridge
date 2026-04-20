"""Write Claude Code permission rules to the right settings.json scope.

Matches the "Trust" button on Slack approval cards to CC's
"Yes, don't ask again" TUI option. Rules go into whichever
settings.json already defines `permissions.allow` (project first,
then user, then user by default).
"""
from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path

logger = logging.getLogger("claude_slack_bridge")


def build_allow_rule(tool_name: str, tool_input: dict | None) -> str:
    """Map (tool_name, tool_input) to a settings.json permissions.allow entry.

    Heuristics:
    - Bash: use the first token as a prefix, e.g. `sudo -n uptime` → `Bash(sudo:*)`.
    - Write / Edit / MultiEdit / Read / NotebookEdit: restrict to the
      directory of the referenced file, e.g. `/tmp/foo.py` → `Write(/tmp/**)`.
    - Everything else: tool name alone (covers all invocations).
    """
    tool_input = tool_input or {}
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", "")).strip()
        if cmd:
            try:
                first = shlex.split(cmd)[0]
            except ValueError:
                first = cmd.split()[0]
            if first:
                return f"Bash({first}:*)"
        return "Bash"
    if tool_name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "").strip()
        if path:
            directory = str(Path(path).parent)
            if directory and directory != "/":
                return f"{tool_name}({directory}/**)"
        return tool_name
    return tool_name


def add_allow_rule(cwd: str, rule: str) -> tuple[Path, bool]:
    """Append `rule` to the `permissions.allow` list in settings.json.

    Picks project scope (<cwd>/.claude/settings.json) if that file
    already has a `permissions` section; otherwise user scope
    (~/.claude/settings.json). Creates the file if missing.

    Returns (settings_path, added). `added` is False when the rule was
    already present.
    """
    target = _pick_scope(cwd)
    data: dict = {}
    if target.is_file():
        try:
            data = json.loads(target.read_text())
        except json.JSONDecodeError:
            logger.warning("Cannot parse %s; starting fresh permissions block", target)
            data = {}
    permissions = data.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    if rule in allow:
        return target, False
    allow.append(rule)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2))
    return target, True


def _pick_scope(cwd: str) -> Path:
    if cwd:
        project_path = Path(cwd).resolve() / ".claude" / "settings.json"
        if project_path.is_file():
            try:
                data = json.loads(project_path.read_text())
                if isinstance(data.get("permissions"), dict):
                    return project_path
            except json.JSONDecodeError:
                pass
    return Path.home() / ".claude" / "settings.json"
