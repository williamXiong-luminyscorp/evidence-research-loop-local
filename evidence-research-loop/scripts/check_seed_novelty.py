#!/usr/bin/env python3
"""Prove that a planned holdout seed set is absent from recorded raw results."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planned", required=True, help="JSON with dev and holdout integer arrays")
    parser.add_argument("--history-root", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    planned_path = Path(args.planned).resolve()
    planned = load(planned_path)
    dev = [int(value) for value in planned["dev"]]
    holdout = [int(value) for value in planned["holdout"]]
    history_files: list[dict[str, Any]] = []
    historical_seeds: set[int] = set()
    for root_arg in args.history_root:
        root = Path(root_arg).resolve()
        for path in sorted(root.rglob("raw-results.json")):
            try:
                rows = load(path)
            except (OSError, json.JSONDecodeError):
                continue
            seeds = sorted({int(row["seed"]) for row in rows
                            if isinstance(row, dict) and isinstance(row.get("seed"), int)})
            if not seeds:
                continue
            historical_seeds.update(seeds)
            history_files.append({"path": str(path), "sha256": sha256_file(path), "seeds": seeds})
    overlap = sorted(set(holdout) & historical_seeds)
    dev_holdout_overlap = sorted(set(dev) & set(holdout))
    duplicates = {
        "dev": sorted({value for value in dev if dev.count(value) > 1}),
        "holdout": sorted({value for value in holdout if holdout.count(value) > 1}),
    }
    valid = not overlap and not dev_holdout_overlap and not duplicates["dev"] and not duplicates["holdout"]
    payload = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "planned_seed_file": str(planned_path),
        "planned_seed_file_sha256": sha256_file(planned_path),
        "planned_dev": dev,
        "planned_holdout": holdout,
        "history_roots": [str(Path(value).resolve()) for value in args.history_root],
        "history_file_count": len(history_files),
        "history_files": history_files,
        "historical_seed_count": len(historical_seeds),
        "holdout_history_overlap": overlap,
        "dev_holdout_overlap": dev_holdout_overlap,
        "duplicates": duplicates,
        "valid": valid,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if not valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
