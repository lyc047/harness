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


def test_subagent_model_env():
    s = Settings.from_env({"HARNESS_SUBAGENT_MODEL": "deepseek-v3"})
    assert s.subagent_model == "deepseek-v3"
    # unset => subagents inherit the parent model
    assert Settings.from_env({}).subagent_model == ""


def test_subagent_api_key_env():
    s = Settings.from_env(
        {
            "HARNESS_SUBAGENT_API_KEY": "sk-sub",
            "HARNESS_SUBAGENT_BASE_URL": "https://gateway.example.com/v1",
        }
    )
    assert s.subagent_api_key == "sk-sub"
    assert s.subagent_base_url == "https://gateway.example.com/v1"
    # unset => subagents inherit the parent's key / base_url
    d = Settings.from_env({})
    assert d.subagent_api_key == ""
    assert d.subagent_base_url == ""


def test_replace_returns_new_instance():
    s = Settings.from_env({})
    s2 = s.replace(model="other-model")
    assert s2.model == "other-model"
    assert s.model == "deepseek-v4-flash"
    assert s2 is not s


def test_subagent_advanced_and_budget_env():
    s = Settings.from_env(
        {"HARNESS_SUBAGENT_ADVANCED": "1", "HARNESS_SUBAGENT_BUDGET": "12"}
    )
    assert s.subagent_advanced is True
    assert s.subagent_budget == 12

    d = Settings.from_env({})
    assert d.subagent_advanced is False
    assert d.subagent_budget == 40  # safe default


def test_subagent_budget_bad_value_falls_back():
    s = Settings.from_env({"HARNESS_SUBAGENT_BUDGET": "abc"})
    assert s.subagent_budget == 40


def test_subagent_fallback_model_env():
    s = Settings.from_env({"HARNESS_SUBAGENT_FALLBACK_MODEL": "deepseek-v4-pro"})
    assert s.subagent_fallback_model == "deepseek-v4-pro"
    # unset => escalation off
    assert Settings.from_env({}).subagent_fallback_model == ""
