# Review and dev patterns when agents are in the loop

The author-agent's verdict on its own work carries almost no weight; human reading finds a minority of defects and only at ≤200–400 LOC/hour. Correctness must come from things that are neither author nor human: external oracles, deterministic gates, and tests whose inputs the author didn't pick. Human attention goes to intent, risk, and one slice where reading pays.

## 1. Find the oracle before building
Run for every task whose correctness matters; write the result in the brief; chosen oracles become merge gates before the first change ships.
1. User named an oracle? → mandatory; a result for every touched surface is part of "done"; if it can't reach the surface, extend it.
2. Reference implementation? → differential testing: same inputs, two systems, every disagreement a candidate bug. Stay inside the domain both support; "couldn't compare" is a third outcome, never agreement. Keep a known-divergence allowlist with rationale; its growth is a metric.
3. Spec/standard/model? → executable properties and state-machine tests driven by random sequences; shrink divergences.
4. Golden data or recorded behaviour? → snapshot tests, trace replay with per-step hashes, reconciliation totals.
5. Metamorphic relations? → same seed replays bit-identically; relabeling equivalents changes nothing; two optimisation levels agree.
6. External test suite (the reference's, a conformance suite, upstream's)? → run it.
7. Nothing applies → implicit oracles (no panic/hang/leak; fuzz) + property tests, then a human reading in an adversarial role. State "oracle: none" explicitly.
Own unit tests are welcome but never the oracle.

| Domain | Strong oracles | Don't count |
|---|---|---|
| Compilers / interpreters / rules engines | Differential vs reference; -O0 vs -O2; conformance suite; spec-derived models | Own unit tests; "smoke program runs" |
| Data pipelines | Dual-run old vs new and diff; reconciliation invariants (counts, sums, keys); golden outputs on a frozen sample | "Job completed"; schema check alone |
| UIs | Visual snapshots vs baseline; accessibility-tree assertions; e2e on recorded fixtures; design tokens as spec | "Looks right"; author component tests only |
| ML / RL | Reproduce a published number with the reference impl; seeded determinism; bootstrap CIs over ≥5 seeds; SPRT vs frozen baseline; sudden jump → suspect env bug first | Single-run win rate; "loss went down" |
| API / dependency migrations | Shadow traffic / dual-write diff; provider's contract tests; recorded request/response replay | Own mocks; "types compile" |
| Infra / config | Plan/dry-run diff vs expected; policy-as-code; idempotent re-run yields no diff | "Applied successfully" |

## 2. Evidence table (unit of review; lint its shape)
`claim | check (exact command) | result (exact count/value) | artifact link | independent of author? (Y/N, producer)`
Rules: a number or it didn't happen; a link or it didn't happen; a required "Not run" row ("none" if empty); ≥1 Y row for correctness-critical work; the author's tests, project audit tooling and smoke runs are N. Follow with divergence from brief and risk/rollback.

## 3. Fresh-context review (advisory)
Reviewer with no access to the author's session; gets brief, diff, evidence table, spec excerpt. Narrow role questions: "which brief property does this evidence not establish?", "which spec interactions do the tests not exercise?", "which rows claim independence falsely?" No style. Output is a comment, not a gate and not the sole independent row; prefer a different model family.

## 4. Test the tests
Coverage is not quality. Mutation-test changed lines: a surviving mutant means the author's test passes with the implementation wrong. Threshold, don't zero-chase. Property/state-machine tests remove author control over inputs; seed failures into a regression corpus. Both need deterministic tests: fix or quarantine flakes in the same PR, never rerun-until-green.

## 5. Deterministic gates vs probabilistic guidance
Mandatory procedure → required status check, no bypass (fmt, lint, full suite with count floor, oracle run, mutants, packet lint). Behavioural preference → prose. A mandatory step that lives only in prose will be skipped. Keep gates few, fast, mechanical; add one only for an incident, a named oracle or a measurable property; postmortems end in a new gate, not a new sentence.

## 6. Act vs escalate
Default: act, record, ship on green. Escalate only on (a) oracle vs implementation disagreement the spec can't settle; (b) same check failed 3× (or task recurred across 2 sessions) without a new hypothesis; (c) irreversible/public action new in kind; (d) fork in what is built or in success criteria; (e) only the human can do it. Packet: one-sentence decision, options + trade-offs, recommendation, evidence links, blast radius, default if unanswered. Only (c)–(e) block; (a)–(b) proceed on the recommendation marked pending. Never wait silently; never loop past the bound.

## 7. Human reading order
1. Evidence table: any correctness claim without a Y row? any "not run" that should have run?
2. Divergence from brief.
3. Risk: which single change would you bet is wrong, and does the evidence address it?
4. The ~200 highest-risk lines at inspection speed against the spec; tests before implementation.
5. Everything else: skim or skip; gates own it. Approving without reading = add a gate, not read faster.

## 8. Anti-patterns
Author as sole oracle · "I ran the tests" without command/count/link · subset reported as full suite · coverage as quality · AI review as merge gate · decisions only in chat · big-batch delivery · rerun-until-green · prompting the author harder instead of adding independence · single-run strength claims · reward jump treated as progress instead of a possible env bug.
