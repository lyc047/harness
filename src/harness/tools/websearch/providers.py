"""Pluggable web-search backends: free Bing/DDG scrapers + optional Tavily.

The free backends use only the stdlib (``urllib`` + ``html.parser``) and
degrade to empty result lists on any error — a search backend must never
crash the agent loop.
"""

from __future__ import annotations

import asyncio
import html.parser
import json
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from harness.observability.logging import get_logger

_UA = {"User-Agent": "Mozilla/5.0 (compatible; HarnessBot/1.0)"}

logger = get_logger("tools")


@dataclass
class WebSearchResult:
    title: str
    url: str
    snippet: str


class WebSearchProvider(Protocol):
    """A backend that turns a query into ranked web results."""

    async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]: ...


async def _http_get(url: str, *, headers: dict[str, str] | None = None) -> str:
    def _blocking() -> str:
        req = urllib.request.Request(url, headers=headers or _UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw: str = resp.read().decode("utf-8", errors="replace")
            return raw

    return await asyncio.to_thread(_blocking)


async def _http_post(url: str, body: str, *, headers: dict[str, str] | None = None) -> str:
    def _blocking() -> str:
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), headers=headers or {}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw: str = resp.read().decode("utf-8", errors="replace")
            return raw

    return await asyncio.to_thread(_blocking)


class _BingParser(html.parser.HTMLParser):
    """Best-effort extraction of cn.bing.com/search organic results.

    Each result is an ``<li class="b_algo">``: the title is an ``<a>`` inside
    ``<h2>``, the snippet is the first ``<p>`` inside ``.b_caption``. Markup
    drift degrades to fewer/empty results — parsing never raises.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[WebSearchResult] = []
        self._algo = 0
        self._cur: WebSearchResult | None = None
        self._title_done = False
        self._in_p = False
        self._purpose = "body"  # "title" | "snippet" | "body"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        cls = dict(attrs).get("class") or ""
        if tag == "li" and "b_algo" in cls.split():
            self._algo += 1
            self._cur = WebSearchResult(title="", url="", snippet="")
            self._title_done = False
            self._purpose = "body"
        if self._cur is None:
            return
        if tag == "h2":
            self._purpose = "title"
        elif tag == "a" and self._purpose == "title" and not self._cur.url:
            href = dict(attrs).get("href") or ""
            if href.startswith(("http://", "https://")):
                self._cur.url = href
        elif tag == "p" and self._title_done and not self._in_p:
            self._purpose = "snippet"
            self._in_p = True

    def handle_endtag(self, tag: str) -> None:
        if self._cur is None:
            return
        if tag == "h2":
            self._title_done = True
            self._purpose = "body"
        elif tag == "p" and self._in_p:
            self._in_p = False
            self._purpose = "body"
        elif tag == "li" and self._algo:
            self._algo -= 1
            if self._algo == 0 and self._cur:
                self.results.append(self._cur)
            self._cur = None

    def handle_data(self, data: str) -> None:
        if self._cur is None:
            return
        if self._purpose == "title":
            self._cur.title = (self._cur.title + " " + data.strip()).strip()
        elif self._purpose == "snippet":
            self._cur.snippet = (self._cur.snippet + " " + data.strip()).strip()


def _extract_bing_results(html_text: str) -> list[WebSearchResult]:
    parser = _BingParser()
    try:
        parser.feed(html_text)
    except Exception as exc:  # noqa: BLE001 — malformed HTML degrades to empty
        logger.warning("web_search Bing parse failed: %s", exc)
        return []
    return parser.results


class _DuckDuckGoParser(html.parser.HTMLParser):
    """Best-effort extraction of html.duckduckgo.com/html results.

    A result is ``<a class="result__a" href="URL">Title</a>`` followed by
    ``<a class="result__snippet" ...>Snippet</a>``. Parsing never raises.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[WebSearchResult] = []
        self._title: str | None = None
        self._url = ""
        self._in_title_a = False
        self._in_snippet_a = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        cls = dict(attrs).get("class") or ""
        if "result__a" in cls.split():
            self._in_title_a = True
            self._url = dict(attrs).get("href") or ""
            self._buf = []
        elif "result__snippet" in cls.split():
            self._in_snippet_a = True
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_title_a or self._in_snippet_a:
            self._buf.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag != "a":
            return
        if self._in_title_a:
            self._in_title_a = False
            self._title = " ".join(self._buf).strip()
        elif self._in_snippet_a:
            self._in_snippet_a = False
            if self._title and self._url:
                self.results.append(
                    WebSearchResult(
                        title=self._title,
                        url=self._url,
                        snippet=" ".join(self._buf).strip(),
                    )
                )
            self._title = None


def _extract_duckduckgo_results(html_text: str) -> list[WebSearchResult]:
    parser = _DuckDuckGoParser()
    try:
        parser.feed(html_text)
    except Exception as exc:  # noqa: BLE001 — malformed HTML degrades to empty
        logger.warning("web_search DuckDuckGo parse failed: %s", exc)
        return []
    return parser.results


class BingProvider:
    """Free default backend: scrape cn.bing.com/search (China-reachable)."""

    def __init__(
        self,
        *,
        fetch: Callable[[str], Awaitable[str]] | None = None,
        base_url: str = "https://cn.bing.com/search",
    ) -> None:
        self._fetch = fetch or _http_get
        self._base_url = base_url

    async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
        url = f"{self._base_url}?q={urllib.parse.quote(query)}&setlang=zh-hans"
        try:
            html_text = await self._fetch(url)
        except Exception as exc:  # noqa: BLE001 — network failure degrades to no results
            logger.warning("web_search Bing fetch failed for %s: %s", url, exc)
            return []
        return _extract_bing_results(html_text)[:max_results]


class DuckDuckGoProvider:
    """Alternative free backend: scrape html.duckduckgo.com/html."""

    def __init__(
        self,
        *,
        fetch: Callable[[str], Awaitable[str]] | None = None,
        base_url: str = "https://html.duckduckgo.com/html/",
    ) -> None:
        self._fetch = fetch or _http_get
        self._base_url = base_url

    async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
        url = f"{self._base_url}?q={urllib.parse.quote(query)}"
        try:
            html_text = await self._fetch(url)
        except Exception as exc:  # noqa: BLE001 — network failure degrades to no results
            logger.warning("web_search DuckDuckGo fetch failed for %s: %s", url, exc)
            return []
        return _extract_duckduckgo_results(html_text)[:max_results]


class TavilyProvider:
    """Keyed backend; only constructed when a TAVILY_API_KEY is configured."""

    def __init__(self, api_key: str, endpoint: str = "https://api.tavily.com/search") -> None:
        self._api_key = api_key
        self._endpoint = endpoint

    async def search(self, query: str, max_results: int = 5) -> list[WebSearchResult]:
        body = json.dumps({"query": query, "max_results": max_results})
        try:
            raw = await _http_post(
                self._endpoint,
                body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 — any failure degrades to no results
            logger.warning("web_search Tavily request failed for %s: %s", self._endpoint, exc)
            return []
        return [
            WebSearchResult(
                title=str(r.get("title", "")),
                url=str(r.get("url", "")),
                snippet=str(r.get("content", "")),
            )
            for r in data.get("results", [])
        ][:max_results]
