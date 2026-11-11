from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path

import pytest

from scripts.compare_card_scenarios import compare, read_results
from scripts.run_card_scenarios import (
    check_changed_scenario_suites,
    load_specs,
    main,
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


def test_runner_rejects_empty_second_suite_with_file_and_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = tmp_path / "first.toml"
    first.write_text(
        '[[scenarios]]\nname = "first"\n[scenarios.setup.P0]\n',
        encoding="utf-8",
    )
    second = tmp_path / "empty.toml"
    second.write_text("", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["run_card_scenarios.py", str(first), str(second)])
    assert main() == 2
    error = capsys.readouterr().err
    assert str(second) in error
    assert "contains no scenarios" in error


def test_runner_rejects_scenario_without_name_with_file_and_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = tmp_path / "unnamed.toml"
    spec.write_text(
        "[[scenarios]]\n[scenarios.setup.P0]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["run_card_scenarios.py", str(spec)])
    assert main() == 2
    error = capsys.readouterr().err
    assert str(spec) in error
    assert "scenario 1 is missing a name" in error


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


def test_comma_card_name_resolves_to_one_scenario_spec() -> None:
    root = Path(__file__).parents[1]
    assert scenario_paths_for_cards("Ms. Marvel, Kamala Khan") == [
        root / "oracle/scenarios/ms-marvel-kamala-khan.toml"
    ]


def test_semicolon_separated_card_names_resolve_to_two_specs() -> None:
    root = Path(__file__).parents[1]
    assert scenario_paths_for_cards("a; b") == [
        root / "oracle/scenarios/a.toml",
        root / "oracle/scenarios/b.toml",
    ]


def test_card_names_reject_empty_semicolon_segments() -> None:
    with pytest.raises(ValueError, match="semicolon-separated"):
        scenario_paths_for_cards("a;;b")


def test_listed_scenario_suite_slug_matches_card() -> None:
    check_changed_scenario_suites(
        "Essence Burn", ["oracle/scenarios/essence-burn.toml"]
    )


def test_comma_card_name_matches_changed_scenario_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = tmp_path / "oracle/scenarios/ms-marvel-kamala-khan.toml"
    suite.parent.mkdir(parents=True)
    suite.write_text("", encoding="utf-8")
    monkeypatch.setattr("scripts.run_card_scenarios.ROOT", tmp_path)
    check_changed_scenario_suites(
        "Ms. Marvel, Kamala Khan",
        ["oracle/scenarios/ms-marvel-kamala-khan.toml"],
    )


def test_unlisted_changed_scenario_suite_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.run_card_scenarios.ROOT", tmp_path)
    with pytest.raises(ValueError, match="not listed") as error:
        check_changed_scenario_suites(
            "essence burn", ["oracle/scenarios/unlisted.toml"]
        )
    assert "oracle/scenarios/unlisted.toml" in str(error.value)


def test_deleted_unlisted_scenario_suite_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.run_card_scenarios.ROOT", tmp_path)
    with pytest.raises(ValueError, match="not listed") as error:
        check_changed_scenario_suites("essence burn", ["oracle/scenarios/deleted.toml"])
    assert "oracle/scenarios/deleted.toml" in str(error.value)


def test_deleted_listed_scenario_suite_fails_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scripts.run_card_scenarios.ROOT", tmp_path)
    with pytest.raises(ValueError, match="is missing") as error:
        check_changed_scenario_suites(
            "essence burn", ["oracle/scenarios/essence-burn.toml"]
        )
    assert "oracle/scenarios/essence-burn.toml" in str(error.value)


def test_card_scenario_runner_fails_when_mapped_spec_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("scripts.run_card_scenarios.ROOT", tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["run_card_scenarios.py", "--cards", "essence burn"]
    )
    assert main() == 2
    assert (
        str(tmp_path / "oracle/scenarios/essence-burn.toml") in capsys.readouterr().err
    )


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


def test_projection_comparator_ignores_unordered_zone_permutations() -> None:
    left = {
        "S1": {
            "scenario": "S1",
            "projection": {
                "battlefield": [
                    {"controller": "P0", "name": "Bringer"},
                    {"controller": "P1", "name": "Grizzly"},
                ],
                "exile": ["Shock", "Giant Growth"],
                "players": {
                    "P0": {
                        "battlefield": ["Bringer", "Swamp"],
                        "hand": ["Forest", "Shock"],
                        "library": ["Forest", "Swamp"],
                        "graveyard": ["Walking Corpse", "Shock"],
                    }
                },
            },
            "reach": True,
        }
    }
    right = copy.deepcopy(left)
    projection = right["S1"]["projection"]
    projection["battlefield"].reverse()
    projection["exile"].reverse()
    projection["players"]["P0"]["battlefield"].reverse()
    projection["players"]["P0"]["hand"].reverse()
    left_before = copy.deepcopy(left)
    right_before = copy.deepcopy(right)

    assert compare(left, right) == []
    assert left == left_before
    assert right == right_before


def test_projection_comparator_rejects_battlefield_content_difference() -> None:
    left = {
        "S1": {
            "scenario": "S1",
            "projection": {"battlefield": [{"controller": "P0", "name": "Bringer"}]},
            "reach": True,
        }
    }
    right = copy.deepcopy(left)
    right["S1"]["projection"]["battlefield"][0]["name"] = "Vren"

    assert compare(left, right) == ["S1"]


@pytest.mark.parametrize("zone", ("graveyard", "library"))
def test_projection_comparator_preserves_ordered_zone_permutations(
    zone: str,
) -> None:
    left = {
        "S1": {
            "scenario": "S1",
            "projection": {
                "players": {"P0": {zone: ["first", "second"]}},
            },
            "reach": True,
        }
    }
    right = copy.deepcopy(left)
    right["S1"]["projection"]["players"]["P0"][zone].reverse()

    assert compare(left, right) == ["S1"]


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
