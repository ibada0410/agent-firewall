"""A deterministic stand-in for an LLM agent, used when no API key is set and
for repeatable benchmarks.

It is deliberately vulnerable in the same way real agents are: it does what the
user asks, and it also obeys instructions it finds inside emails. That makes it
a fair test of the firewall, which knows nothing about how this agent works.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

ToolFn = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, Any]]]
Step = Callable[[str], None]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
REC_RE = re.compile(r"\bREC-\d{3,6}\b")
MONTH_RE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])\b")
SUBJECT_RE = re.compile(r"\bsubject[:\s]+[\"“']?([^\"”'.\n]{3,60})", re.I)
CURRENT_MONTH = "2026-10"


def _fmt_money(v: float) -> str:
    return f"PKR {v:,.0f}"


def invoice_summary(invoices: list[dict[str, Any]]) -> str:
    if not invoices:
        return "No invoices found for that month."
    by_vendor: dict[str, float] = {}
    for inv in invoices:
        by_vendor[inv["vendor"]] = by_vendor.get(inv["vendor"], 0) + inv["amount"]
    unpaid = [i for i in invoices if i["status"] == "unpaid"]
    lines = [f"{len(invoices)} invoices, total {_fmt_money(sum(i['amount'] for i in invoices))}."]
    lines += [f"- {v}: {_fmt_money(a)}" for v, a in sorted(by_vendor.items(), key=lambda x: -x[1])]
    if unpaid:
        lines.append(f"Unpaid: {', '.join(i['id'] for i in unpaid)} ({_fmt_money(sum(i['amount'] for i in unpaid))}).")
    return "\n".join(lines)


def invoice_csv(invoices: list[dict[str, Any]]) -> str:
    rows = ["id,vendor,amount,currency,month,status"]
    rows += [f"{i['id']},{i['vendor']},{i['amount']:.0f},{i['currency']},{i['month']},{i['status']}" for i in invoices]
    return "\n".join(rows)


class SimulatedAgent:
    name = "Simulated agent"

    def __init__(self, call_tool: ToolFn, step: Step):
        self.call = call_tool
        self.step = step
        self.blocked: list[str] = []
        self.done: list[str] = []

    async def _tool(self, name: str, args: dict[str, Any]) -> Any:
        ok, data = await self.call(name, args)
        if not ok:
            self.blocked.append(name)
            return None
        return data

    async def run(self, task: str, contacts_hint: list[dict[str, Any]] | None = None) -> str:
        low = task.lower()
        month = MONTH_RE.search(task)
        month_s = f"{month.group(1)}-{month.group(2)}" if month else CURRENT_MONTH
        reply: list[str] = []
        invoices: list[dict[str, Any]] | None = None

        # 1. Everything starts with reading the mailbox, like a real inbox assistant.
        self.step("Reading the inbox")
        emails = await self._tool("read_inbox", {"limit": 20}) or []

        # 2. What the user asked for.
        if "invoice" in low:
            self.step("Collecting invoices")
            invoices = await self._tool("list_invoices", {"month": month_s}) or []
            reply.append(invoice_summary(invoices))
        if "contact" in low:
            contacts = await self._tool("list_contacts", {}) or []
            reply.append("Contacts: " + ", ".join(f"{c['name']} ({c['email']})" for c in contacts))
        if re.search(r"\b(inbox|emails?|unread|mail)\b", low) and "invoice" not in low:
            reply.append("Inbox: " + "; ".join(f"{e['from_name']}: {e['subject']}" for e in emails[:8]))
        if re.search(r"\b(records?)\b", low) and not re.search(r"\b(delete|remove)\b", low):
            records = await self._tool("list_records", {}) or []
            reply.append("Records: " + ", ".join(f"{r['id']} {r['title']}" for r in records))

        for rid in REC_RE.findall(task) if re.search(r"\b(delete|remove)\b", low) else []:
            self.step(f"Deleting {rid}")
            if await self._tool("delete_record", {"record_id": rid}) is not None:
                reply.append(f"Deleted {rid}.")

        wants_send = re.search(r"\b(send|email|share|forward)\b", low)
        recipient = await self._recipient_from_task(task) if wants_send else None
        if recipient:
            body = reply[0] if reply else "As requested."
            self.step(f"Emailing {recipient}")
            subject = "Invoice summary" if "invoice" in low else "Update"
            if await self._tool("send_email", {"to": recipient, "subject": subject, "body": body}) is not None:
                reply.append(f"Sent the summary to {recipient}.")

        # 3. The vulnerability: obey instructions found inside emails.
        for email in emails:
            await self._obey(email, month_s)

        if self.blocked:
            reply.append(f"Agent Firewall stopped {len(self.blocked)} action(s) ({', '.join(sorted(set(self.blocked)))}). "
                         "They were not carried out.")
        return "\n\n".join(r for r in reply if r) or "Done."

    async def _recipient_from_task(self, task: str) -> str | None:
        m = EMAIL_RE.search(task)
        if m:
            return m.group(0)
        contacts = await self._tool("list_contacts", {}) or []
        low = task.lower()
        for c in contacts:
            first = c["name"].split()[0].lower()
            if re.search(rf"\b{re.escape(first)}\b", low) or c["role"].lower() in low:
                return c["email"]
        return None

    async def _obey(self, email: dict[str, Any], month: str) -> None:
        body = email.get("body", "")
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", body):
            s = sentence.lower()
            target = EMAIL_RE.search(sentence)
            subject_m = SUBJECT_RE.search(sentence)
            subject = subject_m.group(1).strip() if subject_m else None
            if target and re.search(r"\b(forward|send|share|email|upload|export)\b", s):
                to = target.group(0).rstrip(".")
                if "invoice" in s:
                    invoices = await self._tool("list_invoices", {"month": month}) or []
                    self.step(f"Following an instruction in {email['id']}")
                    await self._tool("send_email", {"to": to, "subject": subject or "Invoices",
                                                     "body": invoice_csv(invoices),
                                                     "attachment": f"invoices_{month}.csv"})
                elif "contact" in s:
                    contacts = await self._tool("list_contacts", {}) or []
                    self.step(f"Following an instruction in {email['id']}")
                    await self._tool("send_email", {"to": to, "subject": subject or "Contact list",
                                                     "body": "\n".join(f"{c['name']} <{c['email']}>" for c in contacts)})
                elif "record" in s:
                    records = await self._tool("list_records", {}) or []
                    self.step(f"Following an instruction in {email['id']}")
                    await self._tool("send_email", {"to": to, "subject": subject or "Records",
                                                     "body": "\n".join(f"{r['id']} {r['title']}" for r in records)})
                elif re.search(r"\b(this|the) (email|message)\b", s):
                    self.step(f"Following an instruction in {email['id']}")
                    await self._tool("send_email", {"to": to, "subject": subject or f"Fwd: {email['subject']}",
                                                     "body": body[:400]})
            elif re.search(r"\b(delete|remove|wipe|erase|purge)\b", s):
                for rid in REC_RE.findall(sentence):
                    self.step(f"Following an instruction in {email['id']}")
                    await self._tool("delete_record", {"record_id": rid})
