# XMage oracle (mage-bench)

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
