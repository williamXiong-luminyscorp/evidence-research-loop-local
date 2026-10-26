#!/usr/bin/env python3
"""Safe-failing verifier skeleton; replace it with a frozen domain verifier."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    result = Path(args.result)
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text(json.dumps({
        "hard_pass": False,
        "node_id": args.node,
        "reason": "No domain verifier is configured; the template refuses acceptance.",
        "artifact_received": Path(args.artifact).name,
    }, indent=2) + "\n", encoding="utf-8")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
