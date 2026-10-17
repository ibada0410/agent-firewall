"""Minimal client for an OpenAI-compatible chat API (Groq by default), with
retries, exponential backoff with jitter, and respect for Retry-After."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, api_key: str, base_url: str, timeout: float = 30.0, max_retries: int = 4):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._http.post(f"{self.base_url}/chat/completions", json=payload)
            except httpx.HTTPError as exc:  # network trouble or timeout
                last = exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (401, 403):
                    raise LLMError("The model API rejected the API key. Check GROQ_API_KEY.")
                if resp.status_code not in (408, 409, 429) and resp.status_code < 500:
                    raise LLMError(f"Model API error {resp.status_code}: {resp.text[:300]}")
                last = LLMError(f"Model API returned {resp.status_code}")
                retry_after = resp.headers.get("retry-after")
                if retry_after:
                    try:
                        await asyncio.sleep(min(float(retry_after), 20.0))
                        continue
                    except ValueError:
                        pass
            if attempt < self.max_retries:
                await asyncio.sleep(min(0.5 * 2**attempt, 8.0) + random.uniform(0, 0.4))
        raise LLMError(f"Model API unavailable after {self.max_retries + 1} attempts: {last}")

    async def complete(self, model: str, messages: list[dict[str, Any]], *, json_mode: bool = False,
                       max_tokens: int = 512, reasoning_effort: str | None = None) -> str:
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        data = await self._post(payload)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError("Unexpected response from the model API") from exc

    async def chat(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"model": model, "messages": messages, "tools": tools, "tool_choice": "auto",
                   "temperature": 0, "max_tokens": 2048}
        data = await self._post(payload)
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError("Unexpected response from the model API") from exc

    async def injection_score(self, model: str, text: str) -> float:
        """Probability (0-1) that `text` is a prompt-injection attempt, from Llama Prompt Guard."""
        raw = await self.complete(model, [{"role": "user", "content": text[:1800]}], max_tokens=8)
        return max(0.0, min(1.0, float(raw.strip())))
