# Postmortem: `phase-env` changes skipped by `test-full`

_2026-11-10. Blameless; written by Devin._

## Impact

PR #6 changed `phase-env/src/main.rs`, but its pull-request `test-full` run classified the scope as Python-only. [Run 42783871211](https://github.com/kevinbrar/mtg-rl-sim/actions/runs/42783871211), job `129720155028`, logged:

```text
mode=python-only
reason=no-engine-paths-changed
##[notice]Engine suite skipped: no engine paths changed
Changed files:
...
phase-env/src/main.rs
```

The required check passed after running 115 Python tests, without the engine suite. The subsequent [main push run 42787939150](https://github.com/kevinbrar/mtg-rl-sim/actions/runs/42787939150), job `129733778898`, used `reason=non-pull-request-event`, ran the full engine suite, and passed 29,417 tests against a 29,400 minimum.

PR #11 also touched `phase-env/`. Its first run at `8b35451`, [run 42905584696](https://github.com/kevinbrar/mtg-rl-sim/actions/runs/42905584696), job `130142611022`, likewise logged `mode=python-only`, `reason=no-engine-paths-changed`, and `Engine suite skipped: no engine paths changed`, despite listing `phase-env/src/main.rs`. After adding `phase-env/` to the scope regex, PR #11 run [42910884841](https://github.com/kevinbrar/mtg-rl-sim/actions/runs/42910884841), job `130160922557`, ran the full suite and passed 29,417 tests. Thus the final PR #11 gate was corrected before merge; the pre-merge scope miss remained on PR #6.

## Root cause

The `test-full` engine-path regex in `.github/workflows/ci.yml` was a hand-written list that omitted `phase-env/`, although `AGENTS.md` lists `phase-env/` as an oracle-gated path. Its Python-only branch therefore treated the changed Rust environment as a harness-only change.

## New check

`tests/test_ci_engine_scope.py` reads the scope regex from the workflow and checks the engine paths that require the full suite. It also asserts the intended separation: `oracle/` and `driver/diff_game.py` are covered by `xmage-diff`, not the `test-full` engine suite. The test also rejects accidental matches for `driver/test_diff_game.py` and `docs/`. It is collected by CI's `python -m pytest driver tests -q` step.
