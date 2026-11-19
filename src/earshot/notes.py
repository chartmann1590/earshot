# SPDX-License-Identifier: GPL-3.0-or-later
"""Show notes: untrusted HTML (or plain text) to safe Pango markup.

Only a few tags survive (bold, italic, links, paragraphs, lists, headings); everything else is text.
Links are kept only for web and mail addresses. Times written in the notes ("12:34", "1:02:03") become
links to "seek:<seconds>", so a tap jumps there in the episode, the way show notes are meant to be used.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser


TIMESTAMP = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?![\d:])")
URL = re.compile(r"\bhttps?://[^\s<>\"']+[^\s<>\"'.,;:!?)\]]")
BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "tr", "table", "section"}


def esc(text: str) -> str:
    """Escape text for Pango markup (what GLib.markup_escape_text does), without needing GLib."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _seconds(m: re.Match) -> int:
    h, mi, s = m.group(1), m.group(2), m.group(3)
    return int(h or 0) * 3600 + int(mi) * 60 + int(s)


def _linkify(text: str, duration: float, in_link: bool) -> str:
    """Escape plain text, turning bare web addresses and in-range timestamps into links."""
    if in_link:
        return esc(text)
    out, pos = [], 0
    spans = []
    for m in URL.finditer(text):
        spans.append((m.start(), m.end(), "url", m.group(0)))
    for m in TIMESTAMP.finditer(text):
        sec = _seconds(m)
        if (duration <= 0 or sec <= duration + 1) and not any(a <= m.start() < b for a, b, *_ in spans):
            spans.append((m.start(), m.end(), "seek", sec))
    for start, end, kind, value in sorted(spans):
        if start < pos:
            continue
        out.append(esc(text[pos:start]))
        label = esc(text[start:end])
        href = f"seek:{value}" if kind == "seek" else value
        out.append(f'<a href="{esc(href)}">{label}</a>')
        pos = end
    out.append(esc(text[pos:]))
    return "".join(out)


class _ToMarkup(HTMLParser):
    def __init__(self, duration: float):
        super().__init__(convert_charrefs=True)
        self.duration = duration
        self.out: list[str] = []
        self.open: list[str] = []     # pango tags we opened, to close them in order
        self.link = 0
        self.list_stack: list[list] = []
        self.skip = 0                 # inside <script>/<style>

    def _nl(self, n: int = 1):
        text = "".join(self.out)
        have = len(text) - len(text.rstrip("\n"))
        if text.strip():
            self.out.append("\n" * max(0, n - have))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("b", "strong"):
            self.out.append("<b>"); self.open.append("b")
        elif tag in ("i", "em", "cite"):
            self.out.append("<i>"); self.open.append("i")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._nl(2); self.out.append("<b>"); self.open.append("b")
        elif tag == "a":
            href = (a.get("href") or "").strip()
            if href.lower().startswith(("http://", "https://", "mailto:")) and not self.link:
                self.out.append(f'<a href="{esc(href)}">'); self.open.append("a"); self.link += 1
            else:
                self.open.append("")
        elif tag in ("ul", "ol"):
            self._nl(1); self.list_stack.append([tag, 0])
        elif tag == "li":
            self._nl(1)
            if self.list_stack and self.list_stack[-1][0] == "ol":
                self.list_stack[-1][1] += 1
                self.out.append(f"{self.list_stack[-1][1]}. ")
            else:
                self.out.append("•  ")
        elif tag == "br":
            self.out.append("\n")
        elif tag in BLOCK:
            self._nl(2)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("b", "strong", "i", "em", "cite", "a", "h1", "h2", "h3", "h4", "h5", "h6"):
            want = {"strong": "b", "em": "i", "cite": "i"}.get(tag, "b" if tag.startswith("h") else tag)
            if want in self.open or (tag == "a" and "" in self.open):
                # close everything above it, then it (HTML in feeds is often badly nested)
                while self.open:
                    t = self.open.pop()
                    if t:
                        self.out.append(f"</{t}>")
                    if t == "a":
                        self.link -= 1
                    if t == want or (tag == "a" and t == ""):
                        break
            if tag.startswith("h"):
                self._nl(1)
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            self._nl(2)
        elif tag in ("p", "div", "blockquote", "section"):
            self._nl(2)

    def handle_data(self, data):
        if self.skip:
            return
        data = re.sub(r"\s+", " ", data)
        if not "".join(self.out).strip() or "".join(self.out).endswith("\n"):
            data = data.lstrip()
        self.out.append(_linkify(data, self.duration, bool(self.link)))

    def result(self) -> str:
        while self.open:
            t = self.open.pop()
            if t:
                self.out.append(f"</{t}>")
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_markup(notes: str, duration: float = 0.0) -> str:
    """Show notes as Pango markup, safe for Gtk.Label.set_markup."""
    notes = (notes or "").strip()
    if not notes:
        return ""
    if "<" not in notes or not re.search(r"</?[a-zA-Z][^>]*>", notes):
        # plain text: keep its line breaks
        notes = html.unescape(notes)
        paras = [_linkify(p.strip(), duration, False) for p in re.split(r"\n\s*\n", notes) if p.strip()]
        return "\n\n".join(paras)
    parser = _ToMarkup(duration)
    try:
        parser.feed(notes)
        parser.close()
    except Exception:
        return esc(re.sub(r"<[^>]+>", " ", notes))
    return parser.result()


def to_plain(notes: str, limit: int = 300) -> str:
    """A short plain-text summary (for a show's description line)."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", notes or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"
