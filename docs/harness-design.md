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

## Comparator v0

At every decision boundary and after each applied action, compare a canonical
projection: zone contents per player by card name (hand/library/graveyard/
battlefield/exile), stack spell names, life totals, tapped status, phase/turn.
Divergence ⇒ dump both snapshots + the action history for triage.

Known noise sources to normalize: token names, DFC face names, card-name
formatting (`Æ`, accents), zone ordering, phased-out permanents.

## Validation loop in training later

Sample live self-play games (a % of games) through this same harness; a policy
that learns to exploit a sim bug will steer into it — sampling must follow the
current policy, not a fixed early one.
