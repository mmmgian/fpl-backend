"""FPL League API: thin, cached facade over the official FPL API plus league snapshots.

Endpoints (paths unchanged from v1):
  GET  /health
  GET  /league/{league_id}                 full classic-league standings (all pages)
  POST /autosnapshot/{league_id}           store standings once a gameweek is fully over
  GET  /history/{league_id}                list of stored snapshots   -> {league_id, snapshots: [{gw, taken_at}]}
  GET  /history/{league_id}/{gw}           one snapshot               -> {league_id, gw, taken_at, data}
  GET  /tenure/{entry_id}                  seasons played
  GET  /team/{entry_id}                    current picks grouped by position
  GET  /bonus/{gw}                         players with bonus in a gameweek
  GET  /bootstrap-static, /fixtures        pass-through proxies

Configuration (environment):
  LEAGUE_ID          default league for docs/tests (467763 = The Lobster League, 2026/27)
  DB_PATH            SQLite file (default: snapshots.db in the working directory)
  ALLOWED_ORIGINS    comma-separated CORS origins (default "*")
  SNAPSHOT_SECRET    if set, POST /autosnapshot requires header  X-Snapshot-Secret: <value>
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ---------- Config ----------
LEAGUE_ID = int(os.environ.get("LEAGUE_ID", "467763"))
DB_PATH = Path(os.environ.get("DB_PATH", "snapshots.db"))
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
SNAPSHOT_SECRET = os.environ.get("SNAPSHOT_SECRET") or None

FPL_BASE = "https://fantasy.premierleague.com/api"
# FPL frequently answers 403 to requests without a browser-like UA.
FPL_HEADERS = {
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "accept": "application/json,text/plain,*/*",
    "referer": "https://fantasy.premierleague.com/",
}
HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


# ---------- DB ----------
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with closing(db()) as con, con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS league_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              league_id INTEGER NOT NULL,
              gw INTEGER NOT NULL,
              taken_at TEXT NOT NULL,
              data TEXT NOT NULL,
              UNIQUE(league_id, gw)
            )
            """
        )
        # v1 wrote to league_snapshots but read from a legacy `snapshots` table; fold any legacy rows in.
        legacy = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='snapshots'").fetchone()
        if legacy:
            con.execute(
                "INSERT OR IGNORE INTO league_snapshots (league_id, gw, taken_at, data) "
                "SELECT league_id, gw, ?, data FROM snapshots WHERE league_id IS NOT NULL AND gw IS NOT NULL",
                (datetime.now(timezone.utc).isoformat(),),
            )


def save_snapshot(league_id: int, gw: int, data: dict) -> bool:
    """True if stored, False if a snapshot for (league, gw) already existed."""
    with closing(db()) as con, con:
        cur = con.execute(
            "INSERT OR IGNORE INTO league_snapshots (league_id, gw, taken_at, data) VALUES (?, ?, ?, ?)",
            (league_id, gw, datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(data)),
        )
        return cur.rowcount == 1


def list_snapshots(league_id: int) -> list[dict]:
    with closing(db()) as con:
        rows = con.execute(
            "SELECT gw, taken_at FROM league_snapshots WHERE league_id = ? ORDER BY gw ASC", (league_id,)
        ).fetchall()
    return [{"gw": r["gw"], "taken_at": r["taken_at"]} for r in rows]


def get_snapshot(league_id: int, gw: int) -> dict | None:
    with closing(db()) as con:
        r = con.execute(
            "SELECT gw, taken_at, data FROM league_snapshots WHERE league_id = ? AND gw = ?", (league_id, gw)
        ).fetchone()
    if r is None:
        return None
    return {"league_id": league_id, "gw": r["gw"], "taken_at": r["taken_at"], "data": json.loads(r["data"])}


# ---------- FPL client ----------
class Upstream(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


async def fpl_get(path: str, params: dict | None = None, attempts: int = 2) -> Any:
    """GET {FPL_BASE}{path} with browser headers, timeout and one retry on network/5xx errors."""
    url = f"{FPL_BASE}{path}"
    last: Exception | None = None
    for i in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=FPL_HEADERS) as client:
                r = await client.get(url, params=params)
            if r.status_code == 404:
                raise Upstream(404, f"Not found upstream: {path}")
            if r.status_code >= 500 or r.status_code == 429:
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except Upstream:
            raise
        except httpx.HTTPStatusError as e:
            last = e
            if e.response.status_code < 500 and e.response.status_code != 429:
                raise Upstream(e.response.status_code, f"FPL responded {e.response.status_code}") from e
        except (httpx.TransportError, ValueError) as e:     # network trouble / invalid JSON
            last = e
        if i + 1 < attempts:
            await asyncio.sleep(0.2 + random.random() * 0.3)
    raise Upstream(502, f"FPL unavailable: {last}")


async def fetch_bootstrap() -> dict:
    return await fpl_get("/bootstrap-static/")


async def fetch_event_status() -> dict:
    return await fpl_get("/event-status/")


async def fetch_fixtures(event: int | None = None) -> list:
    return await fpl_get("/fixtures/", params={"event": event} if event is not None else None)


async def fetch_all_standings(league_id: int) -> dict:
    page, combined = 1, None
    while True:
        chunk = await fpl_get(f"/leagues-classic/{league_id}/standings/", params={"page_standings": page})
        if combined is None:
            combined = chunk
        else:
            combined["standings"]["results"].extend(chunk["standings"]["results"])
        if not chunk.get("standings", {}).get("has_next") or page >= 40:   # hard stop: 40 pages x 50
            break
        page += 1
    return combined or {}


def current_gw(events: list[dict]) -> int | None:
    cur = next((e for e in events if e.get("is_current")), None)
    if cur:
        return cur["id"]
    finished = [e["id"] for e in events if e.get("finished")]
    return max(finished) if finished else None


def fixtures_all_finished(fixtures: list) -> bool:
    return bool(fixtures) and all(f.get("finished") and f.get("finished_provisional", True) for f in fixtures)


# ---------- App ----------
init_db()
app = FastAPI(title="FPL League API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOWED_ORIGINS != ["*"],   # credentials + wildcard is invalid per the CORS spec
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def upstream_error(e: Upstream) -> HTTPException:
    return HTTPException(status_code=e.status if e.status in (404, 429) else 502, detail=e.detail)


@app.get("/health")
async def health():
    return {"status": "ok", "league_id": LEAGUE_ID}


@app.get("/league/{league_id}")
async def get_league(league_id: int):
    try:
        return await fetch_all_standings(league_id)
    except Upstream as e:
        raise upstream_error(e)


@app.post("/autosnapshot/{league_id}")
async def autosnapshot(league_id: int, x_snapshot_secret: str | None = Header(default=None)):
    if SNAPSHOT_SECRET and x_snapshot_secret != SNAPSHOT_SECRET:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Snapshot-Secret")

    async def work():
        s = ((await fetch_event_status()).get("status") or [{}])[0]
        gw, bonus_done = s.get("event"), bool(s.get("bonus_added"))
        if not gw:
            raise HTTPException(status_code=503, detail="Could not determine current GW")
        fixtures, bootstrap = await asyncio.gather(fetch_fixtures(gw), fetch_bootstrap())
        all_done = fixtures_all_finished(fixtures)
        meta = next((e for e in bootstrap.get("events", []) if e.get("id") == gw), None)
        gw_finished = bool(meta and meta.get("finished"))
        if not (bonus_done and all_done and gw_finished):
            return {"ok": False, "gw": gw, "bonus_added": bonus_done, "fixtures_all_finished": all_done,
                    "gw_finished_flag": gw_finished, "reason": "GW not fully over yet"}
        stored = save_snapshot(league_id, gw, await fetch_all_standings(league_id))
        return {"ok": True, "snapshotted": stored, "already_existed": not stored, "gw": gw}

    try:
        return await asyncio.wait_for(work(), timeout=25.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Upstream timeout (FPL API slow)")
    except Upstream as e:
        raise upstream_error(e)


@app.get("/history/{league_id}")
async def get_history(league_id: int):
    return {"league_id": league_id, "snapshots": list_snapshots(league_id)}


@app.get("/history/{league_id}/{gw}")
async def get_history_gw(league_id: int, gw: int):
    snap = get_snapshot(league_id, gw)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"No snapshot for league {league_id}, GW {gw}")
    return snap


@app.get("/tenure/{entry_id}")
async def tenure(entry_id: int):
    try:
        data = await fpl_get(f"/entry/{entry_id}/history/")
    except Upstream as e:
        raise upstream_error(e)
    seasons = sorted(p["season_name"] for p in data.get("past") or [] if isinstance(p, dict) and p.get("season_name"))
    first = seasons[0] if seasons else None
    since = None
    if first and "/" in first:
        try:
            since = int(first.split("/")[0])
        except ValueError:
            since = None
    return {"entry_id": entry_id, "seasons_played": len(seasons), "first_season": first,
            "playing_since_year": since, "seasons": seasons}


@app.get("/team/{entry_id}")
async def get_team(entry_id: int):
    try:
        bootstrap = await fetch_bootstrap()
        gw = current_gw(bootstrap.get("events", []))
        if not gw:
            raise HTTPException(status_code=503, detail="Season has not started: no current GW")
        data = await fpl_get(f"/entry/{entry_id}/event/{gw}/picks/")
    except Upstream as e:
        raise upstream_error(e)
    elements = {e["id"]: e for e in bootstrap.get("elements", [])}
    teams = {t["id"]: t for t in bootstrap.get("teams", [])}
    grouped: dict[str, list] = {p: [] for p in POSITIONS.values()}
    for p in data.get("picks", []):
        el = elements.get(p.get("element"))
        if not el:
            continue
        team = teams.get(el.get("team"), {})
        grouped[POSITIONS.get(el.get("element_type"), "MID")].append({
            "id": el["id"],
            "name": f"{el.get('first_name', '')} {el.get('second_name', '')}".strip(),
            "web_name": el.get("web_name"),
            "team": team.get("short_name") or team.get("name"),
            "now_cost": el.get("now_cost"),
            "selected_by_percent": el.get("selected_by_percent"),
            "is_captain": bool(p.get("is_captain")),
            "is_vice_captain": bool(p.get("is_vice_captain")),
            "multiplier": p.get("multiplier"),
            "position": p.get("position"),       # 1-11 starting XI, 12-15 bench
        })
    return {"entry_id": entry_id, "gw": gw, "team": grouped}


@app.get("/bonus/{gw}")
async def get_bonus_points(gw: int):
    try:
        data = await fpl_get(f"/event/{gw}/live/")
    except Upstream as e:
        raise upstream_error(e)
    bonuses = [{"player_id": el["id"], "bonus": el["stats"]["bonus"]}
               for el in data.get("elements", []) if el.get("stats", {}).get("bonus", 0) > 0]
    return {"gameweek": gw, "bonuses": bonuses}


@app.get("/bootstrap-static")
async def proxy_bootstrap_static():
    try:
        return await fetch_bootstrap()
    except Upstream as e:
        raise upstream_error(e)


@app.get("/fixtures")
async def proxy_fixtures(event: int | None = None):
    try:
        return await fetch_fixtures(event)
    except Upstream as e:
        raise upstream_error(e)
