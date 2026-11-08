# mtg-rl-sim

Path toward a superhuman Standard MTG agent via self-play RL. Context and prior art live in the [idea journal](https://github.com/kevinbrar/ideas/blob/main/ideas/mtg-self-play-rl.md).

## Layout

- `phase/` — pinned submodule of [phase-rs](https://github.com/phase-rs/phase) at `988bc34` (v0.120.0). The rules engine (~98% of Standard parsed).
- `phase-env/` — Rust bin wrapping the engine in a line-delimited JSON protocol: emits each decision point with the engine-issued legal-action list and a compact state snapshot; accepts a chosen index/action on stdin. This is both the RL-env spine and the phase side of the differential harness.
- `driver/` — Python client + scripts.

## Card data

`card-data.json` must match the engine commit. Fetch the hashed file named in `card-data-meta.json`:

```bash
curl -O https://data.phase-rs.dev/card-data-5b455542437560f2.json   # for 988bc34
mv card-data-*.json <data-dir>/card-data.json
```

## Local phase-rs parser patches

The `phase/` submodule remains pinned at `988bc3412f5d4e475c1837fbc88062b14d1f5489`;
local parser fixes are stored as patches in [`phase-patches/`](phase-patches/).
With a clean submodule worktree, apply the series using:

```bash
./scripts/apply-phase-patches.sh
```

The script checks out the pinned commit detached and reapplies the patch series,
so it can be rerun to reproduce the same local parser. The card data generated
from that patched parser is copied to
`~/repos/eng/pdata/patched/card-data.json`; use
`~/repos/eng/pdata/patched/` as the `phase-env` data root when running against
the local fixes. Do not stage or commit generator side effects from
`gen-card-data.sh`.

## Build & run

```bash
rustup toolchain install nightly-2026-04-19   # phase pins nightly
cargo build --release -p phase-env
python driver/phase_env.py                    # see PhaseEnv.play()
```

Run the lockstep oracle after applying the local mage-bench patch described in
[`oracle/README.md`](oracle/README.md):

```bash
python driver/diff_game.py oracle/decks/mono-green-v0.txt \
  oracle/decks/mono-green-v0.txt --seed 17
```

## Status

- [x] phase-env JSONL driver; random-policy games complete end-to-end
- [x] surface unresolved deck-card names and reject them in the differential driver
- [x] XMage oracle channel proven: headless game with custom .dck decks driven via mage-bench MCP bridge (see `oracle/`)
- [x] XMage/phase sync-point v0 harness (meaningful decisions and legal sets compared; terminal board snapshot unavailable after XMage closes its bridge)
- [ ] per-card / per-mechanic differential suite over the meta card pool (~600 cards)
- [ ] batched env + training scaffolding
