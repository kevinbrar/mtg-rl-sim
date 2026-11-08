# XMage oracle (mage-bench)

Working recipe (verified 2026-11-07, mage-bench master):

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
