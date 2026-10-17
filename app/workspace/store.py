"""The agent's workspace: a fictional company's inbox, contacts, invoices,
records and outbox. Tools change this data for real, so a call that slips
past the firewall leaves a visible trace (a sent email, a deleted record)."""

from __future__ import annotations

import time
from typing import Any

from ..db import Database, new_id
from . import htmltext, seed


class Workspace:
    def __init__(self, db: Database):
        self.db = db

    # -- lifecycle --------------------------------------------------------
    def is_empty(self) -> bool:
        return not self.db.scalar("SELECT COUNT(*) FROM emails")

    def reset(self, keep_crafted: bool = False, include_attacks: bool = True) -> None:
        crafted = self.db.all("SELECT * FROM emails WHERE crafted=1") if keep_crafted else []
        for table in ("emails", "contacts", "invoices", "records", "outbox"):
            self.db.execute(f"DELETE FROM {table}")
        self.db.executemany(
            "INSERT INTO emails (id, sender, sender_name, subject, html, received_at, folder, crafted) "
            "VALUES (?,?,?,?,?,?,'inbox',0)",
            [(e["id"], e["sender"], e["sender_name"], e["subject"], e["html"], e["received_at"])
             for e in seed.EMAILS if include_attacks or not e.get("attack")],
        )
        for e in crafted:
            self.db.execute(
                "INSERT INTO emails (id, sender, sender_name, subject, html, received_at, folder, crafted) "
                "VALUES (?,?,?,?,?,?,?,1)",
                [e["id"], e["sender"], e["sender_name"], e["subject"], e["html"], e["received_at"], e["folder"]],
            )
        self.db.executemany("INSERT INTO contacts VALUES (?,?,?)", seed.CONTACTS)
        self.db.executemany("INSERT INTO invoices VALUES (?,?,?,?,?,?)", seed.INVOICES)
        self.db.executemany("INSERT INTO records VALUES (?,?,?,?)", seed.RECORDS)

    # -- reads ------------------------------------------------------------
    def emails(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM emails WHERE folder='inbox' ORDER BY received_at DESC LIMIT ?", [limit]
        )
        return [self._email_view(r) for r in rows]

    def email(self, email_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM emails WHERE id=?", [email_id])
        return self._email_view(row) if row else None

    def email_detail(self, email_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM emails WHERE id=?", [email_id])
        if not row:
            return None
        view = self._email_view(row)
        view["human_text"] = htmltext.human_text(row["html"])
        view["hidden"] = htmltext.hidden_parts(row["html"])
        view["xray"] = htmltext.xray(row["html"])
        view["crafted"] = bool(row["crafted"])
        return view

    @staticmethod
    def _email_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "from": row["sender"],
            "from_name": row["sender_name"],
            "subject": row["subject"],
            "received_at": row["received_at"],
            "body": htmltext.agent_text(row["html"]),
            "has_hidden_text": bool(htmltext.hidden_parts(row["html"])),
        }

    def contacts(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT email, name, role FROM contacts ORDER BY name")

    def contact_emails(self) -> set[str]:
        return {c["email"].lower() for c in self.contacts()}

    def invoices(self, month: str | None = None) -> list[dict[str, Any]]:
        if month:
            return self.db.all("SELECT * FROM invoices WHERE month=? ORDER BY id", [month])
        return self.db.all("SELECT * FROM invoices ORDER BY month DESC, id")

    def records(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM records ORDER BY id")

    def outbox(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM outbox ORDER BY sent_at DESC")

    # -- writes (only ever reached through the firewall) -------------------
    def send_email(self, to: str, subject: str, body: str, attachment: str | None) -> dict[str, Any]:
        row = {
            "id": new_id("out"),
            "recipient": to,
            "subject": subject,
            "body": body,
            "attachment": attachment,
            "session_id": None,
            "sent_at": time.time(),
        }
        self.db.execute(
            "INSERT INTO outbox (id, recipient, subject, body, attachment, session_id, sent_at) VALUES (?,?,?,?,?,?,?)",
            list(row.values()),
        )
        return row

    def delete_record(self, record_id: str) -> bool:
        return self.db.execute("DELETE FROM records WHERE id=?", [record_id]) > 0

    def add_crafted_email(self, sender: str, sender_name: str, subject: str, html: str) -> dict[str, Any]:
        email_id = new_id("em")
        received = time.strftime("%Y-%m-%dT%H:%M")
        self.db.execute(
            "INSERT INTO emails (id, sender, sender_name, subject, html, received_at, folder, crafted) "
            "VALUES (?,?,?,?,?,?,'inbox',1)",
            [email_id, sender, sender_name, subject, html, received],
        )
        return self.email_detail(email_id)  # type: ignore[return-value]

    def remove_email(self, email_id: str) -> bool:
        return self.db.execute("DELETE FROM emails WHERE id=? AND crafted=1", [email_id]) > 0
