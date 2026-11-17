from pathlib import Path

import pytest

from ci.mutants_test_targets import main, resolve_test_targets


def phase_tree(
    root: Path,
    *,
    main_source: str = "",
    test_targets: tuple[tuple[str, str, str], ...] = (),
) -> Path:
    engine = root / "crates/engine"
    integration = engine / "tests/integration"
    integration.mkdir(parents=True)
    (engine / "Cargo.toml").write_text(
        "[package]\nname = 'phase-engine'\nversion = '0.1.0'\n"
        + "".join(
            f'\n[[test]]\nname = "{name}"\npath = "{path}"\n'
            for name, path, _ in test_targets
        ),
        encoding="utf-8",
    )
    (integration / "main.rs").write_text(main_source, encoding="utf-8")
    for _, path, source in test_targets:
        (engine / path).write_text(source, encoding="utf-8")
    return root


def test_resolves_main_only_with_attributes_and_ignores_line_comment(
    tmp_path: Path,
) -> None:
    root = phase_tree(
        tmp_path,
        main_source='#[cfg(test)]\n#[path = "renamed.rs"]\npub(crate) mod main_case;\n'
        "// mod hidden;\n",
    )

    assert resolve_test_targets(root, ["main_case"]) == ["integration"]


def test_resolves_shard_only_from_cargo_manifest(tmp_path: Path) -> None:
    root = phase_tree(
        tmp_path,
        test_targets=(
            ("custom_cases", "tests/integration/shard_27.rs", "mod shard_case;\n"),
        ),
    )

    assert resolve_test_targets(root, ["shard_case"]) == ["custom_cases"]


def test_resolves_mixed_modules_to_sorted_unique_targets(tmp_path: Path) -> None:
    root = phase_tree(
        tmp_path,
        main_source="mod main_case;\n",
        test_targets=(
            ("z_cases", "tests/integration/shard_4.rs", "mod shard_case;\n"),
            ("a_cases", "tests/integration/shard_8.rs", "mod second_shard_case;\n"),
        ),
    )

    assert resolve_test_targets(
        root, ["shard_case", "main_case", "second_shard_case"]
    ) == [
        "a_cases",
        "integration",
        "z_cases",
    ]


def test_unregistered_module_exits_with_a_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = phase_tree(tmp_path)

    assert main([str(root), "missing_module"]) == 1
    assert (
        "changed module 'missing_module' is declared in no integration test target"
        in (capsys.readouterr().err)
    )


def test_duplicate_module_declarations_across_targets_are_rejected(
    tmp_path: Path,
) -> None:
    root = phase_tree(
        tmp_path,
        main_source="mod duplicated;\n",
        test_targets=(
            ("custom_cases", "tests/integration/shard_2.rs", "mod duplicated;\n"),
        ),
    )

    with pytest.raises(ValueError, match="declared in multiple targets"):
        resolve_test_targets(root, ["duplicated"])


def test_commented_out_module_declarations_are_ignored(tmp_path: Path) -> None:
    root = phase_tree(
        tmp_path,
        main_source="// mod commented_line;\n/*\nmod commented_block;\n*/\n",
    )

    with pytest.raises(ValueError, match="declared in no integration test target"):
        resolve_test_targets(root, ["commented_block"])
    with pytest.raises(ValueError, match="declared in no integration test target"):
        resolve_test_targets(root, ["commented_line"])


def test_lists_target_roots_for_workflow_filtering(tmp_path: Path, capsys) -> None:
    root = phase_tree(
        tmp_path,
        test_targets=(("custom_cases", "tests/integration/shard_2.rs", "mod case;\n"),),
    )

    assert main([str(root), "--target-source-files"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "crates/engine/tests/integration/main.rs",
        "crates/engine/tests/integration/shard_2.rs",
    ]
