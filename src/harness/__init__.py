"""Harness — a Python production-grade AI agent framework.

Core abstractions: :class:`~harness.core.agent.Agent` (config),
:class:`~harness.core.runner.Runner` (executor) and pluggable
LLM providers / tools / sandboxes.
"""

from harness.config import Settings

__version__ = "0.1.0"
__all__ = ["Settings", "__version__"]
