"""
Tests for the web search seam.

Server-tool errors arrive with HTTP 200 and a `content` that is a single
error OBJECT rather than a list. Indexing it as a list raises instead of
degrading, which is the one failure mode worth pinning here.
"""

from __future__ import annotations

import os

import httpx
import pytest
import respx

os.environ.setdefault("TYPESAFE_API_KEY", "test-key")

from search import (  # noqa: E402
    AnthropicSearch,
    default_search,
    unconfigured_search,
)

pytestmark = pytest.mark.asyncio

API = "https://api.anthropic.com/v1/messages"


def ok_body(n_results=2, searches=1):
    return {
        "content": [
            {"type": "text", "text": "I'll look that up."},
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": [
                    {
                        "type": "web_search_result",
                        "url": f"https://example.com/{i}",
                        "title": f"Result {i}",
                        "page_age": "September 18, 2026",
                        "encrypted_content": "Eq gf...",
                    }
                    for i in range(n_results)
                ],
            },
        ],
        "usage": {
            "input_tokens": 300,
            "output_tokens": 120,
            "server_tool_use": {"web_search_requests": searches},
        },
    }


@respx.mock
async def test_returns_the_shape_gatherers_expect():
    respx.post(API).mock(return_value=httpx.Response(200, json=ok_body(3)))
    s = AnthropicSearch(api_key="k")
    out = await s(" Georgia Arkansas injury report")
    assert len(out) == 3
    assert set(out[0]) == {"title", "url", "snippet"}
    assert out[0]["url"].startswith("https://")


@respx.mock
async def test_counts_billable_searches_not_calls():
    """
    Billing is per search, and one request can run several. Counting calls
    would under-report the spend that the kill switch is watching.
    """
    respx.post(API).mock(
        return_value=httpx.Response(200, json=ok_body(2, searches=3))
    )
    s = AnthropicSearch(api_key="k")
    await s("weather at Sanford Stadium")
    assert s.calls == 1
    assert s.searches == 3
    assert s.usd_spent == pytest.approx(0.03)


@respx.mock
async def test_server_tool_error_arrives_as_http_200():
    """
    A rate-limited search returns 200 with `content` as an error object,
    not a list. Treating it as a list raises TypeError mid-pipeline.
    """
    body = {
        "content": [{
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": {
                "type": "web_search_tool_result_error",
                "error_code": "too_many_requests",
            },
        }],
        "usage": {"server_tool_use": {"web_search_requests": 0}},
    }
    respx.post(API).mock(return_value=httpx.Response(200, json=body))
    s = AnthropicSearch(api_key="k")
    out = await s("anything")
    assert out == [], "an errored search must degrade, not raise"
    assert s.searches == 0, "an errored search is not billed"


@respx.mock
async def test_transport_failure_degrades_to_empty():
    respx.post(API).mock(return_value=httpx.Response(500, json={"e": "x"}))
    s = AnthropicSearch(api_key="k", backoff_base=0.0)
    assert await s("q") == []
    assert s.failures == 1


@respx.mock
async def test_respects_max_results():
    respx.post(API).mock(return_value=httpx.Response(200, json=ok_body(20)))
    s = AnthropicSearch(api_key="k", max_results=4)
    assert len(await s("q")) == 4


async def test_no_key_returns_nothing_rather_than_raising():
    s = AnthropicSearch(api_key="")
    assert s.configured is False
    assert await s("q") == []


async def test_unconfigured_search_is_explicit():
    assert await unconfigured_search("q") == []


async def test_default_search_is_the_noop_when_disabled():
    assert default_search(enabled=False) is unconfigured_search


async def test_default_search_falls_back_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert default_search(enabled=True) is unconfigured_search


@respx.mock
async def test_retries_a_rate_limit_then_succeeds():
    route = respx.post(API).mock(side_effect=[
        httpx.Response(429, json={"type": "error"}),
        httpx.Response(200, json=ok_body(1)),
    ])
    s = AnthropicSearch(api_key="k", max_retries=2, backoff_base=0.0)
    out = await s("q")
    assert len(out) == 1
    assert route.call_count == 2
    assert s.failures == 0


@respx.mock
async def test_gives_up_after_max_retries():
    route = respx.post(API).mock(
        return_value=httpx.Response(503, json={"type": "error"})
    )
    s = AnthropicSearch(api_key="k", max_retries=1, backoff_base=0.0)
    assert await s("q") == []
    assert route.call_count == 2
    assert s.failures == 1


@respx.mock
async def test_snippet_carries_the_cited_source_text():
    """
    Result blocks are encrypted; the only readable source text is
    `cited_text` on the answer's citations. It used to be discarded, so a
    gatherer was handed headlines and asked for facts.
    """
    body = ok_body(3)
    body["content"].append({
        "type": "text",
        "text": "The starter is out.",
        "citations": [{
            "type": "web_search_result_location",
            "url": "https://example.com/2",
            "title": "Result 2",
            "cited_text": "QB1 was ruled OUT on Saturday's final injury report.",
        }],
    })
    respx.post(API).mock(return_value=httpx.Response(200, json=body))
    out = await AnthropicSearch(api_key="k")("injury report")
    assert out[0]["url"] == "https://example.com/2", "quoted sources first"
    assert "ruled OUT" in out[0]["snippet"]
    assert out[1]["snippet"] == "September 18, 2026", "uncited keeps page_age"
