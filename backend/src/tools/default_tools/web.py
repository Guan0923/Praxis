"""Definitions for public web tools."""

from __future__ import annotations

from ..base import Tool
from ..web import DdgrWebSearch, SafeWebFetcher
from .schema import object_schema


def web_tools(search: DdgrWebSearch, fetcher: SafeWebFetcher) -> tuple[Tool, ...]:
    return (
        Tool(
            "web_search",
            "Searches the public web using DuckDuckGo HTML with Lite fallback; returns titles, URLs, and snippets.",
            search.search,
            object_schema(
                {
                    "query": {
                        "type": "string",
                        "description": "The web search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                        "description": ("The maximum number of search results to return, from 1 to 10. Defaults to 5."),
                    },
                },
                ["query"],
            ),
            requires_confirmation=True,
            retryable=True,
        ),
        Tool(
            "web_fetch",
            (
                "Fetches public HTTP/HTTPS HTML, text, JSON, Markdown, CSV, XML, or ZIP. "
                "ZIP returns a file listing and text previews without extracting files to disk. "
                "Limits: 200 ZIP entries, 8 MB expanded ZIP. Binary files are listed only."
            ),
            fetcher.fetch,
            object_schema(
                {
                    "url": {
                        "type": "string",
                        "description": "The HTTP or HTTPS URL to fetch.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100_000,
                        "default": 50_000,
                        "description": (
                            "The maximum number of content characters to return, from 1 to 100000. Defaults to 50000."
                        ),
                    },
                },
                ["url"],
            ),
            requires_confirmation=True,
            retryable=True,
        ),
    )
