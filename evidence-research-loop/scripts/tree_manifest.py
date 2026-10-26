#!/usr/bin/env python3
"""Write or verify a SHA-256 manifest for an evidence or release tree."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(root: Path, manifest_name: str) -> dict:
    files = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == manifest_name:
            continue
        files[relative] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return {"schema_version": 1, "algorithm": "sha256", "file_count": len(files), "files": files}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--manifest", default="release-manifest.json")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    path = root / args.manifest
    current = build(root, args.manifest)
    if args.write:
        path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"written": str(path), "file_count": current["file_count"]}, indent=2))
        return
    if not path.is_file():
        raise SystemExit(f"manifest missing: {path}")
    expected = json.loads(path.read_text(encoding="utf-8"))
    if expected != current:
        changed = sorted(key for key in set(expected.get("files", {})) | set(current["files"])
                         if expected.get("files", {}).get(key) != current["files"].get(key))
        print(json.dumps({"valid": False, "changed": changed}, indent=2))
        raise SystemExit(1)
    print(json.dumps({"valid": True, "file_count": current["file_count"],
                      "manifest": str(path)}, indent=2))


if __name__ == "__main__":
    main()
