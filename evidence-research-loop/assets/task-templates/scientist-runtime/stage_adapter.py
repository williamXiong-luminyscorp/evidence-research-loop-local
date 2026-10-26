#!/usr/bin/env python3
"""Replace this safe-failing skeleton with real Scientist stage integrations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    stage = request.get("stage", "unknown")
    raise SystemExit(
        f"Scientist stage {stage!r} is not configured. Replace stage_adapter.py "
        "with a real model/search/experiment/analysis/review adapter; the template "
        "intentionally does not fabricate a successful response."
    )


if __name__ == "__main__":
    raise SystemExit(main())
