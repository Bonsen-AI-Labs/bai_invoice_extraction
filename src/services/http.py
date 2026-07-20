"""Async HTTP client with Microsoft Graph auth.

The one place that knows how to reach an HTTP backend. SharePoint and Excel
clients build on this; they never touch httpx or MSAL directly. Azure DI and
Cosmos use their own SDKs and do not go through here.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import msal

from src.env import Settings

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class HTTPClient:
    def __init__(self, settings: Settings, max_retries: int = 3):
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=60.0)
        self._max_retries = max_retries
        self._msal = msal.ConfidentialClientApplication(
            client_id=settings.APPLICATION_CLIENT_ID,
            client_credential=settings.APPLICATION_CLIENT_SECRET,
            authority=f"https://login.microsoftonline.com/{settings.APPLICATION_TENANT_ID}",
        )
        self._token: str | None = None
        self._token_exp: float = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HTTPClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # --- auth ----------------------------------------------------------------
    def _graph_token(self) -> str:
        # ponytail: MSAL is sync and caches internally; skew guard is enough.
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        result = (
            self._msal.acquire_token_for_client(
                scopes=["https://graph.microsoft.com/.default"]
            )
            or {}
        )
        token = result.get("access_token")
        if not token:
            raise RuntimeError(
                f"Graph auth failed: {result.get('error_description', result)}"
            )
        self._token = token
        self._token_exp = time.time() + int(result.get("expires_in", 3600))
        return token

    # --- core request with retry --------------------------------------------
    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        resp = await self._client.request(method, url, **kwargs)
        for attempt in range(self._max_retries - 1):
            if resp.status_code not in _RETRY_STATUSES:
                return resp
            delay = float(resp.headers.get("Retry-After", 5 * 2**attempt))
            await asyncio.sleep(delay)
            resp = await self._client.request(method, url, **kwargs)
        return resp  # last response (caller inspects status)

    async def graph(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Call Graph; `path` is relative to /v1.0 (or an absolute @odata.nextLink)."""
        url = path if path.startswith("http") else f"{GRAPH_ROOT}{path}"
        headers = {
            "Authorization": f"Bearer {self._graph_token()}",
            **kwargs.pop("headers", {}),
        }
        return await self.request(method, url, headers=headers, **kwargs)

    async def graph_json(self, method: str, path: str, **kwargs) -> Any:
        resp = await self.graph(method, path, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    async def download(self, url: str) -> bytes:
        resp = await self.request("GET", url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
