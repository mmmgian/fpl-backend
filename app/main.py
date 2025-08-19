from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx, sqlite3, json, asyncio, random, time
from datetime import datetime
from pathlib import Path

DB_PATH = Path("snapshots.db")
FPL_HEADERS = {"referer": "https://fantasy.premierleague.com/"}

# ---------- DB init ----------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
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
    con.commit()
    con.close()

init_db()

# ---------- App & CORS ----------
app = FastAPI(title="FPL League API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Helpers ----------
HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=None)

async def _get_json(url: str, timeout: httpx.Timeout = HTTP_TIMEOUT, attempts: int = 2):
    last_err = None
    for i in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.05 + random.random() * 0.1)
    raise last_err

async def fetch_event_status():
    return await _get_json("https://fantasy.premierleague.com/api/event-status/")

async def fetch_bootstrap():
    return await _get_json("https://fantasy.premierleague.com/api/bootstrap-static/")

async def fetch_fixtures_for_gw(gw: int):
    all_fix = await _get_json("https://fantasy.premierleague.com/api/fixtures/")
    return [f for f in all_fix if f.get("event") == gw]

def fixtures_all_finished(fixtures: list) -> bool:
    if not fixtures:
        return False
    for f in fixtures:
        if not f.get("finished"):
            return False
        if "finished_provisional" in f and not f.get("finished_provisional"):
            return False
    return True

async def fetch_fpl_classic(league_id: int, page: int = 1):
    url = f"https://fantasy.premierleague.com/api/leagues-classic/{league_id}/standings/?page_standings={page}&per_page=50"
    return await _get_json(url)

async def fetch_all_standings(league_id: int):
    page, combined = 1, None
    while True:
        chunk = await fetch_fpl_classic(league_id, page)
        if combined is None:
            combined = chunk
        else:
            combined["standings"]["results"].extend(chunk["standings"]["results"])
        if not chunk["standings"].get("has_next"):
            break
        page += 1
    return combined or {}

async def fetch_entry_picks(entry_id: int, gw: int):
    url = f"https://fantasy.premierleague.com/api/entry/{entry_id}/event/{gw}/picks/"
    return await _get_json(url)

# ---------- Routes ----------

# --- Health check ---
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/league/{league_id}")
async def get_league(league_id: int):
    return await fetch_all_standings(league_id)

@app.post("/autosnapshot/{league_id}")
async def autosnapshot(league_id: int):
    try:
        async def do_work():
            status = await fetch_event_status()
            s = (status.get("status") or [{}])[0]
            gw = s.get("event")
            bonus_done = bool(s.get("bonus_added"))
            if not gw:
                raise HTTPException(status_code=503, detail="Could not determine current GW")

            fixtures = await fetch_fixtures_for_gw(gw)
            all_done = fixtures_all_finished(fixtures)

            bootstrap = await fetch_bootstrap()
            events = bootstrap.get("events", [])
            event_meta = next((e for e in events if e.get("id") == gw), None)
            gw_finished = bool(event_meta and event_meta.get("finished"))

            if not (bonus_done and all_done and gw_finished):
                return {"ok": False, "gw": gw, "bonus_added": bonus_done, "fixtures_all_finished": all_done, "gw_finished_flag": gw_finished, "reason": "GW not fully over yet"}

            data = await fetch_all_standings(league_id)
            con = sqlite3.connect(DB_PATH)
            cur = con.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO league_snapshots (league_id, gw, taken_at, data) VALUES (?, ?, ?, ?)",
                (league_id, gw, datetime.utcnow().isoformat(), json.dumps(data)),
            )
            con.commit()
            con.close()
            return {"ok": True, "snapshotted": True, "gw": gw}

        return await asyncio.wait_for(do_work(), timeout=25.0)

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Upstream timeout (FPL API slow)")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- History: all GWs for a league ---
@app.get("/history/{league_id}")
async def get_history(league_id: int):
    try:
        conn = sqlite3.connect("snapshots.db")
        cursor = conn.cursor()
        cursor.execute("SELECT gw, data FROM snapshots WHERE league_id = ? ORDER BY gw ASC", (league_id,))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return {"history": []}

        history = []
        for gw, data in rows:
            history.append({
                "gw": gw,
                "data": json.loads(data)
            })

        return {"history": history}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- History: specific GW ---
@app.get("/history/{league_id}/{gw}")
async def get_history_gw(league_id: int, gw: int):
    try:
        conn = sqlite3.connect("snapshots.db")
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM snapshots WHERE league_id = ? AND gw = ?", (league_id, gw))
        row = cursor.fetchone()
        conn.close()

        if not row:
            raise HTTPException(status_code=404, detail="No snapshot found for that GW")

        return {"gw": gw, "data": json.loads(row[0])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tenure/{entry_id}")
async def tenure(entry_id: int):
    """
    Returns how long an entry has played FPL, based on /api/entry/{id}/history/
    Response:
      {
        "entry_id": 9264528,
        "seasons_played": 9,
        "first_season": "2016/17",
        "playing_since_year": 2016,
        "seasons": ["2016/17","2017/18", ...]
      }
    """
    url = f"https://fantasy.premierleague.com/api/entry/{entry_id}/history/"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(url, headers=FPL_HEADERS)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Upstream error from FPL")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch tenure: {e}")

    past = data.get("past") or []
    # Collect season_name strings like "2016/17"
    seasons = [p.get("season_name") for p in past if isinstance(p, dict) and p.get("season_name")]
    seasons.sort()  # "YYYY/YY" sorts correctly for earliest first

    seasons_played = len(seasons)
    first_season = seasons[0] if seasons else None

    # Safely parse the starting year from "YYYY/YY"
    playing_since_year = None
    if first_season and isinstance(first_season, str) and "/" in first_season:
        try:
            playing_since_year = int(first_season.split("/")[0])
        except Exception:
            playing_since_year = None

    return {
        "entry_id": entry_id,
        "seasons_played": seasons_played,
        "first_season": first_season,
        "playing_since_year": playing_since_year,
        "seasons": seasons,
    }

@app.get("/team/{entry_id}")
async def get_team(entry_id: int):
    # 1) get current GW and bootstrap lookup tables
    bootstrap = await fetch_bootstrap()
    events = bootstrap.get("events", [])
    current = next((e for e in events if e.get("is_current")), None)
    gw = current["id"] if current else max((e for e in events if e.get("finished")), key=lambda e: e["id"], default={}).get("id")
    if not gw:
        raise HTTPException(status_code=503, detail="Could not determine current GW")

    elements = {e["id"]: e for e in bootstrap.get("elements", [])}
    teams = {t["id"]: t for t in bootstrap.get("teams", [])}
    pos_map = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    # 2) fetch picks
    data = await fetch_entry_picks(entry_id, gw)
    picks = data.get("picks", [])

    # 3) group by position
    grouped = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in picks:
        el = elements.get(p.get("element"))
        if not el:
            continue
        pos = pos_map.get(el.get("element_type"), "?")
        team = teams.get(el.get("team"), {})
        grouped[pos].append({
            "name": f"{el.get('first_name', '')} {el.get('second_name', '')}".strip(),
            "web_name": el.get("web_name"),
            "team": team.get("short_name") or team.get("name"),
            "now_cost": el.get("now_cost"),
            "selected_by_percent": el.get("selected_by_percent"),
            "is_captain": bool(p.get("is_captain")),
            "is_vice_captain": bool(p.get("is_vice_captain")),
            "multiplier": p.get("multiplier"),
        })

    return {
        "entry_id": entry_id,
        "gw": gw,
        "team": grouped,
    }

@app.get("/bonus/{gw}")
async def get_bonus_points(gw: int):
    url = f"https://fantasy.premierleague.com/api/event/{gw}/live/"
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()

    # Extract bonus points per match
    bonuses = []
    for element in data["elements"]:
        if element["stats"]["bonus"] > 0:
            bonuses.append({
                "player_id": element["id"],
                "bonus": element["stats"]["bonus"]
            })

    return {"gameweek": gw, "bonuses": bonuses}

    # --- Pass-through proxy endpoints for Nuxt ---

@app.get("/bootstrap-static")
async def proxy_bootstrap_static():
    url = "https://fantasy.premierleague.com/api/bootstrap-static/"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"referer": "https://fantasy.premierleague.com/"}) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()

@app.get("/fixtures")
async def proxy_fixtures(event: int | None = None):
    url = "https://fantasy.premierleague.com/api/fixtures/"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"referer": "https://fantasy.premierleague.com/"}) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    if event is not None:
        data = [f for f in data if f.get("event") == event]
    return data