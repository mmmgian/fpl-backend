from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx, sqlite3, json, asyncio, random, time
from datetime import datetime
from pathlib import Path

DB_PATH = Path("snapshots.db")

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

# ---------- Routes ----------

@app.get("/health")
async def health():
    return {"ok": True}

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

@app.get("/history/{league_id}")
async def list_history(league_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT gw, taken_at FROM league_snapshots WHERE league_id=? ORDER BY gw", (league_id,))
    rows = [{"gw": gw, "taken_at": taken} for gw, taken in cur.fetchall()]
    con.close()
    return {"league_id": league_id, "snapshots": rows}

@app.get("/history/{league_id}/{gw}")
async def get_snapshot(league_id: int, gw: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT data FROM league_snapshots WHERE league_id=? AND gw=?", (league_id, gw))
    row = cur.fetchone()
    con.close()
    if not row:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return json.loads(row[0])