"""Text labels, Markdown → mrkdwn / HTML goldens, and message chunking."""

from __future__ import annotations

import pytest

from enso.formatting import (
    chunk_text,
    format_elapsed,
    has_slack_code_language,
    md_to_html,
    md_to_mrkdwn,
    split_markdown,
    status_text,
)
from enso.routing import ResolvedAgent


def test_status_text_formats_agent_and_elapsed() -> None:
    assert [format_elapsed(s) for s in (5, 65, 3725)] == ["5s", "1m 05s", "1h 02m"]
    agent = ResolvedAgent(
        "opencode", "openrouter/deepseek/deepseek-v4-flash-0731", "low", "workspace"
    )
    assert status_text(agent, 12, "Reading foo.py") == (
        "opencode · deepseek-v4-flash-0731 · low · 12s\n↳ Reading foo.py"
    )


@pytest.mark.parametrize(
    ("markdown", "mrkdwn"),
    [
        ("# Title\n\n**bold** and *it* and ~~gone~~", "*Title*\n\n*bold* and _it_ and ~gone~"),
        ("[docs](https://x.y/z)", "<https://x.y/z|docs>"),
        ("[email](mailto:hello@x.y)", "<mailto:hello@x.y|email>"),
        ("[channel](slack://open)", "<slack://open|channel>"),
        ("[channel](slack:open)", "`slack:open`"),
        ("[proposal](/Users/gavin/drafts/report.md)", "`/Users/gavin/drafts/report.md`"),
        ("[proposal](/Users/gavin/Enso(old)/report.md)", "`/Users/gavin/Enso(old)/report.md`"),
        ("[proposal](file:///Users/gavin/report.md)", "`file:///Users/gavin/report.md`"),
        ("[proposal](drafts/*report*.md)", "`drafts/*report*.md`"),
        ("[broken](https://[)", "`https://[`"),
        ("keep `**code**` and\n```py\n**x**\n```", "keep `**code**` and\n```py\n**x**\n```"),
        ("a\n\n\n\n\nb", "a\n\nb"),
        ("**bold *inner* bold**", "*bold *inner* bold*"),
    ],
)
def test_md_to_mrkdwn(markdown: str, mrkdwn: str) -> None:
    assert md_to_mrkdwn(markdown) == mrkdwn


@pytest.mark.parametrize(
    ("markdown", "html"),
    [
        ("# T\n**b** _i_ ~s~ <x>", "<b>T</b>\n<b>b</b> <i>i</i> <s>s</s> &lt;x&gt;"),
        ("`a<b`", "<code>a&lt;b</code>"),
        ("```sh\nls\n```", '<pre><code class="language-sh">ls</code></pre>'),
        ("> q1\n> q2", "<blockquote>q1\nq2</blockquote>"),
        ("[l](http://u)", '<a href="http://u">l</a>'),
    ],
)
def test_md_to_html(markdown: str, html: str) -> None:
    assert md_to_html(markdown) == html


def test_chunk_text_keeps_markdown_or_falls_back_to_lines() -> None:
    fence = "```\n" + "\n".join(f"line {i}" for i in range(6)) + "\n```\n"
    chunks = chunk_text(fence, limit=30)
    assert len(chunks) > 1
    assert all(
        len(c) <= 30 and c.startswith("```\n") and c.rstrip("\n").endswith("```") for c in chunks
    )
    table = "| a | b |\n| --- | --- |\n" + "".join(f"| {i} | {i} |\n" for i in range(8))
    chunks = chunk_text(table, limit=50)
    assert len(chunks) > 1
    assert all(len(c) <= 50 and c.startswith("| a | b |\n| --- | --- |\n") for c in chunks)
    assert chunk_text("x" * 10, limit=5) == ["xxxxx", "xxxxx"]
    assert chunk_text("a\n" * 10, limit=8) == ["a\na\na\na\n", "a\na\na\na\n", "a\na\n"]

    oversized_fence = "```\n" + "y" * 40 + "\n```"
    assert split_markdown(oversized_fence, limit=10) is None
    assert chunk_text(oversized_fence, limit=10) == ["```", *["y" * 10] * 4, "```"]
    oversized_table = "| a |\n| --- |\n| abcdefghijk |"
    assert split_markdown(oversized_table, limit=12) is None
    assert chunk_text(oversized_table, limit=12) == ["| a |", "| --- |", "| abcdefghij", "k |"]


@pytest.mark.parametrize(
    ("text", "labelled"),
    [
        ("Here:\n```python\nprint(1)\n```\n", True),
        ("Here:\n```\nplain\n```\n", False),
        ("```mermaid\ngraph TD\n```", False),  # not a language Slack highlights
    ],
)
def test_has_slack_code_language(text: str, labelled: bool) -> None:
    assert has_slack_code_language(text) is labelled
