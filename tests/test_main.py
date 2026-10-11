"""API, service and cache tests. FPL is replaced by an httpx.MockTransport; the DB is a temp file.

Run from the repo root:  pip install -r requirements-dev.txt && pytest
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import domain  # noqa: E402
from app.api import create_app  # noqa: E402
from app.cache import AsyncTTLCache  # noqa: E402
from app.config import Settings  # noqa: E402
from app.fpl import FplGateway, Upstream  # noqa: E402


class FakeFpl:
    """routes: FPL path -> payload | callable(params) -> payload | int (HTTP error status)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        params = {k: int(v) if v.isdigit() else v for k, v in request.url.params.items()} or None
        self.calls.append((path, params))
        v = self.routes.get(path, 404)
        if isinstance(v, int):
            return httpx.Response(v, text="error")
        body = v(params) if callable(v) else v
        return httpx.Response(200, content=json.dumps(body).encode(), headers={"content-type": "application/json"})

    def gateway(self, **kw) -> FplGateway:
        return FplGateway(transport=httpx.MockTransport(self.handler), retry_delay=(0, 0), **kw)


def make(tmp_path, routes=None, **env):
    fake = FakeFpl(routes or {})
    settings = Settings.from_env({"DB_PATH": str(tmp_path / "test.db"), **env})
    return TestClient(create_app(settings, fpl=fake.gateway())), fake


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


def gw_routes(bonus_added=True, finished=True):
    return {
        "/event-status/": {"status": [{"event": 7, "bonus_added": bonus_added}]},
        "/fixtures/": lambda p: [{"event": p["event"], "finished": finished, "finished_provisional": finished}],
        "/bootstrap-static/": BOOT,
        "/leagues-classic/467763/standings/": standings_pages,
    }


# ---------- HTTP contract (unchanged from v2.0) ----------

def test_health(tmp_path):
    c, _ = make(tmp_path)
    r = c.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "league_id": 467763}


def test_league_combines_pages(tmp_path):
    c, _ = make(tmp_path, {"/leagues-classic/467763/standings/": standings_pages})
    body = c.get("/league/467763").json()
    assert [x["entry"] for x in body["standings"]["results"]] == [1, 2]


def test_league_upstream_404_is_404(tmp_path):
    c, _ = make(tmp_path, {"/leagues-classic/1/standings/": 404})
    assert c.get("/league/1").status_code == 404


def test_upstream_outage_is_502_after_one_retry(tmp_path):
    c, fake = make(tmp_path, {"/bootstrap-static/": 503})
    assert c.get("/bootstrap-static").status_code == 502
    assert len(fake.calls) == 2


def test_upstream_other_4xx_is_502_without_retry(tmp_path):
    c, fake = make(tmp_path, {"/bootstrap-static/": 403})
    assert c.get("/bootstrap-static").status_code == 502
    assert len(fake.calls) == 1


def test_snapshot_then_history_roundtrip(tmp_path):
    c, fake = make(tmp_path, gw_routes())
    r = c.post("/autosnapshot/467763").json()
    assert r == {"ok": True, "snapshotted": True, "already_existed": False, "gw": 7}
    assert ("/fixtures/", {"event": 7}) in fake.calls            # filtered upstream, not client-side

    again = c.post("/autosnapshot/467763").json()
    assert again["snapshotted"] is False and again["already_existed"] is True

    hist = c.get("/history/467763").json()
    assert hist["league_id"] == 467763 and [s["gw"] for s in hist["snapshots"]] == [7]
    one = c.get("/history/467763/7")
    assert one.headers["content-type"] == "application/json"
    body = one.json()
    assert body["gw"] == 7 and body["league_id"] == 467763 and body["taken_at"]
    assert body["data"]["league"]["name"] == "The Lobster League"
    assert [x["entry"] for x in body["data"]["standings"]["results"]] == [1, 2]


def test_snapshot_skipped_while_gw_live(tmp_path):
    c, _ = make(tmp_path, gw_routes(bonus_added=False, finished=False))
    r = c.post("/autosnapshot/467763").json()
    assert r == {"ok": False, "gw": 7, "bonus_added": False, "fixtures_all_finished": False,
                 "gw_finished_flag": True, "reason": "GW not fully over yet"}


def test_snapshot_unknown_gw_is_503(tmp_path):
    c, _ = make(tmp_path, {"/event-status/": {"status": []}})
    assert c.post("/autosnapshot/467763").status_code == 503


def test_snapshot_secret_enforced(tmp_path):
    c, _ = make(tmp_path, gw_routes(), SNAPSHOT_SECRET="s3cret")
    assert c.post("/autosnapshot/467763").status_code == 401
    assert c.post("/autosnapshot/467763", headers={"X-Snapshot-Secret": "s3cret"}).status_code == 200


def test_history_missing_gw_is_404_not_500(tmp_path):
    c, _ = make(tmp_path)
    assert c.get("/history/467763/99").status_code == 404


def test_history_empty_league(tmp_path):
    c, _ = make(tmp_path)
    assert c.get("/history/123").json() == {"league_id": 123, "snapshots": []}


def test_legacy_snapshots_table_migrated(tmp_path):
    con = sqlite3.connect(tmp_path / "test.db")
    con.execute("CREATE TABLE snapshots (league_id INTEGER, gw INTEGER, data TEXT, PRIMARY KEY (league_id, gw))")
    con.execute("INSERT INTO snapshots VALUES (5, 3, ?)", (json.dumps({"x": 1}),))
    con.commit()
    con.close()
    c, _ = make(tmp_path)
    assert c.get("/history/5/3").json()["data"] == {"x": 1}


def test_team_grouped_by_position(tmp_path):
    c, _ = make(tmp_path, {
        "/bootstrap-static/": BOOT,
        "/entry/42/event/7/picks/": {"picks": [
            {"element": 1, "position": 1, "multiplier": 2, "is_captain": True, "is_vice_captain": False},
            {"element": 2, "position": 12, "multiplier": 0, "is_captain": False, "is_vice_captain": True},
            {"element": 999, "position": 2, "multiplier": 1},          # unknown element is skipped
        ]},
    })
    body = c.get("/team/42").json()
    assert body["gw"] == 7
    assert body["team"]["FWD"][0]["web_name"] == "Haaland" and body["team"]["FWD"][0]["is_captain"]
    assert body["team"]["FWD"][0]["name"] == "Erling Haaland" and body["team"]["FWD"][0]["team"] == "MCI"
    assert body["team"]["GK"][0]["position"] == 12
    assert body["team"]["DEF"] == [] and body["team"]["MID"] == []


def test_team_unknown_entry_404(tmp_path):
    c, _ = make(tmp_path, {"/bootstrap-static/": BOOT, "/entry/1/event/7/picks/": 404})
    assert c.get("/team/1").status_code == 404


def test_team_preseason_503(tmp_path):
    c, _ = make(tmp_path, {"/bootstrap-static/": {**BOOT, "events": [{"id": 1, "finished": False}]}})
    assert c.get("/team/1").status_code == 503


def test_tenure(tmp_path):
    c, _ = make(tmp_path, {"/entry/42/history/": {"past": [{"season_name": "2019/20"}, {"season_name": "2017/18"}]}})
    body = c.get("/tenure/42").json()
    assert body["seasons_played"] == 2 and body["first_season"] == "2017/18" and body["playing_since_year"] == 2017


def test_bonus_filters_zero(tmp_path):
    c, _ = make(tmp_path, {"/event/7/live/": {"elements": [
        {"id": 1, "stats": {"bonus": 3}}, {"id": 2, "stats": {"bonus": 0}}]}})
    assert c.get("/bonus/7").json() == {"gameweek": 7, "bonuses": [{"player_id": 1, "bonus": 3}]}


def test_fixtures_proxy_passes_event(tmp_path):
    c, fake = make(tmp_path, {"/fixtures/": lambda p: [{"event": (p or {}).get("event")}]})
    assert c.get("/fixtures?event=3").json() == [{"event": 3}]
    assert c.get("/fixtures").json() == [{"event": None}]
    assert fake.calls == [("/fixtures/", {"event": 3}), ("/fixtures/", None)]


def test_cors_wildcard_without_credentials(tmp_path):
    c, _ = make(tmp_path)
    r = c.get("/health", headers={"Origin": "https://example.com"})
    assert r.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in r.headers


def test_cors_allowlist(tmp_path):
    c, _ = make(tmp_path, ALLOWED_ORIGINS="https://lobster.vercel.app")
    ok = c.get("/health", headers={"Origin": "https://lobster.vercel.app"})
    bad = c.get("/health", headers={"Origin": "https://evil.example"})
    assert ok.headers.get("access-control-allow-origin") == "https://lobster.vercel.app"
    assert "access-control-allow-origin" not in bad.headers


# ---------- Performance behaviour ----------

def test_bootstrap_proxy_serves_upstream_bytes_gzipped(tmp_path):
    big = {**BOOT, "pad": "x" * 5000}
    c, _ = make(tmp_path, {"/bootstrap-static/": big})
    r = c.get("/bootstrap-static", headers={"accept-encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    assert r.json() == big


def test_responses_are_cached_within_ttl(tmp_path):
    c, fake = make(tmp_path, {"/bootstrap-static/": BOOT, "/entry/42/event/7/picks/": {"picks": []}})
    for _ in range(3):
        assert c.get("/team/42").status_code == 200
    assert [p for p, _ in fake.calls] == ["/bootstrap-static/", "/entry/42/event/7/picks/"]


def test_autosnapshot_bypasses_cache(tmp_path):
    routes = gw_routes(bonus_added=False, finished=False)
    c, fake = make(tmp_path, routes)
    c.get("/bootstrap-static")                                    # warm the cache
    assert c.post("/autosnapshot/467763").json()["ok"] is False
    routes.update(gw_routes())                                     # GW ends a moment later
    assert c.post("/autosnapshot/467763").json()["ok"] is True
    assert sum(1 for p, _ in fake.calls if p == "/bootstrap-static/") == 3


def test_failures_are_not_cached(tmp_path):
    routes = {"/bootstrap-static/": 503}
    c, _ = make(tmp_path, routes)
    assert c.get("/bootstrap-static").status_code == 502
    routes["/bootstrap-static/"] = BOOT
    assert c.get("/bootstrap-static").status_code == 200


def test_combined_pages_report_no_further_pages_once_everything_was_read():
    """A merged table that still said has_next=True would make every consumer think it was incomplete."""
    gw = FakeFpl({"/leagues-classic/1/standings/": standings_pages}).gateway()
    assert asyncio.run(gw.standings(1))["standings"]["has_next"] is False


def test_combined_pages_still_say_more_when_the_page_cap_cut_the_read_short():
    never_ends = lambda params: {"league": {"id": 1}, "standings": {"has_next": True, "results": [{"entry": params["page_standings"]}]}}
    merged = asyncio.run(FakeFpl({"/leagues-classic/1/standings/": never_ends}).gateway().standings(1))
    assert merged["standings"]["has_next"] is True and len(merged["standings"]["results"]) > 1


def test_standings_cache_is_not_mutated_by_combining_pages():
    fake = FakeFpl({"/leagues-classic/1/standings/": standings_pages})
    gw = fake.gateway()

    async def run():
        a = await gw.standings(1)
        b = await gw.standings(1)
        return a, b
    a, b = asyncio.run(run())
    assert [x["entry"] for x in b["standings"]["results"]] == [1, 2]
    assert a == b and len(fake.calls) == 2                         # two pages, fetched once


def test_concurrent_requests_share_one_upstream_call():
    fake = FakeFpl({"/bootstrap-static/": BOOT})
    gw = fake.gateway()

    async def run():
        return await asyncio.gather(*(gw.bootstrap() for _ in range(10)))
    results = asyncio.run(run())
    assert len(fake.calls) == 1 and all(r is results[0] for r in results)


def test_cache_retries_for_waiters_when_owner_is_cancelled():
    cache = AsyncTTLCache()
    calls = 0

    async def slow():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05 if calls == 1 else 0)
        return calls

    async def run():
        owner = asyncio.create_task(cache.get_or_load("k", 60, slow))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(cache.get_or_load("k", 60, slow))
        await asyncio.sleep(0)
        owner.cancel()
        return await waiter
    assert asyncio.run(run()) == 2


def test_cache_expires_and_evicts():
    now = [0.0]
    cache = AsyncTTLCache(max_entries=2, clock=lambda: now[0])

    async def run():
        n = 0

        async def load():
            nonlocal n
            n += 1
            return n
        assert await cache.get_or_load("a", 10, load) == 1
        assert await cache.get_or_load("a", 10, load) == 1
        now[0] = 11
        assert await cache.get_or_load("a", 10, load) == 2       # expired
        await cache.get_or_load("b", 10, load)
        await cache.get_or_load("c", 10, load)                    # evicts "a" (LRU)
        assert await cache.get_or_load("a", 10, load) == 5
    asyncio.run(run())


# ---------- Domain ----------

def test_gameweek_completion_requires_all_three_signals():
    events = [{"id": 7, "finished": True}]
    done = [{"finished": True, "finished_provisional": True}]
    assert domain.gameweek_completion(7, True, done, events).is_over
    assert not domain.gameweek_completion(7, False, done, events).is_over
    assert not domain.gameweek_completion(7, True, [], events).is_over
    assert not domain.gameweek_completion(7, True, done, [{"id": 7, "finished": False}]).is_over


def test_current_gw_falls_back_to_latest_finished():
    assert domain.current_gw([{"id": 1, "finished": True}, {"id": 2, "finished": True}]) == 2
    assert domain.current_gw([{"id": 1, "finished": False}]) is None


# ---------- Entry point ----------

@pytest.mark.parametrize("cwd,target", [(ROOT, "app.main:app"), (ROOT / "app", "main:app")])
def test_entrypoint_imports_from_either_working_directory(tmp_path, cwd, target):
    module, attr = target.split(":")
    code = f"import importlib; m = importlib.import_module('{module}'); print(type(getattr(m, '{attr}')).__name__)"
    env = {**os.environ, "DB_PATH": str(tmp_path / "e.db"), "PYTHONPATH": ""}
    out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, ''); {code}"],
                         cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "FastAPI"


def test_upstream_is_exported_for_callers():
    assert Upstream(404, "x").status == 404
