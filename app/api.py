"""HTTP layer: routing, auth header, error mapping, CORS and compression. No business rules here."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response

from .config import Settings
from .fpl import FplGateway, Upstream
from .services import GameweekService, LeagueService, NotReady, TeamService, TimedOut
from .storage import SnapshotRepository


def upstream_error(e: Upstream) -> HTTPException:
    return HTTPException(status_code=e.status if e.status in (404, 429) else 502, detail=e.detail)


def json_bytes(body: bytes) -> Response:
    return Response(content=body, media_type="application/json")


def create_app(settings: Settings, *, fpl: FplGateway | None = None,
               snapshots: SnapshotRepository | None = None) -> FastAPI:
    fpl = fpl or FplGateway(settings.cache_ttl)
    snapshots = snapshots or SnapshotRepository(settings.db_path)
    snapshots.init()

    leagues = LeagueService(fpl, snapshots)
    teams = TeamService(fpl)
    gameweeks = GameweekService(fpl)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await fpl.aclose()

    app = FastAPI(title="FPL League API", version="2.1.0", lifespan=lifespan)
    origins = list(settings.allowed_origins)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=origins != ["*"],   # credentials + wildcard is invalid per the CORS spec
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok", "league_id": settings.league_id}

    @app.get("/league/{league_id}")
    async def get_league(league_id: int):
        try:
            return await leagues.standings(league_id)
        except Upstream as e:
            raise upstream_error(e)

    @app.post("/autosnapshot/{league_id}")
    async def autosnapshot(league_id: int, x_snapshot_secret: str | None = Header(default=None)):
        if settings.snapshot_secret and x_snapshot_secret != settings.snapshot_secret:
            raise HTTPException(status_code=401, detail="Missing or invalid X-Snapshot-Secret")
        try:
            return await leagues.autosnapshot(league_id)
        except NotReady as e:
            raise HTTPException(status_code=503, detail=str(e))
        except TimedOut as e:
            raise HTTPException(status_code=504, detail=str(e))
        except Upstream as e:
            raise upstream_error(e)

    # Plain `def`: FastAPI runs these in its thread pool, keeping SQLite I/O off the event loop.
    @app.get("/history/{league_id}")
    def get_history(league_id: int):
        return leagues.history(league_id)

    @app.get("/history/{league_id}/{gw}")
    def get_history_gw(league_id: int, gw: int):
        body = leagues.snapshot_json(league_id, gw)
        if body is None:
            raise HTTPException(status_code=404, detail=f"No snapshot for league {league_id}, GW {gw}")
        return json_bytes(body)

    @app.get("/tenure/{entry_id}")
    async def tenure(entry_id: int):
        try:
            return await teams.tenure(entry_id)
        except Upstream as e:
            raise upstream_error(e)

    @app.get("/team/{entry_id}")
    async def get_team(entry_id: int):
        try:
            return await teams.current_team(entry_id)
        except NotReady as e:
            raise HTTPException(status_code=503, detail=str(e))
        except Upstream as e:
            raise upstream_error(e)

    @app.get("/bonus/{gw}")
    async def get_bonus_points(gw: int):
        try:
            return await gameweeks.bonus(gw)
        except Upstream as e:
            raise upstream_error(e)

    # Pass-throughs serve FPL's bytes directly: re-encoding bootstrap costs ~60 ms of event-loop CPU.
    @app.get("/bootstrap-static")
    async def proxy_bootstrap_static():
        try:
            return json_bytes(await gameweeks.bootstrap_raw())
        except Upstream as e:
            raise upstream_error(e)

    @app.get("/fixtures")
    async def proxy_fixtures(event: int | None = None):
        try:
            return json_bytes(await gameweeks.fixtures_raw(event))
        except Upstream as e:
            raise upstream_error(e)

    return app
