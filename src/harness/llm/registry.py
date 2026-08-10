"""Provider factory: resolve a configured provider name to an instance.

Extensibility point — register new providers here (e.g. an Anthropic-format
provider for DeepSeek's ``/anthropic`` endpoint or the real Anthropic API).
"""

from __future__ import annotations

from harness.config import Settings
from harness.llm.base import LLMProvider
from harness.llm.openai_compat import OpenAICompatProvider


def get_provider(settings: Settings) -> LLMProvider:
    """Build the LLM provider configured in ``settings``."""
    if settings.provider == "openai_compat":
        return OpenAICompatProvider(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout,
            max_tool_calls=settings.max_tool_calls_per_turn,
            retry_attempts=settings.retry_attempts,
            retry_base_delay=settings.retry_base_delay,
        )
    raise ValueError(f"unknown provider: {settings.provider!r}")
