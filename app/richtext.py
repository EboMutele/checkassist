"""Turn whatever the blog editor (or a paste from Word or Google Docs) produces into a small, safe
subset of HTML, and back out to plain text.

Allowed: paragraphs, two heading levels, bold, italic, strikethrough, inline code, links, quotes,
bulleted and numbered lists (nested), code blocks, dividers, and figures with an image from our own
media store plus an optional caption. Everything else is unwrapped (its text kept) or, for things
like <script>, <style> and <iframe>, dropped with its contents. No attributes survive except a
checked href, our own image src and alt, and heading ids we generate. The stored result is what
readers get, so this is the security boundary: the editor's own clean-up is only a convenience.
"""
import re
from html import escape, unescape
from html.parser import HTMLParser

MAX_HTML = 400_000  # characters accepted from the editor

VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed",
        "param", "track", "keygen"}
DROP = {"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript", "head",
        "title", "form", "button", "select", "textarea", "canvas", "video", "audio", "input", "option",
        "applet", "frame", "frameset", "link", "meta", "base", "picture", "map", "dialog"}
RENAME = {"b": "strong", "i": "em", "del": "s", "strike": "s", "h1": "h2", "h4": "h3", "h5": "h3",
          "h6": "h3", "div": "p", "tt": "code", "kbd": "code", "samp": "code"}
INLINE = {"strong", "em", "s", "code", "a"}
CONTAINERS = {"p", "section", "article", "main", "header", "footer", "aside", "nav", "center",
              "table", "thead", "tbody", "tfoot", "tr", "td", "th", "dl", "dt", "dd", "address", "details",
              "summary", "caption", "body", "html"}

MEDIA_SRC = re.compile(r"^/blog/media/[0-9a-f]{32}\.webp$")
SPACE = re.compile(r"[ \t\r\n\f ]+")


class El:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs=None, children=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = children if children is not None else []


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = El("root")
        self.stack = [self.root]
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if self.skipping:
            if tag in DROP and tag not in VOID:
                self.skipping += 1
            return
        if tag in DROP:
            if tag not in VOID:
                self.skipping = 1
            return
        el = El(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(el)
        if tag not in VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and not self.skipping and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if self.skipping:
            if tag in DROP:
                self.skipping -= 1
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if not self.skipping:
            self.stack[-1].children.append(data)


def _parse(html):
    tree = _Tree()
    tree.feed(html)
    tree.close()
    return tree.root.children


def _tag(node):
    return RENAME.get(node.tag, node.tag) if isinstance(node, El) else None


def _href(value):
    v = (value or "").strip()
    if not v or re.search(r"[\x00-\x20\x7f<>\"']", v):
        return None
    low = v.lower()
    if low.startswith(("https://", "http://", "mailto:")) or (v.startswith("/") and not v.startswith("//")) \
            or v.startswith("#"):
        return v[:2000]
    return None


def _blank(node):
    return (isinstance(node, str) and not node.strip(" \t\r\n\f ")) or (isinstance(node, El) and node.tag == "br")


def _trim(nodes):
    while nodes and _blank(nodes[0]):
        nodes.pop(0)
    while nodes and _blank(nodes[-1]):
        nodes.pop()
    if nodes and isinstance(nodes[0], str):
        nodes[0] = nodes[0].lstrip()
    if nodes and isinstance(nodes[-1], str):
        nodes[-1] = nodes[-1].rstrip()
    return nodes


def _text(node):
    if isinstance(node, str):
        return node
    if node.tag == "br":
        return "\n"
    return "".join(_text(c) for c in node.children)


def _inline(nodes, in_link=False):
    """Inline content only. Blocks are flattened to their text; images are left to _blocks."""
    out = []
    for n in nodes:
        if isinstance(n, str):
            text = SPACE.sub(" ", n)
            if text:
                out.append(text)
            continue
        tag = _tag(n)
        if tag == "br":
            out.append(El("br"))
        elif tag in ("img", "hr"):
            continue
        elif tag in ("strong", "em", "s", "code"):
            kids = _inline(n.children, in_link)
            if any(not _blank(k) for k in kids):
                out.append(El(tag, {}, kids))
            else:
                out.extend(kids)
        elif tag == "a" and not in_link:
            kids = _inline(n.children, True)
            href = _href(n.attrs.get("href"))
            if href and any(not _blank(k) for k in kids):
                out.append(El("a", {"href": href}, kids))
            else:
                out.extend(kids)
        else:
            if tag in ("p", "h2", "h3", "li", "blockquote") and out and not _blank(out[-1]):
                out.append(" ")
            out.extend(_inline(n.children, in_link))
    # merge neighbouring text
    merged = []
    for n in out:
        if isinstance(n, str) and merged and isinstance(merged[-1], str):
            merged[-1] = SPACE.sub(" ", merged[-1] + n)
        else:
            merged.append(n)
    return merged


def _figure(img, caption_nodes=()):
    src = img.attrs.get("src", "").strip()
    if not MEDIA_SRC.match(src):
        return None
    caption = _trim(_inline(list(caption_nodes)))
    alt = SPACE.sub(" ", img.attrs.get("alt", "")).strip()[:300] or SPACE.sub(" ", "".join(_text(c) for c in caption)).strip()[:300]
    kids = [El("img", {"src": src, "alt": alt, "loading": "lazy"})]
    if caption:
        kids.append(El("figcaption", {}, caption))
    return El("figure", {}, kids)


def _find(node, tag):
    for c in node.children:
        if isinstance(c, El):
            if _tag(c) == tag:
                return c
            hit = _find(c, tag)
            if hit:
                return hit
    return None


def _list(node, depth=0):
    items = []
    for c in node.children:
        tag = _tag(c)
        if tag == "li":
            parts = [x for x in c.children if _tag(x) not in ("ul", "ol")]
            nested = [_list(x, depth + 1) for x in c.children if _tag(x) in ("ul", "ol") and depth < 3]
            kids = _trim(_inline(parts)) + [x for x in nested if x]
            if kids:
                items.append(El("li", {}, kids))
        elif tag in ("ul", "ol"):
            sub = _list(c, depth + 1) if depth < 3 else None
            if sub and items:
                items[-1].children.append(sub)
            elif sub:
                items.append(El("li", {}, [sub]))
        elif not _blank(c):
            kids = _trim(_inline([c]))
            if kids:
                items.append(El("li", {}, kids))
    return El(_tag(node), {}, items) if items else None


def _blocks(nodes, quote=False):
    out, run = [], []

    def flush():
        kids = _trim(_inline(run))
        if kids:
            out.append(El("p", {}, kids))
        run.clear()

    for n in nodes:
        tag = _tag(n)
        if tag is None or tag in INLINE or tag == "br":
            run.append(n)
        elif tag == "img":
            fig = None if quote else _figure(n)
            if fig:  # an image we don't host is dropped without splitting the paragraph
                flush()
                out.append(fig)
        elif tag in ("h2", "h3"):
            flush()
            kids = _trim(_inline(n.children))
            if kids:
                out.append(El("p" if quote else tag, {}, kids))
        elif tag == "blockquote":
            flush()
            inner = [b for b in _blocks(n.children, quote=True) if b.tag in ("p", "ul", "ol")]
            if inner:
                out.extend(inner) if quote else out.append(El("blockquote", {}, inner))
        elif tag in ("ul", "ol"):
            flush()
            lst = _list(n)
            if lst:
                out.append(lst)
        elif tag == "li":
            flush()
            out.extend(_blocks(n.children, quote))
        elif tag == "pre":
            flush()
            text = _text(n).replace("\r\n", "\n").strip("\n")
            if text.strip():
                out.append(El("p", {}, [El("code", {}, [text])]) if quote else El("pre", {}, [El("code", {}, [text])]))
        elif tag == "hr":
            flush()
            if not quote:
                out.append(El("hr"))
        elif tag == "figure":
            flush()
            img = _find(n, "img")
            cap = _find(n, "figcaption")
            fig = _figure(img, cap.children if cap else ()) if img and not quote else None
            if fig:
                out.append(fig)
        elif tag in CONTAINERS or tag == "p":
            flush()
            out.extend(_blocks(n.children, quote))
        else:  # span, font, u, mark, sup, label… keep the text
            run.append(n)
    flush()
    return out


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60].strip("-") or "section"


def _number_headings(blocks):
    seen = {}
    for b in blocks:
        if b.tag in ("h2", "h3"):
            base = _slug("".join(_text(c) for c in b.children))
            seen[base] = seen.get(base, 0) + 1
            b.attrs = {"id": base if seen[base] == 1 else f"{base}-{seen[base]}"}


def _html(node):
    if isinstance(node, str):
        return escape(node, quote=False)
    attrs = "".join(f' {k}="{escape(v, quote=True)}"' for k, v in node.attrs.items())
    if node.tag in ("br", "hr", "img"):
        return f"<{node.tag}{attrs}>"
    return f"<{node.tag}{attrs}>{''.join(_html(c) for c in node.children)}</{node.tag}>"


def clean(html):
    """Safe HTML for storage and display."""
    html = (html or "")[:MAX_HTML]
    blocks = _blocks(_parse(html))
    _number_headings(blocks)
    return "\n".join(_html(b) for b in blocks)


def plain_text(html):
    """Readable text, one block per line: for excerpts, word counts and the feed. Expects clean() output."""
    text = re.sub(r"</(p|li|h2|h3|blockquote|figcaption|pre)>|<br>", "\n", html or "")
    text = unescape(re.sub(r"<[^>]+>", " ", text))
    lines = (SPACE.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def headings(html):
    """[(id, text)] for each h2, for a table of contents. Expects clean() output."""
    return [(m.group(1), SPACE.sub(" ", unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip())
            for m in re.finditer(r'<h2 id="([^"]+)">(.*?)</h2>', html or "", re.S)]


def media_names(html):
    return re.findall(r'src="/blog/media/([0-9a-f]{32})\.webp"', html or "")
