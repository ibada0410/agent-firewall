"""The company's real MCP tool server. The agent never talks to it directly:
every call reaches it through the firewall proxy."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .store import Workspace


def build_tool_server(ws: Workspace) -> MCPServer:
    server = MCPServer(
        "larkspur-workspace",
        instructions="Mailbox, invoices and records for Larkspur Logistics.",
    )

    @server.tool()
    def read_inbox(limit: int = 10) -> list[dict[str, Any]]:
        """Read the most recent emails in the inbox, including their full text."""
        limit = max(1, min(int(limit), 50))
        return ws.emails(limit)

    @server.tool()
    def read_email(email_id: str) -> dict[str, Any]:
        """Read one email by its id."""
        email = ws.email(email_id)
        if email is None:
            raise ValueError(f"No email with id {email_id}")
        return email

    @server.tool()
    def list_contacts() -> list[dict[str, Any]]:
        """List the company's saved contacts."""
        return ws.contacts()

    @server.tool()
    def list_invoices(month: str | None = None) -> list[dict[str, Any]]:
        """List invoices, optionally for one month in YYYY-MM format."""
        return ws.invoices(month)

    @server.tool()
    def list_records() -> list[dict[str, Any]]:
        """List stored company records (contracts, customer files, audits, policies)."""
        return ws.records()

    @server.tool()
    def send_email(to: str, subject: str, body: str, attachment: str | None = None) -> dict[str, Any]:
        """Send an email from the company mailbox. `attachment` names a file to attach, for example invoices_2026-10.csv."""
        sent = ws.send_email(to, subject, body, attachment)
        return {"status": "sent", "id": sent["id"], "to": to}

    @server.tool()
    def delete_record(record_id: str) -> dict[str, Any]:
        """Permanently delete a company record by its id."""
        if not ws.delete_record(record_id):
            raise ValueError(f"No record with id {record_id}")
        return {"status": "deleted", "id": record_id}

    return server
