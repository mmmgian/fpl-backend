from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import httpx, time, sqlite3, json
from datetime import datetime
from pathlib import Path

DB_PATH = Path("snapshots.db")

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

app = FastAPI(title="FPL League API")
origins = ["http://localhost:3000", "https://*.vercel.app"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CACHE_TTL = 60
_cache = {}

async def fetch_fpl_classic(league_id: int, page: int = 1):
    url = f"https://fantasy.premierleague.com/api/leagues-classic/{league_id}/standings/?page_standings={page}&per_page=50"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()

async def fetch_all_standings(league_id: int):
    page = 1
    combined = None
    while True:
        chunk = await fetch_fpl_classic(league_id, page=page)
        if combined is None:
            combined = chunk
        else:
            combined["standings"]["results"].extend(chunk["standings"]["results"])
        if not chunk["standings"].get("has_next"):
            break
        page += 1
    return combined or {}

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/league/{league_id}")
async def get_league(league_id: int):
    now = time.time()
    key = ("classic", league_id)
    if key in _cache:
        cached_at, data = _cache[key]
        if now - cached_at < CACHE_TTL:
            return data
    try:
        raw = await fetch_all_standings(league_id)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    _cache[key] = (now, raw)
    return raw

async def current_gw():
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get("https://fantasy.premierleague.com/api/bootstrap-static/")
        r.raise_for_status()
        data = r.json()
        events = data.get("events", [])
        curr = next((e for e in events if e.get("is_current")), None)
        if not curr:
            curr = max((e for e in events if e.get("finished")), key=lambda e: e["id"], default=None)
        return curr["id"] if curr else None

@app.post("/snapshot/{league_id}")
async def snapshot_league(league_id: int, gw: Optional[int] = None):
    try:
        gw_id = gw or (await current_gw())
        if not gw_id:
            raise HTTPException(status_code=400, detail="Could not determine current GW")
        data = await fetch_all_standings(league_id)
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO league_snapshots (league_id, gw, taken_at, data) VALUES (?, ?, ?, ?)",
            (league_id, gw_id, datetime.utcnow().isoformat(), json.dumps(data)),
        )
        con.commit()
        con.close()
        return {"ok": True, "league_id": league_id, "gw": gw_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/history/{league_id}")
async def list_history(league_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT gw, taken_at FROM league_snapshots WHERE league_id=? ORDER BY gw", (league_id,))
    rows = [{"gw": gw, "taken_at": taken_at} for gw, taken_at in cur.fetchall()]
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