# Evidence Research Loop v2.6.4

ERL v2.6.4 makes a progress Canvas part of every search, and records Codex token usage without storing secrets.

## Changed

- Skill requires a progress board on every search: status numbers, flow checklist, budget bars, engine table, lineage DAG, score line, and token ledger.
- Layout samples live in `evidence-research-loop/assets/examples/`.
- Agents write a Cursor canvas beside chat and a Git copy at `$project/progress.canvas.tsx`.
- Each Codex call appends `codex-token-usage.jsonl` (input/output/cached/reasoning tokens). Access tokens are never recorded.
- Audit summaries include the extra usage fields when present.

## Claim boundary

This release standardizes how search progress is shown and how token counts are logged. It does not prove global optimality, canvas export quality, or production security of any login or packing example.

## Validation

- Engine controller regressions include Codex usage parsing and token-ledger deduplication.
- Full suite results are recorded in `release-evidence-v2.6.4.json`.
