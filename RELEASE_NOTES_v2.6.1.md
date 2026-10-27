# Evidence Research Loop v2.6.1

ERL v2.6.1 is a focused reliability release for the outer evolution controller.

## Fixed

- A timeout or launch error at the transition from search to `freeze` is now recorded as `freeze_controller_error` and returned as `holdout_incomplete` instead of escaping as an uncaught exception.
- Resuming after that incomplete freeze retries the missing freeze and holdout work without deleting the original failure event.
- The wall-time regression now measures the declared search budget through the recorded generator timeout and `accumulated_active_sec`; one-time protocol initialization and baseline calibration remain outside that budget.
- Windows tests continue to use an explicit workspace-local `ERL_TEST_TMP` so restricted system temporary directories do not create false functional failures.

## Claim boundary

This release corrects a state-machine boundary and its regression coverage. It does not add evidence for autonomous discovery quality, cross-domain generalization, formal proof, or production readiness.

## Validation

- Targeted wall-time and freeze-recovery regressions: 2/2 passed.
- Engine regressions: 45/45 passed.
- MCP server regressions: 9/9 passed.
- Composition MCP regressions: 3/3 passed.
- Total release regression: 57/57 passed in run `20261027T223417Z-4ef73b9d`.
- Skill tree: 76 files, SHA256 `d905a27b5f17850f475227b4934bcf8c86cb0cde2aa9ade2bb3c5cab552071a1`.

The earlier full run `20261027T222901Z-38e640ff` remains preserved. It exposed two normal verifier launches exceeding an overly narrow 0.5-second test-fixture timeout under Windows load. The fixture limit was raised to 2 seconds while its dedicated timeout case still sleeps for 4 seconds.
