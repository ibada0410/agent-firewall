"""Permission rules: the YAML policy plus rules learned from reviewer decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from ..db import Database

ALLOW, ASK, DENY = "allow", "ask", "deny"


@dataclass
class PolicyResult:
    action: str
    reasons: list[str] = field(default_factory=list)
    learned_allow: list[dict[str, Any]] = field(default_factory=list)
    learned_block: list[dict[str, Any]] = field(default_factory=list)


def domain_of(address: str) -> str:
    address = (address or "").strip().lower()
    return address.rsplit("@", 1)[-1] if "@" in address else address


class Policy:
    def __init__(self, path: Path, db: Database, contacts: Callable[[], set[str]]):
        self.path = path
        self.db = db
        self._contacts = contacts
        self.raw_text = ""
        self.doc: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        self.raw_text = self.path.read_text(encoding="utf-8")
        doc = yaml.safe_load(self.raw_text) or {}
        if not isinstance(doc.get("tools", {}), dict):
            raise ValueError("policy.yaml: 'tools' must be a mapping")
        for name, rule in doc.get("tools", {}).items():
            if rule.get("action") not in (ALLOW, ASK, DENY):
                raise ValueError(f"policy.yaml: tool {name} needs action allow, ask or deny")
        self.doc = doc

    # -- accessors --------------------------------------------------------
    @property
    def company_domains(self) -> set[str]:
        return {d.lower() for d in self.doc.get("company_domains", [])}

    @property
    def untrusted_sources(self) -> set[str]:
        return set(self.doc.get("untrusted_sources", []))

    @property
    def review_timeout(self) -> float | None:
        v = self.doc.get("review_timeout_seconds")
        return float(v) if v else None

    def blocked_domains(self) -> set[str]:
        base = {d.lower() for d in self.doc.get("blocked_domains", [])}
        learned = {r["value"] for r in self.learned_rules() if r["effect"] == "block" and r["field"] == "domain"}
        return base | learned

    def learned_rules(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM learned_rules" + ("" if include_inactive else " WHERE active=1") + " ORDER BY created_at DESC"
        return self.db.all(sql)

    def is_trusted_address(self, address: str) -> bool:
        address = (address or "").strip().lower()
        if domain_of(address) in self.company_domains or address in self._contacts():
            return True
        return any(
            r["effect"] == "allow" and r["field"] == "to" and r["value"] == address for r in self.learned_rules()
        )

    # -- evaluation -------------------------------------------------------
    def evaluate(self, tool: str, args: dict[str, Any]) -> PolicyResult:
        rules = self.doc.get("tools", {})
        rule = rules.get(tool)
        if rule is None:
            return PolicyResult(self.doc.get("default_action", ASK), [f"{tool} is not listed in the policy"])

        result = PolicyResult(rule["action"])
        if rule.get("reason") and rule["action"] != ALLOW:
            result.reasons.append(rule["reason"])

        # learned rules from reviewers
        for r in self.learned_rules():
            if r["tool"] not in (tool, "*"):
                continue
            value = str(args.get(r["field"], "")).strip().lower() if r["field"] != "domain" else None
            if r["field"] == "domain":
                hit = any(domain_of(str(v)) == r["value"] for v in args.values() if isinstance(v, str) and "@" in v)
            else:
                hit = value == r["value"]
            if hit:
                (result.learned_allow if r["effect"] == "allow" else result.learned_block).append(r)

        if result.learned_block:
            r = result.learned_block[0]
            result.action = DENY
            result.reasons.insert(0, f"A reviewer blocked {r['field']} {r['value']}")
            return result

        for v in args.values():
            if isinstance(v, str) and "@" in v and domain_of(v) in self.blocked_domains():
                result.action = DENY
                result.reasons.insert(0, f"{domain_of(v)} is on the blocked list")
                return result

        if result.action == ALLOW:
            for cond in rule.get("ask_if", []) or []:
                if self._condition(cond, args, result):
                    result.action = ASK
                    result.reasons.append(cond.get("reason", cond["when"]))
        return result

    def _condition(self, cond: dict[str, Any], args: dict[str, Any], result: PolicyResult) -> bool:
        when, fld = cond.get("when"), cond.get("field")
        value = args.get(fld)
        if when == "has_value":
            return value not in (None, "", [])
        if when == "recipient_outside_company":
            if not isinstance(value, str):
                return False
            if any(r["field"] == fld for r in result.learned_allow):
                return False
            return not self.is_trusted_address(value)
        raise ValueError(f"Unknown policy condition: {when}")
