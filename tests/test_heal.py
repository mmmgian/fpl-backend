"""Self-healing history: a server whose disk was wiped rebuilds the tables it is missing, and only those.

The rebuild itself is covered in test_backfill.py; these tests are about WHEN it runs and what it must never do:
overwrite a stored table, touch the current gameweek, hammer FPL, or serve a half-finished list as a complete one.
"""
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import backfill  # noqa: E402
from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.fpl import Upstream  # noqa: E402
from app.services import HistoryKeeper  # noqa: E402
from app.storage import SnapshotRepository  # noqa: E402
from tests.test_main import FakeFpl  # noqa: E402

LEAGUE = 467763
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
BUILT_AT = NOW.isoformat(timespec="seconds")


def row(event, points, total, overall):
    return {"event": event, "points": points, "total_points": total, "event_transfers_cost": 0, "overall_rank": overall}


# Three managers, four gameweeks. GW1-2 are over and verified, GW3 is over and verified but still the current one,
# GW4 hasn't been played.
MEMBERS = {1: ("Alpha", "a"), 2: ("Bravo", "b"), 3: ("Charlie", "c")}
HISTORIES = {
    1: {"current": [row(1, 50, 50, 30), row(2, 40, 90, 10), row(3, 30, 120, 12)]},
    2: {"current": [row(1, 60, 60, 10), row(2, 20, 80, 20), row(3, 50, 130, 8)]},
    3: {"current": [row(1, 40, 40, 40), row(2, 55, 95, 5), row(3, 20, 115, 15)]},
}
EVENTS = [
    {"id": 1, "finished": True, "data_checked": True, "is_current": False},
    {"id": 2, "finished": True, "data_checked": True, "is_current": False},
    {"id": 3, "finished": True, "data_checked": True, "is_current": True},
    {"id": 4, "finished": False, "data_checked": False, "is_current": False},
]
LIVE = {
    "league": {"id": LEAGUE, "name": "The Lobster League"},
    "standings": {"has_next": False, "page": 1, "results": [
        {"entry": e, "entry_name": n, "player_name": p, "club_badge_src": None} for e, (n, p) in MEMBERS.items()]},
}


def expected(gw):
    return backfill.reconstruct_table(gw, LIVE, HISTORIES, built_at=BUILT_AT)


class StubFpl:
    """Just the three calls the keeper makes, counted, with optional delay / failure."""

    def __init__(self, events=EVENTS, histories=HISTORIES, delay=0.0):
        self.events, self.histories, self.delay = events, histories, delay
        self.fail_standings: Exception | None = None
        self.calls: list[str] = []

    async def bootstrap(self, *, fresh=False):
        self.calls.append("bootstrap")
        return SimpleNamespace(events=self.events)

    async def standings(self, league_id, *, fresh=False):
        self.calls.append("standings")
        if self.fail_standings:
            raise self.fail_standings
        return LIVE

    async def entry_history(self, entry):
        self.calls.append(f"history:{entry}")
        await asyncio.sleep(self.delay)
        return self.histories[entry]

    def count(self, prefix):
        return sum(c.startswith(prefix) for c in self.calls)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def keeper(tmp_path, fpl=None, clock=None, **kw):
    repo = SnapshotRepository(tmp_path / "t.db")
    repo.init()
    fpl = fpl or StubFpl()
    return HistoryKeeper(fpl, repo, LEAGUE, clock=clock or Clock(), now=lambda: NOW, **kw), fpl, repo


def stored_gws(repo):
    return [s["gw"] for s in repo.list(LEAGUE)]


# ---------------------------------------------------------------- what it rebuilds

def test_an_empty_store_gets_every_finished_gameweek_except_the_current_one(tmp_path):
    k, _, repo = keeper(tmp_path)
    assert asyncio.run(k.settle(LEAGUE)) == set()
    assert stored_gws(repo) == [1, 2]                         # GW3 is current: left for /autosnapshot; GW4 isn't played
    for gw in (1, 2):
        assert json.loads(repo.get(LEAGUE, gw).data_json) == expected(gw)      # exactly the validated rebuild
    assert all(s["rebuilt"] for s in repo.list(LEAGUE))       # and flagged as rebuilt, never passed off as saved at the time


def test_the_current_gameweek_is_left_for_autosnapshot_so_the_real_table_can_still_be_saved(tmp_path):
    k, _, repo = keeper(tmp_path)
    asyncio.run(k.settle(LEAGUE))
    assert 3 not in stored_gws(repo)
    assert repo.save(LEAGUE, 3, {"real": "fpl table"}) is True      # /autosnapshot can still store it


def test_a_gameweek_that_is_no_longer_current_is_rebuilt_on_the_next_check(tmp_path):
    clock = Clock()
    k, fpl, repo = keeper(tmp_path, clock=clock)
    asyncio.run(k.settle(LEAGUE))
    assert stored_gws(repo) == [1, 2]
    fpl.events = [{**e, "is_current": e["id"] == 4, **({"finished": True, "data_checked": True} if e["id"] == 4 else {})} for e in EVENTS]
    fpl.events[3]["finished"] = fpl.events[3]["data_checked"] = False   # GW4 is current but not over
    clock.t += 301                                            # past the re-check window
    assert asyncio.run(k.settle(LEAGUE)) == set()
    assert stored_gws(repo) == [1, 2, 3]


def test_it_never_overwrites_a_stored_table(tmp_path):
    k, _, repo = keeper(tmp_path)
    repo.save(LEAGUE, 1, {"saved": "at the time, from FPL"})
    asyncio.run(k.settle(LEAGUE))
    assert json.loads(repo.get(LEAGUE, 1).data_json) == {"saved": "at the time, from FPL"}
    assert stored_gws(repo) == [1, 2]
    assert asyncio.run(k.settle(LEAGUE)) == set()


def test_only_the_configured_league_ever_causes_a_fetch(tmp_path):
    k, fpl, repo = keeper(tmp_path)
    assert asyncio.run(k.settle(999)) == set()
    assert fpl.calls == [] and stored_gws(repo) == []


# ---------------------------------------------------------------- what it costs

def test_a_complete_history_costs_nothing_but_a_cached_check(tmp_path):
    clock = Clock()
    k, fpl, _ = keeper(tmp_path, clock=clock)
    asyncio.run(k.settle(LEAGUE))
    fpl.calls.clear()
    for _ in range(5):
        assert asyncio.run(k.settle(LEAGUE)) == set()
    assert fpl.calls == []                                    # inside the window: not even a bootstrap
    clock.t += 301
    asyncio.run(k.settle(LEAGUE))
    assert fpl.calls == ["bootstrap"]                         # after it: one look, and nothing is missing so nothing else


def test_many_simultaneous_requests_share_one_rebuild(tmp_path):
    k, fpl, repo = keeper(tmp_path, fpl=StubFpl(delay=0.05))

    async def go():
        return await asyncio.gather(*(k.settle(LEAGUE) for _ in range(8)))

    assert asyncio.run(go()) == [set()] * 8
    assert fpl.count("history:") == len(MEMBERS) and fpl.count("standings") == 1
    assert stored_gws(repo) == [1, 2]


def test_a_slow_rebuild_is_waited_for_briefly_then_reported_as_missing_and_carries_on(tmp_path):
    k, _, repo = keeper(tmp_path, fpl=StubFpl(delay=0.2))

    async def go():
        first = await k.settle(LEAGUE, wait=0.01)             # a reader that can't wait
        assert stored_gws(repo) == []
        second = await k.settle(LEAGUE, wait=5)               # the same rebuild, still running, now awaited
        return first, second

    first, second = asyncio.run(go())
    assert first == {1, 2} and second == set() and stored_gws(repo) == [1, 2]


# ---------------------------------------------------------------- when FPL is unwell

def test_when_fpl_fails_it_reports_the_gaps_leaves_fpl_alone_for_the_cooldown_then_recovers(tmp_path, caplog):
    clock = Clock()
    k, fpl, repo = keeper(tmp_path, clock=clock, cooldown=120)
    fpl.fail_standings = Upstream(503, "FPL responded 503")
    with caplog.at_level(logging.WARNING, logger="app.history"):
        assert asyncio.run(k.settle(LEAGUE)) == {1, 2}        # known to be missing, can't supply them now
    assert "history rebuild failed" in caplog.text
    assert fpl.count("standings") == 1 and stored_gws(repo) == []

    clock.t += 60
    assert asyncio.run(k.settle(LEAGUE)) == {1, 2}
    assert fpl.count("standings") == 1                        # still cooling down: no new call

    clock.t += 61
    fpl.fail_standings = None
    assert asyncio.run(k.settle(LEAGUE)) == set()
    assert stored_gws(repo) == [1, 2]


def test_a_rebuild_that_hangs_is_given_up_on_and_retried_later(tmp_path):
    clock = Clock()
    k, fpl, repo = keeper(tmp_path, fpl=StubFpl(delay=5), clock=clock, timeout=0.05, cooldown=60)
    assert asyncio.run(k.settle(LEAGUE, wait=2)) == {1, 2}
    assert stored_gws(repo) == []
    clock.t += 61
    fpl.delay = 0
    assert asyncio.run(k.settle(LEAGUE)) == set() and stored_gws(repo) == [1, 2]


def test_if_fpl_cannot_even_say_which_gameweeks_are_over_nothing_is_claimed_missing(tmp_path):
    k, fpl, _ = keeper(tmp_path)

    async def boom(*, fresh=False):
        raise Upstream(502, "down")

    fpl.bootstrap = boom
    assert asyncio.run(k.settle(LEAGUE)) == set()             # serve whatever is stored; don't call it incomplete


def test_a_gameweek_that_cannot_be_rebuilt_consistently_is_skipped_not_retried_forever_and_does_not_block_the_rest(tmp_path, caplog):
    hist = {e: {"current": [r for r in h["current"] if r["event"] != 2]} for e, h in HISTORIES.items()}   # nobody has GW2
    clock = Clock()
    k, fpl, repo = keeper(tmp_path, fpl=StubFpl(histories=hist), clock=clock)
    with caplog.at_level(logging.WARNING, logger="app.history"):
        assert asyncio.run(k.settle(LEAGUE)) == set()         # not "missing": it can't be supplied, so it isn't promised
    assert stored_gws(repo) == [1] and "GW2 cannot be rebuilt" in caplog.text
    fpl.calls.clear()
    clock.t += 301
    assert asyncio.run(k.settle(LEAGUE)) == set()
    assert fpl.count("standings") == 0                        # not retried: it would fail the same way


def test_stop_cancels_a_rebuild_in_flight(tmp_path):
    k, _, repo = keeper(tmp_path, fpl=StubFpl(delay=5))

    async def go():
        k.kick()
        await asyncio.sleep(0.02)
        await k.stop()

    asyncio.run(go())
    assert stored_gws(repo) == []


# ---------------------------------------------------------------- through the API

def routes(histories=HISTORIES, history_status=None):
    r = {
        "/bootstrap-static/": {"events": EVENTS, "elements": [], "teams": []},
        f"/leagues-classic/{LEAGUE}/standings/": LIVE,
    }
    for e in MEMBERS:
        r[f"/entry/{e}/history/"] = history_status if history_status else histories[e]
    return r


def app_client(tmp_path, fake, *, heal=True, **kw):
    settings = Settings.from_env({"DB_PATH": str(tmp_path / "api.db"), "SELF_HEAL_HISTORY": "1" if heal else "0"})
    repo = SnapshotRepository(settings.db_path)
    gateway = fake.gateway()
    k = HistoryKeeper(gateway, repo, LEAGUE, now=lambda: NOW, **kw)
    return TestClient(create_app(settings, fpl=gateway, snapshots=repo, keeper=k)), repo


def test_a_restart_that_wiped_the_disk_comes_back_with_its_history(tmp_path):
    fake = FakeFpl(routes())
    c, repo = app_client(tmp_path, fake)
    with c:                                                   # startup begins the rebuild before anyone asks
        r = c.get(f"/history/{LEAGUE}")
        assert r.status_code == 200
        assert [(s["gw"], s["rebuilt"]) for s in r.json()["snapshots"]] == [(1, True), (2, True)]
        one = c.get(f"/history/{LEAGUE}/2")
        assert one.status_code == 200 and one.json()["data"] == expected(2)
        assert c.get(f"/history/{LEAGUE}/3").status_code == 404      # current: left for /autosnapshot
        assert c.get(f"/history/{LEAGUE}/4").status_code == 404


def test_startup_alone_rebuilds_the_history_before_anyone_has_asked(tmp_path):
    import time
    c, repo = app_client(tmp_path, FakeFpl(routes()))
    with c:                                                   # no request is made at all
        deadline = time.time() + 5
        while time.time() < deadline and [s["gw"] for s in repo.list(LEAGUE)] != [1, 2]:
            time.sleep(0.02)
        assert [s["gw"] for s in repo.list(LEAGUE)] == [1, 2]


def test_with_self_healing_off_startup_does_nothing(tmp_path):
    import time
    fake = FakeFpl(routes())
    c, repo = app_client(tmp_path, fake, heal=False)
    with c:
        time.sleep(0.2)
    assert repo.list(LEAGUE) == [] and fake.calls == []


def test_the_first_reader_after_a_restart_gets_a_complete_list_without_startup_help(tmp_path):
    c, _ = app_client(tmp_path, FakeFpl(routes()))
    r = c.get(f"/history/{LEAGUE}")                           # no lifespan: the read itself starts and waits for the rebuild
    assert r.status_code == 200 and [s["gw"] for s in r.json()["snapshots"]] == [1, 2]


def test_a_list_known_to_be_incomplete_is_a_503_never_a_cacheable_200(tmp_path):
    fake = FakeFpl(routes(history_status=503))
    c, repo = app_client(tmp_path, fake)
    with c:
        r = c.get(f"/history/{LEAGUE}")
        assert r.status_code == 503 and r.headers["retry-after"] == "30" and "GW 1, 2" in r.json()["detail"]
        assert c.get(f"/history/{LEAGUE}/1").status_code == 503      # asking for a gameweek that is on its way
        assert c.get(f"/history/{LEAGUE}/4").status_code == 404      # one that never will be: an honest 404


def test_a_server_that_already_has_everything_just_answers(tmp_path):
    fake = FakeFpl(routes())
    c, repo = app_client(tmp_path, fake)
    repo.init()
    for gw in (1, 2):
        repo.save(LEAGUE, gw, expected(gw))
    r = c.get(f"/history/{LEAGUE}")
    assert r.status_code == 200 and len(r.json()["snapshots"]) == 2
    assert not any(p.startswith("/entry/") or "/leagues-classic/" in p for p, _ in fake.calls)


def test_another_leagues_history_is_not_ours_to_rebuild(tmp_path):
    fake = FakeFpl(routes())
    c, _ = app_client(tmp_path, fake)
    r = c.get("/history/12345")
    assert r.status_code == 200 and r.json() == {"league_id": 12345, "snapshots": []}
    assert fake.calls == []


def test_switching_it_off_restores_the_plain_behaviour(tmp_path):
    fake = FakeFpl(routes())
    c, _ = app_client(tmp_path, fake, heal=False)
    with c:
        assert c.get(f"/history/{LEAGUE}").json()["snapshots"] == []
    assert fake.calls == []


def test_it_is_on_by_default_and_the_switch_reads_the_environment():
    assert Settings().self_heal is True and Settings.from_env({}).self_heal is True
    for off in ("0", "false", "No", "OFF", " 0 "):
        assert Settings.from_env({"SELF_HEAL_HISTORY": off}).self_heal is False, off
    for on in ("1", "true", "yes", ""):
        assert Settings.from_env({"SELF_HEAL_HISTORY": on}).self_heal is True, on


def test_the_pure_rule_for_what_is_worth_rebuilding():
    ev = [{"id": 1, "finished": True, "data_checked": True}, {"id": 2, "finished": True, "data_checked": False},
          {"id": 3, "finished": True, "data_checked": True, "is_current": True}, {"id": 4, "finished": True, "data_checked": True},
          {"id": 5, "finished": False, "data_checked": False}]
    assert backfill.healable_gameweeks(ev, set()) == [1, 4]       # GW2 not verified, GW3 current, GW5 not over
    assert backfill.healable_gameweeks(ev, {1}) == [4]
    assert backfill.healable_gameweeks(ev, {1, 4}) == []
    assert backfill.healable_gameweeks([], set()) == []
