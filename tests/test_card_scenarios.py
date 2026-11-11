from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from scripts.compare_card_scenarios import compare, read_results
from scripts.run_card_scenarios import load_specs


def test_essence_burn_spec_contains_five_engine_computed_scenarios() -> None:
    path = Path(__file__).parents[1] / "oracle/scenarios/essence-burn.toml"
    with path.open("rb") as source:
        spec = tomllib.load(source)
    assert [scenario["name"] for scenario in spec["scenarios"]] == [
        "S1 lethal black creature",
        "S2 later same-turn death is exiled",
        "S3 damage wears off at end of turn",
        "S4 legal targets",
        "S5 lethal black planeswalker",
    ]
    assert all("expected" not in scenario for scenario in spec["scenarios"])


def test_load_specs_rejects_duplicate_names(tmp_path: Path) -> None:
    first = tmp_path / "first.toml"
    second = tmp_path / "second.toml"
    first.write_text('[[scenarios]]\nname = "same"\n', encoding="utf-8")
    second.write_text('[[scenarios]]\nname = "same"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_specs([first, second])


def test_projection_comparator_requires_equal_projections_and_reach() -> None:
    left = {"S1": {"scenario": "S1", "projection": {"life": 20}, "reach": True}}
    assert compare(left, left) == []
    right = {"S1": {"scenario": "S1", "projection": {"life": 19}, "reach": True}}
    assert compare(left, right) == ["S1"]
    right["S1"]["projection"] = {"life": 20}
    right["S1"]["reach"] = False
    assert compare(left, right) == ["S1 (reach failed)"]


def test_projection_file_rejects_duplicate_names(tmp_path: Path) -> None:
    path = tmp_path / "projection.json"
    path.write_text(
        json.dumps(
            [
                {"scenario": "S1", "projection": {}},
                {"scenario": "S1", "projection": {}},
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        read_results(path)
