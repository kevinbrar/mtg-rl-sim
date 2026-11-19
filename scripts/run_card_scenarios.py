#!/usr/bin/env python3
"""Run TOML card scenarios against Phase and XMage."""

from __future__ import annotations

import argparse
import copy
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
FORGE_PROMPT_FAILURE_PREFIX = "CARD_SCENARIO_PROMPT_FAILURE:"
PLAYER_SETUP_FIELDS = {
    "life",
    "battlefield",
    "hand",
    "library_top",
    "graveyard",
    "sideboard",
    "mana_pool",
    "exile",
}
PERMANENT_SETUP_FIELDS = {"label", "card", "tapped", "counters"}
REACH_FIELDS = {
    "all",
    "player",
    "life",
    "zone",
    "card",
    "label",
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
    "solved",
    "token",
}
KNOWN_DIFF_FIELDS = {
    "card",
    "category",
    "rationale",
    "link",
    "regression_test",
    "suite",
    "scenario",
}
KNOWN_DIFF_DIGEST_FIELDS = {
    "phase_sha256",
    "mage_sha256",
    "forge_sha256",
}
KNOWN_DIFF_CATEGORIES = {"engine-bug", "xmage-bug", "forge-bug", "unsupported"}


def _validate_reach_fields(
    name: str, reach: object, path: str = "reach", oracle: str = "xmage"
) -> None:
    if not isinstance(reach, dict):
        raise TypeError(f"{name}: {path} must be a table")
    unknown = reach.keys() - REACH_FIELDS
    if unknown:
        raise ValueError(f"{name}: {path}.{min(unknown)} is unsupported")
    if "token" in reach and not isinstance(reach["token"], bool):
        raise TypeError(f"{name}: {path}.token must be a boolean")
    if "token" in reach and reach.get("zone", "graveyard") != "battlefield":
        raise ValueError(f"{name}: {path}.token is only valid for battlefield cards")
    if "label" in reach:
        if oracle == "xmage":
            raise ValueError(
                f"{name}: {path}.label is unsupported for oracle = 'xmage'; "
                "label-based reach assertions require oracle = 'forge'"
            )
        if not isinstance(reach["label"], str) or not reach["label"]:
            raise TypeError(f"{name}: {path}.label must be a battlefield label")
        if reach.get("zone", "graveyard") != "battlefield":
            raise ValueError(
                f"{name}: {path}.label is only valid for battlefield permanents"
            )
    if "solved" in reach:
        if not isinstance(reach["solved"], bool):
            raise TypeError(f"{name}: {path}.solved must be a boolean")
        if reach.get("zone", "graveyard") != "battlefield":
            raise ValueError(
                f"{name}: {path}.solved is only valid for battlefield Cases"
            )
    if "life" in reach:
        if isinstance(reach["life"], bool) or not isinstance(reach["life"], int):
            raise TypeError(f"{name}: {path}.life must be an integer")
        if reach.keys() - {"player", "life"}:
            raise ValueError(f"{name}: {path}.life can only be combined with player")
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
            _validate_reach_fields(name, check, f"{path}.all[{index}]", oracle)


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


def parse_forge_report(report: str) -> list[dict]:
    return parse_mage_report(report)


def parse_forge_prompt_failures(report: str) -> list[dict]:
    root = ET.fromstring(report)
    failures = []
    for output in root.iter("system-out"):
        for line in (output.text or "").splitlines():
            if line.startswith(FORGE_PROMPT_FAILURE_PREFIX):
                event = json.loads(line[len(FORGE_PROMPT_FAILURE_PREFIX) :])
                if (
                    not isinstance(event, dict)
                    or set(event) != {"scenario", "prompt"}
                    or event["prompt"] != "outside_game"
                    or not isinstance(event["scenario"], str)
                ):
                    raise ValueError(f"invalid Forge prompt failure event: {event!r}")
                failures.append(event)
    return failures


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
        unknown = entry.keys() - KNOWN_DIFF_FIELDS - KNOWN_DIFF_DIGEST_FIELDS
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
        required_digests = {"phase_sha256"}
        required_digests.add(
            "forge_sha256" if entry["category"] == "forge-bug" else "mage_sha256"
        )
        missing_digests = required_digests - entry.keys()
        if missing_digests:
            raise ValueError(
                f"known-diffs diff {index} missing digest fields: "
                f"{', '.join(sorted(missing_digests))}"
            )
        unexpected_digests = (
            KNOWN_DIFF_DIGEST_FIELDS & entry.keys()
        ) - required_digests
        if unexpected_digests:
            raise ValueError(
                f"known-diffs diff {index} has unexpected digest fields: "
                f"{', '.join(sorted(unexpected_digests))}"
            )
        for field in KNOWN_DIFF_DIGEST_FIELDS & entry.keys():
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


def forge_prompt_sha256(event: dict) -> str:
    canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_label_insensitive_count_groups(
    scenarios: list[dict], results: list[dict]
) -> list[dict]:
    scenario_by_name = {scenario["name"]: scenario for scenario in scenarios}
    normalized = copy.deepcopy(results)

    def strings(value: object):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)

    def count_groups(value: object, groups: dict[tuple[str, str], set[bool]]) -> None:
        if isinstance(value, dict):
            player = value.get("player")
            card = value.get("card")
            tapped = value.get("tapped")
            if (
                isinstance(player, str)
                and isinstance(card, str)
                and isinstance(tapped, bool)
                and isinstance(value.get("count"), int)
            ):
                groups.setdefault((player, card), set()).add(tapped)
            for item in value.values():
                count_groups(item, groups)
        elif isinstance(value, list):
            for item in value:
                count_groups(item, groups)

    for result in normalized:
        scenario = scenario_by_name.get(result.get("scenario"))
        projection = result.get("projection")
        if scenario is None or not isinstance(projection, dict):
            continue
        labels = {}
        for player, setup in scenario.get("setup", {}).items():
            for item in setup.get("battlefield", []):
                if not isinstance(item.get("label"), str):
                    continue
                characteristics = {
                    field: value for field, value in item.items() if field != "label"
                }
                signature = json.dumps(
                    characteristics, sort_keys=True, separators=(",", ":")
                )
                labels[item["label"]] = (player, item["card"], signature)
        if not labels:
            continue
        referenced = set(strings(scenario.get("script", [])))
        referenced.update(strings(scenario.get("reach", {})))
        groups: dict[tuple[str, str], set[bool]] = {}
        count_groups(scenario.get("reach", {}), groups)
        setup_group_sizes: dict[tuple[str, str, str], int] = {}
        for group in labels.values():
            setup_group_sizes[group] = setup_group_sizes.get(group, 0) + 1
        fungible = {
            label
            for label, (player, card, signature) in labels.items()
            if label not in referenced
            and setup_group_sizes[(player, card, signature)] > 1
            and len(groups.get((player, card), set())) > 1
        }
        battlefield = projection.get("battlefield")
        if not fungible or not isinstance(battlefield, list):
            continue
        group_tokens: dict[tuple[str, str, str], str] = {}
        token_groups: dict[str, tuple[str, str, str]] = {}
        for permanent in battlefield:
            label = permanent.get("label")
            if label not in fungible:
                continue
            group = labels[label]
            token = group_tokens.get(group)
            if token is None:
                encoded_group = json.dumps(group, separators=(",", ":"))
                token = (
                    "~group:"
                    + hashlib.sha256(encoded_group.encode("utf-8")).hexdigest()
                )
                if token in labels:
                    raise ValueError(
                        f"{result['scenario']}: count-group token collides with setup label {token!r}"
                    )
                previous_group = token_groups.get(token)
                if previous_group is not None and previous_group != group:
                    raise ValueError(
                        f"{result['scenario']}: count-group token collision for {token!r}"
                    )
                group_tokens[group] = token
                token_groups[token] = group
            permanent["label"] = token
        battlefield.sort(key=lambda permanent: json.dumps(permanent, sort_keys=True))
    return normalized


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


def validate_refusal_indices(
    scenarios: list[dict], results: list[dict], engine: str
) -> list[str]:
    result_by_name, errors = _results_by_name(results, engine)
    for scenario in scenarios:
        name = scenario["name"]
        result = result_by_name.get(name)
        if result is None:
            continue
        expected = [
            index
            for index, action in enumerate(scenario.get("script", []))
            if action.get("expect") == "refused"
        ]
        actual = result.get("refusals")
        if not isinstance(actual, list) or any(
            type(index) is not int for index in actual
        ):
            errors.append(f"{engine} scenario {name} has missing or invalid refusals")
        elif actual != expected:
            errors.append(
                f"{engine} scenario {name} refusal indices mismatch: "
                f"expected {expected}, got {actual}"
            )
        cost_prompts = result.get("refusal_cost_prompts")
        if not isinstance(cost_prompts, list) or any(
            not isinstance(prompt, dict)
            or type(prompt.get("script_index")) is not int
            or prompt.get("script_index") not in expected
            or not isinstance(prompt.get("prompt"), str)
            or not prompt["prompt"].strip()
            for prompt in cost_prompts
        ):
            errors.append(
                f"{engine} scenario {name} has missing or invalid refusal_cost_prompts"
            )
    return errors


def _results_by_suite_scenario(
    results: list[dict], engine: str, suite_by_scenario: dict[str, str]
) -> tuple[dict[tuple[str, str], dict], list[str]]:
    mapped = {}
    errors = []
    for index, result in enumerate(results, start=1):
        if not isinstance(result, dict) or not isinstance(result.get("scenario"), str):
            errors.append(f"{engine} result {index} has no scenario name")
            continue
        name = result["scenario"]
        suite = suite_by_scenario.get(name)
        if suite is None:
            errors.append(f"{engine} returned unexpected scenario {name}")
            continue
        key = (suite, name)
        if key in mapped:
            errors.append(
                f"{engine} returned duplicate scenario {name} in suite {suite}"
            )
        mapped[key] = result
    return mapped, errors


def evaluate_known_diffs(
    suite_scenarios: dict[str, list[str]],
    phase_results: list[dict],
    oracle_results: list[dict],
    entries: list[dict],
    runner_errors: list[str] | None = None,
    forge_prompt_failures: list[dict] | None = None,
    suite_oracles: dict[str, str] | None = None,
) -> dict:
    expected_keys = [
        (suite, name)
        for suite, scenario_names in suite_scenarios.items()
        for name in scenario_names
    ]
    suite_by_scenario = {
        name: suite for suite, names in suite_scenarios.items() for name in names
    }
    phase, phase_errors = _results_by_suite_scenario(
        phase_results, "Phase", suite_by_scenario
    )
    oracle, oracle_errors = _results_by_suite_scenario(
        oracle_results, "oracle", suite_by_scenario
    )
    prompt_failures = {
        event["scenario"]: event for event in (forge_prompt_failures or [])
    }
    failed = [
        {"suite": "<runner>", "scenario": "<runner>", "reason": error}
        for error in [*(runner_errors or []), *phase_errors, *oracle_errors]
    ]
    if len(prompt_failures) != len(forge_prompt_failures or []):
        failed.append(
            {
                "suite": "<runner>",
                "scenario": "<Forge>",
                "reason": "duplicate Forge prompt failure events",
            }
        )
    for name in prompt_failures:
        suite = suite_by_scenario.get(name)
        if suite is None or (suite_oracles or {}).get(suite) != "forge":
            failed.append(
                {
                    "suite": suite or "<unknown>",
                    "scenario": name,
                    "reason": "Forge prompt failure event is outside a Forge-backed suite",
                }
            )
    if set(phase) != set(expected_keys):
        failed.append(
            {
                "suite": "<runner>",
                "scenario": "<Phase>",
                "reason": "Phase (suite, scenario) results are missing or unexpected",
            }
        )
    expected_oracle_keys = [
        key for key in expected_keys if key[1] not in prompt_failures
    ]
    if set(oracle) != set(expected_oracle_keys):
        failed.append(
            {
                "suite": "<runner>",
                "scenario": "<oracle>",
                "reason": "oracle (suite, scenario) results are missing or unexpected",
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
            phase_result = phase.get(key)
            oracle_result = oracle.get(key)
            differences = compare(
                {name: phase_result} if phase_result is not None else {},
                {name: oracle_result} if oracle_result is not None else {},
            )
            phase_digest = projection_sha256(phase_result)
            oracle_digest = projection_sha256(oracle_result)
            if not differences:
                if matching:
                    failed.append(
                        {
                            "suite": suite,
                            "scenario": name,
                            "reason": "stale known-diff entry; remove it",
                            "card": matching[0]["card"],
                            "phase_sha256": phase_digest,
                            "mage_sha256": oracle_digest,
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
                "mage_sha256": oracle_digest,
            }
            if not matching:
                failed.append({**details, "reason": "untriaged divergence"})
                continue
            entry = matching[0]
            if entry["category"] == "forge-bug":
                event = prompt_failures.get(name)
                event_digest = forge_prompt_sha256(event) if event is not None else None
                if (suite_oracles or {}).get(suite) != "forge":
                    failed.append(
                        {
                            **details,
                            "card": entry["card"],
                            "reason": "forge-bug entries require a Forge-backed suite",
                        }
                    )
                elif event is None or oracle_result is not None or phase_digest is None:
                    failed.append(
                        {
                            **details,
                            "card": entry["card"],
                            "reason": "Forge prompt failure is missing or unexpected",
                        }
                    )
                elif (
                    entry["phase_sha256"].lower() != phase_digest
                    or entry["forge_sha256"].lower() != event_digest
                ):
                    failed.append(
                        {
                            **details,
                            "card": entry["card"],
                            "forge_sha256": event_digest,
                            "reason": (
                                "digest mismatch; "
                                f"expected phase_sha256={entry['phase_sha256'].lower()} "
                                f"forge_sha256={entry['forge_sha256'].lower()}; "
                                f"observed phase_sha256={phase_digest}; "
                                f"observed forge_sha256={event_digest}"
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
                            "forge_sha256": event_digest,
                        }
                    )
                continue
            if entry["category"] == "unsupported":
                failed.append(
                    {
                        **details,
                        "card": entry["card"],
                        "reason": "unsupported entries never waive divergences",
                    }
                )
            elif phase_digest is None or oracle_digest is None:
                failed.append(
                    {
                        **details,
                        "card": entry["card"],
                        "reason": "divergence has a missing projection and cannot be triaged",
                    }
                )
            elif (
                entry["phase_sha256"].lower() != phase_digest
                or entry["mage_sha256"].lower() != oracle_digest
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
                            f"observed mage_sha256={oracle_digest}"
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
                        "mage_sha256": oracle_digest,
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
    for item in report["triaged"]:
        oracle = report.get("oracles_by_suite", {}).get(item["suite"], "xmage")
        oracle_hash = "forge_sha256" if oracle == "forge" else "mage_sha256"
        oracle_digest = item.get("forge_sha256", item.get("mage_sha256"))
        lines.append(
            f"  [{item['category']}] {item['card']}: {item['suite']} / "
            f"{item['scenario']} (phase_sha256={item['phase_sha256']}, "
            f"{oracle_hash}={oracle_digest})"
        )
    lines.append(f"FAILED: {counts['failed']}")
    lines.extend(
        f"  {item.get('suite', '<unknown>')} / {item.get('scenario', '<unknown>')}: "
        f"{item['reason']}"
        for item in report["failed"]
    )
    if report.get("oracles_by_suite"):
        lines.append(
            f"ORACLES {json.dumps(report['oracles_by_suite'], sort_keys=True)}"
        )
    return "\n".join(lines) + "\n"


def _validate_setup(scenario: dict, oracle: str = "xmage") -> None:
    name = scenario.get("name", "<unnamed>")
    for field in ("active_player", "priority_player"):
        if field in scenario and (
            type(scenario[field]) is not int or scenario[field] not in {0, 1}
        ):
            raise ValueError(f"{name}: {field} must be 0 or 1")
    if "reach" in scenario:
        _validate_reach_fields(name, scenario["reach"], oracle=oracle)
    setup = scenario.get("setup")
    if not isinstance(setup, dict):
        raise TypeError(f"{name}: setup must be a table")
    labels: set[str] = set()
    label_locations: dict[str, tuple[str, str, str]] = {}
    for player, player_setup in setup.items():
        if player not in {"P0", "P1"}:
            raise ValueError(f"{name}: setup.{player} is unsupported")
        if not isinstance(player_setup, dict):
            raise TypeError(f"{name}: setup.{player} must be a table")
        unknown = player_setup.keys() - PLAYER_SETUP_FIELDS
        if unknown:
            raise ValueError(f"{name}: setup.{player}.{min(unknown)} is unsupported")
        if "life" in player_setup and (
            isinstance(player_setup["life"], bool)
            or not isinstance(player_setup["life"], int)
            or not -(2**31) <= player_setup["life"] < 2**31
        ):
            raise ValueError(f"{name}: setup.{player}.life must be a 32-bit integer")
        for field in ("hand", "library_top", "graveyard", "sideboard", "exile"):
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
                        label_locations[label] = (player, field, card["card"])
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
            label_locations[label] = (player, "battlefield", permanent["card"])
            if "tapped" in permanent and not isinstance(permanent["tapped"], bool):
                raise ValueError(f"{context}.tapped must be a boolean")
            if "counters" in permanent and not isinstance(permanent["counters"], dict):
                raise TypeError(f"{context}.counters must be a table")
        mana_pool = player_setup.get("mana_pool", [])
        if not isinstance(mana_pool, list):
            raise TypeError(f"{name}: setup.{player}.mana_pool must be an array")
        for index, unit in enumerate(mana_pool):
            context = f"{name}: {player}.mana_pool[{index}]"
            if not isinstance(unit, dict) or unit.keys() != {"color", "source"}:
                raise ValueError(f"{context} needs color and source")
            if unit["color"] not in {"W", "U", "B", "R", "G", "C"}:
                raise ValueError(f"{context}.color must be W, U, B, R, G, or C")
            source = unit["source"]
            location = label_locations.get(source) if isinstance(source, str) else None
            if location is None or location[:2] != (player, "battlefield"):
                raise ValueError(
                    f"{context}.source must name a card on {player}'s battlefield"
                )

    script = scenario.get("script", [])
    if not isinstance(script, list):
        raise TypeError(f"{name}: script must be an array")
    for index, action in enumerate(script):
        if not isinstance(action, dict):
            raise TypeError(f"{name}: script[{index}] must be a table")
        if "expect" in action:
            context = f"{name}: script[{index}].expect"
            if action["expect"] != "refused":
                raise ValueError(f"{context} must be 'refused'")
            if action.get("action") not in {"cast", "activate"}:
                raise ValueError(f"{context} is only valid for cast or activate")
            if {"sacrifice", "alternative_cost", "x"} & action.keys():
                raise ValueError(f"{context} cannot be combined with choice scripts")
        if action.get("action") == "cast" and "player" in action:
            if action["player"] not in {"P0", "P1"}:
                raise ValueError(
                    f"{name}: script[{index}].cast.player must be P0 or P1"
                )
        if action.get("action") == "pass_priority":
            context = f"{name}: script[{index}].pass_priority"
            unknown = action.keys() - {"action", "player"}
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            if action.get("player") not in {"P0", "P1"}:
                raise ValueError(f"{context}.player must be P0 or P1")
        if action.get("action") == "label_permanent":
            context = f"{name}: script[{index}].label_permanent"
            unknown = action.keys() - {"action", "player", "card", "label", "token"}
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            player = action.get("player")
            if player not in {"P0", "P1"}:
                raise ValueError(f"{context}.player must be P0 or P1")
            for field in ("card", "label"):
                if not isinstance(action.get(field), str) or not action[field]:
                    raise ValueError(f"{context}.{field} must be a non-empty string")
            if action["label"].startswith("@") or action["label"] in labels:
                raise ValueError(
                    f"{context}.label must be unique and not use the reserved @ prefix"
                )
            if not isinstance(action.get("token"), bool):
                raise TypeError(f"{context}.token must be a boolean")
            labels.add(action["label"])
            label_locations[action["label"]] = (player, "battlefield", action["card"])
        if action.get("action") == "choose_surveil":
            context = f"{name}: script[{index}].choose_surveil"
            unknown = action.keys() - {"action", "player", "cards"}
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            player = action.get("player")
            if player not in {"P0", "P1"}:
                raise ValueError(f"{context}.player must be P0 or P1")
            choices = action.get("cards")
            if not isinstance(choices, list):
                raise TypeError(f"{context}.cards must be an array")
            if any(not isinstance(label, str) or not label for label in choices):
                raise ValueError(f"{context}.cards entries must be labels")
            if len(choices) != len(set(choices)):
                raise ValueError(f"{context}.cards cannot repeat labels")
            for label in choices:
                if label_locations.get(label, (None, None, None))[:2] != (
                    player,
                    "library_top",
                ):
                    raise ValueError(
                        f"{context} label {label} must be in {player}'s library_top"
                    )
        if action.get("action") == "choose_outside_game":
            context = f"{name}: script[{index}].choose_outside_game"
            unknown = action.keys() - {"action", "player", "accept", "cards"}
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            if action.get("player") not in {"P0", "P1"}:
                raise ValueError(f"{context}.player must be P0 or P1")
            if not isinstance(action.get("accept"), bool):
                raise TypeError(f"{context}.accept must be a boolean")
            choices = action.get("cards")
            if not isinstance(choices, list):
                raise TypeError(f"{context}.cards must be an array")
            if not action["accept"] and choices:
                raise ValueError(f"{context}.cards must be empty when declining")
            for label in choices:
                if not isinstance(label, str) or not label:
                    raise ValueError(f"{context}.cards entries must be labels")
                if label_locations.get(label, (None, None, None))[1] != "sideboard":
                    raise ValueError(f"{context} label {label} must be in a sideboard")
        if action.get("action") == "choose_mana":
            context = f"{name}: script[{index}].choose_mana"
            unknown = action.keys() - {
                "action",
                "player",
                "sources",
                "phase_sources",
                "forge_sources",
            }
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            player = action.get("player")
            if player not in {"P0", "P1"}:
                raise ValueError(f"{context}.player must be P0 or P1")
            source_sets = []
            if "sources" in action:
                if "phase_sources" in action or "forge_sources" in action:
                    raise ValueError(
                        f"{context} cannot combine sources with oracle-specific sources"
                    )
                source_sets.append(("sources", action["sources"]))
            else:
                if "phase_sources" not in action or "forge_sources" not in action:
                    raise ValueError(
                        f"{context} needs sources or both phase_sources and forge_sources"
                    )
                source_sets.extend(
                    (
                        ("phase_sources", action["phase_sources"]),
                        ("forge_sources", action["forge_sources"]),
                    )
                )
            setup = scenario.get("setup", {}).get(player, {})
            mana_sources = {
                unit.get("source")
                for unit in setup.get("mana_pool", [])
                if isinstance(unit, dict)
            }
            for field, sources in source_sets:
                if not isinstance(sources, list) or not sources:
                    raise TypeError(f"{context}.{field} must be a non-empty array")
                if any(not isinstance(source, str) or not source for source in sources):
                    raise ValueError(f"{context}.{field} entries must be labels")
                if len(sources) != len(set(sources)):
                    raise ValueError(f"{context}.{field} cannot repeat labels")
                if any(source not in mana_sources for source in sources):
                    raise ValueError(f"{context}.{field} must name mana_pool sources")
        if action.get("action") == "choose_replacement":
            context = f"{name}: script[{index}].choose_replacement"
            unknown = action.keys() - {"action", "player", "description"}
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            if action.get("player") not in {"P0", "P1"}:
                raise ValueError(f"{context}.player must be P0 or P1")
            description = action.get("description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"{context}.description must be a non-empty string")
        if action.get("action") == "choose_entity":
            context = f"{name}: script[{index}].choose_entity"
            unknown = action.keys() - {"action", "player", "card"}
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            if action.get("player") not in {"P0", "P1"}:
                raise ValueError(f"{context}.player must be P0 or P1")
            card = action.get("card")
            if not isinstance(card, str) or not card.strip():
                raise ValueError(f"{context}.card must be a non-empty string")
        if "behold" in action and action.get("action") != "play_land":
            raise ValueError(
                f"{name}: script[{index}].behold is only valid for play_land actions"
            )
        if "choices" in action and action.get("action") != "unless_choices":
            raise ValueError(
                f"{name}: script[{index}].choices is only valid for unless_choices actions"
            )
        if "attackers" in action and action.get("action") != "attack":
            raise ValueError(
                f"{name}: script[{index}].attackers is only valid for attack actions"
            )
        if "new_targets" in action and action.get("action") != "copy_choice":
            raise ValueError(
                f"{name}: script[{index}].new_targets is only valid for copy_choice actions"
            )
        if "accept" in action and action.get("action") != "choose_outside_game":
            raise ValueError(
                f"{name}: script[{index}].accept is only valid for choose_outside_game actions"
            )
        if "sources" in action and action.get("action") != "choose_mana":
            raise ValueError(
                f"{name}: script[{index}].sources is only valid for choose_mana actions"
            )
        if "description" in action and action.get("action") != "choose_replacement":
            raise ValueError(
                f"{name}: script[{index}].description is only valid for choose_replacement actions"
            )
        if "sacrifice" in action:
            context = f"{name}: script[{index}].sacrifice"
            if action.get("action") != "cast":
                raise ValueError(f"{context} is only valid for cast actions")
            if "x" in action:
                raise ValueError(f"{context} cannot be combined with cast.x")
            sacrifices = action["sacrifice"]
            if not isinstance(sacrifices, list):
                raise TypeError(f"{context} must be an array of battlefield labels")
            if any(not isinstance(label, str) or not label for label in sacrifices):
                raise TypeError(f"{context} entries must be non-empty setup labels")
            if len(sacrifices) != len(set(sacrifices)):
                raise ValueError(f"{context} cannot repeat labels")
            for label in sacrifices:
                location = label_locations.get(label)
                if location is None:
                    raise ValueError(f"{context} contains unknown setup label {label}")
                if location[1] != "battlefield":
                    raise ValueError(
                        f"{context} label {label} must be in the battlefield"
                    )
        if action.get("action") == "attack":
            context = f"{name}: script[{index}].attack"
            unknown = action.keys() - {"action", "attackers", "through"}
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            if "through" in action and action["through"] != "damage":
                raise ValueError(f'{context}.through must be "damage"')
            attackers = action.get("attackers")
            if not isinstance(attackers, list) or not attackers:
                raise ValueError(f"{context}.attackers must be a non-empty label array")
            if any(not isinstance(label, str) or not label for label in attackers):
                raise TypeError(f"{context}.attackers entries must be non-empty labels")
            if len(attackers) != len(set(attackers)):
                raise ValueError(f"{context}.attackers cannot repeat labels")
            for label in attackers:
                location = label_locations.get(label)
                if location is None:
                    raise ValueError(
                        f"{context}.attackers contains unknown label {label}"
                    )
                if location[0] != "P0" or location[1] != "battlefield":
                    raise ValueError(
                        f"{context}.attackers label {label} must be on P0's battlefield"
                    )
        if action.get("action") in {"graveyard_targets", "copy_targets"}:
            action_name = action["action"]
            context = f"{name}: script[{index}].{action_name}"
            unknown = action.keys() - {"action", "player", "targets"}
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            player = action.get("player")
            if player not in {"P0", "P1"}:
                raise ValueError(f"{context}.player must be P0 or P1")
            targets = action.get("targets")
            if not isinstance(targets, list):
                raise TypeError(f"{context}.targets must be an array")
            if any(not isinstance(label, str) or not label for label in targets):
                raise TypeError(f"{context}.targets entries must be non-empty labels")
            if len(targets) != len(set(targets)):
                raise ValueError(f"{context}.targets cannot repeat labels")
            for label in targets:
                location = label_locations.get(label)
                if location is None:
                    raise ValueError(
                        f"{context}.targets contains unknown label {label}"
                    )
                if action_name == "graveyard_targets" and location[:2] != (
                    player,
                    "graveyard",
                ):
                    raise ValueError(
                        f"{context} label {label} must be in {player}'s graveyard"
                    )
        if action.get("action") == "choose_copies":
            context = f"{name}: script[{index}].choose_copies"
            unknown = action.keys() - {"action", "player", "source", "cards"}
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            player = action.get("player")
            if player not in {"P0", "P1"}:
                raise ValueError(f"{context}.player must be P0 or P1")
            source = action.get("source")
            if not isinstance(source, str) or not source:
                raise TypeError(f"{context}.source must be a setup label")
            if label_locations.get(source, (None, None, None))[:2] != (
                player,
                "hand",
            ):
                raise ValueError(f"{context}.source must be in {player}'s hand")
            cards = action.get("cards")
            if not isinstance(cards, list):
                raise TypeError(f"{context}.cards must be an array")
            if any(not isinstance(label, str) or not label for label in cards):
                raise TypeError(f"{context}.cards entries must be non-empty labels")
            if len(cards) != len(set(cards)):
                raise ValueError(f"{context}.cards cannot repeat labels")
            for label in cards:
                if label_locations.get(label, (None, None, None))[:2] != (
                    player,
                    "graveyard",
                ):
                    raise ValueError(
                        f"{context}.cards label {label} must be in {player}'s graveyard"
                    )
        if action.get("action") == "copy_choice":
            context = f"{name}: script[{index}].copy_choice"
            unknown = action.keys() - {"action", "player", "decision", "new_targets"}
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            if action.get("player") not in {"P0", "P1"}:
                raise ValueError(f"{context}.player must be P0 or P1")
            decision = action.get("decision")
            if decision not in {"accept", "decline"}:
                raise ValueError(f"{context}.decision must be accept or decline")
            if "new_targets" in action:
                if decision != "accept":
                    raise ValueError(
                        f"{context}.new_targets is only valid when accepting the copy"
                    )
                new_targets = action["new_targets"]
                if new_targets != "keep":
                    if isinstance(new_targets, list) and new_targets:
                        if any(
                            not isinstance(label, str) or not label
                            for label in new_targets
                        ):
                            raise TypeError(
                                f"{context}.new_targets entries must be non-empty labels"
                            )
                        for label in new_targets:
                            if label not in labels:
                                raise ValueError(
                                    f"{context}.new_targets contains unknown setup label {label}"
                                )
                    else:
                        raise ValueError(
                            f"{context}.new_targets must be keep or a non-empty label array"
                        )
        if action.get("action") == "unless_choices":
            context = f"{name}: script[{index}].unless_choices"
            unknown = action.keys() - {"action", "player", "choices"}
            if unknown:
                raise ValueError(f"{context}.{min(unknown)} is unsupported")
            player = action.get("player")
            if not isinstance(player, str) or player not in {"P0", "P1"}:
                raise ValueError(f"{context}.player must be P0 or P1")
            choices = action.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError(f"{context}.choices must be a non-empty array")
            for choice_index, choice in enumerate(choices):
                if choice == "decline":
                    continue
                choice_context = f"{context}.choices[{choice_index}]"
                if not isinstance(choice, dict) or len(choice) != 1:
                    raise ValueError(
                        f"{choice_context} must contain exactly one sacrifice or discard choice"
                    )
                kind, label = next(iter(choice.items()))
                if kind not in {"sacrifice", "discard"}:
                    raise ValueError(f"{choice_context} must use sacrifice or discard")
                if not isinstance(label, str) or not label:
                    raise TypeError(f"{choice_context}.{kind} must be a setup label")
                location = label_locations.get(label)
                if location is None:
                    raise ValueError(
                        f"{choice_context}.{kind} contains unknown setup label {label}"
                    )
                expected_zone = "battlefield" if kind == "sacrifice" else "hand"
                if location[0] != player or location[1] != expected_zone:
                    raise ValueError(
                        f"{choice_context}.{kind} label {label} must be in {player}'s {expected_zone}"
                    )
        if "from" in action:
            context = f"{name}: script[{index}].from"
            if action.get("action") != "cast":
                raise ValueError(f"{context} is only valid for cast actions")
            source = action["from"]
            if not isinstance(source, str) or source not in {"hand", "graveyard"}:
                raise ValueError(f"{context} must be hand or graveyard")
            if source == "graveyard":
                card_label = action.get("card")
                if not isinstance(card_label, str) or card_label not in label_locations:
                    raise ValueError(
                        f"{name}: script[{index}].card must be a setup label"
                    )
                if (
                    action.get("expect") != "refused"
                    and label_locations[card_label][1] != "graveyard"
                ):
                    raise ValueError(
                        f"{name}: script[{index}].card must be a graveyard setup label"
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

    queue_choice_actions = {
        "choose_cards",
        "choose_copies",
        "choose_discard",
        "choose_mana",
        "choose_outside_game",
        "choose_surveil",
        "copy_choice",
        "copy_targets",
        "graveyard_targets",
        "trigger_targets",
        "unless_choices",
    }
    for index, action in enumerate(script):
        choice_action = action.get("action")
        if choice_action not in queue_choice_actions:
            continue
        remaining = script[index + 1 :]
        if choice_action != "choose_mana":
            resolution_index = next(
                (
                    offset
                    for offset, candidate in enumerate(remaining)
                    if candidate.get("action") == "pass_until_resolved"
                ),
                None,
            )
            if resolution_index is not None:
                remaining = remaining[:resolution_index]
        consumer = next(
            (
                candidate
                for candidate in remaining
                if candidate.get("action") in {"cast", "activate"}
            ),
            None,
        )
        if consumer is not None and consumer.get("expect") == "refused":
            raise ValueError(
                f"{name}: script[{index}].{choice_action} cannot precede a refused action"
            )


def load_specs(paths: list[Path]) -> dict:
    scenarios = []
    suite_oracles = {}
    for path in paths:
        with path.open("rb") as source:
            data = tomllib.load(source)
        oracle = data.get("oracle", "xmage")
        if oracle not in {"xmage", "forge"}:
            raise ValueError(f"{path}: oracle must be xmage or forge")
        suite_oracles[path.stem] = oracle
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
            _validate_setup(scenario, oracle)
            scenarios.append(scenario)
    names = [scenario["name"] for scenario in scenarios]
    if len(names) != len(set(names)):
        raise ValueError("scenario names must be unique across spec files")
    return {"scenarios": scenarios, "suite_oracles": suite_oracles}


def split_scenarios_by_oracle(
    scenarios: list[dict],
    suite_scenarios: dict[str, list[str]],
    suite_oracles: dict[str, str],
) -> dict[str, list[dict]]:
    suite_by_name = {
        name: suite for suite, names in suite_scenarios.items() for name in names
    }
    if set(suite_by_name) != {scenario["name"] for scenario in scenarios}:
        raise ValueError("scenario suites do not match the loaded scenario list")
    unknown_suites = set(suite_scenarios) - suite_oracles.keys()
    if unknown_suites:
        raise ValueError(f"missing oracle for suite {min(unknown_suites)}")
    return {
        oracle: [
            scenario
            for scenario in scenarios
            if suite_oracles[suite_by_name[scenario["name"]]] == oracle
        ]
        for oracle in ("xmage", "forge")
    }


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
        "--forge-root",
        type=Path,
        default=Path(os.environ.get("FORGE_CHECKOUT", ROOT / ".cache/forge")),
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
    try:
        dispatch = split_scenarios_by_oracle(
            spec["scenarios"], suite_scenarios, spec["suite_oracles"]
        )
    except ValueError as error:
        print(f"invalid oracle dispatch: {error}", file=sys.stderr)
        return 2
    xmage_scenarios = dispatch["xmage"]
    forge_scenarios = dispatch["forge"]
    xmage_spec_path = outdir / "xmage-scenario-spec.json"
    forge_spec_path = outdir / "forge-scenario-spec.json"
    xmage_spec_path.write_text(
        json.dumps({"scenarios": xmage_scenarios}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    forge_spec_path.write_text(
        json.dumps({"scenarios": forge_scenarios}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
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
    mage_returncode = 0
    mage_results = []
    if xmage_scenarios:
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
                f"-DoracleScenarioSpec={xmage_spec_path}",
                "test",
            ],
            cwd=mage_root,
            text=True,
            capture_output=True,
            check=False,
        )
        mage_returncode = mage.returncode
        (outdir / "mage.log").write_text(mage.stdout + mage.stderr, encoding="utf-8")
        mage_report_path = (
            mage_root
            / "Mage.Tests/target/surefire-reports/"
            / "TEST-org.mage.test.testapi.CardScenarioProjectionTest.xml"
        )
        try:
            report = mage_report_path.read_text(encoding="utf-8")
            (outdir / "mage-surefire-report.xml").write_text(report, encoding="utf-8")
            mage_results = parse_mage_report(report)
        except (OSError, ET.ParseError, json.JSONDecodeError) as error:
            print(f"Could not read XMage scenario results: {error}", file=sys.stderr)
    else:
        (outdir / "mage.log").write_text(
            "XMage not selected by any suite\n", encoding="utf-8"
        )

    forge_returncode = 0
    forge_results = []
    forge_prompt_failures = []
    if forge_scenarios:
        forge_root = args.forge_root
        if not forge_root.is_absolute():
            forge_root = ROOT / forge_root
        forge_output = outdir / "forge"
        forge = subprocess.run(
            [
                str(ROOT / "scripts/run_forge_scenarios.sh"),
                str(forge_spec_path),
                str(forge_output),
                str(forge_root),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        forge_returncode = forge.returncode
        (outdir / "forge.log").write_text(forge.stdout + forge.stderr, encoding="utf-8")
        forge_report_path = forge_output / "surefire-report.xml"
        try:
            report = forge_report_path.read_text(encoding="utf-8")
            (outdir / "forge-surefire-report.xml").write_text(report, encoding="utf-8")
            forge_results = parse_forge_report(report)
            forge_prompt_failures = parse_forge_prompt_failures(report)
        except (OSError, ET.ParseError, json.JSONDecodeError) as error:
            print(f"Could not read Forge scenario results: {error}", file=sys.stderr)
    else:
        (outdir / "forge.log").write_text(
            "Forge not selected by any suite\n", encoding="utf-8"
        )
    (outdir / "phase-projection.json").write_text(
        json.dumps(phase_results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (outdir / "mage-projection.json").write_text(
        json.dumps(mage_results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (outdir / "forge-projection.json").write_text(
        json.dumps(forge_results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if phase.returncode:
        runner_errors.append(f"Phase runner exited {phase.returncode}")
    if mage_returncode:
        runner_errors.append(f"XMage runner exited {mage_returncode}")
    if forge_returncode:
        runner_errors.append(f"Forge runner exited {forge_returncode}")
    runner_errors.extend(
        validate_refusal_indices(spec["scenarios"], phase_results, "Phase")
    )
    if xmage_scenarios:
        runner_errors.extend(
            validate_refusal_indices(xmage_scenarios, mage_results, "XMage")
        )
    if forge_scenarios:
        runner_errors.extend(
            validate_refusal_indices(forge_scenarios, forge_results, "Forge")
        )
    compared_phase_results = normalize_label_insensitive_count_groups(
        spec["scenarios"], phase_results
    )
    oracle_results = [*mage_results, *forge_results]
    compared_oracle_results = normalize_label_insensitive_count_groups(
        spec["scenarios"], oracle_results
    )
    report = evaluate_known_diffs(
        suite_scenarios,
        compared_phase_results,
        compared_oracle_results,
        known_diffs,
        runner_errors,
        forge_prompt_failures,
        spec["suite_oracles"],
    )
    report["oracles_by_suite"] = spec["suite_oracles"]
    report_path = outdir / "known-diffs-report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    comparison_log = format_known_diff_report(report)
    (outdir / "compare.log").write_text(comparison_log, encoding="utf-8")
    print(comparison_log, end="")
    phase_map, _ = _results_by_name(compared_phase_results, "Phase")
    oracle_map, _ = _results_by_name(compared_oracle_results, "oracle")
    for item in [*report["triaged"], *report["failed"]]:
        scenario = item.get("scenario")
        if not scenario or scenario not in phase_map or scenario not in oracle_map:
            continue
        differences = compare(
            {scenario: phase_map[scenario]}, {scenario: oracle_map[scenario]}
        )
        if differences:
            oracle_name = spec["suite_oracles"].get(item.get("suite"), "unknown")
            oracle_label = "XMage" if oracle_name == "xmage" else "Forge"
            print(
                f"DIVERGENCE {item.get('suite')} ({oracle_label}) / "
                f"{scenario}: {', '.join(differences)}"
            )
            print(f"phase-rs: {json.dumps(phase_map[scenario], sort_keys=True)}")
            print(f"{oracle_label}: {json.dumps(oracle_map[scenario], sort_keys=True)}")
    for engine, results in (
        ("PHASE", phase_results),
        ("XMAGE", mage_results),
        ("FORGE", forge_results),
    ):
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
