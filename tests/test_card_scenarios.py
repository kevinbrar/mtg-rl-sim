from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from scripts.compare_card_scenarios import compare, read_results
from scripts.run_card_scenarios import (
    load_specs,
    parse_mage_report,
    scenario_paths_for_cards,
)


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
    first.write_text(
        '[[scenarios]]\nname = "same"\n[scenarios.setup.P0]\n',
        encoding="utf-8",
    )
    second.write_text(
        '[[scenarios]]\nname = "same"\n[scenarios.setup.P0]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unique"):
        load_specs([first, second])


def test_load_specs_rejects_damage_setup(tmp_path: Path) -> None:
    spec = tmp_path / "damage.toml"
    spec.write_text(
        """
[[scenarios]]
name = "damage fixture"

[scenarios.setup.P0]
battlefield = [{ label = "p0.bf.0", card = "Walking Corpse", damage = 0 }]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="damage is unsupported"):
        load_specs([spec])


def test_load_specs_rejects_unrepresentable_setup_fields(tmp_path: Path) -> None:
    spec = tmp_path / "unsupported.toml"
    spec.write_text(
        """
[[scenarios]]
name = "unsupported setup"

[scenarios.setup.P0]
exile = ["Walking Corpse"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="setup.P0.exile is unsupported"):
        load_specs([spec])


def test_card_names_resolve_to_scenario_specs() -> None:
    root = Path(__file__).parents[1]
    assert scenario_paths_for_cards("essence burn") == [
        root / "oracle/scenarios/essence-burn.toml"
    ]


def test_parse_mage_report_extracts_projection_records() -> None:
    report = """
<testsuite>
  <testcase>
    <system-out><![CDATA[
CARD_SCENARIO_RESULT:{"scenario":"S1","projection":{"reach":true},"reach":true}
]]></system-out>
  </testcase>
</testsuite>
"""
    assert parse_mage_report(report) == [
        {"scenario": "S1", "projection": {"reach": True}, "reach": True}
    ]


def test_projection_comparator_requires_equal_projections_and_reach() -> None:
    left = {"S1": {"scenario": "S1", "projection": {"life": 20}, "reach": True}}
    assert compare(left, left) == []
    right = {"S1": {"scenario": "S1", "projection": {"life": 19}, "reach": True}}
    assert compare(left, right) == ["S1"]
    right["S1"]["projection"] = {"life": 20}
    right["S1"]["reach"] = False
    assert compare(left, right) == ["S1 (reach failed)"]


def test_projection_comparator_rejects_empty_sides() -> None:
    projection = {"S1": {"scenario": "S1", "projection": {}, "reach": True}}
    assert compare({}, projection) == ["left side has no scenarios"]
    assert compare(projection, {}) == ["right side has no scenarios"]


def test_projection_comparator_rejects_scenarios_missing_from_one_side() -> None:
    left = {"S1": {"scenario": "S1", "projection": {}, "reach": True}}
    right = {"S2": {"scenario": "S2", "projection": {}, "reach": True}}
    assert compare(left, right) == ["S1 missing from right", "S2 missing from left"]


@pytest.mark.parametrize(
    ("left", "right", "difference"),
    [
        ({"S1": {"reach": True}}, {"S1": {"projection": {}, "reach": True}}, "left"),
        ({"S1": {"projection": {}, "reach": True}}, {"S1": {"reach": True}}, "right"),
        (
            {"S1": {"projection": None, "reach": True}},
            {"S1": {"projection": {}, "reach": True}},
            "left",
        ),
        (
            {"S1": {"projection": {}, "reach": True}},
            {"S1": {"projection": None, "reach": True}},
            "right",
        ),
    ],
)
def test_projection_comparator_rejects_missing_or_null_projection(
    left: dict[str, dict], right: dict[str, dict], difference: str
) -> None:
    assert compare(left, right) == [f"S1 missing projection on {difference}"]


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
