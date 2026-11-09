# Mission

## Goal
A Standard-only MTG agent trained by self-play RL that plays beyond human level, on a fast Rust simulator (phase-rs) whose correctness is checked against XMage.

## Success criteria
- **Engine correctness:** TBD (Kevin is deciding). Working proposal: < 1 mismatch per 1,000 decisions in the phase-rs vs XMage differential on real Standard meta decks, with every disagreement triaged.
- **Agent strength:** TBD. Working proposal: an Elo ladder of fixed opponents (random, phase-rs AI per difficulty, frozen earlier agents, mage-bench LLM players, Kevin's own games); "superhuman" = beats the strongest available opponents and strong humans by a statistically clear margin (SPRT / confidence intervals).

## Non-goals
- Formats other than current Standard; cards not played in the Standard meta.
- Drafting / deck building (parked; see the ideas repo).
- Getting patches merged into phase-rs (courtesy only; phase-rs#10736).

## Autonomy envelope
- Devin decides technical approach, libraries, repo structure and makes engine changes without asking.
- Devin merges PRs itself when required checks are green; no human approver. Kevin can be in the loop but is not needed in it: escalation triggers and packet format are in `AGENTS.md`.
- Named oracle: XMage (mage-bench). Every correctness-critical change needs one check Devin did not write; the XMage differential is mandatory for engine/card changes (see `AGENTS.md`).
