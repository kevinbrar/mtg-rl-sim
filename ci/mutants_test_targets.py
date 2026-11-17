#!/usr/bin/env python3
"""Resolve changed Phase integration modules to their Cargo test targets."""

import argparse
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath
from typing import Sequence

MODULE_DECLARATION = re.compile(
    r"^\s*(?:(?:pub(?:\s*\([^)]*\))?)\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)
INTEGRATION_ROOT = PurePosixPath("tests/integration")


def _blank(source: str, chars: list[str], start: int, end: int) -> None:
    for index in range(start, end):
        if chars[index] not in "\r\n":
            chars[index] = " "


def _literal_end(source: str, start: int) -> int | None:
    raw_prefix = None
    if source.startswith("br", start):
        raw_prefix = 2
    elif source.startswith("r", start):
        raw_prefix = 1
    if raw_prefix is not None:
        index = start + raw_prefix
        while index < len(source) and source[index] == "#":
            index += 1
        if index < len(source) and source[index] == '"':
            hashes = source[start + raw_prefix : index]
            terminator = '"' + hashes
            end = source.find(terminator, index + 1)
            return len(source) if end == -1 else end + len(terminator)

    quote_start = start
    if source.startswith('b"', start):
        quote_start += 1
    if quote_start < len(source) and source[quote_start] == '"':
        index = quote_start + 1
        while index < len(source):
            if source[index] == "\\":
                index += 2
            elif source[index] == '"':
                return index + 1
            else:
                index += 1
        return len(source)

    if source[start] == "'":
        index = start + 1
        while index < len(source) and source[index] not in "\r\n":
            if source[index] == "\\":
                index += 2
            elif source[index] == "'":
                return index + 1 if index > start + 1 else None
            else:
                index += 1
    return None


def _without_rust_comments(source: str) -> str:
    chars = list(source)
    index = 0
    while index < len(source):
        literal_end = _literal_end(source, index)
        if literal_end is not None:
            _blank(source, chars, index, literal_end)
            index = literal_end
        elif source.startswith("//", index):
            end = source.find("\n", index)
            end = len(source) if end == -1 else end
            _blank(source, chars, index, end)
            index = end
        elif source.startswith("/*", index):
            depth = 1
            end = index + 2
            while end < len(source) and depth:
                if source.startswith("/*", end):
                    depth += 1
                    end += 2
                elif source.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            _blank(source, chars, index, end)
            index = end
        else:
            index += 1
    return "".join(chars)


def _test_target_sources(phase_root: Path) -> list[tuple[str, Path]]:
    engine_root = phase_root / "crates/engine"
    manifest_path = engine_root / "Cargo.toml"
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(
            f"cannot read engine Cargo manifest {manifest_path}: {error}"
        ) from error

    sources: dict[PurePosixPath, str] = {}
    package = manifest.get("package", {})
    main_path = INTEGRATION_ROOT / "main.rs"
    if package.get("autotests", True):
        sources[main_path] = "integration"

    for test in manifest.get("test", []):
        path = test.get("path")
        name = test.get("name")
        if not isinstance(path, str) or not isinstance(name, str):
            continue
        relative = PurePosixPath(path)
        if relative.parent != INTEGRATION_ROOT:
            continue
        existing_name = sources.get(relative)
        if existing_name is not None and existing_name != name:
            raise ValueError(
                f"Cargo test source {relative} maps to both {existing_name!r} and {name!r}"
            )
        sources[relative] = name

    result = []
    for relative, name in sources.items():
        source_path = engine_root / Path(*relative.parts)
        if not source_path.is_file():
            raise ValueError(
                f"Cargo integration test target {name!r} is missing {source_path}"
            )
        result.append((name, source_path))
    return sorted(result)


def resolve_test_targets(phase_root: Path, modules: Sequence[str]) -> list[str]:
    requested = sorted(set(modules))
    if not requested:
        return []

    declarations: dict[str, list[tuple[str, Path]]] = {
        module: [] for module in requested
    }
    for target, source_path in _test_target_sources(phase_root):
        source = source_path.read_text(encoding="utf-8")
        declared_modules = set(
            MODULE_DECLARATION.findall(_without_rust_comments(source))
        )
        for module in requested:
            if module in declared_modules:
                declarations[module].append((target, source_path))

    errors = []
    targets = set()
    for module, matches in declarations.items():
        distinct_targets = sorted({target for target, _ in matches})
        if not distinct_targets:
            errors.append(
                f"changed module {module!r} is declared in no integration test target"
            )
        elif len(distinct_targets) > 1:
            locations = ", ".join(
                f"{path} ({target})"
                for target, path in sorted(matches, key=lambda item: item[0])
            )
            errors.append(
                f"changed module {module!r} is declared in multiple targets: {locations}"
            )
        else:
            targets.add(distinct_targets[0])
    if errors:
        raise ValueError("; ".join(errors))
    return sorted(targets)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase_root", type=Path, help="root of the patched Phase tree")
    parser.add_argument(
        "--target-source-files",
        action="store_true",
        help="print integration target root paths instead of resolving modules",
    )
    parser.add_argument("modules", nargs="*", help="changed integration module names")
    args = parser.parse_args(argv)
    try:
        if args.target_source_files:
            if args.modules:
                raise ValueError(
                    "module names cannot be supplied with --target-source-files"
                )
            paths = [
                source.relative_to(args.phase_root).as_posix()
                for _, source in _test_target_sources(args.phase_root)
            ]
            print("\n".join(sorted(paths)))
        else:
            targets = resolve_test_targets(args.phase_root, args.modules)
            print("\n".join(targets))
    except (OSError, UnicodeError, ValueError) as error:
        print(f"mutants-test-targets: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
