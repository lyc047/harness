"""Tests for configuration loading."""

from harness.config import Settings


def test_defaults():
    s = Settings.from_env({})
    assert s.provider == "openai_compat"
    assert s.base_url == "https://api.deepseek.com"
    assert s.model == "deepseek-v4-flash"
    assert s.max_turns == 30
    assert s.sandbox_mode == "local"


def test_env_override():
    s = Settings.from_env(
        {
            "DEEPSEEK_API_KEY": "sk-test",
            "DEEPSEEK_MODEL": "deepseek-v4-pro",
            "HARNESS_MAX_TURNS": "5",
        }
    )
    assert s.api_key == "sk-test"
    assert s.model == "deepseek-v4-pro"
    assert s.max_turns == 5


def test_alias_vars():
    s = Settings.from_env({"HARNESS_BASE_URL": "http://localhost:8000", "HARNESS_API_KEY": "k"})
    assert s.base_url == "http://localhost:8000"
    assert s.api_key == "k"


def test_replace_returns_new_instance():
    s = Settings.from_env({})
    s2 = s.replace(model="other-model")
    assert s2.model == "other-model"
    assert s.model == "deepseek-v4-flash"
    assert s2 is not s
