"""Use cases. Orchestrate the FPL gateway, storage and domain rules; know nothing about HTTP."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from . import backfill, domain
from .fpl import FplGateway, FplResponse
from .storage import SnapshotRepository

AUTOSNAPSHOT_TIMEOUT_S = 25.0


class NotReady(Exception):
    """FPL has no usable state yet (e.g. pre-season, or the current GW is unknown)."""


class TimedOut(Exception):
    """FPL was too slow to complete the use case."""


class InvalidSnapshot(Exception):
    """A table offered for import is malformed. `problems` says what is wrong."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class NotFinished(Exception):
    """The gameweek is not over and verified by FPL yet, so its table can still change."""


class AlreadyStored(Exception):
    """A snapshot for this league and gameweek exists. Imports never overwrite one."""


class LeagueService:
    def __init__(self, fpl: FplGateway, snapshots: SnapshotRepository):
        self._fpl = fpl
        self._snapshots = snapshots

    async def standings(self, league_id: int) -> dict:
        return await self._fpl.standings(league_id)

    async def autosnapshot(self, league_id: int) -> dict[str, Any]:
        """Store the table once the current GW is completely over. Idempotent per (league, GW)."""
        try:
            return await asyncio.wait_for(self._autosnapshot(league_id), timeout=AUTOSNAPSHOT_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise TimedOut("Upstream timeout (FPL API slow)")

    async def _autosnapshot(self, league_id: int) -> dict[str, Any]:
        # Always fresh: a stale "not finished" answer would postpone the snapshot until the next daily run.
        status = ((await self._fpl.event_status(fresh=True)).get("status") or [{}])[0]
        gw, bonus_added = status.get("event"), bool(status.get("bonus_added"))
        if not gw:
            raise NotReady("Could not determine current GW")
        fixtures, bootstrap = await asyncio.gather(self._fpl.fixtures(gw, fresh=True), self._fpl.bootstrap(fresh=True))
        c = domain.gameweek_completion(gw, bonus_added, fixtures.data, bootstrap.events)
        if not c.is_over:
            return {"ok": False, "gw": gw, "bonus_added": c.bonus_added, "fixtures_all_finished": c.fixtures_all_finished,
                    "gw_finished_flag": c.gw_finished_flag, "reason": "GW not fully over yet"}
        standings = await self._fpl.standings(league_id, fresh=True)
        stored = await asyncio.to_thread(self._snapshots.save, league_id, gw, standings)
        return {"ok": True, "snapshotted": stored, "already_existed": not stored, "gw": gw}

    async def import_rebuilt(self, league_id: int, gw: int, data: Any) -> None:
        """Store a table rebuilt after the fact (see backfill.py). Refuses anything unfinished, malformed or duplicate."""
        problems = backfill.validate_snapshot_payload(league_id, gw, data)
        if problems:
            raise InvalidSnapshot(problems)
        if gw not in backfill.finished_gameweeks((await self._fpl.bootstrap(fresh=True)).events):
            raise NotFinished(f"GW{gw} is not finished and verified by FPL yet")
        if not await asyncio.to_thread(self._snapshots.save, league_id, gw, data):
            raise AlreadyStored(f"A snapshot for league {league_id}, GW{gw} already exists")

    # Synchronous (SQLite); the API layer runs these in a worker thread.
    def history(self, league_id: int) -> dict:
        return {"league_id": league_id, "snapshots": self._snapshots.list(league_id)}

    def snapshot_json(self, league_id: int, gw: int) -> bytes | None:
        """`{league_id, gw, taken_at, data}` as JSON bytes, splicing the stored text instead of re-encoding it."""
        s = self._snapshots.get(league_id, gw)
        if s is None:
            return None
        head = f'{{"league_id":{s.league_id},"gw":{s.gw},"taken_at":{json.dumps(s.taken_at)},"data":'
        return (head + s.data_json + "}").encode()


class TeamService:
    def __init__(self, fpl: FplGateway):
        self._fpl = fpl

    async def current_team(self, entry_id: int) -> dict:
        bootstrap = await self._fpl.bootstrap()
        gw = domain.current_gw(bootstrap.events)
        if not gw:
            raise NotReady("Season has not started: no current GW")
        picks = await self._fpl.picks(entry_id, gw)
        return {"entry_id": entry_id, "gw": gw,
                "team": domain.group_picks(picks.get("picks", []), bootstrap.elements, bootstrap.teams)}

    async def tenure(self, entry_id: int) -> dict:
        return domain.tenure_summary(entry_id, await self._fpl.entry_history(entry_id))


class GameweekService:
    def __init__(self, fpl: FplGateway):
        self._fpl = fpl

    async def bonus(self, gw: int) -> dict:
        return {"gameweek": gw, "bonuses": domain.bonuses(await self._fpl.event_live(gw))}

    async def bootstrap_raw(self) -> bytes:
        return (await self._fpl.bootstrap()).raw

    async def fixtures_raw(self, event: int | None) -> bytes:
        r: FplResponse = await self._fpl.fixtures(event)
        return r.raw
