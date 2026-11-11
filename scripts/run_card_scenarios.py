#!/usr/bin/env python3
"""Run TOML card scenarios against the Phase oracle-scenario binary."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_specs(paths: list[Path]) -> dict:
    scenarios = []
    for path in paths:
        with path.open("rb") as source:
            data = tomllib.load(source)
        scenarios.extend(data.get("scenarios", []))
    names = [scenario["name"] for scenario in scenarios]
    if len(names) != len(set(names)):
        raise ValueError("scenario names must be unique across spec files")
    return {"scenarios": scenarios}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("specs", nargs="+", type=Path)
    parser.add_argument(
        "--phase-bin", type=Path, default=ROOT / "target/release/oracle-scenario"
    )
    parser.add_argument(
        "--card-data",
        type=Path,
        default=Path(
            os.environ.get(
                "PHASE_CARD_DATA", str(ROOT / "phase/client/public/card-data.json")
            )
        ),
    )
    parser.add_argument("--outdir", type=Path)
    args = parser.parse_args()

    paths = [path if path.is_absolute() else ROOT / path for path in args.specs]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        print(f"missing card scenario spec(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    try:
        spec = load_specs(paths)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as error:
        print(f"invalid scenario spec: {error}", file=sys.stderr)
        return 2
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    outdir = args.outdir or ROOT / "runs/card-scenarios" / stamp
    if not outdir.is_absolute():
        outdir = ROOT / outdir
    outdir.mkdir(parents=True, exist_ok=True)
    spec_path = outdir / "scenario-spec.json"
    spec_path.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    phase_bin = (
        args.phase_bin if args.phase_bin.is_absolute() else ROOT / args.phase_bin
    )
    card_data = (
        args.card_data if args.card_data.is_absolute() else ROOT / args.card_data
    )
    phase = subprocess.run(
        [str(phase_bin), str(card_data), str(spec_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    (outdir / "phase.log").write_text(phase.stdout + phase.stderr, encoding="utf-8")
    if phase.returncode:
        print(
            f"Phase runner exited {phase.returncode}; see {outdir / 'phase.log'}",
            file=sys.stderr,
        )
        return phase.returncode
    try:
        phase_results = json.loads(phase.stdout)
    except json.JSONDecodeError as error:
        print(f"Phase runner emitted invalid JSON: {error}", file=sys.stderr)
        return 2
    (outdir / "phase-projection.json").write_text(
        json.dumps(phase_results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    expected = [scenario["name"] for scenario in spec["scenarios"]]
    if [result.get("scenario") for result in phase_results] != expected:
        print("Phase runner returned unexpected scenario names", file=sys.stderr)
        return 1
    for result in phase_results:
        if "error" in result or not result.get("reach", False):
            print(json.dumps(result, sort_keys=True), file=sys.stderr)
            return 1
        print(
            f"PASS {result['scenario']} {json.dumps(result['projection'], separators=(',', ':'))}"
        )
    print(f"Artifacts: {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
