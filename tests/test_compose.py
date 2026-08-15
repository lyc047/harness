"""build_core_stack provider seams: provider + subagent_provider injection."""

import pytest

from harness.config import Settings
from harness.core.compose import build_core_stack


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def complete(self, messages, *, tools=None, model=None):  # pragma: no cover
        raise NotImplementedError

    def stream(self, messages, *, tools=None, model=None):  # pragma: no cover
        raise NotImplementedError


def _settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "HARNESS_DB_PATH": str(tmp_path / "harness.db"),
            "HARNESS_SKILLS_DIR": str(tmp_path / "skills"),
            "HARNESS_PERMISSIONS_FILE": str(tmp_path / "nonexistent.toml"),
        }
    )


@pytest.mark.asyncio
async def test_injected_subagent_provider_wins(tmp_path):
    main = _FakeProvider("main")
    sub = _FakeProvider("sub")
    stack = await build_core_stack(_settings(tmp_path), provider=main, subagent_provider=sub)
    assert stack.provider is main
    assert stack.subagent_provider is sub


@pytest.mark.asyncio
async def test_subagent_provider_none_without_key(tmp_path):
    stack = await build_core_stack(_settings(tmp_path), provider=_FakeProvider("main"))
    assert stack.subagent_provider is None


@pytest.mark.asyncio
async def test_subagent_provider_built_from_env_key(tmp_path):
    settings = Settings.from_env(
        {
            "HARNESS_DB_PATH": str(tmp_path / "harness.db"),
            "HARNESS_SKILLS_DIR": str(tmp_path / "skills"),
            "HARNESS_PERMISSIONS_FILE": str(tmp_path / "nonexistent.toml"),
            "HARNESS_SUBAGENT_API_KEY": "sk-sub",
            "HARNESS_SUBAGENT_MODEL": "deepseek-v4-flash",
        }
    )
    stack = await build_core_stack(settings, provider=_FakeProvider("main"))
    assert stack.subagent_provider is not None
    assert stack.subagent_provider._api_key == "sk-sub"
    assert stack.subagent_provider.model == "deepseek-v4-flash"


def _ctx_settings(tmp_path) -> Settings:
    return Settings.from_env(
        {
            "HARNESS_DB_PATH": str(tmp_path / "harness.db"),
            "HARNESS_SKILLS_DIR": str(tmp_path / "skills"),
            "HARNESS_PERMISSIONS_FILE": str(tmp_path / "nonexistent.toml"),
            "HARNESS_CONTEXT_ENABLED": "true",
            "HARNESS_CONTEXT_DIR": str(tmp_path / "ctx"),
        }
    )


@pytest.mark.asyncio
async def test_build_core_stack_wires_context(tmp_path):
    stack = await build_core_stack(_ctx_settings(tmp_path), provider=_FakeProvider("main"))
    assert stack.context_store is not None
    assert "compact_conversation" in stack.agent.tools.names()


@pytest.mark.asyncio
async def test_build_core_stack_disabled_context(tmp_path):
    settings = _ctx_settings(tmp_path).replace(context_enabled=False)
    stack = await build_core_stack(settings, provider=_FakeProvider("main"))
    assert stack.context_store is None
    assert "compact_conversation" not in stack.agent.tools.names()
