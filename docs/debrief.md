# Debrief: Standard simulator work so far

## What was done

1. **Choosing an engine.** I surveyed the existing MTG engines: XMage, Forge, mtg-kernel, phase-rs, DeepScry and forge-rules-rs. I picked [phase-rs](https://github.com/phase-rs/phase) for three reasons:
   - It covers about 98% of Standard.
   - Its cards are built by parsing the rules text, which suits an LLM workflow.
   - It runs a full game with its built-in AI in about 0.6 s per core.

   I pinned it at upstream commit `988bc34` as a git submodule.
2. **Measuring the gap.** I checked phase-rs against the 60 current Standard archetypes on MTGGoldfish, about 98% of the meta. Those decks use 599 distinct non-basic cards, and 18 of them were unsupported.
3. **Python/Rust interface** (`phase-env/`, `driver/phase_env.py`). A small Rust binary drives a seeded phase-rs game over JSONL: at each decision it emits the legal actions and a compact board state, and the caller picks one. Random-vs-random games run end to end with real meta decks.
4. **XMage comparison harness** (`oracle/`, commit `16cf76b`). It plays the same game in phase-rs and in XMage, using the mage-bench bridge with XMage's shuffle disabled. Both engines get identical library order, starting player and choices. At every meaningful decision it compares:
   - life totals and every zone
   - which permanents are tapped, and who controls them
   - the stack
   - the turn and phase
   - the set of legal actions
5. **Fixing all 18 missing cards** (30 commits on phase-rs, exported to `phase-patches/`). Those 18 cards appear in 25 of the 60 meta decks. About half of the fixes are parser rules; the rest are engine changes:
   - casting from the graveyard for one specific card (Case of the Uneaten Feast)
   - an interactive Behold cost (Theorist's Sanctum)
   - instant-speed loyalty abilities (Jace's Machinations)
   - targeting one card of each card type, and casting copies up to a total mana value (Uldaros)
   - fetching from outside the game with the opponent choosing (Extrapolate)
   - a collect-evidence cost that depends on the targets (Urgent Necropsy)
   - a conditional alternative equip cost (Kíli)
   - copying triggered abilities (Firebender Ascension)
6. **Upstream courtesy offer.** I opened [phase-rs#10736](https://github.com/phase-rs/phase/issues/10736) instead of PRs. phase-rs's AI-contributor policy rules out PRs from us: it requires one card per PR and a verified model, among other things. The patches are public on [kevinbrar/phase@standard-meta-fixes](https://github.com/kevinbrar/phase/tree/standard-meta-fixes).

## How confident can we be that it works?

Evidence, strongest first:

| Check | Result | What it proves |
| --- | --- | --- |
| XMage comparison, 3 full games (14–19 turns) | 0 meaningful mismatches | The harness works, and phase-rs matches XMage on basic play: turn structure, combat, mana, creatures. |
| Runtime test per fixed card | All pass; each failed before its fix | Each card does what its rules text says, **as I understood the text**. |
| Full phase-rs engine suite | 29,417 passed, 0 failed | The fixes didn't break existing behavior that phase-rs tests. |
| phase-rs coverage check | All 18 at `gap_count: 0`; 0 cards went from supported to unsupported; 28 cards outside our list became supported | Every clause on these cards now parses into something the engine runs. |
| phase-rs semantic audit | None of the 18 flagged | The parsed result passes the audit's consistency checks. |
| AI-vs-AI game with all 18 cards in both decks | Reached game over in 24 turns and 390 decisions, nothing unresolved | Nothing crashes or hangs in real play. |

**What is not yet proven** (I'd rate these honestly as open):

- **The 18 fixes haven't been checked against XMage.** Their tests were written by the same agent that wrote the fixes, so a misreading of the rules would show up in both. An independent check would close this: the XMage harness on decks with these cards, or porting XMage's own tests for them.
- **The XMage comparison has only used a simple deck.** All three games used mono-green creatures with no targets or triggers. Real meta decks, with the stack, triggers, replacement effects and copies, haven't been compared yet. Most real bugs will be found there.
- **"Supported" doesn't mean correct.** phase-rs's audit flags 191 issues across all cards; I didn't count how many are Standard cards. Its known-misparse backlog may affect up to 62 meta cards (a rough count). Four meta cards are flagged as playing wrong, including Tishana's Tidebinder.
- **Side effects on other cards.** The parse changed for 26 cards outside our list. They're covered only by the general test suite, not by tests of their own.
- **Review was self-review.** phase-rs's process has a separate review step; this work had no independent reviewer.

**Plan for confidence:** run the XMage comparison continuously on real meta decks (the "sample ~1% of games" idea), keep sampling from the current training policy so the agent can't learn to exploit a bug undetected, and add targeted tests for rare interactions.

## Problems encountered (and fixes)

- **Mismatched card data.** phase-rs's default card-data download can be newer than the engine, which fails with `unknown variant`. Fix: use the data file that matches the pinned commit.
- **XMage can't be seeded.** The harness turns off its opening shuffle and gives both engines the same library order.
- **Uldaros targeting.** The parser understood "one card of each type", but the engine still offered only one target slot. Fixed in the shared targeting code, not just for this card.
- **Urgent Necropsy's cost** only counted the first of its four targets. Fixed by reusing the existing helper that collects targets across the whole spell.
- **Kíli's free equip** never applied. Fixed by extending the existing support for "pay one of these costs" to activated abilities.
- **Extrapolate** worked in its tests, but the coverage check said the "you may" was being dropped. Fixed in the parser.
- **Regression: Gaea's Liege broke.** The Ms. Marvel parser change stopped it working, and the final coverage check caught it. Fixed generically (handling "its" as self-reference) with a new regression test.
- **Firebender Ascension's first version** interfered with an unrelated trigger-copy test. Narrowed so it only affects attack triggers; that test now passes 10 of 10 runs.
- **Test-count confusion.** One run reported about 3k tests (a subset). The real full suite is about 29.4k, and that's what the final numbers use.
- **Upstream policy.** PRs weren't possible within phase-rs's rules, so the fixes were offered through an issue instead.

## Other things to know

- **One behavior change:** for "you may pay X; if you don't, Y", if X can't be paid the engine now applies Y without asking. Two existing tests were updated for this.
- **Upstream risk:** phase-rs is a few months old, has essentially one maintainer, and releases several times a week. Our engine is 20 commits behind upstream (as of 2026-11-09). Updating means rebasing the 30 patches and regenerating card data; `scripts/apply-phase-patches.sh` recreates our engine from the pin.
- **Speed is fine for RL:** about 13 games per second on 8 cores with phase-rs's built-in AI. The engine alone, without the AI, hasn't been measured.
- **Standard rotates.** Each new set adds unsupported cards, so the gap check needs re-running every set.

## Next

1. A UI for watching and playing games, using phase-rs's own web client with our patched engine (in progress).
2. Run the XMage comparison on real meta decks.
3. A batched Python training environment, then the RL/self-play research.
