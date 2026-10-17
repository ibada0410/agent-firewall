"""Turn email HTML into the plain text an agent receives, and find the parts a
human reader would never see (white text, zero-size text, display:none,
HTML comments). Agents read all of it; people read none of the hidden parts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0*[01](\.\d+)?(px|pt|em)?\s*(;|$)"
    r"|color\s*:\s*(#fff(fff)?|white|rgba?\(\s*255\s*,\s*255\s*,\s*255)|opacity\s*:\s*0(\.0+)?\s*(;|$)",
    re.I,
)
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table", "ul", "ol"}
_VOID_TAGS = {"br", "img", "hr", "meta", "input", "link"}


@dataclass
class Segment:
    text: str
    hidden: bool
    how: str | None = None  # why it is hidden: "white text", "HTML comment", ...


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[Segment] = []
        self._stack: list[tuple[str, str | None]] = []

    def _hidden_reason(self) -> str | None:
        for _tag, reason in reversed(self._stack):
            if reason:
                return reason
        return None

    def handle_starttag(self, tag, attrs):
        style = dict(attrs).get("style") or ""
        reason = None
        m = _HIDDEN_STYLE.search(style)
        if m:
            s = m.group(0).lower()
            if "color" in s:
                reason = "white text"
            elif "font-size" in s:
                reason = "zero-size text"
            elif "opacity" in s:
                reason = "transparent text"
            else:
                reason = "hidden element"
        if tag in _BLOCK_TAGS:
            self.segments.append(Segment("\n", False))
        if tag not in _VOID_TAGS:
            self._stack.append((tag, reason))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break
        if tag in _BLOCK_TAGS:
            self.segments.append(Segment("\n", False))

    def handle_data(self, data):
        if data:
            reason = self._hidden_reason()
            self.segments.append(Segment(data, reason is not None, reason))

    def handle_comment(self, data):
        text = data.strip()
        if text:
            self.segments.append(Segment(f" {text} ", True, "HTML comment"))


def parse(html: str) -> list[Segment]:
    p = _Parser()
    p.feed(html)
    p.close()
    # merge neighbours with the same visibility to keep the output compact
    merged: list[Segment] = []
    for seg in p.segments:
        if merged and merged[-1].hidden == seg.hidden and merged[-1].how == seg.how:
            merged[-1].text += seg.text
        else:
            merged.append(Segment(seg.text, seg.hidden, seg.how))
    return merged


def _tidy(text: str) -> str:
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def agent_text(html: str) -> str:
    """Everything the agent reads, hidden parts included."""
    return _tidy("".join(s.text for s in parse(html)))


def human_text(html: str) -> str:
    """Only what a person sees in their mail client."""
    return _tidy("".join(s.text for s in parse(html) if not s.hidden))


def hidden_parts(html: str) -> list[dict[str, str]]:
    out = []
    for s in parse(html):
        t = _tidy(s.text)
        if s.hidden and t:
            out.append({"text": t, "how": s.how or "hidden"})
    return out


def xray(html: str) -> list[dict[str, object]]:
    """Segments for the UI's x-ray view, in reading order."""
    out: list[dict[str, object]] = []
    for s in parse(html):
        t = re.sub(r"[ \t]+", " ", unescape(s.text))
        t = t.strip() if s.hidden else re.sub(r" *\n[ \n]*", "\n\n", t)
        if not t.strip() and (not out or not str(out[-1]["text"]).strip()):
            continue  # no leading or doubled blank runs
        out.append({"text": t, "hidden": s.hidden, "how": s.how})
    while out and not str(out[-1]["text"]).strip():
        out.pop()
    if out and not out[0]["hidden"]:
        out[0]["text"] = str(out[0]["text"]).lstrip()
    return out
