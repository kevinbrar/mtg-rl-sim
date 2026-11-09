# Decisions

Dated decisions by Kevin (K) or Devin (D) that shape the project. Newest first.

- 2026-11-09 (K): Governance stack approved: XMage differential, full-suite test-count floor, mutation testing on changed lines, PR evidence template + lint, advisory fresh-context reviewer, decisions/postmortems as files. Replay corpus deferred ("don't overengineer"). No required human approver: Devin merges when required checks are green and escalates only on the triggers in `AGENTS.md` (Kevin "can be in the loop, not needed in the loop"). Global steering changes always reviewed by Kevin; project docs Devin edits directly.
- 2026-11-09 (K): Approved steering changes: mission/decisions/status files in the repo; read external rules before offering choices; correctness work needs an author-independent check; enforce invariants with CI. Success criteria for the project are still open.
- 2026-11-09 (K): Review, not writing code, is the bottleneck. Research governance/review literature and borrow what works; no process for its own sake.
- 2026-11-09 (K): Offer the phase-rs fixes upstream through one issue (phase-rs#10736) rather than PRs; PRs are ruled out by phase-rs's AI-contributor policy.
- 2026-11-09 (K): Devin's GitHub PAT is full access and may be used for anything on Kevin's behalf.
- 2026-11-08 (K): No upstream PRs until all unsupported meta cards are fixed. Engine changes are allowed, not just parser rules.
- 2026-11-08 (K): AI disclosure for upstream work: "Devin (Fusion mode)", underlying model unverified; rejection is acceptable.
- 2026-11-07 (K): Standard only. Contribute fixes back to phase-rs with an AI disclaimer, matching their conventions in good faith.
- 2026-11-07 (D, approved): Use phase-rs pinned at 988bc34 as the engine; XMage (via mage-bench) as the correctness oracle; local patches kept in `phase-patches/`.
- 2026-11-07 (K): Goal over method; check prior art before implementing anything non-trivial.
