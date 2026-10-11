"""The three independent checks a rebuilt history must pass before it is published. Pure: inputs in, findings out.

  1. fpl_ground_truth   FPL's own live league table carries each manager's previous rank (`last_rank`) and
                        totals. The rebuild must reproduce them exactly.
  2. second_api_path    Every manager x gameweek is read again from a different endpoint
                        (`/entry/{id}/event/{gw}/picks/` -> `entry_history`) and must agree on four fields.
  3. historical_reports The tables printed in the league's earlier reports are an independent record made at
                        the time. Differences are allowed only when they are *explained* by evidence, never waved
                        through: anything unexplained fails the check.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


Finding = dict[str, Any]


def result(passed: bool, **details: Any) -> Finding:
    return {"passed": passed, **details}


# ---------------------------------------------------------------- 1. FPL ground truth

def ground_truth_check(live: dict, histories: dict[int, dict], tables: dict[int, dict], latest_finished: int, live_gw: int) -> Finding:
    """Compare against the live table.

    While gameweek N+1 is in progress, each live row's `last_rank` is its rank after N. Its `total` already
    includes N+1's net points, and `event_total` is N+1's points *before* the hit, so the total after N is
    `total - event_total + hit`, with the hit taken from the manager's own current-gameweek row.
    """
    rows = live["standings"]["results"]
    last = tables[latest_finished]["standings"]["results"]
    ours = {r["entry"]: r for r in last}
    if live_gw == latest_finished + 1:
        mode = "last_rank"
    elif live_gw == latest_finished:
        mode = "direct"
    else:
        return result(False, mode="n/a", reason=f"live table is for GW{live_gw} but the latest verified GW is {latest_finished}: nothing to compare against")

    mismatches: list[str] = []
    compared = 0
    for r in rows:
        mine = ours.get(r["entry"])
        if mine is None:
            if mode == "direct" or r.get("last_rank"):
                mismatches.append(f"entry {r['entry']} is in the live table but not in the rebuilt GW{latest_finished} table")
            continue
        compared += 1
        if mode == "last_rank":
            hit = next((g.get("event_transfers_cost", 0) for g in histories[r["entry"]]["current"] if g["event"] == live_gw), 0)
            expect_rank, expect_total = r["last_rank"], r["total"] - r["event_total"] + hit
        else:
            expect_rank, expect_total = r["rank"], r["total"]
        if mine["rank"] != expect_rank:
            mismatches.append(f"{r['entry_name']}: rank {mine['rank']} != FPL's {expect_rank}")
        if mine["total"] != expect_total:
            mismatches.append(f"{r['entry_name']}: total {mine['total']} != FPL-derived {expect_total}")
    return result(not mismatches and compared == len(ours), mode=mode, gameweek=latest_finished, managers_compared=compared,
                  mismatches=mismatches)


# ---------------------------------------------------------------- 2. second API path

FIELDS = (("total_points", "total_points"), ("points", "points"), ("event_transfers_cost", "event_transfers_cost"), ("overall_rank", "overall_rank"))


def second_path_check(histories: dict[int, dict], picks: dict[tuple[int, int], dict]) -> Finding:
    """`picks[(entry, gw)]` is the picks response. Each must agree with the history endpoint on every field."""
    mismatches: list[str] = []
    expected = 0
    for entry, h in histories.items():
        for row in h["current"]:
            key = (entry, row["event"])
            if key not in picks:
                continue
            expected += 1
            other = (picks[key].get("entry_history") or {})
            for mine, theirs in FIELDS:
                if row.get(mine) != other.get(theirs):
                    mismatches.append(f"entry {entry} GW{row['event']}: {mine} {row.get(mine)} (history) != {other.get(theirs)} (picks)")
    return result(not mismatches and expected > 0, compared=expected, fields=[m for m, _ in FIELDS], mismatches=mismatches)


# ---------------------------------------------------------------- 3. the printed reports

class Report:
    """A report's page text, with the league's team names located in the order they are printed."""

    def __init__(self, pages: list[str], teams: list[str], marker: str):
        self.text = "\n".join(pages)
        start = self.text.find(marker)
        body = self.text[start:] if start >= 0 else self.text
        first: dict[str, int] = {}
        for t in sorted(teams, key=len, reverse=True):
            m = re.search(r"(?<![\w'])" + re.escape(t) + r"(?![\w'])", body)
            if m:
                first[t] = m.start()
        self.order = sorted(first, key=first.get)
        self.rows = {t: body[first[t] + len(t):(first[nxt] if (nxt := self._next(first, t)) else len(body))] for t in self.order}

    @staticmethod
    def _next(first: dict[str, int], team: str) -> str | None:
        later = [t for t in first if first[t] > first[team]]
        return min(later, key=first.get) if later else None


NUM = r"(\d+|–|—)"


def _int(tok: str) -> int | None:
    return int(tok) if tok.isdigit() else None


def parse_gw2_recap(r: Report) -> dict[str, dict]:
    """`<team> <GW2 pts> [(hit)] <total> <xG> ...` -> gross GW2 points and total."""
    out = {}
    for t in r.order:
        m = re.search(r"(\d+)\s+(?:\((-?\d+)\)\s+)?(\d+)\s+\d+\.\d+", r.rows[t])
        if m:
            out[t] = {"event_total": int(m.group(1)), "total": int(m.group(3))}
    return out


def gw2_moves(r: Report) -> dict[str, int]:
    """Rank movement from GW1 by the printed rank order (`▲3` = climbed three places)."""
    moves = re.findall(r"^(\d{1,2}) (?:([▲▼])\s?(\d+)|[–=])", r.text[r.text.find("# MOVE"):], flags=re.M)
    return {t: (1 if arrow == "▲" else -1) * int(n) if arrow else 0 for t, (_rank, arrow, n) in zip(r.order, moves)}


def parse_gw3_times(r: Report) -> dict[str, dict]:
    """`<team> ... <delta> <GW3 pts> <total> ...`."""
    out = {}
    for t in r.order:
        m = re.search(r"(?:[+\-−]\d+|=|NEW)\s+(\d+)\s+(\d+)\s", r.rows[t])
        if m:
            out[t] = {"event_total": int(m.group(1)), "total": int(m.group(2))}
    return out


def parse_gw4_times(r: Report) -> dict[str, dict]:
    """`<team> <delta vs GW1> <total> <GW4> <GW1> <GW2> <GW3> <GW4>`: gross points per gameweek and the delta."""
    out = {}
    for t in r.order:
        m = re.match(rf"\s*(?:([▲▼])\s?(\d+)|=|NEW)\s+(\d+)\s+(\d+)\s+{NUM}\s+{NUM}\s+{NUM}\s+{NUM}", r.rows[t])
        if m:
            delta = (1 if m.group(1) == "▲" else -1) * int(m.group(2)) if m.group(1) else 0
            out[t] = {"delta_vs_gw1": delta, "total": int(m.group(3)), "splits": {g: _int(m.group(4 + g)) for g in range(1, 5)}}
    return out


def parse_gw5_front(r: Report) -> dict[str, dict]:
    """`<team> <total> <GW5 pts> <world %>`."""
    out = {}
    for t in r.order:
        m = re.search(r"(\d+)\s+(\d+)\s+[\d.]+\s?%", r.rows[t])
        if m:
            out[t] = {"total": int(m.group(1)), "event_total": int(m.group(2))}
    return out


def _rows(table: dict) -> dict[str, dict]:
    return {r["entry_name"]: r for r in table["standings"]["results"]}


def _order_ok(printed: list[str], table: dict) -> tuple[bool, list[str]]:
    ours = [r["entry_name"] for r in table["standings"]["results"]]
    return printed[: len(ours)] == ours, [f"printed order differs at position {i + 1}: {a!r} vs ours {b!r}" for i, (a, b) in enumerate(zip(printed, ours)) if a != b][:3]


def reports_check(tables: dict[int, dict], reports: dict[str, Report]) -> Finding:
    """Compare against the printed reports. `reports` keys: gw2, gw3, gw4, gw5. Missing ones fail the check."""
    need = ("gw2", "gw3", "gw4", "gw5")
    if any(k not in reports for k in need):
        return result(False, reason=f"missing reports: {[k for k in need if k not in reports]}")
    findings: list[Finding] = []
    unexplained: list[str] = []

    def add(name: str, compared: int, matched: int, explained: list[str] | None = None, bad: list[str] | None = None, *, expected: int, **extra: Any) -> None:
        """`expected` is how many managers this check must cover. A parser that silently skips rows would otherwise
        make a check pass on fewer managers than it claims; that is a failure, not a pass."""
        bad = list(bad or [])
        if compared != expected:
            bad.append(f"only {compared} of {expected} managers could be compared (a printed row was not read, or a manager is missing from the report)")
        findings.append({"check": name, "compared": compared, "expected": expected, "matched": matched, "explained_differences": explained or [], "unexplained": bad, **extra})
        unexplained.extend(f"{name}: {b}" for b in bad)

    t1, t2, t3, t4, t5 = (_rows(tables[g]) for g in (1, 2, 3, 4, 5))
    p2, p3, p4, p5 = (parse_gw2_recap(reports["gw2"]), parse_gw3_times(reports["gw3"]), parse_gw4_times(reports["gw4"]), parse_gw5_front(reports["gw5"]))

    # GW1 ranks, from the GW4 newspaper (rank now + places climbed since GW1): a tie-free table of its own.
    # That newspaper's GW1 ranks came from the league's own earlier script, not from FPL, so where FPL's order is
    # itself unrecoverable (level on points AND overall rank) a swap inside that flagged pair is not evidence
    # against the rebuild. Any other difference is.
    gw4_rank = {t: i + 1 for i, t in enumerate(reports["gw4"].order)}
    derived = {t: gw4_rank[t] + p4[t]["delta_vs_gw1"] for t in p4 if t in t1}
    group_ranks = {}
    for g in tables[1]["reconstructed"]["unresolved_ties"]:
        ranks = {r["rank"] for r in tables[1]["standings"]["results"] if r["entry"] in g["entries"]}
        group_ranks.update({r["entry_name"]: ranks for r in tables[1]["standings"]["results"] if r["entry"] in g["entries"]})
    explained, bad = [], []
    for t, rank in derived.items():
        if rank == t1[t]["rank"]:
            continue
        if t in group_ranks and rank in group_ranks[t]:
            explained.append(f"{t}: GW1 rank {rank} per the GW4 newspaper, {t1[t]['rank']} rebuilt: level on {t1[t]['total']} points and overall rank, so FPL's order is not recoverable (flagged rank_approximate)")
        else:
            bad.append(f"{t}: GW1 rank {rank} per the GW4 newspaper, {t1[t]['rank']} rebuilt")
    add("GW1 ranks via the GW4 newspaper's change-since-GW1 column", len(derived), len(derived) - len(bad), explained, bad, expected=len(t1))

    # GW1 ranks again, via the GW2 recap's movement column. That recap gives tied managers one shared rank, which
    # is a convention difference, so the second of a tied pair may be one place lower in ours.
    moves = gw2_moves(reports["gw2"])
    gw2_rank = {t: i + 1 for i, t in enumerate(reports["gw2"].order)}
    explained, bad, ok, seen = [], [], 0, 0
    for t, mv in moves.items():
        if t not in t1:
            continue
        seen += 1
        shared = gw2_rank[t] + mv
        if shared == t1[t]["rank"]:
            ok += 1
        elif shared == t1[t]["rank"] - 1 and any(o["total"] == t1[t]["total"] and o["rank"] == t1[t]["rank"] - 1 for o in t1.values()):
            explained.append(f"{t}: shares rank {shared} with the manager tied on {t1[t]['total']} points (we give distinct ranks, as FPL does)")
            ok += 1
        else:
            bad.append(f"{t}: GW1 rank {shared} per the GW2 recap, {t1[t]['rank']} rebuilt")
    add("GW1 ranks via the GW2 recap's movement column", seen, ok, explained, bad, expected=len(t1))

    # GW2: the validated recap is a complete record: order, gross points and total.
    ok_order, order_notes = _order_ok(reports["gw2"].order, tables[2])
    bad = list(order_notes if not ok_order else [])
    for t, v in p2.items():
        if t in t2 and (v["total"] != t2[t]["total"] or v["event_total"] != t2[t]["event_total"]):
            bad.append(f"{t}: printed {v['event_total']}/{v['total']}, rebuilt {t2[t]['event_total']}/{t2[t]['total']}")
    seen = sum(t in t2 for t in p2)
    add("GW2 recap: order, GW points and total", seen, seen - len(bad), bad=bad, expected=len(t2))

    # GW4: order, total and every per-gameweek gross split. The splits also pin down GW1-GW3 independently.
    ok_order, order_notes = _order_ok(reports["gw4"].order, tables[4])
    bad = list(order_notes if not ok_order else [])
    compared, differing = 0, set()
    for t, v in p4.items():
        if t not in t4:
            continue
        compared += 1
        if v["total"] != t4[t]["total"]:
            bad.append(f"{t}: printed total {v['total']}, rebuilt {t4[t]['total']}")
            differing.add(t)
        for g in range(1, 5):
            mine = next((r["event_total"] for r in tables[g]["standings"]["results"] if r["entry_name"] == t), None) if g in tables else None
            if v["splits"][g] != mine:
                bad.append(f"{t}: GW{g} printed {v['splits'][g]}, rebuilt {mine}")
                differing.add(t)
    add("GW4 newspaper: order, total and the GW1-GW4 points of every manager", compared, compared - len(differing), bad=bad, expected=len(t4))

    # GW5: order, total and GW points.
    ok_order, order_notes = _order_ok(reports["gw5"].order, tables[5])
    bad = list(order_notes if not ok_order else [])
    for t, v in p5.items():
        if t in t5 and (v["total"] != t5[t]["total"] or v["event_total"] != t5[t]["event_total"]):
            bad.append(f"{t}: printed {v['event_total']}/{v['total']}, rebuilt {t5[t]['event_total']}/{t5[t]['total']}")
    seen = sum(t in t5 for t in p5)
    add("GW5 newspaper: order, GW points and total", seen, seen - len(bad), bad=bad, expected=len(t5))

    # GW3: this earlier newspaper disagrees with the API in two *known* ways. Each is allowed only if the evidence
    # says so; anything else fails.
    explained, bad, ok = [], [], 0
    stale = set()
    for t, v in p3.items():
        if t not in t3:
            continue
        if v["total"] == t3[t]["total"] and v["event_total"] == t3[t]["event_total"]:
            ok += 1
            continue
        before = t2[t]["total"] if t in t2 else 0
        hit = t3[t]["event_total"] - (t3[t]["total"] - before)  # points lost to transfers that week (the table's total is net)
        if hit and v["total"] == t3[t]["total"] and v["event_total"] == t3[t]["event_total"] - hit:
            explained.append(f"{t}: the GW3 newspaper printed {v['event_total']} for the week, which is {t3[t]['event_total']} after a {hit}-point transfer hit; the total ({v['total']}) agrees")
            ok += 1
            continue
        arbiter = p4.get(t, {}).get("splits", {}).get(3)          # the later GW4 newspaper's GW3 points for this manager
        if arbiter is not None and arbiter == t3[t]["event_total"]:
            explained.append(f"{t}: the GW3 newspaper printed {v['event_total']}/{v['total']}; the later GW4 newspaper and the API both have {t3[t]['event_total']}/{t3[t]['total']} (stale figure)")
            stale.add(t)
            ok += 1
        else:
            bad.append(f"{t}: printed {v['event_total']}/{v['total']}, rebuilt {t3[t]['event_total']}/{t3[t]['total']}, and the GW4 newspaper does not back the printed figure")
    printed = [t for t in reports["gw3"].order if t in t3 and t not in stale]
    ours = [r["entry_name"] for r in tables[3]["standings"]["results"] if r["entry_name"] not in stale]
    totals = {t: t3[t]["total"] for t in t3}
    if [totals[t] for t in printed] != sorted((totals[t] for t in printed), reverse=True):
        bad.append("the GW3 newspaper's rows are not in descending order of total")
    groups_printed, groups_ours = defaultdict(set), defaultdict(set)
    for pos, t in enumerate(printed):
        groups_printed[totals[t]].add(t)
    for t in ours:
        groups_ours[totals[t]].add(t)
    if groups_printed != groups_ours:
        bad.append("the GW3 newspaper groups managers differently from the rebuild beyond tie order")
    swapped = [f"{a} / {b}" for a, b in zip(printed, ours) if a != b]
    if swapped:
        explained.append(f"tie order only: the newspaper lists tied managers by that week's points; FPL (and the rebuild) use overall rank. Positions differing: {len(swapped)}")
    add("GW3 newspaper: totals, GW points and order (earlier, partly stale)", sum(t in t3 for t in p3), ok, explained, bad, expected=len(t3))

    unresolved = {f"GW{g}": [e["entries"] for e in tables[g]["reconstructed"]["unresolved_ties"]] for g in sorted(tables) if tables[g]["reconstructed"]["unresolved_ties"]}
    return result(not unexplained, checks=findings, unexplained=unexplained, unresolved_ties_flagged=unresolved)
