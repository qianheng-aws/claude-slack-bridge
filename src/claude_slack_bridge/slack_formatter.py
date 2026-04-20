from __future__ import annotations

import json
import re

# ── Constants ──

SLACK_MAX_TEXT = 39_000       # Global text cap before truncation
SLACK_MSG_LIMIT = 3_900      # Per-message cap (API rejects above ~4000)
TRUNCATION_NOTICE = "\n\n_(Response truncated — Slack message limit)_"
CONTINUATION = "\n\n_(continued...)_"

_OPTIONS_RE = re.compile(r"\[OPTIONS:\s*(.+?)\]\s*$", re.MULTILINE)
OPTIONS_ACTION_PREFIX = "options_choice_"

# ── Markdown → Slack mrkdwn patterns ──

_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_MD_HR = re.compile(r"^[\s]*([-*_])\1{2,}\s*$")
_MD_STRIKE = re.compile(r"~~(.+?)~~")
_MD_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_MD_CODE_LANG = re.compile(r"```\w*\n", re.MULTILINE)

# Markdown table patterns
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+\|)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")

# Mermaid diagram patterns
_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
_GRAPH_EDGE_RE = re.compile(
    r"(\w+)(?:\[([^\]]*)\]|\{([^}]*)\}|(?:\([^)]*\)))?"
    r"\s*(-->|---|-\.->|==>)(?:\|([^|]*)\|)?\s*"
    r"(\w+)(?:\[([^\]]*)\]|\{([^}]*)\}|(?:\([^)]*\)))?"
)
_SEQ_RE = re.compile(r"(\S+?)\s*(->>|-->>|->|-->)\s*(\S+?):\s*(.+)")

# Inline thinking tags
_THINKING_TAG_RE = re.compile(
    r"<(?:thinking|antml:thinking)>.*?</(?:thinking|antml:thinking)>",
    re.DOTALL,
)

# ANSI escape codes
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ── Core conversion ──


def md_to_mrkdwn(text: str) -> str:
    """Convert Markdown to Slack mrkdwn format.

    Handles: bold, headings, links, images, strikethrough, horizontal rules,
    tables (→ vertical bullet format), mermaid diagrams (→ text arrows),
    ANSI codes, and thinking tags. Preserves code blocks.
    """
    text = _strip_ansi(text)

    if len(text) > SLACK_MAX_TEXT:
        cut = text[:SLACK_MAX_TEXT].rfind("\n") or SLACK_MAX_TEXT
        text = f"{text[:cut]}\n\n_...truncated ({len(text)} chars total)_"

    text = _convert_tables(text)
    text = _convert_mermaid(text)

    out: list[str] = []
    in_code = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                in_code = False
                out.append(line)
            else:
                in_code = True
                # Strip language identifier from opening fence
                out.append(_MD_CODE_LANG.sub("```\n", line) if "\n" in line else re.sub(r"```\w+", "```", line))
        elif in_code:
            out.append(line)
        else:
            out.append(_convert_inline(line))
    return "\n".join(out)


def _convert_inline(line: str) -> str:
    """Convert a single non-code line from markdown to Slack mrkdwn."""
    # Headings → bold
    m = _MD_HEADING.match(line)
    if m:
        return f"*{m.group(2).strip()}*"

    # Horizontal rule → unicode line
    if _MD_HR.match(line):
        return "\u2500" * 30

    # Images → links
    line = _MD_IMG.sub(r"<\2|\1>", line)

    # **bold** → *bold*
    line = line.replace("**", "*")

    # ~~strike~~ → ~strike~
    line = _MD_STRIKE.sub(r"~\1~", line)

    # [text](url) → <url|text>
    line = _MD_LINK.sub(r"<\2|\1>", line)

    return line


# ── Table conversion ──


def _convert_tables(text: str) -> str:
    """Convert markdown tables to vertical bullet list format (mobile-friendly)."""
    lines = text.split("\n")
    result: list[str] = []
    headers: list[str] = []
    data_rows: list[list[str]] = []

    def _flush_table() -> None:
        if not headers or not data_rows:
            return
        for row in data_rows:
            parts: list[str] = []
            for i, cell in enumerate(row):
                if not cell:
                    continue
                if i < len(headers):
                    parts.append(f"*{headers[i]}:* {cell}")
                else:
                    parts.append(cell)
            result.append("\u2022 " + " | ".join(parts))
        headers.clear()
        data_rows.clear()

    for line in lines:
        if _TABLE_ROW_RE.match(line):
            if _TABLE_SEP_RE.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not headers:
                headers.extend(cells)
            else:
                data_rows.append(cells)
        else:
            _flush_table()
            result.append(line)

    _flush_table()
    return "\n".join(result)


# ── Mermaid conversion ──


def _convert_mermaid(text: str) -> str:
    """Replace ```mermaid blocks with readable text diagrams."""

    def _replace(m: re.Match) -> str:
        body = m.group(1).strip()
        first = body.split("\n", 1)[0].strip().lower()
        if first.startswith(("graph ", "flowchart ")):
            return _mermaid_graph(body)
        if first.startswith("sequencediagram"):
            return _mermaid_sequence(body)
        return f"```\n{body}\n```"

    return _MERMAID_BLOCK_RE.sub(_replace, text)


def _mermaid_graph(body: str) -> str:
    labels: dict[str, str] = {}
    edges: list[str] = []
    for line in body.split("\n")[1:]:
        m = _GRAPH_EDGE_RE.search(line.strip())
        if not m:
            continue
        src, sl1, sl2, _, edge_label, dst, dl1, dl2 = m.groups()
        if sl1 or sl2:
            labels[src] = sl1 or sl2
        if dl1 or dl2:
            labels[dst] = dl1 or dl2
        src_name = labels.get(src, src)
        dst_name = labels.get(dst, dst)
        arrow = f" ({edge_label.strip()}) " if edge_label else " "
        edges.append(f"  {src_name} \u2192{arrow}{dst_name}")
    return "\n".join(edges) if edges else body


def _mermaid_sequence(body: str) -> str:
    lines: list[str] = []
    for line in body.split("\n")[1:]:
        m = _SEQ_RE.match(line.strip())
        if not m:
            continue
        src, arrow_type, dst, msg = m.groups()
        arrow = "\u2192" if ">>" in arrow_type else "\u21e2"
        if "--" in arrow_type:
            arrow = "\u21e0"
        lines.append(f"  {src} {arrow} {dst}: {msg.strip()}")
    return "\n".join(lines) if lines else body


# ── Thinking tags ──


def strip_thinking_tags(text: str) -> tuple[str, str]:
    """Strip inline <thinking> tags from text.

    Returns (cleaned_text, extracted_thinking).
    """
    thinking_parts: list[str] = []
    for m in _THINKING_TAG_RE.finditer(text):
        block = m.group(0)
        inner = re.sub(r"^<[^>]+>|<[^>]+>$", "", block).strip()
        if inner:
            thinking_parts.append(inner)
    cleaned = _THINKING_TAG_RE.sub("", text).strip()
    return cleaned, "\n\n".join(thinking_parts)


# ── Utilities ──


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def extract_options(text: str) -> tuple[str, list[str]]:
    """Extract [OPTIONS: A | B | C] from text. Returns (cleaned_text, choices)."""
    m = _OPTIONS_RE.search(text)
    if not m:
        return text, []
    choices = [c.strip() for c in m.group(1).split("|") if c.strip()]
    return text[: m.start()].rstrip(), choices


_BUTTON_TEXT_LIMIT = 75


def build_options_blocks(choices: list[str]) -> list[dict]:
    """Build Slack action buttons from OPTIONS choices.

    Default: each button's label is the full choice text.
    Fallback: when any choice exceeds Slack's 75-char button label limit,
    render a section block listing all choices numbered, with buttons
    labeled "Option 1", "Option 2", etc.
    """
    choices = choices[:5]
    needs_fallback = any(len(c) > _BUTTON_TEXT_LIMIT for c in choices)

    blocks: list[dict] = []
    if needs_fallback:
        listing = "\n".join(f"*{i + 1}.* {c}" for i, c in enumerate(choices))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": listing}})

    buttons = [
        {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": f"Option {i + 1}" if needs_fallback else c,
            },
            "action_id": f"{OPTIONS_ACTION_PREFIX}{i}",
            "value": c,
        }
        for i, c in enumerate(choices)
    ]
    blocks.append({"type": "actions", "elements": buttons})
    return blocks


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 30] + "\n... (truncated, full output too long)"


def split_message(text: str, limit: int = SLACK_MSG_LIMIT) -> list[str]:
    """Split text into chunks that fit within Slack's message limit.

    Splits on newline boundaries when possible.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        chunk_limit = limit - len(CONTINUATION)
        cut = text.rfind("\n", 0, chunk_limit)
        if cut <= 0:
            cut = chunk_limit
        remainder = text[cut:].lstrip("\n")
        if remainder:
            parts.append(text[:cut] + CONTINUATION)
        else:
            parts.append(text[:cut])
        text = remainder
    return parts


def _code_block(text: str, max_chars: int = 2500) -> str:
    truncated = truncate_text(text, max_chars)
    return f"```\n{truncated}\n```"


# ── Block Kit builders ──


def build_session_header_blocks(
    session_id: str,
    directory: str,
) -> list[dict]:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    resume_cmd = f"cd {directory} && claude --resume {session_id}" if directory else f"claude --resume {session_id}"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*New Claude Code Session*\n"
                    f"Started: {now}\n\n"
                    f"Resume in TUI:\n"
                    f"`{resume_cmd}`"
                ),
            },
        },
        {"type": "divider"},
    ]


def build_approval_blocks(
    tool_name: str,
    tool_input: dict,
    session_id: str,
    session_name: str,
    request_id: str,
) -> list[dict]:
    if tool_name == "Bash":
        detail = tool_input.get("command", json.dumps(tool_input))
    elif tool_name in ("Read", "Write", "Edit"):
        detail = tool_input.get("file_path", json.dumps(tool_input))
    else:
        detail = json.dumps(tool_input, indent=2)

    detail = truncate_text(str(detail), 2500)

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"\U0001f510 *Tool approval requested*\n"
                    f"*{tool_name}* \u2014 `{session_name}`\n\n"
                    f"```\n{detail}\n```"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "approve_tool",
                    "value": request_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Trust"},
                    "action_id": "trust_tool",
                    "value": request_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "YOLO"},
                    "action_id": "yolo_session",
                    "value": session_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "reject_tool",
                    "value": request_id,
                },
            ],
        },
    ]


def build_tool_notification_blocks(
    tool_name: str,
    tool_input: dict,
) -> list[dict]:
    """Notification-only block for PROCESS mode (no approval buttons)."""
    if tool_name == "Bash":
        detail = tool_input.get("command", json.dumps(tool_input))
    elif tool_name in ("Read", "Write", "Edit"):
        detail = tool_input.get("file_path", json.dumps(tool_input))
    else:
        detail = json.dumps(tool_input, indent=2)

    detail = truncate_text(str(detail), 2500)

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"\U0001fac6 `{tool_name}`\n```\n{detail}\n```",
            },
        },
    ]


def build_permission_denied_blocks(denials: list[dict]) -> list[dict]:
    """Show permission denials from Claude Code."""
    lines = []
    for d in denials[:5]:
        tool = d.get("tool_name", "unknown")
        reason = d.get("reason", "permission denied")
        lines.append(f"\u2022 *{tool}*: {reason}")
    text = "\U0001f6ab *Permission Denied*\n" + "\n".join(lines)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def build_post_tool_blocks(
    tool_name: str,
    tool_input: dict,
    output: str,
    duration_ms: float = 0,
) -> list[dict]:
    duration_str = f" ({duration_ms / 1000:.1f}s)" if duration_ms else ""

    if tool_name == "Bash":
        summary = tool_input.get("command", tool_name)
    elif tool_name in ("Read", "Write", "Edit", "Glob", "Grep"):
        summary = tool_input.get("file_path", tool_input.get("pattern", tool_name))
    else:
        summary = tool_name

    output_text = truncate_text(output, 2500) if output else "(no output)"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{tool_name}*{duration_str}\n"
                    f"> {truncate_text(str(summary), 200)}\n"
                    f"```\n{output_text}\n```"
                ),
            },
        },
    ]


def build_response_blocks(response_text: str) -> list[dict]:
    text = md_to_mrkdwn(truncate_text(response_text, 2900))
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Claude Response*\n\n{text}",
            },
        },
    ]


def build_user_prompt_blocks(prompt: str) -> list[dict]:
    text = truncate_text(prompt, 2900)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*User Prompt*\n\n{text}",
            },
        },
    ]


def build_approval_resolved_blocks(
    tool_name: str,
    decision: str,
    detail: str,
) -> list[dict]:
    """Replace the approval block with a compact resolved summary.

    `detail` is rendered verbatim (e.g. a rule like `Bash(sudo:*)`
    for trust, or the tool name for plain approve/reject).
    """
    if decision == "approved":
        label = "\u2705 Approved"
    elif decision == "trusted":
        label = "\U0001f91d Trusted"
    elif decision == "yolo":
        label = "\U0001f3bd YOLO"
    else:
        label = "\U0001f6ab Rejected"
    text = f"{label} \u2014 `{detail}`" if detail else label
    return [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": text}],
        },
    ]
