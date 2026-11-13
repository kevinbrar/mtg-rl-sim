# Differential harness design

Goal: prove (or falsify) that phase-rs plays the Standard meta card pool correctly,
by comparing it against a second engine on identical games and explicit scenarios.

## Pieces

- **phase side:** `phase-env` (in repo). JSONL stdin/stdout. Each decision emits
  `waiting_for` label, semantic owner, authorized actor, the engine-issued
  `AiDecisionContract` candidate list, and a compact state snapshot
  (zone contents by name, life, tapped, stack, mana-ish). Chosen index is
  re-validated by `apply_ai_action_proposal` — the same path phase-llm uses.

- **XMage side:** [mage-bench](https://github.com/GregorStocks/mage-bench) (vendored
  XMage 1.4.61V1 + Java bridge exposing MCP tools over HTTP). Relevant tools:
  `get_action_choices` (indexed legal choices + board summary, optional blocking
  `until`), `choose_action`, `pass_priority`, `get_game_state`, `get_game_log`.
  `sleepwalker` is a reference MCP client. Decks come from `.dck` files in the
  per-player config.

## The determinism problem

A cross-engine replay needs identical shuffles. XMage's RNG isn't externally
seedable. Two complementary approaches:

1. **Identical-card decks.** If each library is N copies of the same card(s),
   shuffle is a no-op and both engines see the same hand/draw sequence. Illegal
   under deck-construction rules but engines don't enforce legality on load.
   Gives true whole-game replay: drive the same decision sequence in both
   engines, compare state after every action.
2. **Scenario tests (the real workhorse).** XMage's `Mage.Tests` harness builds
   explicit states (card X in hand/battlefield/graveyard, Y mana available) then
   executes actions and asserts outcomes — deterministic by construction. Port
   meta-pool cards through auto-generated scenarios: "cast C, let resolve",
   "activate C's ability", "attack/block with C", plus targeted interaction
   pairs. phase-rs has a matching scenario API (`game/scenario.rs`).

## Scenario mana payments

Fixtures must make every scripted mana payment forced, so neither engine has
to choose between interchangeable lands. Avoid supplying multiple lands of the
same type when a script taps only some of them; use distinct colors where the
spell costs allow it. This keeps tapped-state comparisons about game behavior,
not auto-payment tie-breaking.

## Scenario activation

Scripts can activate a battlefield permanent with
`{ action = "activate", source = "label", ability = "{cost}:" }`; `targets` is
an optional list of setup labels. The source and targets resolve only through
those labels. Both runners select exactly one non-mana activated ability whose
normalized description starts with the cost prefix; zero, multiple, or missing
descriptions fail closed. Phase submits `ActivateAbility` with automatic cost
payment and handles target-selection prompts like casts. XMage uses its regular
`activateAbility` helper, not the separate `manaActivate:` path.
XMage applies `tapped = true` battlefield fixtures during upkeep, after its
opening untap step, so the precombat-main state matches the Phase fixture.

## X casts and triggered-ability targets

`cast` accepts an optional non-negative `x` value. Both runners require it for
spells with X in their mana cost and reject it for spells without X; there is no
default. Phase passes the value to the cast builder. XMage queues `X=<n>` for
TestPlayer's X-announcement prompt. An X=0 spell that creates no target prompt
does not need a `trigger_targets` action.

`trigger_targets` queues one nonempty group of battlefield labels in FIFO
order. Queue a group before the cast or other action that causes its trigger to
go on the stack. The next triggered-ability target prompt consumes exactly one group.
Phase rejects an empty queue, illegal labels, groups with too few required or
too many targets, and groups left when the script ends. XMage uses one
`addTarget` entry per group and strict choose mode fails on missing or illegal
targets; the runner also rejects unconsumed target entries at script end.
Optional target slots may be left unselected when the prompt allows it.

`choose_cards` queues one group of setup labels for each card-selection prompt.
The group must be queued before the action that causes the prompt. Phase
consumes it at the real `ChooseFromZoneChoice` prompt: an empty group declines
an `up_to` choice, while a nonempty group must contain only offered cards and
respect the prompt's minimum and maximum. Anzrag's singular exiled-card wording
parses to a tracked `ChooseFromZone` choice followed by one `ChangeZone`, so
the runner does not synthesize a decision at `OptionalEffectChoice`. XMage
continues to use its native card-selection prompt. Both runners use only the
FIFO head and fail on an empty queue, unoffered labels, too many selections, or
leftover groups.

## Multi-turn scenario scripts

`end_turn` advances to the next turn's precombat main when a later action is
scripted, passing through untap, upkeep, and draw and resolving triggers along
the way. If `end_turn` is the final action, both runners stop at the next
turn's Upkeep, after untap and before its draw step. XMage schedules this at
`UPKEEP`; its previous `UNTAP` stop left permanents tapped, unlike Phase's
first exposed state after the untap step.

XMage automatically orders a trigger group only when every trigger has the
same source and rule text; any other group fails with its pending triggers
listed. Strict choose mode remains enabled. Phase drains every pending
trigger-order prompt by identity index, without an identical-only guard.

## Comparator v0

At every decision boundary and after each applied action, compare a canonical
projection: zone contents per player by card name (hand/library/graveyard/
battlefield/exile), stack spell names, life totals, tapped status, phase/turn.
Divergence ⇒ dump both snapshots + the action history for triage.

Battlefield object projections include sorted `types`, `subtypes`, and `colors`
arrays. Type and subtype spelling is normalized by capitalization; colors use
the single-letter W/U/B/R/G notation. These fields participate in exact
projection comparison.
Battlefield reach assertions may include `counters = { name = count }`; counter
names use the projection's canonical spelling, and an absent counter counts as
zero.

Known noise sources to normalize: token names, DFC face names, card-name
formatting (`Æ`, accents), zone ordering, phased-out permanents.

## Digest-pinned known differences

`scripts/run_card_scenarios.py` requires `--known-diffs`. Each `[[diff]]` entry
identifies one exact scenario by suite stem and scenario name, and pins the
SHA-256 of each engine's projection after the comparator's unordered-zone
normalization. A divergence is triaged only by exactly one matching entry in
category `engine-bug` or `xmage-bug` with both digests unchanged. `unsupported`
never waives a divergence. Matching scenarios with an entry are stale and fail;
duplicates, unknown scenarios in a run suite, malformed entries, and digest
drift also fail closed. Entries for suites not run are ignored. The report
artifact lists matched, triaged (including category and card), and failed
scenarios and counts. This follows strict `xfail` semantics: once a difference
disappears, remove its entry. CI compares base and head registry entries as
whole tables; every added, edited, or removed entry requires its card in the
packet's `cards:` list. The base reader accepts legacy entries with only a
non-empty string `card` field.

## Validation loop in training later

Sample live self-play games (a % of games) through this same harness; a policy
that learns to exploit a sim bug will steer into it — sampling must follow the
current policy, not a fixed early one.
