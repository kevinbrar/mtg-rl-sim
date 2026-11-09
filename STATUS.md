# Status

_Updated 2026-11-09._

## State
- phase-rs pinned at `988bc34` + 30 local patches (`phase-patches/`, apply with `scripts/apply-phase-patches.sh`); all 18 unsupported Standard-meta cards supported; 29,417 engine tests pass. Evidence: `phase-patches/README.md`, `docs/debrief.md`.
- Differential harness (`driver/diff_game.py`) v0: lands, creature casts, all-or-none attacks, no blocks, no targets. 3 clean games with a mono-green test deck. Real meta decks are **not** supported yet.
- phase-rs web client runs locally on the patched engine (`docs/play.md`).
- Upstream offer: phase-rs#10736; patches public at kevinbrar/phase@standard-meta-fixes.

## In progress
- Extend the differential harness to real Standard meta decks containing the 18 fixed cards (targets, blocks, activated abilities, modal choices, mulligans).
- Governance: `AGENTS.md` + `docs/review-patterns.md` landed; CI checks (`fmt`, `clippy`, `test-full`, `xmage-diff`, `mutants`, `packet-lint`), branch protection, PR template, `oracle/known-diffs.toml`, first postmortem (18 cards / no XMage) still to implement.
- CI: apply patches + quick phase-vs-XMage game.

## Next
- Batched Python training environment over phase-env; then agent/training research.

## Dead ends
- Grouped upstream PRs: blocked by phase-rs scope + model-disclosure policy.
- mtg-kernel's XMage bridge: too specialized to reuse; mage-bench's bridge used instead.
