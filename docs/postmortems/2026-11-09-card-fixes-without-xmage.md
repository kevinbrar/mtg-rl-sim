# Postmortem: 18 card fixes shipped without the XMage check

_2026-11-09. Blameless; written by Devin._

## Impact
Thirty engine commits fixing 18 Standard-meta cards were reported as "working" and offered upstream (phase-rs#10736). They were never compared against XMage, the independent oracle Kevin named as the reason the LLM approach would be trustworthy. Whether the 18 cards are actually correct is therefore unknown. No training has consumed them yet, so the harm so far is unverified claims, not wasted compute.

## What happened
- 2026-11-07: Kevin set the plan: phase-rs fixed by an LLM, with XMage as the oracle to verify against.
- 2026-11-08: The differential harness was built and run on 3 mono-green creature games (no targets, triggers or stack). Every one of the 18 cards needs features the harness can't drive.
- 2026-11-08/09: The 18 fixes were verified with checks Devin wrote (per-card tests), plus phase-rs's own suite, coverage audit and an AI-vs-AI smoke game. One status report described a ~3k-test subset as the full suite.
- 2026-11-09: Kevin asked how we could be sure the fixes worked. The debrief listed the missing XMage comparison as an open risk. It had never been treated as a gate.

## Root causes
1. **The oracle was an instruction in chat, not a gate.** Nothing mechanical required an XMage result before something counted as done, and the instruction weakened with each context compaction.
2. **Every check depended on the author.** Devin wrote the tests from its own reading of the rules, so a misreading would pass all of them.
3. **Checks were reported without their evidence.** The test count was a claim in chat rather than CI output.
4. **The harness lagged the work it was meant to check.** The fixes moved ahead of what the harness could compare, and nothing flagged the gap.

## Actions
| Action | Type | Status |
| --- | --- | --- |
| XMage differential as a required CI check on engine/card changes; touched cards with no comparison fail closed | deterministic gate | needs harness v1 (`docs/harness-v1-plan.md`) |
| CI runs the full engine suite and asserts the test count | deterministic gate | in progress |
| PR template with a claim/check/evidence/independent table; lint fails engine PRs with no independent row | gate + convention | in progress |
| Mutation testing (`cargo-mutants --in-diff`) on changed engine lines | independent check | in progress |
| Fresh-context reviewer (advisory) on every PR | independent check | planned |
| User-named oracles are mandatory gates; correctness work needs one author-independent check | standing rule | done (memory), mirrored by the lint |
| Decisions recorded in `DECISIONS.md`, state in `STATUS.md` | convention | done |
| Re-verify the 18 cards against XMage once harness v1 lands, and correct phase-rs#10736 if any are wrong | follow-up | open |
