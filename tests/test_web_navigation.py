"""Navigation exposes every view while the phone's primary bar stays at five controls."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

import pytest

from enso.web import server

VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "wbr",
}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str | None]
    children: list[Node] = field(default_factory=list)
    text: str = ""

    def find(self, tag, class_name=None):
        found = []
        for node in self.children:
            if node.tag == tag and (
                class_name is None or class_name in (node.attrs.get("class") or "").split()
            ):
                found.append(node)
            found.extend(node.find(tag, class_name))
        return found


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.root = Node("root", {})
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        assert self.stack[-1].tag == tag
        self.stack.pop()

    def handle_data(self, data):
        for node in self.stack:
            node.text += data


def shell(path="/today", *, alarm=False):
    html = (
        server.environment()
        .get_template("base.html")
        .render(active=server._active(path), alarm=alarm, config_problems=[])
    )
    return Document(html).root


def test_desktop_sidebar_and_mobile_more_reach_every_view_without_javascript():
    root = shell()
    (sidebar,) = root.find("nav", "side-nav")
    assert [node.attrs["href"] for node in sidebar.find("a")] == [
        "/today",
        "/tasks",
        "/heartbeats",
        "/runs",
        "/knowledge",
        "/jobs",
        "/workspaces",
        "/secrets",
        "/health",
    ]
    (mobile,) = root.find("nav", "tabs")
    primary = mobile.children[0]
    assert primary.tag == "ul" and len(primary.children) == 5
    assert [item.children[0].attrs["href"] for item in primary.children[:4]] == [
        "/today",
        "/tasks",
        "/heartbeats",
        "/runs",
    ]
    (more,) = primary.children[-1].find("details")
    assert more.children[0].tag == "summary"
    assert more.children[0].text.strip().endswith("More")
    assert "open" not in more.attrs
    assert more.children[0].attrs["aria-controls"] == "more-sections"
    assert [node.attrs["href"] for node in more.find("a")] == [
        "/knowledge",
        "/jobs",
        "/workspaces",
        "/secrets",
        "/health",
    ]
    # Native summary and plain links supply every operation before the script enhances focus.
    assert not mobile.find("button")


@pytest.mark.parametrize(
    ("path", "parent", "under_more"),
    [
        ("/tasks/EN-001", "/tasks", False),
        ("/knowledge/notes/abc", "/knowledge", True),
        ("/skills/enso-heartbeat", "/workspaces", True),  # Skills belongs to Workspaces
    ],
)
def test_deep_links_highlight_their_section_and_more_parent(path, parent, under_more):
    root = shell(path)
    current = [
        node.attrs["href"] for node in root.find("a") if node.attrs.get("aria-current") == "page"
    ]
    assert current == [parent, parent]  # one desktop and one mobile destination
    (more,) = root.find("details", "more-menu")
    assert ("is-current" in more.attrs["class"].split()) is under_more


def test_health_attention_is_visible_on_the_closed_more_control_and_health_links():
    root = shell(alarm=True)
    (more,) = root.find("details", "more-menu")
    summary = more.children[0]
    assert "open" not in more.attrs and summary.find("span", "pip")
    assert "Health needs attention" in summary.text
    health_links = [node for node in root.find("a") if node.attrs["href"] == "/health"]
    assert len(health_links) == 2 and all(node.find("span", "nav-alert") for node in health_links)
    assert not shell().find("span", "nav-alert")


def test_navigation_keeps_the_skip_link():
    root = shell()
    (skip,) = root.find("a", "skip")
    (main,) = root.find("main")
    assert skip.attrs["href"] == "#main" and main.attrs["id"] == "main"
    assert main.attrs["tabindex"] == "-1"
