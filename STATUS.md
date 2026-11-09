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
- CI: per-card XMage diffs remain fail-closed until the differential harness supports real meta decks.

## Governance / CI
- PR #1 merged at `2ddbe44` after its first run failed `fmt` (129323517036), `packet-lint` (129323517028), `test-full` (129323517027), `clippy` (129323516995), and `mutants` (129323516897); `xmage-diff` was pending. PR #2 carries the card-data download/verification, mutants gitlink, review-packet evidence/artifact, and patched-engine fmt/Clippy fixes.
- PR #2 run `42674684408` (head `4942c9b`): `fmt` (129352709046), `mutants` (129352709193), and `xmage-diff` (129352710014) passed; `clippy` (129352709381) and `test-full` (129352708981) failed at patch application because Git committer identity was unset; `packet-lint` (129352708729) rejected `.py`/`.yml` artifact paths. Workflow-dispatch run `42674232924` had `fmt` fail because the dated toolchain action ref was unavailable (129351690059); fixed in `4942c9b`.
- PR #2 run `42676376772` (head `728e76d`): `fmt` (129358720789), `test-full` (129358720255), `xmage-diff` (129358720679), `mutants` (129358720956), and `packet-lint` (129358720829) passed; `clippy` (129358721184) failed only on the accepted dead-code `Out` warning at `phase-env/src/main.rs:38`. Devin Review passed (no Actions job ID).
- PR #2 pull-request runs `42672336187` and `42673028298` failed with zero jobs; at inspection the PR was mergeable and `refs/pull/2/merge` existed, so the cause at those runs is unconfirmed.
- Patched card-data release: `card-data-34ab5f693`, SHA-256 `54ebe1c98e5d15de9d3e844845cb96a7cd87e3229957222616bec782827a8d6a`.
- XMage CI runs real mono-green smoke seeds 17 and 42 for `cards: none` engine-path changes; per-card diffs remain fail-closed until harness v1.
- The dead-code `Out` warning was removed in PR #2. Patched-engine Clippy also reports `expect_fun_call` in `kili_the_resourceful.rs:40` from patch 0018; it will surface on the next engine PR.
- Representative mutants measurement: 16 candidates; 4 missed, 1 timeout, 11 unknown after about 33 minutes. Log: `/home/ubuntu/ci-governance-worktrees/mutants-0017-tmpfs.log`.
- Branch protection is enabled on the public repository: all six required checks, `enforce_admins` on, no approvals required.

## Next
- Batched Python training environment over phase-env; then agent/training research.

## Dead ends
- Grouped upstream PRs: blocked by phase-rs scope + model-disclosure policy.
- mtg-kernel's XMage bridge: too specialized to reuse; mage-bench's bridge used instead.
