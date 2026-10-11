"""Pure FPL rules. No I/O, no framework imports: everything here is trivially unit-testable."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def current_gw(events: list[dict]) -> int | None:
    """Current gameweek, else the latest finished one, else None (pre-season)."""
    cur = next((e for e in events if e.get("is_current")), None)
    if cur:
        return cur["id"]
    finished = [e["id"] for e in events if e.get("finished")]
    return max(finished) if finished else None


def fixtures_all_finished(fixtures: list) -> bool:
    return bool(fixtures) and all(f.get("finished") and f.get("finished_provisional", True) for f in fixtures)


@dataclass(frozen=True)
class GameweekCompletion:
    gw: int
    bonus_added: bool
    fixtures_all_finished: bool
    gw_finished_flag: bool

    @property
    def is_over(self) -> bool:
        """Safe to snapshot: bonus confirmed, every fixture finished and FPL marks the GW finished."""
        return self.bonus_added and self.fixtures_all_finished and self.gw_finished_flag


def gameweek_completion(gw: int, bonus_added: bool, fixtures: list, events: list[dict]) -> GameweekCompletion:
    meta = next((e for e in events if e.get("id") == gw), None)
    return GameweekCompletion(
        gw=gw,
        bonus_added=bonus_added,
        fixtures_all_finished=fixtures_all_finished(fixtures),
        gw_finished_flag=bool(meta and meta.get("finished")),
    )


def group_picks(picks: list[dict], elements: dict[int, dict], teams: dict[int, dict]) -> dict[str, list]:
    """Picks grouped by position (GK/DEF/MID/FWD). Unknown elements are skipped, not fatal."""
    grouped: dict[str, list] = {p: [] for p in POSITIONS.values()}
    for p in picks:
        el = elements.get(p.get("element"))
        if not el:
            continue
        team = teams.get(el.get("team"), {})
        grouped[POSITIONS.get(el.get("element_type"), "MID")].append({
            "id": el["id"],
            "name": f"{el.get('first_name', '')} {el.get('second_name', '')}".strip(),
            "web_name": el.get("web_name"),
            "team": team.get("short_name") or team.get("name"),
            "now_cost": el.get("now_cost"),
            "selected_by_percent": el.get("selected_by_percent"),
            "is_captain": bool(p.get("is_captain")),
            "is_vice_captain": bool(p.get("is_vice_captain")),
            "multiplier": p.get("multiplier"),
            "position": p.get("position"),       # 1-11 starting XI, 12-15 bench
        })
    return grouped


def bonuses(live: dict) -> list[dict]:
    return [{"player_id": el["id"], "bonus": el["stats"]["bonus"]}
            for el in live.get("elements", []) if el.get("stats", {}).get("bonus", 0) > 0]


def tenure_summary(entry_id: int, history: dict) -> dict[str, Any]:
    seasons = sorted(p["season_name"] for p in history.get("past") or []
                     if isinstance(p, dict) and p.get("season_name"))
    first = seasons[0] if seasons else None
    since = None
    if first and "/" in first:
        try:
            since = int(first.split("/")[0])
        except ValueError:
            since = None
    return {"entry_id": entry_id, "seasons_played": len(seasons), "first_season": first,
            "playing_since_year": since, "seasons": seasons}
