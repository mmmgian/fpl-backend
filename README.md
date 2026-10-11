# FPL League API

A FastAPI service that sits in front of the official Fantasy Premier League API. It stores a frozen copy of the classic-league table once each gameweek is fully settled, so the frontend can show past standings.

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Liveness check. Free Render instances take about 40 s to wake up. |
| GET | `/league/{league_id}` | Full classic-league standings, with all pages merged. |
| POST | `/autosnapshot/{league_id}` | Stores the standings only after the current GW is finished, all fixtures are final and bonus has been added. Safe to call repeatedly. |
| GET | `/history/{league_id}` | `{league_id, snapshots: [{gw, taken_at, rebuilt}]}`. For the configured league this first makes sure nothing is missing (see *Self-healing history*); `503` + `Retry-After` while finished gameweeks are known to be missing and can't be supplied yet. |
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
| `SELF_HEAL_HISTORY` | `1` | Rebuild missing history from FPL (see below). `0`, `false`, `no` or `off` turns it off. |
| `SNAPSHOT_SECRET` | unset | When set, `POST /autosnapshot` requires the header `X-Snapshot-Secret`, and it **turns on** `PUT /history/...` (which refuses everything while this is unset). |

> **Persistence:** the filesystem on Render's free tier is wiped on every deploy and restart, so any snapshots you've stored are lost. With *Self-healing history* (below) the server rebuilds what it can by itself, so the free tier is workable; a Render Disk (e.g. at `/data` with `DB_PATH=/data/snapshots.db`) or a hosted database is still the way to keep the tables saved *at the time*, which are exact.

## Self-healing history

On a server whose disk is wiped, the stored history is rebuilt from FPL without anyone running a script (`HistoryKeeper` in `app/services.py`, using the rebuild described below). It starts in the background at startup, and `GET /history/...` for the configured league makes sure it is running and waits up to 8 s for it.

- **What it rebuilds:** every gameweek that is finished and verified by FPL, has no stored table, and is **not the current gameweek**. The current one is left for `/autosnapshot`, which can still save FPL's own table, and a stored snapshot is never overwritten.
- **How it is stored:** through the same checks as `PUT /history/...` (internal consistency against the managers' histories, then the strict payload validation). Tables are flagged `rebuilt`, and managers whose order can't be recovered are flagged `≈`, exactly as for a manual backfill.
- **What it costs:** nothing while the history is complete (one cached look at FPL's gameweek list every 5 minutes). A rebuild is one league request plus one history request per manager, four at a time.
- **When FPL is unwell:** it backs off for 2 minutes and retries on the next request. While gameweeks are *known* to be missing and can't be supplied, `/history/{league}` answers `503` (`Retry-After: 30`) instead of a short list, because the frontend caches a `200` list for a day. If FPL can't even say which gameweeks are over, nothing is claimed missing and what is stored is served.
- **A gameweek that can't be rebuilt consistently** is left out for that process, logged (`app.history` at WARNING), and never makes the list a `503`.
- **Only the configured league** (`LEAGUE_ID`) ever causes a fetch.

What it can't recover: tables saved at the time by `/autosnapshot` are FPL's own and exact; after a wipe they come back as rebuilt ones (same totals; only managers level on points *and* overall rank have an approximate order).

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
