import pytest

from scripts.check_review_packet import (
    REQUIRED_SECTIONS,
    parse_cards_line,
    validate_review_packet,
)


DEFAULT_ROW = (
    "Parser behavior | python -m pytest tests -q | passed: 3 "
    "| artifacts/test-results.log | N"
)


def packet(
    *,
    sections: tuple[str, ...] = REQUIRED_SECTIONS,
    evidence_header: str = (
        "| Claim | Check | Result | Artifact | Independent (Y/N, producer) |"
    ),
    evidence_rows: tuple[str, ...] = (DEFAULT_ROW,),
    cards: str = "cards: none",
) -> str:
    section_content = {
        "## Brief / decision reference": "Implements the approved CI governance brief.",
        "## Surfaces": f"Workflow and Python review-packet checks.\n\n{cards}",
        "## Evidence": "\n".join(
            (
                evidence_header,
                "| --- | --- | --- | --- | --- |",
                *(f"| {row} |" for row in evidence_rows),
            )
        ),
        "## Not verified": "GitHub-hosted runner disk capacity.",
        "## Divergence from plan": "None.",
        "## Risk & rollback": "Revert this commit to remove the gates.",
    }
    return "\n\n".join(
        f"{heading}\n\n{section_content[heading]}"
        for heading in sections
        if heading in section_content
    )


def test_valid_packet_passes_for_changes_outside_engine_paths() -> None:
    assert validate_review_packet(packet(), ["README.md"]) == []


@pytest.mark.parametrize("heading", REQUIRED_SECTIONS)
def test_each_required_section_is_enforced(heading: str) -> None:
    errors = validate_review_packet(
        packet(
            sections=tuple(
                section for section in REQUIRED_SECTIONS if section != heading
            )
        ),
        ["README.md"],
    )
    assert f"Missing required section: {heading}" in errors


def test_evidence_section_requires_a_markdown_table() -> None:
    body = packet().replace(
        "| Claim | Check | Result | Artifact | Independent (Y/N, producer) |",
        "Claim: Check: Result: Artifact: Independent:",
    )
    errors = validate_review_packet(body, ["README.md"])
    assert any(
        "Evidence section must contain a Markdown table" in error for error in errors
    )


@pytest.mark.parametrize(
    "header",
    (
        "| Check | Result | Artifact | Independent |",
        "| Claim | Result | Artifact | Independent |",
        "| Claim | Check | Artifact | Independent |",
        "| Claim | Check | Result | Independent |",
        "| Claim | Check | Result | Artifact |",
    ),
)
def test_evidence_table_requires_each_header(header: str) -> None:
    errors = validate_review_packet(packet(evidence_header=header), ["README.md"])
    assert any(
        "header includes Claim, Check, Result, Artifact, and Independent" in error
        for error in errors
    )


@pytest.mark.parametrize(
    "result",
    (
        "verified",
        "pass",
        "passed",
        "Passed with warnings",
        "success: 29417",
        "needs follow-up",
        "skipped 2, passed 38",
        "skipped: 2, passed 38",
    ),
)
@pytest.mark.parametrize("artifact", ("", "ran focused test"))
def test_results_require_an_artifact_path_or_link(
    result: str,
    artifact: str,
) -> None:
    row = f"Parser behavior | focused test | {result} | {artifact} | N"
    errors = validate_review_packet(packet(evidence_rows=(row,)), ["README.md"])
    assert (
        "Evidence table row 1 has a result but Artifact must contain a link or path"
        in errors
    )


@pytest.mark.parametrize(
    "artifact",
    (
        "artifacts/engine-tests.log",
        "artifacts/run@v1.log",
        "/home/ubuntu/x.log",
        "phase-patches/0017-foo.patch",
        "tests/test_check_review_packet.py",
        ".github/workflows/ci.yml",
        "[test log](https://example.test/engine.log)",
    ),
)
def test_results_accept_artifact_paths_and_links(artifact: str) -> None:
    row = f"Parser behavior | focused test | passed | {artifact} | N"
    assert validate_review_packet(packet(evidence_rows=(row,)), ["README.md"]) == []


def test_version_like_text_is_not_an_artifact_path() -> None:
    row = "Parser behavior | focused test | passed | v1.0 pending | N"
    errors = validate_review_packet(packet(evidence_rows=(row,)), ["README.md"])
    assert (
        "Evidence table row 1 has a result but Artifact must contain a link or path"
        in errors
    )


@pytest.mark.parametrize(
    "result",
    (
        "not run",
        "not run: needs runner",
        "Skipped",
        "skipped (no XMage)",
        "n/a",
        "N/A",
        "not applicable",
    ),
)
def test_not_run_results_do_not_require_an_artifact(result: str) -> None:
    row = f"Parser behavior | focused test | {result} |  | N"
    assert validate_review_packet(packet(evidence_rows=(row,)), ["README.md"]) == []


def test_nonpositive_result_with_artifact_is_valid() -> None:
    row = "Parser behavior | focused test | needs follow-up | artifacts/review.md | N"
    assert validate_review_packet(packet(evidence_rows=(row,)), ["README.md"]) == []


@pytest.mark.parametrize(
    "path",
    (
        "phase",
        "phase/crates/engine/src/lib.rs",
        "phase-env/src/main.rs",
        "driver/diff_game.py",
        "oracle/README.md",
    ),
)
def test_engine_paths_require_cards_and_independent_evidence(path: str) -> None:
    assert (
        validate_review_packet(
            packet(
                evidence_rows=(DEFAULT_ROW.replace(" | N", " | Yes, cargo mutants"),)
            ),
            [path],
        )
        == []
    )


def test_engine_path_without_independent_row_fails() -> None:
    errors = validate_review_packet(packet(), ["phase-env/src/main.rs"])
    assert (
        "Engine-path changes require an Evidence row whose Independent value starts with Y"
        in errors
    )


@pytest.mark.parametrize(
    ("result", "artifact"),
    (
        ("not run", "artifacts/test-results.log"),
        ("SKIPPED", "artifacts/test-results.log"),
        ("n/a", "artifacts/test-results.log"),
        ("passed", ""),
        ("", "artifacts/test-results.log"),
    ),
)
def test_independent_row_requires_result_and_artifact(
    result: str,
    artifact: str,
) -> None:
    row = f"Parser behavior | focused test | {result} | {artifact} | Y, reviewer"
    errors = validate_review_packet(
        packet(evidence_rows=(row,)), ["phase-env/src/main.rs"]
    )
    assert (
        "Engine-path changes require an Evidence row whose Independent value starts with Y"
        in errors
    )


def test_independent_row_with_result_and_artifact_counts() -> None:
    row = (
        "Parser behavior | fresh-context review | behavior checked "
        "| artifacts/review.md | Y, reviewer"
    )
    assert (
        validate_review_packet(packet(evidence_rows=(row,)), ["phase-env/src/main.rs"])
        == []
    )


def test_engine_path_without_cards_line_fails() -> None:
    body = packet(cards="")
    errors = validate_review_packet(body, ["phase-patches/0001-example.patch"])
    assert "Engine-path changes require a cards: line" in errors


@pytest.mark.parametrize(
    "cards",
    ("cards: Bringer of the Last Gift, Firebender Ascension", "Cards: none"),
)
def test_cards_line_accepts_names_or_none_case_insensitively(cards: str) -> None:
    body = packet(
        cards=cards,
        evidence_rows=(DEFAULT_ROW.replace(" | N", " | Y, mutants"),),
    )
    assert validate_review_packet(body, ["phase-env/src/main.rs"]) == []


@pytest.mark.parametrize(
    "path",
    (
        "phase-patches/0031-x.patch",
        "phase/client/public/card-data.json",
    ),
)
def test_cards_none_is_rejected_for_patches_and_card_data(path: str) -> None:
    body = packet(
        cards="cards: none",
        evidence_rows=(DEFAULT_ROW.replace(" | N", " | Y, mutants"),),
    )
    errors = validate_review_packet(body, [path])
    assert (
        "phase-patches/card-data changes need per-card XMage diffs (harness v1); "
        "cards: none is not accepted for them"
    ) in errors


def test_cards_none_is_allowed_for_phase_env_changes() -> None:
    body = packet(
        cards="cards: none",
        evidence_rows=(DEFAULT_ROW.replace(" | N", " | Y, mutants"),),
    )
    assert validate_review_packet(body, ["phase-env/src/main.rs"]) == []


def test_card_names_are_allowed_for_phase_patch_changes() -> None:
    body = packet(
        cards="cards: Lost in the Maze",
        evidence_rows=(DEFAULT_ROW.replace(" | N", " | Y, mutants"),),
    )
    assert validate_review_packet(body, ["phase-patches/0031-x.patch"]) == []


@pytest.mark.parametrize(
    "cards",
    (
        "cards:",
        "cards: Bringer of the Last Gift,, Firebender Ascension",
        "cards: none, Firebender Ascension",
    ),
)
def test_cards_line_must_be_none_or_comma_separated_names(cards: str) -> None:
    body = packet(
        cards=cards,
        evidence_rows=(DEFAULT_ROW.replace(" | N", " | Y, mutants"),),
    )
    errors = validate_review_packet(body, ["phase-patches/0001-example.patch"])
    assert "cards: must be `none` or a comma-separated list of card names" in errors


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ("cards: none", "none"),
        ("## Surfaces\nCards: Forest", "Forest"),
        ("cards: Forest\ncards: none", "Forest"),
        ("## Surfaces\nNo card claim", None),
    ),
)
def test_parse_cards_line_returns_the_first_case_insensitive_line(
    body: str, expected: str | None
) -> None:
    assert parse_cards_line(body) == expected
