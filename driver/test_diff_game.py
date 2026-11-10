import json
import random
from pathlib import Path

import pytest

from diff_game import (
    _legal_action_diff,
    _matching_trivial_priority_stop,
    _phase_activation_set,
    _phase_hand_card,
    _phase_target_key,
    _xmage_hand_choice,
    _xmage_target_choice,
    _scry_legal_choices,
    _scry_selection_payload,
    _xmage_activation_choice,
    _xmage_activation_set,
    _xmage_target_key,
    _policy_candidate_v0,
    policy_candidate,
    summary_exit_code,
    UnmappedPromptError,
)

SCRY_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "boros-dimir-seed-17-scry.json"
)
DUPLICATE_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "boros-dimir-seed-17-duplicate-choices.json"
)
HAND_DUPLICATE_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "mono-green-v0-seed-17-duplicate-forests.json"
)
DISCARD_HAND_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "mono-green-v0-seed-42-discard-hand.json"
)
ABILITY_ZONE_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "boros-dimir-seed-42-ability-zone.json"
)


@pytest.mark.parametrize(
    "status",
    ("completed", "completed_terminal_projection_unavailable"),
)
def test_terminal_summary_with_matching_winner_exits_successfully(status: str) -> None:
    summary = {
        "status": status,
        "divergences": 0,
        "winner": {"phase": 1, "xmage": 1},
    }

    assert summary_exit_code(summary) == 0


def test_unmapped_prompt_summary_exits_with_distinct_status() -> None:
    summary = {
        "status": "unmapped_prompt",
        "divergences": 0,
        "winner": {"phase": None, "xmage": None},
    }

    assert summary_exit_code(summary) == 2


def test_scry_prompt_maps_phase_library_choice_to_xmage_semantically() -> None:
    fixture = json.loads(SCRY_FIXTURE.read_text())
    decision = fixture["prompts"]["phase"]
    xmage = fixture["prompts"]["xmage"]

    phase_choices, xmage_choices = _scry_legal_choices(decision, xmage)
    assert phase_choices == xmage_choices
    assert _legal_action_diff(decision, xmage) == {}

    bottom = next(
        candidate
        for candidate in decision["candidates"]
        if candidate.get("data", {}).get("cards")
    )
    assert _scry_selection_payload(bottom, decision, xmage) == {"choice": "p27"}

    keep = next(
        candidate
        for candidate in decision["candidates"]
        if candidate.get("data", {}).get("cards") == []
    )
    assert ("keep",) in phase_choices
    assert _scry_selection_payload(keep, decision, xmage) == {"choice": "no"}


def test_scry_legal_set_difference_is_not_normalized_away() -> None:
    fixture = json.loads(SCRY_FIXTURE.read_text())
    decision = fixture["prompts"]["phase"]
    xmage = fixture["prompts"]["xmage"]
    xmage["choices"][0]["name"] = "Different Card"

    assert _legal_action_diff(decision, xmage)


def test_scry_waiting_player_mismatch_is_a_divergence() -> None:
    fixture = json.loads(SCRY_FIXTURE.read_text())
    decision = fixture["prompts"]["phase"]
    decision["owner"] = 0
    xmage = fixture["prompts"]["xmage"]

    assert _legal_action_diff(decision, xmage)


def test_trivial_priority_stop_does_not_pass_activated_ability_prompt() -> None:
    decision = {
        "owner": 0,
        "state": {"turn": 3, "active_player": 0, "phase": "PreCombatMain"},
        "candidates": [{"type": "PassPriority"}, {"type": "ActivateAbility"}],
    }
    xmage = {
        "action_pending": True,
        "action_type": "GAME_SELECT",
        "response_type": "select",
        "context": "T2 Precombat Main/Precombat Main (DiffA) YOUR_MAIN",
        "board": [{"name": "DiffA", "is_active": True, "is_you": True}],
        "choices": [{"name": "Basilisk Collar", "action": "activate"}],
    }
    skipped_stops = [
        {
            "kind": "priority",
            "turn": 3,
            "active_player": "DiffA",
            "step": "precombatmain",
        }
    ]

    assert _matching_trivial_priority_stop(xmage, decision, skipped_stops) is None


def test_duplicate_activation_matches_are_unmapped_with_both_choice_lists() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    prompt = fixture["prompts"]["activation"]
    decision = prompt["phase"]
    xmage = prompt["xmage"]

    assert _phase_activation_set(decision) == _xmage_activation_set(xmage)
    assert _legal_action_diff(decision, xmage) == {}
    assert fixture["source_run"]["phase_candidates"] == [
        candidate
        for candidate in decision["candidates"]
        if candidate["type"] == "ActivateAbility"
    ]
    action = next(
        candidate
        for candidate in decision["candidates"]
        if candidate["type"] == "ActivateAbility"
        and candidate["data"]["source_id"] == 45
    )
    with pytest.raises(UnmappedPromptError) as error:
        _xmage_activation_choice(action, decision, xmage)
    assert "phase_choices=" in str(error.value)
    assert "xmage_choices=" in str(error.value)
    assert "Basilisk Collar" in str(error.value)
    xmage["choices"][0]["playable_abilities"] = ["Equip {1}"]
    assert _legal_action_diff(decision, xmage)


def test_activation_with_multiple_playable_abilities_is_unmapped() -> None:
    action = {
        "type": "ActivateAbility",
        "data": {"ability_index": 0, "source_id": 27},
    }
    decision = {
        "waiting_for": {"type": "Priority"},
        "objects": {
            "27": {
                "name": "Basilisk Collar",
                "controller": 0,
                "zone": "Battlefield",
                "abilities": [{"index": 0, "text": "Equip {2}"}],
            }
        },
        "candidates": [action],
    }
    xmage = {
        "action_type": "GAME_SELECT",
        "board": [
            {
                "name": "DiffA",
                "is_you": True,
                "battlefield": [{"id": "p3", "name": "Basilisk Collar"}],
            },
            {"name": "DiffB", "is_you": False, "battlefield": []},
        ],
        "choices": [
            {
                "id": "p3",
                "name": "Basilisk Collar",
                "action": "activate",
                "playable_abilities": [
                    "Equip {2} <i>({2}: Attach to target creature you control)</i>",
                    "Other ability",
                ],
            }
        ],
    }

    with pytest.raises(UnmappedPromptError, match="requires an ability choice"):
        _xmage_activation_choice(action, decision, xmage)


def test_duplicate_hand_land_choices_are_fungible() -> None:
    fixture = json.loads(HAND_DUPLICATE_FIXTURE.read_text())
    decision = fixture["prompts"]["phase"]
    xmage = fixture["prompts"]["xmage"]
    action = next(
        candidate
        for candidate in decision["candidates"]
        if candidate["type"] == "PlayLand" and candidate["data"]["object_id"] == 75
    )
    card, owner = _phase_hand_card(action["data"]["object_id"], decision)

    choice = _xmage_hand_choice(card["name"], owner, "land", decision, xmage)

    assert fixture["source_artifact"] == "runs/20261110T042903165466Z/divergence.json"
    assert owner == 1
    assert len(xmage["choices"]) == 2
    assert choice == xmage["choices"][0]


def test_duplicate_hand_select_choices_are_fungible() -> None:
    fixture = json.loads(DISCARD_HAND_FIXTURE.read_text())
    decision = fixture["prompts"]["phase"]
    xmage = fixture["prompts"]["xmage"]
    action = next(
        candidate
        for candidate in decision["candidates"]
        if candidate["type"] == "SelectCards" and candidate["data"]["cards"] == [67]
    )
    card, owner = _phase_hand_card(action["data"]["cards"][0], decision)

    assert _legal_action_diff(decision, xmage) == {}
    choice = _xmage_hand_choice(card["name"], owner, None, decision, xmage)

    assert fixture["source_artifact"] == "runs/20261110T043821774195Z/divergence.json"
    assert choice == xmage["choices"][1]
    assert (
        len(
            [
                candidate
                for candidate in xmage["choices"]
                if candidate["name"] == "Forest"
            ]
        )
        == 2
    )


def test_activation_key_uses_the_choice_source_zone() -> None:
    fixture = json.loads(ABILITY_ZONE_FIXTURE.read_text())
    decision = fixture["prompts"]["phase"]
    xmage = fixture["prompts"]["xmage"]

    assert fixture["source_artifact"] == "runs/20261110T044740750318Z/divergence.json"
    assert _legal_action_diff(decision, xmage) == {}


def test_target_maps_by_card_controller_and_zone() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    prompt = fixture["prompts"]["target"]
    decision = prompt["phase"]
    xmage = prompt["xmage"]

    assert _legal_action_diff(decision, xmage) == {}
    assert [choice["id"] for choice in xmage["choices"]] == [
        permanent["id"]
        for permanent in fixture["source_run"]["xmage_permanents"]
        if permanent["name"] == "Dwarven Mauler"
    ]
    with pytest.raises(UnmappedPromptError) as error:
        _xmage_target_choice(decision["candidates"][0], decision, xmage)
    assert "phase_choices=" in str(error.value)
    assert "xmage_choices=" in str(error.value)
    assert "Dwarven Mauler" in str(error.value)


def test_xmage_target_controller_comes_from_choice_source_id() -> None:
    result = {
        "board": [
            {
                "name": "DiffA",
                "battlefield": [{"id": "p3", "name": "Dwarven Mauler"}],
            },
            {
                "name": "DiffB",
                "battlefield": [{"id": "p4", "name": "Dwarven Mauler"}],
            },
        ],
        "choices": [
            {"id": "p3", "name": "Dwarven Mauler"},
            {"id": "p4", "name": "Dwarven Mauler"},
        ],
    }

    assert _xmage_target_key(
        {"id": "p4", "name": "Dwarven Mauler", "target_type": "permanent"},
        result,
    ) == ("permanent", "dwarven mauler", 1, "battlefield")
    with pytest.raises(UnmappedPromptError):
        _xmage_target_key(
            {"id": "missing", "name": "Dwarven Mauler", "target_type": "permanent"},
            result,
        )


def test_choose_player_target_uses_the_phase_player_name() -> None:
    action = {"type": "ChooseTarget", "data": {"target": {"Player": 1}}}

    assert _phase_target_key(action, {}) == ("player", "diffb")


def test_select_targets_maps_one_target_through_the_same_keying() -> None:
    action = {"type": "SelectTargets", "data": {"targets": [{"Player": 1}]}}

    assert _phase_target_key(action, {}) == ("player", "diffb")


@pytest.mark.parametrize(
    "action",
    (
        {"type": "SelectTargets", "data": {"targets": []}},
        {
            "type": "SelectTargets",
            "data": {"targets": [{"Player": 0}, {"Player": 1}]},
        },
        {"type": "SelectTargets", "data": {"targets": [{"Other": 1}]}},
        {"type": "ChooseTarget", "data": {"target": {"Other": 1}}},
        {"type": "ChooseTarget", "data": {"target": {"Object": 99}}},
    ),
)
def test_unmapped_target_shapes_fail_closed(action: dict) -> None:
    decision = {
        "objects": {
            "3": {"name": "Dwarven Mauler", "controller": 0, "zone": "Battlefield"}
        }
    }

    with pytest.raises(UnmappedPromptError):
        _phase_target_key(action, decision)


def test_v0_policy_keeps_existing_choice_stream() -> None:
    decision = {
        "candidates": [
            {"type": "CastSpell", "data": {}},
            {"type": "PassPriority", "data": {}},
        ]
    }
    rng = random.Random(42)

    assert policy_candidate(decision, rng, v0=True) == _policy_candidate_v0(
        decision, random.Random(42)
    )


def test_v1_policy_is_seeded_and_biases_casts_and_activations() -> None:
    decision = {
        "candidates": [
            {"type": "CastSpell"},
            {"type": "ActivateAbility"},
            {"type": "PlayLand"},
            {"type": "PassPriority"},
        ]
    }

    def choices(seed: int) -> list[dict]:
        rng = random.Random(seed)
        return [policy_candidate(decision, rng)[1] for _ in range(1000)]

    first = choices(17)
    assert first == choices(17)
    preferred = sum(
        action["type"] in {"CastSpell", "ActivateAbility"} for action in first
    )
    assert 750 <= preferred <= 850


@pytest.mark.parametrize(
    "summary",
    (
        {
            "status": "divergence",
            "divergences": 1,
            "winner": {"phase": 1, "xmage": 0},
        },
        {
            "status": "turn_cap",
            "divergences": 0,
            "winner": {"phase": None, "xmage": None},
        },
        {
            "status": "completed",
            "divergences": 1,
            "winner": {"phase": 1, "xmage": 1},
        },
        {
            "status": "completed_terminal_projection_unavailable",
            "divergences": 0,
            "winner": {"phase": 1, "xmage": 0},
        },
        {"status": "completed", "winner": {"phase": 1, "xmage": 1}},
    ),
)
def test_summary_without_all_success_conditions_exits_nonzero(
    summary: dict,
) -> None:
    assert summary_exit_code(summary) != 0
