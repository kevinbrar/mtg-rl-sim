# Postmortem: scry polarity masked by duplicate cards

_2026-11-10. Blameless; written by Devin._

## Impact

Boros Dwarves vs. Dimir Midrange, seed 42, diverged at step 24: Phase drew Kaito, Bane of Nightmares while XMage drew Dream Beavers. The two engines had previously resolved two Dream Beavers scry-1 triggers. The mismatch was in the harness's scry action translation, not evidence of an engine rules bug.

## Root cause

PR #4 mapped the action semantics from a misreading. The legal-set comparison matched names only, and identical copies masked the reordering until a later draw. In Phase, `SelectCards.cards` is the keep-on-top set; the complement is bottomed.

## New check

The driver tests now assert both scry-1 mappings (empty keep-on-top set to the XMage bottom-choice ID; the looked-at card in the keep-on-top set to `choice=no`) and fail closed for multi-card scry prompts. A seed-42 diagnostic fixture pins the observed Phase action and XMage prompt to the Phase handler semantics in `engine_resolution_choices.rs` and CR 701.22a.

## Pending gap

XMage's bridge exposes library size but not library order/top. Consequently, a scry-order divergence may remain hidden when duplicate names are on top and only become observable at the next draw.
