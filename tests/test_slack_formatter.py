import json

from claude_slack_bridge.slack_formatter import (
    build_approval_blocks,
    build_approval_resolved_blocks,
    build_options_blocks,
    build_post_tool_blocks,
    build_response_blocks,
    build_session_header_blocks,
    build_user_prompt_blocks,
    extract_options,
    md_to_mrkdwn,
    split_message,
    strip_thinking_tags,
    truncate_text,
)


# ── Existing tests ──


def test_build_session_header_blocks() -> None:
    blocks = build_session_header_blocks(
        session_id="abc123",
        directory="/workplace/my-project",
    )
    text = json.dumps(blocks)
    assert "cd /workplace/my-project && claude --resume abc123" in text
    assert any(b["type"] == "section" for b in blocks)


def test_build_approval_blocks_with_bash() -> None:
    blocks = build_approval_blocks(
        tool_name="Bash",
        tool_input={"command": "npm install"},
        session_id="abc123",
        session_name="/workplace/my-project",
        request_id="req-1",
    )
    text = json.dumps(blocks)
    assert "Bash" in text
    assert "npm install" in text
    # Must have actions block with approve/reject/trust/yolo buttons
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    buttons = actions[0]["elements"]
    assert len(buttons) == 4
    assert buttons[0]["action_id"] == "approve_tool"
    assert buttons[1]["action_id"] == "trust_tool"
    assert buttons[2]["action_id"] == "yolo_session"
    assert buttons[3]["action_id"] == "reject_tool"
    assert buttons[0]["value"] == "req-1"


def test_build_post_tool_blocks() -> None:
    blocks = build_post_tool_blocks(
        tool_name="Bash",
        tool_input={"command": "echo hello"},
        output="hello\n",
        duration_ms=1500,
    )
    text = json.dumps(blocks)
    assert "Bash" in text
    assert "hello" in text
    assert "1.5s" in text


def test_build_response_blocks() -> None:
    blocks = build_response_blocks(response_text="Build succeeded.")
    text = json.dumps(blocks)
    assert "Build succeeded." in text


def test_build_user_prompt_blocks() -> None:
    blocks = build_user_prompt_blocks(prompt="Fix the login bug")
    text = json.dumps(blocks)
    assert "Fix the login bug" in text


def test_truncate_text_short() -> None:
    assert truncate_text("hello", 3000) == "hello"


def test_truncate_text_long() -> None:
    long = "x" * 5000
    result = truncate_text(long, 3000)
    assert len(result) <= 3000 + 50  # allow for truncation suffix
    assert "truncated" in result.lower()


def test_approval_blocks_truncate_large_input() -> None:
    blocks = build_approval_blocks(
        tool_name="Write",
        tool_input={"content": "x" * 5000, "file_path": "/tmp/test.py"},
        session_id="s1",
        session_name="proj",
        request_id="r1",
    )
    text = json.dumps(blocks)
    # Should not contain the full 5000 chars
    assert len(text) < 5000


# ── New tests: md_to_mrkdwn ──


def test_md_to_mrkdwn_bold() -> None:
    assert "*bold*" in md_to_mrkdwn("**bold**")


def test_md_to_mrkdwn_headings() -> None:
    result = md_to_mrkdwn("# Header")
    assert result == "*Header*"

    result2 = md_to_mrkdwn("## Sub Header")
    assert result2 == "*Sub Header*"

    result3 = md_to_mrkdwn("### Third Level")
    assert result3 == "*Third Level*"


def test_md_to_mrkdwn_links() -> None:
    result = md_to_mrkdwn("[text](https://example.com)")
    assert result == "<https://example.com|text>"


def test_md_to_mrkdwn_strikethrough() -> None:
    result = md_to_mrkdwn("~~text~~")
    assert result == "~text~"


def test_md_to_mrkdwn_horizontal_rule() -> None:
    result = md_to_mrkdwn("---")
    assert "\u2500" in result
    assert len(result) == 30


def test_md_to_mrkdwn_preserves_code_blocks() -> None:
    text = "```\n**bold** [link](url) ~~strike~~\n```"
    result = md_to_mrkdwn(text)
    # Inside code blocks, markdown should NOT be transformed
    assert "**bold**" in result
    assert "[link](url)" in result
    assert "~~strike~~" in result


def test_md_to_mrkdwn_strips_ansi() -> None:
    text = "\x1b[31mred text\x1b[0m"
    result = md_to_mrkdwn(text)
    assert "\x1b" not in result
    assert "red text" in result


def test_md_to_mrkdwn_tables() -> None:
    table = (
        "| Name | Age |\n"
        "| --- | --- |\n"
        "| Alice | 30 |\n"
        "| Bob | 25 |"
    )
    result = md_to_mrkdwn(table)
    # Table should be converted to bullet format with *Header:* Value
    assert "*Name:*" in result
    assert "*Age:*" in result
    assert "Alice" in result
    assert "Bob" in result
    assert "\u2022" in result  # bullet character


def test_md_to_mrkdwn_mermaid_graph() -> None:
    text = (
        "```mermaid\n"
        "graph LR\n"
        "    A[Start] --> B[End]\n"
        "```"
    )
    result = md_to_mrkdwn(text)
    # Should convert to text arrows with the right-arrow unicode char
    assert "\u2192" in result
    assert "Start" in result
    assert "End" in result
    # Should not contain the mermaid fence
    assert "```mermaid" not in result


def test_md_to_mrkdwn_mermaid_sequence() -> None:
    text = (
        "```mermaid\n"
        "sequenceDiagram\n"
        "    Client->>Server: Request\n"
        "    Server-->>Client: Response\n"
        "```"
    )
    result = md_to_mrkdwn(text)
    assert "Client" in result
    assert "Server" in result
    assert "Request" in result
    assert "Response" in result
    # Should not contain mermaid fence
    assert "```mermaid" not in result


# ── New tests: strip_thinking_tags ──


def test_strip_thinking_tags() -> None:
    text = "Hello <thinking>internal thought</thinking> world"
    cleaned, thinking = strip_thinking_tags(text)
    assert "Hello" in cleaned
    assert "world" in cleaned
    assert "<thinking>" not in cleaned
    assert "internal thought" in thinking

    # Also test antml:thinking variant
    text2 = "Before <thinking>deep thought</thinking> After"
    cleaned2, thinking2 = strip_thinking_tags(text2)
    assert "Before" in cleaned2
    assert "After" in cleaned2
    assert "<thinking>" not in cleaned2
    assert "deep thought" in thinking2


# ── New tests: split_message ──


def test_split_message_short() -> None:
    result = split_message("short text", limit=100)
    assert result == ["short text"]


def test_split_message_long() -> None:
    # Build text that exceeds the limit, with newlines for splitting
    lines = ["Line number %d is here" % i for i in range(200)]
    text = "\n".join(lines)
    result = split_message(text, limit=500)
    assert len(result) > 1
    # All parts except possibly the last should end with continuation marker
    for part in result[:-1]:
        assert "_(continued...)_" in part
    # All original content should be recoverable
    joined = "".join(
        p.replace("\n\n_(continued...)_", "") for p in result
    )
    # The original lines should all be present
    assert "Line number 0 is here" in joined
    assert "Line number 199 is here" in joined


# ── New tests: extract_options ──


def test_extract_options() -> None:
    text = "Here is my answer.\n[OPTIONS: A | B | C]"
    cleaned, choices = extract_options(text)
    assert choices == ["A", "B", "C"]
    assert "[OPTIONS:" not in cleaned
    assert "Here is my answer." in cleaned

    # No options case
    text2 = "Just a normal response."
    cleaned2, choices2 = extract_options(text2)
    assert cleaned2 == text2
    assert choices2 == []


# ── build_options_blocks ──


def test_build_options_blocks_short_choices() -> None:
    """Short choices: button label shows full text, no section block."""
    blocks = build_options_blocks(["Yes", "No", "Maybe"])
    assert len(blocks) == 1
    assert blocks[0]["type"] == "actions"
    buttons = blocks[0]["elements"]
    assert [b["text"]["text"] for b in buttons] == ["Yes", "No", "Maybe"]
    assert [b["value"] for b in buttons] == ["Yes", "No", "Maybe"]


def test_build_options_blocks_long_choice_falls_back() -> None:
    """Any choice > 75 chars: section block lists all, buttons labeled Option N."""
    long_choice = "a" * 80
    choices = [long_choice, "Short"]
    blocks = build_options_blocks(choices)

    assert len(blocks) == 2
    assert blocks[0]["type"] == "section"
    section_text = blocks[0]["text"]["text"]
    assert long_choice in section_text
    assert "Short" in section_text
    assert "*1.*" in section_text
    assert "*2.*" in section_text

    assert blocks[1]["type"] == "actions"
    buttons = blocks[1]["elements"]
    assert [b["text"]["text"] for b in buttons] == ["Option 1", "Option 2"]
    # Values preserve full choice text so the daemon receives the real selection
    assert [b["value"] for b in buttons] == [long_choice, "Short"]


def test_build_options_blocks_exactly_75_chars_no_fallback() -> None:
    """Boundary: choice exactly at Slack's 75-char limit stays in compact mode."""
    choice = "x" * 75
    blocks = build_options_blocks([choice])
    assert len(blocks) == 1
    assert blocks[0]["type"] == "actions"
    assert blocks[0]["elements"][0]["text"]["text"] == choice


# ── New tests: build_approval_resolved_blocks ──


def test_build_approval_resolved_blocks() -> None:
    # Approved
    blocks = build_approval_resolved_blocks("Bash", "approved", "req-1")
    text = json.dumps(blocks)
    assert "Approved" in text
    assert "Bash" in text
    assert blocks[0]["type"] == "context"

    # Rejected
    blocks = build_approval_resolved_blocks("Write", "rejected", "req-2")
    text = json.dumps(blocks)
    assert "Rejected" in text
    assert "Write" in text
    assert blocks[0]["type"] == "context"

    # Trusted
    blocks = build_approval_resolved_blocks("Edit", "trusted", "req-3")
    text = json.dumps(blocks)
    assert "Trusted" in text
    assert "Edit" in text
    assert blocks[0]["type"] == "context"
