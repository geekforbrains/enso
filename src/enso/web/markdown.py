"""Shared Markdown presentation with readable tables and read-only task lists."""

from __future__ import annotations

import re
from collections.abc import Sequence

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.rules_core import StateCore
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict

_TASK_MARKER = re.compile(r"^ *\[([ \t\n\v\f\rxX])\](?=[ \t\n\v\f\r])")


def _task_list_markers(state: StateCore) -> None:
    """Recognize task markers only in a list item's first paragraph, before escapes resolve."""
    for index, token in enumerate(state.tokens):
        if (
            token.type != "inline"
            or index < 2
            or state.tokens[index - 1].type != "paragraph_open"
            or state.tokens[index - 2].type != "list_item_open"
        ):
            continue
        if match := _TASK_MARKER.match(token.content):
            token.meta["task_list_checked"] = match[1] in "xX"
            token.content = token.content[match.end() :]
            state.tokens[index - 2].attrJoin("class", "task-list-item")


def _task_list_labels(state: StateCore) -> None:
    """Associate the disabled control with its rendered text without IDs or source HTML."""
    for token in state.tokens:
        if "task_list_checked" not in token.meta:
            continue
        checkbox = Token(
            "task_list_checkbox",
            "input",
            0,
            attrs={"class": "task-list-checkbox", "type": "checkbox", "disabled": ""},
        )
        if token.meta["task_list_checked"]:
            checkbox.attrSet("checked", "")
        token.children = [
            Token("label_open", "label", 1, attrs={"class": "task-list-label"}),
            checkbox,
            *(token.children or []),
            Token("label_close", "label", -1),
        ]


class _ViewerRenderer(RendererHTML):
    def table_open(
        self, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
    ) -> str:
        return (
            '<div class="markdown-table-scroll" tabindex="0" role="region" aria-label="Table">\n'
            + self.renderToken(tokens, idx, options, env)
        )

    def table_close(
        self, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
    ) -> str:
        return self.renderToken(tokens, idx, options, env) + "</div>\n"

    def _table_cell_open(
        self, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
    ) -> str:
        token = tokens[idx]
        # Markdown-it's table rule emits inline alignment styles, forbidden by our CSP.
        # Only its known values become classes; source HTML never reaches this renderer.
        alignment = {
            "text-align:left": "align-left",
            "text-align:center": "align-center",
            "text-align:right": "align-right",
        }.get(str(token.attrs.pop("style", "")))
        if alignment:
            token.attrJoin("class", alignment)
        return self.renderToken(tokens, idx, options, env)

    th_open = _table_cell_open
    td_open = _table_cell_open


def renderer(*, breaks: bool = False) -> MarkdownIt:
    """Use shared safe markup; callers own their link and image policies."""
    md = MarkdownIt(
        "commonmark",
        {"html": False, "linkify": False, "typographer": False, "breaks": breaks},
        renderer_cls=_ViewerRenderer,
    )
    md.enable(["table", "strikethrough"])
    md.core.ruler.before("inline", "task_list_markers", _task_list_markers)
    md.core.ruler.after("inline", "task_list_labels", _task_list_labels)
    return md
