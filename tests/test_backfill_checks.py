"""The three validations and the publish gate. Synthetic data; the point is that each check can FAIL."""
import copy
import re
import hashlib
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import backfill_checks as checks  # noqa: E402
import backfill_history as cli  # noqa: E402
from app import backfill  # noqa: E402

BUILT = "2026-10-11T12:00:00+00:00"


def gw(event, points, total, *, cost=0, overall=None):
    return {"event": event, "points": points, "total_points": total, "event_transfers_cost": cost, "overall_rank": overall}


LIVE = {"league": {"id": 7, "name": "L"}, "standings": {"has_next": False, "results": [
    {"entry": e, "entry_name": n, "player_name": f"m{e}", "club_badge_src": None, "rank": 0, "last_rank": 0, "event_total": 0, "total": 0}
    for e, n in ((1, "Alpha"), (2, "Beta"), (3, "Gamma"))]}}
# GW1: Alpha 60, Beta 50, Gamma 40.  GW2: Beta 70 (total 120), Alpha 40 (100), Gamma 50 (90).  Beta takes a 4-point hit in GW3.
HIST = {
    1: {"current": [gw(1, 60, 60, overall=10), gw(2, 40, 100, overall=20), gw(3, 30, 130, overall=20)]},
    2: {"current": [gw(1, 50, 50, overall=20), gw(2, 70, 120, overall=10), gw(3, 20, 136, cost=4, overall=10)]},
    3: {"current": [gw(1, 40, 40, overall=30), gw(2, 50, 90, overall=30), gw(3, 40, 130, overall=30)]},
}
TABLES = {g: backfill.reconstruct_table(g, LIVE, HIST, built_at=BUILT) for g in (1, 2)}


def live_with(rows):
    live = copy.deepcopy(LIVE)
    live["standings"]["results"] = rows
    return live


class TestGroundTruth:
    # While GW3 is live: FPL shows last_rank (rank after GW2) and a live total/event_total.
    ROWS = [
        {"entry": 2, "entry_name": "Beta", "rank": 1, "last_rank": 1, "event_total": 20, "total": 136},   # gross 20, hit 4, net total 136
        {"entry": 1, "entry_name": "Alpha", "rank": 2, "last_rank": 2, "event_total": 30, "total": 130},
        {"entry": 3, "entry_name": "Gamma", "rank": 3, "last_rank": 3, "event_total": 40, "total": 130 - 0},
    ]

    def test_passes_when_ranks_and_totals_match_including_the_hit_correction(self):
        rows = copy.deepcopy(self.ROWS)
        rows[2]["total"] = 130
        r = checks.ground_truth_check(live_with(rows), HIST, TABLES, 2, 3)
        assert r["passed"], r
        assert r["mode"] == "last_rank" and r["managers_compared"] == 3

    def test_a_wrong_rank_is_caught(self):
        rows = copy.deepcopy(self.ROWS)
        rows[0]["last_rank"] = 3
        r = checks.ground_truth_check(live_with(rows), HIST, TABLES, 2, 3)
        assert not r["passed"] and any("Beta: rank 1 != FPL's 3" in m for m in r["mismatches"])

    def test_a_wrong_total_is_caught(self):
        rows = copy.deepcopy(self.ROWS)
        rows[1]["total"] = 140
        r = checks.ground_truth_check(live_with(rows), HIST, TABLES, 2, 3)
        assert not r["passed"] and any("Alpha: total" in m for m in r["mismatches"])

    def test_direct_mode_when_the_live_table_is_the_last_finished_gameweek(self):
        rows = [{"entry": 2, "entry_name": "Beta", "rank": 1, "last_rank": 1, "event_total": 70, "total": 120},
                {"entry": 1, "entry_name": "Alpha", "rank": 2, "last_rank": 1, "event_total": 40, "total": 100},
                {"entry": 3, "entry_name": "Gamma", "rank": 3, "last_rank": 3, "event_total": 50, "total": 90}]
        r = checks.ground_truth_check(live_with(rows), HIST, TABLES, 2, 2)
        assert r["passed"] and r["mode"] == "direct"

    def test_refuses_to_compare_when_the_live_table_is_from_another_week(self):
        r = checks.ground_truth_check(live_with(self.ROWS), HIST, TABLES, 2, 5)
        assert not r["passed"] and r["mode"] == "n/a"

    def test_a_member_missing_from_the_rebuild_fails(self):
        rows = copy.deepcopy(self.ROWS) + [{"entry": 9, "entry_name": "Ghost", "rank": 4, "last_rank": 4, "event_total": 0, "total": 0}]
        assert not checks.ground_truth_check(live_with(rows), HIST, TABLES, 2, 3)["passed"]


class TestSecondApiPath:
    def picks(self, override=None):
        out = {(e, row["event"]): {"entry_history": {k: row[k] for k in ("total_points", "points", "event_transfers_cost", "overall_rank")}} for e, h in HIST.items() for row in h["current"]}
        for key, fields in (override or {}).items():
            out[key]["entry_history"].update(fields)
        return out

    def test_agreement_passes_and_counts_what_it_compared(self):
        r = checks.second_path_check(HIST, self.picks())
        assert r["passed"] and r["compared"] == 9

    @pytest.mark.parametrize("field", ["total_points", "points", "event_transfers_cost", "overall_rank"])
    def test_each_field_is_compared(self, field):
        r = checks.second_path_check(HIST, self.picks({(1, 2): {field: 999999}}))
        assert not r["passed"] and any(field in m for m in r["mismatches"])

    def test_comparing_nothing_is_a_failure_not_a_pass(self):
        r = checks.second_path_check(HIST, {})
        assert not r["passed"] and r["compared"] == 0


# ---- the printed reports -------------------------------------------------------------------------------------
TEAMS = ["Alpha", "Beta", "Gamma"]


def report(text, marker="TABLE"):
    return checks.Report([text], TEAMS, marker)


def test_parsers_read_the_formats_they_are_built_for():
    gw2 = report("TABLE\n1 ▲3 Mgr\nAlpha 111 171 6.86 1.4\n2 ▼1 Mgr\nBeta 96 (-4) 155 5.6 1.6\n")
    assert checks.parse_gw2_recap(gw2) == {"Alpha": {"event_total": 111, "total": 171}, "Beta": {"event_total": 96, "total": 155}}
    gw4 = report("TABLE\n01 Alpha ▲14 323 92 39 115 77 92 FH\n02 Beta ▼5 300 80 – – 40 80 WC\n03 Gamma = 290 70 1 2 3 70 —\n")
    p = checks.parse_gw4_times(gw4)
    assert p["Alpha"] == {"delta_vs_gw1": 14, "total": 323, "splits": {1: 39, 2: 115, 3: 77, 4: 92}}
    assert p["Beta"]["delta_vs_gw1"] == -5 and p["Beta"]["splits"][1] is None            # a dash = had not started
    assert p["Gamma"]["delta_vs_gw1"] == 0
    gw5 = report("THE TABLE\n01 ▲ Alpha 381 59 0.6%\n02 ▼ Beta 373 50 1.4 %\n")
    assert checks.parse_gw5_front(gw5) == {"Alpha": {"total": 381, "event_total": 59}, "Beta": {"total": 373, "event_total": 50}}
    gw3 = report("TABLE\n01 Alpha\nmgr (8 yrs)\n▲\n+14 77 231 3XC Haaland\n02 Beta\nmgr\n= 89 222 3XC\n")
    assert checks.parse_gw3_times(gw3) == {"Alpha": {"event_total": 77, "total": 231}, "Beta": {"event_total": 89, "total": 222}}


def test_the_report_locates_team_names_in_printed_order_without_matching_inside_longer_names():
    r = checks.Report(["TABLE\nBart's Team 5\nBart 6\nBartholomew 7"], ["Bart", "Bart's Team"], "TABLE")
    assert r.order == ["Bart's Team", "Bart"]


# ---- end to end: generate every report format from known tables, then tamper -----------------------------------
H5 = {  # five gameweeks, three managers; Gamma joins in GW2
    1: {"current": [gw(g, p, t, overall=o) for g, p, t, o in ((1, 60, 60, 10), (2, 40, 100, 10), (3, 50, 150, 10), (4, 30, 180, 10), (5, 20, 200, 10))]},
    2: {"current": [gw(g, p, t, overall=o) for g, p, t, o in ((1, 50, 50, 20), (2, 70, 120, 5), (3, 20, 140, 20), (4, 70, 210, 5), (5, 10, 220, 5))]},
    3: {"current": [gw(g, p, t, overall=o) for g, p, t, o in ((2, 30, 30, 30), (3, 50, 80, 30), (4, 40, 120, 30), (5, 30, 150, 30))]},
}
T5 = {g: backfill.reconstruct_table(g, LIVE, H5, built_at=BUILT) for g in range(1, 6)}
NAMES = {1: "Alpha", 2: "Beta", 3: "Gamma"}


def make_reports(tables, *, gw3_total_for_gamma=None, gw4_gw1_rank=None):
    """Text in the four formats the parsers read, produced from the tables themselves (so they agree by construction)."""
    rows = lambda g: tables[g]["standings"]["results"]          # noqa: E731
    gw1_rank = {r["entry_name"]: r["rank"] for r in rows(1)}
    gw4_from = gw4_gw1_rank or gw1_rank                       # the GW1 ranks the GW4 newspaper's "change since GW1" column implies
    gw2 = "OFFICIAL GW2\n# MOVE\n" + "".join(
        f"{r['rank']} {('▲ ' + str(gw1_rank[r['entry_name']] - r['rank'])) if gw1_rank.get(r['entry_name'], r['rank']) > r['rank'] else ('▼ ' + str(r['rank'] - gw1_rank[r['entry_name']])) if gw1_rank.get(r['entry_name'], r['rank']) < r['rank'] else '–'} mgr\n{r['entry_name']} {r['event_total']} {r['total']} 6.5 1.2\n"
        for r in rows(2))
    gw3 = "TABLE\n" + "".join(
        f"{r['rank']:02d} {r['entry_name']}\nmgr (2 yrs)\n= {r['event_total']} {gw3_total_for_gamma if (gw3_total_for_gamma and r['entry_name'] == 'Gamma') else r['total']} 3XC Haaland\n" for r in rows(3))
    delta = lambda r: ("=" if r["entry_name"] not in gw4_from else f"▲{gw4_from[r['entry_name']] - r['rank']}" if gw4_from[r["entry_name"]] > r["rank"] else f"▼{r['rank'] - gw4_from[r['entry_name']]}" if gw4_from[r["entry_name"]] < r["rank"] else "=")  # noqa: E731
    gw4 = "TABLE\n" + "".join(
        f"{r['rank']:02d} {r['entry_name']} {delta(r)} {r['total']} {r['event_total']} "
        + " ".join(str(next((x['event_total'] for x in rows(g) if x['entry_name'] == r['entry_name']), '–')) for g in (1, 2, 3, 4)) + " FH\n"
        for r in rows(4))
    gw5 = "THE TABLE\n" + "".join(f"{r['rank']:02d} {r['entry_name']} {r['total']} {r['event_total']} 1.2%\n" for r in rows(5))
    return {"gw2": report(gw2, "OFFICIAL GW2"), "gw3": report(gw3), "gw4": report(gw4), "gw5": report(gw5, "THE TABLE")}


def test_reports_that_agree_with_the_rebuild_pass_in_all_four_formats():
    r = checks.reports_check(T5, make_reports(T5))
    assert r["passed"], r["unexplained"]
    assert all(c["compared"] > 0 for c in r["checks"])            # every check actually compared something


def test_a_changed_total_in_a_report_is_unexplained_and_fails():
    tampered = copy.deepcopy(T5)
    tampered[5]["standings"]["results"][0]["total"] += 7
    r = checks.reports_check(T5, make_reports(tampered))
    assert not r["passed"] and any("GW5" in u for u in r["unexplained"])


@pytest.mark.parametrize("key,marker", [("gw2", "OFFICIAL GW2"), ("gw3", "TABLE"), ("gw4", "TABLE"), ("gw5", "THE TABLE")])
def test_a_report_row_the_parser_cannot_read_fails_instead_of_quietly_passing(key, marker):
    """If a printed row is unreadable the check compares fewer managers than it claims: that must be a failure."""
    reports = make_reports(T5)
    text = reports[key].text
    at = text.index("Gamma")
    unreadable = text[:at] + re.sub(r"\d", "x", text[at:at + 40]) + text[at + 40:]       # the numbers after one team's name are gone
    reports[key] = checks.Report([unreadable], TEAMS, marker)
    r = checks.reports_check(T5, reports)
    assert not r["passed"] and any("could be compared" in u for u in r["unexplained"]), r["unexplained"]


def test_a_swapped_order_in_a_report_fails():
    tampered = copy.deepcopy(T5)
    rows = tampered[4]["standings"]["results"]
    rows[0], rows[1] = rows[1], rows[0]
    assert not checks.reports_check(T5, make_reports(tampered))["passed"]


def test_a_stale_figure_in_the_earlier_newspaper_is_excused_only_because_the_later_one_backs_the_rebuild():
    r = checks.reports_check(T5, make_reports(T5, gw3_total_for_gamma=999))
    assert r["passed"]
    gw3 = next(c for c in r["checks"] if c["check"].startswith("GW3"))
    assert any("stale figure" in e and "Gamma" in e for e in gw3["explained_differences"])
    # the same difference with NO later newspaper to corroborate it is not excused
    reports = make_reports(T5, gw3_total_for_gamma=999)
    reports["gw4"] = report("TABLE\n" + "".join(
        f"{r['rank']:02d} {r['entry_name']} = {r['total']} {r['event_total']} 1 1 {1 if r['entry_name'] != 'Gamma' else 77} 1 FH\n" for r in T5[4]["standings"]["results"]))
    uncorroborated = checks.reports_check(T5, reports)
    assert not uncorroborated["passed"]
    # ...and it is the GW3 check itself that refuses the excuse, not merely some other check failing alongside it
    gw3_again = next(c for c in uncorroborated["checks"] if c["check"].startswith("GW3"))
    assert any("Gamma" in u and "does not back" in u for u in gw3_again["unexplained"])


# Alpha and Beta are level on points AND overall rank after GW1, so FPL's order between them is not recoverable.
HT = {
    1: {"current": [gw(g, p, t, overall=10) for g, p, t in ((1, 60, 60), (2, 40, 100), (3, 30, 130), (4, 30, 160), (5, 30, 190))]},
    2: {"current": [gw(1, 60, 60, overall=10)] + [gw(g, p, t, overall=5) for g, p, t in ((2, 50, 110), (3, 30, 140), (4, 30, 170), (5, 30, 200))]},
    3: {"current": [gw(g, 30, 30 * (g - 1), overall=30) for g in (2, 3, 4, 5)]},
}
TT = {g: backfill.reconstruct_table(g, LIVE, HT, built_at=BUILT) for g in range(1, 6)}


def test_a_swap_inside_a_flagged_unresolved_pair_is_excused_but_the_same_swap_elsewhere_is_not():
    assert TT[1]["reconstructed"]["unresolved_ties"] == [{"total": 60, "entries": [1, 2]}]
    ours = {r["entry_name"]: r["rank"] for r in TT[1]["standings"]["results"]}
    assert checks.reports_check(TT, make_reports(TT))["passed"]                     # baseline: everything agrees

    # the GW4 newspaper implies the OPPOSITE GW1 order for the tied pair
    r = checks.reports_check(TT, make_reports(TT, gw4_gw1_rank={**ours, "Alpha": ours["Beta"], "Beta": ours["Alpha"]}))
    gw1 = next(c for c in r["checks"] if "GW4 newspaper's change-since-GW1" in c["check"])
    assert r["passed"] and len(gw1["explained_differences"]) == 2 and gw1["unexplained"] == []
    assert all("not recoverable" in e for e in gw1["explained_differences"])

    # the same size of disagreement for a manager who is NOT tied (here: shifting Alpha to a rank Beta does not hold)
    wrong = checks.reports_check(TT, make_reports(TT, gw4_gw1_rank={**ours, "Alpha": ours["Alpha"] + 5}))
    assert not wrong["passed"] and any("Alpha" in u for u in wrong["unexplained"])


def test_missing_reports_fail_loudly():
    r = checks.reports_check(TABLES, {})
    assert not r["passed"] and "missing reports" in r["reason"]


class TestPublishGate:
    def prepare(self, tmp_path, current=None, **validation):
        files = {}
        for g, t in TABLES.items():
            raw = cli.canonical(t)
            (tmp_path / f"gw-{g}.json").write_bytes(raw)
            files[f"gw-{g}.json"] = {"gw": g, "sha256": hashlib.sha256(raw).hexdigest(), "managers": len(t["standings"]["results"]), "bytes": len(raw)}
        v = {n: {"passed": True} for n in cli.CHECKS} | validation
        (tmp_path / "manifest.json").write_text(json.dumps({"league_id": 7, "built_at": BUILT, "files": files, "validation": v, "current_event": current,
                                                            "scope": {"fpl_ground_truth": "GW3 only"}}))
        return Namespace(league=7, out=str(tmp_path), execute=False, url=None, include_current=False)

    def test_a_fully_validated_set_is_a_dry_run_unless_asked(self, tmp_path, capsys):
        assert cli.cmd_publish(self.prepare(tmp_path)) == 0
        assert "Dry run. Nothing was sent." in capsys.readouterr().out

    @pytest.mark.parametrize("failing", cli.CHECKS)
    def test_any_failed_or_unrun_check_blocks_publishing(self, tmp_path, capsys, failing):
        assert cli.cmd_publish(self.prepare(tmp_path, **{failing: {"passed": False}})) == 2
        assert failing in capsys.readouterr().out

    def test_a_check_that_was_never_run_blocks_publishing(self, tmp_path):
        assert cli.cmd_publish(self.prepare(tmp_path, historical_reports={"passed": None})) == 2

    def sent(self, tmp_path, monkeypatch, **kw):
        args = self.prepare(tmp_path, **kw)
        args.execute, args.url = True, "http://backend"
        monkeypatch.setenv("SNAPSHOT_SECRET", "s")
        calls = []
        monkeypatch.setattr(cli, "http", lambda method, url, body=None, secret=None: (calls.append((method, url)), (200 if method == "GET" else 201, ""))[1])
        return args, calls

    def test_the_current_gameweek_is_left_to_autosnapshot_unless_asked(self, tmp_path, monkeypatch, capsys):
        args, calls = self.sent(tmp_path, monkeypatch, current=max(TABLES))
        assert cli.cmd_publish(args) == 0
        puts = [u for m, u in calls if m == "PUT"]
        assert puts == [f"http://backend/history/7/{g}" for g in sorted(TABLES) if g != max(TABLES)]
        assert "Holding back" in capsys.readouterr().out

    def test_include_current_publishes_it_too(self, tmp_path, monkeypatch):
        args, calls = self.sent(tmp_path, monkeypatch, current=max(TABLES))
        args.include_current = True
        assert cli.cmd_publish(args) == 0
        assert [u for m, u in calls if m == "PUT"][-1] == f"http://backend/history/7/{max(TABLES)}"

    def test_when_nothing_is_current_everything_is_published_and_the_scopes_are_stated(self, tmp_path, monkeypatch, capsys):
        args, calls = self.sent(tmp_path, monkeypatch)
        assert cli.cmd_publish(args) == 0
        assert len([1 for m, _ in calls if m == "PUT"]) == len(TABLES)
        out = capsys.readouterr().out
        assert "fpl_ground_truth: GW3 only" in out and "scope not recorded" in out        # an unrecorded scope is said so, not glossed over

    def test_a_table_edited_after_validation_is_refused(self, tmp_path):
        args = self.prepare(tmp_path)
        p = tmp_path / "gw-1.json"
        data = json.loads(p.read_text())
        data["standings"]["results"][0]["total"] += 1
        p.write_bytes(cli.canonical(data))
        with pytest.raises(SystemExit, match="hash mismatch"):
            cli.cmd_publish(args)

    def test_executing_without_a_url_or_secret_does_nothing(self, tmp_path, monkeypatch):
        args = self.prepare(tmp_path)
        args.execute = True
        monkeypatch.delenv("SNAPSHOT_SECRET", raising=False)
        with pytest.raises(SystemExit, match="--url and SNAPSHOT_SECRET"):
            cli.cmd_publish(args)
