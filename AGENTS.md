# AGENTS.md — mtg-rl-sim

Read `MISSION.md`, `DECISIONS.md`, `STATUS.md` first. CI enforces this file; if they disagree, fix CI in the same PR. General playbook: `docs/review-patterns.md`.

## Oracle: XMage (mandatory)
XMage via mage-bench (`oracle/README.md`) is the correctness oracle; Kevin named it, so it is a gate.
- Applies to any change under `phase-patches/`, `phase/`, `phase-env/`, `oracle/`, `driver/diff_game.py`, or card data. Such PRs carry a `cards:` line with semicolon-separated card names (`cards: none` only if no card logic is touched; the lint checks). Card names may contain commas.
- Per listed card, the card's `oracle/scenarios/<card>.toml` suite must pass in phase-rs and XMage (Forge where XMage lacks the card); scenarios are derived from Oracle text and official rulings. Triage every divergence in `oracle/known-diffs.toml` as `engine-bug | xmage-bug | unsupported` with rationale + link. `unsupported` is never a pass; list growth is tracked in `STATUS.md`.
- If the harness can't exercise a card, extending the harness is part of the task. Own tests, the engine audit tool and smoke games are not substitutes. If infeasible in this PR: don't merge, record the gap in `STATUS.md`, escalate per below.

## Required checks (`main`: required, no admin bypass, 0 approvers)
| Check | Fails when |
|---|---|
| `fmt` | `cargo fmt --check` / `ruff format --check` differ |
| `clippy` | `cargo clippy --workspace -D warnings` / `ruff` report anything |
| `test-full` | always compiles/runs Python tests; full patched engine suite and test-count floor on engine-path changes, non-PR events, or failed/empty scope detection |
| `xmage-diff` | untriaged divergence; `cards:` entry without a report artifact; `cards:` missing on an engine-path change |
| `mutants` | `cargo mutants --in-diff` survivors in engine/card logic above `ci/mutants-threshold.txt`; survivors posted as PR comment |
| `packet-lint` | missing template section; engine PR with no `independent: Y` row; "verified" claim without a link |
Flaky test → fix or quarantine in the same PR; never rerun-until-green.

## PR template
1. Brief/decision reference. 2. Surfaces & `cards:`. 3. Evidence table: `claim | check (exact command) | result (exact count) | artifact | independent? (Y/N, producer)` — own tests, engine suite, audit tool, smoke game = N; XMage diff, mutants, fresh-context review = Y. 4. Not run / not verified ("none" if empty). 5. Divergence from plan. 6. Risk & rollback (which training signal is corrupted if wrong; revert = drop the patch). One card/mechanic or one harness capability per PR (~≤400 LOC logic).

## Fresh-context review (advisory)
Engine PRs get a review from an agent that sees only the brief, diff, evidence table and Comprehensive Rules text for touched cards — never the author session. Roles: rules lawyer (untested interactions?) and XMage auditor (claims without an independent row?). Address or rebut each item before merge; never the sole independent row.

## Git
`devin/<ts>-<slug>` → PR → all checks green → Devin squash-merges. No human approver, no direct pushes to `main`, never bypass/delete a check. Red `main` → revert first. Submodule pinned at `988bc34`; engine changes are patches in `phase-patches/`.

## Done
All checks green + XMage row per listed card + `STATUS.md` updated in the same PR (numbers from CI artifacts) + decisions in `DECISIONS.md`. "All tests pass" alone is not done.

## Records
`DECISIONS.md`: dated K/D decisions with reasons; escalations as `D, pending K`, resolved in place. `STATUS.md`: every PR. `docs/postmortems/YYYY-MM-DD-<slug>.md` per "merged but wrong" event: impact, root cause, the new check added.

## Escalate to Kevin only on
(a) phase vs XMage disagree and the CR don't settle it; (b) same check failed 3 attempts without a new hypothesis; (c) public/irreversible action new in kind; (d) change to success criteria or scope; (e) logins/secrets. Decision packet: one-sentence decision, options, recommendation, evidence links, blast radius, default if unanswered. Only (c)–(e) block; (a)–(b) proceed on the recommendation as `D, pending K`. Everything else: decide, record, ship.

## Anti-patterns (seen here)
Card semantics verified only by checks you wrote/chose · reporting a check you didn't run, or a subset as the full suite (~3k ≠ 29.4k) · decisions only in chat · rerun-until-green, lowering a floor to merge, `unsupported` as a pass · waiting on Kevin outside (a)–(e), or looping past 3 attempts.
