"""Minimal chat-with-tools client abstraction over OpenAI-compatible APIs (OpenRouter, DeepSeek).

The investigator depends only on ``ChatClient``; tests use scripted fakes. Provider selection:
``LLM_PROVIDER`` (openrouter | deepseek), else the first of OPENROUTER_API_KEY / DEEPSEEK_API_KEY
that is set. Models: OPENROUTER_MODEL / DEEPSEEK_MODEL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text exactly as the model produced it


@dataclass(frozen=True)
class AssistantTurn:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls
            ]
        return msg


class ChatClient(Protocol):
    model: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantTurn: ...


class OpenRouterChatClient:
    """Chat client for any OpenAI-compatible endpoint (name kept from the OpenRouter original)."""

    provider = "openrouter"

    def __init__(self, client: Any, model: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._client = client.with_options(timeout=timeout, max_retries=2)
        self.model = model

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantTurn:
        response = self._client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, tool_choice="auto", temperature=0,
        )
        msg = response.choices[0].message
        calls = [ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}") for tc in (msg.tool_calls or [])]
        return AssistantTurn(msg.content, calls)


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    base_url: str
    api_key: str = field(repr=False)
    model: str


# provider -> (base URL, key variable, model variable, default model). All are OpenAI-compatible.
PROVIDERS: dict[str, tuple[str, str, str, str]] = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
                   "anthropic/claude-5.5-sonnet"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "deepseek-flash"),
}


def resolve_llm_settings(env: Mapping[str, str]) -> LLMSettings | None:
    """Pick the provider from ``LLM_PROVIDER``; otherwise the first provider whose key is set.

    Returns None when no usable key is configured. Never falls back to a fake model.
    """
    wanted = (env.get("LLM_PROVIDER") or "").strip().lower()
    if wanted and wanted not in PROVIDERS:
        raise ValueError(f"LLM_PROVIDER must be one of {sorted(PROVIDERS)}, got {wanted!r}")
    for name in ([wanted] if wanted else list(PROVIDERS)):
        base_url, key_var, model_var, default_model = PROVIDERS[name]
        key = (env.get(key_var) or "").strip()
        if key:
            return LLMSettings(name, base_url, key, (env.get(model_var) or "").strip() or default_model)
    return None


def llm_settings() -> LLMSettings | None:
    from dotenv import load_dotenv

    load_dotenv()
    return resolve_llm_settings(os.environ)


def agent_configured() -> bool:
    return llm_settings() is not None


def chat_client_from_env() -> ChatClient | None:
    """An OpenAI-compatible chat client for the configured provider, or None (no fake fallback)."""
    settings = llm_settings()
    if settings is None:
        return None
    from openai import OpenAI

    client = OpenRouterChatClient(OpenAI(base_url=settings.base_url, api_key=settings.api_key), settings.model)
    client.provider = settings.provider
    return client
