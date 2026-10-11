"""PUT /history/{league}/{gw}: the one endpoint that accepts caller-supplied data, so the tests are mostly about refusing."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import backfill  # noqa: E402
from tests.test_main import make  # noqa: E402

SECRET = "s3cret"
LEAGUE = 467763


def events(finished=(1, 2), unchecked=(3,)):
    return [{"id": i, "finished": True, "data_checked": True} for i in finished] + \
           [{"id": i, "finished": True, "data_checked": False} for i in unchecked] + [{"id": 4, "finished": False, "data_checked": False}]


def gw(event, points, total, overall):
    return {"event": event, "points": points, "total_points": total, "event_transfers_cost": 0, "overall_rank": overall}


def good_table(g=2):
    live = {"league": {"id": LEAGUE, "name": "The Lobster League"}, "standings": {"has_next": False, "results": [
        {"entry": 1, "entry_name": "A", "player_name": "a", "club_badge_src": None},
        {"entry": 2, "entry_name": "B", "player_name": "b", "club_badge_src": None}]}}
    hist = {1: {"current": [gw(1, 50, 50, 20), gw(2, 40, 90, 10)]}, 2: {"current": [gw(1, 60, 60, 10), gw(2, 20, 80, 20)]}}
    return backfill.reconstruct_table(g, live, hist, built_at="2026-10-11T12:00:00+00:00")


def client(tmp_path, secret=SECRET, ev=None):
    env = {"SNAPSHOT_SECRET": secret} if secret else {}
    return make(tmp_path, {"/bootstrap-static/": {"events": ev or events(), "elements": [], "teams": []}}, **env)


def put(c, body=None, *, gw_=2, secret=SECRET, raw=None):
    headers = {"x-snapshot-secret": secret} if secret is not None else {}
    return c.put(f"/history/{LEAGUE}/{gw_}", content=raw if raw is not None else json.dumps(body if body is not None else good_table(gw_)), headers=headers)


def test_stores_a_valid_finished_table_and_serves_it_back(tmp_path):
    c, _ = client(tmp_path)
    r = put(c)
    assert r.status_code == 201 and r.json() == {"ok": True, "stored": True, "league_id": LEAGUE, "gw": 2}
    assert [s["gw"] for s in c.get(f"/history/{LEAGUE}").json()["snapshots"]] == [2]
    stored = c.get(f"/history/{LEAGUE}/2").json()
    assert stored["data"]["reconstructed"]["gw"] == 2
    assert [x["entry"] for x in stored["data"]["standings"]["results"]] == [1, 2]


def test_the_list_says_which_snapshots_were_rebuilt_and_which_were_saved_at_the_time(tmp_path):
    from app.storage import SnapshotRepository
    c, _ = client(tmp_path)
    assert put(c, gw_=2).status_code == 201
    SnapshotRepository(str(tmp_path / "test.db")).save(LEAGUE, 3, {"league": {"id": LEAGUE}, "standings": {"results": []}})   # saved at the time, as /autosnapshot does
    items = {s["gw"]: s for s in c.get(f"/history/{LEAGUE}").json()["snapshots"]}
    assert items[2]["rebuilt"] is True and items[3]["rebuilt"] is False


def test_is_disabled_unless_a_secret_is_configured(tmp_path):
    c, _ = client(tmp_path, secret=None)
    for presented in (None, "", "anything"):
        r = put(c, secret=presented)
        assert r.status_code == 403 and "disabled" in r.json()["detail"]
    assert c.get(f"/history/{LEAGUE}").json()["snapshots"] == []


@pytest.mark.parametrize("presented", [None, "", "wrong", SECRET + "x", SECRET[:-1]])
def test_rejects_a_missing_or_wrong_secret(tmp_path, presented):
    c, _ = client(tmp_path)
    assert put(c, secret=presented).status_code == 401
    assert c.get(f"/history/{LEAGUE}").json()["snapshots"] == []


def test_never_overwrites_an_existing_snapshot(tmp_path):
    c, _ = client(tmp_path)
    assert put(c).status_code == 201
    taken = c.get(f"/history/{LEAGUE}").json()["snapshots"][0]["taken_at"]
    tampered = good_table(2)
    tampered["standings"]["results"][0]["total"] = 999
    assert put(c, tampered).status_code == 409
    assert c.get(f"/history/{LEAGUE}/2").json()["data"]["standings"]["results"][0]["total"] == 90
    assert c.get(f"/history/{LEAGUE}").json()["snapshots"][0]["taken_at"] == taken


@pytest.mark.parametrize("target", [3, 4, 5, 38])
def test_only_finished_and_verified_gameweeks(tmp_path, target):
    c, _ = client(tmp_path)                           # 1 and 2 are verified; 3 finished but unchecked; 4 not over
    r = put(c, good_table(2) | {"reconstructed": good_table(2)["reconstructed"] | {"gw": target}}, gw_=target)
    assert r.status_code == 422 and "not finished" in r.json()["detail"]


@pytest.mark.parametrize("target", [0, -1, 39, 1000])
def test_gameweek_must_be_in_range(tmp_path, target):
    c, _ = client(tmp_path)
    assert put(c, good_table(2), gw_=target).status_code == 422   # a valid payload, sent to an impossible gameweek


def test_rejects_malformed_tables_with_reasons(tmp_path):
    c, _ = client(tmp_path)
    bad = good_table(2)
    bad["standings"]["results"][0]["total"] = "ninety"
    r = put(c, bad)
    assert r.status_code == 422 and any("total must be an integer" in p for p in r.json()["detail"]["problems"])
    wrong_league = good_table(2)
    wrong_league["league"]["id"] = 1
    assert put(c, wrong_league).status_code == 422
    not_rebuilt = good_table(2)
    del not_rebuilt["reconstructed"]
    assert put(c, not_rebuilt).status_code == 422       # only tables marked as rebuilt can be imported this way
    assert c.get(f"/history/{LEAGUE}").json()["snapshots"] == []


@pytest.mark.parametrize("raw", ["not json", "", "[1,2", "{'single': 'quotes'}"])
def test_rejects_bodies_that_are_not_json(tmp_path, raw):
    c, _ = client(tmp_path)
    assert put(c, raw=raw).status_code == 400


def test_rejects_an_oversized_body_before_parsing_it(tmp_path):
    c, _ = client(tmp_path)
    assert put(c, raw=json.dumps({"pad": "x" * (300 * 1024)})).status_code == 413


def test_a_streamed_body_is_cut_off_at_the_limit_without_a_content_length(tmp_path):
    """No Content-Length (chunked): the limit must still apply while reading, not after buffering everything."""
    c, _ = client(tmp_path)
    def chunks():
        for _ in range(10_000):
            yield b"x" * 1024                 # would be ~10 MB if it were read to the end
    r = c.put(f"/history/{LEAGUE}/2", content=chunks(), headers={"x-snapshot-secret": SECRET})
    assert r.status_code == 413


def test_a_declared_oversize_is_refused_without_reading(tmp_path):
    c, _ = client(tmp_path)
    r = c.put(f"/history/{LEAGUE}/2", content=b"{}", headers={"x-snapshot-secret": SECRET, "content-length": str(10 * 1024 * 1024)})
    assert r.status_code == 413


def test_absurdly_nested_json_is_a_400_not_a_crash(tmp_path):
    c, _ = client(tmp_path)
    assert put(c, raw="[" * 100_000 + "]" * 100_000).status_code == 400


def test_a_table_the_server_can_see_is_inconsistent_is_refused(tmp_path):
    c, _ = client(tmp_path)
    bad = good_table(2)
    bad["standings"]["results"][0]["last_rank"] = -5
    assert put(c, bad).status_code == 422
    bad = good_table(2)
    bad["standings"]["results"][0]["club_badge_src"] = "javascript:alert(1)"
    assert put(c, bad).status_code == 422
    assert c.get(f"/history/{LEAGUE}").json()["snapshots"] == []


def test_failing_closed_when_fpl_cannot_confirm_the_gameweek(tmp_path):
    c, _ = make(tmp_path, {"/bootstrap-static/": 503}, SNAPSHOT_SECRET=SECRET)
    assert put(c).status_code == 502
    assert c.get(f"/history/{LEAGUE}").json()["snapshots"] == []


def test_a_browser_cannot_call_it_cross_origin(tmp_path):
    c, _ = client(tmp_path)
    r = c.options(f"/history/{LEAGUE}/2", headers={"origin": "https://evil.example", "access-control-request-method": "PUT"})
    assert "PUT" not in r.headers.get("access-control-allow-methods", "")


def test_autosnapshot_and_reads_are_unchanged(tmp_path):
    c, _ = client(tmp_path)
    assert c.get(f"/history/{LEAGUE}/9").status_code == 404
    assert c.get("/health").json()["status"] == "ok"
