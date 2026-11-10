import pytest

from diff_game import summary_exit_code


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
