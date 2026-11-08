#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PHASE="$ROOT/phase"
PATCH_DIR="$ROOT/phase-patches"
PINNED=988bc3412f5d4e475c1837fbc88062b14d1f5489

if [[ ! -d "$PHASE/.git" && ! -f "$PHASE/.git" ]]; then
    echo "error: phase submodule not found at $PHASE" >&2
    exit 1
fi

shopt -s nullglob
patches=("$PATCH_DIR"/*.patch)
if ((${#patches[@]} == 0)); then
    echo "error: no phase patches found in $PATCH_DIR" >&2
    exit 1
fi

if [[ -n "$(git -C "$PHASE" status --porcelain --untracked-files=all)" ]]; then
    echo "error: phase submodule must have a clean worktree before applying patches" >&2
    exit 1
fi

git -C "$PHASE" cat-file -e "$PINNED^{commit}"
git -C "$PHASE" checkout --detach "$PINNED"
git -C "$PHASE" am "${patches[@]}"

printf 'Applied phase patches at %s\n' "$(git -C "$PHASE" rev-parse HEAD)"
