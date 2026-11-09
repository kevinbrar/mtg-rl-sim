# Status

_Updated 2026-11-09._

## State
- phase-rs pinned at `988bc34` + 30 local patches (`phase-patches/`, apply with `scripts/apply-phase-patches.sh`); all 18 unsupported Standard-meta cards supported; 29,417 engine tests pass. Evidence: `phase-patches/README.md`, `docs/debrief.md`.
- Differential harness (`driver/diff_game.py`) v0: lands, creature casts, all-or-none attacks, no blocks, no targets. 3 clean games with a mono-green test deck. Real meta decks are **not** supported yet.
- phase-rs web client runs locally on the patched engine (`docs/play.md`).
- Upstream offer: phase-rs#10736; patches public at kevinbrar/phase@standard-meta-fixes.

## In progress
- Extend the differential harness to real Standard meta decks containing the 18 fixed cards (targets, blocks, activated abilities, modal choices, mulligans).
- Governance: `AGENTS.md` + `docs/review-patterns.md` landed; required CI checks, PR template/lint, `oracle/known-diffs.toml`, and the first postmortem are implemented.
- CI: `xmage-diff` remains fail-closed until the differential harness supports real meta decks.

## Governance / CI
- PR #1 merged at `2ddbe44` after its first run failed `fmt` (129323517036), `packet-lint` (129323517028), `test-full` (129323517027), `clippy` (129323516995), and `mutants` (129323516897); `xmage-diff` was pending. PR #2 carries the card-data download/verification, mutants gitlink, review-packet evidence/artifact, and patched-engine fmt/Clippy fixes. CI job results: pending first green run.
- Patched card-data release: `card-data-34ab5f693`, SHA-256 `54ebe1c98e5d15de9d3e844845cb96a7cd87e3229957222616bec782827a8d6a`.
- Known red: `clippy` reports dead-code for `Out` at `phase-env/src/main.rs:38`; accepted until harness v1. Patched-engine Clippy also reports `expect_fun_call` in `kili_the_resourceful.rs:40` from patch 0018; it will surface on the next engine PR.
- Representative mutants measurement: 16 candidates; 4 missed, 1 timeout, 11 unknown after about 33 minutes. Log: `/home/ubuntu/ci-governance-worktrees/mutants-0017-tmpfs.log`.
- Branch protection is not enabled (private repository on GitHub Free); pending K.

## Next
- Batched Python training environment over phase-env; then agent/training research.

## Dead ends
- Grouped upstream PRs: blocked by phase-rs scope + model-disclosure policy.
- mtg-kernel's XMage bridge: too specialized to reuse; mage-bench's bridge used instead.
