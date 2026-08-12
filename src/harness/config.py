"""Configuration for the harness agent framework.

Loads settings from environment variables / `.env` file via pydantic-settings
style defaults. Kept dependency-light: plain dataclass + python-dotenv.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_DEFAULT_ENV_PATH = Path(".env")


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the harness.

    All fields have safe defaults so the CLI can boot without a `.env`;
    talking to a real LLM requires ``DEEPSEEK_API_KEY``.
    """

    # LLM provider
    provider: str = "openai_compat"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-v4-flash"

    # Loop behaviour
    max_turns: int = 30
    max_tool_calls_per_turn: int = 128
    request_timeout: float = 60.0
    retry_attempts: int = 3
    retry_base_delay: float = 1.0

    # Storage
    db_path: str = "harness.db"

    # Sandbox: local | ssh | http  (ssh/http implemented in P7)
    sandbox_mode: str = "local"
    sandbox_host: str = ""
    sandbox_port: int = 22
    sandbox_user: str = ""
    sandbox_key_path: str = ""
    sandbox_workdir: str = "~/harness-workspace"

    # Skills / memory directories (resolved against cwd)
    skills_dir: str = "skills"
    memory_dir: str = "memory"

    # Permission policy file (TOML); absent => safe defaults
    permissions_file: str = "permissions.toml"

    # Multi-agent orchestration: register researcher/coder delegate tools
    subagents: bool = False
    subagent_model: str = ""  # cheaper model for subagents; empty => inherit parent
    subagent_advanced: bool = False  # advanced orchestration (nesting + concurrency)
    subagent_budget: int = 40  # per-run subagent turn budget (advanced-mode guardrail)

    # Logging / tracing
    log_level: str = "INFO"
    log_file: str = "harness.log"
    trace_file: str = "harness.trace.jsonl"

    # ---- helpers ----
    _env: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    def replace(self, **overrides: object) -> Settings:
        """Return a new Settings with the given fields overridden."""
        return Settings(**{**self.__dict__, **overrides})

    @classmethod
    def load(cls, env_path: str | Path | None = _DEFAULT_ENV_PATH) -> Settings:
        """Build settings from an env file (optional) + process environment."""
        env = {}
        if env_path is not None and Path(env_path).exists():
            load_dotenv(env_path, override=False)
        # Snapshot relevant keys for explicit forwards in from_env.
        prefixes = ("DEEPSEEK_", "HARNESS_", "SANDBOX_")
        env = {k: v for k, v in os.environ.items() if k.startswith(prefixes)}
        return cls.from_env(env)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """Build settings from a dict of env vars (testable, no process coupling)."""
        env = env or {}

        def get(*names: str, default: str = "") -> str:
            for n in names:
                if n in env and env[n] != "":
                    return env[n]
            return default

        def get_bool(name: str, default: bool) -> bool:
            raw = env.get(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        def get_int(name: str, default: int) -> int:
            raw = env.get(name)
            try:
                return int(raw) if raw not in (None, "") else default
            except ValueError:
                return default

        return cls(
            provider=get("HARNESS_PROVIDER", default="openai_compat"),
            base_url=get("DEEPSEEK_BASE_URL", "HARNESS_BASE_URL", default="https://api.deepseek.com"),
            api_key=get("DEEPSEEK_API_KEY", "HARNESS_API_KEY"),
            model=get("DEEPSEEK_MODEL", "HARNESS_MODEL", default="deepseek-v4-flash"),
            max_turns=get_int("HARNESS_MAX_TURNS", 30),
            max_tool_calls_per_turn=get_int("HARNESS_MAX_TOOL_CALLS", 128),
            request_timeout=get_int("HARNESS_REQUEST_TIMEOUT", 60),
            retry_attempts=get_int("HARNESS_RETRY_ATTEMPTS", 3),
            retry_base_delay=get_int("HARNESS_RETRY_BASE_DELAY", 1),
            db_path=get("HARNESS_DB_PATH", default="harness.db"),
            sandbox_mode=get("HARNESS_SANDBOX_MODE", default="local"),
            sandbox_host=get("SANDBOX_HOST"),
            sandbox_port=get_int("SANDBOX_PORT", 22),
            sandbox_user=get("SANDBOX_USER"),
            sandbox_key_path=get("SANDBOX_KEY_PATH"),
            sandbox_workdir=get("SANDBOX_WORKDIR", default="~/harness-workspace"),
            skills_dir=get("HARNESS_SKILLS_DIR", default="skills"),
            memory_dir=get("HARNESS_MEMORY_DIR", default="memory"),
            permissions_file=get("HARNESS_PERMISSIONS_FILE", default="permissions.toml"),
            subagents=get_bool("HARNESS_SUBAGENTS", False),
            subagent_model=get("HARNESS_SUBAGENT_MODEL"),
            subagent_advanced=get_bool("HARNESS_SUBAGENT_ADVANCED", False),
            subagent_budget=get_int("HARNESS_SUBAGENT_BUDGET", 40),
            log_level=get("HARNESS_LOG_LEVEL", default="INFO"),
            log_file=get("HARNESS_LOG_FILE", default="harness.log"),
            trace_file=get("HARNESS_TRACE_FILE", default="harness.trace.jsonl"),
        )
