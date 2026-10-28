# Evidence Research Loop v2.6.3

ERL v2.6.3 restores the original Codex Skill execution path for Cursor and Codex, and fixes Codex login detection.

## Changed

- Cursor and Codex now run searches through Skill scripts, not MCP tools.
- Project layout documents both `.cursor/skills/evidence-research-loop/` and `.codex/skills/evidence-research-loop/`.
- `codex login status` is accepted when the logged-in message is written to stderr.
- Formal searches continue to require Codex authentication before candidate generation.
- MCP server source remains in the package but is not required for Skill-only use.

## Claim boundary

This release restores the original Skill-driven controller path. It does not prove global optimality, autonomous discovery quality, or production readiness.

## Validation

- Targeted Codex stderr-login and deterministic-guard regressions are included in the engine suite.
- Engine regressions: 48/48 passed.
- MCP server regressions: 9/9 passed.
- Composition MCP regressions: 3/3 passed.
- Total release regression: 60/60 passed in run `20261028T181649Z-5abe0b3d`.
- Skill tree: 77 files, SHA256 `6984bd9f76034e0eb80388ce22e3241de8abfd3fbc25519cb0b72d4c940baac3`.
- Original Skill+Codex restore on `python-unique-sort-optimization`: 1 model call completed, winner `c-0002-8c2e8f24`.
