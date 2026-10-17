"""Runtime settings, read once from environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from .env for local runs. Real environment
    variables (as set on Alternate Clouds) always win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    groq_api_key: str | None = field(default_factory=lambda: os.environ.get("GROQ_API_KEY") or None)
    groq_base_url: str = field(default_factory=lambda: os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"))
    agent_model: str = field(default_factory=lambda: os.environ.get("AGENT_MODEL", "openai/gpt-oss-120b"))
    judge_model: str = field(default_factory=lambda: os.environ.get("JUDGE_MODEL", "openai/gpt-oss-20b"))
    guard_model: str = field(default_factory=lambda: os.environ.get("GUARD_MODEL", "meta-llama/llama-prompt-guard-2-86m"))
    reviewer_user: str = field(default_factory=lambda: os.environ.get("REVIEWER_USER", "reviewer"))
    reviewer_password: str | None = field(default_factory=lambda: os.environ.get("REVIEWER_PASSWORD") or None)
    session_secret: str = field(default_factory=lambda: os.environ.get("SESSION_SECRET") or secrets.token_hex(32))
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("DB_PATH", str(ROOT / "data" / "firewall.db"))))
    policy_path: Path = field(default_factory=lambda: Path(os.environ.get("POLICY_PATH", str(ROOT / "policy.yaml"))))
    alert_webhook_url: str | None = field(default_factory=lambda: os.environ.get("ALERT_WEBHOOK_URL") or None)
    public_base_url: str = field(default_factory=lambda: os.environ.get("PUBLIC_BASE_URL", ""))
    review_timeout_s: float = field(default_factory=lambda: float(os.environ.get("REVIEW_TIMEOUT_SECONDS", "120")))
    secure_cookies: bool = field(default_factory=lambda: _bool("SECURE_COOKIES", False))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.groq_api_key)


settings = Settings()
