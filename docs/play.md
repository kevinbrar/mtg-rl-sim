# Playing against the patched engine

phase-rs ships a full web client: you play against its built-in AI, choose formats and decks, and can review the game log. This runs that client locally on our patched engine and card data.

```bash
./scripts/apply-phase-patches.sh                      # phase at 988bc34 + our 30 patches
cd phase
rustup target add wasm32-unknown-unknown
cargo install wasm-bindgen-cli@<version in Cargo.lock>
CARGO_INCREMENTAL=0 ./scripts/build-wasm.sh           # ~8 min; incremental cache can exceed 12 GB
cp ../../pdata/patched/card-data.json client/public/card-data-<hash16>.json
#   also update client/public/card-data-meta.json (data_hash, data_filename, commit) to match
cd client && pnpm install --frozen-lockfile           # Node 24 (.nvmrc)
CARD_DATA_URL=/card-data-<hash16>.json pnpm dev --host 0.0.0.0
```

Open http://localhost:5173 → Play vs AI → Format: Standard → pick a deck → Start Match. The version tooltip in the sidebar shows which engine commit generated the card data; it should be ours.

Don't use Tilt here: it regenerates card data from scratch, and direct cargo builds conflict with it.
