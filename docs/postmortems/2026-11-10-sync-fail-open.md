# Synchronization failed open after XMage loop handling

## Impact

In Boros Dwarves vs. Dimir Midrange runs, Mage-Bench's interaction guard auto-passed XMage decisions while Phase still exposed meaningful choices. The driver treated the resulting non-pending status as a reason to pass priority again, eventually timing out instead of recording the synchronization loss. This obscured the first divergence point; it does not establish an engine rules bug.

## Root cause

The loop guard was designed to stop LLM pilots from repeatedly interacting, but differential runs use deterministic policies and need every decision surfaced. The external-player launch path did not expose the existing interaction-limit setting. Separately, `_sync_xmage_to_decision` re-passed on any `action_pending: false` result, including unknown statuses and the loop-guard result.

## New check

Differential config now sets the existing limit for both external players. The bridge result still fails closed as `xmage_loop_guard`; unknown non-pending result shapes raise `UnmappedPromptError`. Regression tests cover the loop-guard no-repass path and unknown statuses. Mage-Bench's external-player unit tests cover parsing and bridge launch arguments.
