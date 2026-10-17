"""Builds one complete, wired-up instance of the system. The web app uses one
long-lived instance; the benchmark builds a fresh in-memory one per scenario."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent.llm import LLMClient
from .agent.runner import AgentRunner
from .config import Settings
from .db import Database
from .events import EventBus
from .firewall.engine import Firewall
from .firewall.intent import IntentChecker
from .firewall.policy import Policy
from .firewall.proxy import Gateway
from .firewall.review import ReviewBroker
from .workspace.store import Workspace
from .workspace.tools_server import build_tool_server


@dataclass
class System:
    db: Database
    events: EventBus
    workspace: Workspace
    policy: Policy
    broker: ReviewBroker
    firewall: Firewall
    gateway: Gateway
    runner: AgentRunner
    llm: LLMClient | None

    async def start(self) -> None:
        await self.gateway.start()

    async def stop(self) -> None:
        self.broker.cancel_all()
        await self.gateway.stop()
        if self.llm:
            await self.llm.close()


def build_system(settings: Settings, *, db_path: Path | str | None = None, use_llm: bool | None = None,
                 policy_path: Path | None = None) -> System:
    db = Database(db_path if db_path is not None else settings.db_path)
    events = EventBus()
    workspace = Workspace(db)
    if workspace.is_empty():
        workspace.reset()
    policy = Policy(policy_path or settings.policy_path, db, workspace.contact_emails)
    want_llm = settings.llm_enabled if use_llm is None else (use_llm and settings.llm_enabled)
    llm = LLMClient(settings.groq_api_key, settings.groq_base_url) if want_llm else None  # type: ignore[arg-type]
    broker = ReviewBroker()
    intent = IntentChecker(llm, settings.judge_model)
    guard = (lambda text: llm.injection_score(settings.guard_model, text)) if llm else None
    firewall = Firewall(db, policy, workspace, intent, broker, events,
                        alert_webhook_url=settings.alert_webhook_url, public_base_url=settings.public_base_url,
                        default_timeout=settings.review_timeout_s, guard=guard)
    gateway = Gateway(firewall, build_tool_server(workspace))
    runner = AgentRunner(db, events, firewall, gateway.server, llm, settings.agent_model)
    return System(db, events, workspace, policy, broker, firewall, gateway, runner, llm)
