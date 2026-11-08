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

## Build & run

```bash
rustup toolchain install nightly-2026-04-19   # phase pins nightly
cargo build --release -p phase-env
python driver/phase_env.py                    # see PhaseEnv.play()
```

## Status

- [x] phase-env JSONL driver; random-policy games complete end-to-end
- [ ] surface unresolved deck-card names (currently skipped silently upstream)
- [ ] XMage oracle via mage-bench bridge + state-diff comparators
- [ ] per-card / per-mechanic differential suite over the meta card pool (~600 cards)
- [ ] batched env + training scaffolding
