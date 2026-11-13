import json
from pathlib import Path

import pytest

from ci.mutants_summary import main


def phase(name: str, status: object) -> dict:
    return {
        "argv": [],
        "duration": 1.0,
        "phase": name,
        "process_status": status,
    }


def baseline(summary: str = "Success", phases: list[dict] | None = None) -> dict:
    return {
        "diff_path": None,
        "log_path": "log/baseline.log",
        "phase_results": phases
        if phases is not None
        else [phase("Build", "Success"), phase("Test", "Success")],
        "scenario": "Baseline",
        "summary": summary,
    }


def mutant(name: str, summary: str, phases: list[dict]) -> dict:
    return {
        "diff_path": f"diff/{name}.diff",
        "log_path": f"log/{name}.log",
        "phase_results": phases,
        "scenario": {
            "Mutant": {
                "file": "crates/engine/src/lib.rs",
                "name": name,
                "package": "phase-engine",
                "replacement": "false",
            }
        },
        "summary": summary,
    }


def document(*outcomes: dict) -> dict:
    mutants = [outcome for outcome in outcomes if outcome["scenario"] != "Baseline"]
    return {
        "cargo_mutants_version": "27.1.0",
        "caught": sum(outcome["summary"] == "CaughtMutant" for outcome in mutants),
        "end_time": "2026-11-13T00:00:01Z",
        "missed": sum(outcome["summary"] == "MissedMutant" for outcome in mutants),
        "outcomes": list(outcomes),
        "start_time": "2026-11-13T00:00:00Z",
        "success": 1,
        "timeout": sum(outcome["summary"] == "Timeout" for outcome in mutants),
        "total_mutants": len(mutants),
        "unviable": sum(outcome["summary"] == "Unviable" for outcome in mutants),
    }


def output_dir(path: Path, outcomes: dict, missed: str = "") -> Path:
    path.mkdir()
    (path / "outcomes.json").write_text(json.dumps(outcomes), encoding="utf-8")
    (path / "missed.txt").write_text(missed, encoding="utf-8")
    return path


def run_summary(path: Path, threshold: int = 0) -> int:
    return main([str(path), "--threshold", str(threshold)])


def test_all_caught_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    outcomes = document(
        baseline(),
        mutant(
            "caught-a",
            "CaughtMutant",
            [phase("Build", "Success"), phase("Test", {"Failure": 101})],
        ),
        mutant(
            "caught-b",
            "CaughtMutant",
            [phase("Build", "Success"), phase("Test", {"Failure": 101})],
        ),
    )
    path = output_dir(tmp_path / "mutants", outcomes)

    assert run_summary(path) == 0
    assert "total=2 caught=2 missed=0" in capsys.readouterr().out


def test_missed_mutant_over_threshold_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = "crates/engine/src/lib.rs:1:1: replace true with false"
    outcomes = document(
        baseline(),
        mutant(
            name,
            "MissedMutant",
            [phase("Build", "Success"), phase("Test", "Success")],
        ),
    )
    path = output_dir(tmp_path / "mutants", outcomes, missed=f"{name}\n")

    assert run_summary(path) == 1
    captured = capsys.readouterr()
    assert f"MISSED {name}" in captured.out
    assert "::error::missed mutant count 1 exceeds threshold 0" in captured.err


def test_build_timeout_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = "crates/engine/src/lib.rs:2:1: replace true with false"
    outcomes = document(
        baseline(),
        mutant(name, "Timeout", [phase("Build", "Timeout")]),
    )
    path = output_dir(tmp_path / "mutants", outcomes)

    assert run_summary(path) == 1
    captured = capsys.readouterr()
    assert "build-timeout=1" in captured.out
    assert f"BUILD-TIMEOUT {name}" in captured.out
    assert "::error::1 mutant build phase(s) timed out" in captured.err


def test_build_phase_timeout_fails_even_if_summary_disagrees(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = "misclassified-build-timeout"
    outcomes = document(
        baseline(),
        mutant(name, "CaughtMutant", [phase("Build", "Timeout")]),
    )
    path = output_dir(tmp_path / "mutants", outcomes)

    assert run_summary(path) == 1
    captured = capsys.readouterr()
    assert f"BUILD-TIMEOUT {name}" in captured.out
    assert f"::error::{name} has a Build timeout but summary 'CaughtMutant'" in (
        captured.err
    )


def test_test_timeout_is_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outcomes = document(
        baseline(),
        mutant(
            "test-timeout",
            "Timeout",
            [phase("Build", "Success"), phase("Test", "Timeout")],
        ),
    )
    path = output_dir(tmp_path / "mutants", outcomes)

    assert run_summary(path) == 0
    assert "test-timeout=1 build-timeout=0" in capsys.readouterr().out


def test_zero_mutants_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = output_dir(tmp_path / "mutants", document(baseline()))

    assert run_summary(path) == 0
    assert "total=0 caught=0 missed=0" in capsys.readouterr().out


def test_declared_total_must_match_outcome_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outcomes = document(
        baseline(),
        mutant(
            "caught",
            "CaughtMutant",
            [phase("Build", "Success"), phase("Test", {"Failure": 101})],
        ),
    )
    outcomes["total_mutants"] = 0
    path = output_dir(tmp_path / "mutants", outcomes)

    assert run_summary(path) == 1
    assert (
        "::error::outcomes.json declares 0 mutant(s) but contains 1 "
        "non-baseline outcome(s)"
    ) in capsys.readouterr().err


def test_missing_outcomes_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "mutants"
    path.mkdir()
    (path / "missed.txt").write_text("", encoding="utf-8")

    assert run_summary(path) == 1
    assert "::error::cannot read" in capsys.readouterr().err


def test_missed_txt_disagreement_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = "missed-mutant"
    outcomes = document(
        baseline(),
        mutant(
            name,
            "MissedMutant",
            [phase("Build", "Success"), phase("Test", "Success")],
        ),
    )
    path = output_dir(tmp_path / "mutants", outcomes)

    assert run_summary(path, threshold=1) == 1
    assert "missed.txt line count 0 disagrees with outcomes.json count 1" in (
        capsys.readouterr().err
    )


def test_failed_baseline_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = output_dir(
        tmp_path / "mutants",
        document(
            baseline(
                "Timeout",
                [phase("Build", "Timeout")],
            )
        ),
    )

    assert run_summary(path) == 1
    assert "::error::unmutated baseline failed" in capsys.readouterr().err


def test_unparseable_outcomes_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "mutants"
    path.mkdir()
    (path / "outcomes.json").write_text("{", encoding="utf-8")
    (path / "missed.txt").write_text("", encoding="utf-8")

    assert run_summary(path) == 1
    assert "::error::cannot read" in capsys.readouterr().err
