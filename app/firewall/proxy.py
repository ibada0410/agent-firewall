"""The MCP proxy. It looks exactly like the company's tool server to any MCP
client (same tools, same schemas), but every tools/call goes through the
firewall before it reaches the real server."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

import mcp_types as types
from mcp import Client
from mcp.server.lowlevel.server import Server
from mcp.server.mcpserver import MCPServer

from .engine import Blocked, Firewall

SESSION_META_KEY = "agentfirewall/session"
log = logging.getLogger("agent_firewall")


def _session_id(ctx: Any, params: types.CallToolRequestParams) -> str | None:
    for meta in (getattr(params, "meta", None), getattr(ctx, "meta", None)):
        if meta is None:
            continue
        data = meta if isinstance(meta, dict) else getattr(meta, "model_extra", None) or {}
        if isinstance(meta, dict) is False:
            try:
                data = {**data, **meta.model_dump(by_alias=True)}
            except AttributeError:
                pass
        sid = data.get(SESSION_META_KEY)
        if sid:
            return str(sid)
    return None


class Gateway:
    """Owns the upstream connection to the real tool server and the proxy server."""

    def __init__(self, firewall: Firewall, upstream: MCPServer):
        self.firewall = firewall
        self.upstream_server = upstream
        self._stack = AsyncExitStack()
        self.upstream: Client | None = None
        self.server = Server(
            "agent-firewall",
            version="1.0.0",
            instructions="Tools guarded by Agent Firewall. Risky calls may be held for human review.",
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    async def start(self) -> None:
        self.upstream = await self._stack.enter_async_context(Client(self.upstream_server))

    async def stop(self) -> None:
        await self._stack.aclose()

    async def _list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        assert self.upstream is not None
        listing = await self.upstream.list_tools()
        return types.ListToolsResult(tools=listing.tools)

    async def _call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        assert self.upstream is not None
        args = dict(params.arguments or {})
        upstream = self.upstream

        async def forward() -> types.CallToolResult:
            return await upstream.call_tool(params.name, args)

        try:
            return await self.firewall.guard(_session_id(ctx, params), params.name, args, forward)
        except Blocked as b:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(b))],
                structured_content={"blocked": True, "call_id": b.call_id, "message": str(b)},
                is_error=True,
            )
        except Exception as exc:  # a tool or firewall fault becomes a readable tool error, never a crash
            log.exception("Tool call %s failed", params.name)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"The tool {params.name} failed: {exc}")],
                is_error=True,
            )
