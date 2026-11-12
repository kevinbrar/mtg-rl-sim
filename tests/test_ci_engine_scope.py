import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def test_ci_scope_regexes_cover_engine_and_xmage_paths() -> None:
    workflow = WORKFLOW.read_text()
    engine_match = re.search(
        r"^\s*engine_path_regex='([^']+)'$", workflow, flags=re.MULTILINE
    )
    xmage_match = re.search(
        r'(?:if|elif) ! grep -Eiq \'([^\']+)\' "\$RUNNER_TEMP/changed-files\.txt"; then',
        workflow,
    )
    assert engine_match is not None
    assert xmage_match is not None

    engine_regex = re.compile(engine_match.group(1))
    xmage_regex = re.compile(xmage_match.group(1))

    for path in (
        "phase-patches/x.patch",
        "phase",
        "phase/crates/x.rs",
        "phase-env/src/main.rs",
        "phase/client/public/card-data-test.json",
    ):
        assert engine_regex.search(path), path

    for path in (
        "oracle/x",
        "oracle/scenarios/essence-burn.toml",
        "oracle/known-diffs.toml",
        "driver/diff_game.py",
    ):
        assert not engine_regex.search(path), path
        assert xmage_regex.search(path), path

    for path in ("driver/test_diff_game.py", "docs/x.md"):
        assert not engine_regex.search(path), path


def test_listed_packet_cards_route_before_changed_path_skip() -> None:
    workflow = WORKFLOW.read_text()
    validation = workflow.split(
        "- name: Validate changed scenario suites and known-diff coverage", 1
    )[1].split("\n      - name:", 1)[0]
    scope = workflow.split("- name: Detect XMage scope", 1)[1].split(
        "- name: Fail closed", 1
    )[0]
    listed_cards = scope.index('if [[ -n "$cards" && "${cards,,}" != "none" ]]')
    changed_path_check = scope.index("grep -Eiq", listed_cards)
    per_card_mode = scope.index('echo "mode=per-card"', listed_cards)
    assert listed_cards < per_card_mode < changed_path_check
    assert (
        'python scripts/run_card_scenarios.py --cards "$CARDS" '
        '--check-changed-suites "$RUNNER_TEMP/changed-files.txt" '
        "--known-diffs oracle/known-diffs.toml --base-known-diffs "
        '"$RUNNER_TEMP/base-known-diffs.toml"'
    ) in workflow
    assert (
        'git show "origin/$BASE_REF:oracle/known-diffs.toml" > '
        '"$RUNNER_TEMP/base-known-diffs.toml"'
    ) in workflow
    assert "BASE_REF: ${{ github.base_ref }}" in validation
    assert validation.index("git show") < validation.index(
        "python scripts/run_card_scenarios.py"
    )


def test_known_diff_registry_with_cards_none_fails_closed() -> None:
    workflow = WORKFLOW.read_text()
    scope = workflow.split("- name: Detect XMage scope", 1)[1].split(
        "- name: Fail closed", 1
    )[0]
    registry_path = "oracle/known-diffs.toml"
    smoke_guard = re.search(
        r"&& ! grep -Eiq '([^']+)' \"\$RUNNER_TEMP/changed-files\.txt\"",
        scope,
    )
    fail_closed_guard = re.search(
        r"if grep -Eiq '([^']+)' \"\$RUNNER_TEMP/changed-files\.txt\"; then"
        r'\s+echo "mode=fail-closed" >> "\$GITHUB_OUTPUT"'
        r'\s+echo "reason=cards-none-per-card"',
        scope,
    )

    assert smoke_guard is not None
    assert fail_closed_guard is not None
    assert re.compile(smoke_guard.group(1)).search(registry_path)
    assert re.compile(fail_closed_guard.group(1)).search(registry_path)
