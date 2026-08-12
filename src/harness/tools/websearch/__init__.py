"""Web search provider seam: select a backend from settings."""

from __future__ import annotations

from harness.config import Settings
from harness.tools.websearch.providers import (
    BingProvider,
    DuckDuckGoProvider,
    TavilyProvider,
    WebSearchProvider,
    WebSearchResult,
)

__all__ = [
    "WebSearchProvider",
    "WebSearchResult",
    "BingProvider",
    "DuckDuckGoProvider",
    "TavilyProvider",
    "select_websearch_provider",
]


def select_websearch_provider(settings: Settings) -> WebSearchProvider:
    """Pick the backend: Tavily when a key is configured, else the free scraper.

    ``HARNESS_WEB_SEARCH_BACKEND`` chooses between the two free scrapers
    (``bing`` default, ``duckduckgo`` alternative).
    """
    if settings.tavily_api_key:
        return TavilyProvider(settings.tavily_api_key)
    if settings.web_search_backend == "duckduckgo":
        return DuckDuckGoProvider()
    return BingProvider()
