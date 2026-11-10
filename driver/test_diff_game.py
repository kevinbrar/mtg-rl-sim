import json
import random
from pathlib import Path

import pytest

from diff_game import (
    ObjectBindings,
    _legal_action_diff,
    _matching_trivial_priority_stop,
    _phase_activation_set,
    _phase_hand_card,
    _phase_target_key,
    _xmage_hand_choice,
    _scry_legal_choices,
    _scry_selection_payload,
    _xmage_activation_choice,
    _xmage_activation_set,
    _xmage_target_key,
    _xmage_target_choice,
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


def _signature_fields(decision: dict) -> None:
    battlefield = []
    for object_id, obj in decision.get("objects", {}).items():
        if obj.get("zone") != "Battlefield":
            continue
        obj.setdefault("incarnation", 0)
        obj.setdefault("tapped", False)
        obj.setdefault("damage", 0)
        obj.setdefault("attached_to", None)
        if "Dwarven Mauler" in obj.get("name", ""):
            obj.setdefault("power", 3)
            obj.setdefault("toughness", 2)
            obj.setdefault("summoning_sick", False)
        battlefield.append(
            {
                "id": int(object_id),
                "incarnation": obj["incarnation"],
                "name": obj["name"],
                "controller": obj["controller"],
            }
        )
    decision.setdefault("state", {})["battlefield"] = battlefield


def _xmage_signature_state(battlefields: dict[str, list[dict]]) -> dict:
    return {
        "available": True,
        "players": [
            {"name": name, "battlefield": permanents}
            for name, permanents in battlefields.items()
        ],
    }


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


def test_duplicate_battlefield_collars_bind_and_reuse_ids() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    decision = fixture["prompts"]["activation"]["phase"]
    xmage = fixture["prompts"]["activation"]["xmage"]
    _signature_fields(decision)
    xmage_state = _xmage_signature_state(
        {
            "DiffA": [
                {
                    "id": "p3",
                    "name": "Basilisk Collar",
                    "tapped": False,
                    "damage": 0,
                },
                {
                    "id": "p4",
                    "name": "Basilisk Collar",
                    "tapped": False,
                    "damage": 0,
                },
            ],
            "DiffB": [],
        }
    )
    bindings = ObjectBindings()

    def activation(phase_id: int) -> dict:
        return next(
            action
            for action in decision["candidates"]
            if action.get("type") == "ActivateAbility"
            and action["data"]["source_id"] == phase_id
        )

    assert (
        _xmage_activation_choice(
            activation(27), decision, xmage, bindings, xmage_state
        )["id"]
        == "p3"
    )
    assert bindings.phase_to_xmage == {(27, 0): "p3"}
    assert (
        _xmage_activation_choice(
            activation(45), decision, xmage, bindings, xmage_state
        )["id"]
        == "p4"
    )
    assert (
        _xmage_activation_choice(
            activation(27), decision, xmage, bindings, xmage_state
        )["id"]
        == "p3"
    )


def test_unbound_attachment_hosts_use_full_signature() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    decision = fixture["prompts"]["activation"]["phase"]
    xmage = fixture["prompts"]["activation"]["xmage"]
    for host_id, tapped in (("2", False), ("23", True)):
        decision["objects"][host_id] = {
            "name": "Dwarven Mauler",
            "zone": "Battlefield",
            "controller": 0,
            "incarnation": 0,
            "tapped": tapped,
            "power": 3,
            "toughness": 2,
            "damage": 0,
            "summoning_sick": False,
            "attached_to": None,
        }
    _signature_fields(decision)
    decision["objects"]["27"]["attached_to"] = 2
    decision["objects"]["45"]["attached_to"] = 23
    xmage_state = _xmage_signature_state(
        {
            "DiffA": [
                {
                    "id": "p3",
                    "name": "Basilisk Collar",
                    "tapped": False,
                    "damage": 0,
                    "attached_to": "p5",
                },
                {
                    "id": "p4",
                    "name": "Basilisk Collar",
                    "tapped": False,
                    "damage": 0,
                    "attached_to": "p28",
                },
                {
                    "id": "p5",
                    "name": "Dwarven Mauler",
                    "tapped": True,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                },
                {
                    "id": "p28",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                },
            ],
            "DiffB": [],
        }
    )
    bindings = ObjectBindings()
    action = next(
        item
        for item in decision["candidates"]
        if item.get("type") == "ActivateAbility" and item["data"]["source_id"] == 27
    )

    assert (
        _xmage_activation_choice(action, decision, xmage, bindings, xmage_state)["id"]
        == "p4"
    )


def test_tapped_mauler_target_matches_by_signature() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    decision = fixture["prompts"]["target"]["phase"]
    xmage = fixture["prompts"]["target"]["xmage"]
    _signature_fields(decision)
    decision["objects"]["2"]["tapped"] = True
    decision["objects"]["23"]["tapped"] = False
    decision["objects"]["25"]["tapped"] = False
    xmage_state = _xmage_signature_state(
        {
            "DiffA": [
                {
                    "id": "p5",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
                {
                    "id": "p28",
                    "name": "Dwarven Mauler",
                    "tapped": True,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
                {
                    "id": "p43",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
            ],
            "DiffB": [],
        }
    )
    bindings = ObjectBindings()
    action = next(
        item
        for item in decision["candidates"]
        if item.get("data", {}).get("target", {}).get("Object") == 2
    )

    assert (
        _xmage_target_choice(action, decision, xmage, bindings, xmage_state)["id"]
        == "p28"
    )


def test_no_matching_battlefield_signature_reports_both_sides() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    decision = fixture["prompts"]["target"]["phase"]
    xmage = fixture["prompts"]["target"]["xmage"]
    _signature_fields(decision)
    decision["objects"]["2"]["counters"] = {"P1P1": 1}
    xmage_state = _xmage_signature_state(
        {
            "DiffA": [
                {
                    "id": choice_id,
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                    "counters": {"+1/+1": 2},
                }
                for choice_id in ("p5", "p28", "p43")
            ],
            "DiffB": [],
        }
    )
    action = next(
        item
        for item in decision["candidates"]
        if item.get("data", {}).get("target", {}).get("Object") == 2
    )

    with pytest.raises(UnmappedPromptError) as error:
        _xmage_target_choice(action, decision, xmage, ObjectBindings(), xmage_state)
    assert "phase_signatures=" in str(error.value)
    assert "xmage_signatures=" in str(error.value)


def test_bound_battlefield_id_must_remain_a_matching_choice() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    decision = fixture["prompts"]["target"]["phase"]
    xmage = fixture["prompts"]["target"]["xmage"]
    _signature_fields(decision)
    xmage["choices"] = [xmage["choices"][0]]
    xmage_state = _xmage_signature_state(
        {
            "DiffA": [
                {
                    "id": "p5",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
                {
                    "id": "p28",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
            ],
            "DiffB": [],
        }
    )
    bindings = ObjectBindings()
    bindings.bind(2, 0, "p28")
    action = next(
        item
        for item in decision["candidates"]
        if item.get("data", {}).get("target", {}).get("Object") == 2
    )

    with pytest.raises(UnmappedPromptError, match="not among matching target choices"):
        _xmage_target_choice(action, decision, xmage, bindings, xmage_state)


def test_leaving_battlefield_prunes_binding_before_rebinding() -> None:
    bindings = ObjectBindings()
    bindings.bind(2, 0, "p5")
    decision = {
        "waiting_for": {"type": "TargetSelection"},
        "state": {
            "battlefield": [{"id": 99, "incarnation": 0, "name": "Dwarven Mauler"}]
        },
        "objects": {
            "99": {
                "name": "Dwarven Mauler",
                "controller": 0,
                "zone": "Battlefield",
                "incarnation": 0,
                "tapped": False,
                "power": 3,
                "toughness": 2,
                "damage": 0,
                "summoning_sick": False,
                "attached_to": None,
            }
        },
        "candidates": [
            {
                "type": "ChooseTarget",
                "data": {"target": {"Object": 99}},
            }
        ],
    }
    xmage_state = _xmage_signature_state(
        {
            "DiffA": [
                {
                    "id": "p5",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
                {
                    "id": "p6",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
            ],
            "DiffB": [],
        }
    )
    bindings.prune(decision["state"], xmage_state)
    xmage = {
        "action_type": "GAME_TARGET",
        "choices": [
            {
                "id": "p6",
                "name": "Dwarven Mauler",
                "target_type": "permanent",
            }
        ],
    }

    assert bindings.phase_to_xmage == {}
    assert (
        _xmage_target_choice(
            decision["candidates"][0], decision, xmage, bindings, xmage_state
        )["id"]
        == "p6"
    )
    assert bindings.phase_to_xmage == {(99, 0): "p6"}


def test_xmage_battlefield_exit_prunes_binding() -> None:
    bindings = ObjectBindings()
    bindings.bind(2, 0, "p5")
    phase_state = {"battlefield": [{"id": 2, "incarnation": 0}]}
    xmage_state = _xmage_signature_state(
        {
            "DiffA": [
                {
                    "id": "p6",
                    "name": "Dwarven Mauler",
                }
            ],
            "DiffB": [],
        }
    )

    bindings.prune(phase_state, xmage_state)

    assert bindings.phase_to_xmage == {}
    assert bindings.xmage_to_phase == {}


def test_reentered_phase_object_rebinds_after_incarnation_changes() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    decision = fixture["prompts"]["target"]["phase"]
    xmage = fixture["prompts"]["target"]["xmage"]
    _signature_fields(decision)
    decision["objects"]["2"]["tapped"] = True
    decision["objects"]["2"]["incarnation"] = 1
    next(item for item in decision["state"]["battlefield"] if item["id"] == 2)[
        "incarnation"
    ] = 1
    xmage_state = _xmage_signature_state(
        {
            "DiffA": [
                {
                    "id": "p5",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
                {
                    "id": "p28",
                    "name": "Dwarven Mauler",
                    "tapped": True,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
                {
                    "id": "p43",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
            ],
            "DiffB": [],
        }
    )
    bindings = ObjectBindings()
    bindings.bind(2, 0, "p5")
    action = next(
        item
        for item in decision["candidates"]
        if item.get("data", {}).get("target", {}).get("Object") == 2
    )

    bindings.prune(decision["state"], xmage_state)
    assert bindings.phase_to_xmage == {}
    assert "p5" not in bindings.xmage_to_phase
    assert (
        _xmage_target_choice(action, decision, xmage, bindings, xmage_state)["id"]
        == "p28"
    )
    assert bindings.phase_to_xmage == {(2, 1): "p28"}


def test_unknown_counter_name_fails_closed() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    decision = fixture["prompts"]["target"]["phase"]
    xmage = fixture["prompts"]["target"]["xmage"]
    _signature_fields(decision)
    decision["objects"]["2"]["counters"] = {"mystery": 1}
    xmage_state = _xmage_signature_state(
        {
            "DiffA": [
                {
                    "id": choice_id,
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": 3,
                    "toughness": 2,
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                }
                for choice_id in ("p5", "p28", "p43")
            ],
            "DiffB": [],
        }
    )
    action = next(
        item
        for item in decision["candidates"]
        if item.get("data", {}).get("target", {}).get("Object") == 2
    )

    with pytest.raises(UnmappedPromptError, match="Unknown phase counter name"):
        _xmage_target_choice(action, decision, xmage, ObjectBindings(), xmage_state)


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


def test_duplicate_target_without_board_ids_fails_closed() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    prompt = fixture["prompts"]["target"]
    decision = prompt["phase"]
    xmage = prompt["xmage"]

    with pytest.raises(UnmappedPromptError, match="Could not identify XMage permanent"):
        _legal_action_diff(decision, xmage)
    assert [choice["id"] for choice in xmage["choices"]] == [
        permanent["id"]
        for permanent in fixture["source_run"]["xmage_permanents"]
        if permanent["name"] == "Dwarven Mauler"
    ]


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


def test_xmage_target_uses_name_when_unique_and_board_ids_are_absent() -> None:
    result = {
        "board": [
            {"name": "DiffA", "battlefield": [{"name": "Dwarven Mauler"}]},
            {"name": "DiffB", "battlefield": [{"name": "Dream Beavers"}]},
        ],
        "choices": [{"id": "p5", "name": "Dwarven Mauler"}],
    }

    assert _xmage_target_key(
        {"id": "p5", "name": "Dwarven Mauler", "target_type": "permanent"},
        result,
    ) == ("permanent", "dwarven mauler", 0, "battlefield")


def test_xmage_target_mirror_duplicates_without_ids_fail_closed() -> None:
    result = {
        "board": [
            {"name": "DiffA", "battlefield": [{"name": "Dwarven Mauler"}]},
            {"name": "DiffB", "battlefield": [{"name": "Dwarven Mauler"}]},
        ],
        "choices": [{"id": "p5", "name": "Dwarven Mauler"}],
    }

    with pytest.raises(UnmappedPromptError):
        _xmage_target_key(
            {"id": "p5", "name": "Dwarven Mauler", "target_type": "permanent"},
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
