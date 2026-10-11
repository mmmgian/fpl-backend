"""Gateway to the official FPL API: the only module that knows FPL URLs or speaks HTTP to FPL.

- One shared httpx.AsyncClient (connection + TLS reuse) instead of a client per request.
- Retry once on network errors, invalid JSON, 5xx and 429; 404 and other 4xx fail fast.
- Responses cached per endpoint (see config.CacheTTL) with single-flight loading.
  Cached values are shared between requests: treat them as read-only.
- Raw response bytes are kept for pass-through endpoints so they are not re-encoded.
"""
from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from typing import Any

import httpx

from .cache import AsyncTTLCache
from .config import CacheTTL

FPL_BASE = "https://fantasy.premierleague.com/api"
# FPL frequently answers 403 to requests without a browser-like UA.
FPL_HEADERS = {
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "accept": "application/json,text/plain,*/*",
    "referer": "https://fantasy.premierleague.com/",
}
HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
HTTP_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)
MAX_STANDINGS_PAGES = 40          # hard stop: 40 pages x 50 entries


class Upstream(Exception):
    """FPL failed. `status` is the upstream status, or 502 after retries were exhausted."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass(frozen=True)
class FplResponse:
    raw: bytes
    data: Any


@dataclass(frozen=True)
class Bootstrap:
    raw: bytes
    data: dict
    elements: dict[int, dict]
    teams: dict[int, dict]

    @property
    def events(self) -> list[dict]:
        return self.data.get("events", [])


class FplGateway:
    def __init__(self, ttl: CacheTTL | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
                 cache: AsyncTTLCache | None = None, retry_delay: tuple[float, float] = (0.2, 0.5)):
        self._ttl = ttl or CacheTTL()
        self._transport = transport
        self._cache = cache or AsyncTTLCache()
        self._retry_delay = retry_delay
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    # ---------- transport ----------
    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:   # a client is bound to its event loop
            self._client = httpx.AsyncClient(base_url=FPL_BASE, timeout=HTTP_TIMEOUT, headers=FPL_HEADERS,
                                             limits=HTTP_LIMITS, transport=self._transport)
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _fetch(self, path: str, params: dict | None, attempts: int = 2) -> FplResponse:
        last: Exception | None = None
        for i in range(attempts):
            try:
                r = await self._http().get(path, params=params)
                if r.status_code == 404:
                    raise Upstream(404, f"Not found upstream: {path}")
                if r.status_code >= 500 or r.status_code == 429:
                    raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                return FplResponse(raw=r.content, data=json.loads(r.content))
            except Upstream:
                raise
            except httpx.HTTPStatusError as e:
                last = e
                if e.response.status_code < 500 and e.response.status_code != 429:
                    raise Upstream(e.response.status_code, f"FPL responded {e.response.status_code}") from e
            except (httpx.TransportError, ValueError) as e:     # network trouble / invalid JSON
                last = e
            if i + 1 < attempts:
                lo, hi = self._retry_delay
                await asyncio.sleep(lo + random.random() * (hi - lo))
        raise Upstream(502, f"FPL unavailable: {last}")

    async def _get(self, path: str, params: dict | None = None, *, ttl: float, fresh: bool = False) -> FplResponse:
        key = (path, tuple(sorted((params or {}).items())))
        if fresh:
            return await self._cache.get_or_load(("fresh", key), 0, lambda: self._fetch(path, params))
        return await self._cache.get_or_load(key, ttl, lambda: self._fetch(path, params))

    # ---------- endpoints ----------
    async def bootstrap(self, *, fresh: bool = False) -> Bootstrap:
        async def load() -> Bootstrap:
            r = await self._fetch("/bootstrap-static/", None)
            data = r.data
            return Bootstrap(raw=r.raw, data=data,
                             elements={e["id"]: e for e in data.get("elements", [])},
                             teams={t["id"]: t for t in data.get("teams", [])})
        return await self._cache.get_or_load(("bootstrap", fresh), 0 if fresh else self._ttl.bootstrap, load)

    async def event_status(self, *, fresh: bool = False) -> dict:
        return (await self._get("/event-status/", ttl=self._ttl.event_status, fresh=fresh)).data

    async def fixtures(self, event: int | None = None, *, fresh: bool = False) -> FplResponse:
        params = {"event": event} if event is not None else None
        return await self._get("/fixtures/", params, ttl=self._ttl.fixtures, fresh=fresh)

    async def standings(self, league_id: int, *, fresh: bool = False) -> dict:
        """Every page of a classic league, combined. Never mutates cached pages."""
        pages: list[dict] = []
        page = 1
        while True:
            chunk = (await self._get(f"/leagues-classic/{league_id}/standings/", {"page_standings": page},
                                     ttl=self._ttl.standings, fresh=fresh)).data
            pages.append(chunk)
            if not chunk.get("standings", {}).get("has_next") or page >= MAX_STANDINGS_PAGES:
                break
            page += 1
        first = pages[0]
        if len(pages) == 1:
            return first
        results = [row for p in pages for row in p.get("standings", {}).get("results", [])]
        # has_next describes the LAST page fetched: false once everything was read, still true if the page cap cut us off.
        more = bool(pages[-1].get("standings", {}).get("has_next"))
        return {**first, "standings": {**first.get("standings", {}), "results": results, "has_next": more}}

    async def entry_history(self, entry_id: int) -> dict:
        return (await self._get(f"/entry/{entry_id}/history/", ttl=self._ttl.entry_history)).data

    async def picks(self, entry_id: int, gw: int) -> dict:
        return (await self._get(f"/entry/{entry_id}/event/{gw}/picks/", ttl=self._ttl.picks)).data

    async def event_live(self, gw: int) -> dict:
        return (await self._get(f"/event/{gw}/live/", ttl=self._ttl.live)).data
