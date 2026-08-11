"""Shared agent-stack composition.

Both the CLI REPL and the web server build their core runtime through
:func:`build_core_stack`, so the two surfaces stay behaviorally identical as
the harness evolves (new tools, new permission behavior, new executor layers
only have to be wired in one place).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harness.config import Settings
from harness.core.agent import Agent
from harness.core.hooks import Hooks
from harness.core.runner import PauseCheck, Runner, ToolExecutor, default_executor
from harness.core.snapshot import SnapshotExecutor
from harness.llm.base import LLMProvider
from harness.llm.registry import get_provider
from harness.memory.preferences import make_remember_preference_tool
from harness.memory.store import Store
from harness.observability.logging import get_logger
from harness.planning.planner import Planner
from harness.safety.approver import ApprovalExecutor, ApprovalPrompt
from harness.safety.permissions import Permissions
from harness.sandbox import SandboxedExecutor, SandboxProvider, build_sandbox
from harness.skills.loader import make_create_skill_tool
from harness.skills.registry import SkillRegistry
from harness.tools.builtin import builtin_registry

logger = get_logger("compose")

DEFAULT_INSTRUCTIONS = """\
You are a capable AI assistant running inside the 'harness' agent framework.
You have access to tools for reading, writing, searching and running shell
commands. Use them when they help answer the user's question.
Be concise, accurate, and prefer existing files over re-creating them.
"""


def default_agent(settings: Settings) -> Agent:
    """The baseline agent: built-in tools + the configured model."""
    return Agent(
        name="assistant",
        instructions=DEFAULT_INSTRUCTIONS,
        tools=builtin_registry(),
        model=settings.model,
        max_turns=settings.max_turns,
    )


def add_example_subagents(stack: CoreStack) -> None:
    """Register the built-in researcher/coder subagents as delegate tools.

    One implementation shared by the CLI (``--subagents``), the web runtime and
    the web REST read stack, so every surface exposes the same delegation tools.
    Lazy imports keep the ``agents`` package out of the hot composition path.
    """
    from harness.agents.examples import example_subagents
    from harness.agents.orchestrator import add_subagents

    add_subagents(stack.agent, stack.runner, example_subagents())


def load_permissions(settings: Settings) -> Permissions:
    """Load the TOML policy file, falling back to safe defaults."""
    path = Path(settings.permissions_file)
    if path.exists():
        try:
            return Permissions.from_config(path)
        except Exception as exc:  # noqa: BLE001 — a broken policy must not crash
            logger.warning("failed to load %s (%s); using defaults", path, exc)
    return Permissions.default_harness()


@dataclass
class CoreStack:
    """The fully-wired agent stack shared by CLI and web surfaces."""

    store: Store
    provider: LLMProvider
    agent: Agent
    skill_registry: SkillRegistry
    permissions: Permissions
    sandbox: SandboxProvider
    sandboxed: SandboxedExecutor
    approval: ApprovalExecutor
    runner: Runner
    planner: Planner


async def build_core_stack(
    settings: Settings,
    *,
    store: Store | None = None,
    provider: LLMProvider | None = None,
    tool_executor: ToolExecutor | None = None,
    prompt: ApprovalPrompt | None = None,
    on_pause: Callable[[], None] | None = None,
    pause_check: PauseCheck | None = None,
    hooks: Hooks | None = None,
) -> CoreStack:
    """Compose the full agent stack in the CLI's canonical order.

    ``store`` defaults to a fresh initialized :class:`Store` (the CLI case);
    the web server passes its single shared store. ``provider`` and
    ``tool_executor`` are test seams that replace the configured provider and
    the sandbox-wrapped default executor. ``prompt`` / ``on_pause`` /
    ``pause_check`` are per-surface: the CLI wires an interactive console
    prompt, the web wires a :class:`~harness.web.runtime.WebApprover`.
    """
    if store is None:
        store = Store(settings)
        await store.initialize()

    provider = provider or get_provider(settings)
    agent = default_agent(settings)

    # Self-evolving skills + user preferences: expose the tools and inject any
    # discovered skills into the agent's system prompt so they apply from turn 1.
    skill_registry = SkillRegistry(settings.skills_dir)
    skill_registry.discover()
    agent.tools.register(make_create_skill_tool(skill_registry))
    agent.tools.register(make_remember_preference_tool(store.preferences))
    agent.instructions = skill_registry.inject(agent.instructions)

    # Sandbox: bash runs through the configured provider (local dev default,
    # remote SSH for isolation). Approval wraps it so humans see commands first.
    sandbox = build_sandbox(settings)
    # Pre-write snapshots of every write_file target (rollback support). Sits
    # inside the sandbox (bash never reaches it) but outside default_executor.
    base_executor = tool_executor or default_executor
    base_executor = SnapshotExecutor(base_executor, store.sessions)
    sandboxed = SandboxedExecutor(base_executor, sandbox)

    # Human-in-the-loop: ASK-decided tools consult the injected prompt; "p"
    # pauses after the turn via ``on_pause`` (the caller owns the pause flag).
    permissions = load_permissions(settings)
    approval = ApprovalExecutor(
        sandboxed,
        permissions,
        prompt=prompt,
        on_pause=on_pause,
    )

    runner = Runner(
        provider,
        session_store=store.sessions,
        tool_executor=approval,
        pause_check=pause_check,
        hooks=hooks,
    )
    planner = Planner(provider, settings.model)

    return CoreStack(
        store=store,
        provider=provider,
        agent=agent,
        skill_registry=skill_registry,
        permissions=permissions,
        sandbox=sandbox,
        sandboxed=sandboxed,
        approval=approval,
        runner=runner,
        planner=planner,
    )
