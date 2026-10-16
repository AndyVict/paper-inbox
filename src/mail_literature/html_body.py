from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re

from .identifiers import clean_text, public_article_url

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCK_TAGS = {"p", "div", "tr", "table", "li", "h1", "h2", "h3", "h4", "section", "article"}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False)

    def text(self) -> str:
        return clean_text("".join(child.text() if isinstance(child, Node) else child for child in self.children))

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.nodes()


class BodyTree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {key: value or "" for key, value in attrs}, parent=self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def hidden(node: Node) -> bool:
    style = re.sub(r"\s+", "", node.attrs.get("style", "")).casefold()
    return node.tag in {"head", "style", "script"} or "hidden" in node.attrs or "display:none" in style or "visibility:hidden" in style


def body_tree(html: str) -> Node:
    parser = BodyTree()
    parser.feed(html)
    return parser.root


def lines_from_tree(root: Node) -> list[str]:
    chunks: list[str] = []

    def visit(node: Node):
        if hidden(node):
            return
        if node.tag in BLOCK_TAGS or node.tag in {"br", "hr"}:
            chunks.append("\n")
        for child in node.children:
            if isinstance(child, Node):
                visit(child)
            else:
                chunks.append(child)
        if node.tag == "a":
            url = public_article_url(node.attrs.get("href", ""))
            if url:
                chunks.append(" " + url + " ")
        if node.tag in BLOCK_TAGS:
            chunks.append("\n")

    visit(root)
    return [clean_text(line) for line in "".join(chunks).splitlines() if clean_text(line)]


def following_node(node: Node) -> Node | None:
    if not node.parent:
        return None
    found = False
    for sibling in node.parent.children:
        if sibling is node:
            found = True
        elif found and isinstance(sibling, Node) and sibling.tag not in {"br", "hr"}:
            return sibling
        elif found and isinstance(sibling, str) and sibling.strip():
            return None
    return None
