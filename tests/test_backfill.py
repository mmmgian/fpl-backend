"""Rebuilding past league tables from season histories (app/backfill.py). Pure logic, synthetic data."""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import backfill  # noqa: E402
from app.backfill import BackfillError  # noqa: E402

BUILT = "2026-10-11T12:00:00+00:00"


def gw(event, points, total, *, cost=0, overall=None):
    return {"event": event, "points": points, "total_points": total, "event_transfers_cost": cost, "overall_rank": overall}


def league(*entries):
    return {
        "league": {"id": 7, "name": "Test League"},
        "standings": {"has_next": False, "page": 1, "results": [
            {"entry": e, "entry_name": f"Team {e}", "player_name": f"Manager {e}", "club_badge_src": None, "rank": 0, "total": 0}
            for e in entries
        ]},
    }


# 4 managers, 3 gameweeks. Entry 4 starts in GW2. Entry 2 takes a 4-point hit in GW3.
HIST = {
    1: {"current": [gw(1, 50, 50, overall=300), gw(2, 40, 90, overall=250), gw(3, 30, 120, overall=200)]},
    2: {"current": [gw(1, 60, 60, overall=100), gw(2, 30, 90, overall=260), gw(3, 36, 122, cost=4, overall=180)]},
    3: {"current": [gw(1, 40, 40, overall=900), gw(2, 50, 90, overall=240), gw(3, 20, 110, overall=400)]},
    4: {"current": [gw(2, 70, 70, overall=500), gw(3, 60, 130, overall=150)]},
}
LEAGUE = league(1, 2, 3, 4)


def table(g):
    return backfill.reconstruct_table(g, LEAGUE, HIST, built_at=BUILT)


def rows(g):
    return table(g)["standings"]["results"]


def test_first_gameweek_has_only_managers_who_had_started_and_no_previous_ranks():
    r = rows(1)
    assert [x["entry"] for x in r] == [2, 1, 3]            # entry 4 had not started
    assert [x["rank"] for x in r] == [1, 2, 3]
    assert {x["last_rank"] for x in r} == {0}               # FPL reports 0 where there is no previous table


def test_event_total_is_gross_of_hits_and_total_is_net():
    r = {x["entry"]: x for x in rows(3)}
    assert r[2]["event_total"] == 36 and r[2]["total"] == 122  # 90 + (36 - 4): the hit is in total, not in event_total
    assert 90 + r[2]["event_total"] != r[2]["total"]


def test_a_late_starter_appears_from_their_first_gameweek_with_last_rank_zero():
    r2 = {x["entry"]: x for x in rows(2)}
    assert r2[4]["last_rank"] == 0 and r2[4]["total"] == 70
    r3 = {x["entry"]: x for x in rows(3)}
    assert r3[4]["last_rank"] == 4                          # last of four at the end of GW2 (70 points)
    assert r3[4]["rank"] == 1                               # and top after GW3 (130)


def test_ties_are_broken_by_overall_rank_after_that_gameweek_not_by_entry_id_or_this_weeks_points():
    # After GW2 entries 1, 2 and 3 all have 90. Overall ranks: entry 3 (240) < entry 1 (250) < entry 2 (260).
    r = rows(2)
    tied = [x["entry"] for x in r if x["total"] == 90]
    assert tied == [3, 1, 2]
    # this week's points would put entry 3 (50) first too, so check a case that separates the two rules:
    h = copy.deepcopy(HIST)
    h[1]["current"][1]["overall_rank"] = 100                # entry 1 now has the best overall rank among the tied
    got = [x["entry"] for x in backfill.reconstruct_table(2, LEAGUE, h, built_at=BUILT)["standings"]["results"] if x["total"] == 90]
    assert got == [1, 3, 2]                                 # overall rank wins; entry 3's higher week doesn't
    assert got != sorted(got)                               # and it is not just entry-id order


class TestUnresolvedTies:
    """Level on points AND overall rank: FPL's order is not recoverable, so it must be flagged, never presented as fact."""

    H2 = {
        1: {"current": [gw(1, 56, 56, overall=777), gw(2, 10, 66, overall=50)]},
        2: {"current": [gw(1, 56, 56, overall=777), gw(2, 20, 76, overall=40)]},   # same points AND overall rank as entry 1 in GW1
        3: {"current": [gw(1, 56, 56, overall=600), gw(2, 5, 61, overall=90)]},    # same points, better overall rank: decided
    }
    L = league(1, 2, 3)

    def t(self, g):
        return backfill.reconstruct_table(g, self.L, self.H2, built_at=BUILT)

    def test_a_tie_that_overall_rank_decides_is_not_flagged_but_a_shared_overall_rank_is(self):
        t = self.t(1)
        assert [r["entry"] for r in t["standings"]["results"]] == [3, 1, 2]           # 3 is decided (better overall rank)
        assert t["reconstructed"]["unresolved_ties"] == [{"total": 56, "entries": [1, 2]}]
        assert {r["entry"]: r["rank_approximate"] for r in t["standings"]["results"]} == {3: False, 1: True, 2: True}
        assert not any(r["last_rank_approximate"] for r in t["standings"]["results"])     # the first table has no previous one

    def test_the_order_inside_an_unresolved_group_is_deterministic(self):
        assert [r["entry"] for r in self.t(1)["standings"]["results"] if r["rank_approximate"]] == [1, 2]

    def test_the_guess_is_reported_as_an_approximate_last_rank_in_the_next_table_not_as_an_approximate_rank(self):
        r = {x["entry"]: x for x in self.t(2)["standings"]["results"]}
        assert r[2]["rank"] == 1 and r[1]["rank"] == 2 and r[3]["rank"] == 3
        assert not any(x["rank_approximate"] for x in r.values())                       # GW2 itself has no unresolved tie
        assert r[1]["last_rank_approximate"] and r[2]["last_rank_approximate"]          # but their movement arrow rests on the GW1 guess
        assert not r[3]["last_rank_approximate"]
        assert self.t(2)["reconstructed"]["unresolved_ties"] == []
        assert backfill.check_table(self.t(1), self.H2) == [] and backfill.check_table(self.t(2), self.H2) == []

    def test_a_missing_overall_rank_makes_everyone_level_with_it_unresolved(self):
        """Nothing separates a manager with no overall rank from the others on the same points, so it is a guess."""
        h = {1: {"current": [gw(1, 10, 10, overall=None)]}, 2: {"current": [gw(1, 10, 10, overall=None)]}, 3: {"current": [gw(1, 10, 10, overall=5)]}}
        t = backfill.reconstruct_table(1, league(1, 2, 3), h, built_at=BUILT)
        assert t["reconstructed"]["unresolved_ties"] == [{"total": 10, "entries": [1, 2, 3]}]
        one = {1: {"current": [gw(1, 10, 10, overall=None)]}, 2: {"current": [gw(1, 10, 10, overall=5)]}, 3: {"current": [gw(1, 8, 8, overall=9)]}}
        t1 = backfill.reconstruct_table(1, league(1, 2, 3), one, built_at=BUILT)
        assert t1["reconstructed"]["unresolved_ties"] == [{"total": 10, "entries": [1, 2]}]      # the real rank is listed first, but it's still flagged
        assert [r["entry"] for r in t1["standings"]["results"]] == [2, 1, 3]
        assert backfill.check_table(t1, one) == []

    def test_a_wrong_flag_or_a_missing_group_is_caught(self):
        t = self.t(1)
        next(r for r in t["standings"]["results"] if r["rank_approximate"])["rank_approximate"] = False   # hide a real guess
        assert any("rank_approximate" in p for p in backfill.check_table(t, self.H2))
        t = self.t(1)
        next(r for r in t["standings"]["results"] if not r["rank_approximate"])["rank_approximate"] = True  # invent one
        assert any("rank_approximate" in p for p in backfill.check_table(t, self.H2))
        t = self.t(2)
        next(r for r in t["standings"]["results"] if r["last_rank_approximate"])["last_rank_approximate"] = False
        assert any("last_rank_approximate" in p for p in backfill.check_table(t, self.H2))
        t = self.t(1)
        t["reconstructed"]["unresolved_ties"] = []
        assert any("unresolved_ties" in p for p in backfill.check_table(t, self.H2))


def test_missing_overall_rank_sorts_last_then_entry_id_keeps_it_deterministic():
    h = {1: {"current": [gw(1, 10, 10, overall=None)]}, 2: {"current": [gw(1, 10, 10, overall=None)]}, 3: {"current": [gw(1, 10, 10, overall=5)]}}
    r = backfill.reconstruct_table(1, league(1, 2, 3), h, built_at=BUILT)["standings"]["results"]
    assert [x["entry"] for x in r] == [3, 1, 2]


def test_last_rank_is_the_previous_tables_rank():
    for g in (2, 3):
        prev = {x["entry"]: x["rank"] for x in rows(g - 1)}
        assert {x["entry"]: x["last_rank"] for x in rows(g)} == {e: prev.get(e, 0) for e in (x["entry"] for x in rows(g))}


def test_output_has_the_shape_the_app_already_reads_plus_provenance():
    t = table(3)
    assert set(t) == {"league", "standings", "new_entries", "reconstructed"}
    assert t["standings"]["has_next"] is False
    assert set(t["standings"]["results"][0]) == {"entry", "entry_name", "player_name", "rank", "last_rank", "rank_sort", "event_total", "total", "club_badge_src", "rank_approximate", "last_rank_approximate"}
    assert t["reconstructed"] == {"gw": 3, "method": backfill.METHOD, "tie_break": backfill.TIE_BREAK, "source": "FPL entry history", "built_at": BUILT, "unresolved_ties": []}
    assert t["league"] == {"id": 7, "name": "Test League"}


def test_it_is_deterministic_and_does_not_modify_its_inputs():
    before = copy.deepcopy((LEAGUE, HIST))
    assert table(3) == table(3)
    assert (LEAGUE, HIST) == before


def test_refuses_to_guess_when_inputs_are_incomplete():
    paged = copy.deepcopy(LEAGUE)
    paged["standings"]["has_next"] = True
    with pytest.raises(BackfillError, match="more pages"):
        backfill.reconstruct_table(1, paged, HIST, built_at=BUILT)
    with pytest.raises(BackfillError, match="No season history"):
        backfill.reconstruct_table(1, league(1, 2, 3, 99), HIST, built_at=BUILT)
    with pytest.raises(BackfillError, match="no members"):
        backfill.reconstruct_table(1, league(), HIST, built_at=BUILT)
    with pytest.raises(BackfillError, match="gameweek 9"):
        backfill.reconstruct_table(9, LEAGUE, HIST, built_at=BUILT)


def test_only_finished_and_verified_gameweeks_are_rebuilt():
    events = [
        {"id": 1, "finished": True, "data_checked": True},
        {"id": 3, "finished": True, "data_checked": True},
        {"id": 2, "finished": True, "data_checked": False},   # over, but points can still change
        {"id": 4, "finished": False, "data_checked": False},
    ]
    assert backfill.finished_gameweeks(events) == [1, 3]


class TestCheckTable:
    def test_a_correct_table_has_no_problems(self):
        for g in (1, 2, 3):
            assert backfill.check_table(table(g), HIST) == []

    @pytest.mark.parametrize("mutate,expected", [
        (lambda r: r[0].update(total=999), "total 999"),
        (lambda r: r[0].update(event_total=1), "event_total 1"),
        (lambda r: r[0].update(last_rank=9), "last_rank 9"),
        (lambda r: r[1].update(rank=5), "ranks are not 1..n"),
        (lambda r: r[1].update(entry=r[0]["entry"]), "appears twice"),
        (lambda r: r[0].update(rank_sort=7), "rank_sort differs"),
    ])
    def test_it_notices_tampering(self, mutate, expected):
        t = table(3)
        mutate(t["standings"]["results"])
        assert any(expected in p for p in backfill.check_table(t, HIST)), backfill.check_table(t, HIST)

    def test_it_notices_totals_out_of_order_and_broken_cumulative_arithmetic(self):
        t = table(3)
        t["standings"]["results"].reverse()
        for i, r in enumerate(t["standings"]["results"], 1):
            r["rank"] = r["rank_sort"] = i
        assert any("descending" in p for p in backfill.check_table(t, HIST))
        bad = copy.deepcopy(HIST)
        bad[1]["current"][1]["total_points"] = 95           # total no longer equals previous + points - hit
        assert any("total moved by" in p for p in backfill.check_table(table(2), bad))

    def test_swapping_two_managers_level_on_points_is_caught_by_the_overall_rank_rule(self):
        """HIST GW2: entries 1, 2 and 3 are all on 90 with overall ranks 250/260/240, so the order must be 3, 1, 2."""
        t = table(2)
        r = t["standings"]["results"]
        assert [x["entry"] for x in r[:3]] == [3, 1, 2]
        r[0], r[1] = r[1], r[0]
        for i, x in enumerate(r, 1):
            x["rank"] = x["rank_sort"] = i
        assert any("level on 90 points" in p for p in backfill.check_table(t, HIST)), backfill.check_table(t, HIST)

    def test_a_missing_overall_rank_cannot_sit_above_a_real_one(self):
        h = {1: {"current": [gw(1, 10, 10, overall=None)]}, 2: {"current": [gw(1, 10, 10, overall=5)]}}
        t = backfill.reconstruct_table(1, league(1, 2), h, built_at=BUILT)
        r = t["standings"]["results"]
        r[0], r[1] = r[1], r[0]
        for i, x in enumerate(r, 1):
            x["rank"] = x["rank_sort"] = i
        assert any("level on 10 points" in p for p in backfill.check_table(t, h))

    def test_dropping_or_inventing_a_manager_is_caught(self):
        t = table(3)
        dropped = t["standings"]["results"].pop()
        for i, x in enumerate(t["standings"]["results"], 1):
            x["rank"] = x["rank_sort"] = i
        assert any(f"missing [{dropped['entry']}]" in p for p in backfill.check_table(t, HIST)), backfill.check_table(t, HIST)
        t = table(1)                                       # entry 4 hadn't started in GW1; listing it is wrong
        t["standings"]["results"].append(dict(t["standings"]["results"][-1], entry=4, rank=4, rank_sort=4, total=0))
        assert any("unexpected [4]" in p for p in backfill.check_table(t, HIST))

    def test_a_table_without_a_gameweek_is_rejected(self):
        t = table(3)
        del t["reconstructed"]
        assert backfill.check_table(t, HIST) == ["table has no reconstructed.gw"]


class TestPayloadValidation:
    def good(self):
        return table(3)

    def test_accepts_a_rebuilt_table(self):
        assert backfill.validate_snapshot_payload(7, 3, self.good()) == []

    @pytest.mark.parametrize("payload", [None, [], "x", 5])
    def test_rejects_non_objects(self, payload):
        assert backfill.validate_snapshot_payload(7, 3, payload) == ["payload must be an object"]

    def test_the_league_and_gameweek_must_match_the_url(self):
        assert any("league.id" in p for p in backfill.validate_snapshot_payload(8, 3, self.good()))
        assert any("`reconstructed`" in p for p in backfill.validate_snapshot_payload(7, 4, self.good()))

    def test_only_rebuilt_tables_are_accepted(self):
        d = self.good()
        del d["reconstructed"]
        assert any("only rebuilt tables" in p for p in backfill.validate_snapshot_payload(7, 3, d))

    @pytest.mark.parametrize("edit,expected", [
        (lambda d: d["standings"].update(results=[]), "non-empty"),
        (lambda d: d["standings"].update(results=None), "non-empty"),
        (lambda d: d["standings"]["results"][0].update(total="5"), "total must be an integer"),
        (lambda d: d["standings"]["results"][0].update(total=True), "total must be an integer"),
        (lambda d: d["standings"]["results"][0].update(entry_name=""), "entry_name"),
        (lambda d: d["standings"]["results"][0].update(club_badge_src=5), "club_badge_src"),
        (lambda d: d["standings"]["results"][0].update(rank_approximate="yes"), "rank_approximate must be true or false"),
        (lambda d: d["standings"]["results"][0].update(last_rank_approximate=1), "last_rank_approximate must be true or false"),
        (lambda d: d["standings"]["results"].__setitem__(0, "nope"), "not an object"),
        (lambda d: d["standings"]["results"][1].update(rank=9), "rank must be an integer from 1 to"),
        (lambda d: d["standings"]["results"][1].update(rank=1), "ranks must be 1..n"),
        (lambda d: d["standings"]["results"][1].update(entry=d["standings"]["results"][0]["entry"]), "appears twice"),
        (lambda d: d["standings"]["results"][0].update(total=1), "descending"),
    ])
    def test_rejects_malformed_rows(self, edit, expected):
        d = self.good()
        edit(d)
        assert any(expected in p for p in backfill.validate_snapshot_payload(7, 3, d)), backfill.validate_snapshot_payload(7, 3, d)

    @pytest.mark.parametrize("edit,expected", [
        (lambda d: d["standings"]["results"][0].update(last_rank=-5), "last_rank must be an integer"),
        (lambda d: d["standings"]["results"][0].update(event_total=-1), "event_total must be an integer"),
        (lambda d: d["standings"]["results"][0].update(total=10**9), "total must be an integer"),
        (lambda d: d["standings"]["results"][0].update(entry=0), "entry must be an integer"),
        (lambda d: d["standings"]["results"][0].update(club_badge_src="javascript:alert(1)"), "https link"),
        (lambda d: d["standings"]["results"][0].update(club_badge_src="data:text/html,x"), "https link"),
        (lambda d: d["standings"]["results"][0].update(club_badge_src="https://x/" + "a" * 400), "https link"),
        (lambda d: d["standings"]["results"][0].update(entry_name="x" * 500), "entry_name"),
        (lambda d: d["standings"]["results"][0].update(surprise=1), "unexpected keys"),
        (lambda d: d.update(surprise=1), "unexpected keys"),
        (lambda d: d["reconstructed"].update(surprise=1), "unexpected keys"),
        (lambda d: d["standings"].update(has_next=True), "has_next"),
        (lambda d: d.update(new_entries={"results": [{"entry": 1}]}), "new_entries"),
        (lambda d: d["league"].update(blob={"a": [1] * 50}), "scalar"),
        (lambda d: d["standings"]["results"][0].pop("rank_approximate"), "rank_approximate must be true or false"),
        (lambda d: d["standings"]["results"][0].update(last_rank=0, last_rank_approximate=True), "no previous rank"),
    ])
    def test_a_permanent_record_refuses_odd_values_not_just_wrong_types(self, edit, expected):
        d = self.good()
        edit(d)
        assert any(expected in p for p in backfill.validate_snapshot_payload(7, 3, d)), backfill.validate_snapshot_payload(7, 3, d)

    def test_the_tie_list_and_the_row_flags_must_agree(self):
        d = table(1)                                       # GW1 has no ties in HIST, so build one to tamper with
        d["standings"]["results"][0]["rank_approximate"] = True                       # flag without a group
        assert any("do not match" in p for p in backfill.validate_snapshot_payload(7, 1, d))
        d = table(1)
        d["reconstructed"]["unresolved_ties"] = [{"total": 60, "entries": [2, 1]}]    # group without flags, and not level
        problems = backfill.validate_snapshot_payload(7, 1, d)
        assert any("not level" in p for p in problems) and any("do not match" in p for p in problems)
        for bad in ("x", [None], [{"total": 60, "entries": [2]}], [{"total": 60, "entries": [2, 99]}], [{"total": 60, "entries": [2, 2]}]):
            d = table(1)
            d["reconstructed"]["unresolved_ties"] = bad
            assert backfill.validate_snapshot_payload(7, 1, d), bad

    def test_rejects_an_oversized_table(self):
        d = self.good()
        row = d["standings"]["results"][0]
        d["standings"]["results"] = [dict(row, entry=i, rank=i, rank_sort=i, total=10_000 - i) for i in range(1, backfill.MAX_PAYLOAD_ROWS + 2)]
        assert any("more than" in p for p in backfill.validate_snapshot_payload(7, 3, d))
