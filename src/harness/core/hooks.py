"""Lifecycle hooks for observability and CLI rendering.

Every hook is an optional async callable. Hooks are fire-and-forget from the
runner's perspective: an exception in a hook is logged, never propagates.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

AsyncHook = Callable[..., Awaitable[None]]


@dataclass
class Hooks:
    on_run_start: AsyncHook | None = None
    on_turn_start: AsyncHook | None = None
    on_text: AsyncHook | None = None
    on_reasoning: AsyncHook | None = None
    on_tool_call: AsyncHook | None = None
    on_tool_result: AsyncHook | None = None
    on_model_call: AsyncHook | None = None
    on_compacted: AsyncHook | None = None
    on_final: AsyncHook | None = None

    async def emit(self, hook: AsyncHook | None, *args: Any, **kwargs: Any) -> None:
        if hook is None:
            return
        try:
            if asyncio.iscoroutinefunction(hook):
                await hook(*args, **kwargs)
            else:
                hook(*args, **kwargs)
        except Exception:  # noqa: BLE001 — hooks must never break the loop
            logging.getLogger("harness.hooks").exception("hook raised")
