"""One small adapter per AI provider. Each returns (json_text, tokens_in, tokens_out).

All use structured JSON output where the provider has it. Errors are raised as
`RateLimited`, `TimeoutError` or `ProviderError` so the advisor can fail over.
API keys come from `Secrets` and never appear in logs or messages (invariant 18).
"""

from __future__ import annotations

from typing import Any, Protocol

import anthropic
import httpx2 as httpx

from app.config.schema import AISlot
from app.config.secrets import Secrets


class ProviderError(Exception):
    pass


class RateLimited(ProviderError):
    pass


class Provider(Protocol):
    name: str

    async def complete(
        self, system: str, user: str, schema: dict[str, Any], timeout_s: float, max_out: int
    ) -> tuple[str, int | None, int | None]: ...


def _status(r: httpx.Response) -> None:
    if r.status_code == 429:
        raise RateLimited("HTTP 429")
    if r.status_code >= 400:
        raise ProviderError(f"HTTP {r.status_code}")  # body may echo the request: not logged


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, slot: AISlot, key: str) -> None:
        self.slot = slot
        self.client = anthropic.AsyncAnthropic(api_key=key, max_retries=0)

    async def complete(
        self, system: str, user: str, schema: dict[str, Any], timeout_s: float, max_out: int
    ) -> tuple[str, int | None, int | None]:
        try:
            r = await self.client.with_options(timeout=timeout_s).messages.create(
                model=self.slot.model,
                max_tokens=max_out,
                system=system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "disabled"},  # a 1.5 s verdict: no thinking (Haiku 5.5)
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except anthropic.APITimeoutError as e:
            raise TimeoutError("anthropic timeout") from e
        except anthropic.RateLimitError as e:
            raise RateLimited("anthropic 429") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"anthropic HTTP {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("anthropic connection error") from e
        if r.stop_reason == "refusal":
            raise ProviderError("anthropic refused")
        text = next((b.text for b in r.content if b.type == "text"), "")
        return text, r.usage.input_tokens, r.usage.output_tokens


class GeminiProvider:
    """Gemini Interactions API (aistudio.google.com/api-reference/interactions-api)."""

    name = "gemini"
    URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

    def __init__(self, slot: AISlot, key: str) -> None:
        self.slot, self._key = slot, key

    async def complete(
        self, system: str, user: str, schema: dict[str, Any], timeout_s: float, max_out: int
    ) -> tuple[str, int | None, int | None]:
        body = {
            "model": self.slot.model,
            "system_instruction": system,
            "input": user,
            "store": False,
            "response_format": {"type": "text", "mime_type": "application/json", "schema": schema},
            "generation_config": {"max_output_tokens": max_out, "thinking_level": "minimal"},
        }
        headers = {
            "x-goog-api-key": self._key,
            "Content-Type": "application/json",
            "Api-Revision": "2026-05-20",
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as c:
                r = await c.post(self.URL, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise TimeoutError("gemini timeout") from e
        except httpx.HTTPError as e:
            raise ProviderError("gemini connection error") from e
        _status(r)
        d = r.json()
        text = "".join(
            c.get("text", "")
            for st in d.get("steps", [])
            if st.get("type") == "model_output"
            for c in st.get("content", [])
            if c.get("type") == "text"
        )
        u = d.get("usage", {})
        return text, u.get("total_input_tokens"), u.get("total_output_tokens")


class OpenAICompatProvider:
    """OpenAI Chat Completions with a JSON schema; OpenRouter speaks the same API."""

    def __init__(self, slot: AISlot, key: str, base_url: str, name: str) -> None:
        self.slot, self._key, self.base_url, self.name = slot, key, base_url, name

    async def complete(
        self, system: str, user: str, schema: dict[str, Any], timeout_s: float, max_out: int
    ) -> tuple[str, int | None, int | None]:
        body = {
            "model": self.slot.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": max_out,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "verdict", "strict": True, "schema": schema},
            },
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as c:
                r = await c.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
        except httpx.TimeoutException as e:
            raise TimeoutError(f"{self.name} timeout") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name} connection error") from e
        _status(r)
        d = r.json()
        text = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        u = d.get("usage", {})
        return text, u.get("prompt_tokens"), u.get("completion_tokens")


class OllamaProvider:
    """Local Ollama. Unlikely to fit next to the engine in 1 GB of RAM (shown in the UI)."""

    name = "ollama"

    def __init__(self, slot: AISlot) -> None:
        self.slot = slot
        self.base = (slot.base_url or "http://127.0.0.1:11434").rstrip("/")

    async def complete(
        self, system: str, user: str, schema: dict[str, Any], timeout_s: float, max_out: int
    ) -> tuple[str, int | None, int | None]:
        body = {
            "model": self.slot.model,
            "stream": False,
            "format": schema,
            "options": {"num_predict": max_out},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as c:
                r = await c.post(f"{self.base}/api/chat", json=body)
        except httpx.TimeoutException as e:
            raise TimeoutError("ollama timeout") from e
        except httpx.HTTPError as e:
            raise ProviderError("ollama connection error") from e
        _status(r)
        d = r.json()
        return (
            d.get("message", {}).get("content", ""),
            d.get("prompt_eval_count"),
            d.get("eval_count"),
        )


def make_provider(slot: AISlot, secrets: Secrets) -> Provider | None:
    """None when the slot is off or its key isn't set: AI is fully optional."""

    def key(v: Any) -> str | None:
        return v.get_secret_value() if v is not None else None

    if slot.provider == "none" or slot.model.startswith("UNSET"):
        return None
    if slot.provider == "anthropic" and (k := key(secrets.anthropic_api_key)):
        return AnthropicProvider(slot, k)
    if slot.provider == "gemini" and (k := key(secrets.gemini_api_key)):
        return GeminiProvider(slot, k)
    if slot.provider == "openai" and (k := key(secrets.openai_api_key)):
        return OpenAICompatProvider(slot, k, "https://api.openai.com/v1", "openai")
    if slot.provider == "openrouter" and (k := key(secrets.openrouter_api_key)):
        return OpenAICompatProvider(slot, k, "https://openrouter.ai/api/v1", "openrouter")
    if slot.provider == "ollama":
        return OllamaProvider(slot)
    return None
