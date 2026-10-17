"""Signs that untrusted content is trying to give the agent orders."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .taint import TaintLedger

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("override", re.compile(r"\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|above|earlier|all)\b.{0,20}\b(instructions?|rules|prompts?)\b", re.I)),
    ("secrecy", re.compile(r"\bdo not (tell|mention|inform|notify|alert)\b|\bwithout (telling|informing)\b|\bkeep this (secret|quiet|between)\b", re.I)),
    ("addressed to the AI", re.compile(r"\b(ai assistant|note for the assistant|assistants? (processing|reading)|dear (ai|assistant|agent)|to the (ai|assistant|agent))\b", re.I)),
    ("role change", re.compile(r"\byou are now\b|\bnew (system )?instructions\b|\bsystem prompt\b|\bdeveloper mode\b", re.I)),
    ("false authority", re.compile(r"\b(approved|authori[sz]ed) by (the )?(cfo|ceo|admin|management|it)\b|\bmandatory\b.{0,40}\b(audit|compliance)\b", re.I)),
    ("urgency", re.compile(r"\b(immediately|within \d+ (hours?|minutes?)|urgent(ly)?|right now)\b", re.I)),
]


GUARD_THRESHOLD = 0.8


@dataclass
class InjectionSignal:
    pattern: str
    excerpt: str
    source_id: str
    source_label: str
    hidden: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def scan(ledger: TaintLedger) -> list[InjectionSignal]:
    """Every instruction-like sentence from an untrusted source that also matches
    a known manipulation pattern, plus any instruction that was hidden from people."""
    out: list[InjectionSignal] = []
    seen: set[tuple[str, str]] = set()
    for item in ledger.instructions():
        for name, rx in PATTERNS:
            if rx.search(item.sentence) and (item.source_id, name) not in seen:
                seen.add((item.source_id, name))
                out.append(InjectionSignal(name, item.sentence, item.source_id, item.source_label, item.hidden))
        if item.hidden and (item.source_id, "hidden text") not in seen:
            seen.add((item.source_id, "hidden text"))
            out.append(InjectionSignal(f"hidden text ({item.how or 'hidden'})", item.sentence, item.source_id,
                                       item.source_label, True))
    # verdicts from the Prompt Guard classifier, stored on each source at ingest
    for sid, src in ledger.sources.items():
        score = src.get("guard")
        if score is not None and score >= GUARD_THRESHOLD:
            sample = next((i.sentence for i in ledger.instructions() if i.source_id == sid), src.get("subject") or "")
            hidden = any(i.hidden for i in ledger.instructions() if i.source_id == sid)
            out.append(InjectionSignal(f"Prompt Guard model {score:.2f}", sample, sid, src["label"], hidden))
    return out
