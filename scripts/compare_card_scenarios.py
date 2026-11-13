"""Compare two JSON projection files emitted by card-scenario runners."""

from __future__ import annotations

import copy
import json
import sys
import unicodedata
from pathlib import Path


def _json_sort_key(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def _normalize_card_name(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.category(character).startswith("M")
    )


def _normalize_projection_names(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "name" and isinstance(item, str):
                value[key] = _normalize_card_name(item)
            else:
                _normalize_projection_names(item)
    elif isinstance(value, list):
        for item in value:
            _normalize_projection_names(item)


def normalize_projection(projection: object) -> object:
    if not isinstance(projection, dict):
        return projection
    normalized = copy.deepcopy(projection)
    _normalize_projection_names(normalized)
    for zone in ("battlefield", "exile", "hand"):
        values = normalized.get(zone)
        if isinstance(values, list):
            values = [
                _normalize_card_name(value) if isinstance(value, str) else value
                for value in values
            ]
            normalized[zone] = sorted(values, key=_json_sort_key)

    players = normalized.get("players")
    if isinstance(players, dict):
        for player in players.values():
            if not isinstance(player, dict):
                continue
            for zone in ("battlefield", "exile", "hand", "library", "graveyard"):
                values = player.get(zone)
                if isinstance(values, list):
                    values = [
                        _normalize_card_name(value) if isinstance(value, str) else value
                        for value in values
                    ]
                    player[zone] = (
                        sorted(values, key=_json_sort_key)
                        if zone in ("battlefield", "exile", "hand")
                        else values
                    )

    return normalized


def read_results(path: Path) -> dict[str, dict]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise ValueError(f"{path}: expected a list of scenario projections")
    results = {value["scenario"]: value for value in values}
    if len(results) != len(values):
        raise ValueError(f"{path}: duplicate scenario names")
    return results


def compare(left: dict[str, dict], right: dict[str, dict]) -> list[str]:
    differences = []
    if not left:
        differences.append("left side has no scenarios")
    if not right:
        differences.append("right side has no scenarios")
    if differences:
        return differences
    for name in sorted(left.keys() | right.keys()):
        a, b = left.get(name), right.get(name)
        if a is None:
            differences.append(f"{name} missing from left")
        elif b is None:
            differences.append(f"{name} missing from right")
        elif "projection" not in a or a["projection"] is None:
            differences.append(f"{name} missing projection on left")
        elif "projection" not in b or b["projection"] is None:
            differences.append(f"{name} missing projection on right")
        elif not a.get("reach", False) or not b.get("reach", False):
            differences.append(f"{name} (reach failed)")
        elif normalize_projection(a["projection"]) != normalize_projection(
            b["projection"]
        ):
            differences.append(name)
    return differences


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: compare_card_scenarios.py LEFT.json RIGHT.json", file=sys.stderr)
        return 2
    try:
        left, right = map(read_results, map(Path, sys.argv[1:]))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 2
    differences = compare(left, right)
    if differences:
        print(f"DIFF: {', '.join(differences)}")
        return 1
    print(f"PASS: {len(left)} scenarios match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
