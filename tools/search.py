from typing import Optional
from tavily import TavilyClient

from app_config import get_app_setting

_client: Optional[TavilyClient] = None
_client_signature: Optional[str] = None


def _get_client() -> TavilyClient:
    global _client, _client_signature
    api_key = get_app_setting("TAVILY_API_KEY", "") or ""
    if _client is None or _client_signature != api_key:
        if not api_key:
            raise ValueError("TAVILY_API_KEY is not set")
        _client = TavilyClient(api_key=api_key)
        _client_signature = api_key
    return _client


def search(query: str, max_results: int = 5) -> dict:
    """Search the web using Tavily API."""
    client = _get_client()
    response = client.search(
        query=query,
        max_results=max_results,
        include_answer=True,
    )
    return {
        "answer": response.get("answer", ""),
        "results": [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
                "score": r.get("score", 0),
            }
            for r in response.get("results", [])
        ],
    }


# Tool definition for Anthropic Tool Use API
TOOL_DEFINITION = {
    "name": "internet_lookup",
    "description": (
        "Search the web for current information. Use this when you need up-to-date "
        "facts, news, or information that may not be in your training data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (1-10, default 5)",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}
