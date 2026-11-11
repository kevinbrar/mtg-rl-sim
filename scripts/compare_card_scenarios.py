"""Compare two JSON projection files emitted by card-scenario runners."""

from __future__ import annotations

import json
import sys
from pathlib import Path


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
    for name in sorted(left.keys() | right.keys()):
        a, b = left.get(name), right.get(name)
        if a is None or b is None or a.get("projection") != b.get("projection"):
            differences.append(name)
        elif not a.get("reach", False) or not b.get("reach", False):
            differences.append(f"{name} (reach failed)")
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
