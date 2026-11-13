from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path

import pytest

from scripts.compare_card_scenarios import compare, read_results
from scripts.run_card_scenarios import (
    check_changed_known_diff_entries,
    check_changed_scenario_suites,
    evaluate_known_diffs,
    load_base_known_diffs,
    load_known_diffs,
    load_specs,
    main,
    parse_mage_report,
    projection_sha256,
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
    registry = tmp_path / "known-diffs.toml"
    registry.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_card_scenarios.py",
            "--known-diffs",
            str(registry),
            str(first),
            str(second),
        ],
    )
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
    registry = tmp_path / "known-diffs.toml"
    registry.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["run_card_scenarios.py", "--known-diffs", str(registry), str(spec)]
    )
    assert main() == 2
    error = capsys.readouterr().err
    assert str(spec) in error
    assert "scenario 1 is missing a name" in error


def test_base_known_diffs_requires_changed_suite_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "known-diffs.toml"
    base_registry = tmp_path / "base-known-diffs.toml"
    registry.write_text("", encoding="utf-8")
    base_registry.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_card_scenarios.py",
            "--known-diffs",
            str(registry),
            "--base-known-diffs",
            str(base_registry),
        ],
    )

    assert main() == 2
    assert (
        "--base-known-diffs requires --check-changed-suites" in capsys.readouterr().err
    )


def test_base_known_diffs_is_optional_when_registry_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed_files = tmp_path / "changed-files.txt"
    changed_files.write_text("docs/unrelated.md\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_card_scenarios.py",
            "--cards",
            "Vren, the Relentless",
            "--check-changed-suites",
            str(changed_files),
            "--known-diffs",
            "oracle/known-diffs.toml",
        ],
    )

    assert main() == 0


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


def test_load_specs_accepts_labeled_library_cards(tmp_path: Path) -> None:
    spec = tmp_path / "labeled-library.toml"
    spec.write_text(
        """
[[scenarios]]
name = "labeled library card"
script = [{ action = "choose_cards", cards = ["first"] }]

[scenarios.setup.P0]
library_top = [{ card = "Grizzly Bears", label = "first" }]
""",
        encoding="utf-8",
    )

    loaded = load_specs([spec])

    assert loaded["scenarios"][0]["setup"]["P0"]["library_top"] == [
        {"card": "Grizzly Bears", "label": "first"}
    ]


def test_load_specs_rejects_duplicate_setup_labels_across_zones(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "duplicate-labels.toml"
    spec.write_text(
        """
[[scenarios]]
name = "duplicate labels"

[scenarios.setup.P0]
battlefield = [{ label = "same", card = "Forest" }]
library_top = [{ label = "same", card = "Grizzly Bears" }]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate or empty setup label"):
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
    assert scenario_paths_for_cards("Anzrag's Rampage") == [
        root / "oracle/scenarios/anzrags-rampage.toml"
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
        "sys.argv",
        [
            "run_card_scenarios.py",
            "--cards",
            "essence burn",
            "--known-diffs",
            str(tmp_path / "known-diffs.toml"),
        ],
    )
    (tmp_path / "known-diffs.toml").write_text("", encoding="utf-8")
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


def test_projection_comparator_compares_battlefield_characteristics() -> None:
    left = {
        "S1": {
            "scenario": "S1",
            "projection": {
                "battlefield": [
                    {
                        "name": "Restless Prairie",
                        "types": ["Creature", "Land"],
                        "subtypes": ["Llama"],
                        "colors": ["G", "W"],
                    }
                ]
            },
            "reach": True,
        }
    }
    for field, changed in (
        ("types", ["Land"]),
        ("subtypes", []),
        ("colors", ["G"]),
    ):
        right = copy.deepcopy(left)
        right["S1"]["projection"]["battlefield"][0][field] = changed
        assert compare(left, right) == ["S1"]


def test_projection_comparison_normalizes_card_name_diacritics() -> None:
    left = {
        "S1": {
            "scenario": "S1",
            "projection": {
                "battlefield": [{"name": "Kíli the Resourceful"}],
                "players": {"P0": {"hand": ["Kíli the Resourceful"]}},
            },
            "reach": True,
        }
    }
    right = {
        "S1": {
            "scenario": "S1",
            "projection": {
                "battlefield": [{"name": "Kili the Resourceful"}],
                "players": {"P0": {"hand": ["Kili the Resourceful"]}},
            },
            "reach": True,
        }
    }

    assert compare(left, right) == []


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


def _projection_result(name: str, projection: dict) -> dict:
    return {"scenario": name, "projection": projection, "reach": True}


def _known_diff_entry(
    name: str,
    phase: dict,
    mage: dict,
    *,
    suite: str = "suite",
    category: str = "engine-bug",
) -> dict:
    return {
        "card": "Test Card",
        "category": category,
        "rationale": "Test rationale",
        "link": "https://example.com/card",
        "regression_test": "oracle/scenarios/suite.toml",
        "suite": suite,
        "scenario": name,
        "phase_sha256": projection_sha256(phase),
        "mage_sha256": projection_sha256(mage),
    }


def test_known_diff_triaged_pass() -> None:
    phase = _projection_result("V2", {"battlefield": ["2/2"]})
    mage = _projection_result("V2", {"battlefield": ["3/3"]})

    report = evaluate_known_diffs(
        {"suite": ["V2"]}, [phase], [mage], [_known_diff_entry("V2", phase, mage)]
    )

    assert report["counts"] == {"matched": 0, "triaged": 1, "failed": 0}
    assert report["triaged"][0]["category"] == "engine-bug"
    assert report["triaged"][0]["card"] == "Test Card"


def test_known_diff_stale_entry_fails() -> None:
    result = _projection_result("S1", {"life": 20})
    entry = _known_diff_entry("S1", result, result)

    report = evaluate_known_diffs({"suite": ["S1"]}, [result], [result], [entry])

    assert report["counts"]["failed"] == 1
    assert "stale" in report["failed"][0]["reason"]


def test_known_diff_phase_digest_mismatch_reports_both_observed_digests() -> None:
    phase = _projection_result("V2", {"power": 2})
    mage = _projection_result("V2", {"power": 3})
    entry = _known_diff_entry("V2", phase, mage)
    entry["phase_sha256"] = "0" * 64

    report = evaluate_known_diffs({"suite": ["V2"]}, [phase], [mage], [entry])
    reason = report["failed"][0]["reason"]

    assert "observed phase_sha256=" + projection_sha256(phase) in reason
    assert "observed mage_sha256=" + projection_sha256(mage) in reason


def test_known_diff_mage_digest_mismatch_fails() -> None:
    phase = _projection_result("V2", {"power": 2})
    mage = _projection_result("V2", {"power": 3})
    entry = _known_diff_entry("V2", phase, mage)
    entry["mage_sha256"] = "0" * 64

    report = evaluate_known_diffs({"suite": ["V2"]}, [phase], [mage], [entry])

    assert "digest mismatch" in report["failed"][0]["reason"]
    assert "observed phase_sha256=" in report["failed"][0]["reason"]
    assert "observed mage_sha256=" in report["failed"][0]["reason"]


def test_unsupported_known_diff_never_waives_a_divergence() -> None:
    phase = _projection_result("S1", {"life": 19})
    mage = _projection_result("S1", {"life": 20})
    entry = _known_diff_entry("S1", phase, mage, category="unsupported")

    report = evaluate_known_diffs({"suite": ["S1"]}, [phase], [mage], [entry])

    assert "never waive" in report["failed"][0]["reason"]


def test_duplicate_known_diff_entries_fail() -> None:
    phase = _projection_result("S1", {"life": 19})
    mage = _projection_result("S1", {"life": 20})
    entry = _known_diff_entry("S1", phase, mage)

    report = evaluate_known_diffs({"suite": ["S1"]}, [phase], [mage], [entry, entry])

    assert report["counts"]["failed"] == 1
    assert "duplicate" in report["failed"][0]["reason"]


def test_known_diff_for_unknown_scenario_in_run_suite_fails() -> None:
    result = _projection_result("S1", {"life": 20})
    entry = _known_diff_entry("missing", result, result)

    report = evaluate_known_diffs({"suite": ["S1"]}, [result], [result], [entry])

    assert any("not present" in failure["reason"] for failure in report["failed"])


@pytest.mark.parametrize(
    "entry_text",
    [
        'card = "Test Card"\ncategory = "engine-bug"\nrationale = "why"\n'
        'link = "https://example.com"\nregression_test = "test"\nsuite = "suite"\n'
        'scenario = "S1"\nphase_sha256 = "' + "a" * 64 + '"\n',
        'card = "Test Card"\ncategory = "engine-bug"\nrationale = "why"\n'
        'link = "https://example.com"\nregression_test = "test"\nsuite = "suite"\n'
        'scenario = "S1"\nphase_sha256 = "' + "a" * 64 + '"\n'
        'mage_sha256 = "' + "b" * 64 + '"\nextra = "unknown"\n',
        'card = "Test Card"\ncategory = "not-a-category"\nrationale = "why"\n'
        'link = "https://example.com"\nregression_test = "test"\nsuite = "suite"\n'
        'scenario = "S1"\nphase_sha256 = "' + "a" * 64 + '"\n'
        'mage_sha256 = "' + "b" * 64 + '"\n',
        'card = "Test Card"\ncategory = "unsupported"\nrationale = "why"\n'
        'link = "https://example.com"\nregression_test = "test"\nsuite = "suite"\n'
        'scenario = "S1"\nphase_sha256 = "z"\nmage_sha256 = "b"\n',
    ],
)
def test_known_diff_registry_rejects_malformed_entries(
    tmp_path: Path, entry_text: str
) -> None:
    path = tmp_path / "known-diffs.toml"
    path.write_text(f"[[diff]]\n{entry_text}", encoding="utf-8")

    with pytest.raises(ValueError):
        load_known_diffs(path)


def test_untriaged_divergence_still_fails() -> None:
    phase = _projection_result("S1", {"life": 19})
    mage = _projection_result("S1", {"life": 20})

    report = evaluate_known_diffs({"suite": ["S1"]}, [phase], [mage], [])

    assert report["counts"] == {"matched": 0, "triaged": 0, "failed": 1}


def test_known_diff_mixed_run_counts_matched_triaged_and_failed() -> None:
    matched_phase = _projection_result("match", {"life": 20})
    matched_mage = _projection_result("match", {"life": 20})
    triaged_phase = _projection_result("triaged", {"power": 2})
    triaged_mage = _projection_result("triaged", {"power": 3})
    failed_phase = _projection_result("failed", {"life": 19})
    failed_mage = _projection_result("failed", {"life": 20})
    entry = _known_diff_entry("triaged", triaged_phase, triaged_mage)

    report = evaluate_known_diffs(
        {"suite": ["match", "triaged", "failed"]},
        [matched_phase, triaged_phase, failed_phase],
        [matched_mage, triaged_mage, failed_mage],
        [entry],
    )

    assert report["counts"] == {"matched": 1, "triaged": 1, "failed": 1}


def test_known_diffs_for_unrun_suites_are_ignored() -> None:
    result = _projection_result("S1", {"life": 20})
    unrelated = _known_diff_entry("missing", result, result, suite="unrun-suite")

    report = evaluate_known_diffs({"suite": ["S1"]}, [result], [result], [unrelated])

    assert report["counts"] == {"matched": 1, "triaged": 0, "failed": 0}


def _write_base_known_diffs(path: Path, entries: list[dict]) -> None:
    lines = []
    for entry in entries:
        lines.append("[[diff]]")
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in entry.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_changed_known_diff_entry_for_unlisted_card_fails(tmp_path: Path) -> None:
    base_path = tmp_path / "base-known-diffs.toml"
    _write_base_known_diffs(base_path, [{"card": "Vren", "rationale": "old"}])

    with pytest.raises(ValueError, match="Vren"):
        check_changed_known_diff_entries(
            "Anzrag",
            ["oracle/known-diffs.toml"],
            base_path,
            [{"card": "Vren", "rationale": "updated"}],
        )


def test_changed_known_diff_entry_for_listed_card_passes(tmp_path: Path) -> None:
    base_path = tmp_path / "base-known-diffs.toml"
    _write_base_known_diffs(base_path, [{"card": "Vren", "rationale": "old"}])

    check_changed_known_diff_entries(
        "Vren",
        ["oracle/known-diffs.toml"],
        base_path,
        [{"card": "Vren", "rationale": "updated"}],
    )


def test_removed_known_diff_entry_requires_its_card(tmp_path: Path) -> None:
    base_path = tmp_path / "base-known-diffs.toml"
    _write_base_known_diffs(base_path, [{"card": "Vren", "rationale": "old"}])

    with pytest.raises(ValueError, match="Vren"):
        check_changed_known_diff_entries(
            "Anzrag", ["oracle/known-diffs.toml"], base_path, []
        )


def test_added_known_diff_entry_requires_its_card(tmp_path: Path) -> None:
    base_path = tmp_path / "base-known-diffs.toml"
    _write_base_known_diffs(base_path, [])

    with pytest.raises(ValueError, match="Vren"):
        check_changed_known_diff_entries(
            "Anzrag",
            ["oracle/known-diffs.toml"],
            base_path,
            [{"card": "Vren", "rationale": "new"}],
        )


def test_unchanged_known_diff_registry_with_base_arg_passes(tmp_path: Path) -> None:
    base_path = tmp_path / "base-known-diffs.toml"
    unchanged = {"card": "Vren", "rationale": "same"}
    _write_base_known_diffs(base_path, [unchanged])

    check_changed_known_diff_entries(
        "Vren",
        ["oracle/known-diffs.toml"],
        base_path,
        [unchanged],
    )


def test_changed_known_diff_registry_without_base_arg_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    changed_files = tmp_path / "changed-files.txt"
    changed_files.write_text("oracle/known-diffs.toml\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_card_scenarios.py",
            "--cards",
            "Vren, the Relentless",
            "--check-changed-suites",
            str(changed_files),
            "--known-diffs",
            "oracle/known-diffs.toml",
        ],
    )

    assert main() == 2
    assert "--base-known-diffs is required" in capsys.readouterr().err


def test_base_known_diff_parser_accepts_old_entry_schema(tmp_path: Path) -> None:
    path = tmp_path / "base-known-diffs.toml"
    _write_base_known_diffs(path, [{"card": "Vren", "legacy_field": "accepted"}])

    assert load_base_known_diffs(path) == [{"card": "Vren", "legacy_field": "accepted"}]


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ({"category": "engine-bug"}, "requires a non-empty card string"),
        ({"card": 5}, "requires a non-empty card string"),
    ],
)
def test_base_known_diff_parser_rejects_missing_or_non_string_card(
    tmp_path: Path, entry: dict, error: str
) -> None:
    path = tmp_path / "base-known-diffs.toml"
    _write_base_known_diffs(path, [entry])

    with pytest.raises(ValueError, match=error):
        load_base_known_diffs(path)


def test_projection_sha256_uses_comparator_unordered_zone_normalization() -> None:
    first = _projection_result(
        "S1", {"exile": ["Shock", "Forest"], "library": ["a", "b"]}
    )
    second = _projection_result(
        "S1", {"exile": ["Forest", "Shock"], "library": ["a", "b"]}
    )
    ordered_change = _projection_result(
        "S1", {"exile": ["Forest", "Shock"], "library": ["b", "a"]}
    )

    assert projection_sha256(first) == projection_sha256(second)
    assert projection_sha256(first) != projection_sha256(ordered_change)


@pytest.mark.parametrize("decision", ["accept", "decline"])
def test_load_specs_accepts_activated_alternative_cost_decision(
    tmp_path: Path, decision: str
) -> None:
    spec = tmp_path / "alternative-cost.toml"
    spec.write_text(
        f"""
[[scenarios]]
name = "alternative cost"
script = [{{ action = "activate", alternative_cost = "{decision}" }}]

[scenarios.setup.P0]
""",
        encoding="utf-8",
    )

    loaded = load_specs([spec])

    assert loaded["scenarios"][0]["script"][0]["alternative_cost"] == decision


@pytest.mark.parametrize(
    ("action", "error"),
    [
        (
            '{ action = "cast", alternative_cost = "accept" }',
            "only valid for activate",
        ),
        (
            '{ action = "activate", alternative_cost = "sometimes" }',
            "must be accept or decline",
        ),
    ],
)
def test_load_specs_rejects_invalid_alternative_cost_decision(
    tmp_path: Path, action: str, error: str
) -> None:
    spec = tmp_path / "invalid-alternative-cost.toml"
    spec.write_text(
        f"""
[[scenarios]]
name = "invalid alternative cost"
script = [{action}]

[scenarios.setup.P0]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=error):
        load_specs([spec])
