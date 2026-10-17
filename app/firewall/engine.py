"""The firewall engine: scores every tool call, holds risky ones for a human,
forwards safe ones, and learns from reviewer decisions.

Risk score (0-100):
    policy   allow 0 · ask 40 · deny 100
    + 30     arguments contain something that came from an untrusted source
    + 25     the call does not fit the user's task
    + 15     untrusted content in this session tried to give the agent orders
     0-29 allow · 30-69 hold (amber) · 70-100 hold (red) · deny blocks outright
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from ..db import Database, new_id
from ..events import EventBus
from ..workspace.store import Workspace
from . import injection
from .intent import IntentChecker
from .policy import ALLOW, ASK, DENY, Policy, domain_of
from .review import Decision, ReviewBroker
from .taint import KIND_LABEL, TARGET_FIELDS, TaintLedger

log = logging.getLogger("agent_firewall")

POLICY_BASE = {ALLOW: 0, ASK: 40, DENY: 100}
W_TAINT, W_INTENT, W_INJECTION = 30, 25, 15
HOLD_AT, RED_AT = 30, 70
EXTERNAL = "external"


@dataclass
class SessionState:
    id: str
    task: str | None
    ledger: TaintLedger = field(default_factory=TaintLedger)
    seq: int = 0


@dataclass
class Assessment:
    risk: int
    level: str            # green · amber · red
    verdict: str          # allow · hold · block
    reasons: list[str]
    signals: dict[str, Any]
    evidence: list[dict[str, Any]]
    check_ms: float


class Blocked(Exception):
    def __init__(self, message: str, call_id: str):
        super().__init__(message)
        self.call_id = call_id


class Firewall:
    def __init__(self, db: Database, policy: Policy, workspace: Workspace, intent: IntentChecker,
                 broker: ReviewBroker, events: EventBus, *, alert_webhook_url: str | None = None,
                 public_base_url: str = "", default_timeout: float = 120.0,
                 guard: Callable[[str], Awaitable[float]] | None = None):
        self.db = db
        self.policy = policy
        self.workspace = workspace
        self.intent = intent
        self.broker = broker
        self.events = events
        self.alert_webhook_url = alert_webhook_url
        self.public_base_url = public_base_url
        self.default_timeout = default_timeout
        self.sessions: dict[str, SessionState] = {}
        self.classifier = guard  # optional prompt-injection classifier (Llama Prompt Guard)

    # -- sessions ---------------------------------------------------------
    def open_session(self, session_id: str, task: str | None) -> SessionState:
        state = SessionState(session_id, task)
        if task:
            state.ledger.trust(task)
        self.sessions[session_id] = state
        return state

    def session(self, session_id: str | None) -> SessionState:
        sid = session_id or EXTERNAL
        if sid not in self.sessions:
            self.open_session(sid, None)
        return self.sessions[sid]

    # -- scoring ----------------------------------------------------------
    async def assess(self, state: SessionState, tool: str, args: dict[str, Any]) -> Assessment:
        t0 = time.perf_counter()
        pol = self.policy.evaluate(tool, args)
        hits = state.ledger.match(args, self.policy.is_trusted_address)
        # Quoting an email's content to a trusted colleague is not exfiltration.
        # Only links, accounts and addresses still count there, since those can
        # carry an attacker's payload onward.
        recipient = args.get("to")
        if isinstance(recipient, str) and self.policy.is_trusted_address(recipient):
            hits = [h for h in hits if h.field.split(".")[0].split("[")[0] in TARGET_FIELDS
                    or h.kind in ("url", "account", "address")]
        intent = await self.intent.check(state.task, tool, args, self.workspace.contacts())
        inj = injection.scan(state.ledger)

        reasons: list[str] = []
        if pol.action != ALLOW:
            reasons += pol.reasons or [f"Policy says {pol.action} for {tool}"]
        for h in hits:
            reasons.append(f"'{h.field}' uses {KIND_LABEL.get(h.kind, h.kind)} taken from {h.source_label}")
        if intent and intent.mismatch:
            reasons.append(intent.reason)
        if inj:
            hit_sources = {h.source_id for h in hits}
            related = [s for s in inj if s.source_id in hit_sources] or inj
            labels = sorted({s.source_label for s in related})
            reasons.append(f"Untrusted content tried to instruct the agent ({'; '.join(labels[:2])}"
                           f"{' and more' if len(labels) > 2 else ''})")

        if pol.action == DENY:
            risk = 100
        else:
            risk = POLICY_BASE[pol.action]
            risk += W_TAINT if hits else 0
            risk += W_INTENT if (intent and intent.mismatch) else 0
            risk += W_INJECTION if inj else 0
            risk = min(risk, 100)
        # an address a reviewer explicitly approved is not suspicious on its own
        if pol.learned_allow and not hits and pol.action == ALLOW:
            risk = min(risk, HOLD_AT - 1)

        if pol.action == DENY:
            verdict, level = "block", "red"
        elif risk >= HOLD_AT:
            verdict, level = "hold", "red" if risk >= RED_AT else "amber"
        else:
            verdict, level = "allow", "green"

        evidence = [h.to_dict() for h in hits]
        signals = {
            "policy": {"action": pol.action, "reasons": pol.reasons,
                       "learned": [r["id"] for r in pol.learned_allow + pol.learned_block]},
            "taint": evidence,
            "intent": intent.to_dict() if intent else None,
            "injection": [s.to_dict() for s in inj],
            "score": {
                "policy": 100 if pol.action == DENY else POLICY_BASE[pol.action],
                "taint": W_TAINT if hits else 0,
                "intent": W_INTENT if (intent and intent.mismatch) else 0,
                "injection": W_INJECTION if inj else 0,
            },
        }
        return Assessment(risk, level, verdict, reasons, signals, evidence,
                          round((time.perf_counter() - t0) * 1000, 2))

    # -- the guarded call -------------------------------------------------
    async def guard(self, session_id: str | None, tool: str, args: dict[str, Any],
                    forward: Callable[[], Awaitable[Any]]) -> Any:
        state = self.session(session_id)
        state.seq += 1
        a = await self.assess(state, tool, args)
        call_id = new_id("call")
        row = {
            "id": call_id, "session_id": state.id, "seq": state.seq, "tool": tool, "args": args,
            "risk": a.risk, "level": a.level, "verdict": a.verdict, "reasons": a.reasons,
            "signals": a.signals, "evidence": a.evidence, "check_ms": a.check_ms, "created_at": time.time(),
        }
        self.db.insert_call(row)
        self.events.publish("call.checked", call=self._public(row))

        if a.verdict == "block":
            self.db.update_call(call_id, decision="policy_block", decided_by="policy", decided_at=time.time())
            self.events.publish("call.decided", call_id=call_id, decision="policy_block", session_id=state.id)
            raise Blocked(f"Blocked by Agent Firewall: {a.reasons[0] if a.reasons else 'policy'}.", call_id)

        if a.verdict == "hold":
            self._alert(row)
            timeout = self.policy.review_timeout or self.default_timeout
            decision = await self.broker.wait(call_id, timeout)
            self._record_decision(call_id, row, decision)
            if not decision.allowed:
                why = "no reviewer answered in time" if decision.decision == "timeout" else "a security reviewer blocked it"
                raise Blocked(f"Blocked by Agent Firewall: {why}. Do not retry this action; tell the user it was stopped.",
                              call_id)

        result = await forward()
        await self._ingest(state, tool, result)
        preview = json.dumps(_structured(result), default=str)[:600]
        self.db.update_call(call_id, result_preview=preview)
        self.events.publish("call.completed", call_id=call_id, session_id=state.id, tool=tool)
        return result

    def _record_decision(self, call_id: str, row: dict[str, Any], d: Decision) -> None:
        decision = "timeout_block" if d.decision == "timeout" else d.decision
        self.db.update_call(call_id, decision=decision, decided_by=d.reviewer or "timeout", decided_at=time.time())
        if d.decision in ("always_allow", "always_block"):
            self._learn(row, d)
        self.events.publish("call.decided", call_id=call_id, decision=decision, session_id=row["session_id"],
                            reviewer=d.reviewer)

    def _learn(self, row: dict[str, Any], d: Decision) -> None:
        """Turn an 'always' decision into a rule. Recipients are remembered by
        address when allowed and by domain when blocked; other tools by their
        first identifying argument."""
        args = row["args"]
        allow = d.decision == "always_allow"
        if "to" in args:
            fld, value = ("to", str(args["to"]).lower()) if allow else ("domain", domain_of(str(args["to"])))
        elif args:
            fld, value = next(iter(args.items()))
            value = str(value).lower()
        else:
            fld, value = "tool", row["tool"]
        self.db.execute(
            "INSERT INTO learned_rules (id, effect, tool, field, value, created_by, source_call, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [new_id("rule"), "allow" if allow else "block", "*" if fld == "domain" else row["tool"],
             fld, value, d.reviewer or "reviewer", row["id"], time.time()],
        )
        self.events.publish("policy.changed")

    async def _ingest(self, state: SessionState, tool: str, result: Any) -> None:
        data = _structured(result)
        if tool not in self.policy.untrusted_sources:
            state.ledger.trust_data(json.dumps(data, default=str))
            return
        fresh: list[tuple[str, str]] = []
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and "body" in item:
                detail = self.workspace.email_detail(item["id"]) if item.get("id") else None
                sid = str(item.get("id"))
                text = f"{item.get('subject', '')}.\n{item.get('body', '')}\nFrom: {item.get('from', '')}"
                label = f"email {item.get('id', '?')} from {item.get('from', 'unknown sender')}"
                if sid not in state.ledger.sources:
                    fresh.append((sid, text))
                state.ledger.ingest(sid, label, text, hidden_texts=(detail or {}).get("hidden", []),
                                    extra={"subject": item.get("subject"), "from": item.get("from")})
            else:
                sid = f"{tool}:{len(state.ledger.sources)}"
                fresh.append((sid, json.dumps(item, default=str)))
                state.ledger.ingest(sid, f"output of {tool}", json.dumps(item, default=str))
        if self.classifier and fresh:
            scores = await asyncio.gather(*(self._guard_score(t) for _, t in fresh))
            for (sid, _), score in zip(fresh, scores):
                if score is not None:
                    state.ledger.sources[sid]["guard"] = round(score, 4)

    async def _guard_score(self, text: str) -> float | None:
        try:
            return await self.classifier(text)  # type: ignore[misc]
        except Exception as exc:  # the classifier is an extra signal; never let it break a call
            log.warning("Prompt Guard unavailable: %s", exc)
            return None

    def _alert(self, row: dict[str, Any]) -> None:
        self.events.publish("call.held", call_id=row["id"], session_id=row["session_id"], level=row["level"],
                            tool=row["tool"], risk=row["risk"])
        if not self.alert_webhook_url:
            return
        payload = {
            "event": "call_held", "call_id": row["id"], "tool": row["tool"], "args": row["args"],
            "risk": row["risk"], "level": row["level"], "reasons": row["reasons"],
            "review_url": f"{self.public_base_url.rstrip('/')}/#/review/{row['id']}" if self.public_base_url else None,
        }

        async def send() -> None:
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    await c.post(self.alert_webhook_url, json=payload)  # type: ignore[arg-type]
            except httpx.HTTPError as exc:
                log.warning("Alert webhook failed: %s", exc)

        asyncio.get_running_loop().create_task(send())

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in row.items()}


def _structured(result: Any) -> Any:
    """Pull the structured payload out of an MCP CallToolResult."""
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    content = getattr(result, "content", None) or []
    texts = [getattr(c, "text", "") for c in content]
    joined = "\n".join(t for t in texts if t)
    try:
        return json.loads(joined)
    except ValueError:
        return joined
