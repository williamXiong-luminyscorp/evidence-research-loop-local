from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deployment" / "release-manifest.json"
EXCLUDED_TOP = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".data",
    ".local-data",
    ".all-test-tmp",
    "test-output",
}
EXCLUDED_PREFIXES = (
    ".container-data",
    ".server-test-",
    ".test-",
    ".wrapper-test-",
    ".installed-v242-test",
    "pytest-cache-files-",
    "server-pytest-",
    "test-output-",
    "review-test-",
)
EXCLUDED_PART_PREFIXES = (".tmp-", ".test-")
EXCLUDED_PART_NAMES = {".tmp"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files() -> dict[str, dict[str, int | str]]:
    result: dict[str, dict[str, int | str]] = {}
    for path in sorted(item for item in ROOT.rglob("*") if item.is_file()):
        relative = path.relative_to(ROOT)
        if relative.as_posix() == "deployment/release-manifest.json":
            continue
        if relative.parts[0] in EXCLUDED_TOP or relative.parts[0].startswith(EXCLUDED_PREFIXES):
            continue
        if any(part in EXCLUDED_PART_NAMES or part.startswith(EXCLUDED_PART_PREFIXES)
               for part in relative.parts):
            continue
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        result[relative.as_posix()] = {"size": path.stat().st_size, "sha256": sha256(path)}
    return result


def build() -> dict:
    entries = files()
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 1,
        "algorithm": "sha256",
        "file_count": len(entries),
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
        "files": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    observed = build()
    if args.write:
        OUTPUT.write_text(json.dumps(observed, indent=2) + "\n", encoding="utf-8")
    if args.verify:
        expected = json.loads(OUTPUT.read_text(encoding="utf-8"))
        if expected != observed:
            raise SystemExit("release manifest mismatch")
    print(json.dumps({key: observed[key] for key in ("file_count", "tree_sha256")}))


if __name__ == "__main__":
    main()
