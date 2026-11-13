# Local phase-rs fixes

The phase submodule remains pinned at `988bc3412f5d4e475c1837fbc88062b14d1f5489`.
Card fixes are committed on the local `local-fixes` branch and
exported here with `git format-patch 988bc34..local-fixes`. No upstream PRs or pushes are
planned.

## Card status

| Card | Status | Runtime test | Coverage / semantic audit | Self-review |
| --- | --- | --- | --- | --- |
| Restless Prairie | Fixed in phase commit `0621bcebc88f0ab367d567d4bfe475fe4b443c76`: the coverage subtype lexicon includes canonical parser vocabulary as well as printed type-line subtypes. The Oracle text's Llama subtype is valid under CR 205.3m but does not appear on a printed type line. | `restless_prairie_becomes_a_3_3_llama_and_remains_a_land` passed; `GameRunner::activate` resolves the printed ability to a 3/3 Llama that remains a land. | `supported: true`, `gap_count: 0`; semantic audit has 0 Restless Prairie findings. Final aggregate: 32,321/36,280 supported. | Pass: general vocabulary union; existing `text_grammar::parse_subtype` and `oracle_util::parse_creature_subtype` are analogous canonical-list consumers. Existing unknown-subtype test still covers the filler-word rejection path. |
| Rottenmouth Viper | Fixed in phase commit `478d48ccb5dff67c6b2cb21a78091d3f2956e645`: `starts_prefix_clause` keeps a sequence-connected `Then for each` prefix with its comma-delimited effect body, allowing the existing counter-count parser to attach `repeat_for` to the scoped unless effect. | `viper_attack_repeats_the_unless_clause_for_each_blight_counter` passed with the printed Oracle text: one seeded blight counter plus the attack trigger's counter produces two separate P1 prompts and two 4-life losses; P0 remains at 20 and the trigger completes. Parser regressions `then_for_each_prefix_keeps_its_effect_body_together` and `trigger_for_each_blight_counter_repeats_scoped_unless` passed. | `supported: true`, `gap_count: 0`; semantic audit has 0 Rottenmouth Viper findings. Final aggregate: 32,321/36,280 supported. | Pass: self-review only; see AI-CONTRIBUTOR §5(a–i) below. |
| Lost in the Maze | Fixed in phase commit `93dfa0c8ee33ea6122b20bc3b0995fab0c0b327d`: anaphoric tracked-set parsing preserves controller-qualified typed filters such as “each of those creatures you don't control.” | `lost_in_the_maze_stuns_only_targeted_creatures_not_controlled_by_its_controller` passed through the scenario runner; casts the printed Oracle text for X=3 and verifies all targets tap, only an opponent-controlled target gets a stun counter, a stolen opponent-owned target does not, and an untargeted creature is unaffected. Baseline failed at the opponent's missing stun counter. Parser regression `each_of_those_creatures_filters_the_tracked_set_by_controller` passed. | `supported: true`, `gap_count: 0`; semantic audit has 0 Lost in the Maze findings. Final aggregate: 32,321/36,280 supported. | Pass: self-review only; see AI-CONTRIBUTOR §5(a–i) below. |
| Seasoned Cryomancer | Fixed in phase commit `f54786c9ff4d45108cee5f1aa39f384fc5eb0ca9`: filtered discard reflexive target counts use the matching discarded-card set, and the reflexive body preserves its own announced target set. | `seasoned_cryomancer_targets_up_to_the_number_of_nonland_cards_discarded` passed through `GameRunner`; two nonland discards offer two target slots, one nonland plus one land offers one, and land-only discards create no target prompt. | `supported: true`, `gap_count: 0`; semantic audit has 0 Seasoned Cryomancer findings. Final aggregate: 32,321/36,280 supported. | Pass: self-review only; see AI-CONTRIBUTOR §5(a–i) below. |
| Essence Burn | Fixed in phase commit `81ddaa94d5ed2efdbcb4d3657c63df496925e011`: die-exile rider parsing accepts the generic “that permanent” referent, and the target parser scopes a color disjunction over each branch of a type disjunction. | `essence_burn_exiles_lethally_damaged_creature_and_planeswalker` and `essence_burn_does_not_target_a_black_noncreature_artifact` passed through `GameRunner`; the latter also rejects a colorless creature and includes a legal-target positive control. Parser regression `essence_burn_parses_its_target_permanent_die_exile_rider` and target-shape regression `color_disjunction_distributes_over_type_disjunction` passed. `color_or_type_union`, `colorless_adjective_does_not_distribute_across_or`, the Carbonize/Disintegrate die-exile parser tests, and Torch the Tower's lethal-damage runtime test also passed. | `supported: true`, `gap_count: 0`; semantic audit has 0 Essence Burn findings. Final aggregate: 32,321/36,280 supported. | Pass: self-review only; see AI-CONTRIBUTOR §5(a–i) below. |
| Anzrag's Rampage | Patch 0007 fixes the typed battlefield-to-graveyard count through `ZoneChangeCountThisTurn`; patch 0031 fixes singular “a creature card exiled this way” by choosing up to one card from the tracked Exile set and moving only that object. The upstream `ChangeZoneAll` misparse predates 0007, which exposed it. A count-one `ChangeZone { target: Any }` forwards its unchosen complement only to a direct zone-move sub-ability; non-movement riders bind to the selected card. Plural “any number of cards” wording remains unchanged. | Eight Anzrag runtime tests passed, including `bare_optional_exiled_card_placement_moves_only_the_chosen_card`, `bare_optional_exiled_card_placement_can_be_declined`, `mandatory_exiled_card_placement_requires_exactly_one_selection`, and `mandatory_exiled_card_placement_binds_followups_to_selected_creature`. A1–A7 match both engines; A5 moves only Grizzly Bears and leaves Llanowar Elves in exile. Full `phase-engine` suite: 29,424 passed, 0 failed, 18 ignored. Clippy passed with `-D warnings`; the Kíli `expect_fun_call` fix is in patch 0031. | Full regenerated card-data gate: 31 matched, 1 triaged (Vren V2), 0 failed; Vren Phase/XMage digests unchanged. The per-card comparison shows only Anzrag's Rampage changed. Anzrag-only entry is byte-identical to the published release entry (4,714 bytes; SHA-256 `edb31b5f9b951a595e80cd12a68153b11122987093305644113f5465944dd3b5`). See `runs/card-scenarios/anzrag-0031-zone-move-receiver-final-20261113/known-diffs-report.json`, `runs/card-scenarios/anzrag-0031-known-diffs-final-20261112/card-data-per-card-diff.md`, and `runs/card-scenarios/anzrag-0031-complement-forwarding-final-20261112/card-data-anzrag-post-clippy.json`. Targeted coverage still reports Anzrag supported with 0 gaps. | Reuses existing `ChooseFromZone` up-to-one selection; parser and runtime tests cover optional and mandatory wording, and XMage independently confirms A1–A7. |
| Urgent Necropsy | Fixed in phase commits `32412a72a`, `7b75abb6e`, and `a4f5cd961`: Collect Evidence resolves target-set mana value after target selection, and payability uses the same computed requirement. Resolution-time target aggregates now recover the full resolving ability chain. | `urgent_necropsy_requires_the_targets_total_mana_value`, `urgent_necropsy_cannot_be_cast_when_graveyard_cannot_cover_targets`, and `urgent_necropsy_requirement_changes_with_its_target_set` passed; Command the Dreadhorde's three target-set regression tests also passed. | `supported: true`, `gap_count: 0`; semantic audit has 0 Urgent Necropsy findings. | Pass: self-review only; no independent reviewer was available. |
| Vren, the Relentless | Fixed in phase commit `7cd439def8d3c7159c0824d43023d59d45f12234`: parses typed objects exiled from the battlefield under a controller's control this turn into `ZoneChangeCountThisTurn`, filtered by last-known controller. | `vren_counts_creatures_exiled_under_opponents_control_this_turn` passed through `GameRunner`; Vren's replacement exile and a direct exile of opponent-controlled creatures count, while a P1-owned creature controlled by P0 does not. | `supported: true`, `gap_count: 0`; semantic audit has 0 Vren findings. Final aggregate: 32,321/36,280 supported. | Pass: self-review only; see Vren self-review below. |
| Theorist's Sanctum | Fixed in phase commit `37f86de726ffefa5f5031747cc96728e6a75d193`: as-enters `MayCost` replacements now support interactive Behold payment/resume and automatically decline when the cost is unpayable. | `theorists_sanctum_beholding_a_jace_enters_untapped`, `theorists_sanctum_interactive_behold_resumes_entry_untapped`, `theorists_sanctum_declining_behold_enters_tapped`, and `theorists_sanctum_without_a_jace_enters_tapped_without_prompt` passed. | `supported: true`, `gap_count: 0`; semantic audit has 0 Theorist's Sanctum findings. | Pass: self-review only; see §5(a–i) below. |
| Kíli the Resourceful | Fixed in phase commits `52c928d34` and `4b1a7bedb`: shared activated-ability cost selection offers the static-granted `{0}` Equip alternative once per turn, without card-name dispatch; Equip alternative costs are included in coverage classification. | `kili_alternative_equip_cost_is_story_gated_and_first_per_turn` and `kili_uses_the_normal_equip_cost_without_an_enduring_story` passed; the first activation uses `{0}`, the second pays full cost, and `{0}` returns next turn. | `supported: true`, `gap_count: 0`; semantic audit has 0 Kíli findings. | Pass: self-review only; no independent reviewer was available. |
| Case of the Uneaten Feast | Fixed in phase commit `9ef67fee1269aa26f3e614ebe89620f7d4cae76a`: snapshot GenericEffect recipients at resolution, pin each recipient incarnation, and route object-scoped `GraveyardCastPermission` through the graveyard-casting collector. | `case_of_the_uneaten_feast_grants_only_to_resolution_time_graveyard_creatures` passed: a resolution-time graveyard creature is castable, a later graveyard entrant is not, and the permission expires at cleanup. | `supported: true`, `gap_count: 0`; semantic audit has 0 Case findings. | Pass: self-review only; see §5(a–i) below. |
| Jace's Machinations | Fixed in phase commit `24d8853299066453f8922177e980658aef65ed2a`: intercept the subject-predicate route and lower the duration-bound loyalty timing permission through the existing `ActivateAsInstant` mode. | `jace_loyalty_is_not_activatable_on_an_opponents_turn_without_permission`, `jace_machinations_does_not_grant_opponents_jace_timing`, and `jace_machinations_allows_new_jace_on_opponents_turn_and_preserves_once_per_turn` passed; the full-card parser regression passed. | `supported: true`, `gap_count: 0`; semantic audit has 0 Jace's Machinations findings. | Pass: self-review only; see §5(a–i) below. |
| Uldaros Theorix | Fixed in phase commits `9a8a7017b` and `fb406fbc4`: parse the intervening cast condition, carry `DistinctCardTypes` and a 0..=8 target cardinality from the target-phrase parser, and reuse copy/free-cast machinery with a total mana-value limit. | `casts_copies_for_free_and_enforces_the_total_mana_value_budget`, `choosing_no_copies_leaves_the_original_cards_exiled`, and `uldaros_put_onto_the_battlefield_without_being_cast_has_no_effect` passed. Tests cover instant+sorcery+creature, reject two sorceries and a 1+2+4 budget, accept 2+4, verify selected originals are exiled, and observe free resolution/token creation. | `supported: true`, `gap_count: 0`; semantic audit has 0 Uldaros findings. | Pass: self-review only; no independent review was available. |
| Extrapolate the Impossible | Phase commit `c759b3948` preserves ability-level optionality for exact-count outside-game searches; the runtime implementation in `9a8a7017b` reuses sideboard selection and opponent choice. | `optional_you_may_exact_outside_game_pair_preserves_ability_optionality`, `fewer_than_two_different_names_cannot_reveal_a_partial_pair`, and `caster_reveals_two_distinct_names_then_opponent_puts_only_one_into_hand` passed. | The parser regression now preserves `You may` and avoids the prior swallow warning. End-to-end card-data coverage and semantic audit are deferred to the final sub-batch C gates. | Pass: self-review only; no independent reviewer was available. |
| Bringer of the Last Gift | Fixed in phase commit `cd078932e`: parses “that weren't put there this way” as a negated tracked-set membership filter on the mass graveyard return, reusing the existing sacrifice publication and zone-move exclusion machinery. | `bringer_of_the_last_gift_tracks_sacrificed_creatures_as_excluded`, `returns_creatures_that_were_already_in_each_graveyard`, and `does_not_return_creatures_put_into_graveyards_by_its_sacrifice` passed. | Card-data coverage and semantic audit are deferred to the final sub-batch C gates. | Pass: self-review only; no independent reviewer was available. |
| Ms. Marvel, Kamala Khan | Fixed in phase commits `12d071180`, `f292b2560`, and `5d9db2489`: quoted card-name possessives normalize to the granting-object reference, and CDA parsing reuses the shared self-reference grammar so the temporary base-power static applies to its granter. | `ms_marvel_granted_base_power_static_reaches_the_granter`, `ms_marvel_draws_and_sets_power_until_the_end_of_turn`, and `ms_marvel_does_not_trigger_for_a_spell_targeting_an_opponent_creature` passed. | Card-data coverage and semantic audit are deferred to the final sub-batch C gates. | Pass: self-review only; no independent reviewer was available. |
| Lady Octopus, Inspired Inventor | Fixed in phase commit `8f454ec4b`: the shared `NthDrawThisTurn` constraint now carries a comparator; exact ordinals remain `EQ`, while consecutive first-or-second draw events parse as `LE 2`. | `lady_octopus_triggers_for_first_and_second_draw_only_and_resets_each_turn` and parser regression `trigger_nth_draw_first_or_second_card` passed. | Card-data coverage and semantic audit are deferred to the final sub-batch C gates. | Pass: self-review only; no independent reviewer was available. |

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

### Case of the Uneaten Feast self-review (AI-CONTRIBUTOR §5 a–i)

- **(a) Correct seam — pass.** GenericEffect recipient enumeration now includes the zones named by its filter; the resolution-created object grant is collected by the graveyard-cast permission path.
- **(b) Idiomatic change — pass.** Reuses the existing zone-aware candidate enumeration, transient continuous effects, and static permission collector rather than adding a Case-specific cast path.
- **(c) Parser compliance — pass.** The existing typed `AddStaticMode::GraveyardCastPermission` is retained; no card-name dispatch was added.
- **(d) CR citations — pass.** CR 611.2c fixes the affected object set at resolution, CR 400.7 pins the object's incarnation, and CR 514.2 ends the duration at cleanup.
- **(e) Pattern coverage — pass.** Supports object-scoped, resolution-created graveyard-cast grants generally; player-scoped transient grants remain on their existing path.
- **(f) Logic placement — pass.** Zone enumeration and permission collection live in the engine; no transport or frontend logic changed.
- **(g) Building-block reuse — pass.** Reuses GenericEffect candidate enumeration, `AddStaticMode`, and `transient_graveyard_permission_sources`; commit anchors are recorded in the phase commit message.
- **(h) Bool-flag avoidance — pass.** Uses the existing typed filter and `ObjectIncarnationRef`; no new bool or enum was added.
- **(i) Test discrimination — pass.** The scenario-runner test proves a card already in the graveyard is castable, a later entrant is not, and the grant ends at cleanup.

This was a self-review only; no independent reviewer was used.

### Theorist's Sanctum self-review (AI-CONTRIBUTOR §5 a–i)

- **(a) Correct seam — pass.** The as-enters replacement pauses before battlefield entry, then resumes the same replacement after the Behold choice.
- **(b) Idiomatic change — pass.** Extends the existing `MayCost` replacement, Behold choice, and replacement-resume paths; the commit names reveal-land/shock-land parsing and Mox Diamond/Mimeoplasm payment paths as anchors.
- **(c) Parser compliance — pass.** The replacement parser composes existing nom parsers; no card-name string dispatch was added.
- **(d) CR citations — pass.** CR 118.3 and 614.12a cover the cost and pre-entry choice; CR 701.4a governs the reveal of a Jace card from hand.
- **(e) Pattern coverage — pass.** Handles interactive Behold costs on as-enters replacements, including multiple legal cards, decline, and an unpayable cost.
- **(f) Logic placement — pass.** Cost payment, replacement state, and resumption are handled in engine code.
- **(g) Building-block reuse — pass.** Reuses the existing replacement cost and Behold choice/resume machinery; no parallel card-specific continuation was introduced.
- **(h) Bool-flag avoidance — pass.** No new bool or enum variant was added for Sanctum.
- **(i) Test discrimination — pass.** Four scenario-runner cases cover one Jace, multiple Jaces and selected-card revelation, explicit decline, and no-Jace auto-decline.

This was a self-review only; no independent reviewer was used.

### Jace's Machinations self-review (AI-CONTRIBUTOR §5 a–i)

- **(a) Correct seam — pass.** The full Oracle sentence reaches subject-predicate lowering before imperative/YouMay dispatch; one early interceptor handles it before `unbound_subject`.
- **(b) Idiomatic change — pass.** Reuses `parse_static_line`, `StaticMode::ActivateAsInstant`, and the existing transient continuous-effect path.
- **(c) Parser compliance — pass.** No card-name or verbatim-text dispatch was added; the parsed static definition supplies the mode and filter.
- **(d) CR citations — pass.** CR 304.5 and 606.3 govern timing and once-per-turn loyalty use; CR 611.2a/c governs duration and the live filter for this rules-modifying permission.
- **(e) Pattern coverage — pass.** Handles subtype-qualified loyalty timing permissions rather than naming Jace's Machinations in the engine.
- **(f) Logic placement — pass.** Parsing constructs the effect; the engine's timing permission and loyalty-use tracking remain authoritative.
- **(g) Building-block reuse — pass.** Uses the existing `ActivateAsInstant` static mode and transient-permission machinery; the commit records `MayLookAtFaceDown` and Chandra cast-permission analogues.
- **(h) Bool-flag avoidance — pass.** No new enum variant or flag was added.
- **(i) Test discrimination — pass.** Runtime tests cover opponent-turn and stack timing, absent permission, opponent-controlled Jace exclusion, a Jace created after resolution, and CR 606.3's per-permanent limit; the full-card parser test checks Empower Jace remains present.

This was a self-review only; no independent reviewer was used.

## Final gates

The gate results below first record sub-batch A at its then-current `local-fixes` head
`55b63c91a446a419f7f725cb95797d4f9cbb39d5`, based on pinned upstream
`988bc3412f5d4e475c1837fbc88062b14d1f5489`. Uldaros and Extrapolate are
committed at `fb406fbc4`; their focused parser/runtime tests, Gate A, and patch
reproduction passed. Full-suite, coverage, audit, and smoke gates for these two
cards remain deferred to the final sub-batch B gates. The superproject
submodule pointer remains pinned; no upstream push or PR was made.

- Cryomancer's commit message contains literal `\n\n` separators. The Jace
  commit has the same formatting issue. Both were left unchanged rather than
  rewriting accepted local history.
- Gate A passed against the pinned base on the committed head. Exact output:

  ```text
  Gate G PASS (router/grant architecture: strict router vs permissive grant boundary intact)
  residue-ratchet: tighten crates/phase-parser/src/parser/oracle_cost.rs 3 -> 2
  residue-ratchet: tighten crates/phase-parser/src/parser/oracle_static/static_helpers.rs 1 -> 0 (entry can be deleted)
  Gate H PASS (residue: 121 legacy obligations, ceiling 123; files scanned 91; snapshot worktree)
  Gate A: scanned 8 file(s) under crates/engine/src/parser crates/engine/src/types/text_grammar.rs crates/phase-ast/src/types/text_grammar.rs crates/phase-parser/src/parser (working tree vs 988bc3412f5d4e475c1837fbc88062b14d1f5489).
  Gate A PASS head=55b63c91a446a419f7f725cb95797d4f9cbb39d5 base=988bc3412f5d4e475c1837fbc88062b14d1f5489
  ```
- `cargo fmt --all` passed. The final full `cargo test -p phase-engine` passed:
  13,790 engine unit tests, 15,603 tests across the other targets, and zero
  failures; 4 engine unit, 12 other-target, and 2 doc tests were ignored.
  The first full run exposed two old Karoo tests expecting a choice when
  `MayCost` was unpayable. The follow-up test commit `55b63c91a` keeps the
  voluntary-decline case payable and asserts the unpayable auto-decline path;
  both focused tests and the final full suite pass.
- Baseline card data at
  `/home/ubuntu/repos/eng/pdata/baseline-988bc34-card-data.json` has SHA-256
  `45bb5ca39612a82b255c121f0fba69d2a688b832242aad0b1a38e36f575ed8c1`.
  Final patched data is at
  `/home/ubuntu/repos/eng/pdata/patched/card-data.json`, SHA-256
  `a6172384ac49eff2105c12f6b2ca991be9a4dd871352cd01b6c6064152ec3e45`.
  The baseline metadata is MTGJSON `5.3.0+20261107`; the regenerated patched
  data is `5.3.0+20261108`. Coverage increased from 32,303/36,280 to
  32,321/36,280 (89.1%). All ten fixed targets are supported with `gap_count: 0`.
- The supported-status diff against the requested `45bb...` baseline found
  zero supported-to-unsupported flips. Semantic audit covered 32,835 supported
  cards, with 191 global findings; Case, Theorist's Sanctum, and Jace's
  Machinations each had zero findings.
- Parsed-ability review:
  - The “this way” / Essence Burn “exile it instead” slice had eight changed
    records. The target-card changes are improvements for Anzrag's Rampage,
    Essence Burn, Rottenmouth Viper, Seasoned Cryomancer, and Vren.
    Non-target improvements: Narset of the Ancient Way now retains its
    reflexive target and mana-value amount; Thorin, Mountain-king retains its
    own target instead of inheriting `ParentTarget`; Vivien's Invocation
    retains its opponent-creature target and power amount. No regression was
    found in this slice.
  - `GraveyardCastPermission` markers were present on 76 cards in both data
    sets, with no changed records. `MayCost` markers rose from 36 to 37, only
    Theorist's Sanctum; `ActivateAsInstant` markers rose from 5 to 6, only
    Jace's Machinations. There were no non-target changes in these three
    permission/replacement classes.
  - Snaremaster Sprite's trigger now preserves the opponent-controller filter
    on its tap effect (improvement; it remains supported). Wurmquake's
    `Then for each opponent` clause remains explicitly unimplemented and the
    card remains unsupported (neutral, no support regression).
- Re-exported 14 patches (`0001`–`0014`). The apply-script check from the
  clean pinned submodule reproduced tree
  `1a6c8ea136cc1ecabcdde3fa60a8e965011cf1a4`, matching `local-fixes`; the
  submodule was returned to `local-fixes`.
- `cargo build --release -p phase-env` passed (existing resolver and
  `Out`-is-never-constructed warnings). The smoke game with two copies of each
  fixed card in both decks reached `game_over` (winner P0, 922 trace events)
  using the patched data root, with `unresolved: []`.
- Card-data generation reported 554 parser warnings across three categories.
  The generator-dirtied `known-tokens.toml` and `mtgjson-vintage` were reverted.

The engine implementation reviews recorded here were self-reviews only; no
independent review was performed.

## Sub-batch B final gates

The final `local-fixes` head is `4b1a7bedbd2698f69bb474cf99c80f0a0d27afb0`,
based on `988bc3412f5d4e475c1837fbc88062b14d1f5489`. The phase submodule remains
pinned in the superproject; only the local patch series is exported. No upstream
PR or push was made.

- Gate A passed against the pinned base: scanned 17 parser files; Gate G/H
  passed with 121 residue obligations under the 123 ceiling.
- `cargo fmt --all` and the full `cargo test -p phase-engine` passed. The run
  reported 29,404 passed, 0 failed, and 18 ignored tests across unit,
  integration, and doc-test targets. Kíli's two runtime tests and Urgent
  Necropsy's three runtime tests passed.
- Regenerated card data is
  `/home/ubuntu/repos/eng/pdata/patched/card-data.json`, SHA-256
  `77eccbfd664ee9ec42f7fd742d8755e3f24adcefda83a3400c61c323ed640151`.
  Coverage is 32,326/36,280 (89.1%), up from 32,303/36,280 in the baseline;
  the supported-to-unsupported comparison found zero flips.
- Coverage and semantic audit: Uldaros, Kíli, and Urgent Necropsy are
  `supported: true`, `gap_count: 0`, with zero semantic findings. Extrapolate
  remains unsupported with one `Swallow:Optional_YouMay` gap; although its
  runtime test reaches the optional reveal, it is not included in the semantic
  audit. The audit covered 32,839 supported cards and found 191 global
  findings.
- Parsed-ability diff is saved in
  `/home/ubuntu/repos/eng/pdata/patched/parsed-ability-diff.json`. The
  non-target changes relevant to the new seams were expected: Bruenor
  Battlehammer and Forge Anew now parse Equip alternatives; Gavi, Nest Warden
  and New Perspectives receive Cycling alternative-cost coverage; Conspiracy
  Unraveler's fixed Collect Evidence amount is represented as `Fixed(10)`.
  No non-target changes were found for cast-copy, “of each card type,” or
  outside-game/Wish/Learn classes.
- Card-data generation reported 553 parser warnings in three categories. The
  generated `known-tokens.toml` and `mtgjson-vintage` changes were reverted.
- Re-exported 21 phase patches. The apply-script check reproduced tree
  `c5e81a62fa669acfa138001112f61edaa0639442`, matching `local-fixes`.
- `cargo build --release -p phase-env` passed (workspace resolver and unused
  `Out` warnings). The smoke game included all 14 fixed cards in both decks,
  plus `Opt` and `Shock` in each sideboard; it reached `game_over` after 75
  turns and 1,863 decisions with no unresolved cards. The focused Extrapolate
  runtime tests verify the outside-game selection and opponent choice.

## Sub-batch C interim gates

The current `local-fixes` head is `5d9db248957a3cab4ad3a6651be87ea72c4d3945`,
based on `988bc3412f5d4e475c1837fbc88062b14d1f5489`.

- Extrapolate's focused parser/runtime tests passed. Bringer's target-phrase
  parser test, full-card parser regression, and both runtime tests passed;
  `cargo fmt --all` passed. Ms. Marvel's focused integration tests passed
  (3/3), Lady Octopus's runtime test passed (1/1), and the shared CDA parser
  regression passed (1/1).
- Gate A passed against the pinned base: 19 parser files scanned; Gate G/H
  passed with 121 residue obligations under the 123 ceiling.
- Re-exported 27 phase patches. The apply-script check reproduced tree
  `a2861af3485b72ed9d1ba8115537f728269c4d16`, matching `local-fixes`.
- Firebender Ascension now uses a same-pass `AbilityTriggered` event carrying
  the triggering stack-entry identity; the matcher checks the attacking
  creature and attack-declaration cause, and the copy effect resolves that
  exact entry through the existing triggered-ability copy path. If the original
  entry has left the stack, the existing `CopySourceLookup::Gone` behavior
  resolves the copy effect as a no-op, without falling back to another entry.
  Focused runtime tests cover Firebending, the fourth-counter choice, accepting
  and declining the copy, a combat-damage trigger, and an opponent's attacker.
- Final Firebender phase commits are `09534e898e7bd56c5292befcf3e511c30e24d347`
  and `7c0fde963`; exported as patches 0028 and 0029.

## Final 18-card gates

- The corrected `local-fixes` head is
  `34ab5f693a6c5c383a8eccab25f4bbf1fa0af37c`, based on
  `988bc3412f5d4e475c1837fbc88062b14d1f5489`. The CDA regression was
  introduced by `5d9db2489`: the shared Ms. Marvel self-reference change
  stopped recognizing the generic pronoun “its,” leaving Gaea's Liege's
  dynamic power/toughness modifiers unparsed. The shared parser now maps
  “its” to `SelfRef`; a parser regression asserts both dynamic modifiers for
  Gaea's Liege. No card-name special case is used.
- `cargo fmt --all` and Gate A passed. Gate G/H passed with 121 residue
  obligations under the 123 ceiling; 19 parser files were scanned.
- Full `cargo test -p phase-engine` passed: 29,417 passed, 0 failed, 18
  ignored. This includes 13,791 library tests (4 ignored), 90 binary tests,
  15,536 integration tests (12 ignored), and 2 ignored doc tests.
  `issue_6877_departed_triggering_spell::older_trigger_copies_its_own_cast_not_a_later_recast`
  passed 10/10 on the fixed head and 10/10 on pinned base `988bc34`; no
  flakiness was observed. The earlier head-only trigger-identity regression
  was fixed by limiting identity reservation to attack-caused triggers
  Firebender observes.
- Regenerated data is in `/home/ubuntu/repos/eng/pdata/patched/`:
  `card-data.json` SHA-256
  `54ebe1c98e5d15de9d3e844845cb96a7cd87e3229957222616bec782827a8d6a`,
  and coverage is 32,331/36,280 (89.1%), up from 32,303. The flip comparison
  against the pinned-base coverage generated from the requested
  `45bb5ca39612` baseline found zero supported-to-unsupported flips and 28
  newly supported cards. All 18 target cards are supported with
  `gap_count: 0`.
- The corrected parsed-ability diff compares coverage snapshots (the format
  consumed by `coverage-parse-diff`) and reports 41 changed cards and 53
  signatures against the `45bb5ca3` baseline. Its Markdown and JSON artifacts
  are in `/home/ubuntu/repos/eng/pdata/patched/parsed-ability-diff.{md,json}`.
  It includes the expected shared Equip, Cycling, tracked-set, trigger,
  target-filter, and fixed Collect Evidence parser changes; there were no
  support regressions.
- Semantic audit was not rerun for this corrective parser commit; the
  previously recorded audit predates the Gaea's Liege correction.
- Re-exported 30 patches. The apply-script check reproduced tree
  `b55566fad463e2e8f99ebef1c27f9af24cf0137e`, matching the tree at
  `local-fixes`; the phase submodule was returned to `local-fixes`.
- Card-data generation ran in an isolated worktree; generator side effects
  were not applied to the active phase worktree.

## Patch 0031 — Anzrag's Rampage

- Patch 0031 now exports 8 Phase files, with 930 insertions and 69 deletions.
  The production-entry-point Burning-Rune Demon test covers count-one routing:
  the opponent-selected card goes to hand and its complement goes to the
  graveyard.
- `singular_zone_choice_routing` passes normally (1/1) and fails when the
  `!` in `!chosen.is_empty()` is manually deleted. The selected card then
  reaches the graveyard instead of the hand.
- A current `ChangeZone::resolve -> Ok(None)` mutant was caught by the
  Anzrag/Kíli filters (4 pass, 6 fail); removing the ChooseFromZone decline
  guard makes both Anzrag decline tests fail with a second Exile choice.
- The earlier full mutants run reported 4 caught, 2 missed, 16 timeouts, and
  2 unviable. It has not been rerun after adding the focused routing module.
