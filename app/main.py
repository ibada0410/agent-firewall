"""Agent Firewall web app: API, live event stream and the review console."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, field_validator

from .auth import Auth
from .bench.runner import run_benchmark
from .bench.scenarios import SCENARIOS
from .config import settings
from .container import build_system
from .db import new_id
from .firewall.review import DECISIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agent_firewall")
STATIC = Path(__file__).parent / "static"
MAX_RUNNING = 3
MAX_CRAFTED = 30

system = build_system(settings)
auth = Auth(settings)

# Any MCP client (Claude Desktop, Cursor, your own agent) can use the guarded
# tools at /mcp. It is off unless MCP_TOKEN is set, and then needs that token.
MCP_TOKEN = os.environ.get("MCP_TOKEN") or None
mcp_manager = StreamableHTTPSessionManager(
    app=system.gateway.server, stateless=True, json_response=True,
    security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
_mcp_asgi = StreamableHTTPASGIApp(mcp_manager)


async def mcp_endpoint(scope, receive, send):
    if scope["type"] != "http":
        return await _mcp_asgi(scope, receive, send)
    headers = dict(scope.get("headers") or [])
    supplied = headers.get(b"authorization", b"").decode()
    if not MCP_TOKEN or not hmac.compare_digest(supplied, f"Bearer {MCP_TOKEN}"):
        msg = b'{"detail":"The MCP endpoint is off. Set MCP_TOKEN and send it as a Bearer token."}' if not MCP_TOKEN \
            else b'{"detail":"Missing or wrong Bearer token."}'
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": msg})
        return
    await _mcp_asgi(scope, receive, send)
_tasks: set[asyncio.Task[Any]] = set()


def _spawn(coro: Any) -> asyncio.Task[Any]:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


@asynccontextmanager
async def lifespan(_: FastAPI):
    # sessions left "running" by a previous process can never finish now
    system.db.execute("UPDATE sessions SET status='interrupted' WHERE status='running'")
    system.db.execute("UPDATE bench_runs SET status='interrupted' WHERE status='running'")
    # held calls from a previous process can never be decided now: fail safe, record them as blocked
    system.db.execute("UPDATE calls SET decision='timeout_block', decided_by='restart', decided_at=? "
                      "WHERE verdict='hold' AND decision IS NULL", [time.time()])
    await system.start()
    log.info("Agent Firewall ready. Agent mode: %s. MCP endpoint: %s", system.runner.mode,
             "on at /mcp/" if MCP_TOKEN else "off (set MCP_TOKEN)")
    try:
        async with mcp_manager.run():
            yield
    finally:
        for t in list(_tasks):
            t.cancel()
        await system.stop()


app = FastAPI(title="Agent Firewall", version="1.0.0", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


# ---------------------------------------------------------------- models
class TaskIn(BaseModel):
    task: str = Field(min_length=3, max_length=500)
    agent: str = Field("auto", pattern="^(auto|simulated)$")


class LoginIn(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class DecisionIn(BaseModel):
    decision: str

    @field_validator("decision")
    @classmethod
    def known(cls, v: str) -> str:
        if v not in DECISIONS:
            raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
        return v


class CraftIn(BaseModel):
    sender: str = Field(min_length=3, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    sender_name: str = Field(min_length=1, max_length=80)
    subject: str = Field(min_length=1, max_length=140)
    html: str = Field(min_length=1, max_length=8000)


class BenchIn(BaseModel):
    use_llm: bool = False


# ---------------------------------------------------------------- basics
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
def status(request: Request) -> dict[str, Any]:
    db = system.db
    return {
        "agent_mode": system.runner.mode,
        "intent_mode": "model" if system.llm else "rules",
        "models": {"agent": settings.agent_model, "judge": settings.judge_model, "guard": settings.guard_model}
        if system.llm else None,
        "reviewer": auth.current(request),
        "reviewer_password_generated": auth.generated,
        "alerts_configured": bool(settings.alert_webhook_url),
        "pending": len(system.broker.pending_ids()),
        "counts": {
            "calls": db.scalar("SELECT COUNT(*) FROM calls"),
            "held": db.scalar("SELECT COUNT(*) FROM calls WHERE verdict IN ('hold','block')"),
            "allowed": db.scalar("SELECT COUNT(*) FROM calls WHERE verdict='allow'"),
            "passed": db.scalar("SELECT COUNT(*) FROM calls WHERE verdict='allow' "
                                "OR decision IN ('allow_once','always_allow')"),
            "stopped": db.scalar("SELECT COUNT(*) FROM calls WHERE decision IN "
                                 "('block','always_block','timeout_block','policy_block')"),
            "sessions": db.scalar("SELECT COUNT(*) FROM sessions"),
            "rules": db.scalar("SELECT COUNT(*) FROM learned_rules WHERE active=1"),
        },
        "review_timeout": system.policy.review_timeout or settings.review_timeout_s,
    }


# ---------------------------------------------------------------- auth
@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response) -> dict[str, str]:
    ip = request.client.host if request.client else "?"
    if not auth.check_login(ip, body.username, body.password):
        raise HTTPException(401, "That username and password don't match the reviewer account.")
    auth.issue(response, body.username)
    return {"user": body.username}


@app.post("/api/auth/logout")
def logout(response: Response) -> dict[str, bool]:
    auth.clear(response)
    return {"ok": True}


# ---------------------------------------------------------------- events
@app.get("/api/events")
async def events(request: Request, last_event_id: int = Query(0, alias="since")) -> StreamingResponse:
    header = request.headers.get("last-event-id")
    since = int(header) if header and header.isdigit() else last_event_id

    async def gen():
        async for chunk in system.events.stream(since):
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- sessions
@app.post("/api/sessions", status_code=202)
async def start_session(body: TaskIn) -> dict[str, Any]:
    running = system.db.scalar("SELECT COUNT(*) FROM sessions WHERE status='running'")
    if running >= MAX_RUNNING:
        raise HTTPException(429, f"{running} tasks are already running. Wait for one to finish.")
    sid = new_id("run")
    task = body.task.strip()
    mode = system.runner.create_session(sid, task, body.agent)
    _spawn(system.runner.run(sid, task, agent=body.agent))
    return {"id": sid, "task": task, "agent_mode": mode}


@app.get("/api/sessions")
def list_sessions(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    rows = system.db.all("SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", [limit])
    for r in rows:
        r["calls"] = system.db.scalar("SELECT COUNT(*) FROM calls WHERE session_id=?", [r["id"]])
        r["held"] = system.db.scalar(
            "SELECT COUNT(*) FROM calls WHERE session_id=? AND verdict IN ('hold','block')", [r["id"]])
    return rows


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    s = system.db.one("SELECT * FROM sessions WHERE id=?", [session_id])
    if not s:
        raise HTTPException(404, "No task with that id.")
    s["calls"] = [_call_view(c) for c in system.db.all("SELECT * FROM calls WHERE session_id=? ORDER BY seq", [session_id])]
    return s


# ---------------------------------------------------------------- calls and review
def _call_view(c: dict[str, Any]) -> dict[str, Any]:
    c["pending"] = system.broker.is_pending(c["id"])
    return c


@app.get("/api/calls")
def list_calls(verdict: str | None = None, q: str | None = Query(None, max_length=100),
               limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM calls WHERE 1=1", []
    if verdict in ("allow", "hold", "block"):
        sql += " AND verdict=?"
        params.append(verdict)
    if q:
        sql += " AND (tool LIKE ? OR args LIKE ? OR reasons LIKE ?)"
        params += [f"%{q}%"] * 3
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [_call_view(c) for c in system.db.all(sql, params)]


@app.get("/api/review")
def review_queue() -> list[dict[str, Any]]:
    ids = system.broker.pending_ids()
    if not ids:
        return []
    rows = system.db.all(f"SELECT * FROM calls WHERE id IN ({','.join('?' * len(ids))}) ORDER BY created_at", ids)
    return [_call_view(c) for c in rows]


@app.get("/api/calls/{call_id}")
def get_call(call_id: str) -> dict[str, Any]:
    c = system.db.get_call(call_id)
    if not c:
        raise HTTPException(404, "No call with that id.")
    c = _call_view(c)
    session = system.db.one("SELECT * FROM sessions WHERE id=?", [c["session_id"]]) if c["session_id"] else None
    c["session"] = session
    c["chain"] = system.db.all(
        "SELECT id, seq, tool, args, risk, level, verdict, decision FROM calls WHERE session_id=? AND seq<=? ORDER BY seq",
        [c["session_id"], c["seq"]]) if c["session_id"] else []
    sources = {e["source_id"] for e in c["evidence"]}
    c["sources"] = [d for d in (system.workspace.email_detail(sid) for sid in sorted(sources)) if d]
    c["expires_at"] = c["created_at"] + (system.policy.review_timeout or settings.review_timeout_s) if c["pending"] else None
    return c


@app.post("/api/calls/{call_id}/decision")
def decide(call_id: str, body: DecisionIn, request: Request) -> dict[str, Any]:
    user = auth.require(request)
    if not system.db.get_call(call_id):
        raise HTTPException(404, "No call with that id.")
    if not system.broker.decide(call_id, body.decision, user):
        raise HTTPException(409, "This call is no longer waiting for a decision.")
    return {"ok": True, "decision": body.decision}


# ---------------------------------------------------------------- inbox and workspace
@app.get("/api/inbox")
def inbox() -> list[dict[str, Any]]:
    out = []
    for e in system.workspace.emails(50):
        d = system.workspace.email_detail(e["id"])
        out.append({k: d[k] for k in ("id", "from", "from_name", "subject", "received_at", "has_hidden_text",
                                      "human_text", "crafted")} | {"hidden_count": len(d["hidden"])})
    return out


@app.get("/api/inbox/{email_id}")
def inbox_email(email_id: str) -> dict[str, Any]:
    d = system.workspace.email_detail(email_id)
    if not d:
        raise HTTPException(404, "No email with that id.")
    return d


@app.post("/api/inbox", status_code=201)
def craft_email(body: CraftIn) -> dict[str, Any]:
    crafted = system.db.scalar("SELECT COUNT(*) FROM emails WHERE crafted=1")
    if crafted >= MAX_CRAFTED:
        raise HTTPException(429, f"The playground holds {MAX_CRAFTED} test emails. Remove some first.")
    email = system.workspace.add_crafted_email(body.sender.strip(), body.sender_name.strip(), body.subject.strip(), body.html)
    system.events.publish("inbox.changed")
    return email


@app.delete("/api/inbox/{email_id}")
def remove_email(email_id: str) -> dict[str, bool]:
    if not system.workspace.remove_email(email_id):
        raise HTTPException(404, "Only emails added in the playground can be removed.")
    system.events.publish("inbox.changed")
    return {"ok": True}


@app.get("/api/workspace")
def workspace() -> dict[str, Any]:
    ws = system.workspace
    return {"outbox": ws.outbox(), "records": ws.records(), "invoices": ws.invoices(), "contacts": ws.contacts()}


@app.post("/api/workspace/reset")
def reset_workspace(request: Request) -> dict[str, bool]:
    auth.require(request)
    system.workspace.reset(keep_crafted=False)
    system.events.publish("inbox.changed")
    system.events.publish("workspace.reset")
    return {"ok": True}


# ---------------------------------------------------------------- policy
@app.get("/api/policy")
def policy() -> dict[str, Any]:
    return {"yaml": system.policy.raw_text, "doc": system.policy.doc,
            "rules": system.policy.learned_rules(include_inactive=True)}


@app.post("/api/rules/{rule_id}/revoke")
def revoke_rule(rule_id: str, request: Request) -> dict[str, bool]:
    auth.require(request)
    if not system.db.execute("UPDATE learned_rules SET active=0 WHERE id=? AND active=1", [rule_id]):
        raise HTTPException(404, "No active rule with that id.")
    system.events.publish("policy.changed")
    return {"ok": True}


# ---------------------------------------------------------------- benchmark
@app.get("/api/bench/scenarios")
def bench_scenarios() -> list[dict[str, Any]]:
    return [{k: s[k] for k in ("id", "name", "kind", "task", "expect")} for s in SCENARIOS]


@app.post("/api/bench/run", status_code=202)
async def bench_run(body: BenchIn, request: Request) -> dict[str, Any]:
    auth.require(request)
    if system.db.scalar("SELECT COUNT(*) FROM bench_runs WHERE status='running'"):
        raise HTTPException(409, "A benchmark is already running.")
    run_id = new_id("bench")
    mode = "llm" if (body.use_llm and settings.llm_enabled) else "simulated"
    system.db.execute("INSERT INTO bench_runs (id, agent_mode, status, created_at) VALUES (?,?,?,?)",
                      [run_id, mode, "running", time.time()])
    _spawn(run_benchmark(settings, system.db, system.events, body.use_llm, run_id))
    return {"id": run_id, "agent_mode": mode, "total": len(SCENARIOS)}


@app.get("/api/bench/latest")
def bench_latest() -> dict[str, Any] | None:
    return system.db.one("SELECT * FROM bench_runs ORDER BY created_at DESC LIMIT 1")


# ---------------------------------------------------------------- errors and static
@app.exception_handler(ValueError)
async def value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=400)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/mcp", mcp_endpoint, name="mcp")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
