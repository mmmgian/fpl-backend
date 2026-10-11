"""Composition root. Render starts this module: `uvicorn app.main:app`.

Endpoints:
  GET  /health
  GET  /league/{league_id}                 full classic-league standings (all pages)
  POST /autosnapshot/{league_id}           store standings once a gameweek is fully over
  GET  /history/{league_id}                list of stored snapshots   -> {league_id, snapshots: [{gw, taken_at}]}
  GET  /history/{league_id}/{gw}           one snapshot               -> {league_id, gw, taken_at, data}
  GET  /tenure/{entry_id}                  seasons played
  GET  /team/{entry_id}                    current picks grouped by position
  GET  /bonus/{gw}                         players with bonus in a gameweek
  GET  /bootstrap-static, /fixtures        pass-through proxies

Configuration (environment): LEAGUE_ID, DB_PATH, ALLOWED_ORIGINS, SNAPSHOT_SECRET (see config.py).

Layers (dependencies point inwards):
  api.py  ->  services.py  ->  domain.py (pure rules)
                 |-> fpl.py (FPL gateway, cache.py)   |-> storage.py (SQLite)
"""
import sys
from pathlib import Path

if not __package__:   # started as `uvicorn main:app` from inside app/: make the `app` package importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402

app = create_app(Settings.from_env())
