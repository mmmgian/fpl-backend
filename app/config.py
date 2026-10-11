"""Runtime configuration, read once from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CacheTTL:
    """Seconds to reuse an FPL response. Short enough that live pages stay current."""
    bootstrap: float = 300.0
    event_status: float = 30.0
    fixtures: float = 60.0
    standings: float = 60.0
    live: float = 60.0
    picks: float = 60.0
    entry_history: float = 600.0


@dataclass(frozen=True)
class Settings:
    league_id: int = 467763                      # The Lobster League, 2026/27
    db_path: Path = Path("snapshots.db")
    allowed_origins: tuple[str, ...] = ("*",)
    snapshot_secret: str | None = None
    cache_ttl: CacheTTL = field(default_factory=CacheTTL)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        origins = tuple(o.strip() for o in e.get("ALLOWED_ORIGINS", "*").split(",") if o.strip())
        return cls(
            league_id=int(e.get("LEAGUE_ID", "467763")),
            db_path=Path(e.get("DB_PATH", "snapshots.db")),
            allowed_origins=origins or ("*",),
            snapshot_secret=e.get("SNAPSHOT_SECRET") or None,
        )
