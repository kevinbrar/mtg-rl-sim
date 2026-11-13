#!/usr/bin/env python3
"""Summarize cargo-mutants outcomes and fail on untested build timeouts."""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MutantSummary:
    caught: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    unviable: list[str] = field(default_factory=list)
    test_timeouts: list[str] = field(default_factory=list)
    build_timeouts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    baseline_failed: bool = False

    @property
    def total(self) -> int:
        return sum(
            len(outcomes)
            for outcomes in (
                self.caught,
                self.missed,
                self.unviable,
                self.test_timeouts,
                self.build_timeouts,
            )
        )

    def format(self) -> str:
        return (
            f"Mutants: total={self.total} caught={len(self.caught)} "
            f"missed={len(self.missed)} unviable={len(self.unviable)} "
            f"test-timeout={len(self.test_timeouts)} "
            f"build-timeout={len(self.build_timeouts)}"
        )


def _phase_results(
    outcome: dict[str, Any], label: str, summary: MutantSummary
) -> list[dict[str, Any]]:
    results = outcome.get("phase_results")
    if not isinstance(results, list):
        summary.errors.append(f"{label} has no phase_results list")
        return []

    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("phase"), str):
            summary.errors.append(f"{label} has an invalid phase result")
            return []
        if "process_status" not in result:
            summary.errors.append(f"{label} has a phase result without process_status")
            return []
    return results


def summarize_outcomes(document: object) -> MutantSummary:
    if not isinstance(document, dict):
        raise ValueError("outcomes.json must contain a JSON object")
    outcomes = document.get("outcomes")
    if not isinstance(outcomes, list):
        raise ValueError("outcomes.json must contain an outcomes list")

    summary = MutantSummary()
    baselines = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, dict) and outcome.get("scenario") == "Baseline"
    ]
    if len(baselines) != 1:
        summary.baseline_failed = True
        summary.errors.append(f"expected one baseline outcome, found {len(baselines)}")
    declared_total = document.get("total_mutants")
    observed_total = len(outcomes) - len(baselines)
    if (
        not isinstance(declared_total, int)
        or isinstance(declared_total, bool)
        or declared_total != observed_total
    ):
        summary.errors.append(
            f"outcomes.json declares {declared_total!r} mutant(s) but contains "
            f"{observed_total} non-baseline outcome(s)"
        )

    for index, outcome in enumerate(outcomes, start=1):
        if not isinstance(outcome, dict):
            summary.errors.append(f"outcome {index} must be a JSON object")
            continue

        scenario = outcome.get("scenario")
        if scenario == "Baseline":
            phases = _phase_results(outcome, "baseline", summary)
            phase_names = {phase["phase"] for phase in phases}
            if (
                outcome.get("summary") != "Success"
                or not {"Build", "Test"}.issubset(phase_names)
                or any(phase.get("process_status") != "Success" for phase in phases)
            ):
                summary.baseline_failed = True
            continue

        mutant = scenario.get("Mutant") if isinstance(scenario, dict) else None
        name = mutant.get("name") if isinstance(mutant, dict) else None
        if not isinstance(name, str) or not name:
            summary.errors.append(f"outcome {index} has no mutant name")
            continue

        phases = _phase_results(outcome, name, summary)
        phase_names = {phase["phase"] for phase in phases}
        if "Build" not in phase_names:
            summary.errors.append(f"{name} has no Build phase result")

        build_timed_out = any(
            phase["phase"] == "Build" and phase.get("process_status") == "Timeout"
            for phase in phases
        )
        build_succeeded = any(
            phase["phase"] == "Build" and phase.get("process_status") == "Success"
            for phase in phases
        )
        test_timed_out = any(
            phase["phase"] == "Test" and phase.get("process_status") == "Timeout"
            for phase in phases
        )

        result = outcome.get("summary")
        if build_timed_out:
            summary.build_timeouts.append(name)
            if test_timed_out:
                summary.errors.append(f"{name} timed out in both Build and Test")
            if result != "Timeout":
                summary.errors.append(
                    f"{name} has a Build timeout but summary {result!r}"
                )
        elif test_timed_out:
            summary.test_timeouts.append(name)
            if not build_succeeded:
                summary.errors.append(
                    f"{name} timed out in Test without a successful Build"
                )
            if result != "Timeout":
                summary.errors.append(
                    f"{name} has a Test timeout but summary {result!r}"
                )
        elif result == "CaughtMutant":
            summary.caught.append(name)
        elif result == "MissedMutant":
            summary.missed.append(name)
        elif result == "Unviable":
            summary.unviable.append(name)
        elif result == "Timeout":
            summary.errors.append(
                f"{name} has summary Timeout without a timed-out phase"
            )
        else:
            summary.errors.append(f"{name} has unknown summary {result!r}")

    return summary


def _read_outcomes(path: Path) -> object:
    outcomes_path = path / "outcomes.json"
    try:
        return json.loads(outcomes_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {outcomes_path}: {error}") from error


def _read_missed_lines(path: Path) -> list[str]:
    missed_path = path / "missed.txt"
    try:
        contents = missed_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot read {missed_path}: {error}") from error

    lines = contents.splitlines()
    if any(not line.strip() for line in lines):
        raise ValueError(f"{missed_path} contains an empty line")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path, help="cargo-mutants output directory")
    parser.add_argument("--threshold", type=int, required=True)
    parser.add_argument(
        "--no-mutants-ok",
        action="store_true",
        help="allow absent output files for a confirmed empty mutation set",
    )
    args = parser.parse_args(argv)

    if args.threshold < 0:
        print("::error::threshold must be non-negative", file=sys.stderr)
        return 1

    outcomes_path = args.output_dir / "outcomes.json"
    missed_path = args.output_dir / "missed.txt"
    if args.no_mutants_ok and not outcomes_path.exists() and not missed_path.exists():
        print(MutantSummary().format(), flush=True)
        return 0

    try:
        summary = summarize_outcomes(_read_outcomes(args.output_dir))
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1

    print(summary.format(), flush=True)
    for name in summary.missed:
        print(f"MISSED {name}", flush=True)
    for name in summary.build_timeouts:
        print(f"BUILD-TIMEOUT {name}", flush=True)

    try:
        missed_lines = _read_missed_lines(args.output_dir)
    except ValueError as error:
        summary.errors.append(str(error))
        missed_lines = []

    if len(missed_lines) != len(summary.missed):
        summary.errors.append(
            "missed.txt line count "
            f"{len(missed_lines)} disagrees with outcomes.json count "
            f"{len(summary.missed)}"
        )
    if summary.baseline_failed:
        summary.errors.append("unmutated baseline failed")
    if len(summary.missed) > args.threshold:
        summary.errors.append(
            f"missed mutant count {len(summary.missed)} exceeds threshold "
            f"{args.threshold}"
        )
    if summary.build_timeouts:
        summary.errors.append(
            f"{len(summary.build_timeouts)} mutant build phase(s) timed out"
        )

    for error in summary.errors:
        print(f"::error::{error}", file=sys.stderr)
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
