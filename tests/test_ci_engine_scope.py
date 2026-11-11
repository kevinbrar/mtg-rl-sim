import re
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def test_ci_scope_regexes_cover_engine_and_xmage_paths() -> None:
    workflow = WORKFLOW.read_text()
    engine_match = re.search(
        r"^\s*engine_path_regex='([^']+)'$", workflow, flags=re.MULTILINE
    )
    xmage_match = re.search(
        r'elif ! grep -Eiq \'([^\']+)\' "\$RUNNER_TEMP/changed-files\.txt"; then',
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

    for path in ("oracle/x", "driver/diff_game.py"):
        assert not engine_regex.search(path), path
        assert xmage_regex.search(path), path

    for path in ("driver/test_diff_game.py", "docs/x.md"):
        assert not engine_regex.search(path), path
