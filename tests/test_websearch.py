# ruff: noqa: E501  # _DDG_FIXTURE raw-string line; an inline noqa there would be string content

"""Pluggable web_search backend: provider selection, Bing/DDG parsing, tool shape."""

from __future__ import annotations

from harness.config import Settings
from harness.tools.base import ToolResult
from harness.tools.builtin import builtin_registry
from harness.tools.websearch import (
    BingProvider,
    DuckDuckGoProvider,
    TavilyProvider,
    WebSearchResult,
    select_websearch_provider,
)

_BING_FIXTURE = """\
<ol id="b_results">
  <li class="b_algo">
    <h2><a href="https://www.python.org/">Welcome to Python.org</a></h2>
    <div class="b_caption"><p>The official home of the Python Programming Language.</p></div>
  </li>
  <li class="b_algo">
    <h2><a href="https://docs.python.org/3/">Python 3 documentation</a></h2>
    <div class="b_caption"><p>Python 3.11 documentation and reference.</p></div>
  </li>
</ol>
"""

_DDG_FIXTURE = """\
<div class="result">
  <h2 class="result__title"><a class="result__a" href="https://duckduckgo.com">DuckDuckGo</a></h2>
  <a class="result__snippet" href="https://duckduckgo.com">The search engine that doesn't track you.</a>
</div>
"""


class _FakeFetch:
    """Injects canned HTML so provider tests never touch the network."""

    def __init__(self, html: str) -> None:
        self._html = html

    async def fetch(self, url: str) -> str:
        return self._html


# ---- provider selection ---- #


def test_select_provider_default_bing() -> None:
    s = Settings.from_env({})
    assert isinstance(select_websearch_provider(s), BingProvider)


def test_select_provider_backend_switch() -> None:
    s = Settings.from_env({"HARNESS_WEB_SEARCH_BACKEND": "duckduckgo"})
    assert isinstance(select_websearch_provider(s), DuckDuckGoProvider)


def test_select_provider_tavily_when_key_set() -> None:
    s = Settings.from_env({"TAVILY_API_KEY": "tk", "HARNESS_WEB_SEARCH_BACKEND": "bing"})
    assert isinstance(select_websearch_provider(s), TavilyProvider)


def test_settings_web_search_fields() -> None:
    assert Settings.from_env({}).web_search_backend == "bing"
    assert Settings.from_env({}).tavily_api_key == ""
    s = Settings.from_env({"HARNESS_WEB_SEARCH_BACKEND": "duckduckgo", "TAVILY_API_KEY": "k"})
    assert s.web_search_backend == "duckduckgo"
    assert s.tavily_api_key == "k"


# ---- parsing (canned fixtures) ---- #


async def test_bing_provider_parses_canned_results() -> None:
    provider = BingProvider(fetch=_FakeFetch(_BING_FIXTURE).fetch)
    results = await provider.search("python")
    assert len(results) == 2
    assert results[0].title == "Welcome to Python.org"
    assert results[0].url == "https://www.python.org/"
    assert "programming language" in results[0].snippet.lower()
    assert results[1].url == "https://docs.python.org/3/"


async def test_duckduckgo_provider_parses_canned_results() -> None:
    provider = DuckDuckGoProvider(fetch=_FakeFetch(_DDG_FIXTURE).fetch)
    results = await provider.search("privacy")
    assert len(results) == 1
    assert results[0].title == "DuckDuckGo"
    assert results[0].url == "https://duckduckgo.com"
    assert "doesn't track you" in results[0].snippet


async def test_provider_degrades_to_empty_on_transport_error() -> None:
    class _Boom:
        async def fetch(self, url: str) -> str:
            raise OSError("network down")

    provider = BingProvider(fetch=_Boom().fetch)
    assert await provider.search("x") == []


# ---- tool shape ---- #


async def test_web_search_tool_returns_ok(monkeypatch) -> None:
    import harness.tools.builtin.websearch as ws_mod

    class _Stub:
        async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
            return [WebSearchResult("Python", "https://python.org/", "the language")]

    monkeypatch.setattr(ws_mod, "select_websearch_provider", lambda settings: _Stub())
    tool = builtin_registry().require("web_search")
    res = await tool.invoke(query="python", max_results=5)
    assert isinstance(res, ToolResult)
    assert not res.is_error
    assert "Python" in res.content
    assert "https://python.org/" in res.content


async def test_web_search_tool_no_results(monkeypatch) -> None:
    import harness.tools.builtin.websearch as ws_mod

    class _Empty:
        async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
            return []

    monkeypatch.setattr(ws_mod, "select_websearch_provider", lambda settings: _Empty())
    res = await builtin_registry().require("web_search").invoke(query="no such thing")
    assert "No results" in res.content
