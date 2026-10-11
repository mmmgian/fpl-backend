#!/usr/bin/env python3
"""Rebuild, verify and publish past league tables.

    python scripts/backfill_history.py build            fetch, rebuild, run checks 1 and 2, write backfill/<league>/
    python scripts/backfill_history.py verify-reports   check 3, against the league's printed reports
    python scripts/backfill_history.py publish          dry run; add --execute to send the tables to a backend

The tables are written to disk first and published from disk, with their hashes recorded in a manifest, so what
gets published is exactly what was validated. `publish` refuses to run unless every check in the manifest passed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import backfill_checks as checks  # noqa: E402
from app import backfill  # noqa: E402
from app.fpl import FplGateway  # noqa: E402

DEFAULT_LEAGUE = 467763
CONCURRENCY = 4
CHECKS = ("internal_consistency", "fpl_ground_truth", "second_api_path", "historical_reports")
# which report validates which gameweek, and where its table starts
REPORT_DOCS = {
    "gw2": ("gw2-surreal-recap", "OFFICIAL GW2"),
    "gw3": ("lobster-league-times-gw3", "COMPLETE 21-MANAGER MASTER LEDGER"),
    "gw4": ("lobster-league-times-gw4", "COMPLETE 21-MANAGER LEDGER"),
    "gw5": ("lobster-times-gw5", "THE TABLE"),
}


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def out_dir(args) -> Path:
    return Path(args.out) if args.out else ROOT / "backfill" / str(args.league)


def read_manifest(d: Path) -> dict:
    p = d / "manifest.json"
    if not p.exists():
        sys.exit(f"No manifest at {p}. Run `build` first.")
    return json.loads(p.read_text())


def write_manifest(d: Path, manifest: dict) -> None:
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def load_tables(d: Path, manifest: dict) -> dict[int, dict]:
    tables = {}
    for name, meta in manifest["files"].items():
        raw = (d / name).read_bytes()
        if sha256(raw) != meta["sha256"]:
            sys.exit(f"{name} has changed since it was validated (hash mismatch). Run `build` again.")
        tables[meta["gw"]] = json.loads(raw)
    return tables


# ------------------------------------------------------------------ build

async def fetch_all(gateway: FplGateway, league_id: int):
    league = await gateway.standings(league_id, fresh=True)
    events = (await gateway.bootstrap(fresh=True)).events
    members = [r["entry"] for r in league["standings"]["results"]]
    sem = asyncio.Semaphore(CONCURRENCY)

    async def limited(coro):
        async with sem:
            return await coro

    histories = dict(zip(members, await asyncio.gather(*(limited(gateway.entry_history(e)) for e in members))))
    return league, events, histories, limited


async def cmd_build(args) -> int:
    gateway = FplGateway()
    try:
        league, events, histories, limited = await fetch_all(gateway, args.league)
        finished = backfill.finished_gameweeks(events)
        if not finished:
            sys.exit("No gameweek is finished and verified yet: nothing to rebuild.")
        built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"{len(histories)} managers; gameweeks to rebuild: {finished}")

        tables = {gw: backfill.reconstruct_table(gw, league, histories, built_at=built_at) for gw in finished}

        # check 1 (internal): every table agrees with the histories it was built from
        internal = {gw: backfill.check_table(t, histories) for gw, t in tables.items()}
        internal_result = checks.result(not any(internal.values()), tables_checked=len(tables), problems={f"GW{g}": p for g, p in internal.items() if p})

        # check 2: FPL's own live table
        live_gw = next((e["id"] for e in events if e.get("is_current")), max(finished))
        truth = checks.ground_truth_check(league, histories, tables, max(finished), live_gw)

        # check 3: the same facts read through a different endpoint
        wanted = [(e, row["event"]) for e, h in histories.items() for row in h["current"] if row["event"] in tables]
        fetched = await asyncio.gather(*(limited(gateway.picks(e, g)) for e, g in wanted))
        second = checks.second_path_check(histories, dict(zip(wanted, fetched)))
    finally:
        await gateway.aclose()

    d = out_dir(args)
    d.mkdir(parents=True, exist_ok=True)
    for stale in d.glob("gw-*.json"):
        stale.unlink()
    files = {}
    for gw, t in sorted(tables.items()):
        raw = canonical(t)
        (d / f"gw-{gw}.json").write_bytes(raw)
        files[f"gw-{gw}.json"] = {"gw": gw, "sha256": sha256(raw), "managers": len(t["standings"]["results"]), "bytes": len(raw)}
    manifest = {
        "league_id": args.league,
        "built_at": built_at,
        "method": backfill.METHOD,
        "tie_break": backfill.TIE_BREAK,
        "gameweeks": finished,
        "current_event": live_gw,
        "files": files,
        "scope": {
            "internal_consistency": "every rebuilt table, against the histories it was built from",
            "fpl_ground_truth": f"GW{max(finished)} only: the one table FPL still serves live",
            "second_api_path": "every manager-gameweek, via a second FPL endpoint",
            "historical_reports": "GW1-GW5 only: the league's printed reports",
        },
        "validation": {
            "internal_consistency": internal_result,
            "fpl_ground_truth": truth,
            "second_api_path": second,
            "historical_reports": {"passed": None, "status": "not run: use `verify-reports`"},
        },
    }
    write_manifest(d, manifest)
    for name in CHECKS[:3]:
        v = manifest["validation"][name]
        print(f"  {'PASS' if v['passed'] else 'FAIL'}  {name}")
        for m in (v.get("mismatches") or [])[:5]:
            print("        ", m)
        for gw, ps in (v.get("problems") or {}).items():
            print("        ", gw, ps[:3])
    print(f"wrote {len(files)} tables to {d}")
    return 0 if all(manifest["validation"][n]["passed"] for n in CHECKS[:3]) else 1


# ------------------------------------------------------------------ verify-reports

def cmd_verify_reports(args) -> int:
    d = out_dir(args)
    manifest = read_manifest(d)
    tables = load_tables(d, manifest)
    teams = sorted({r["entry_name"] for t in tables.values() for r in t["standings"]["results"]})
    reports, evidence = {}, {}
    for key, (doc, marker) in REPORT_DOCS.items():
        path = Path(args.reports) / doc / "text.json"
        if not path.exists():
            sys.exit(f"Missing report text: {path}")
        raw = path.read_bytes()
        pages = json.loads(raw)["pages"]
        reports[key] = checks.Report(pages[:2] if key == "gw3" else pages[:1], teams, marker)
        evidence[key] = {"document": doc, "sha256": sha256(raw)}
    result = checks.reports_check(tables, reports)
    result["evidence"] = evidence
    manifest["validation"]["historical_reports"] = result
    write_manifest(d, manifest)
    for c in result.get("checks", []):
        flag = "PASS" if not c["unexplained"] else "FAIL"
        print(f"  {flag}  {c['check']}: {c['matched']}/{c['compared']} agree" + (f", {len(c['explained_differences'])} explained" if c["explained_differences"] else ""))
        for e in c["explained_differences"][:6]:
            print("        explained:", e)
        for b in c["unexplained"][:6]:
            print("        UNEXPLAINED:", b)
    print("historical_reports:", "PASS" if result["passed"] else "FAIL")
    return 0 if result["passed"] else 1


# ------------------------------------------------------------------ publish

def http(method: str, url: str, body: bytes | None = None, secret: str | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers={"content-type": "application/json", **({"x-snapshot-secret": secret} if secret else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def cmd_publish(args) -> int:
    d = out_dir(args)
    manifest = read_manifest(d)
    failed = [n for n in CHECKS if manifest["validation"].get(n, {}).get("passed") is not True]
    if failed:
        print("Refusing to publish: these checks have not passed:", ", ".join(failed))
        return 2
    load_tables(d, manifest)                                  # every file still matches the hash that was validated
    plan = sorted(manifest["files"].values(), key=lambda m: m["gw"])
    print(f"Validated on {manifest['built_at']}: all {len(CHECKS)} checks passed, each over its own scope:")
    for name in CHECKS:
        print(f"  - {name}: {(manifest.get('scope') or {}).get(name, 'scope not recorded')}")
    # The gameweek that is still current can be captured properly, from FPL itself, by /autosnapshot. A stored
    # snapshot is never overwritten, so a rebuilt copy published first would shut that out for good.
    current = manifest.get("current_event")
    held_back = [m["gw"] for m in plan if m["gw"] == current and not args.include_current]
    plan = [m for m in plan if m["gw"] not in held_back]
    if held_back:
        print(f"Holding back GW{held_back[0]}: it is the current gameweek, which /autosnapshot can still save from FPL's own table. "
              "Use --include-current to publish the rebuilt one anyway.")
    print(f"Would publish {[m['gw'] for m in plan]} to league {manifest['league_id']}.")
    if not args.execute:
        print("Dry run. Nothing was sent. Re-run with --execute --url <backend> (and SNAPSHOT_SECRET in the environment).")
        return 0
    secret = os.environ.get("SNAPSHOT_SECRET")
    if not args.url or not secret:
        sys.exit("--execute needs --url and SNAPSHOT_SECRET in the environment.")
    base = args.url.rstrip("/")
    status, _ = http("GET", f"{base}/health")
    if status != 200:
        sys.exit(f"{base}/health answered {status}: not publishing.")
    code = 0
    for meta in plan:
        name = next(n for n, m in manifest["files"].items() if m is meta)
        status, text = http("PUT", f"{base}/history/{manifest['league_id']}/{meta['gw']}", (d / name).read_bytes(), secret)
        outcome = {201: "stored", 409: "already there (left untouched)"}.get(status, f"FAILED {status}: {text[:200]}")
        print(f"  GW{meta['gw']}: {outcome}")
        code = code or (0 if status in (201, 409) else 1)
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", type=int, default=DEFAULT_LEAGUE)
    ap.add_argument("--out", help="artifact folder (default: backfill/<league>)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    v = sub.add_parser("verify-reports")
    v.add_argument("--reports", default=str(ROOT.parent / "fpl-frontend" / "public" / "times"), help="folder holding <doc>/text.json")
    p = sub.add_parser("publish")
    p.add_argument("--url")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--include-current", action="store_true", help="also publish the current gameweek (normally left to /autosnapshot)")
    args = ap.parse_args()
    if args.cmd == "build":
        return asyncio.run(cmd_build(args))
    return {"verify-reports": cmd_verify_reports, "publish": cmd_publish}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
