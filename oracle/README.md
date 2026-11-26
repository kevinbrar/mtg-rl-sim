# XMage oracle (mage-bench)

## Forge differential oracle

Forge is the independent oracle for Standard cards absent from the pinned
XMage/mage-bench source. The runner currently uses it for Extrapolate the
Impossible, Jace's Machinations, Seasoned Cryomancer, and Uldaros Theorix. The pin is recorded
in `oracle/forge.txt`:

- Repository: `https://github.com/Card-Forge/forge`
- Commit: `0f82b4f4693357638c368b9fb0b64833eb44faf8`

`scripts/run_forge_scenarios.sh` creates a blobless, shallow sparse checkout,
applies `oracle/forge-patches/*.patch` in sorted order, and runs the headless
`CardScenarioProjectionTest` in `forge-gui-desktop`. The Java test consumes the
same JSON spec as the XMage runner and emits the same projection schema.
`FORGE_CHECKOUT` selects the fresh checkout path; `FORGE_MAVEN_REPO` selects an
isolated Maven local repository so Forge artifacts do not pollute `~/.m2`.

The selected Maven reactor is `forge-gui-desktop -am` (plus its core/game/AI/GUI
dependencies). `-Denforcer.skip=true` is required by the available Forge
environment: its root POM requires Maven 3.8.1, while the provisioned Maven is
3.6.3. The isolated settings file uses the same Google-hosted Central mirror as
the feasibility spike.

Scenario suites declare `oracle = "forge"` at the top level; omitted values
default to XMage. CI caches the isolated Maven repository using the Forge SHA
and the sorted Forge patch hashes, then requires both the Forge Surefire report
and the shared known-diffs report artifact.

Known Forge prompt-shape bugs use the `forge-bug` category. These entries pin
the Phase projection and a structured Forge prompt-failure event. The runner
records the event without answering it; it is counted as triaged rather than
matched.

Both scenario runners support a `setup.P0.sideboard` zone. Label an entry as
`{ label = "sideboard.card", card = "Card Name" }`, then use
`choose_outside_game` with those labels for the revealing player's and
opponent's choices. Choices must match the offered prompt and every scripted
choice must be consumed; a missing or ineligible selection fails closed.
Tournament "outside the game" choices are represented by the sideboard.

Forge projects ordered libraries, exile contents, and each permanent's
characteristics and counters. Both runners support `pass_priority`,
`label_permanent`, `activate`, `choose_surveil`, `choose_scry`,
`choose_discard`, and `trigger_targets`.
`choose_scry` answers one scry: `cards` lists the labels put on the bottom of
the library, and the other looked-at cards stay on top in their current order
(`cards = []` keeps everything). Neither order is scriptable: kept cards and
bottomed cards both keep the order in which they were looked at, whatever
the order of `cards`. A scry with no cards to look at takes no entry.
A scry with no `choose_scry` entry fails in both runners, a `choose_scry`
entry cannot answer a surveil (or the reverse) in Phase (Forge keeps separate
queues and does not check the order between the two), and the loader
rejects `choose_scry` outside Forge-oracle suites because the XMage runner
does not implement it. In Phase, a resolution step ends only at a priority
window or a finished game; any other prompt left open fails the scenario.
A `play_land` action may answer the land's "as this land enters" prompts:
`land_type` names the basic land type and `pay_life` (a boolean) accepts or
declines an optional life payment. An answer nothing asked for and a prompt
with no answer both fail. Phase and XMage only; `behold` cannot be combined
with them. When a player has lost, the game is over (CR 104.1, 104.2a) and
both runners project the loser as `life` and `lost` only and leave cards the
loser owns or controls out of `battlefield` and `exile`, since what an engine
does with the loser's cards after the game is not game state. `game_over` is
added as the engine reports it; the rest of the projection is unchanged. The loser's board is therefore not
compared. The
reach check `{ player = "P0", lost = true }` asserts who lost (XMage suites).
An `attack` action may set `through = "damage"` to continue past declare
attackers, declare blockers, and combat damage, resolving scripted triggers
before stopping with an empty stack and priority at the active player. Omitted
`through` preserves the existing declare-attackers stop. The current script
declares no blockers unless `blocks = [{ blocker = "<P1 label>", attacker =
"<attacking label>" }]` is given (Phase and XMage only; requires `through =
"damage"`; each blocker blocks one attacker), and unscripted combat
or damage-assignment prompts fail closed. When several replacement effects
apply to one event (CR 616.1), script one
`{ action = "choose_replacement", player = "P0", source = "<label>" }` per
prompt before the `attack` or `pass_until_resolved` that reaches it: a
per-player FIFO naming the permanent whose replacement effect that player
applies next. Phase and XMage only; a prompt with no entry, a source that is
not offered, and an unconsumed entry all fail. A replacement choice that opens
while a spell is cast or an ability is activated (for example a sacrifice
cost) is not scripted yet. An explore's "may put that card into their
graveyard" (CR 701.44a) is scripted with `choose_optional`: `"yes"` puts the
revealed card into the graveyard, `"no"` leaves it on top. Trigger targets, card choices, and
other supported resolution choices use the same FIFO handlers during combat
damage, and the runner stops only after regular combat damage is complete.
A `cast` of a spell with kicker (CR 702.33) carries `kicker = true` or
`kicker = false` (XMage suites only). XMage fails a kicker prompt with no
answer; Phase fails `kicker` on a spell without the keyword. It cannot be
combined with `x`, `sacrifice`, `alternative_cost` or `expect`. A refused
`cast` with no `targets` also fails when Phase lists the cast as a legal
action and only rejects it on submit: the cast must not be offered at all.
Activations can name a labeled battlefield permanent or a card in the
activating player's graveyard. Activation labels can match a unique
loyalty-cost label (for example, `-3` or `+2`) or an ability description;
missing and ambiguous matches fail closed. `choose_discard` is a per-player
FIFO of labeled cards, validated against the discarding player's offered cards
and the prompt's count. Missing, extra, duplicate, ineligible, and
wrong-player choices fail closed. For a multi-card discard, Forge uses the
scripted selection order for the CR 404.3 graveyard-order choice and fails
closed if that prompt is missing or unexpected. Use `reach.label` with a battlefield
assertion to check a specific same-named permanent's state. Forge token
projections and reach checks strip exactly one trailing ` Token` from token
names to match XMage's convention; Phase token names are unchanged. Forge also
supports `choose_replacement` by exact description and `choose_entity` by
unique exact card name. A sole legal replacement is selected deterministically;
if a matching scripted replacement is queued, that entry is consumed. Multiple
replacement candidates require an exact scripted selection. These scripted
choices are FIFO and fail closed when absent, ambiguous, ineligible, or
unconsumed.

`choose_cards` also scripts optional card picks from looked-at or revealed
cards: an empty group declines, and a non-empty group accepts and selects the
listed card(s). XMage maps `LookLibraryAndPickControllerEffect`'s `chooseUse`
prompt from the acting player's next card-choice group, and a non-empty group
is consumed by its subsequent `TargetCard` selection; Phase maps the
corresponding `DigChoice` from that group. Neither runner consumes a
`choose_optional` answer for this decision. XMage's
`isLookLibraryAndPickPrompt` hook currently treats every `chooseUse` on an
ability containing `LookLibraryAndPickControllerEffect` as this pick; an
ability with a second independent "you may" decision needs a narrower match.
When no looked-at card matches, script no `choose_cards` group because no choice exists (CR 701.20e, 608.2c); XMage skips its prompt, and Phase auto-answers a `DigChoice` only when its selectable set is empty.

`choose_optional` queues non-empty FIFO answers of exact `"yes"` or `"no"` for
the acting player when a decision has no associated card-choice group, such as
whether to cast an exiled card. An optional `source` label binds the answers to
that prompt's source; a mismatch fails closed without consuming the answer.
Phase consumes them for other `OptionalEffectChoice` and `OpponentMayChoice`
prompts; XMage maps them to `chooseUse`'s literal `Yes`/`No`. Missing,
wrong-player, wrong-source, and unconsumed answers fail closed. Forge rejects
this action as unsupported.

In Phase and XMage, `trigger_targets` queues one labeled target group per
triggered-target prompt. It may be scripted before `attack` when a delayed or
beginning-of-combat trigger asks for targets while the runner advances to
declare attackers. Use `@skip` only for an optional target slot; required
targets, illegal labels, and unconsumed groups fail closed.
`order_triggers` (`player`, `resolve`) scripts the order of non-identical
simultaneous triggers (CR 603.3b) in Phase and XMage. `resolve` lists
ability-text prefixes, first to resolve first, matched against the lower-cased
rules text with the source written as `~`. Prefixes are matched from the last
to resolve backwards, each against the triggers not yet placed. Identical
triggers need no entry; any other unscripted order, a wrong count, an unmatched
or ambiguous prefix, and an unconsumed entry fail closed. `only = "xmage"` or
`only = "phase"` marks a prompt that just one engine asks; the other engine
skips the entry, and each use must be backed by a `STATUS.md` entry. Phase offers a
delayed triggered ability without ability text; the entry `@unlabeled`
(`only = "phase"`) matches such a trigger, and fails as ambiguous when the prompt holds two of them. Forge rejects this action as unsupported.
`reach.damage` checks marked damage on a battlefield permanent; Feral F5
asserts that the target receives damage equal to the source's power.

Uldaros Theorix scenarios use `graveyard_targets` to choose up to one card for
each card-type slot, `choose_copies` to select which copies to cast and their
order, and `copy_targets` to script targets for copied spells. Both runners
validate these player-scoped choices against the offered prompts and fail
closed on missing, extra, ineligible, wrong-player, or unconsumed entries.
Forge's settle point processes state-based actions and waiting triggers, then
requires an empty stack and simultaneous-entry queue with priority returned to
the active player before projecting.

Phase can resolve exact-count forced discards without presenting `DiscardChoice`.
If a discard script is pending, the runner reconciles it against the `Discarded`
events captured for that stack resolution and consumes it only on exact set
equality; mismatches report both sets, while a resolution with no discard leaves
the script unconsumed. Local card-data runs must use the CI-pinned release in
`ci/card-data.txt`; after an undo build, restore or regenerate card data from the
fully patched parser before running normal differentials.

`choose_mana` accepts shared `sources`, or both `phase_sources` and
`forge_sources` when the engines validly spend different equivalent sources.
Each runner checks its exact scripted set for casts and activations; both
oracle-specific lists are required, and choices left unconsumed fail closed.

Cast `targets` accepts setup labels, `@skip` for an optional target slot, and
the reserved player tokens `@P0` and `@P1`. Player tokens name the scenario's
two players and must match a legal player target; unknown, illegal, or missing
targets fail closed in Phase, XMage, and Forge.
`cast.from` accepts `hand`, `graveyard`, or `exile`. Graveyard and exile casts
identify the exact setup-labeled card, and the selected engine must offer that
object as a legal cast from the requested zone.

`expect = "refused"` is valid on `cast` and `activate` steps and, in
Forge-oracle suites, on `choose_copies` and `trigger_targets` (see below). On
`cast` and `activate` the runners
preserve normal label, priority, zone, and ability preconditions, attempt the
action through the engine, and require the state projection to remain unchanged
when the engine declines it. Accepted actions and refusals that change state
fail the scenario. Refused script indices are emitted as `refusals` and must
match exactly across Phase and the selected oracle. A refused action cannot
consume a preceding `choose_mana` script or an inline choice script.
Hand casts select the first card with the matching name, including expected-
refused casts, because same-named cards in hand are interchangeable. Graveyard
casts use setup labels to identify the specific object; ordinary graveyard casts
retain the original setup-zone validation.

On `choose_copies` and `trigger_targets`, `expect = "refused"` marks the last
entry of `cards` / `targets` as a pick the engine must refuse after the earlier
entries were accepted; the earlier entries are then applied as the real choice.
Phase submits the whole `choose_copies` selection, requires the engine to
reject it with the prompt and projection unchanged, and then submits the
accepted prefix. For `trigger_targets`, Phase requires that no further target
slot offers the refused target. Forge requires that the refused card is not
offered by the next copy prompt (or that the effect stops asking and the card
stays in exile), and that the refused target exceeds the ability's target
maximum or is not a legal target. An entry that the engine would accept fails
the scenario. The refused entry must also be legal on its own, so that the
earlier entries are what make it illegal: Phase requires a cloned state to
accept the refused card alone and the first target slot to offer the refused
target; Forge requires the first copy prompt to offer the refused card and the
ability to accept the refused target before any target is chosen. A refused
step therefore needs at least one accepted entry, no `@skip`, and no repeated
entry. Limits: the checks assume an "up to N of one kind" prompt (Phase looks
for the refused target in the first slot), "legal on its own" means
selectable or offered, not proven castable, a player (`@P0`/`@P1`) cannot be
the refused entry, and in Forge the refused target must be a permanent that
started on the battlefield (a label for a card that changed zones during the
script fails closed). The script index is recorded in `refusals` like an action-level
refusal. The XMage runner does not implement this, so the loader rejects it
outside Forge-oracle suites.

During an expected-refusal attempt only, a card-selection prompt raised while
paying a recognized monotone cost may be answered with every legal candidate
up to the prompt's maximum. Phase recognizes its `CollectEvidenceChoice`
prompt; XMage recognizes Collect Evidence and exile-from-graveyard costs; Forge
recognizes fixed-count graveyard-exile costs. Each automatic answer is logged
and recorded in `refusal_cost_prompts`, which is evidence only and is not
compared across engines. The action still counts as refused only when the
engine declines it and the projection is unchanged. This exception is limited
to monotone costs: if the maximal selection cannot pay, no smaller selection
can. Do not extend it to non-monotone costs without a new decision. All other
unscripted prompts, including card selections outside cost payment, remain
fail-closed.

Working recipe (verified 2026-11-10, pinned mage-bench revision):

1. `mvn install package -DskipTests` in a mage-bench clone (Java 21 + Maven; ~25 min).
2. Config `diff-smoke.json`: players `{"type":"sleepwalker","name":"SleepA","deck":"tmp/decks/islands.dck"}` —
   copy these `.dck` files into `<mage-bench>/tmp/decks/` first.
   - Player names must be 3-14 chars (server rejects shorter names and the bridge exits rc=1).
   - Deck lines: `N [SET:COLLECTOR] Card Name`; no legality enforcement — 60x Island works,
     which is how we neutralize the shuffle for cross-engine replay.
   - `deckType` must be a real validator name, e.g. `"Constructed - Standard"` (bare `"Constructed"` NPEs).
3. `uv run python -m magebench.cli --config <cfg> --skip-compile` (no `--observer` for headless).
4. Control channel: the bridge exposes MCP tools `get_action_choices`, `choose_action`,
   `pass_priority`, `get_game_state`, `get_game_log`; `src/magebench/pilot/replay.py`
   shows scripted driving + assert steps.

## Invariant fuzzer

`scripts/run_invariant_fuzz.py --games N --outdir runs/fuzz/<name>` plays N seeded random games for every ordered deck pair in `oracle/decks` with `target/release/invariant-fuzz` and writes one JSONL per pair plus `summary.json`. It needs no oracle. A violation fails the run unless its `signature` is a line of `ci/fuzz-known.txt`. To replay one game with the last 40 decisions:

```
FUZZ_TRACE=1 target/release/invariant-fuzz phase/client/public oracle/decks/<a>.txt oracle/decks/<b>.txt <seed> 1
```

Set `FUZZ_BACKTRACE=1` as well to print the backtrace of a panic to stderr.

## Lockstep differential game

`decks/mono-green-v0.txt` is the 60-card Standard test deck (24 Forests and six
French-vanilla creature names). Its Scryfall FDN printings and Standard legality
are cached in `decks/scryfall-printings.json`. Give the driver two deck-list
files in the `N Card Name` format:

```bash
python driver/diff_game.py oracle/decks/mono-green-v0.txt \
  oracle/decks/mono-green-v0.txt --seed 17 --v0
```

Before running, apply all local mage-bench patches in sorted filename order
from the `mtg-rl-sim` repository root:

```bash
MAGE_BENCH_ROOT=/home/ubuntu/repos/eng/mage-bench
mapfile -t patches < <(find oracle/patches -maxdepth 1 -type f -name 'mage-bench-*.patch' -print | LC_ALL=C sort)
for patch in "${patches[@]}"; do
  git -C "$MAGE_BENCH_ROOT" apply "$PWD/$patch"
done
```

`driver/meta_decks.py --refresh-printings` refreshes the deck cache by selecting
the newest Scryfall printing whose set and collector number are registered in
`MAGE_BENCH_ROOT`, then canonicalizes deck names from Scryfall. `oracle/xmage-missing.txt`
lists cards that are absent from the pinned upstream source and any decks that
cannot load because of them.

The external-pilot patch adds a bridge-only `external` pilot. Its process
reserves the XMage seat, starts the existing HTTP bridge, writes
`<game_dir>/<name>_mcp_url.txt`, and stays alive while the game runs. The
upstream-cards patch adds five upstream card implementations and the three
requested card tests. Enlightened Confidant remains unpatched because its
upstream class needs a missing pinned-core accessor; it and its deck are listed
in `oracle/xmage-missing.txt`. These patches are applied locally; do not push
them to mage-bench.
The driver launches both seats and sets
`XMAGE_AI_PUPPETEER_CHOOSING_PLAYER=DiffA`; it answers XMage's starting-player
target by selecting `DiffA`, matching phase-rs `PlayerId(0)`.
Both engines keep their initial seven cards. XMage startup shuffling is
disabled with `skipInitShuffling: true`.

The driver uses sync-point lockstep: phase-rs `PassPriority` and empty combat
stops are auto-applied until the next meaningful decision, while XMage's
`pass_priority` advances through priority windows until that seat has a pending
action. At each meaningful decision it checks turn, active player, normalized
step, board projection, and legal land/cast choices plus attacker availability.
Land and creature choices map by normalized card name and action kind; attackers
are all-or-none and blockers are always declined. If XMage exposes a combat
prompt for a phase-rs trivial combat stop, the driver declines it and records a
normalization event in `summary.json`. XMage's turn counter is normalized from
round numbers to per-player turn numbers using the active player's seat (the
first player's turn is odd, the second player's is even). Phase names are
normalized by removing punctuation and case differences; card names are
normalized by removing diacritics and using the front face before `//`.
Single-card non-priority selections (for example, cleanup discards) map by
normalized card name.

Each run writes `summary.json` on completion (or at the 30-turn cap) or stops
at the first mismatch and writes `divergence.json` under `runs/<timestamp>/`.
The board projection contains life, hand-name multisets, library counts,
graveyard-name multisets, battlefield name/controller/tapped multisets, turn,
and active player.

## Verification status

Seeds 17, 23, and 42 reached game over after 14, 19, and 16 turns respectively.
All meaningful sync points matched. Phase and XMage winners matched in the
available terminal records (seed 17's winner is recorded in the bridge logs).
The external pilot shuts down the bridge on game over, so the final canonical
board projection cannot be fetched; summaries use the last available XMage
projection. The seed-23 summary predates the final status-label edit and says
`phase_completed_xmage_terminal_unverified`; the other two say
`completed_terminal_projection_unavailable`. Seed 17's summary omits the XMage
winner, but both bridge logs report DiffB as winner. The run artifacts are local
under `runs/20261108T161441446254Z`, `runs/20261108T161826656907Z`, and
`runs/20261108T162123210400Z`.

The driver also normalizes priority-only XMage callbacks by passing them without
per-step mirroring, and maps single-card non-priority selections by normalized
card name. For empty phase-side combat stops, it declines matching XMage
attacker/blocker prompts and records the normalization event in the summary.

The `--xmage-library-top` option controls the direction used when creating the
XMage `.dck` files (`first` or `last`). XMage treats the first `.dck` entry as
the library top, so the default `first` preserves the deck-list order. Runs
and generated decks/configs are local artifacts and `runs/` is git-ignored.

### Opening-library pilot

With `skipInitShuffling: true` and the driver writing the `.dck` in reverse
(`--xmage-library-top last`), XMage's observed opening hand contained two
Tajuru Pathwardens and five Thornweald Archers—the final seven entries in the
source deck. This establishes that XMage takes its opening cards from the
first `.dck` entries. The default `first` direction therefore aligns XMage's
opening hand with phase-rs's first seven deck-list entries.
