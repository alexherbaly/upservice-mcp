#!/usr/bin/env python3
"""Compare the live Upservice OpenAPI spec against the committed snapshot.

Usage:
    python scripts/check_api_drift.py            # report drift, exit 1 if found
    python scripts/check_api_drift.py --update    # refresh the snapshot to match live

This only diffs endpoint paths and query-param names — it won't catch every
possible change (request-body field additions, enum value changes), but it
covers the class of drift that has actually broken this server before
(missing/renamed query params on list endpoints).
"""
import json
import sys
import urllib.request
from pathlib import Path

SPEC_URL = "https://public.upservice.io/openapi.json"
SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "openapi_snapshot.json"


def fetch_live_spec() -> dict:
    with urllib.request.urlopen(SPEC_URL, timeout=30) as resp:
        return json.load(resp)


def extract_signature(spec: dict) -> dict:
    """Map "METHOD /path" -> sorted list of query-param names, for every operation."""
    sig = {}
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            params = sorted(p["name"] for p in op.get("parameters", []))
            sig[f"{method.upper()} {path}"] = params
    return sig


def main() -> int:
    live = fetch_live_spec()

    if "--update" in sys.argv:
        SNAPSHOT_PATH.write_text(json.dumps(live, indent=2, ensure_ascii=False) + "\n")
        print(f"Snapshot updated: {SNAPSHOT_PATH}")
        return 0

    if not SNAPSHOT_PATH.exists():
        print(f"No snapshot at {SNAPSHOT_PATH}; run with --update first.")
        return 1

    snapshot = json.loads(SNAPSHOT_PATH.read_text())

    snap_sig = extract_signature(snapshot)
    live_sig = extract_signature(live)

    added_ops = sorted(set(live_sig) - set(snap_sig))
    removed_ops = sorted(set(snap_sig) - set(live_sig))
    changed_params = {}
    for op in sorted(set(live_sig) & set(snap_sig)):
        if live_sig[op] != snap_sig[op]:
            changed_params[op] = {
                "added_params": sorted(set(live_sig[op]) - set(snap_sig[op])),
                "removed_params": sorted(set(snap_sig[op]) - set(live_sig[op])),
            }

    drift = bool(added_ops or removed_ops or changed_params)

    if not drift:
        print("No drift detected -- live API matches the committed snapshot.")
        return 0

    print("DRIFT DETECTED between the live Upservice API and openapi_snapshot.json:\n")
    if added_ops:
        print("New endpoints:")
        for op in added_ops:
            print(f"  + {op}")
        print()
    if removed_ops:
        print("Removed endpoints:")
        for op in removed_ops:
            print(f"  - {op}")
        print()
    if changed_params:
        print("Changed query params:")
        for op, diff in changed_params.items():
            print(f"  {op}")
            for p in diff["added_params"]:
                print(f"    + {p}")
            for p in diff["removed_params"]:
                print(f"    - {p}")
        print()

    print("Check whether src/upservice_mcp/server.py needs updating for this, then run:")
    print("  python scripts/check_api_drift.py --update")
    return 1


if __name__ == "__main__":
    sys.exit(main())
