# Evidence Research Loop v2.6.2

ERL v2.6.2 prevents deterministic fixtures from being mistaken for autonomous AI search.

## Changed

- Normal searches now reject the `deterministic` generator.
- Infrastructure tests may still use deterministic fixtures only when the task declares
  `generator.test_only: true` and the process explicitly sets
  `ERL_ALLOW_DETERMINISTIC_TEST_MODE=1`.
- `codex` tasks now run `codex login status` during validation and stop before candidate
  generation when authentication is unavailable.
- Authentication errors tell the user to log in privately with an access token or API key;
  tokens must not be stored in `task.json`.
- Windows self-tests now place temporary fixtures under an explicit writable test root,
  including installed repositories whose parent directory is not writable.

## Claim boundary

This release enforces the distinction between fixture-driven controller regression and
AI-generated search. It does not prove that a selected candidate is globally optimal, that
the configured model is capable of autonomous discovery, or that a simulated winner is
production-ready.

## Validation

- Deterministic production guard and Codex authentication regressions: 2/2 passed.
- Engine regressions: 47/47 passed.
- MCP server regressions: 9/9 passed.
- Composition MCP regressions: 3/3 passed.
- Total release regression: 59/59 passed in run `20261028T165435Z-f329d0fb`.
- Skill tree: 76 files, SHA256
  `00da2a0d9ed0a2060700f63241257633f3d2c0d7c2c5847e98b4f2172175c29c`.

The earlier full attempt `20261028T164334Z-17771b0a` remains preserved. Its engine and
server suites passed, while three Composition MCP tests failed before execution because
their fixture root resolved to the protected `C:\Users` directory. The fixture root was
made installation-safe and all three tests passed in the final run.
