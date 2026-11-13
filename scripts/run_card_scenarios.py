#!/usr/bin/env python3
"""Run TOML card scenarios against Phase and XMage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .compare_card_scenarios import compare, normalize_projection
else:
    from compare_card_scenarios import compare, normalize_projection

ROOT = Path(__file__).resolve().parents[1]
MAGE_RESULT_PREFIX = "CARD_SCENARIO_RESULT:"
PLAYER_SETUP_FIELDS = {"life", "battlefield", "hand", "library_top", "graveyard"}
PERMANENT_SETUP_FIELDS = {"label", "card", "tapped", "counters"}
REACH_FIELDS = {
    "all",
    "player",
    "zone",
    "card",
    "count",
    "tapped_labels",
    "types",
    "subtypes",
    "colors",
    "power",
    "toughness",
    "power_is_null",
    "toughness_is_null",
    "creature",
    "tapped",
    "attached_to",
    "counters",
}
KNOWN_DIFF_FIELDS = {
    "card",
    "category",
    "rationale",
    "link",
    "regression_test",
    "suite",
    "scenario",
    "phase_sha256",
    "mage_sha256",
}
KNOWN_DIFF_CATEGORIES = {"engine-bug", "xmage-bug", "unsupported"}


def _validate_reach_fields(name: str, reach: object, path: str = "reach") -> None:
    if not isinstance(reach, dict):
        raise TypeError(f"{name}: {path} must be a table")
    unknown = reach.keys() - REACH_FIELDS
    if unknown:
        raise ValueError(f"{name}: {path}.{min(unknown)} is unsupported")
    if "all" in reach:
        if len(reach) != 1:
            raise ValueError(
                f"{name}: reach.all cannot be combined with other reach fields"
            )
        checks = reach["all"]
        if not isinstance(checks, list):
            raise TypeError(f"{name}: reach.all must be an array of reach assertions")
        if not checks:
            raise ValueError(f"{name}: reach.all must contain at least one assertion")
        for index, check in enumerate(checks):
            _validate_reach_fields(name, check, f"{path}.all[{index}]")


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
        .replace("'", "")
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


def load_known_diffs(path: Path) -> list[dict]:
    with path.open("rb") as source:
        data = tomllib.load(source)
    unknown_top_level = data.keys() - {"diff"}
    if unknown_top_level:
        raise ValueError(
            f"unknown top-level known-diffs field {min(unknown_top_level)}"
        )
    entries = data.get("diff", [])
    if not isinstance(entries, list):
        raise ValueError("known-diffs diff must be an array")
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"known-diffs diff {index} must be a table")
        missing = KNOWN_DIFF_FIELDS - entry.keys()
        unknown = entry.keys() - KNOWN_DIFF_FIELDS
        if missing:
            raise ValueError(
                f"known-diffs diff {index} missing fields: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise ValueError(
                f"known-diffs diff {index} has unknown fields: {', '.join(sorted(unknown))}"
            )
        for field in KNOWN_DIFF_FIELDS:
            if not isinstance(entry[field], str) or not entry[field].strip():
                raise ValueError(
                    f"known-diffs diff {index} field {field} must be a non-empty string"
                )
        if entry["category"] not in KNOWN_DIFF_CATEGORIES:
            raise ValueError(
                f"known-diffs diff {index} has bad category {entry['category']!r}"
            )
        for field in ("phase_sha256", "mage_sha256"):
            if re.fullmatch(r"[0-9a-fA-F]{64}", entry[field]) is None:
                raise ValueError(
                    f"known-diffs diff {index} field {field} must be a 64-digit hex SHA-256"
                )
    return entries


def load_base_known_diffs(path: Path) -> list[dict]:
    with path.open("rb") as source:
        data = tomllib.load(source)
    unknown_top_level = data.keys() - {"diff"}
    if unknown_top_level:
        raise ValueError(
            f"unknown top-level base known-diffs field {min(unknown_top_level)}"
        )
    entries = data.get("diff", [])
    if not isinstance(entries, list):
        raise ValueError("base known-diffs diff must be an array")
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"base known-diffs diff {index} must be a table")
        if not isinstance(entry.get("card"), str) or not entry["card"].strip():
            raise ValueError(
                f"base known-diffs diff {index} requires a non-empty card string"
            )
    return entries


def _freeze_toml_value(value: object) -> object:
    if isinstance(value, dict):
        return tuple(
            (key, _freeze_toml_value(item)) for key, item in sorted(value.items())
        )
    if isinstance(value, list):
        return tuple(_freeze_toml_value(item) for item in value)
    return value


def _changed_known_diff_entries(
    base_entries: list[dict], head_entries: list[dict]
) -> list[dict]:
    unmatched_head = Counter(_freeze_toml_value(entry) for entry in head_entries)
    removed = []
    for entry in base_entries:
        key = _freeze_toml_value(entry)
        if unmatched_head[key]:
            unmatched_head[key] -= 1
        else:
            removed.append(entry)

    unmatched_base = Counter(_freeze_toml_value(entry) for entry in base_entries)
    added = []
    for entry in head_entries:
        key = _freeze_toml_value(entry)
        if unmatched_base[key]:
            unmatched_base[key] -= 1
        else:
            added.append(entry)
    return [*removed, *added]


def check_changed_known_diff_entries(
    cards: str,
    changed_files: Sequence[str],
    base_path: Path | None,
    head_entries: list[dict],
) -> None:
    registry_changed = "oracle/known-diffs.toml" in changed_files
    if registry_changed and base_path is None:
        raise ValueError(
            "oracle/known-diffs.toml changed; --base-known-diffs is required"
        )
    if base_path is None:
        return

    base_entries = load_base_known_diffs(base_path)
    changed_entries = _changed_known_diff_entries(base_entries, head_entries)
    listed_cards = set(_card_names(cards))
    missing_cards = sorted({entry["card"] for entry in changed_entries} - listed_cards)
    if missing_cards:
        raise ValueError(
            "changed known-diff entries require cards: " + "; ".join(missing_cards)
        )


def scenario_names_by_suite(paths: list[Path]) -> dict[str, list[str]]:
    suites = {}
    for path in paths:
        suite = path.stem
        if suite in suites:
            raise ValueError(f"duplicate scenario suite stem {suite}")
        with path.open("rb") as source:
            data = tomllib.load(source)
        suites[suite] = [scenario["name"] for scenario in data["scenarios"]]
    return suites


def projection_sha256(result: dict | None) -> str | None:
    if result is None or result.get("projection") is None:
        return None
    projection = normalize_projection(result["projection"])
    canonical = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _results_by_name(
    results: list[dict], engine: str
) -> tuple[dict[str, dict], list[str]]:
    mapped = {}
    errors = []
    for index, result in enumerate(results, start=1):
        if not isinstance(result, dict) or not isinstance(result.get("scenario"), str):
            errors.append(f"{engine} result {index} has no scenario name")
            continue
        name = result["scenario"]
        if name in mapped:
            errors.append(f"{engine} returned duplicate scenario {name}")
        mapped[name] = result
    return mapped, errors


def evaluate_known_diffs(
    suite_scenarios: dict[str, list[str]],
    phase_results: list[dict],
    mage_results: list[dict],
    entries: list[dict],
    runner_errors: list[str] | None = None,
) -> dict:
    expected_names = [
        name for scenario_names in suite_scenarios.values() for name in scenario_names
    ]
    phase, phase_errors = _results_by_name(phase_results, "Phase")
    mage, mage_errors = _results_by_name(mage_results, "XMage")
    failed = [
        {"suite": "<runner>", "scenario": "<runner>", "reason": error}
        for error in [*(runner_errors or []), *phase_errors, *mage_errors]
    ]
    if list(phase) != expected_names:
        failed.append(
            {
                "suite": "<runner>",
                "scenario": "<Phase>",
                "reason": "Phase scenario names or order differ from the requested suites",
            }
        )
    if list(mage) != expected_names:
        failed.append(
            {
                "suite": "<runner>",
                "scenario": "<XMage>",
                "reason": "XMage scenario names or order differ from the requested suites",
            }
        )

    run_suites = set(suite_scenarios)
    entries_by_key: dict[tuple[str, str], list[dict]] = {}
    for entry in entries:
        suite = entry["suite"]
        if suite not in run_suites:
            continue
        key = (suite, entry["scenario"])
        entries_by_key.setdefault(key, []).append(entry)
        if entry["scenario"] not in suite_scenarios[suite]:
            failed.append(
                {
                    "suite": suite,
                    "scenario": entry["scenario"],
                    "reason": "known-diff entry names a scenario not present in the run suite",
                    "card": entry["card"],
                }
            )

    duplicate_keys = set()
    for (suite, scenario), matching in entries_by_key.items():
        if len(matching) > 1:
            duplicate_keys.add((suite, scenario))
            failed.append(
                {
                    "suite": suite,
                    "scenario": scenario,
                    "reason": f"duplicate known-diff entries ({len(matching)})",
                }
            )

    matched = []
    triaged = []
    for suite, names in suite_scenarios.items():
        for name in names:
            key = (suite, name)
            matching = entries_by_key.get(key, [])
            if key in duplicate_keys:
                continue
            phase_result = phase.get(name)
            mage_result = mage.get(name)
            differences = compare(
                {name: phase_result} if phase_result is not None else {},
                {name: mage_result} if mage_result is not None else {},
            )
            phase_digest = projection_sha256(phase_result)
            mage_digest = projection_sha256(mage_result)
            if not differences:
                if matching:
                    failed.append(
                        {
                            "suite": suite,
                            "scenario": name,
                            "reason": "stale known-diff entry; remove it",
                            "card": matching[0]["card"],
                            "phase_sha256": phase_digest,
                            "mage_sha256": mage_digest,
                        }
                    )
                else:
                    matched.append({"suite": suite, "scenario": name})
                continue

            details = {
                "suite": suite,
                "scenario": name,
                "differences": differences,
                "phase_sha256": phase_digest,
                "mage_sha256": mage_digest,
            }
            if not matching:
                failed.append({**details, "reason": "untriaged divergence"})
                continue
            entry = matching[0]
            if entry["category"] == "unsupported":
                failed.append(
                    {
                        **details,
                        "card": entry["card"],
                        "reason": "unsupported entries never waive divergences",
                    }
                )
            elif phase_digest is None or mage_digest is None:
                failed.append(
                    {
                        **details,
                        "card": entry["card"],
                        "reason": "divergence has a missing projection and cannot be triaged",
                    }
                )
            elif (
                entry["phase_sha256"].lower() != phase_digest
                or entry["mage_sha256"].lower() != mage_digest
            ):
                failed.append(
                    {
                        **details,
                        "card": entry["card"],
                        "reason": (
                            "digest mismatch; "
                            f"expected phase_sha256={entry['phase_sha256'].lower()} "
                            f"mage_sha256={entry['mage_sha256'].lower()}; "
                            f"observed phase_sha256={phase_digest}; "
                            f"observed mage_sha256={mage_digest}"
                        ),
                    }
                )
            else:
                triaged.append(
                    {
                        "suite": suite,
                        "scenario": name,
                        "category": entry["category"],
                        "card": entry["card"],
                        "phase_sha256": phase_digest,
                        "mage_sha256": mage_digest,
                    }
                )

    return {
        "counts": {
            "matched": len(matched),
            "triaged": len(triaged),
            "failed": len(failed),
        },
        "matched": matched,
        "triaged": triaged,
        "failed": failed,
    }


def format_known_diff_report(report: dict) -> str:
    counts = report["counts"]
    lines = [
        f"MATCHED: {counts['matched']}",
        f"TRIAGED: {counts['triaged']}",
    ]
    lines.extend(
        f"  [{item['category']}] {item['card']}: {item['suite']} / {item['scenario']} "
        f"(phase_sha256={item['phase_sha256']}, mage_sha256={item['mage_sha256']})"
        for item in report["triaged"]
    )
    lines.append(f"FAILED: {counts['failed']}")
    lines.extend(
        f"  {item.get('suite', '<unknown>')} / {item.get('scenario', '<unknown>')}: "
        f"{item['reason']}"
        for item in report["failed"]
    )
    return "\n".join(lines) + "\n"


def _validate_setup(scenario: dict) -> None:
    name = scenario.get("name", "<unnamed>")
    if "reach" in scenario:
        _validate_reach_fields(name, scenario["reach"])
    setup = scenario.get("setup")
    if not isinstance(setup, dict):
        raise TypeError(f"{name}: setup must be a table")
    labels: set[str] = set()
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
                    if card.keys() - {"card", "label"} or not isinstance(
                        card.get("card"), str
                    ):
                        raise ValueError(
                            f"{name}: setup.{player}.{field}[{index}] needs card and optional label"
                        )
                    if not card["card"]:
                        raise ValueError(
                            f"{name}: setup.{player}.{field}[{index}] needs a card"
                        )
                    if "label" in card:
                        label = card["label"]
                        if not isinstance(label, str) or not label:
                            raise ValueError(
                                f"{name}: setup.{player}.{field}[{index}].label must be a non-empty string"
                            )
                        if label.startswith("@"):
                            raise ValueError(
                                f"{name}: setup label {label} uses reserved @ prefix"
                            )
                        if label in labels:
                            raise ValueError(f"{name}: duplicate setup label {label}")
                        labels.add(label)
                elif not isinstance(card, str):
                    raise TypeError(
                        f"{name}: setup.{player}.{field}[{index}] must name a card"
                    )
                elif not card:
                    raise ValueError(
                        f"{name}: setup.{player}.{field}[{index}] needs a card"
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
            label = permanent["label"]
            if label.startswith("@"):
                raise ValueError(f"{name}: setup label {label} uses reserved @ prefix")
            if not label or label in labels:
                raise ValueError(f"{name}: duplicate or empty setup label {label}")
            labels.add(label)
            if "tapped" in permanent and not isinstance(permanent["tapped"], bool):
                raise ValueError(f"{context}.tapped must be a boolean")
            if "counters" in permanent and not isinstance(permanent["counters"], dict):
                raise TypeError(f"{context}.counters must be a table")

    script = scenario.get("script", [])
    if not isinstance(script, list):
        raise TypeError(f"{name}: script must be an array")
    for index, action in enumerate(script):
        if not isinstance(action, dict):
            raise TypeError(f"{name}: script[{index}] must be a table")
        if "behold" in action and action.get("action") != "play_land":
            raise ValueError(
                f"{name}: script[{index}].behold is only valid for play_land actions"
            )
        if action.get("action") == "play_land":
            context = f"{name}: script[{index}]"
            if not isinstance(action.get("card"), str) or not action["card"]:
                raise ValueError(f"{context}.card needs a hand label")
            if "behold" in action:
                behold = action["behold"]
                if not isinstance(behold, str) or not behold:
                    raise ValueError(f"{context}.behold must be a label or decline")
        if "alternative_cost" not in action:
            continue
        context = f"{name}: script[{index}].alternative_cost"
        if action.get("action") != "activate":
            raise ValueError(f"{context} is only valid for activate actions")
        decision = action["alternative_cost"]
        if not isinstance(decision, str) or decision not in {"accept", "decline"}:
            raise ValueError(f"{context} must be accept or decline")


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
    parser.add_argument("--known-diffs", type=Path, required=True)
    parser.add_argument("--base-known-diffs", type=Path)
    args = parser.parse_args()

    if args.base_known_diffs is not None and args.check_changed_suites is None:
        print("--base-known-diffs requires --check-changed-suites", file=sys.stderr)
        return 2

    known_diffs_path = (
        args.known_diffs if args.known_diffs.is_absolute() else ROOT / args.known_diffs
    )
    try:
        known_diffs = load_known_diffs(known_diffs_path)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"invalid known-diffs registry: {error}", file=sys.stderr)
        return 2

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
            base_known_diffs_path = args.base_known_diffs
            if (
                base_known_diffs_path is not None
                and not base_known_diffs_path.is_absolute()
            ):
                base_known_diffs_path = ROOT / base_known_diffs_path
            check_changed_known_diff_entries(
                args.cards,
                changed_files,
                base_known_diffs_path,
                known_diffs,
            )
        except (OSError, ValueError) as error:
            print(f"invalid changed scenario scope: {error}", file=sys.stderr)
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
        suite_scenarios = scenario_names_by_suite(paths)
    except (OSError, TypeError, ValueError, KeyError, tomllib.TOMLDecodeError) as error:
        print(f"invalid scenario spec: {error}", file=sys.stderr)
        return 2
    if not spec["scenarios"]:
        print("no scenarios were run", file=sys.stderr)
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
    runner_errors = []
    try:
        phase_results = json.loads(phase.stdout)
        if not isinstance(phase_results, list):
            raise ValueError("expected a list of scenario projections")
    except (json.JSONDecodeError, ValueError) as error:
        print(f"Phase runner emitted invalid JSON: {error}", file=sys.stderr)
        runner_errors.append(f"Phase runner emitted invalid JSON: {error}")
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
    if phase.returncode:
        runner_errors.append(f"Phase runner exited {phase.returncode}")
    if mage.returncode:
        runner_errors.append(f"XMage runner exited {mage.returncode}")
    report = evaluate_known_diffs(
        suite_scenarios, phase_results, mage_results, known_diffs, runner_errors
    )
    report_path = outdir / "known-diffs-report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    comparison_log = format_known_diff_report(report)
    (outdir / "compare.log").write_text(comparison_log, encoding="utf-8")
    print(comparison_log, end="")
    phase_map, _ = _results_by_name(phase_results, "Phase")
    mage_map, _ = _results_by_name(mage_results, "XMage")
    for item in [*report["triaged"], *report["failed"]]:
        scenario = item.get("scenario")
        if not scenario or scenario not in phase_map or scenario not in mage_map:
            continue
        differences = compare(
            {scenario: phase_map[scenario]}, {scenario: mage_map[scenario]}
        )
        if differences:
            print(
                f"DIVERGENCE {item.get('suite')} / {scenario}: {', '.join(differences)}"
            )
            print(f"phase-rs: {json.dumps(phase_map[scenario], sort_keys=True)}")
            print(f"XMage: {json.dumps(mage_map[scenario], sort_keys=True)}")
    for engine, results in (("PHASE", phase_results), ("XMAGE", mage_results)):
        for result in results:
            if result.get("projection") is not None:
                print(
                    f"{engine} {result['scenario']} "
                    f"{json.dumps(result['projection'], separators=(',', ':'))}"
                )
    print(f"Artifacts: {outdir} (including {report_path.name})")
    return int(report["counts"]["failed"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
