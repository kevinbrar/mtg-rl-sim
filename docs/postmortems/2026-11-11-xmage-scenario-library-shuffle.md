# XMage scenario library fixtures were shuffled

## Impact

Per-card scenarios that supplied `library_top` did not reliably preserve their fixture order in XMage. The Essence Burn suite used empty libraries, so it could not expose the defect. Bringer and Ms. Marvel matches made before a nonempty-library regression could therefore pass without establishing that the XMage fixtures were ordered as intended.

PR #19 stated that ordered libraries were handled. That claim was incomplete: the runner inserted the fixture cards in the correct order for XMage's top-insertion API, but did not suppress XMage's subsequent initial shuffle.

## Root cause

`CardScenarioProjectionTest.addScenarioSetup()` reads `library_top` in fixture order and adds cards in reverse. `CardTestPlayerAPIImpl.addCard(Zone.LIBRARY, ...)` documents that each added card is put on top, so reverse insertion correctly makes the first fixture card the top card. The test runner did not call the available `skipInitShuffling()` API before `execute()`, allowing initial game setup to permute that library.

The Phase runner retained `library_top` as an ordered list. With no draw, the resulting projections exposed the XMage permutation; with a draw, a wrong card could be drawn.

## Corrective check

The scenario runner now calls `skipInitShuffling()`. A Mage.Tests regression uses three distinct library cards, checks their order before any draw, draws once, and checks both the drawn card and the remaining top-first order. Keep library order significant in the cross-engine comparator.

## Follow-up

The three-card regression passes in the final patched Mage-Bench tree. Bringer B1–B3 and Ms. Marvel M1–M5 were rerun with nonempty fixture libraries and all matched. Vren V1/V3/V4 also matched with deterministic library projections. Essence Burn S1–S3/S5 matched; S4's `reach` and legal targets matched, but the engines tapped different fixture labels for two identical Mountains while casting Shock. This label-only choice is recorded as an unsupported comparator limitation in `oracle/known-diffs.toml`. Separately, Vren V2 remains a Phase engine P/T bug after forced layer recomputation; see the corresponding known-diff entry.
