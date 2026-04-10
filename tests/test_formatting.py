"""Tests for md_to_mrkdwn conversion."""

from __future__ import annotations

from enso.formatting import md_to_mrkdwn


def test_bold():
    assert md_to_mrkdwn("**hello**") == "*hello*"


def test_italic():
    assert md_to_mrkdwn("*hello*") == "_hello_"


def test_link():
    assert md_to_mrkdwn("[click](https://example.com)") == "<https://example.com|click>"


def test_strikethrough():
    assert md_to_mrkdwn("~~removed~~") == "~removed~"


def test_header():
    assert md_to_mrkdwn("### My Header") == "*My Header*"


def test_header_levels():
    assert md_to_mrkdwn("# H1") == "*H1*"
    assert md_to_mrkdwn("## H2") == "*H2*"
    assert md_to_mrkdwn("###### H6") == "*H6*"


def test_code_block_preserved():
    text = "```python\n**not bold**\n```"
    assert md_to_mrkdwn(text) == text


def test_inline_code_preserved():
    text = "use `**not bold**` here"
    assert md_to_mrkdwn(text) == "use `**not bold**` here"


def test_blockquote_passthrough():
    text = "> this is a quote"
    assert md_to_mrkdwn(text) == "> this is a quote"


def test_mixed_formatting():
    text = "**bold** and *italic* and [link](https://x.com)"
    result = md_to_mrkdwn(text)
    assert result == "*bold* and _italic_ and <https://x.com|link>"


def test_plain_text_passthrough():
    text = "just some plain text"
    assert md_to_mrkdwn(text) == "just some plain text"


def test_blank_line_collapse():
    text = "a\n\n\n\nb"
    assert md_to_mrkdwn(text) == "a\n\nb"


def test_bold_with_italic_inside():
    """Bold wrapping italic-looking content should convert bold only."""
    text = "**important *stuff***"
    result = md_to_mrkdwn(text)
    assert "*important *stuff**" in result or "*important _stuff_*" in result
