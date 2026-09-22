"""Shared Markdown presentation with semantic, keyboard-scrollable tables."""

from __future__ import annotations

from collections.abc import Sequence

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict


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
    return md
