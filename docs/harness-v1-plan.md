# Differential harness v1: real Standard meta decks

Goal: run `driver/diff_game.py` on real Standard meta decks (MTGGoldfish feed in
`phase/client/public/feeds/mtggoldfish-standard.json`, main boards) so the 18
fixed cards and the rest of the meta get checked against XMage. v0 only maps
lands, creature casts, all-or-none attacks, and declines blocks.

## Facts gathered (2026-11-09)
- phase-env emits `candidates` as serde JSON of `GameAction` (see
  `phase/crates/engine/src/types/actions.rs`): `CastSpell{object_id,card_id,targets:[ObjectId]}`,
  `ActivateAbility{source_id,ability_index}`, `SelectTargets{targets:[TargetRef]}`,
  `ChooseTarget{target}`, `DeclareBlockers{assignments:[(blocker,attacker)]}`,
  `DeclareAttackers{attacks:[(id,AttackTarget)]}`, `SelectModes{indices}`, `ChooseX{value}`,
  `ChooseReplacement{index}`, `DecideOptionalCost{pay}`, `ChooseOption{choice}`,
  `SelectCards{cards}`, `OrderTriggers{order}` ... (~170 variants). `state` snapshot has
  battlefield entries `{id,name,controller,tapped}`, hand_cards `{id,name}`, stack `{controller,name}`.
- XMage bridge (`mage-bench/Mage.Client.Bridge/.../BridgePublishedQueryBuilder.java`) publishes
  one pending prompt at a time with `choices[]` of `{index,id(pN),name,...}` and a type:
  GAME_SELECT (priority: `action` = land|cast|activate), GAME_TARGET (targets, each with name /
  is_you / controller info via `buildTargetInfo`), GAME_ASK (yes/no), GAME_CHOOSE_ABILITY,
  GAME_CHOOSE_CHOICE (pick by `text`), GAME_CHOOSE_PILE, GAME_GET_AMOUNT (`amount`),
  GAME_GET_MULTI_AMOUNT, GAME_PLAY_MANA (auto-tap default). `choose_action` params: choice
  (id/index/yes/no), amount, amounts, pile, text, mana, attackers ("all" or ids), blockers
  ("blocker:attacker" pairs).
- XMage decks need `N [SET:NUM] Name`; printings come from Scryfall
  (`oracle/decks/scryfall-printings.json` cache; api.scryfall.com reachable). MTGGoldfish
  deck downloads are Cloudflare-blocked from curl; the phase feed JSON already has main boards.
- Both engines keep opening hands; library order is forced identical (`ordered_library`).

## Plan (incremental; stop at first unmapped prompt, add mapping, rerun)
1. Deck loader for the feed JSON (main board only) + Scryfall printing fetch/cache for any
   card; drop `validate_v0_deck` behind a `--v0` flag.
2. Mapping layer phase→XMage by *card name + controller + zone*, not ids:
   - CastSpell/ActivateAbility → GAME_SELECT choice with same name and action; abilities by
     ability text index.
   - targets: phase `targets`/`SelectTargets` → the subsequent GAME_TARGET prompts, matched by
     name (+ controller when ambiguous); players as targets by seat name.
   - DeclareBlockers → `blockers` pairs by names; DeclareAttackers → attacker ids by name.
   - SelectModes / ChooseOption / ChooseReplacement / DecideOptionalCost → GAME_CHOOSE_CHOICE
     by text or GAME_ASK yes/no. ChooseX → amount.
   - Replacement prompts XMage asks that phase auto-resolves (and vice versa) → normalization
     events, as v0 does for combat stops.
3. Policy: random over phase candidates but biased to cast/activate when legal, so cards get
   exercised; seedable.
4. Compare at every meaningful decision as v0 does; on first unmapped prompt write
   `divergence.json` with both prompts and exit non-zero (that is the signal to extend).
5. Run one game per meta-deck pair that contains each fixed card; record results in
   `oracle/README.md` and `STATUS.md`.

## Oracle coverage (2026-11-09)
- XMage upstream lacks 4 of the 18 fixed cards: Seasoned Cryomancer, Jace's Machinations,
  Uldaros Theorix, Extrapolate the Impossible. Local mage-bench XMage (2026-10-21) also lacks
  Essence Burn, Theorist's Sanctum, Kíli → update mage-bench's XMage first.
- Forge (Card-Forge/forge) has scripts for all 18 → add Forge as a second oracle for cards XMage
  lacks; 2-of-3 agreement is evidence, not proof.
- Adjudication of any disagreement: Comprehensive Rules (`phase/docs/MagicCompRules.txt`,
  effective 2026-09-25) + official rulings (Scryfall `/cards/:id/rulings`); cite rule numbers;
  record as a regression test; escalate to Kevin only if genuinely ambiguous.
- Official rulings → per-card scenario tests whose expected outcomes come from the ruling text.
