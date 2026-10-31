# Evidence Research Loop v2.6.6

ERL v2.6.6 adds an optional, evidence-bounded multi-perspective Codex adapter for
Scientist Runtime and tightens the boundary between research synthesis, design,
proposed validation, and empirical results.

## Added

- `codex_panel_adapter.py`, an opt-in reference adapter that runs configured
  specialist roles and a synthesis call for each Scientist stage.
- Strict stage schemas, frozen-input path and size boundaries, an absolute model
  call ceiling, and per-call prompt/event/response records.
- A generic panel configuration template and five deterministic regression tests,
  including durable budget accounting after a failed or interrupted call.

## Corrected research semantics

- Local observations, literature synthesis, unimplemented design inference,
  proposed protocols, and executed empirical results must be labeled separately.
- Panel agreement, desk scores, and schema completeness do not constitute
  empirical evidence.
- Non-zero architecture scores require a criterion-to-source-to-interface ledger.
- A review-requested correction creates a new plan version while preserving the
  rejected plan and evidence.

## Claim boundary

This release provides reusable research execution and evidence controls. It does
not establish that a proposed domain architecture works in production, that panel
roles are independently necessary, or that model consensus is true. Domain data
is not bundled with this release.

## Validation

The release self-test runs 55 engine regressions, 9 MCP service regressions, and
3 Composition MCP regressions (67 total) without model calls. Exact results and
the bundled skill-tree hash are recorded in `release-evidence-v2.6.6.json`.
