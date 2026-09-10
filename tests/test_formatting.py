"""Markdown → mrkdwn / HTML goldens and chunking."""

from __future__ import annotations

import pytest

from enso.formatting import (
    has_slack_code_language,
    md_to_html,
    md_to_mrkdwn,
    model_label,
    split_markdown,
)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("opus", "opus"),
        ("openrouter/deepseek/deepseek-v4-flash-0731", "deepseek-v4-flash-0731"),
        ("x" * 24, "x" * 24),
        ("openrouter/deepseek/deepseek-v4-flash-vision-exp", "deepseek-v4-fla…sion-exp"),
    ],
)
def test_model_label(model: str, expected: str) -> None:
    assert model_label(model) == expected


@pytest.mark.parametrize(
    ("markdown", "mrkdwn"),
    [
        ("# Title\n\n**bold** and *it* and ~~gone~~", "*Title*\n\n*bold* and _it_ and ~gone~"),
        ("[docs](https://x.y/z)", "<https://x.y/z|docs>"),
        ("[docs](http://x.y/z)", "<http://x.y/z|docs>"),
        ("[email](mailto:hello@x.y)", "<mailto:hello@x.y|email>"),
        ("[channel](slack://open)", "<slack://open|channel>"),
        ("[channel](slack:open)", "`slack:open`"),
        ("[proposal](/Users/gavin/drafts/report.md)", "`/Users/gavin/drafts/report.md`"),
        ("[proposal](/Users/gavin/Enso(old)/report.md)", "`/Users/gavin/Enso(old)/report.md`"),
        ("[proposal](drafts/report.md)", "`drafts/report.md`"),
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


def test_split_markdown_keeps_fences_and_tables_intact() -> None:
    fence = "```\n" + "\n".join(f"line {i}" for i in range(6)) + "\n```\n"
    chunks = split_markdown(fence, limit=30)
    assert chunks is not None and len(chunks) > 1
    assert all(c.startswith("```\n") and c.rstrip("\n").endswith("```") for c in chunks)
    table = "| a | b |\n| --- | --- |\n" + "".join(f"| {i} | {i} |\n" for i in range(8))
    chunks = split_markdown(table, limit=50)
    assert chunks is not None and all(c.startswith("| a | b |\n| --- | --- |\n") for c in chunks)
    assert split_markdown("x" * 10, limit=5) == ["xxxxx", "xxxxx"]
    assert split_markdown("```\n" + "y" * 40 + "\n```", limit=10) is None


@pytest.mark.parametrize(
    ("text", "labelled"),
    [
        ("Here:\n```python\nprint(1)\n```\n", True),
        ("Here:\n```json\n{}\n```", True),
        ("```PYTHON\nprint(1)\n```", True),  # the label is matched case-insensitively
        ("```python title=x\nprint(1)\n```", True),  # only the first info word is the label
        ("  ```yaml\na: 1\n```", True),  # up to three spaces of indent still opens a fence
        ("~~~sql\nselect 1\n~~~", True),
        ("```python\nprint(1)", True),  # an unterminated fence still names its language
        ("Here:\n```\nplain\n```\n", False),
        ("```mermaid\ngraph TD\n```", False),  # not a language Slack highlights
        ("no fence, just `python` inline", False),
        ("```\n```python\n```\n", False),  # quoted inside an unlabelled fence
        ("```text\n```\n```json\n{}\n```\n", True),  # a later fence is still found
    ],
)
def test_has_slack_code_language(text: str, labelled: bool) -> None:
    assert has_slack_code_language(text) is labelled
