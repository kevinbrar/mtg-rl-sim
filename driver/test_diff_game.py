import json
import random
from pathlib import Path

import pytest

from diff_game import (
    _legal_action_diff,
    _scry_legal_choices,
    _scry_selection_payload,
    _policy_candidate_v0,
    policy_candidate,
    summary_exit_code,
)

SCRY_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "boros-dimir-seed-17-scry.json"
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
