# FPL League API

A FastAPI service that sits in front of the official Fantasy Premier League API. It stores a frozen copy of the classic-league table once each gameweek is fully settled, so the frontend can show past standings.

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Liveness check. Free Render instances take about 40 s to wake up. |
| GET | `/league/{league_id}` | Full classic-league standings, with all pages merged. |
| POST | `/autosnapshot/{league_id}` | Stores the standings only after the current GW is finished, all fixtures are final and bonus has been added. Safe to call repeatedly. |
| GET | `/history/{league_id}` | `{league_id, snapshots: [{gw, taken_at}]}` |
| GET | `/history/{league_id}/{gw}` | `{league_id, gw, taken_at, data}`. Returns `404` if no snapshot exists. |
| PUT | `/history/{league_id}/{gw}` | Stores a table **rebuilt after the fact** (see below). Off unless `SNAPSHOT_SECRET` is set; needs `X-Snapshot-Secret`. `201` stored, `401` wrong secret, `403` disabled, `409` already stored (never overwritten), `413` over 256 KB, `422` invalid or the GW is not finished. Not callable from a browser (CORS allows GET/POST only). |
| GET | `/team/{entry_id}` | Current picks grouped by GK/DEF/MID/FWD. Returns `404` for an unknown entry. |
| GET | `/tenure/{entry_id}` | Seasons played. |
| GET | `/bonus/{gw}` | Players who got bonus points in that GW. |
| GET | `/bootstrap-static`, `/fixtures?event=N` | Pass-through proxies. |

Errors from FPL are mapped as follows: `404` stays `404`, `429` stays `429`, and anything else becomes `502`. Errors are never surfaced as a bare `500`.

## Configuration

See `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LEAGUE_ID` | `467763` | Default league ID (The Lobster League, 2026/27). |
| `DB_PATH` | `snapshots.db` | Path to the SQLite file. On Render, point this at a persistent disk. |
| `ALLOWED_ORIGINS` | `*` | Comma-separated list of allowed origins, e.g. `https://your-app.vercel.app`. |
| `SNAPSHOT_SECRET` | unset | When set, `POST /autosnapshot` requires the header `X-Snapshot-Secret`, and it **turns on** `PUT /history/...` (which refuses everything while this is unset). |

> **Persistence:** the filesystem on Render's free tier is wiped on every deploy and restart, so any snapshots you've stored will be lost. To keep them, attach a Render Disk (e.g. mounted at `/data`) and set `DB_PATH=/data/snapshots.db`, or move storage to a hosted database.

## Backfilling missed history

FPL only serves the *current* league table, but every manager's season history is public, so a past table can be rebuilt (`app/backfill.py`): total and GW points come from each manager's history, and managers level on points are ordered by overall rank after that gameweek. Managers level on points **and** overall rank have no recoverable order; those rows are stored flagged (`rank_approximate`, listed in `reconstructed.unresolved_ties`) and the frontend marks them with a `≈`.

```bash
python scripts/backfill_history.py build            # fetch, rebuild, run the FPL-based checks
python scripts/backfill_history.py verify-reports   # compare with the league's printed reports
python scripts/backfill_history.py publish          # dry run
SNAPSHOT_SECRET=... python scripts/backfill_history.py publish --url https://<backend> --execute
```

`publish` refuses unless every check passed, sends exactly the files that were validated (hashes recorded in the manifest), and never overwrites. The current gameweek is held back because `/autosnapshot` can still save the real one (`--include-current` to override). The checks' scopes are printed: the live-table check covers only the latest finished GW, and the printed reports cover GW1-5.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest
```

Tests replace every FPL call with stubs and use a temporary database, so they never touch the network or `snapshots.db`.
