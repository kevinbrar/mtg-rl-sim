from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path

import pytest

from scripts.compare_card_scenarios import compare, normalize_projection, read_results
from scripts.run_card_scenarios import (
    check_changed_known_diff_entries,
    check_changed_scenario_suites,
    evaluate_known_diffs,
    format_known_diff_report,
    load_base_known_diffs,
    load_known_diffs,
    load_specs,
    main,
    normalize_label_insensitive_count_groups,
    forge_prompt_sha256,
    parse_forge_prompt_failures,
    parse_forge_report,
    parse_mage_report,
    projection_sha256,
    scenario_names_by_suite,
    scenario_paths_for_cards,
    split_scenarios_by_oracle,
)


def test_count_based_permanent_comparison_ignores_unreferenced_labels() -> None:
    scenario = {
        "name": "R2",
        "setup": {
            "P0": {
                "battlefield": [
                    {"card": "Swamp", "label": f"swamp-{index}"}
                    for index in range(1, 7)
                ]
            }
        },
        "script": [],
        "reach": {
            "all": [
                {"player": "P0", "card": "Swamp", "count": 4, "tapped": True},
                {"player": "P0", "card": "Swamp", "count": 2, "tapped": False},
            ]
        },
    }
    phase = {
        "scenario": "R2",
        "projection": {
            "battlefield": [
                {"label": f"swamp-{index}", "name": "Swamp", "tapped": index <= 4}
                for index in range(1, 7)
            ]
        },
    }
    mage = {
        "scenario": "R2",
        "projection": {
            "battlefield": [
                {
                    "label": f"swamp-{index}",
                    "name": "Swamp",
                    "tapped": index in {1, 2, 4, 6},
                }
                for index in range(1, 7)
            ]
        },
    }

    normalized_phase = normalize_label_insensitive_count_groups([scenario], [phase])
    normalized_mage = normalize_label_insensitive_count_groups([scenario], [mage])
    assert normalized_phase == normalized_mage
    assert projection_sha256(normalized_phase[0]) == projection_sha256(
        normalized_mage[0]
    )

    scenario["script"] = [
        {"action": "cast", "card": "Rottenmouth Viper", "sacrifice": ["swamp-3"]}
    ]
    referenced = normalize_label_insensitive_count_groups([scenario], [mage])
    assert any(
        permanent.get("label") == "swamp-3"
        for permanent in referenced[0]["projection"]["battlefield"]
    )

    scenario["script"] = []
    scenario["reach"]["all"][0]["tapped_labels"] = ["swamp-4"]
    referenced = normalize_label_insensitive_count_groups([scenario], [mage])
    assert any(
        permanent.get("label") == "swamp-4"
        for permanent in referenced[0]["projection"]["battlefield"]
    )


def test_count_based_permanent_comparison_keeps_tapped_count_mismatches() -> None:
    scenario = {
        "name": "R2",
        "setup": {
            "P0": {
                "battlefield": [
                    {"card": "Swamp", "label": f"swamp-{index}"}
                    for index in range(1, 7)
                ]
            }
        },
        "script": [],
        "reach": {
            "all": [
                {"player": "P0", "card": "Swamp", "count": 4, "tapped": True},
                {"player": "P0", "card": "Swamp", "count": 2, "tapped": False},
            ]
        },
    }
    phase = {
        "scenario": "R2",
        "projection": {
            "battlefield": [
                {"label": f"swamp-{index}", "name": "Swamp", "tapped": index <= 4}
                for index in range(1, 7)
            ]
        },
    }
    mage = copy.deepcopy(phase)
    for index, permanent in enumerate(mage["projection"]["battlefield"], start=1):
        permanent["tapped"] = index in {1, 2, 4}

    normalized_phase = normalize_label_insensitive_count_groups([scenario], [phase])
    normalized_mage = normalize_label_insensitive_count_groups([scenario], [mage])
    report = evaluate_known_diffs(
        {"test": ["R2"]}, normalized_phase, normalized_mage, [], []
    )
    assert len(report["failed"]) == 1


def test_count_group_normalization_requires_identical_setup_characteristics() -> None:
    scenario = {
        "name": "setup distinctions",
        "setup": {
            "P0": {
                "battlefield": [
                    {"label": "same-1", "card": "Swamp"},
                    {"label": "same-2", "card": "Swamp"},
                    {"label": "tapped", "card": "Swamp", "tapped": True},
                    {"label": "countered", "card": "Swamp", "counters": {"blight": 1}},
                    {"label": "attached", "card": "Swamp", "attached_to": "host"},
                ]
            }
        },
        "script": [],
        "reach": {
            "all": [
                {"player": "P0", "card": "Swamp", "count": 1, "tapped": True},
                {"player": "P0", "card": "Swamp", "count": 4, "tapped": False},
            ]
        },
    }
    result = {
        "scenario": "setup distinctions",
        "projection": {
            "battlefield": [
                {"label": label, "name": "Swamp", "tapped": tapped}
                for label, tapped in [
                    ("same-1", False),
                    ("same-2", False),
                    ("tapped", True),
                    ("countered", False),
                    ("attached", False),
                ]
            ]
        },
    }

    normalized = normalize_label_insensitive_count_groups([scenario], [result])
    labels = {
        permanent.get("label")
        for permanent in normalized[0]["projection"]["battlefield"]
    }
    group_tokens = {
        label
        for label in labels
        if isinstance(label, str) and label.startswith("~group:")
    }
    assert labels == {"tapped", "countered", "attached", *group_tokens}
    assert len(group_tokens) == 1


def test_count_group_normalization_preserves_distinct_setup_groups() -> None:
    scenario = {
        "name": "cross-group tapped swap",
        "setup": {
            "P0": {
                "battlefield": [
                    {"label": "tapped-1", "card": "Swamp", "tapped": True},
                    {"label": "tapped-2", "card": "Swamp", "tapped": True},
                    {"label": "untapped-1", "card": "Swamp", "tapped": False},
                    {"label": "untapped-2", "card": "Swamp", "tapped": False},
                ]
            }
        },
        "script": [],
        "reach": {
            "all": [
                {"player": "P0", "card": "Swamp", "count": 2, "tapped": True},
                {"player": "P0", "card": "Swamp", "count": 2, "tapped": False},
            ]
        },
    }
    phase = {
        "scenario": "cross-group tapped swap",
        "projection": {
            "battlefield": [
                {"label": "tapped-1", "name": "Swamp", "tapped": True},
                {"label": "tapped-2", "name": "Swamp", "tapped": True},
                {"label": "untapped-1", "name": "Swamp", "tapped": False},
                {"label": "untapped-2", "name": "Swamp", "tapped": False},
            ]
        },
        "reach": True,
    }
    mage = copy.deepcopy(phase)
    for permanent in mage["projection"]["battlefield"]:
        permanent["tapped"] = not permanent["tapped"]

    normalized_phase = normalize_label_insensitive_count_groups([scenario], [phase])[0]
    normalized_mage = normalize_label_insensitive_count_groups([scenario], [mage])[0]

    assert compare(
        {"cross-group tapped swap": normalized_phase},
        {"cross-group tapped swap": normalized_mage},
    ) == ["cross-group tapped swap"]
    assert projection_sha256(normalized_phase) != projection_sha256(normalized_mage)


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


def test_load_specs_rejects_setup_labels_with_reserved_at_prefix(
    tmp_path: Path,
) -> None:
    graveyard = tmp_path / "reserved-graveyard-label.toml"
    graveyard.write_text(
        """
[[scenarios]]
name = "reserved label"
[scenarios.setup.P0]
graveyard = [{card = "Walking Corpse", label = "@skip"}]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reserved @ prefix"):
        load_specs([graveyard])

    battlefield = tmp_path / "reserved-battlefield-label.toml"
    battlefield.write_text(
        """
[[scenarios]]
name = "reserved label"
[scenarios.setup.P0]
battlefield = [{card = "Walking Corpse", label = "@skip"}]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reserved @ prefix"):
        load_specs([battlefield])


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


def test_load_specs_rejects_unknown_reach_fields(tmp_path: Path) -> None:
    spec = tmp_path / "unknown-reach-field.toml"
    spec.write_text(
        """
[[scenarios]]
name = "unknown reach field"

[scenarios.setup.P0]

[scenarios.reach]
zone = "battlefield"
card = "Jace"
loyalty = 4
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"reach\.loyalty is unsupported"):
        load_specs([spec])


def test_load_specs_accepts_case_solved_reach_and_graveyard_cast(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "case-solved-graveyard-cast.toml"
    spec.write_text(
        """
[[scenarios]]
name = "case solved and graveyard cast"
script = [{ action = "cast", card = "graveyard-creature", from = "graveyard" }]

[scenarios.setup.P0]
graveyard = [{ card = "Silvercoat Lion", label = "graveyard-creature" }]

[scenarios.setup.P1]

[scenarios.reach]
zone = "battlefield"
card = "Case"
solved = false
""",
        encoding="utf-8",
    )

    loaded = load_specs([spec])

    assert loaded["scenarios"][0]["script"][0]["from"] == "graveyard"


def test_load_specs_rejects_solved_reach_outside_battlefield(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "case-solved-wrong-zone.toml"
    spec.write_text(
        """
[[scenarios]]
name = "case solved in wrong zone"

[scenarios.setup.P0]

[scenarios.reach]
card = "Case"
solved = false
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"reach\.solved is only valid"):
        load_specs([spec])


def test_load_specs_rejects_invalid_graveyard_cast_source(tmp_path: Path) -> None:
    spec = tmp_path / "invalid-graveyard-cast-source.toml"
    spec.write_text(
        """
[[scenarios]]
name = "invalid graveyard cast source"
script = [{ action = "cast", card = "Silvercoat Lion", from = "library" }]

[scenarios.setup.P0]

[scenarios.setup.P1]
""",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match=r"script\[0\]\.from must be hand or graveyard"
    ):
        load_specs([spec])


def test_load_specs_requires_graveyard_cast_label_to_be_in_graveyard(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "unlabeled-graveyard-cast.toml"
    spec.write_text(
        """
[[scenarios]]
name = "unlabeled graveyard cast"
script = [{ action = "cast", card = "Silvercoat Lion", from = "graveyard" }]

[scenarios.setup.P0]

[scenarios.setup.P1]
""",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match=r"script\[0\]\.card must be a graveyard setup label"
    ):
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


def test_load_specs_accepts_labeled_play_land_behold(tmp_path: Path) -> None:
    spec = tmp_path / "play-land-behold.toml"
    spec.write_text(
        """
[[scenarios]]
name = "play a land and behold"
script = [{ action = "play_land", card = "sanctum", behold = "jace" }]

[scenarios.setup.P0]
hand = [
  { card = "Theorist's Sanctum", label = "sanctum" },
  { card = "Jace Beleren", label = "jace" },
]
""",
        encoding="utf-8",
    )

    loaded = load_specs([spec])

    assert loaded["scenarios"][0]["script"][0] == {
        "action": "play_land",
        "card": "sanctum",
        "behold": "jace",
    }


@pytest.mark.parametrize(
    ("action", "message"),
    [
        ('{ action = "play_land", behold = "jace" }', ".card needs a hand label"),
        (
            '{ action = "play_land", card = "sanctum", behold = "" }',
            ".behold must be a label or decline",
        ),
        (
            '{ action = "cast", card = "spell", behold = "jace" }',
            "behold is only valid for play_land",
        ),
    ],
)
def test_load_specs_rejects_invalid_play_land_behold_fields(
    tmp_path: Path, action: str, message: str
) -> None:
    spec = tmp_path / "invalid-play-land.toml"
    spec.write_text(
        f"""
[[scenarios]]
name = "invalid play land"
script = [{action}]

[scenarios.setup.P0]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_specs([spec])


def test_load_specs_accepts_rottenmouth_viper_choice_scripts(tmp_path: Path) -> None:
    spec = tmp_path / "rottenmouth-choices.toml"
    spec.write_text(
        """
[[scenarios]]
name = "Viper choices"
script = [
  { action = "unless_choices", player = "P1", choices = [
    { sacrifice = "p1-fodder" }, { discard = "p1-card" }, "decline"
  ] },
  { action = "cast", card = "Rottenmouth Viper", sacrifice = ["p0-fodder"] }
]

[scenarios.setup.P0]
hand = ["Rottenmouth Viper"]
battlefield = [{ label = "p0-fodder", card = "Memnite" }]

[scenarios.setup.P1]
hand = [{ label = "p1-card", card = "Memnite" }]
battlefield = [{ label = "p1-fodder", card = "Memnite" }]
""",
        encoding="utf-8",
    )

    loaded = load_specs([spec])

    assert loaded["scenarios"][0]["script"][0]["choices"] == [
        {"sacrifice": "p1-fodder"},
        {"discard": "p1-card"},
        "decline",
    ]
    assert loaded["scenarios"][0]["script"][1]["sacrifice"] == ["p0-fodder"]


def test_load_specs_accepts_attack_and_copy_choice_scripts(tmp_path: Path) -> None:
    spec = tmp_path / "attack-copy-choices.toml"
    spec.write_text(
        """
[[scenarios]]
name = "attack copy choices"
script = [
  { action = "copy_choice", player = "P0", decision = "accept", new_targets = "keep" },
  { action = "attack", attackers = ["attacker"] }
]

[scenarios.setup.P0]
battlefield = [{ label = "attacker", card = "Pulse Tracker" }]

[scenarios.setup.P1]
battlefield = [{ label = "defender-a", card = "Centaur Courser" }]
""",
        encoding="utf-8",
    )

    loaded = load_specs([spec])

    assert loaded["scenarios"][0]["script"][0] == {
        "action": "copy_choice",
        "player": "P0",
        "decision": "accept",
        "new_targets": "keep",
    }
    assert loaded["scenarios"][0]["script"][1]["attackers"] == ["attacker"]


@pytest.mark.parametrize(
    ("action", "setup", "message"),
    [
        (
            '{ action = "attack", attackers = [] }',
            '[scenarios.setup.P0]\nbattlefield = [{ label = "attacker", card = "Pulse Tracker" }]',
            "non-empty label array",
        ),
        (
            '{ action = "attack", attackers = ["attacker", "attacker"] }',
            '[scenarios.setup.P0]\nbattlefield = [{ label = "attacker", card = "Pulse Tracker" }]',
            "cannot repeat labels",
        ),
        (
            '{ action = "attack", attackers = ["missing"] }',
            "[scenarios.setup.P0]",
            "unknown label",
        ),
        (
            '{ action = "attack", attackers = ["attacker"] }',
            '[scenarios.setup.P1]\nbattlefield = [{ label = "attacker", card = "Pulse Tracker" }]',
            "must be on P0's battlefield",
        ),
        (
            '{ action = "copy_choice", player = "P0", decision = "maybe" }',
            "[scenarios.setup.P0]",
            "decision must be accept or decline",
        ),
        (
            '{ action = "copy_choice", player = "P0", decision = "accept", new_targets = ["missing"] }',
            "[scenarios.setup.P0]",
            "unknown setup label",
        ),
        (
            '{ action = "copy_choice", player = "P0", decision = "decline", new_targets = ["target"] }',
            '[scenarios.setup.P1]\nbattlefield = [{ label = "target", card = "Centaur Courser" }]',
            "only valid when accepting",
        ),
    ],
)
def test_load_specs_rejects_invalid_attack_and_copy_choices(
    tmp_path: Path, action: str, setup: str, message: str
) -> None:
    spec = tmp_path / "invalid-attack-copy-choice.toml"
    spec.write_text(
        f'[[scenarios]]\nname = "invalid action"\nscript = [{action}]\n\n{setup}\n',
        encoding="utf-8",
    )

    with pytest.raises((TypeError, ValueError), match=message):
        load_specs([spec])


@pytest.mark.parametrize(
    ("script", "setup", "message"),
    [
        (
            '[{ action = "cast", card = "Rottenmouth Viper", sacrifice = "fodder" }]',
            '[scenarios.setup.P0]\nhand = ["Rottenmouth Viper"]\nbattlefield = [{ label = "fodder", card = "Memnite" }]',
            "must be an array",
        ),
        (
            '[{ action = "cast", card = "Rottenmouth Viper", sacrifice = ["unknown"] }]',
            '[scenarios.setup.P0]\nhand = ["Rottenmouth Viper"]',
            "unknown setup label",
        ),
        (
            '[{ action = "cast", card = "Rottenmouth Viper", sacrifice = ["hand-card"] }]',
            '[scenarios.setup.P0]\nhand = ["Rottenmouth Viper", { label = "hand-card", card = "Memnite" }]',
            "must be in the battlefield",
        ),
        (
            '[{ action = "cast", card = "Rottenmouth Viper", sacrifice = ["fodder", "fodder"] }]',
            '[scenarios.setup.P0]\nhand = ["Rottenmouth Viper"]\nbattlefield = [{ label = "fodder", card = "Memnite" }]',
            "cannot repeat labels",
        ),
    ],
)
def test_load_specs_rejects_invalid_cast_sacrifice_choices(
    tmp_path: Path, script: str, setup: str, message: str
) -> None:
    spec = tmp_path / "invalid-cast-sacrifice.toml"
    spec.write_text(
        f'[[scenarios]]\nname = "invalid cast sacrifice"\nscript = {script}\n\n{setup}\n',
        encoding="utf-8",
    )

    with pytest.raises((TypeError, ValueError), match=message):
        load_specs([spec])


@pytest.mark.parametrize(
    ("action", "setup", "message"),
    [
        (
            '{ action = "unless_choices", choices = ["decline"] }',
            "[scenarios.setup.P1]",
            "player must be P0 or P1",
        ),
        (
            '{ action = "unless_choices", player = "P1", choices = [] }',
            "[scenarios.setup.P1]",
            "non-empty array",
        ),
        (
            '{ action = "unless_choices", player = "P1", choices = [{ unknown = "card" }] }',
            '[scenarios.setup.P1]\nhand = [{ label = "card", card = "Memnite" }]',
            "must use sacrifice or discard",
        ),
        (
            '{ action = "unless_choices", player = "P1", choices = [{ sacrifice = "card", discard = "card" }] }',
            '[scenarios.setup.P1]\nbattlefield = [{ label = "card", card = "Memnite" }]',
            "exactly one",
        ),
        (
            '{ action = "unless_choices", player = "P1", choices = [{ sacrifice = "card" }] }',
            '[scenarios.setup.P0]\nbattlefield = [{ label = "card", card = "Memnite" }]',
            "must be in P1's battlefield",
        ),
        (
            '{ action = "unless_choices", player = "P1", choices = [{ discard = "missing" }] }',
            "[scenarios.setup.P1]",
            "unknown setup label",
        ),
        (
            '{ action = "unless_choices", player = "P1", choices = ["decline"], extra = true }',
            "[scenarios.setup.P1]",
            "extra is unsupported",
        ),
    ],
)
def test_load_specs_rejects_invalid_unless_choices(
    tmp_path: Path, action: str, setup: str, message: str
) -> None:
    spec = tmp_path / "invalid-unless-choices.toml"
    spec.write_text(
        f'[[scenarios]]\nname = "invalid unless choices"\nscript = [{action}]\n\n{setup}\n',
        encoding="utf-8",
    )

    with pytest.raises((TypeError, ValueError), match=message):
        load_specs([spec])


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


def test_load_specs_accepts_exile_setup(tmp_path: Path) -> None:
    spec = tmp_path / "exile.toml"
    spec.write_text(
        """
[[scenarios]]
name = "exile setup"

[scenarios.setup.P0]
exile = ["Walking Corpse"]
""",
        encoding="utf-8",
    )

    loaded = load_specs([spec])
    assert loaded["scenarios"][0]["setup"]["P0"]["exile"] == ["Walking Corpse"]


def test_load_specs_rejects_unrepresentable_setup_fields(tmp_path: Path) -> None:
    spec = tmp_path / "unsupported.toml"
    spec.write_text(
        """
[[scenarios]]
name = "unsupported setup"

[scenarios.setup.P0]
command = ["Walking Corpse"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="setup.P0.command is unsupported"):
        load_specs([spec])


def test_load_specs_rejects_non_integer_setup_life(tmp_path: Path) -> None:
    spec = tmp_path / "invalid-life.toml"
    spec.write_text(
        """
[[scenarios]]
name = "invalid life"

[scenarios.setup.P0]
life = 20.5
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="setup.P0.life must be a 32-bit integer"):
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


def test_parse_forge_report_extracts_projection_records() -> None:
    report = """
<testsuite>
  <testcase>
    <system-out><![CDATA[
CARD_SCENARIO_RESULT:{"scenario":"E1","projection":{"reach":true},"reach":true}
]]></system-out>
  </testcase>
</testsuite>
"""
    assert parse_forge_report(report) == [
        {"scenario": "E1", "projection": {"reach": True}, "reach": True}
    ]


def test_known_diff_report_names_each_suite_oracle() -> None:
    report = {
        "counts": {"matched": 5, "triaged": 1, "failed": 0},
        "matched": [],
        "triaged": [
            {
                "category": "engine-bug",
                "card": "Extrapolate the Impossible",
                "suite": "extrapolate-the-impossible",
                "scenario": "E1",
                "phase_sha256": "phase",
                "mage_sha256": "forge",
            }
        ],
        "failed": [],
        "oracles_by_suite": {"extrapolate-the-impossible": "forge"},
    }

    formatted = format_known_diff_report(report)
    assert 'ORACLES {"extrapolate-the-impossible": "forge"}' in formatted
    assert "forge_sha256=forge" in formatted


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


def test_projection_normalizes_names_before_unordered_zone_sorting() -> None:
    first = _projection_result(
        "S1",
        {
            "battlefield": [
                {"name": "Kíli", "attached_to": "bear", "label": "equipment"},
                {"name": "Kozilek", "attached_to": "bear", "label": "equipment"},
            ],
            "exile": ["Kíli", "Kozilek"],
        },
    )
    second = _projection_result(
        "S1",
        {
            "battlefield": [
                {"name": "Kozilek", "attached_to": "bear", "label": "equipment"},
                {"name": "Kili", "attached_to": "bear", "label": "equipment"},
            ],
            "exile": ["Kozilek", "Kili"],
        },
    )

    assert normalize_projection(first["projection"]) == normalize_projection(
        second["projection"]
    )
    assert projection_sha256(first) == projection_sha256(second)


@pytest.mark.parametrize("field", ("attached_to", "label"))
def test_projection_preserves_exact_attachment_labels(field: str) -> None:
    left_permanent = {
        "name": "Kíli",
        "attached_to": "bear",
        "label": "bear",
    }
    right_permanent = copy.deepcopy(left_permanent)
    right_permanent["name"] = "Kili"
    right_permanent[field] = "béar"
    left = _projection_result("S1", {"battlefield": [left_permanent]})
    right = _projection_result("S1", {"battlefield": [right_permanent]})

    assert compare({"S1": left}, {"S1": right}) == ["S1"]


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


def test_forge_bug_triage_is_digest_pinned_and_never_matched() -> None:
    phase = _projection_result("E5", {"hand": [], "sideboard": ["Giant Growth"]})
    event = {"scenario": "E5", "prompt": "outside_game"}
    entry = _known_diff_entry("E5", phase, phase, category="forge-bug")
    entry.pop("mage_sha256")
    entry["forge_sha256"] = forge_prompt_sha256(event)

    report = evaluate_known_diffs(
        {"suite": ["E5"]},
        [phase],
        [],
        [entry],
        forge_prompt_failures=[event],
        suite_oracles={"suite": "forge"},
    )

    assert report["counts"] == {"matched": 0, "triaged": 1, "failed": 0}
    assert report["triaged"][0]["category"] == "forge-bug"
    assert report["triaged"][0]["forge_sha256"] == entry["forge_sha256"]

    report_without_event = evaluate_known_diffs(
        {"suite": ["E5"]},
        [phase],
        [],
        [entry],
        suite_oracles={"suite": "forge"},
    )
    assert report_without_event["counts"]["triaged"] == 0
    assert report_without_event["counts"]["failed"] > 0


def test_parse_forge_prompt_failure_report() -> None:
    event = {"scenario": "E5", "prompt": "outside_game"}
    report = (
        "<testsuite><testcase><system-out>"
        "CARD_SCENARIO_PROMPT_FAILURE:"
        + json.dumps(event)
        + "</system-out></testcase></testsuite>"
    )

    assert parse_forge_prompt_failures(report) == [event]


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


def test_missing_oracle_results_are_reported_without_assuming_xmage() -> None:
    phase = _projection_result("E1", {"life": 20})

    report = evaluate_known_diffs({"forge-suite": ["E1"]}, [phase], [], [])

    assert any(
        failure["scenario"] == "<oracle>"
        and "results are missing or unexpected" in failure["reason"]
        for failure in report["failed"]
    )


def test_mixed_oracle_results_pair_by_suite_and_scenario_not_order() -> None:
    forge = _projection_result("F1", {"life": 20})
    xmage = _projection_result("X1", {"life": 20})

    report = evaluate_known_diffs(
        {"forge-suite": ["F1"], "xmage-suite": ["X1"]},
        [forge, xmage],
        [xmage, forge],
        [],
        suite_oracles={"forge-suite": "forge", "xmage-suite": "xmage"},
    )

    assert report["counts"] == {"matched": 2, "triaged": 0, "failed": 0}


def test_mixed_oracle_results_reject_duplicate_and_missing_pairs() -> None:
    forge = _projection_result("F1", {"life": 20})
    xmage = _projection_result("X1", {"life": 20})

    report = evaluate_known_diffs(
        {"forge-suite": ["F1"], "xmage-suite": ["X1"]},
        [forge, xmage],
        [xmage, xmage],
        [],
        suite_oracles={"forge-suite": "forge", "xmage-suite": "xmage"},
    )

    assert any(
        "duplicate scenario X1 in suite xmage-suite" in failure["reason"]
        for failure in report["failed"]
    )
    assert any(failure["scenario"] == "<oracle>" for failure in report["failed"])


def test_load_specs_accepts_mana_pool_with_a_labeled_battlefield_source(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "mana-pool.toml"
    spec.write_text(
        """
[[scenarios]]
name = "cast with seeded mana"

[scenarios.setup.P0]
battlefield = [{ label = "island", card = "Island", tapped = true }]
mana_pool = [{ color = "U", source = "island" }]

[scenarios.setup.P1]

[[scenarios.script]]
action = "choose_mana"
player = "P0"
sources = ["island"]

[[scenarios.script]]
action = "choose_outside_game"
player = "P0"
accept = true
cards = []
""",
        encoding="utf-8",
    )

    loaded = load_specs([spec])

    assert loaded["scenarios"][0]["setup"]["P0"]["mana_pool"] == [
        {"color": "U", "source": "island"}
    ]
    assert loaded["scenarios"][0]["script"][0]["sources"] == ["island"]
    assert loaded["scenarios"][0]["script"][1]["accept"] is True


def test_load_specs_accepts_oracle_specific_mana_sources(tmp_path: Path) -> None:
    spec = tmp_path / "oracle-specific-mana.toml"
    spec.write_text(
        """
[[scenarios]]
name = "oracle-specific mana sources"

[scenarios.setup.P0]
battlefield = [
  { label = "island.1", card = "Island" },
  { label = "island.2", card = "Island" },
]
mana_pool = [
  { color = "U", source = "island.1" },
  { color = "U", source = "island.2" },
]

[scenarios.setup.P1]

[[scenarios.script]]
action = "choose_mana"
player = "P0"
phase_sources = ["island.1"]
forge_sources = ["island.2"]
""",
        encoding="utf-8",
    )

    loaded = load_specs([spec])

    assert loaded["scenarios"][0]["script"][0]["phase_sources"] == ["island.1"]
    assert loaded["scenarios"][0]["script"][0]["forge_sources"] == ["island.2"]
    spec.write_text(
        spec.read_text(encoding="utf-8").replace('forge_sources = ["island.2"]\n', ""),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="both phase_sources and forge_sources"):
        load_specs([spec])


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


def test_forge_suite_dispatch_and_xmage_default(tmp_path: Path) -> None:
    xmage = tmp_path / "xmage-card.toml"
    xmage.write_text(
        '[[scenarios]]\nname = "default-suite"\n[scenarios.setup.P0]\n',
        encoding="utf-8",
    )
    forge = tmp_path / "forge-card.toml"
    forge.write_text(
        'oracle = "forge"\n[[scenarios]]\nname = "forge-suite"\n[scenarios.setup.P0]\n',
        encoding="utf-8",
    )

    loaded = load_specs([xmage, forge])
    suites = scenario_names_by_suite([xmage, forge])
    dispatched = split_scenarios_by_oracle(
        loaded["scenarios"], suites, loaded["suite_oracles"]
    )

    assert loaded["suite_oracles"] == {"xmage-card": "xmage", "forge-card": "forge"}
    assert [scenario["name"] for scenario in dispatched["xmage"]] == ["default-suite"]
    assert [scenario["name"] for scenario in dispatched["forge"]] == ["forge-suite"]


def test_load_specs_accepts_nonactive_priority_and_jace_scripts(tmp_path: Path) -> None:
    spec = tmp_path / "jace-scripts.toml"
    spec.write_text(
        """
oracle = "forge"
[[scenarios]]
name = "nonactive Jace script"
active_player = 1
priority_player = 1
script = [
  { action = "pass_priority", player = "P1" },
  { action = "cast", player = "P0", card = "Jace's Machinations" },
  { action = "label_permanent", player = "P0", card = "Jace", label = "jace-token", token = true },
  { action = "activate", source = "jace-token", ability = "−1: Surveil 1" },
  { action = "choose_surveil", player = "P0", cards = ["top-card"] },
  { action = "choose_replacement", player = "P0", description = "Jace Token enters with zero loyalty counters on it." },
  { action = "choose_entity", player = "P0", card = "Jace Token" }
]

[scenarios.setup.P0]
hand = ["Jace's Machinations"]
library_top = [{ label = "top-card", card = "Island" }]

[scenarios.setup.P1]
""",
        encoding="utf-8",
    )

    loaded = load_specs([spec])

    assert loaded["scenarios"][0]["priority_player"] == 1
    assert loaded["scenarios"][0]["script"][4]["cards"] == ["top-card"]
    assert loaded["scenarios"][0]["script"][5]["description"] == (
        "Jace Token enters with zero loyalty counters on it."
    )
    assert loaded["scenarios"][0]["script"][6]["card"] == "Jace Token"


def test_load_specs_rejects_invalid_replacement_choice(tmp_path: Path) -> None:
    spec = tmp_path / "invalid-replacement-choice.toml"
    spec.write_text(
        """
[[scenarios]]
name = "invalid replacement choice"
script = [{ action = "choose_replacement", player = "P0", description = "" }]
[scenarios.setup.P0]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="description must be a non-empty string"):
        load_specs([spec])


def test_load_specs_rejects_invalid_single_entity_choice(tmp_path: Path) -> None:
    spec = tmp_path / "invalid-single-entity-choice.toml"
    spec.write_text(
        """
[[scenarios]]
name = "invalid single entity choice"
script = [{ action = "choose_entity", player = "P0", card = "" }]
[scenarios.setup.P0]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="card must be a non-empty string"):
        load_specs([spec])


def test_load_specs_rejects_invalid_nonactive_priority_and_surveil_choice(
    tmp_path: Path,
) -> None:
    invalid_priority = tmp_path / "invalid-priority.toml"
    invalid_priority.write_text(
        """
[[scenarios]]
name = "invalid priority"
priority_player = 2
[scenarios.setup.P0]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="priority_player must be 0 or 1"):
        load_specs([invalid_priority])

    invalid_surveil = tmp_path / "invalid-surveil.toml"
    invalid_surveil.write_text(
        """
[[scenarios]]
name = "invalid surveil label"
script = [{ action = "choose_surveil", player = "P0", cards = ["not-top"] }]
[scenarios.setup.P0]
library_top = [{ label = "top", card = "Island" }]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be in P0's library_top"):
        load_specs([invalid_surveil])


def test_choose_outside_game_requires_sideboard_labels(tmp_path: Path) -> None:
    valid = tmp_path / "outside-game.toml"
    valid.write_text(
        """
oracle = "forge"
[[scenarios]]
name = "outside game"
[scenarios.setup.P0]
sideboard = [{ label = "outside.card", card = "Giant Growth" }]
[[scenarios.script]]
action = "choose_outside_game"
player = "P1"
accept = true
cards = ["outside.card"]
[[scenarios.script]]
action = "choose_outside_game"
player = "P0"
accept = false
cards = []
""",
        encoding="utf-8",
    )
    assert load_specs([valid])["suite_oracles"] == {"outside-game": "forge"}

    invalid = tmp_path / "invalid-outside-game.toml"
    invalid.write_text(
        """
[[scenarios]]
name = "outside game"
[scenarios.setup.P0]
hand = [{ label = "hand.card", card = "Giant Growth" }]
[[scenarios.script]]
action = "choose_outside_game"
player = "P1"
accept = true
cards = ["hand.card"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be in a sideboard"):
        load_specs([invalid])


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
