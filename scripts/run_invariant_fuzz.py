"""Run seeded random phase-rs games over every deck pair and check engine invariants.

Usage: python scripts/run_invariant_fuzz.py --games 5 --outdir runs/fuzz/local
Each pair runs `invariant-fuzz` (phase-env) for seeds first-seed..first-seed+games-1.
Exits 1 when any game reports a violation that is not a known signature.
"""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def summarize(reports: dict[str, list[dict]]) -> dict:
    """Aggregate per-pair JSONL records into one summary."""
    totals: dict[str, int] = {}
    failures = []
    games = decisions = 0
    for pair, records in sorted(reports.items()):
        for record in records:
            if record.get("summary"):
                continue
            games += 1
            decisions += record.get("decisions", 0)
            for violation in record["violations"]:
                known = violation.get("known", False)
                key = ("known:" if known else "") + violation["kind"]
                totals[key] = totals.get(key, 0) + 1
                if not known:
                    failures.append(
                        {
                            "pair": pair,
                            "seed": record["seed"],
                            "kind": violation["kind"],
                            "signature": violation.get("signature"),
                            "detail": violation["detail"][:400],
                        }
                    )
    return {
        "games": games,
        "decisions": decisions,
        "totals": totals,
        "failures": failures,
    }


def run_pair(binary: Path, data_root: Path, a: Path, b: Path, args, outdir: Path):
    pair = f"{a.stem}--{b.stem}"
    command = [str(binary), str(data_root), str(a), str(b)]
    command += [str(args.first_seed), str(args.games), str(args.known)]
    result = subprocess.run(command, capture_output=True, text=True)
    (outdir / f"{pair}.jsonl").write_text(result.stdout)
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"{pair}: invariant-fuzz exited {result.returncode}: {result.stderr}"
        )
    return pair, [json.loads(line) for line in result.stdout.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=5)
    parser.add_argument("--first-seed", type=int, default=1)
    parser.add_argument("--decks", type=Path, default=ROOT / "oracle/decks")
    parser.add_argument("--known", type=Path, default=ROOT / "ci/fuzz-known.txt")
    parser.add_argument("--data-root", type=Path, default=ROOT / "phase/client/public")
    parser.add_argument(
        "--binary", type=Path, default=ROOT / "target/release/invariant-fuzz"
    )
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args()

    decks = sorted(args.decks.glob("*.txt"))
    if not decks:
        raise SystemExit(f"no decks in {args.decks}")
    args.outdir.mkdir(parents=True, exist_ok=True)
    pairs = [(a, b) for a in decks for b in decks]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = pool.map(
            lambda pair: run_pair(
                args.binary, args.data_root, *pair, args, args.outdir
            ),
            pairs,
        )
        summary = summarize(dict(results))
    (args.outdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: summary[k] for k in ("games", "decisions", "totals")}))
    for failure in summary["failures"]:
        print(json.dumps(failure), file=sys.stderr)
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
