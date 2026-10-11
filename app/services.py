"""Use cases. Orchestrate the FPL gateway, storage and domain rules; know nothing about HTTP."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import backfill, domain
from .fpl import FplGateway, FplResponse, Upstream
from .storage import SnapshotRepository

log = logging.getLogger("app.history")

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


class HistoryKeeper:
    """Keeps the stored history complete on a server whose disk does not last (Render's free tier wipes it).

    Every finished, verified gameweek that has no stored table is rebuilt from the managers' own season histories
    (backfill.py) and stored through the same checks as a manual import. Rules:
      - only the configured league, so a stranger's league id can't make this server fetch;
      - never the current gameweek, and never over an existing snapshot (see backfill.healable_gameweeks);
      - one rebuild at a time, however many requests ask for it;
      - after an upstream failure, leave FPL alone for `cooldown` seconds, then try again on the next request;
      - a gameweek that cannot be rebuilt consistently is skipped for this process (logged), not retried in a loop.

    It runs in the background from startup, and `settle` lets a read wait briefly for a rebuild in progress. The
    frontend caches a 200 history list for a day, so a list that is known to be incomplete must not be served as one:
    `settle` reports the gameweeks that are missing and can't be supplied right now, and the caller answers 503.
    """

    def __init__(self, fpl: FplGateway, snapshots: SnapshotRepository, league_id: int, *, cooldown: float = 120.0,
                 recheck_after: float = 300.0, timeout: float = 90.0, concurrency: int = 4,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._fpl, self._snapshots, self._league_id = fpl, snapshots, league_id
        self._cooldown, self._recheck_after, self._timeout, self._concurrency = cooldown, recheck_after, timeout, concurrency
        self._clock, self._now = clock, now
        self._task: asyncio.Task | None = None
        self._task_loop: asyncio.AbstractEventLoop | None = None
        self._failed_at: float | None = None
        self._complete_at: float | None = None
        self._missing: set[int] = set()
        self._unhealable: set[int] = set()

    # ---- triggers
    def kick(self) -> None:
        """Start a rebuild in the background unless one is running or FPL was just unreachable."""
        loop = asyncio.get_running_loop()
        if self._task is not None and not self._task.done() and self._task_loop is loop:
            return
        if self._failed_at is not None and self._clock() - self._failed_at < self._cooldown:
            return
        self._task_loop = loop
        self._task = loop.create_task(self._run())

    async def settle(self, league_id: int, wait: float = 8.0) -> set[int]:
        """Make sure a needed rebuild is under way and wait up to `wait` seconds for it. Returns the gameweeks that are
        known to be missing and can't be served yet (empty when the history is complete or nothing is known)."""
        if league_id != self._league_id:
            return set()
        if self._complete_at is not None and self._clock() - self._complete_at < self._recheck_after:
            return set()
        self.kick()
        task = self._task
        if task is not None and not task.done() and self._task_loop is asyncio.get_running_loop():
            await asyncio.wait({task}, timeout=wait)           # waits; never cancels the rebuild if it takes longer
        return set(self._missing)

    async def stop(self) -> None:
        task = self._task
        if task is not None and not task.done() and self._task_loop is asyncio.get_running_loop():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # ---- the rebuild
    async def _run(self) -> None:
        try:
            await asyncio.wait_for(self._heal(), self._timeout)
            self._failed_at = None
        except asyncio.CancelledError:
            raise
        except Exception as e:                                  # background task: log, never die silently
            self._failed_at, self._complete_at = self._clock(), None
            log.warning("history rebuild failed, will retry after %.0fs: %s: %s", self._cooldown, type(e).__name__, e,
                        exc_info=not isinstance(e, (Upstream, asyncio.TimeoutError)))

    async def _heal(self) -> None:
        lid = self._league_id
        events = (await self._fpl.bootstrap()).events
        stored = {s["gw"] for s in await asyncio.to_thread(self._snapshots.list, lid)}
        wanted = [gw for gw in backfill.healable_gameweeks(events, stored) if gw not in self._unhealable]
        self._missing = set(wanted)
        if not wanted:
            self._complete_at = self._clock()
            return
        log.info("rebuilding history for league %s, gameweeks %s", lid, wanted)
        league = await self._fpl.standings(lid, fresh=True)
        members = [r["entry"] for r in (league.get("standings") or {}).get("results", [])]
        gate = asyncio.Semaphore(self._concurrency)

        async def history(entry: int) -> dict:
            async with gate:
                return await self._fpl.entry_history(entry)

        histories = dict(zip(members, await asyncio.gather(*(history(e) for e in members))))
        built_at = self._now().isoformat(timespec="seconds")
        for gw in wanted:
            try:
                table = backfill.reconstruct_table(gw, league, histories, built_at=built_at)
                problems = backfill.check_table(table, histories) or backfill.validate_snapshot_payload(lid, gw, table)
            except backfill.BackfillError as e:
                problems = [str(e)]
            if problems:
                self._unhealable.add(gw)
                log.warning("GW%s cannot be rebuilt consistently, leaving it out: %s", gw, "; ".join(problems[:3]))
                continue
            await asyncio.to_thread(self._snapshots.save, lid, gw, table)      # False = it appeared meanwhile: fine
        self._missing -= self._unhealable
        self._missing -= {s["gw"] for s in await asyncio.to_thread(self._snapshots.list, lid)}
        if not self._missing:
            self._complete_at = self._clock()


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
