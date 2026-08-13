"""Shared agent-stack composition.

Both the CLI REPL and the web server build their core runtime through
:func:`build_core_stack`, so the two surfaces stay behaviorally identical as
the harness evolves (new tools, new permission behavior, new executor layers
only have to be wired in one place).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from harness.agents.orchestrator import SubagentBudget
from harness.config import Settings
from harness.core.agent import Agent
from harness.core.hooks import Hooks
from harness.core.locking import FileLockExecutor
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
from harness.skills.registry import BUNDLED_SKILLS_DIR, SkillRegistry
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


def add_example_subagents(
    stack: CoreStack,
    *,
    subagent_model: str = "",
    on_event: Callable[[str, str, object], Awaitable[None]] | None = None,
    advanced: bool = False,
    subagent_fallback_model: str = "",
) -> None:
    """Register the built-in researcher/coder subagents as delegate tools.

    One implementation shared by the CLI (``--subagents``), the web runtime and
    the web REST read stack, so every surface exposes the same delegation tools.
    Lazy imports keep the ``agents`` package out of the hot composition path.
    ``subagent_model`` is the cheaper model subagents inherit when set
    (``HARNESS_SUBAGENT_MODEL``); empty means they use the parent's model.
    ``subagent_fallback_model`` is the escalation target (a stronger model)
    used when a subagent's first attempt errors
    (``HARNESS_SUBAGENT_FALLBACK_MODEL``); empty disables escalation.
    ``on_event`` (if given) is forwarded the events of each nested subagent run
    so a caller can render the subagent's turns/tools (web run view).
    ``advanced`` turns on nested delegation: each subagent gains delegate tools
    for the others (depth-2), runs its own turns concurrently, and shares the
    stack's per-run turn budget.
    Subagents talk to ``stack.subagent_provider`` when the settings carry a
    separate ``HARNESS_SUBAGENT_API_KEY`` — their own account — and otherwise
    share the parent's provider, differing only by model.
    """
    from harness.agents.examples import example_subagents
    from harness.agents.orchestrator import add_subagents, attach_delegation_protocol

    add_subagents(
        stack.agent,
        stack.runner,
        example_subagents(),
        default_model=subagent_model or None,
        on_event=on_event,
        concurrent=advanced,
        budget=stack.subagent_budget if advanced else None,
        advanced=advanced,
        subagent_provider=stack.subagent_provider,
        fallback_model=subagent_fallback_model,
    )
    # Tell the parent to write self-contained delegation briefs, so isolated
    # subagents (which can't see the conversation) get the context they need.
    attach_delegation_protocol(stack.agent, advanced=advanced)


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
    subagent_budget: SubagentBudget
    subagent_provider: LLMProvider | None = None  # own key/base_url for subagents


async def build_core_stack(
    settings: Settings,
    *,
    store: Store | None = None,
    provider: LLMProvider | None = None,
    subagent_provider: LLMProvider | None = None,
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
    ``subagent_provider`` is a test/benchmark seam that injects the subagent
    LLM account directly; when None the settings-derived one is built
    (``HARNESS_SUBAGENT_API_KEY``), and with no key either way it stays None.
    """
    if store is None:
        store = Store(settings)
        await store.initialize()

    provider = provider or get_provider(settings)
    # A separate subagent account (HARNESS_SUBAGENT_API_KEY): subagents run
    # against their own key/base_url/model instead of the parent's. Absent a
    # key they share the parent's provider and only differ by model.
    if subagent_provider is None and settings.subagent_api_key:
        subagent_provider = get_provider(
            settings.replace(
                api_key=settings.subagent_api_key,
                base_url=settings.subagent_base_url or settings.base_url,
                model=settings.subagent_model or settings.model,
            )
        )
    agent = default_agent(settings)

    # Self-evolving skills + user preferences: expose the tools and inject any
    # discovered skills into the agent's system prompt so they apply from turn 1.
    # Bundled skills ship in the package (fresh clones get them); the runtime
    # skills dir stays the writable override layer for user-authored skills.
    skill_registry = SkillRegistry(settings.skills_dir, bundled_dir=BUNDLED_SKILLS_DIR)
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
    base_executor = FileLockExecutor(base_executor)  # per-path mutual exclusion
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
        subagent_budget=SubagentBudget(settings.subagent_budget),
        subagent_provider=subagent_provider,
    )
