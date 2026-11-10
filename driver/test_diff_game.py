import asyncio
import builtins
import json
import random
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import ModuleType

import pytest
import diff_game

from diff_game import (
    ObjectBindings,
    DIFF_MAX_INTERACTIONS_PER_TURN,
    _mage_bench_external_players,
    _legal_action_diff,
    _matching_trivial_priority_stop,
    _phase_activation_set,
    _phase_hand_card,
    _phase_target_key,
    _xmage_hand_choice,
    _scry_legal_choices,
    _scry_selection_payload,
    _suppress_broken_resource_after_terminal,
    _apply_xmage_action,
    _phase_blocker_key,
    _xmage_activation_choice,
    _xmage_activation_set,
    _xmage_target_key,
    _xmage_target_choice,
    _advance_xmage_to_terminal,
    _policy_candidate_v0,
    policy_candidate,
    summary_exit_code,
    _sync_xmage_to_decision,
    UnmappedPromptError,
)

SCRY_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "boros-dimir-seed-17-scry.json"
)
SCRY_POLARITY_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "boros-dimir-seed-42-scry-polarity.json"
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
MANA_PLAN_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "boros-dimir-seed-42-mana-plan.json"
)
COMBAT_BLOCKER_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "boros-dimir-seed-101-blockers.json"
)
AUTO_EMPTY_BLOCKER_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "boros-dimir-seed-17-auto-empty-blockers.json"
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


def _combat_permanent(
    object_id: int,
    name: str,
    *,
    controller: int = 0,
    power: int = 2,
    summoning_sick: bool = False,
) -> dict:
    return {
        "id": object_id,
        "incarnation": 1,
        "name": name,
        "zone": "Battlefield",
        "controller": controller,
        "tapped": False,
        "power": power,
        "toughness": 1,
        "damage": 0,
        "summoning_sick": summoning_sick,
        "attached_to": None,
    }


def _combat_mapping_decision(permanents: list[dict]) -> dict:
    objects = {str(item["id"]): item for item in permanents}
    return {
        "owner": 0,
        "waiting_for": {"type": "DeclareAttackers", "data": {"player": 0}},
        "objects": objects,
        "state": {
            "turn": 3,
            "active_player": 0,
            "phase": "DeclareAttackers",
            "battlefield": [
                {"id": item["id"], "incarnation": item["incarnation"]}
                for item in permanents
            ],
        },
        "candidates": [],
    }


def _run_combat_action(
    monkeypatch,
    action: dict,
    decision: dict,
    xmage_result: dict,
    xmage_state: dict,
) -> list[tuple[str, dict | None]]:
    calls = []

    async def get_state(_session):
        return xmage_state

    async def call(_session, method, arguments=None):
        calls.append((method, arguments))
        return {"action_pending": True}

    async def apply():
        task = await _apply_xmage_action(
            object(), action, decision, xmage_result, ObjectBindings()
        )
        return await task

    monkeypatch.setattr(diff_game, "_get_state", get_state)
    monkeypatch.setattr(diff_game, "_call", call)
    asyncio.run(apply())
    return calls


def _install_fake_anyio_resource_errors(monkeypatch):
    broken_resource_error = type("BrokenResourceError", (Exception,), {})
    closed_resource_error = type("ClosedResourceError", (Exception,), {})
    anyio = ModuleType("anyio")
    anyio.__dict__["BrokenResourceError"] = broken_resource_error
    anyio.__dict__["ClosedResourceError"] = closed_resource_error
    monkeypatch.setitem(sys.modules, "anyio", anyio)
    return broken_resource_error, closed_resource_error


def _run_resource_error_on_stack_exit(error, terminal_summary_recorded):
    async def run():
        stack = AsyncExitStack()

        async def raise_error():
            raise error

        stack.push_async_callback(raise_error)
        async with _suppress_broken_resource_after_terminal(
            stack, lambda: terminal_summary_recorded
        ):
            pass

    asyncio.run(run())


@pytest.mark.parametrize("grouped", (False, True))
def test_closed_resource_error_is_suppressed_after_terminal(monkeypatch, grouped):
    _, closed_resource_error = _install_fake_anyio_resource_errors(monkeypatch)
    error = closed_resource_error()
    if grouped:
        error = builtins.ExceptionGroup("closed resource", [error])

    _run_resource_error_on_stack_exit(error, terminal_summary_recorded=True)


@pytest.mark.parametrize("grouped", (False, True))
def test_broken_resource_error_is_suppressed_after_terminal(monkeypatch, grouped):
    broken_resource_error, _ = _install_fake_anyio_resource_errors(monkeypatch)
    error = broken_resource_error()
    if grouped:
        error = builtins.ExceptionGroup("broken resource", [error])

    _run_resource_error_on_stack_exit(error, terminal_summary_recorded=True)


def test_closed_resource_error_is_raised_before_terminal(monkeypatch):
    _, closed_resource_error = _install_fake_anyio_resource_errors(monkeypatch)

    with pytest.raises(builtins.ExceptionGroup) as error:
        _run_resource_error_on_stack_exit(
            closed_resource_error(), terminal_summary_recorded=False
        )
    assert error.value.subgroup(closed_resource_error) is not None


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


@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        ({"failed": [{"id": "p5"}]}, "failed entries"),
        ({"interrupted": True}, "batch interrupted"),
    ),
)
def test_terminal_combat_batch_results_fail_closed(
    overrides: dict, reason: str
) -> None:
    result = {
        "action_taken": "batch_block",
        "success": True,
        "interrupted": False,
        "failed": [],
        "declared": [{"id": "p5:p1"}],
        "action_pending": False,
    }
    result.update(overrides)
    fallback_states = [{"available": True}, {"available": True}]

    async def advance():
        task = asyncio.get_running_loop().create_future()
        task.set_result(result)
        return await _advance_xmage_to_terminal(
            [object()],
            {0: task},
            {},
            None,
            fallback_states,
            expected_combat_batches={0: ("batch_block", 1)},
        )

    states, terminal_issue = asyncio.run(advance())

    assert states == fallback_states
    assert terminal_issue["kind"] == "combat_batch"
    assert reason in terminal_issue["reason"]
    assert summary_exit_code({"type": "divergence", "kind": "combat_batch"}) == 1


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
        if candidate.get("data", {}).get("cards") == []
    )
    assert _scry_selection_payload(bottom, decision, xmage) == {"choice": "p27"}

    keep = next(
        candidate
        for candidate in decision["candidates"]
        if candidate.get("data", {}).get("cards") == [96]
    )
    assert ("keep",) in phase_choices
    assert _scry_selection_payload(keep, decision, xmage) == {"choice": "no"}


def _diagnostic_scry_inputs() -> tuple[dict, dict, dict, dict]:
    fixture = json.loads(SCRY_POLARITY_FIXTURE.read_text())
    looked_at_id = 9001
    empty_action = fixture["phase_action"]
    keep_action = {"type": "SelectCards", "data": {"cards": [looked_at_id]}}
    decision = {
        "waiting_for": {
            "type": "ScryChoice",
            "data": {"player": 1, "cards": [looked_at_id]},
        },
        "objects": {str(looked_at_id): fixture["phase_looked_at"]},
        "candidates": [empty_action, keep_action],
    }
    return decision, fixture["xmage_prompt"], empty_action, keep_action


# Captured from seed-42 run 20261110T092457080542Z. The Phase action handler
# treats SelectCards.cards as keep-on-top cards; the complement goes to the
# bottom (phase/crates/engine/src/game/engine_resolution_choices.rs:2296–2323;
# CR 701.22a).
def test_scry_diagnostic_empty_phase_selection_maps_to_xmage_bottom_id() -> None:
    decision, xmage, empty_action, _ = _diagnostic_scry_inputs()

    assert _scry_selection_payload(empty_action, decision, xmage) == {"choice": "p26"}


def test_scry_diagnostic_kept_phase_card_maps_to_xmage_no() -> None:
    decision, xmage, _, keep_action = _diagnostic_scry_inputs()

    assert _scry_selection_payload(keep_action, decision, xmage) == {"choice": "no"}


def test_scry_more_than_one_phase_card_fails_closed() -> None:
    decision, xmage, _, _ = _diagnostic_scry_inputs()
    decision["waiting_for"]["data"]["cards"].append(9002)

    with pytest.raises(UnmappedPromptError, match="Unsupported Phase scry prompt"):
        _scry_legal_choices(decision, xmage)


@pytest.mark.parametrize(
    ("message", "extra_choices"),
    (
        ("Select up to two cards to PUT on the BOTTOM of your library (Scry)", []),
        (
            "Select up to one card to PUT on the BOTTOM of your library (Scry)",
            [{"id": "p27", "name": "Dream Beavers", "target_type": "card"}],
        ),
    ),
)
def test_scry_xmage_multi_selection_prompts_fail_closed(
    message: str, extra_choices: list[dict]
) -> None:
    decision, xmage, _, _ = _diagnostic_scry_inputs()
    xmage["message"] = message
    xmage["choices"].extend(extra_choices)

    with pytest.raises(UnmappedPromptError, match="Unsupported XMage scry prompt"):
        _scry_legal_choices(decision, xmage)


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


def test_blocker_prompt_does_not_match_priority_or_pass(monkeypatch) -> None:
    decision = {
        "owner": 1,
        "state": {"turn": 6, "active_player": 1, "phase": "PostCombatMain"},
        "candidates": [{"type": "PassPriority"}, {"type": "PlayLand"}],
    }
    xmage = {
        "action_pending": True,
        "action_type": "GAME_SELECT",
        "response_type": "select",
        "stop_reason": "combat",
        "combat_phase": "blockers",
        "message": "Select blockers",
        "context": "T3 Combat/Declare Blockers (DiffB)",
        "game_seq": 131,
        "board": [
            {"name": "DiffA", "is_active": False, "is_you": True},
            {"name": "DiffB", "is_active": True, "is_you": False},
        ],
        "choices": [{"id": "p5", "name": "Dwarven Mauler"}],
    }
    skipped_stops = [
        {
            "kind": "priority",
            "turn": 6,
            "active_player": "DiffB",
            "step": "declareblockers",
        }
    ]
    calls = []

    async def call(_session, method, _args=None):
        calls.append(method)
        return xmage

    monkeypatch.setattr(diff_game, "_call", call)

    assert _matching_trivial_priority_stop(xmage, decision, skipped_stops) is None
    result, point_diff = asyncio.run(
        _sync_xmage_to_decision(
            [object()],
            decision,
            {},
            {},
            skipped_stops,
            [],
            timeout=0.1,
        )
    )

    assert result == xmage
    assert point_diff == {
        "phase": {
            "turn": 6,
            "active_player": "DiffB",
            "actor": "DiffB",
            "step": "postcombatmain",
        },
        "xmage": {
            "turn": 6,
            "active_player": "DiffB",
            "actor": "DiffA",
            "step": "declareblockers",
        },
    }
    assert calls == ["get_action_choices"]


def test_attacker_subset_maps_to_explicit_xmage_ids(monkeypatch) -> None:
    permanents = [
        _combat_permanent(10, "Dwarven Mauler"),
        _combat_permanent(20, "Dwarven Mauler", power=3),
        _combat_permanent(30, "Dream Beavers", power=1),
    ]
    decision = _combat_mapping_decision(permanents)
    action = {
        "type": "DeclareAttackers",
        "data": {
            "attacks": [
                [10, {"type": "Player", "data": 1}],
                [30, {"type": "Player", "data": 1}],
            ]
        },
    }
    xmage = {
        "choices": [
            {"choice_type": "attacker", "id": "p5"},
            {"choice_type": "attacker", "id": "p6"},
            {"choice_type": "attacker", "id": "p7"},
        ]
    }
    xmage_state = _xmage_signature_state(
        {
            "DiffA": [
                {
                    "id": "p5",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": "2",
                    "toughness": "1",
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
                {
                    "id": "p6",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": "3",
                    "toughness": "1",
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
                {
                    "id": "p7",
                    "name": "Dream Beavers",
                    "tapped": False,
                    "power": "1",
                    "toughness": "1",
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
            ],
            "DiffB": [],
        }
    )

    calls = _run_combat_action(monkeypatch, action, decision, xmage, xmage_state)

    assert calls == [("choose_action", {"attackers": "p5,p7"})]


def test_duplicate_attackers_bind_by_observable_state(monkeypatch) -> None:
    permanents = [
        _combat_permanent(10, "Dwarven Mauler", power=2),
        _combat_permanent(20, "Dwarven Mauler", power=3),
    ]
    decision = _combat_mapping_decision(permanents)
    action = {
        "type": "DeclareAttackers",
        "data": {"attacks": [[20, {"type": "Player", "data": 1}]]},
    }
    xmage = {
        "choices": [
            {"choice_type": "attacker", "id": "p5"},
            {"choice_type": "attacker", "id": "p6"},
        ]
    }
    xmage_state = _xmage_signature_state(
        {
            "DiffA": [
                {
                    "id": "p5",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": "2",
                    "toughness": "1",
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
                {
                    "id": "p6",
                    "name": "Dwarven Mauler",
                    "tapped": False,
                    "power": "3",
                    "toughness": "1",
                    "damage": 0,
                    "summoning_sick": False,
                    "attached_to": None,
                },
            ],
            "DiffB": [],
        }
    )

    calls = _run_combat_action(monkeypatch, action, decision, xmage, xmage_state)

    assert calls == [("choose_action", {"attackers": "p6"})]


def test_non_player_attack_target_is_unmapped(monkeypatch) -> None:
    permanents = [_combat_permanent(10, "Dwarven Mauler")]
    decision = _combat_mapping_decision(permanents)
    action = {
        "type": "DeclareAttackers",
        "data": {"attacks": [[10, {"type": "Object", "data": 99}]]},
    }
    xmage = {"choices": [{"choice_type": "attacker", "id": "p5"}]}
    xmage_state = _xmage_signature_state({"DiffA": [], "DiffB": []})

    with pytest.raises(UnmappedPromptError, match="not the opponent player"):
        _run_combat_action(monkeypatch, action, decision, xmage, xmage_state)


def test_blocker_assignment_maps_seed_101_fixture_to_bridge_string(
    monkeypatch,
) -> None:
    fixture = json.loads(COMBAT_BLOCKER_FIXTURE.read_text())
    action = {
        "type": "DeclareBlockers",
        "data": {
            "assignments": [
                assignment
                for candidate in fixture["phase"]["candidates"][1:]
                for assignment in candidate["data"]["assignments"]
            ]
        },
    }

    calls = _run_combat_action(
        monkeypatch,
        action,
        fixture["phase"],
        fixture["xmage"],
        fixture["xmage_state"],
    )

    assert calls == [("choose_action", {"blockers": "p24:p5,p27:p5"})]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"failed": [{"id": "p5"}]}, "failed entries"),
        ({"interrupted": True}, "interrupted"),
        ({"success": False}, "success was false"),
        ({"declared": []}, "declared 0 entries"),
    ],
)
def test_combat_batch_results_fail_closed(overrides: dict, reason: str) -> None:
    result = {
        "action_taken": "batch_attack",
        "success": True,
        "interrupted": False,
        "failed": [],
        "declared": [{"id": "p5"}],
        "action_pending": False,
    }
    result.update(overrides)
    decision = {
        "owner": 0,
        "state": {"turn": 3, "active_player": 0, "phase": "DeclareAttackers"},
        "candidates": [],
    }

    async def sync():
        task = asyncio.get_running_loop().create_future()
        task.set_result(result)
        return await _sync_xmage_to_decision(
            [object()],
            decision,
            {0: task},
            {},
            [],
            [],
            expected_combat_batches={0: ("batch_attack", 1)},
        )

    xmage_result, point_diff = asyncio.run(sync())

    assert xmage_result == result
    assert point_diff["kind"] == "combat_batch"
    assert reason in point_diff["reason"]


def test_unmapped_combat_followup_returns_unmapped_prompt() -> None:
    result = {
        "action_taken": "batch_attack",
        "success": True,
        "interrupted": False,
        "failed": [],
        "declared": [{"id": "p5"}],
        "action_pending": True,
        "action_type": "GAME_SELECT",
        "message": "Assign damage order",
    }
    decision = {
        "owner": 0,
        "state": {"turn": 3, "active_player": 0, "phase": "DeclareAttackers"},
        "candidates": [],
    }

    async def sync():
        task = asyncio.get_running_loop().create_future()
        task.set_result(result)
        return await _sync_xmage_to_decision(
            [object()],
            decision,
            {0: task},
            {},
            [],
            [],
            expected_combat_batches={0: ("batch_attack", 1)},
        )

    xmage_result, point_diff = asyncio.run(sync())

    assert xmage_result == result
    assert point_diff == {
        "kind": "unmapped_prompt",
        "seat": 0,
        "reason": "XMage returned an unsupported combat follow-up prompt",
    }


def test_unmapped_combat_followup_on_other_seat_is_not_passed(monkeypatch) -> None:
    decision = {
        "owner": 1,
        "state": {"turn": 3, "active_player": 0, "phase": "DeclareBlockers"},
        "candidates": [{"type": "PassPriority"}],
    }
    results = {
        "DiffA": {
            "action_pending": True,
            "action_type": "GAME_SELECT",
            "response_type": "select",
            "combat_phase": "damage",
            "message": "Assign damage order",
            "context": "T2 Combat/Combat Damage (DiffA)",
            "board": [
                {"name": "DiffA", "is_active": True, "is_you": True},
                {"name": "DiffB", "is_active": False, "is_you": False},
            ],
            "choices": [],
        },
        "DiffB": {"game_over": True},
    }
    calls = []

    async def call(session, method, arguments=None):
        calls.append((session, method))
        return results[session]

    monkeypatch.setattr(diff_game, "_call", call)
    xmage_result, point_diff = asyncio.run(
        _sync_xmage_to_decision(
            ["DiffA", "DiffB"],
            decision,
            {},
            {},
            [
                {
                    "kind": "priority",
                    "turn": 3,
                    "active_player": "DiffA",
                    "step": "combatdamage",
                }
            ],
            [],
            timeout=0.1,
        )
    )

    assert xmage_result == results["DiffA"]
    assert point_diff["kind"] == "unmapped_prompt"
    assert not any(method == "pass_priority" for _, method in calls)


def test_phase_auto_empty_blockers_declines_xmage_prompt(monkeypatch) -> None:
    fixture = json.loads(AUTO_EMPTY_BLOCKER_FIXTURE.read_text())
    decision = fixture["phase"]
    xmage = fixture["xmage"]
    record = fixture["phase_attack_record"]
    calls = []
    priority_result = {
        "action_pending": True,
        "action_type": "GAME_SELECT",
        "response_type": "select",
        "context": "T3 Precombat Main (DiffA) YOUR_MAIN",
        "board": [
            {"name": "DiffA", "is_active": True, "is_you": True},
            {"name": "DiffB", "is_active": False, "is_you": False},
        ],
        "choices": [{"name": "Forest", "action": "land"}],
    }

    async def call(_session, method, arguments=None):
        calls.append((method, arguments))
        return priority_result

    monkeypatch.setattr(diff_game, "_call", call)
    events = []
    result, point_diff = asyncio.run(
        _sync_xmage_to_decision(
            [object()],
            decision,
            {},
            {0: xmage},
            [],
            events,
            phase_attack_records={(record["turn"], record["active_player"])},
            phase_blocker_decisions=set(),
        )
    )

    assert result == priority_result
    assert point_diff == {}
    assert calls == [("choose_action", {"choice": "no"})]
    assert events == [
        {
            "engine": "xmage",
            "rule": "phase_auto_empty_blockers",
            "context": xmage["context"],
            "turn": record["turn"],
            "active_player": "DiffB",
        }
    ]


@pytest.mark.parametrize(
    ("attack_records", "blocker_decisions"),
    [
        ({(4, 1)}, {(4, 1)}),
        (set(), set()),
    ],
)
def test_phase_auto_empty_blockers_requires_matching_phase_evidence(
    attack_records: set[tuple[int, int]],
    blocker_decisions: set[tuple[int, int]],
) -> None:
    fixture = json.loads(AUTO_EMPTY_BLOCKER_FIXTURE.read_text())
    decision = fixture["phase"]
    xmage = fixture["xmage"]
    events = []

    result, point_diff = asyncio.run(
        _sync_xmage_to_decision(
            [object()],
            decision,
            {},
            {0: xmage},
            [],
            events,
            phase_attack_records=attack_records,
            phase_blocker_decisions=blocker_decisions,
        )
    )

    assert result == xmage
    assert point_diff == {
        "phase": {
            "turn": 5,
            "active_player": "DiffA",
            "actor": "DiffA",
            "step": "precombatmain",
        },
        "xmage": {
            "turn": 4,
            "active_player": "DiffB",
            "actor": "DiffA",
            "step": "declareblockers",
        },
    }
    assert events == []


def test_phase_blocker_decision_is_recorded_by_turn_and_active_player() -> None:
    decision = {
        "waiting_for": {"type": "DeclareBlockers"},
        "state": {
            "turn": 4,
            "active_player": 1,
            "phase": "DeclareBlockers",
        },
        "candidates": [
            {
                "type": "DeclareBlockers",
                "data": {"assignments": [[69, 12]]},
            }
        ],
    }

    assert _phase_blocker_key(decision) == (4, 1)


def test_mage_bench_config_sets_loop_guard_limit_for_both_external_players() -> None:
    players = _mage_bench_external_players([Path("a.dck"), Path("b.dck")])

    assert [player["name"] for player in players] == ["DiffA", "DiffB"]
    assert all(
        player["max_interactions_per_turn"] == DIFF_MAX_INTERACTIONS_PER_TURN
        for player in players
    )


@pytest.mark.parametrize(
    "guard_result",
    [
        {
            "success": True,
            "action_pending": False,
            "action_taken": "auto_passed_loop_detected",
            "warning": "Too many interactions this turn. Auto-passing.",
        },
        {
            "success": True,
            "action_pending": False,
            "warning": "Loop detected; auto-passing.",
        },
    ],
)
def test_xmage_loop_guard_result_diverges_without_another_pass(
    monkeypatch, guard_result
) -> None:
    calls = []

    async def call(_session, method, _args=None):
        calls.append(method)
        return guard_result

    monkeypatch.setattr(diff_game, "_call", call)
    result, diff = asyncio.run(
        _sync_xmage_to_decision(
            [object(), object()],
            {"owner": 0},
            {},
            {},
            [],
            [],
        )
    )

    assert result == guard_result
    assert diff["kind"] == "xmage_loop_guard"
    assert calls == ["get_action_choices"]


def test_unknown_non_pending_xmage_status_raises(monkeypatch) -> None:
    async def call(_session, _method, _args=None):
        return {"action_pending": False, "action_taken": "unexpected"}

    monkeypatch.setattr(diff_game, "_call", call)
    with pytest.raises(UnmappedPromptError, match="non-pending result"):
        asyncio.run(
            _sync_xmage_to_decision(
                [object(), object()],
                {"owner": 0},
                {},
                {},
                [],
                [],
            )
        )


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


def test_bound_collar_attached_to_different_host_fails_signature_reuse() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    decision = fixture["prompts"]["activation"]["phase"]
    xmage = fixture["prompts"]["activation"]["xmage"]
    for host_id in ("2", "23"):
        decision["objects"][host_id] = {
            "name": "Dwarven Mauler",
            "zone": "Battlefield",
            "controller": 0,
            "incarnation": 0,
            "tapped": False,
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
                    "attached_to": "p28",
                },
                {
                    "id": "p4",
                    "name": "Basilisk Collar",
                    "tapped": False,
                    "damage": 0,
                    "attached_to": "p5",
                },
                {
                    "id": "p5",
                    "name": "Dwarven Mauler",
                    "tapped": False,
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
    bindings.bind(2, 0, "p5")
    bindings.bind(23, 0, "p28")
    bindings.bind(27, 0, "p3")
    action = next(
        item
        for item in decision["candidates"]
        if item.get("type") == "ActivateAbility" and item["data"]["source_id"] == 27
    )

    with pytest.raises(UnmappedPromptError) as error:
        _xmage_activation_choice(action, decision, xmage, bindings, xmage_state)
    assert "phase_signature=" in str(error.value)
    assert "xmage_signature=" in str(error.value)
    assert bindings.phase_to_xmage[(27, 0)] == "p3"


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


def test_single_battlefield_match_must_match_signature() -> None:
    fixture = json.loads(DUPLICATE_FIXTURE.read_text())
    decision = fixture["prompts"]["target"]["phase"]
    xmage = fixture["prompts"]["target"]["xmage"]
    _signature_fields(decision)
    decision["objects"]["2"]["tapped"] = True
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
                }
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


def test_policy_ignores_cancel_cast_on_equip_target_prompt() -> None:
    decision = {
        "waiting_for": {"type": "Priority", "data": {"player": 0}},
        "candidates": [
            {"type": "ChooseTarget", "data": {"target": {"Object": 2}}},
            {"type": "ChooseTarget", "data": {"target": {"Object": 23}}},
            {"type": "ChooseTarget", "data": {"target": {"Object": 25}}},
            {"type": "CancelCast"},
        ],
    }

    for seed in range(100):
        _, action = policy_candidate(decision, random.Random(seed))
        assert action["type"] == "ChooseTarget"


def test_v0_policy_ignores_cancel_cast_when_supported_choice_remains() -> None:
    decision = {
        "candidates": [
            {"type": "CancelCast"},
            {"type": "PassPriority"},
        ]
    }

    for seed in range(100):
        _, action = policy_candidate(decision, random.Random(seed), v0=True)
        assert action["type"] == "PassPriority"


@pytest.mark.parametrize("v0", (False, True))
def test_cancel_cast_only_is_unmapped(v0: bool) -> None:
    decision = {"candidates": [{"type": "CancelCast"}]}

    with pytest.raises(UnmappedPromptError, match="No phase action remains"):
        policy_candidate(decision, random.Random(0), v0=v0)


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


def _mana_source_decision(
    permanents: list[dict], *, waiting_type: str = "ManaPayment"
) -> dict:
    return {
        "owner": 0,
        "actor": 0,
        "waiting_for": {"type": waiting_type, "data": {"player": 0}},
        "objects": {str(permanent["id"]): permanent for permanent in permanents},
        "state": {
            "turn": 9,
            "active_player": 0,
            "phase": "PrecombatMain",
            "battlefield": [
                {
                    "id": permanent["id"],
                    "incarnation": permanent["incarnation"],
                }
                for permanent in permanents
            ],
        },
        "candidates": [],
    }


def _mana_permanent(
    object_id: int,
    name: str,
    mana_symbol: str | None,
    *,
    tapped: bool = False,
) -> dict:
    rules = [f"{{T}}: Add {{{mana_symbol}}}."] if mana_symbol else []
    return {
        "id": object_id,
        "incarnation": 1,
        "name": name,
        "zone": "Battlefield",
        "controller": 0,
        "tapped": tapped,
        "damage": 0,
        "attached_to": None,
        "rules": rules,
    }


def _mana_applied_state(decision: dict, source_ids: set[int] | None = None) -> dict:
    source_ids = source_ids or set()
    battlefield = []
    for item in decision["state"]["battlefield"]:
        permanent = dict(item)
        permanent["tapped"] = item["id"] in source_ids
        battlefield.append(permanent)
    return {"battlefield": battlefield}


def _mana_added(
    source_id: int, mana_type: str, decision: dict
) -> tuple[dict, dict, dict]:
    return (
        {
            "source_id": source_id,
            "mana_type": mana_type,
            "player_id": 0,
        },
        decision,
        _mana_applied_state(decision, {source_id}),
    )


def test_mana_added_from_applied_keeps_event_and_states() -> None:
    event = {"source_id": 7, "mana_type": "Red", "player_id": 0}
    decision = {"type": "decision"}
    state = {"battlefield": [{"id": 7}]}

    assert diff_game._mana_added_from_applied(
        {"mana_added": [event], "state": state}, decision
    ) == [(event, decision, state)]


def test_single_ability_land_mana_plan_omits_ability_index() -> None:
    phase_source = _mana_permanent(7, "Mountain", "R")
    decision = _mana_source_decision([phase_source])
    xmage_state = {
        "players": [
            {
                "name": "DiffA",
                "battlefield": [
                    {
                        "id": "p5",
                        "name": "Mountain",
                        "tapped": False,
                        "damage": 0,
                        "rules": ["{T}: Add {R}."],
                    }
                ],
            }
        ]
    }

    plan = diff_game._mana_plan_from_events(
        [_mana_added(7, "Red", decision)], 0, ObjectBindings(), xmage_state
    )

    assert plan == ["p5"]


@pytest.mark.parametrize(
    ("mana_type", "rules"),
    (
        (
            "Colorless",
            ["{T}: Add {C}.", "{T}, Pay 1 life: Add {R}."],
        ),
        (
            "Red",
            ["{T}: Add {C}.", "{T}, Pay 1 life: Add {R}."],
        ),
        ("Red", ["{T}: Add {R} or {W}."]),
    ),
)
def test_mana_plan_rejects_unsupported_mana_ability_lines(
    mana_type: str, rules: list[str]
) -> None:
    symbol = "C" if mana_type == "Colorless" else "R"
    phase_source = _mana_permanent(51, "Synthetic Land", symbol)
    decision = _mana_source_decision([phase_source])
    xmage_state = {
        "players": [
            {
                "name": "DiffA",
                "battlefield": [
                    {
                        "id": "p5",
                        "name": "Synthetic Land",
                        "tapped": False,
                        "damage": 0,
                        "rules": rules,
                    }
                ],
            }
        ]
    }

    with pytest.raises(UnmappedPromptError) as error:
        diff_game._mana_plan_from_events(
            [_mana_added(51, mana_type, decision)],
            0,
            ObjectBindings(),
            xmage_state,
        )

    message = str(error.value)
    assert "Synthetic Land" in message
    assert all(line in message for line in rules)


@pytest.mark.parametrize(
    ("mana_type", "expected"), (("White", "p7:1"), ("Red", "p7:0"))
)
def test_inspiring_vantage_plan_uses_color_rule_index(
    mana_type: str, expected: str
) -> None:
    fixture = json.loads(MANA_PLAN_FIXTURE.read_text())
    phase_source = fixture["phase"]["objects"]["901"]
    decision = _mana_source_decision([phase_source])
    decision["state"] = fixture["phase"]["state"]
    xmage_state = fixture["xmage_state"]

    plan = diff_game._mana_plan_from_events(
        [_mana_added(901, mana_type, decision)], 0, ObjectBindings(), xmage_state
    )

    assert fixture["source_artifact"] == "runs/20261110T192400975972Z/divergence.json"
    assert fixture["phase_object_ids_are_synthetic"] is True
    assert plan == [expected]


def test_duplicate_basic_mana_sources_reuse_battlefield_bindings() -> None:
    first = _mana_permanent(11, "Island", "U")
    second = _mana_permanent(12, "Island", "U")
    first_decision = _mana_source_decision([first, second])
    second_decision = _mana_source_decision(
        [_mana_permanent(11, "Island", "U", tapped=True), second]
    )
    xmage_state = {
        "players": [
            {
                "name": "DiffA",
                "battlefield": [
                    {
                        "id": "p3",
                        "name": "Island",
                        "tapped": False,
                        "damage": 0,
                        "rules": ["{T}: Add {U}."],
                    },
                    {
                        "id": "p4",
                        "name": "Island",
                        "tapped": False,
                        "damage": 0,
                        "rules": ["{T}: Add {U}."],
                    },
                ],
            }
        ]
    }

    plan = diff_game._mana_plan_from_events(
        [
            _mana_added(11, "Blue", first_decision),
            _mana_added(12, "Blue", second_decision),
        ],
        0,
        ObjectBindings(),
        xmage_state,
    )

    assert plan == ["p3", "p4"]


def test_sacrificed_treasure_mana_source_is_unmapped() -> None:
    treasure = _mana_permanent(31, "Treasure", None)
    decision = _mana_source_decision([treasure])
    applied_state = {"battlefield": []}
    event = (
        {"source_id": 31, "mana_type": "Red", "player_id": 0},
        decision,
        applied_state,
    )

    with pytest.raises(UnmappedPromptError, match="left the Phase battlefield"):
        diff_game._mana_plan_from_events(
            [event],
            0,
            ObjectBindings(),
            {"players": [{"name": "DiffA", "battlefield": []}]},
        )


def test_two_mana_additions_from_one_source_are_unmapped() -> None:
    source = _mana_permanent(41, "Mountain", "R")
    decision = _mana_source_decision([source])

    with pytest.raises(UnmappedPromptError, match="added mana more than once"):
        diff_game._mana_plan_from_events(
            [
                _mana_added(41, "Red", decision),
                _mana_added(41, "Red", decision),
            ],
            0,
            ObjectBindings(),
            {
                "players": [
                    {
                        "name": "DiffA",
                        "battlefield": [
                            {
                                "id": "p5",
                                "name": "Mountain",
                                "tapped": False,
                                "damage": 0,
                                "rules": ["{T}: Add {R}."],
                            }
                        ],
                    }
                ]
            },
        )


def test_mana_plan_size_mismatch_is_reported() -> None:
    error = diff_game._mana_plan_ack_error(
        {"mana_plan_set": True, "mana_plan_size": 1}, ["p7:1", "p5"]
    )

    assert error is not None
    assert "got 1" in error


def test_phase_cast_macro_auto_pays_on_cast_action() -> None:
    source = _mana_permanent(7, "Mountain", "R")
    cast = {"type": "CastSpell", "data": {"object_id": 50}}
    initial = _mana_source_decision([source], waiting_type="Priority")
    initial["type"] = "decision"
    initial["candidates"] = [cast]
    final = dict(initial, candidates=[{"type": "PassPriority"}])
    event = {"source_id": 7, "mana_type": "Red", "player_id": 0}

    class FakePhase:
        def choose(self, _index: int) -> dict:
            return {
                "type": "applied",
                "ok": True,
                "mana_added": [event],
                "state": _mana_applied_state(initial, {7}),
            }

        def _read(self) -> dict:
            return final

    final_decision, records, targets, events = asyncio.run(
        diff_game._phase_cast_macro(
            FakePhase(), initial, 0, cast, random.Random(0), False
        )
    )
    plan = diff_game._mana_plan_from_events(
        events,
        0,
        ObjectBindings(),
        {
            "players": [
                {
                    "name": "DiffA",
                    "battlefield": [
                        {
                            "id": "p5",
                            "name": "Mountain",
                            "tapped": False,
                            "damage": 0,
                            "rules": ["{T}: Add {R}."],
                        }
                    ],
                }
            ]
        },
    )

    assert final_decision is final
    assert len(records) == 1
    assert targets == []
    assert plan == ["p5"]
    assert diff_game._cast_macro_step_increment(targets) == 1


def test_mana_payment_tap_and_pass_collect_events_without_counting_steps() -> None:
    sources = [_mana_permanent(7, "Mountain", "R"), _mana_permanent(8, "Mountain", "R")]
    initial = _mana_source_decision(sources, waiting_type="Priority")
    initial["type"] = "decision"
    cast = {"type": "CastSpell", "data": {"object_id": 50}}
    initial["candidates"] = [cast]
    tap = {"type": "TapLandForMana", "data": {"object_id": 7}}
    pass_priority = {"type": "PassPriority", "data": {}}
    payment = _mana_source_decision(sources)
    payment["type"] = "decision"
    payment["candidates"] = [tap]
    finalize = dict(payment, candidates=[pass_priority])
    final = dict(payment, waiting_for={"type": "Priority", "data": {"player": 0}})
    events = [
        {"source_id": 7, "mana_type": "Red", "player_id": 0},
        {"source_id": 8, "mana_type": "Red", "player_id": 0},
    ]

    class FakePhase:
        def __init__(self) -> None:
            self.responses = iter(
                [
                    {
                        "type": "applied",
                        "ok": True,
                        "mana_added": [],
                        "state": _mana_applied_state(initial),
                    },
                    {
                        "type": "applied",
                        "ok": True,
                        "mana_added": [events[0]],
                        "state": _mana_applied_state(payment, {7}),
                    },
                    {
                        "type": "applied",
                        "ok": True,
                        "mana_added": [events[1]],
                        "state": _mana_applied_state(payment, {7, 8}),
                    },
                ]
            )
            self.decisions = iter([payment, finalize, final])

        def choose(self, _index: int) -> dict:
            return next(self.responses)

        def _read(self) -> dict:
            return next(self.decisions)

    _, records, targets, mana_events = asyncio.run(
        diff_game._phase_cast_macro(
            FakePhase(), initial, 0, cast, random.Random(0), False
        )
    )

    assert [record[2]["type"] for record in records] == [
        "CastSpell",
        "TapLandForMana",
        "PassPriority",
    ]
    assert [item[0]["source_id"] for item in mana_events] == [7, 8]
    assert targets == []
    assert diff_game._cast_macro_step_increment(targets) == 1


def test_cast_macro_keeps_target_record_order_before_payment() -> None:
    source = _mana_permanent(7, "Mountain", "R")
    initial = _mana_source_decision([source], waiting_type="Priority")
    initial["type"] = "decision"
    cast = {"type": "CastSpell", "data": {"object_id": 50}}
    initial["candidates"] = [cast]
    first_target_action = {"type": "ChooseTarget", "data": {"target": {"Object": 1}}}
    second_target_action = {
        "type": "SelectTargets",
        "data": {"targets": [{"Object": 2}]},
    }
    first_target = {
        "type": "decision",
        "actor": 0,
        "waiting_for": {
            "type": "TargetSelection",
            "data": {"player": 0, "pending_cast": {"object_id": 50}},
        },
        "state": initial["state"],
        "candidates": [first_target_action],
    }
    second_target = dict(first_target, candidates=[second_target_action])
    payment = _mana_source_decision([source])
    payment["type"] = "decision"
    payment["candidates"] = [{"type": "TapLandForMana", "data": {"object_id": 7}}]
    final = dict(payment, waiting_for={"type": "Priority", "data": {"player": 0}})

    class FakePhase:
        def __init__(self) -> None:
            self.decisions = iter([first_target, second_target, payment, final])

        def choose(self, _index: int) -> dict:
            return {
                "type": "applied",
                "ok": True,
                "mana_added": [],
                "state": _mana_applied_state(initial),
            }

        def _read(self) -> dict:
            return next(self.decisions)

    _, _, targets, _ = asyncio.run(
        diff_game._phase_cast_macro(
            FakePhase(), initial, 0, cast, random.Random(0), False
        )
    )

    assert targets == [
        (first_target, 0, first_target_action),
        (second_target, 0, second_target_action),
    ]
    assert diff_game._cast_macro_step_increment(targets) == 3


def test_cast_macro_fails_closed_on_unsupported_and_other_actor_prompts() -> None:
    unsupported = {
        "type": "decision",
        "actor": 0,
        "waiting_for": {
            "type": "ChooseXValue",
            "data": {"player": 0, "pending_cast": {"object_id": 50}},
        },
    }
    other_actor = {
        "type": "decision",
        "actor": 1,
        "waiting_for": {
            "type": "TargetSelection",
            "data": {"player": 1, "pending_cast": {"object_id": 50}},
        },
    }

    with pytest.raises(UnmappedPromptError, match="Unsupported cast continuation"):
        diff_game._is_cast_continuation(unsupported, 50, 0)
    with pytest.raises(UnmappedPromptError, match="another actor"):
        diff_game._is_cast_continuation(other_actor, 50, 0)
    with pytest.raises(UnmappedPromptError, match="2 options is unsupported"):
        diff_game._is_cast_continuation(
            {
                "type": "decision",
                "actor": 0,
                "waiting_for": {
                    "type": "ManaSourceSelection",
                    "data": {
                        "player": 0,
                        "options": [{"source_id": 1}, {"source_id": 2}],
                    },
                },
            },
            50,
            0,
        )


@pytest.mark.parametrize(
    ("response", "raises"),
    (
        (
            {
                "log": "[System] Spell cancelled — mana plan was incorrect or incomplete.",
                "cursor": 6,
                "cursor_reset": False,
            },
            True,
        ),
        ({"log": "The spell resolved.", "cursor": 6, "cursor_reset": False}, False),
        ({"log": "", "cursor": 6, "cursor_reset": True}, True),
    ),
)
def test_game_log_cancellation_check(response: dict, raises: bool, monkeypatch) -> None:
    async def call(_session, name, arguments=None):
        assert name == "get_game_log"
        assert arguments == {"cursor": 5}
        return response

    monkeypatch.setattr(diff_game, "_call", call)
    pending = {
        "owner": 0,
        "cursor": 5,
        "macro_decision": {"type": "decision"},
        "mana_plan": ["p5"],
    }

    async def check() -> None:
        await diff_game._check_pending_mana_plan_log([object()], pending)

    if raises:
        with pytest.raises(diff_game.DifferentialDivergenceError) as error:
            asyncio.run(check())
        if "Spell cancelled" in response["log"]:
            assert error.value.kind == "mana_plan"
            assert error.value.details["mana_plan"] == ["p5"]
            assert "Spell cancelled" in error.value.details["log_excerpt"]
    else:
        asyncio.run(check())


def test_cast_action_sends_nonempty_csv_or_empty_auto_tap_request(monkeypatch) -> None:
    calls = []

    async def call(_session, method, arguments=None):
        calls.append((method, arguments))
        return {"action_pending": True}

    monkeypatch.setattr(diff_game, "_call", call)
    decision = {
        "owner": 0,
        "waiting_for": {"type": "Priority", "data": {"player": 0}},
        "objects": {"20": {"name": "Lightning Bolt", "zone": "Hand", "controller": 0}},
    }
    xmage_result = {
        "board": [
            {
                "name": "DiffA",
                "is_you": True,
                "hand": [{"name": "Lightning Bolt", "id": "p3"}],
            }
        ],
        "choices": [
            {"name": "Lightning Bolt", "index": 5, "action": "cast", "id": "p3"}
        ],
    }

    async def send(plan: list[str]) -> None:
        task = await diff_game._apply_xmage_action(
            object(),
            {"type": "CastSpell", "data": {"object_id": 20}},
            decision,
            xmage_result,
            ObjectBindings(),
            mana_plan=plan,
        )
        await task

    asyncio.run(send(["p2:0", "p4"]))
    asyncio.run(send([]))

    assert calls == [
        (
            "choose_action",
            {"choice": "5", "mana_plan": "p2:0,p4", "auto_tap": False},
        ),
        ("choose_action", {"choice": "5", "auto_tap": True}),
    ]


def test_empty_plan_ack_is_optional_and_size_mismatch_fails(monkeypatch) -> None:
    assert diff_game._mana_plan_ack_error({}, []) is None

    async def call(_session, name, arguments=None):
        assert name == "get_game_log"
        return {"cursor": 5}

    async def apply(*_args, **_kwargs):
        async def result() -> dict:
            return {"mana_plan_set": True, "mana_plan_size": 1}

        return asyncio.create_task(result())

    monkeypatch.setattr(diff_game, "_call", call)
    monkeypatch.setattr(diff_game, "_apply_xmage_action", apply)

    async def send() -> None:
        await diff_game._send_cast_macro_to_xmage(
            [object()],
            0,
            {"type": "CastSpell", "data": {}},
            {},
            {},
            ["p2", "p4"],
            [],
            {},
            {},
            [],
            [],
            ObjectBindings(),
            set(),
            set(),
            {},
            {},
        )

    with pytest.raises(diff_game.DifferentialDivergenceError) as error:
        asyncio.run(send())
    assert error.value.kind == "mana_plan"
    assert "got 1" in error.value.details["reason"]


def test_mana_in_pool_before_cast_macro_is_unmapped() -> None:
    decision = _mana_source_decision([_mana_permanent(7, "Mountain", "R")])
    applied = {
        "mana_added": [{"source_id": 7, "mana_type": "Red", "player_id": 0}],
        "state": _mana_applied_state(decision, {7}),
    }
    unplanned_mana = []

    diff_game._record_unplanned_mana(unplanned_mana, applied, decision)
    with pytest.raises(UnmappedPromptError, match="produced before this cast macro"):
        diff_game._ensure_no_unplanned_mana(unplanned_mana)
