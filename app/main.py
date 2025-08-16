# app/main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import time

app = FastAPI(title="FPL League API")

# 🔐 CORS: update allowed origins for your Vercel domain later
origins = [
    "http://localhost:3000",
    "https://*.vercel.app",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple in-memory cache (per-process)
CACHE_TTL = 60  # seconds
_cache = {}

async def fetch_fpl_classic(league_id: int):
    url = f"https://fantasy.premierleague.com/api/leagues-classic/{league_id}/standings/"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()

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
        raw = await fetch_fpl_classic(league_id)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Return as-is; shape includes league metadata + standings.results
    data = raw
    _cache[key] = (now, data)
    return data
