"""Taint tracking: remember every identifier and instruction that arrived from
an untrusted source, then find them again inside later tool-call arguments.

A hit means the agent is about to act on something a stranger wrote. Values
that are also vouched for by a trusted source (the user's own task, saved
contacts, the company domain) do not count as taint.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
ACCOUNT_RE = re.compile(r"\b(?:PK\d{2}[A-Z]{4}\d{10,16}|\d{10,18})\b")
IDENT_RE = re.compile(r"\b[A-Z]{2,5}-\d{3,6}\b")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
INSTRUCTION_CUES = re.compile(
    r"\b(ignore|disregard|forward|send|share|export|delete|remove|wipe|transfer|wire|pay|upload|"
    r"reply|email|must|do not (?:tell|mention|inform)|assistant|ai\b)",
    re.I,
)
WORD_RE = re.compile(r"[a-z0-9@.\-]+")
# Arguments that choose *who* or *what* an action targets. Only the user's own
# words can vouch for these; data from company systems cannot.
TARGET_FIELDS = {"to", "cc", "bcc", "recipient", "record_id", "email_id", "url"}

KIND_LABEL = {
    "address": "email address",
    "url": "link",
    "account": "account number",
    "identifier": "identifier",
    "instruction": "dictated text",
}


@dataclass
class TaintItem:
    kind: str
    value: str
    source_id: str
    source_label: str
    sentence: str
    hidden: bool = False
    how: str | None = None


@dataclass
class TaintHit:
    field: str
    value: str
    kind: str
    source_id: str
    source_label: str
    sentence: str
    hidden: bool
    how: str | None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind_label"] = KIND_LABEL.get(self.kind, self.kind)
        return d


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_SPLIT.split(text) if s and s.strip()]


def _strings(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path or "value", value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _strings(v, f"{path}[{i}]")


def _shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    words = WORD_RE.findall(text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


@dataclass
class TaintLedger:
    """Everything one agent session has read from untrusted sources."""

    trusted_text: str = ""
    trusted_data: str = ""
    items: list[TaintItem] = field(default_factory=list)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    def trust(self, text: str) -> None:
        """Text from the user: vouches for every argument."""
        self.trusted_text += "\n" + (text or "")

    def trust_data(self, text: str) -> None:
        """Output of internal company systems: vouches for content, not for targets."""
        self.trusted_data += "\n" + (text or "")

    def ingest(
        self,
        source_id: str,
        source_label: str,
        text: str,
        hidden_texts: list[dict[str, str]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> int:
        """Record every identifier and instruction found in one untrusted document."""
        if source_id in self.sources:
            return 0
        hidden_texts = hidden_texts or []
        self.sources[source_id] = {"label": source_label, "hidden": hidden_texts, **(extra or {})}
        before = len(self.items)

        def hidden_info(sentence: str) -> tuple[bool, str | None]:
            low = sentence.lower()
            for h in hidden_texts:
                ht = h["text"].lower()
                if low[:40] in ht or ht[:40] in low:
                    return True, h.get("how")
            return False, None

        for sent in sentences(text):
            hidden, how = hidden_info(sent)
            found: list[tuple[str, str]] = []
            found += [("address", m.group(0).rstrip(".")) for m in EMAIL_RE.finditer(sent)]
            found += [("url", m.group(0).rstrip(".,")) for m in URL_RE.finditer(sent)]
            found += [("account", m.group(0)) for m in ACCOUNT_RE.finditer(sent)]
            found += [("identifier", m.group(0)) for m in IDENT_RE.finditer(sent)]
            for kind, value in found:
                self.items.append(TaintItem(kind, value, source_id, source_label, sent, hidden, how))
            if INSTRUCTION_CUES.search(sent) and len(sent.split()) >= 4:
                self.items.append(TaintItem("instruction", sent, source_id, source_label, sent, hidden, how))
        return len(self.items) - before

    # -- matching ---------------------------------------------------------
    def _vouched(self, kind: str, value: str, path: str, is_trusted_address: Callable[[str], bool]) -> bool:
        low = value.lower()
        if low in self.trusted_text.lower():
            return True
        if kind == "address" and is_trusted_address(low):
            return True
        field_name = re.split(r"[.\[]", path, maxsplit=1)[0]
        return field_name not in TARGET_FIELDS and low in self.trusted_data.lower()

    def match(self, args: dict[str, Any], is_trusted_address: Callable[[str], bool]) -> list[TaintHit]:
        hits: list[TaintHit] = []
        seen: set[tuple[str, str]] = set()
        for path, text in _strings(args):
            low = text.lower()
            for item in self.items:
                if item.kind == "instruction":
                    continue
                if item.value.lower() in low and not self._vouched(item.kind, item.value, path, is_trusted_address):
                    key = (path, item.value.lower())
                    if key not in seen:
                        seen.add(key)
                        hits.append(TaintHit(path, item.value, item.kind, item.source_id, item.source_label,
                                             item.sentence, item.hidden, item.how))
            # text the agent copied out of an untrusted instruction
            stripped = text.strip()
            if len(stripped) < 8 or len(stripped.split()) < 2 or stripped.lower() in self.trusted_text.lower():
                continue
            arg_sh = _shingles(stripped)
            for item in self.items:
                if item.kind != "instruction":
                    continue
                inside = stripped.lower() in item.sentence.lower()
                overlap = len(arg_sh & _shingles(item.sentence)) if arg_sh else 0
                if inside or overlap >= 2:
                    key = (path, item.sentence.lower())
                    if key not in seen:
                        seen.add(key)
                        hits.append(TaintHit(path, stripped[:120], "instruction", item.source_id, item.source_label,
                                             item.sentence, item.hidden, item.how))
                        break
        return hits

    def instructions(self) -> list[TaintItem]:
        return [i for i in self.items if i.kind == "instruction"]
