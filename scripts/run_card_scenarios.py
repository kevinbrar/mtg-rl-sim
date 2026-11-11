#!/usr/bin/env python3
"""Run TOML card scenarios against Phase and XMage."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAGE_RESULT_PREFIX = "CARD_SCENARIO_RESULT:"
PLAYER_SETUP_FIELDS = {"life", "battlefield", "hand", "library_top", "graveyard"}
PERMANENT_SETUP_FIELDS = {"label", "card", "tapped", "counters"}


def _card_names(cards: str) -> list[str]:
    names = [name.strip() for name in cards.split(";")]
    if (
        not names
        or any(not name for name in names)
        or cards.strip().casefold() == "none"
    ):
        raise ValueError("cards must be a semicolon-separated list of card names")
    return names


def _card_slug(name: str) -> str:
    normalized = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a scenario filename for card {name!r}")
    return slug


def scenario_paths_for_cards(cards: str) -> list[Path]:
    names = _card_names(cards)
    paths = []
    for name in names:
        paths.append(ROOT / "oracle/scenarios" / f"{_card_slug(name)}.toml")
    return paths


def check_changed_scenario_suites(cards: str, changed_files: Sequence[str]) -> None:
    listed_slugs = {_card_slug(name) for name in _card_names(cards)}
    for changed_file in changed_files:
        match = re.fullmatch(r"oracle/scenarios/([^/]+)\.toml", changed_file)
        if match is None:
            continue
        slug = match.group(1)
        if slug not in listed_slugs:
            raise ValueError(
                f"changed scenario suite {changed_file} is not listed in cards: {cards}"
            )
        if not (ROOT / changed_file).is_file():
            raise ValueError(f"changed scenario suite {changed_file} is missing")


def parse_mage_report(report: str) -> list[dict]:
    root = ET.fromstring(report)
    results = []
    for output in root.iter("system-out"):
        for line in (output.text or "").splitlines():
            if line.startswith(MAGE_RESULT_PREFIX):
                results.append(json.loads(line[len(MAGE_RESULT_PREFIX) :]))
    return results


def _validate_setup(scenario: dict) -> None:
    name = scenario.get("name", "<unnamed>")
    setup = scenario.get("setup")
    if not isinstance(setup, dict):
        raise TypeError(f"{name}: setup must be a table")
    for player, player_setup in setup.items():
        if player not in {"P0", "P1"}:
            raise ValueError(f"{name}: setup.{player} is unsupported")
        if not isinstance(player_setup, dict):
            raise TypeError(f"{name}: setup.{player} must be a table")
        unknown = player_setup.keys() - PLAYER_SETUP_FIELDS
        if unknown:
            raise ValueError(f"{name}: setup.{player}.{min(unknown)} is unsupported")
        for field in ("hand", "library_top", "graveyard"):
            cards = player_setup.get(field, [])
            if not isinstance(cards, list):
                raise TypeError(f"{name}: setup.{player}.{field} must be an array")
            for index, card in enumerate(cards):
                if isinstance(card, dict):
                    if card.keys() != {"card"}:
                        raise ValueError(
                            f"{name}: setup.{player}.{field}[{index}] only supports card"
                        )
                elif not isinstance(card, str):
                    raise TypeError(
                        f"{name}: setup.{player}.{field}[{index}] must name a card"
                    )
        battlefield = player_setup.get("battlefield", [])
        if not isinstance(battlefield, list):
            raise TypeError(f"{name}: setup.{player}.battlefield must be an array")
        for index, permanent in enumerate(battlefield):
            context = f"{name}: {player}.battlefield[{index}]"
            if not isinstance(permanent, dict):
                raise TypeError(f"{context} must be a table")
            if "damage" in permanent:
                raise ValueError(
                    f"{context}.damage is unsupported by the XMage scenario test API"
                )
            unknown = permanent.keys() - PERMANENT_SETUP_FIELDS
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            if not isinstance(permanent.get("label"), str) or not isinstance(
                permanent.get("card"), str
            ):
                raise TypeError(f"{context} needs label and card")
            if "tapped" in permanent and not isinstance(permanent["tapped"], bool):
                raise ValueError(f"{context}.tapped must be a boolean")
            if "counters" in permanent and not isinstance(permanent["counters"], dict):
                raise TypeError(f"{context}.counters must be a table")


def load_specs(paths: list[Path]) -> dict:
    scenarios = []
    for path in paths:
        with path.open("rb") as source:
            data = tomllib.load(source)
        file_scenarios = data.get("scenarios", [])
        if not isinstance(file_scenarios, list) or not file_scenarios:
            raise ValueError(f"{path}: scenario file contains no scenarios")
        for index, scenario in enumerate(file_scenarios, start=1):
            if (
                not isinstance(scenario, dict)
                or not isinstance(scenario.get("name"), str)
                or not scenario["name"].strip()
            ):
                raise ValueError(f"{path}: scenario {index} is missing a name")
            _validate_setup(scenario)
            scenarios.append(scenario)
    names = [scenario["name"] for scenario in scenarios]
    if len(names) != len(set(names)):
        raise ValueError("scenario names must be unique across spec files")
    return {"scenarios": scenarios}


def _print_differences(
    differences: list[str],
    phase_results: dict[str, dict],
    mage_results: dict[str, dict],
) -> None:
    for name in differences:
        scenario = (
            name.split(" missing from ", 1)[0]
            .split(" missing projection on ", 1)[0]
            .split(" (", 1)[0]
        )
        print(f"DIVERGENCE {name}")
        for engine, results in (("phase-rs", phase_results), ("XMage", mage_results)):
            result = results.get(scenario)
            projection = (
                result.get("projection")
                if result is not None and result.get("projection") is not None
                else result
            )
            print(f"{engine}: {json.dumps(projection, sort_keys=True)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("specs", nargs="*", type=Path)
    parser.add_argument(
        "--cards",
        help="semicolon-separated card names; resolves oracle/scenarios/<card>.toml",
    )
    parser.add_argument(
        "--check-changed-suites",
        type=Path,
        help="check changed scenario suite paths against --cards",
    )
    parser.add_argument(
        "--phase-bin", type=Path, default=ROOT / "target/release/oracle-scenario"
    )
    parser.add_argument(
        "--mage-bench-root",
        type=Path,
        default=Path(os.environ.get("MAGE_BENCH_ROOT", ROOT / "mage-bench")),
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

    if args.check_changed_suites is not None:
        if not args.cards or args.specs:
            print(
                "--check-changed-suites requires --cards and cannot be combined with spec paths",
                file=sys.stderr,
            )
            return 2
        try:
            changed_files = args.check_changed_suites.read_text(
                encoding="utf-8"
            ).splitlines()
            check_changed_scenario_suites(args.cards, changed_files)
        except (OSError, ValueError) as error:
            print(f"invalid changed scenario suites: {error}", file=sys.stderr)
            return 2
        return 0

    if args.cards and args.specs:
        print("spec paths and --cards cannot be used together", file=sys.stderr)
        return 2
    try:
        paths = scenario_paths_for_cards(args.cards) if args.cards else args.specs
    except ValueError as error:
        print(f"invalid card list: {error}", file=sys.stderr)
        return 2
    if not paths:
        print("provide one or more spec paths or --cards", file=sys.stderr)
        return 2
    paths = [path if path.is_absolute() else ROOT / path for path in paths]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        print(f"missing card scenario spec(s): {', '.join(missing)}", file=sys.stderr)
        return 2

    try:
        spec = load_specs(paths)
    except (OSError, TypeError, ValueError, KeyError, tomllib.TOMLDecodeError) as error:
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
    phase_results = []
    try:
        phase_results = json.loads(phase.stdout)
    except json.JSONDecodeError as error:
        print(f"Phase runner emitted invalid JSON: {error}", file=sys.stderr)
    mage_root = args.mage_bench_root
    if not mage_root.is_absolute():
        mage_root = ROOT / mage_root
    if not mage_root.is_dir():
        print(f"missing mage-bench checkout: {mage_root}", file=sys.stderr)
        return 2
    mage = subprocess.run(
        [
            "mvn",
            "-B",
            "-pl",
            "Mage.Tests",
            "-Dsurefire.useFile=false",
            "-Dtest=CardScenarioProjectionTest",
            f"-DoracleScenarioSpec={spec_path}",
            "test",
        ],
        cwd=mage_root,
        text=True,
        capture_output=True,
        check=False,
    )
    (outdir / "mage.log").write_text(mage.stdout + mage.stderr, encoding="utf-8")
    report_path = (
        mage_root
        / "Mage.Tests/target/surefire-reports/"
        / "TEST-org.mage.test.testapi.CardScenarioProjectionTest.xml"
    )
    try:
        report = report_path.read_text(encoding="utf-8")
        (outdir / "mage-surefire-report.xml").write_text(report, encoding="utf-8")
        mage_results = parse_mage_report(report)
    except (OSError, ET.ParseError, json.JSONDecodeError) as error:
        print(f"Could not read XMage scenario results: {error}", file=sys.stderr)
        mage_results = []
    (outdir / "phase-projection.json").write_text(
        json.dumps(phase_results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (outdir / "mage-projection.json").write_text(
        json.dumps(mage_results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    expected = [scenario["name"] for scenario in spec["scenarios"]]
    phase_names = [result.get("scenario") for result in phase_results]
    mage_names = [result.get("scenario") for result in mage_results]
    if phase_names != expected:
        print("Phase runner returned unexpected scenario names", file=sys.stderr)
    if mage_names != expected:
        print("XMage runner returned unexpected scenario names", file=sys.stderr)
    comparison = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/compare_card_scenarios.py"),
            str(outdir / "phase-projection.json"),
            str(outdir / "mage-projection.json"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    comparison_log = comparison.stdout + comparison.stderr
    (outdir / "compare.log").write_text(comparison_log, encoding="utf-8")
    print(comparison_log, end="")
    if comparison.returncode:
        try:
            phase_values = json.loads(
                (outdir / "phase-projection.json").read_text(encoding="utf-8")
            )
            mage_values = json.loads(
                (outdir / "mage-projection.json").read_text(encoding="utf-8")
            )
            phase_map = {value["scenario"]: value for value in phase_values}
            mage_map = {value["scenario"]: value for value in mage_values}
            line = comparison.stdout.strip()
            names = line.removeprefix("DIFF: ").split(", ")
            _print_differences(names, phase_map, mage_map)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            print(f"Could not load projections for divergence report: {error}")
    for engine, results in (("PHASE", phase_results), ("XMAGE", mage_results)):
        for result in results:
            if result.get("projection") is not None:
                print(
                    f"{engine} {result['scenario']} "
                    f"{json.dumps(result['projection'], separators=(',', ':'))}"
                )
    if phase.returncode:
        print(
            f"Phase runner exited {phase.returncode}; see {outdir / 'phase.log'}",
            file=sys.stderr,
        )
    if mage.returncode:
        print(
            f"XMage runner exited {mage.returncode}; see {outdir / 'mage.log'}",
            file=sys.stderr,
        )
    print(f"Artifacts: {outdir}")
    return int(
        phase.returncode != 0
        or mage.returncode != 0
        or comparison.returncode != 0
        or phase_names != expected
        or mage_names != expected
    )


if __name__ == "__main__":
    raise SystemExit(main())
