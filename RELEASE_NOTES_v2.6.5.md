# Evidence Research Loop v2.6.5

ERL v2.6.5 corrects scope and entrypoint guidance after an independent review of v2.6.2–v2.6.4.

## Retained

- Production searches reject deterministic fixtures unless explicit test mode is enabled.
- Codex tasks validate login status before candidate generation.
- Codex usage records include input, cached, output, and reasoning token fields when available, without storing credentials.

## Corrected

- Skill scripts and ERL MCP are both supported entrypoints. A project must use only one writer at a time; local Skill execution is not a reason to prohibit MCP.
- Progress Canvas is optional presentation for long or visual searches. It is not produced or verified by the controller and is not a completion, audit, or acceptance gate.
- KPI guidance distinguishes the historical `c-0006` persistence configuration from a whole-product baseline or winner.
- The revealed 5-trace/915-command KPI holdout is explicitly regression data.
- Whole-product KPI comparisons must implement the same end-to-end scope and require actual composition, root integration, and frozen holdout evidence.
- README self-test counts now match the current suite.

## Claim boundary

This release changes execution guidance, KPI scope boundaries, version identity, and release evidence. It does not add a new KPI candidate, model call, employee study, independent KPI test, autonomous discovery result, or production-readiness evidence.

## Validation

The final self-test, file counts, skill-tree hash, and preserved raw output are recorded in `release-evidence-v2.6.5.json`.
