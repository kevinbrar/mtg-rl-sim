# Local phase-rs fixes

The phase submodule remains pinned at `988bc3412f5d4e475c1837fbc88062b14d1f5489`.
Card fixes are committed one per card on the local `local-fixes` branch and
exported here with `git format-patch 988bc34..local-fixes`. No upstream PRs or pushes are
planned.

## Card status

| Card | Status | Runtime test | Coverage / semantic audit | Self-review |
| --- | --- | --- | --- | --- |
| Restless Prairie | Fixed in phase commit `0621bcebc88f0ab367d567d4bfe475fe4b443c76`: the coverage subtype lexicon includes canonical parser vocabulary as well as printed type-line subtypes. The Oracle text's Llama subtype is valid under CR 205.3m but does not appear on a printed type line. | `restless_prairie_becomes_a_3_3_llama_and_remains_a_land` passed; `GameRunner::activate` resolves the printed ability to a 3/3 Llama that remains a land. | `supported: true`, `gap_count: 0`; semantic audit has 0 Restless Prairie findings. Final aggregate: 32,317/36,280 supported. | Pass: general vocabulary union; existing `text_grammar::parse_subtype` and `oracle_util::parse_creature_subtype` are analogous canonical-list consumers. Existing unknown-subtype test still covers the filler-word rejection path. |
| Rottenmouth Viper | Fixed in phase commit `478d48ccb5dff67c6b2cb21a78091d3f2956e645`: `starts_prefix_clause` keeps a sequence-connected `Then for each` prefix with its comma-delimited effect body, allowing the existing counter-count parser to attach `repeat_for` to the scoped unless effect. | `viper_attack_repeats_the_unless_clause_for_each_blight_counter` passed with the printed Oracle text: one seeded blight counter plus the attack trigger's counter produces two separate P1 prompts and two 4-life losses; P0 remains at 20 and the trigger completes. Parser regressions `then_for_each_prefix_keeps_its_effect_body_together` and `trigger_for_each_blight_counter_repeats_scoped_unless` passed. | `supported: true`, `gap_count: 0`; semantic audit has 0 Rottenmouth Viper findings. Final aggregate: 32,317/36,280 supported. | Pass: self-review only; see AI-CONTRIBUTOR §5(a–i) below. |
| Lost in the Maze | Fixed in phase commit `93dfa0c8ee33ea6122b20bc3b0995fab0c0b327d`: anaphoric tracked-set parsing preserves controller-qualified typed filters such as “each of those creatures you don't control.” | `lost_in_the_maze_stuns_only_targeted_creatures_not_controlled_by_its_controller` passed through the scenario runner; casts the printed Oracle text for X=3 and verifies all targets tap, only an opponent-controlled target gets a stun counter, a stolen opponent-owned target does not, and an untargeted creature is unaffected. Baseline failed at the opponent's missing stun counter. Parser regression `each_of_those_creatures_filters_the_tracked_set_by_controller` passed. | `supported: true`, `gap_count: 0`; semantic audit has 0 Lost in the Maze findings. Final aggregate: 32,317/36,280 supported. | Pass: self-review only; see AI-CONTRIBUTOR §5(a–i) below. |
| Seasoned Cryomancer | Fixed in phase commit `f54786c9ff4d45108cee5f1aa39f384fc5eb0ca9`: filtered discard reflexive target counts use the matching discarded-card set, and the reflexive body preserves its own announced target set. | `seasoned_cryomancer_targets_up_to_the_number_of_nonland_cards_discarded` passed through `GameRunner`; two nonland discards offer two target slots, one nonland plus one land offers one, and land-only discards create no target prompt. | `supported: true`, `gap_count: 0`; semantic audit has 0 Seasoned Cryomancer findings. Final aggregate: 32,317/36,280 supported. | Pass: self-review only; see AI-CONTRIBUTOR §5(a–i) below. |
| Essence Burn | Fixed in phase commit `81ddaa94d5ed2efdbcb4d3657c63df496925e011`: die-exile rider parsing accepts the generic “that permanent” referent, and the target parser scopes a color disjunction over each branch of a type disjunction. | `essence_burn_exiles_lethally_damaged_creature_and_planeswalker` and `essence_burn_does_not_target_a_black_noncreature_artifact` passed through `GameRunner`; the latter also rejects a colorless creature and includes a legal-target positive control. Parser regression `essence_burn_parses_its_target_permanent_die_exile_rider` and target-shape regression `color_disjunction_distributes_over_type_disjunction` passed. `color_or_type_union`, `colorless_adjective_does_not_distribute_across_or`, the Carbonize/Disintegrate die-exile parser tests, and Torch the Tower's lethal-damage runtime test also passed. | `supported: true`, `gap_count: 0`; semantic audit has 0 Essence Burn findings. Final aggregate: 32,317/36,280 supported. | Pass: self-review only; see AI-CONTRIBUTOR §5(a–i) below. |
| Anzrag's Rampage | Fixed in phase commit `a7db66350015a7d77f11b88d5064a93aec3a08d7`: binds typed battlefield-to-graveyard counts through `ZoneChangeCountThisTurn`; the same wording on Structural Assault is covered. | `anzrags_rampage_exiles_one_card_per_destroyed_artifact` passed through `GameRunner::cast` using the printed Oracle text; two opposing artifacts go to graveyards, exactly two library cards are exiled, and the third remains. | `supported: true`, `gap_count: 0`; semantic audit has 0 Anzrag's Rampage findings. Final aggregate: 32,317/36,280 supported. | Pass with corpus note: the shared typed-quantity pattern has two known printed consumers, Anzrag's Rampage and Structural Assault. Self-review only; see AI-CONTRIBUTOR §5(a–i) below. |
| Urgent Necropsy | Parked; unapplied diagnosis in `wip/urgent-necropsy.diff`. `CollectEvidence.amount` is fixed-width and target-dependent X must flow through shared casting-cost resolution; a correct fix must defer payability until targets exist and resolve the amount before evidence payment. | — | Not fixed; no coverage or semantic-audit claim. | WIP diagnosis only; no implementation review. |
| Vren, the Relentless | Fixed in phase commit `7cd439def8d3c7159c0824d43023d59d45f12234`: parses typed objects exiled from the battlefield under a controller's control this turn into `ZoneChangeCountThisTurn`, filtered by last-known controller. | `vren_counts_creatures_exiled_under_opponents_control_this_turn` passed through `GameRunner`; Vren's replacement exile and a direct exile of opponent-controlled creatures count, while a P1-owned creature controlled by P0 does not. | `supported: true`, `gap_count: 0`; semantic audit has 0 Vren findings. Final aggregate: 32,317/36,280 supported. | Pass: self-review only; see Vren self-review below. |
| Theorist's Sanctum | Parked; unapplied diagnosis in `wip/theorists-sanctum.diff`. Supporting this as-enters replacement requires an interactive Behold payment/resume path that preserves the pre-entry event and the tap-on-decline branch. | — | Not fixed; no coverage or semantic-audit claim. | WIP diagnosis only; no implementation review. |
| Kíli the Resourceful | Parked; unapplied diagnosis in `wip/kili-the-resourceful.diff`. Correct support needs a shared activation-cost choice that can substitute {0} for an equip cost once per turn; current numeric reductions and spell alternative costs do not model it. | — | Not fixed; no coverage or semantic-audit claim. | WIP diagnosis only; no implementation review. |
| Case of the Uneaten Feast | Parked; unapplied WIP and casting-permission seam analysis in `wip/case-of-the-uneaten-feast.diff`. A correct fix needs the casting-permission collector to honor an object-scoped grant with a fixed recipient set under CR 611.2c; the saved runtime test still fails. | Runtime test remains failing; no fix claimed. | Not fixed; no coverage or semantic-audit claim. | WIP diagnosis only; no implementation review. |

### Rottenmouth Viper self-review (AI-CONTRIBUTOR §5 a–i)

- **(a) Correct seam — pass.** The defect was the sequence splitter separating `Then for each blight counter on it` from its comma-delimited body. The fix is in `oracle_effect/sequence.rs::starts_prefix_clause`, before the existing for-each lowering.
- **(b) Idiomatic change — pass.** Extends the existing prefix `alt` with a nom `tag`, matching neighboring sequence-prefix handling.
- **(c) Nom compliance — pass.** No string-method dispatch or verbatim Oracle-text dispatch was added.
- **(d) CR citations — pass.** Verified against `docs/MagicCompRules.txt`, including adjacent rules: CR 608.2c covers instruction order, CR 608.2h fixes game information when the effect is applied, CR 608.2f covers actions involving multiple players, and CR 118.12a defines “unless.”
- **(e) Pattern coverage — pass.** The generated Oracle-text corpus has 11 cards with “Then for each,” including Altaïr Ibn-La'Ahad, Arna Kennerüd, Skycaptain, Exotic Pets, and Rottenmouth Viper.
- **(f) Logic placement — pass.** Only parser sequence handling changed; existing engine repeat and player-scope resolution remain authoritative.
- **(g) Building-block reuse — pass.** Existing `strip_for_each_prefix_with_difference`, `parse_for_each_clause`, and `drive_repeat_for_outermost` provide quantity parsing and repeat execution; this change only preserves their input.
- **(h) Bool-flag avoidance — pass.** No field or parameter was added.
- **(i) Test discrimination — pass.** The scenario-runner test drives the printed Oracle text through attack declaration and unless choices. Before the parser change it reached priority after the first decline instead of a second prompt; after the change it observes both prompts and the corresponding life losses.

This was a self-review only; no independent reviewer was used.

### Lost in the Maze self-review (AI-CONTRIBUTOR §5 a–i)

- **(a) Correct seam — pass.** The parsed `each of those <type> you control / you don't control` filter lost its controller qualifier when `parse_target` chose the bare `TrackedSet` form. The generic `target_filter_carries_predicate_property` check now retains typed controller scope.
- **(b) Idiomatic change — pass.** Extends the existing `TypedFilter` predicate check with its typed `controller` field.
- **(c) Nom compliance — pass.** No parser dispatch or card-name matching was added; the existing nom phrase parser still parses the qualifier.
- **(d) CR citations — pass.** Checked against `docs/MagicCompRules.txt`: CR 608.2c for following instructions in order, CR 608.2b for target legality, CR 107.3m for X on the ETB trigger, and CR 122.1d for stun counters.
- **(e) Pattern coverage — pass.** The change applies to controller-qualified typed filters on any “each of those” tracked set, not only Lost in the Maze.
- **(f) Logic placement — pass.** Parser target-filter construction changed; existing `PutCounterAll` lowering and engine tracked-set resolution remain authoritative.
- **(g) Building-block reuse — pass.** Reuses `parse_type_phrase_folding_with_ctx`, `TrackedSetFiltered`, and existing `PutCounterAll` handling. Gate B analogues: Stall for Time, Monstrosity of the Lake, and Hapatra, the Desert Frost already lower stun counters over tracked sets.
- **(h) Bool-flag avoidance — pass.** No new fields, parameters, or flags were added.
- **(i) Test discrimination — pass.** The production `SpellCast` scenario uses the printed Oracle text and X=3; before the parser fix, all three targets tapped but the opponent-controlled target received no stun counter. The passing assertions also distinguish current controller from owner and exclude a non-target.

This was a self-review only; no independent reviewer was used.

### Seasoned Cryomancer self-review (AI-CONTRIBUTOR §5 a–i)

- **(a) Correct seam — pass.** The reflexive target count was parser-bound to the filtered discarded-card set using the existing `FilteredTrackedSetSize` runtime quantity; the earlier typed-target rewrite and reflexive classifier now preserve targets announced by the reflexive body.
- **(b) Idiomatic change — pass.** Reuses `target_filter_has_explicit_object_constraints`, `scan_at_word_boundaries`, `QuantityExpr::rebind_event_context_amount`, `FilteredTrackedSetSize`, and `ThisWayCause::Discarded`; no new engine type or resolver path was added. Gate B analogues: Nyssa of Traken's reflexive up-to target count (`reflexive_sacrifice_this_way_taps_and_draws_that_many`), Dread Summons's filtered this-way count (`for_each_creature_card_this_way_counts_only_creatures`), and Malfegor's discard-caused filtered tracked-set count (`issue_5991_malfegor_discard_sacrifice`).
- **(c) Nom compliance — pass.** The explicit-target check uses the existing nom word-boundary scanner; no card-name or string-method dispatch was added.
- **(d) CR citations — pass.** Verified against `docs/MagicCompRules.txt`, including adjacent rules: CR 115.1d and 603.3d govern announcing triggered-ability targets; CR 603.12 defines reflexive triggers; CR 608.2c directs interpretation of instructions in order and in context; CR 701.9a defines discard.
- **(e) Pattern coverage — pass.** The transformation is generic to filtered discard-this-way target cardinalities, not dispatched by card name. It replaces the event-wide amount with the tracked-set size matching the reflexive filter and discard cause.
- **(f) Logic placement — pass.** The parser constructs the filtered quantity; existing engine quantity resolution and targeting continue to own runtime behavior. No frontend or transport logic changed.
- **(g) Building-block reuse — pass.** The implementation uses existing typed target filters and quantity references rather than introducing another count variant. The Gate B analogues above cover reflexive target counts and filtered tracked-set counts.
- **(h) Bool-flag avoidance — pass.** No fields, parameters, or flags were added.
- **(i) Test discrimination — pass.** The scenario-runner test casts the printed Oracle text and drives the ETB discard and reflexive target prompts. Its mixed land/nonland case fails if the filtered count binding is reverted; it also checks two nonland cards and the land-only no-trigger path, plus completion to priority.

This was a self-review only; no independent reviewer was used. Final per-card coverage and audit results are recorded in the table above.

### Vren self-review (AI-CONTRIBUTOR §5 a–i)

- **(a) Correct seam — pass.** The missing behavior was an Oracle quantity parse; the new parser binds it to the existing `ZoneChangeCountThisTurn` quantity rather than adding a card-specific engine path.
- **(b) Idiomatic change — pass.** The parser composes a typed object phrase, zone-change clause, and controller scope with existing `parse_type_phrase_folding`, `TargetFilter`, and `ControllerRef` building blocks.
- **(c) Nom compliance — pass.** Uses `alt`, `tag`, and `value` combinators; no card-name dispatch or string-method parsing was added.
- **(d) CR citations — pass.** The code cites CR 400.7 for zone-change identity and CR 109.5 for controller-relative wording; both were checked in `docs/MagicCompRules.txt`.
- **(e) Pattern coverage — pass.** Covers typed battlefield objects exiled under a controller's control, including plural creatures/opponents, singular creature/opponent, and artifacts/your control; it is not tied to Vren's name.
- **(f) Logic placement — pass.** The parser constructs the quantity; existing engine zone-change history and quantity resolution remain authoritative.
- **(g) Building-block reuse — pass.** Gate B analogues are `parse_number_of_creatures_died_this_turn`, `parse_entered_this_turn_ref`, and Cruel Calculations' origin-scoped graveyard zone-change count.
- **(h) Bool-flag avoidance — pass.** No new flags, fields, or enum variants were added; the existing `ControllerRef` and zone values express the scope.
- **(i) Test discrimination — pass.** The runtime scenario uses printed Vren, Shock, and Swords to Plowshares Oracle text. It verifies two opponent-controlled creature exiles count, while a creature owned by P1 but controlled by P0 is excluded.

This was a self-review only; no independent reviewer was used. Final per-card coverage and audit results are recorded in the table above.

### Essence Burn self-review (AI-CONTRIBUTOR §5 a–i)

- **(a) Correct seam — pass.** The die-exile rider parser lacked the generic “that permanent” subject form; the target parser also failed to distribute a color disjunction over a type disjunction. Both fixes stay in Oracle parsing, with no engine changes.
- **(b) Idiomatic change — pass.** The rider uses the existing nom `alt`/`tag` subject grammar. The target fix composes `parse_color_disjunction`, `parse_type_phrase_folding_unbound`, and `distribute_shared_properties`.
- **(c) Nom compliance — pass.** New parsing uses nom combinators and the existing `nom_on_lower` bridge; no card-name dispatch or string-method parsing was added.
- **(d) CR citations — pass.** Checked `docs/MagicCompRules.txt`: CR 105.2 and 205.2a for color and card-type characteristics, CR 109.2 for object descriptions, CR 608.2c for interpreting instructions in order, CR 614.1a for replacement effects, and CR 514.2 for the end of “this turn” effects.
- **(e) Pattern coverage — pass with a corpus note.** This extends the existing die-exile rider family used by Touch of the Void, Carbonize, Disintegrate, and Torch the Tower; the corpus contains 65 cards with both “would die this turn” and “exile.” The exact “that permanent would die this turn” wording currently occurs only on Essence Burn, but the added subject is a general grammatical form, not a card-name branch.
- **(f) Logic placement — pass.** Parsing changed; the existing target binding and replacement-effect engine behavior remain authoritative.
- **(g) Building-block reuse — pass.** Reuses `try_parse_die_exile_rider`, declared-slot/anaphor binding, `parse_color_disjunction`, `parse_type_phrase_folding_unbound`, and `distribute_shared_properties`. Gate B analogues: Touch of the Void and Carbonize (damage-spell die-exile riders), Disintegrate (combined regeneration and die-exile riders), and Torch the Tower (target-bound end-of-turn replacement).
- **(h) Bool-flag avoidance — pass.** No new fields, flags, or enum variants were introduced.
- **(i) Test discrimination — pass.** The scenario-runner tests use the exact Oracle text. Before the fix, lethal targets went to the graveyard rather than exile, and a black noncreature artifact was accepted. The passing cases exile a black creature and green planeswalker, reject a black artifact and colorless creature, and include legal-target positive controls; the new color/type parser tests preserve sibling parsing behavior.

This was a self-review only; no independent reviewer was used. Final per-card coverage and audit results are recorded in the table above.

### Anzrag's Rampage self-review (AI-CONTRIBUTOR §5 a–i)

- **(a) Correct seam — pass.** The card's `where X` quantity was embedded as `where_x_binding`; binding belongs in the shared Oracle quantity parser, not a card-specific engine path.
- **(b) Idiomatic change — pass.** Reuses `parse_type_phrase_folding` and `QuantityRef::ZoneChangeCountThisTurn`; `opt`, `alt`, and `tag` compose the movement phrase.
- **(c) Nom compliance — pass.** No string-method dispatch or card-name matching was added.
- **(d) CR citations — pass.** CR 107.3c defines X from the spell text; CR 608.2i governs looking back at prior game actions. Both were checked in `docs/MagicCompRules.txt`.
- **(e) Pattern coverage — pass with corpus note.** The current card-data Oracle corpus has two direct quantity consumers with this wording: Anzrag's Rampage and Structural Assault. The parser is generic over typed object phrases rather than dispatched by card name.
- **(f) Logic placement — pass.** The parser constructs the quantity; existing engine zone-change history and quantity resolution remain authoritative.
- **(g) Building-block reuse — pass.** Gate B analogues are `parse_number_of_creatures_died_this_turn` and `parse_number_of_objects_exiled_under_controller_this_turn`; runtime reuses `ZoneChangeCountThisTurn`.
- **(h) Bool-flag avoidance — pass.** No fields, flags, or enum variants were added.
- **(i) Test discrimination — pass.** The scenario-runner test uses the printed Oracle text and asserts the two-card exile count. The baseline card data had `where_x_binding`; the test also confirms the third library card remains, so the result depends on the dynamic count rather than just spell completion.

This was a self-review only; no independent reviewer was used. Final per-card coverage and audit results are recorded in the table above.

## Final gates

All gates below are for `local-fixes` head
`75a091949084ddd19609739a9d36076742bb0c48`, based on pinned upstream
`988bc3412f5d4e475c1837fbc88062b14d1f5489`. The submodule pointer in the
superproject remains pinned; no phase-rs upstream branch or PR was created.

- Cryomancer commit `f54786c9ff4d45108cee5f1aa39f384fc5eb0ca9` was checked;
  its message has normal blank lines and no literal `\n\n`.
- Gate A passed against the pinned base. The working tree was clean, so the
  scanned working-tree diff is the committed `988bc34..local-fixes` content.
  Exact output:

  ```text
  Gate G PASS (router/grant architecture: strict router vs permissive grant boundary intact)
  residue-ratchet: tighten crates/phase-parser/src/parser/oracle_cost.rs 3 -> 2
  residue-ratchet: tighten crates/phase-parser/src/parser/oracle_static/static_helpers.rs 1 -> 0 (entry can be deleted)
  Gate H PASS (residue: 121 legacy obligations, ceiling 123; files scanned 91; snapshot worktree)
  Gate A: scanned 5 file(s) under crates/engine/src/parser crates/engine/src/types/text_grammar.rs crates/phase-ast/src/types/text_grammar.rs crates/phase-parser/src/parser (working tree vs 988bc3412f5d4e475c1837fbc88062b14d1f5489).
  Gate A PASS head=75a091949084ddd19609739a9d36076742bb0c48 base=988bc3412f5d4e475c1837fbc88062b14d1f5489
  ```
- `cargo fmt --all` passed. One full `cargo test -p phase-engine` passed:
  13,790 unit tests and 15,595 tests across the other test targets passed;
  4 unit, 12 other-target, and 2 doc tests were ignored; zero failures.
- Clean pinned baseline card data was generated at
  `/home/ubuntu/repos/eng/pdata/baseline-988bc34-card-data.json`:
  SHA-256 `45bb5ca39612a82b255c121f0fba69d2a688b832242aad0b1a38e36f575ed8c1`.
  This matches the existing `pdata/baseline-card-data.json`; supplied
  `pdata/v/card-data.json` has SHA-256
  `5b455542437560f27543ca1db746c75d2909a8ddff6fe015f77761c303a28fa0` and
  does not match the pinned generation.
- Final patched data was copied to
  `/home/ubuntu/repos/eng/pdata/patched/card-data.json`; SHA-256
  `738917fb87f2e4cbf08dac8d7816a7d5826f4779989b7647d33870a2c734bdf7`.
  The generated coverage report is 32,317/36,280 supported (89.1%), versus
  32,303/36,280 in the clean baseline. All seven fixed targets are
  `supported: true`, `gap_count: 0`, with zero semantic-audit findings each.
  The audit reports 191 findings globally among 32,834 supported cards.
- Per-card supported-status comparison found zero supported-to-unsupported
  flips and zero partial regressions. For the 1,236 card entries whose Oracle
  text matches “this way,” “would die this turn,” “put into a graveyard from
  the battlefield this turn,” or “exiled under” (including Essence Burn's
  “exile it instead” rider), the parsed fields `keywords`, `abilities`,
  `triggers`, `static_abilities`, `replacements`, and `additional_cost` had
  eight changed records:
  - Anzrag's Rampage and Essence Burn are target-card improvements: their
    defined-X zone-change count and target-bound die-exile replacement are
    represented instead of unimplemented effects.
  - Rottenmouth Viper, Seasoned Cryomancer, and Vren, the Relentless are
    target-card improvements: their trigger JSON now contains the repeated
    scoped unless effect, the filtered discard reflexive effects, and the
    typed zone-change quantity, respectively.
  - Narset of the Ancient Way is a non-target improvement: its discarded-card
    reflexive damage now retains the announced creature/planeswalker target
    and mana-value amount; the runtime test passed.
  - Thorin, Mountain-king is a non-target improvement: the “become attached
    this way” reflexive trigger now retains its own target creature instead of
    inheriting the entering ability's `ParentTarget`; support remains true and
    the existing parser test passes.
  - Vivien's Invocation is a non-target improvement: its reflexive damage now
    retains the opponent-creature target and entering-creature power amount;
    the runtime test passed.
  No neutral or regressive non-target ability changes were found.
- Ten patches were exported from `988bc34..local-fixes` (0001–0010).
  `scripts/apply-phase-patches.sh` applied them from the clean pinned submodule.
  Reapplied tree `e4cd4d366609429566c36e7686a0e325f0b98455` exactly matched the
  `local-fixes` tree; the submodule was returned to `local-fixes`.
- The shared reflexive-target handling is covered by commits
  `6999757f8330c036daba63a8a1298a6e533e9114` (Narset runtime regression),
  `3063c3e9ea3549a157b8ebf097c91f4eabc06719` (typed-target reflexive binding),
  and `75a091949084ddd19609739a9d36076742bb0c48` (restrict the binding skip to
  `WhenYouDo`, preserving ordinary conditional riders such as Cragganwick's).
- `cargo build --release -p phase-env` passed in 7m58s. It emitted the existing
  workspace-resolver warning and `phase-env/src/main.rs:38` dead-code warning
  for `Out`. A seeded random game through `driver/phase_env.py`, using
  `/home/ubuntu/repos/eng/pdata/patched/` and both 58-card decks containing
  two copies of every fixed card, reached `game_over` (winner P0, 1,303 trace
  events) with `unresolved: []`.
- The generator reported 558 parser warnings across three
  categories. It also dirtied `known-tokens.toml` and `mtgjson-vintage`; both
  were reverted after generation and are not part of the patch series.

The engine implementation reviews in this file were self-reviews; no
independent review was performed.
