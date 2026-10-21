"""A tiny, dependency-free HTML tree used by the import and URL adapters.

It is deliberately not a browser engine.  It builds enough of a DOM to read
metadata, JSON-LD, links and the small amount of surrounding text needed for a
company card.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable

from ..normalize import clean_text

_VOID_TAGS = {
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
    "param",
    "source",
    "track",
    "wbr",
}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)
    parent: "Node | None" = None

    def attr(self, name: str, default: str = "") -> str:
        return self.attrs.get(name.lower(), default)

    @property
    def classes(self) -> set[str]:
        return set(self.attr("class").split())

    def append(self, child: "Node | str") -> None:
        if isinstance(child, Node):
            child.parent = self
        self.children.append(child)

    def text(self, *, skip_tags: set[str] | None = None, limit: int = 50_000) -> str:
        skip = skip_tags or {"script", "style", "noscript", "template"}
        parts: list[str] = []
        length = 0

        def walk(node: Node) -> None:
            nonlocal length
            if length >= limit:
                return
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                    length += len(child)
                    if length >= limit:
                        return
                elif child.tag not in skip:
                    walk(child)

        walk(self)
        return clean_text(" ".join(parts), limit)

    def descendants(self, *, limit: int = 20_000) -> Iterable["Node"]:
        count = 0
        stack: list[Node] = [self]
        while stack and count < limit:
            node = stack.pop()
            count += 1
            yield node
            for child in reversed(node.children):
                if isinstance(child, Node):
                    stack.append(child)

    def find_all(
        self,
        tag: str | None = None,
        *,
        attr: str = "",
        contains: str = "",
        equals: str = "",
        limit: int = 1000,
    ) -> list["Node"]:
        result: list[Node] = []
        target_tag = tag.lower() if tag else ""
        for node in self.descendants():
            if target_tag and node.tag != target_tag:
                continue
            value = node.attr(attr) if attr else ""
            if contains and contains.lower() not in value.lower():
                continue
            if equals and value.lower() != equals.lower():
                continue
            result.append(node)
            if len(result) >= limit:
                break
        return result

    def find(self, tag: str | None = None, **kwargs) -> "Node | None":
        found = self.find_all(tag, limit=1, **kwargs)
        return found[0] if found else None

    def ancestors(self, *, limit: int = 8) -> Iterable["Node"]:
        node = self.parent
        count = 0
        while node is not None and count < limit:
            yield node
            node = node.parent
            count += 1

    def has_ancestor_tag(self, tag: str) -> bool:
        return any(node.tag == tag.lower() for node in self.ancestors())


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(
            tag.lower(),
            {key.lower(): (value or "") for key, value in attrs},
        )
        self._stack[-1].append(node)
        if node.tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(
            tag.lower(),
            {key.lower(): (value or "") for key, value in attrs},
        )
        self._stack[-1].append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].append(data)


def parse_html_tree(markup: str) -> Node:
    parser = _TreeParser()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:  # noqa: BLE001 - malformed saved pages should still yield partial data
        pass
    return parser.root
