# Local phase-rs fixes

The phase submodule remains pinned at `988bc3412f5d4e475c1837fbc88062b14d1f5489`.
Card fixes are being developed on the local `local-fixes` branch, then exported
as a patch series after the batch is complete. No upstream PRs or pushes are
planned.

## Card status

| Card | Status | Runtime test | Coverage / semantic audit | Self-review |
| --- | --- | --- | --- | --- |
| Restless Prairie | Fixed locally: the coverage subtype lexicon now includes the canonical parser vocabulary as well as printed type-line subtypes. The Oracle text's Llama subtype is valid under CR 205.3m but does not appear on a printed type line. | `restless_prairie_becomes_a_3_3_llama_and_remains_a_land` passed; `GameRunner::activate` resolves the real Oracle-text ability to a 3/3 Llama that remains a land. | At that card's checkpoint, coverage was 32,307/36,280 (89.05%); the current aggregate after Rottenmouth Viper is 32,309/36,280 (89.1%). The targeted regression test confirms Llama no longer produces an invalid-subtype finding. The existing semantic-audit output has no Restless Prairie finding. | Pass: general vocabulary union; existing `text_grammar::parse_subtype` and `oracle_util::parse_creature_subtype` are analogous canonical-list consumers. Existing unknown-subtype test still covers the filler-word rejection path. |
| Rottenmouth Viper | Fixed locally, uncommitted: `starts_prefix_clause` now keeps a sequence-connected `Then for each` prefix with its comma-delimited effect body, allowing the existing counter-count parser to attach `repeat_for` to the scoped unless effect. | `viper_attack_repeats_the_unless_clause_for_each_blight_counter` passed with the printed Oracle text: one seeded blight counter plus the attack trigger's counter produces two separate P1 prompts and two 4-life losses; P0 remains at 20 and the trigger completes. Parser regressions `then_for_each_prefix_keeps_its_effect_body_together` and `trigger_for_each_blight_counter_repeats_scoped_unless` passed. Existing Viper attack, ETB, sacrifice, and three-player integration tests passed. | `cargo coverage`: `supported: true`, `gap_count: 0` for Rottenmouth Viper; aggregate 32,309/36,280 (89.1%). `cargo semantic-audit`: no Rottenmouth Viper entry in `flagged_cards` (191 cards have findings globally). | Pass: self-review only; see AI-CONTRIBUTOR §5(a–i) below. |
| Lost in the Maze | Fixed locally: anaphoric tracked-set parsing now preserves controller-qualified typed filters such as “each of those creatures you don't control.” | `lost_in_the_maze_stuns_only_targeted_creatures_not_controlled_by_its_controller` passed through the scenario runner; casts the printed Oracle text for X=3 and verifies all targets tap, only an opponent-controlled target gets a stun counter, a stolen opponent-owned target does not, and an untargeted creature is unaffected. Baseline failed at the opponent's missing stun counter. Parser regression `each_of_those_creatures_filters_the_tracked_set_by_controller` passed. | `cargo coverage`: `supported: true`, `gap_count: 0`; aggregate 32,310/36,280 (89.1%). `cargo semantic-audit` has no Lost in the Maze finding. | Pass: self-review only; see AI-CONTRIBUTOR §5(a–i) below. |
| Seasoned Cryomancer | Pending | — | — | — |
| Essence Burn | Pending | — | — | — |
| Anzrag's Rampage | Pending | — | — | — |
| Urgent Necropsy | Pending | — | — | — |
| Vren, the Relentless | Pending | — | — | — |
| Theorist's Sanctum | Pending | — | — | — |
| Kíli the Resourceful | Pending | — | — | — |

Case of the Uneaten Feast is parked. Its saved, unapplied WIP and casting-permission seam analysis are in
`wip/case-of-the-uneaten-feast.diff`.

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

This was a self-review only; no independent reviewer was used. The card fix remains uncommitted for lead review.

## Final gates

- Gate A (`scripts/check-parser-combinators.sh`) passed against pinned base `988bc3412f5d4e475c1837fbc88062b14d1f5489` after staging the three parser files for inspection: `Gate A: scanned 2 file(s) under crates/engine/src/parser crates/engine/src/types/text_grammar.rs crates/phase-ast/src/types/text_grammar.rs crates/phase-parser/src/parser (staged index vs 988bc3412f5d4e475c1837fbc88062b14d1f5489).` `Gate A PASS head=988bc3412f5d4e475c1837fbc88062b14d1f5489 base=988bc3412f5d4e475c1837fbc88062b14d1f5489`. Gate H also passed (121 legacy obligations, ceiling 123; 91 files scanned; snapshot index). The parser files were unstaged after the check.
- Targeted regressions for Restless Prairie, the subtype lexicon, Rottenmouth Viper, scoped-unless behavior, and the `Then for each` splitter passed. `cargo test -p phase-engine`: 13,790 unit tests and 3,179 integration tests passed; 4 unit and 8 integration tests ignored; doc tests had 0 passed and 2 ignored.
- `cargo build --release -p phase-env` passed (existing warning: `phase-env/src/main.rs:38`, `Out` is never constructed). A random `phase-env` game with Restless Prairie, Rottenmouth Viper, and Lost in the Maze in both decks reached `game_over` in 37 turns / 2,233 steps, winner P0, `unresolved: []`.
- Final generated data was copied to `~/repos/eng/pdata/patched/card-data.json`; SHA-256 `d7f2ccf5f09e9b6538983b83d1f8cd794ae734b09ff552d758efbd7933018790` matches `client/public/card-data.json`.
- Patch export and clean-submodule apply-script reproduction remain pending until the per-card commits exist.
