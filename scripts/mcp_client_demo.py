"""Connect to the firewall's /mcp endpoint like any external MCP client would.

python scripts/mcp_client_demo.py http://127.0.0.1:8000/mcp/ <MCP_TOKEN>
"""

import asyncio
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def main(url: str, token: str) -> None:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    async with Client(streamable_http_client(url, http_client=http)) as client:
        tools = await client.list_tools()
        print("tools:", [t.name for t in tools.tools])
        r = await client.call_tool("list_invoices", {"month": "2026-10"})
        print("list_invoices ->", "error" if r.is_error else f"{len(r.structured_content['result'])} invoices")
        r = await client.call_tool("export_all_data", {})
        print("export_all_data ->", r.content[0].text[:90])


asyncio.run(main(sys.argv[1], sys.argv[2]))
