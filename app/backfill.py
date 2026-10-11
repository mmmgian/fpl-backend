"""Rebuild past league tables from each manager's own results. Pure: no I/O, no framework imports.

FPL only exposes the *current* league table. Every manager's season history is public though, and a classic
league table is a function of it, so a past table can be rebuilt exactly:

  total        the manager's cumulative net points after that gameweek (`total_points`)
  event_total  the gameweek's points *before* the transfer hit (`points`); the league table is gross here
  rank         by total, then by the manager's overall rank after that gameweek (best first)
  last_rank    the rank in the previous gameweek's table, 0 for a manager not yet in it

The tie-break was established empirically. On 119 tied groups in five large leagues, 2,589 tied pairs:
  - 2,098 pairs had different overall ranks, and FPL ordered every one of them by better overall rank (100%).
  - 491 pairs had the SAME overall rank (equal points share an overall rank). Nothing public explains FPL's
    order for those: entry id 51%, previous rank 48%, the week's points 49%, registration time 49%, names 42-47%.
So the order of two managers level on points AND overall rank is not recoverable. They are ordered by entry id so
the output is deterministic, and every row in such a tie is marked `rank_approximate` and the groups are listed in the table's
`reconstructed.unresolved_ties`. A row whose PREVIOUS rank came from such a tie is marked `last_rank_approximate`
(its movement arrow may be a place out). Nobody should mistake a guess for FPL's answer.

Only gameweeks that are finished and data-checked are rebuilt: before that, points can still change.
"""
from __future__ import annotations

import math
from typing import Any

METHOD = "rebuilt from each manager's season history"
TIE_BREAK = "total points, then overall rank after the gameweek (best first); level on both: entry id (approximate)"

MAX_PAYLOAD_ROWS = 500
ROW_KEYS = ("entry", "entry_name", "player_name", "rank", "last_rank", "rank_sort", "event_total", "total")


class BackfillError(ValueError):
    """The inputs can't be turned into a trustworthy table."""


def finished_gameweeks(events: list[dict]) -> list[int]:
    """Gameweeks that are over and verified by FPL, ascending."""
    return sorted(e["id"] for e in events if e.get("finished") and e.get("data_checked"))


def healable_gameweeks(events: list[dict], stored: set[int]) -> list[int]:
    """Gameweeks a server with an empty or partial store should rebuild: finished and verified, not stored yet, and
    not the current one. The current gameweek is left alone because `/autosnapshot` can still save FPL's own table
    for it, and a stored snapshot is never overwritten, so a rebuilt copy saved first would shut that out for good."""
    current = {e["id"] for e in events if e.get("is_current")}
    return [gw for gw in finished_gameweeks(events) if gw not in stored and gw not in current]


def _row_at(history: dict, gw: int) -> dict | None:
    return next((g for g in history.get("current", []) if g.get("event") == gw), None)


def _order(present: dict[int, dict]) -> list[int]:
    """Entry ids in table order. `present` maps entry -> that manager's history row for the gameweek."""
    return sorted(
        present,
        key=lambda e: (-present[e]["total_points"], present[e].get("overall_rank") or math.inf, e),
    )


def _unresolved(present: dict[int, dict]) -> list[list[int]]:
    """Groups of managers whose relative order is not recoverable: level on points AND overall rank, or level on
    points with an overall rank missing for someone in the group (nothing then separates them)."""
    by_total: dict[int, list[int]] = {}
    for e, row in present.items():
        by_total.setdefault(row["total_points"], []).append(e)
    groups: list[list[int]] = []
    for members in by_total.values():
        if len(members) < 2:
            continue
        if any(present[e].get("overall_rank") is None for e in members):
            groups.append(members)
            continue
        by_rank: dict[int, list[int]] = {}
        for e in members:
            by_rank.setdefault(present[e]["overall_rank"], []).append(e)
        groups.extend(g for g in by_rank.values() if len(g) > 1)
    return sorted(sorted(g) for g in groups)


def ranks_at(histories: dict[int, dict], gw: int) -> dict[int, int]:
    """entry -> rank in the table after `gw`. Managers who hadn't started yet are not in it."""
    present = {e: row for e, h in histories.items() if (row := _row_at(h, gw))}
    return {e: i + 1 for i, e in enumerate(_order(present))}


def approximate_at(histories: dict[int, dict], gw: int) -> set[int]:
    """Entries whose rank after `gw` depends on an unresolved tie."""
    present = {e: row for e, h in histories.items() if (row := _row_at(h, gw))}
    return {e for g in _unresolved(present) for e in g}


def reconstruct_table(
    gw: int, league: dict, histories: dict[int, dict], *, built_at: str, source: str = "FPL entry history"
) -> dict:
    """The league table after `gw`, in the shape FPL returns (so the app treats it like any stored snapshot).

    `league` is a live standings response: it supplies names, badges and league details. `histories` maps
    each member's entry id to its `/entry/{id}/history/` response.
    """
    standings = league.get("standings") or {}
    if standings.get("has_next"):
        raise BackfillError("The live league table has more pages than were fetched: the member list is incomplete")
    members = {r["entry"]: r for r in standings.get("results", [])}
    if not members:
        raise BackfillError("The league has no members")
    missing = sorted(set(members) - set(histories))
    if missing:
        raise BackfillError(f"No season history for entries {missing}")

    present = {e: row for e in members if (row := _row_at(histories[e], gw))}
    if not present:
        raise BackfillError(f"No manager has results for gameweek {gw}")
    mine = {e: histories[e] for e in members}
    previous = ranks_at(mine, gw - 1) if gw > 1 else {}
    unresolved = _unresolved(present)
    approximate = {e for g in unresolved for e in g}
    last_approximate = approximate_at(mine, gw - 1) if gw > 1 else set()

    results = []
    for rank, entry in enumerate(_order(present), start=1):
        m, row = members[entry], present[entry]
        results.append({
            "entry": entry,
            "entry_name": m["entry_name"],
            "player_name": m["player_name"],
            "rank": rank,
            "last_rank": previous.get(entry, 0),
            "rank_sort": rank,
            "event_total": row["points"],
            "total": row["total_points"],
            "club_badge_src": m.get("club_badge_src"),
            "rank_approximate": entry in approximate,
            "last_rank_approximate": entry in last_approximate,
        })

    return {
        "league": dict(league["league"]),
        "standings": {"has_next": False, "page": 1, "results": results},
        "new_entries": {"has_next": False, "page": 1, "results": []},
        "reconstructed": {
            "gw": gw, "method": METHOD, "tie_break": TIE_BREAK, "source": source, "built_at": built_at,
            "unresolved_ties": [{"total": present[g[0]]["total_points"], "entries": g} for g in unresolved],
        },
    }


def check_table(table: dict, histories: dict[int, dict]) -> list[str]:
    """Internal consistency of a rebuilt table against the histories it came from. Empty list = consistent."""
    problems: list[str] = []
    gw = (table.get("reconstructed") or {}).get("gw")
    if not isinstance(gw, int):
        return ["table has no reconstructed.gw"]
    rows = table["standings"]["results"]
    n = len(rows)

    if [r["rank"] for r in rows] != list(range(1, n + 1)):
        problems.append("ranks are not 1..n in listed order")
    if any(r["rank_sort"] != r["rank"] for r in rows):
        problems.append("rank_sort differs from rank")
    if len({r["entry"] for r in rows}) != n:
        problems.append("an entry appears twice")
    if any(a["total"] < b["total"] for a, b in zip(rows, rows[1:])):
        problems.append("totals are not in descending order")

    # Completeness: exactly the managers who have results for this gameweek, no more and no fewer.
    expected = {e for e, h in histories.items() if _row_at(h, gw)}
    listed = {r["entry"] for r in rows}
    if listed != expected:
        problems.append(f"table does not hold exactly the managers with results for GW{gw}: missing {sorted(expected - listed)}, unexpected {sorted(listed - expected)}")
    # Order, checked pair by pair and written separately from the builder's sort: level on points means the
    # better overall rank comes first, and a missing overall rank can't beat a real one.
    def overall(entry: int):
        row = _row_at(histories[entry], gw) if entry in histories else None
        return row.get("overall_rank") if row else None
    for a, b in zip(rows, rows[1:]):
        if a["total"] != b["total"]:
            continue
        oa, ob = overall(a["entry"]), overall(b["entry"])
        if (oa is None and ob is not None) or (oa is not None and ob is not None and oa > ob):
            problems.append(f"entries {a['entry']} and {b['entry']} are level on {a['total']} points but the one with overall rank {oa} is listed above overall rank {ob}")

    prev_ranks = ranks_at(histories, gw - 1) if gw > 1 else {}
    expect_approx = approximate_at(histories, gw)
    expect_last_approx = approximate_at(histories, gw - 1) if gw > 1 else set()
    expect_groups = _unresolved({e: row for e, h in histories.items() if (row := _row_at(h, gw))})
    if [g["entries"] for g in (table["reconstructed"].get("unresolved_ties") or [])] != expect_groups:
        problems.append("unresolved_ties does not list the managers who are level on points and overall rank")
    for r in rows:
        if r.get("rank_approximate") != (r["entry"] in expect_approx):
            problems.append(f"entry {r['entry']}: rank_approximate is {r.get('rank_approximate')}, expected {r['entry'] in expect_approx}")
        if r.get("last_rank_approximate") != (r["entry"] in expect_last_approx):
            problems.append(f"entry {r['entry']}: last_rank_approximate is {r.get('last_rank_approximate')}, expected {r['entry'] in expect_last_approx}")
        cur = _row_at(histories[r["entry"]], gw)
        prev = _row_at(histories[r["entry"]], gw - 1) if gw > 1 else None
        if cur is None:
            problems.append(f"entry {r['entry']} has no results for GW{gw}")
            continue
        if r["total"] != cur["total_points"]:
            problems.append(f"entry {r['entry']}: total {r['total']} != history {cur['total_points']}")
        if r["event_total"] != cur["points"]:
            problems.append(f"entry {r['entry']}: event_total {r['event_total']} != history {cur['points']}")
        # cumulative arithmetic: this week's net points are what the total moved by
        before = prev["total_points"] if prev else 0
        if cur["total_points"] - before != cur["points"] - cur.get("event_transfers_cost", 0):
            problems.append(f"entry {r['entry']}: total moved by {cur['total_points'] - before}, expected {cur['points'] - cur.get('event_transfers_cost', 0)}")
        if r["last_rank"] != prev_ranks.get(r["entry"], 0):
            problems.append(f"entry {r['entry']}: last_rank {r['last_rank']} != previous table's rank {prev_ranks.get(r['entry'], 0)}")
    return problems


MAX_TEXT = 200
MAX_BADGE = 300
TOP_KEYS = {"league", "standings", "new_entries", "reconstructed"}
ROW_ALLOWED = set(ROW_KEYS) | {"club_badge_src", "rank_approximate", "last_rank_approximate"}
REC_KEYS = {"gw", "method", "tie_break", "source", "built_at", "unresolved_ties"}


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_text(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v.strip()) and len(v) <= MAX_TEXT


def validate_snapshot_payload(league_id: int, gw: int, data: Any) -> list[str]:
    """Strict check of a table offered for storage. A stored snapshot is permanent, so anything unexpected is
    refused: unknown keys, wrong types, out-of-range numbers, links that aren't https, and flags that don't
    agree with each other. (Whether the numbers are *right* is for `check_table`, which has the histories.)"""
    if not isinstance(data, dict):
        return ["payload must be an object"]
    problems: list[str] = []
    extra = set(data) - TOP_KEYS
    if extra:
        problems.append(f"unexpected keys {sorted(extra)}")

    league = data.get("league")
    if not isinstance(league, dict) or league.get("id") != league_id or not _is_int(league.get("id")):
        problems.append("league.id must match the URL")
    elif any(not (v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= MAX_TEXT)) for v in league.values()):
        problems.append("league may only hold short scalar values")

    rec = data.get("reconstructed")
    if not isinstance(rec, dict) or rec.get("gw") != gw or not _is_text(rec.get("method")) or not _is_text(rec.get("built_at")):
        problems.append("`reconstructed` (with gw, method and built_at) is required: only rebuilt tables may be imported")
        rec = None
    elif set(rec) - REC_KEYS:
        problems.append(f"reconstructed has unexpected keys {sorted(set(rec) - REC_KEYS)}")
    for k in ("tie_break", "source"):
        if rec and k in rec and not _is_text(rec[k]):
            problems.append(f"reconstructed.{k} must be short text")

    standings = data.get("standings")
    results = standings.get("results") if isinstance(standings, dict) else None
    if not isinstance(results, list) or not results:
        return problems + ["standings.results must be a non-empty list"]
    if len(results) > MAX_PAYLOAD_ROWS:
        return problems + [f"more than {MAX_PAYLOAD_ROWS} rows"]
    if standings.get("has_next") is not False:
        problems.append("standings.has_next must be false: the whole table is stored")
    new_entries = data.get("new_entries")
    if new_entries is not None and not (isinstance(new_entries, dict) and new_entries.get("results") == []):
        problems.append("new_entries must be empty")

    n = len(results)
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            problems.append(f"row {i} is not an object")
            continue
        if set(r) - ROW_ALLOWED:
            problems.append(f"row {i}: unexpected keys {sorted(set(r) - ROW_ALLOWED)}")
        bounds = {"entry": (1, 10**9), "rank": (1, n), "rank_sort": (1, n), "last_rank": (0, MAX_PAYLOAD_ROWS),
                  "event_total": (0, 2000), "total": (-1000, 100000)}
        for k, (lo, hi) in bounds.items():
            v = r.get(k)
            if not _is_int(v) or not lo <= v <= hi:
                problems.append(f"row {i}: {k} must be an integer from {lo} to {hi}")
        for k in ("entry_name", "player_name"):
            if not _is_text(r.get(k)):
                problems.append(f"row {i}: {k} must be short non-empty text")
        badge = r.get("club_badge_src")
        if badge is not None and not (isinstance(badge, str) and badge.startswith("https://") and len(badge) <= MAX_BADGE):
            problems.append(f"row {i}: club_badge_src must be null or an https link")
        for flag in ("rank_approximate", "last_rank_approximate"):
            if not isinstance(r.get(flag), bool):
                problems.append(f"row {i}: {flag} must be true or false")
    if problems:
        return problems

    if [r["rank"] for r in results] != list(range(1, n + 1)):
        problems.append("ranks must be 1..n in listed order")
    if any(r["rank_sort"] != r["rank"] for r in results):
        problems.append("rank_sort must equal rank")
    if len({r["entry"] for r in results}) != n:
        problems.append("an entry appears twice")
    if any(a["total"] < b["total"] for a, b in zip(results, results[1:])):
        problems.append("rows must be in descending order of total")
    if any(r["last_rank_approximate"] and r["last_rank"] == 0 for r in results):
        problems.append("a manager with no previous rank can't have an approximate previous rank")

    # The tie list and the per-row flags describe the same thing, so they must agree.
    ties = rec.get("unresolved_ties") if rec else None
    if not isinstance(ties, list):
        return problems + ["reconstructed.unresolved_ties must be a list"]
    by_entry = {r["entry"]: r for r in results}
    listed: set[int] = set()
    for g in ties:
        entries = g.get("entries") if isinstance(g, dict) else None
        if not (isinstance(entries, list) and len(entries) >= 2 and all(_is_int(e) for e in entries)) or not _is_int(g.get("total")):
            problems.append("each unresolved tie needs a total and at least two entry ids")
            continue
        if any(e not in by_entry for e in entries) or len(set(entries)) != len(entries) or listed & set(entries):
            problems.append(f"unresolved tie {entries} names an unknown or repeated entry")
            continue
        if any(by_entry[e]["total"] != g["total"] for e in entries):
            problems.append(f"unresolved tie {entries} is not level on {g['total']} points")
        listed |= set(entries)
    flagged = {r["entry"] for r in results if r["rank_approximate"]}
    if flagged != listed:
        problems.append("rank_approximate rows do not match the managers named in unresolved_ties")
    return problems
