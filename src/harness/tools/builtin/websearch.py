"""web_search builtin tool: pluggable backend, defaults to free cn.bing."""

from __future__ import annotations

from harness.config import Settings
from harness.tools.base import tool
from harness.tools.websearch import select_websearch_provider


@tool(
    name="web_search",
    description=(
        "Search the web for up-to-date external information and return ranked "
        "results (title, url, snippet)."
    ),
)
async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web for up-to-date external information.

    The backend is pluggable: TAVILY_API_KEY uses Tavily, otherwise the free
    Bing or DuckDuckGo scraper (HARNESS_WEB_SEARCH_BACKEND). A failure degrades
    to "No results found" so the agent can adapt.
    """
    provider = select_websearch_provider(Settings.load())
    try:
        results = await provider.search(query, max_results=max_results)
    except Exception:  # noqa: BLE001 — a search backend must never crash the run
        return "No results found (search backend unavailable)."
    if not results:
        return "No results found."
    return "\n".join(
        f"{i}. {r.title}\n   {r.url}\n   {r.snippet}"
        for i, r in enumerate(results[:max_results], start=1)
    )
