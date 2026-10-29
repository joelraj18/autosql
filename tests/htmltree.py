"""A small HTML tree for the tests: enough to ask "what is inside this card?" without a browser."""
from __future__ import annotations

from html.parser import HTMLParser

VOID = {"meta", "link", "br", "hr", "img", "input", "area", "base", "col", "embed", "source", "track", "wbr"}


class Node:
    def __init__(self, tag: str, attrs: dict[str, str], parent: "Node | None" = None):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[Node | str] = []

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def walk(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.walk()

    def find_all(self, tag: str | None = None, cls: str | None = None, **attrs: str) -> list["Node"]:
        """Descendants (and this node) with the tag, the CSS class and the attributes (attribute names use _ for -)."""
        want = {k.replace("_", "-"): v for k, v in attrs.items()}
        return [n for n in self.walk()
                if (tag is None or n.tag == tag) and (cls is None or cls in n.classes)
                and all(n.attrs.get(k) == v for k, v in want.items())]

    def find(self, *args, **kwargs) -> "Node | None":
        found = self.find_all(*args, **kwargs)
        return found[0] if found else None


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_tree(page: str) -> Node:
    builder = _Builder()
    builder.feed(page)
    return builder.root
