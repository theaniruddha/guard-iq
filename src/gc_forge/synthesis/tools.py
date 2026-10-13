from __future__ import annotations

"""
Lean pluggable search tools.

Current default: DuckDuckGo (DDG) — no keys needed.

To add other providers (Tavily, SerpAPI, Firecrawl, etc.):
  1. Implement search_xxx and fetch_xxx below (or in a new module).
  2. Change the active implementation at bottom (or add a selector).
  3. The agent code and public search()/fetch() stay unchanged.

Keep it simple: plain functions, no complex registry.
"""

import httpx

try:  # the package was renamed; prefer the maintained one
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    from duckduckgo_search import DDGS


# --- Concrete implementations (easy to read) ---

def _ddg_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    with DDGS() as ddgs:
        for item in ddgs.text(query, max_results=max_results):
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("href", item.get("link", "")),
                    "snippet": item.get("body", item.get("snippet", "")),
                }
            )
    return results


def _ddg_fetch(url: str, max_chars: int = 8000) -> str:
    try:
        response = httpx.get(
            url,
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": "guard-iq-synthesis/0.1"},
        )
        response.raise_for_status()
        text = response.text
        if len(text) > max_chars:
            return text[:max_chars] + "\n...[truncated]"
        return text
    except httpx.HTTPError as exc:
        return f"Failed to fetch {url}: {exc}"


# --- Placeholder connectors (implement when you have keys) ---

def _tavily_search(query: str, max_results: int = 5) -> list[dict[str, str]]:  # pragma: no cover
    # TODO: wire Tavily client here. For now return empty so agent can still run with DDG.
    return []


def _tavily_fetch(url: str, max_chars: int = 8000) -> str:  # pragma: no cover
    return _ddg_fetch(url, max_chars)  # fallback to http for simplicity


def _serp_search(query: str, max_results: int = 5) -> list[dict[str, str]]:  # pragma: no cover
    return []


def _firecrawl_search(query: str, max_results: int = 5) -> list[dict[str, str]]:  # pragma: no cover
    return []


# --- Active implementation (lean: just assign here) ---

search = _ddg_search
fetch = _ddg_fetch

# Example swap (uncomment + implement the placeholder):
# search = _tavily_search
# fetch = _tavily_fetch