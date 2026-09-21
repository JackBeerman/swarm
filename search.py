"""
search.py -- web search behind the SearchFn seam.

`swarm.SearchFn` is deliberately injectable; this module supplies
implementations rather than hard-wiring one into the pipeline.

Anthropic's server-side web_search runs on Anthropic's infrastructure, so
there is no second provider, no second key, and no HTML extraction step.
It is the most expensive per query of the options measured -- $10 per
1,000 searches against Tavily's $8, Brave's $5 and Serper's $1 -- and the
cheapest in integration, since Tier 2/3 already authenticate to Anthropic.
Token costs for the retrieved content are additional and are usually the
larger number.

`web_search_20250305` (basic) is used rather than the dynamic-filtering
variants: those require Claude 4.6 or later, and Tier 2 runs Haiku 4.5.
Pointing Tier 2 at a 4.6+ model would make `web_search_20260209` worth
switching to, since it filters results before they reach the context
window.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("search")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

#: Haiku 4.5 does not support programmatic tool calling, so the basic tool
#: is the correct one here. See the module docstring.
WEB_SEARCH_TOOL = "web_search_20250305"

SEARCH_MODEL = os.getenv("SEARCH_MODEL", "claude-haiku-4-5")

#: $10 per 1,000 searches, billed per search regardless of result count.
#: Errored searches are not billed. Token costs are separate.
USD_PER_SEARCH = float(os.getenv("ANTHROPIC_USD_PER_SEARCH", "0.010"))


class AnthropicSearch:
    """
    A SearchFn backed by Anthropic's server-side web_search tool.

    Returns [{"title", "url", "snippet"}, ...] -- the shape the gatherers
    already expect, so nothing downstream changes.

    Counts searches. The count is the billable unit, not the call: one
    request can run several searches, and `max_uses` is the only hard cap.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = SEARCH_MODEL,
        max_uses: int = 3,
        max_results: int = 6,
        timeout: float = 45.0,
        max_retries: int = 2,
        backoff_base: float = 0.75,
    ) -> None:
        # `is not None`, not `or`: an explicitly empty key means "no key",
        # while omitting it means "read the environment". With `or` the
        # two are indistinguishable, and a caller disabling search would
        # silently get the ambient key instead.
        self._key = (api_key if api_key is not None
                     else os.environ.get("ANTHROPIC_API_KEY", ""))
        self._model = model
        self._max_uses = max_uses
        self._max_results = max_results
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self.searches = 0
        self.calls = 0
        self.failures = 0

    @property
    def configured(self) -> bool:
        return bool(self._key)

    @property
    def usd_spent(self) -> float:
        """Search charges only. Token costs are billed through Tier 2."""
        return self.searches * USD_PER_SEARCH

    async def __call__(self, query: str) -> list[dict[str, str]]:
        if not self._key:
            log.warning("ANTHROPIC_API_KEY not set; search returning nothing")
            return []

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 2048,
            "messages": [{
                "role": "user",
                "content": (
                    f"Search the web for: {query}\n\n"
                    "Report only what the sources say. Do not speculate "
                    "and do not draw conclusions beyond them."
                ),
            }],
            "tools": [{
                "type": WEB_SEARCH_TOOL,
                "name": "web_search",
                "max_uses": self._max_uses,
            }],
        }

        self.calls += 1
        body = await self._post(payload, query)
        if body is None:
            return []

        # Billed per search, reported by the server rather than counted
        # from the blocks -- a search returning no results still costs.
        usage = body.get("usage", {}) or {}
        self.searches += int(
            (usage.get("server_tool_use") or {}).get("web_search_requests", 0)
        )
        return self._extract(body)

    async def _post(
        self, payload: dict[str, Any], query: str
    ) -> dict[str, Any] | None:
        """
        POST with backoff on 429 and 5xx, mirroring JevTriage._post.

        Raw httpx rather than the `anthropic` SDK: the SDK would bring
        retries for free, but this is one endpoint and the repo already
        hand-rolls its other HTTP client. If a second Anthropic call
        appears, take the dependency instead of copying this again.
        """
        delay = self._backoff_base
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        ANTHROPIC_API_URL,
                        json=payload,
                        headers={
                            "x-api-key": self._key,
                            "anthropic-version": ANTHROPIC_VERSION,
                            "content-type": "application/json",
                        },
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt == self._max_retries:
                        resp.raise_for_status()
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                if attempt == self._max_retries:
                    self.failures += 1
                    log.warning("web search failed for %r: %s",
                                query[:60], exc)
                    return None
                await asyncio.sleep(delay)
                delay *= 2
        return None

    def _extract(self, body: dict[str, Any]) -> list[dict[str, str]]:
        """
        Pull result blocks out of the response.

        Server-tool errors arrive with HTTP 200: `content` is a single
        error object rather than a list. Branch on that before indexing,
        or a rate-limited search raises a TypeError instead of degrading.
        """
        # The result blocks carry encrypted page content. The only readable
        # source text in the response is `cited_text` on the answer's
        # citations -- verbatim quotes, already paid for. Without it a
        # gatherer is handed headlines and asked for facts.
        quotes: dict[str, list[str]] = {}
        for block in body.get("content", []) or []:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            for c in block.get("citations") or []:
                if not isinstance(c, dict):
                    continue
                url, text = str(c.get("url") or ""), str(c.get("cited_text") or "")
                if url and text and text not in quotes.setdefault(url, []):
                    quotes[url].append(text)

        out: list[dict[str, str]] = []
        uncited: list[dict[str, str]] = []
        for block in body.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "web_search_tool_result":
                continue
            content = block.get("content")
            if isinstance(content, dict):
                log.warning("web search error: %s",
                            content.get("error_code", "unknown"))
                continue
            for r in content or []:
                if not isinstance(r, dict) or r.get("type") != "web_search_result":
                    continue
                url = str(r.get("url") or "")
                age = str(r.get("page_age") or "")
                cited = " ... ".join(quotes.get(url, []))[:700]
                item = {
                    "title": str(r.get("title") or "")[:200],
                    "url": url,
                    "snippet": f"[{age}] {cited}" if cited and age else cited or age,
                }
                (out if cited else uncited).append(item)
        # Quoted sources first: max_results truncates, and a result with
        # no readable text is worth less than one with a verbatim quote.
        return (out + uncited)[: self._max_results]

    def report(self) -> str:
        return (f"search: {self.calls} calls, {self.searches} searches, "
                f"{self.failures} failed, ${self.usd_spent:.4f} in search fees")


async def unconfigured_search(query: str) -> list[dict[str, str]]:
    """Explicit no-op, for shadow runs that must not spend anything."""
    log.debug("search disabled; returning nothing for %r", query[:60])
    return []


def default_search(enabled: bool = True) -> Any:
    """
    Pick an implementation. Returns the no-op when disabled or unkeyed, so
    a missing key degrades to empty facts rather than an exception --
    which is what the gatherers already handle.
    """
    if not enabled:
        return unconfigured_search
    impl = AnthropicSearch()
    if not impl.configured:
        log.warning("ANTHROPIC_API_KEY not set; gatherers will get no facts")
        return unconfigured_search
    return impl
