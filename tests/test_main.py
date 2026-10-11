"""API tests: FPL calls are stubbed, the DB is a throwaway temp file.

Run from the repo root:  pip install -r requirements-dev.txt && pytest
"""
import importlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_app(db_path: Path, **env):
    os.environ["DB_PATH"] = str(db_path)
    for k in ("SNAPSHOT_SECRET", "ALLOWED_ORIGINS"):
        os.environ.pop(k, None)
    os.environ.update(env)
    import app.main as m
    return importlib.reload(m)


@pytest.fixture
def tmpdb(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def m(tmpdb):
    return load_app(tmpdb)


def stub(m, monkeypatch, routes: dict):
    """routes: path -> payload, or path -> callable(params) -> payload, or Upstream instance."""
    calls = []

    async def fake(path, params=None, attempts=2):
        calls.append((path, params))
        v = routes[path]
        if isinstance(v, Exception):
            raise v
        return v(params) if callable(v) else json.loads(json.dumps(v))

    monkeypatch.setattr(m, "fpl_get", fake)
    return calls


BOOT = {
    "events": [{"id": 6, "finished": True, "is_current": False}, {"id": 7, "finished": True, "is_current": True}],
    "elements": [
        {"id": 1, "first_name": "Erling", "second_name": "Haaland", "web_name": "Haaland", "team": 1,
         "element_type": 4, "now_cost": 145, "selected_by_percent": "60.1"},
        {"id": 2, "first_name": "Jordan", "second_name": "Pickford", "web_name": "Pickford", "team": 2,
         "element_type": 1, "now_cost": 55, "selected_by_percent": "20.0"},
    ],
    "teams": [{"id": 1, "name": "Man City", "short_name": "MCI"}, {"id": 2, "name": "Everton", "short_name": "EVE"}],
}


def standings_pages(params):
    page = params["page_standings"]
    return {"league": {"id": 467763, "name": "The Lobster League"},
            "standings": {"has_next": page < 2, "results": [{"entry": page, "rank": page}]}}


def test_health(m):
    r = TestClient(m.app).get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_league_combines_pages(m, monkeypatch):
    stub(m, monkeypatch, {"/leagues-classic/467763/standings/": standings_pages})
    body = TestClient(m.app).get("/league/467763").json()
    assert [x["entry"] for x in body["standings"]["results"]] == [1, 2]


def test_league_upstream_404_is_404(m, monkeypatch):
    stub(m, monkeypatch, {"/leagues-classic/1/standings/": m.Upstream(404, "nope")})
    assert TestClient(m.app).get("/league/1").status_code == 404


def test_upstream_outage_is_502(m, monkeypatch):
    stub(m, monkeypatch, {"/bootstrap-static/": m.Upstream(503, "down")})
    assert TestClient(m.app).get("/bootstrap-static").status_code == 502


def _gw_routes(bonus_added=True, finished=True):
    return {
        "/event-status/": {"status": [{"event": 7, "bonus_added": bonus_added}]},
        "/fixtures/": lambda p: [{"event": p["event"], "finished": finished, "finished_provisional": finished}],
        "/bootstrap-static/": BOOT,
        "/leagues-classic/467763/standings/": standings_pages,
    }


def test_snapshot_then_history_roundtrip(m, monkeypatch):
    calls = stub(m, monkeypatch, _gw_routes())
    c = TestClient(m.app)
    r = c.post("/autosnapshot/467763").json()
    assert r == {"ok": True, "snapshotted": True, "already_existed": False, "gw": 7}
    assert ("/fixtures/", {"event": 7}) in calls            # filtered upstream, not client-side

    again = c.post("/autosnapshot/467763").json()
    assert again["snapshotted"] is False and again["already_existed"] is True

    hist = c.get("/history/467763").json()
    assert hist["league_id"] == 467763 and [s["gw"] for s in hist["snapshots"]] == [7]
    one = c.get("/history/467763/7").json()
    assert one["gw"] == 7 and one["data"]["league"]["name"] == "The Lobster League" and one["taken_at"]


def test_snapshot_skipped_while_gw_live(m, monkeypatch):
    stub(m, monkeypatch, _gw_routes(bonus_added=False, finished=False))
    r = TestClient(m.app).post("/autosnapshot/467763").json()
    assert r["ok"] is False and r["bonus_added"] is False and r["fixtures_all_finished"] is False


def test_snapshot_secret_enforced(tmpdb, monkeypatch):
    m = load_app(tmpdb, SNAPSHOT_SECRET="s3cret")
    stub(m, monkeypatch, _gw_routes())
    c = TestClient(m.app)
    assert c.post("/autosnapshot/467763").status_code == 401
    assert c.post("/autosnapshot/467763", headers={"X-Snapshot-Secret": "s3cret"}).status_code == 200


def test_history_missing_gw_is_404_not_500(m):
    r = TestClient(m.app).get("/history/467763/99")
    assert r.status_code == 404


def test_history_empty_league(m):
    assert TestClient(m.app).get("/history/123").json() == {"league_id": 123, "snapshots": []}


def test_legacy_snapshots_table_migrated(tmpdb):
    con = sqlite3.connect(tmpdb)
    con.execute("CREATE TABLE snapshots (league_id INTEGER, gw INTEGER, data TEXT, PRIMARY KEY (league_id, gw))")
    con.execute("INSERT INTO snapshots VALUES (5, 3, ?)", (json.dumps({"x": 1}),))
    con.commit()
    con.close()
    m = load_app(tmpdb)
    assert TestClient(m.app).get("/history/5/3").json()["data"] == {"x": 1}


def test_team_grouped_by_position(m, monkeypatch):
    stub(m, monkeypatch, {
        "/bootstrap-static/": BOOT,
        "/entry/42/event/7/picks/": {"picks": [
            {"element": 1, "position": 1, "multiplier": 2, "is_captain": True, "is_vice_captain": False},
            {"element": 2, "position": 12, "multiplier": 0, "is_captain": False, "is_vice_captain": True},
            {"element": 999, "position": 2, "multiplier": 1},          # unknown element is skipped
        ]},
    })
    body = TestClient(m.app).get("/team/42").json()
    assert body["gw"] == 7
    assert body["team"]["FWD"][0]["web_name"] == "Haaland" and body["team"]["FWD"][0]["is_captain"]
    assert body["team"]["GK"][0]["position"] == 12
    assert body["team"]["DEF"] == [] and body["team"]["MID"] == []


def test_team_unknown_entry_404(m, monkeypatch):
    stub(m, monkeypatch, {"/bootstrap-static/": BOOT, "/entry/1/event/7/picks/": m.Upstream(404, "x")})
    assert TestClient(m.app).get("/team/1").status_code == 404


def test_tenure(m, monkeypatch):
    stub(m, monkeypatch, {"/entry/42/history/": {"past": [{"season_name": "2019/20"}, {"season_name": "2017/18"}]}})
    body = TestClient(m.app).get("/tenure/42").json()
    assert body["seasons_played"] == 2 and body["first_season"] == "2017/18" and body["playing_since_year"] == 2017


def test_bonus_filters_zero(m, monkeypatch):
    stub(m, monkeypatch, {"/event/7/live/": {"elements": [
        {"id": 1, "stats": {"bonus": 3}}, {"id": 2, "stats": {"bonus": 0}}]}})
    assert TestClient(m.app).get("/bonus/7").json() == {"gameweek": 7, "bonuses": [{"player_id": 1, "bonus": 3}]}


def test_cors_wildcard_without_credentials(m):
    r = TestClient(m.app).get("/health", headers={"Origin": "https://example.com"})
    assert r.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in r.headers


def test_cors_allowlist(tmpdb):
    m = load_app(tmpdb, ALLOWED_ORIGINS="https://lobster.vercel.app")
    c = TestClient(m.app)
    ok = c.get("/health", headers={"Origin": "https://lobster.vercel.app"})
    bad = c.get("/health", headers={"Origin": "https://evil.example"})
    assert ok.headers.get("access-control-allow-origin") == "https://lobster.vercel.app"
    assert "access-control-allow-origin" not in bad.headers
