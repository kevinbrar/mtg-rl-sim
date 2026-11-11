#!/usr/bin/env python3
"""Lint the required sections and evidence in a pull-request body."""

import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

REQUIRED_SECTIONS = (
    "## Brief / decision reference",
    "## Surfaces",
    "## Evidence",
    "## Not verified",
    "## Divergence from plan",
    "## Risk & rollback",
)
ENGINE_PATHS = (
    "phase-patches/",
    "phase/",
    "phase-env/",
    "oracle/",
    "driver/diff_game.py",
)
REQUIRED_EVIDENCE_COLUMNS = ("claim", "check", "result", "artifact", "independent")
CARDS_NONE_PER_CARD_ERROR = (
    "phase-patches/card-data/oracle-scenarios changes need per-card XMage scenarios; "
    "cards: none is not accepted for them"
)


def _section_lines(body: str, heading: str) -> list[str] | None:
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip().casefold() != heading.casefold():
            continue
        end = next(
            (
                following
                for following in range(index + 1, len(lines))
                if lines[following].startswith("## ")
            ),
            len(lines),
        )
        return lines[index + 1 : end]
    return None


def _split_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None
    stripped = stripped.removeprefix("|").removesuffix("|")
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells
    )


def _header_name(cell: str) -> str:
    return re.sub(r"\s*\([^)]*\)", "", cell).strip().casefold()


def _is_engine_path(path: str) -> bool:
    return path == "phase" or path.startswith(ENGINE_PATHS) or _is_card_data_path(path)


def _is_card_data_path(path: str) -> bool:
    return (
        re.fullmatch(r"(?:[^/]+/)*card-data[^/]*\.json", path, re.IGNORECASE)
        is not None
    )


def _is_per_card_xmage_path(path: str) -> bool:
    return path.startswith(
        ("phase-patches/", "oracle/scenarios/")
    ) or _is_card_data_path(path)


def _result_has_evidence(result: str) -> bool:
    value = result.strip()
    if not value:
        return False
    not_run = re.fullmatch(
        r"(?:not[\s_-]*run|skipped|skip|n\s*/\s*a|not applicable)"
        r"(?:\s*[:(\-—–](?P<reason>.*))?",
        value,
        re.IGNORECASE,
    )
    return not_run is None or bool(re.search(r"\d", not_run.group("reason") or ""))


def _has_artifact_reference(artifact: str) -> bool:
    return bool(
        re.search(r"https?://\S+|\[[^\]]+\]\([^)]+\)", artifact, re.IGNORECASE)
        or re.search(
            r"(?:^|[\s`])(?:/|\.{1,2}/)?(?:[\w.@+-]+/)*[\w.@+-]+\.[A-Za-z][\w+-]*\b",
            artifact,
            re.IGNORECASE,
        )
    )


def _evidence_table(
    lines: list[str],
) -> tuple[dict[str, int], int, list[list[str]]] | None:
    for index in range(len(lines) - 1):
        headers = _split_table_row(lines[index])
        separator = _split_table_row(lines[index + 1])
        if (
            headers is None
            or separator is None
            or len(headers) != len(separator)
            or not _is_separator(separator)
        ):
            continue
        normalized = [_header_name(header) for header in headers]
        if all(column in normalized for column in REQUIRED_EVIDENCE_COLUMNS):
            indexes = {
                column: normalized.index(column) for column in REQUIRED_EVIDENCE_COLUMNS
            }
            rows: list[list[str]] = []
            for line in lines[index + 2 :]:
                row = _split_table_row(line)
                if row is None:
                    break
                rows.append(row)
            return indexes, len(headers), rows
    return None


def parse_cards_line(body: str) -> str | None:
    for line in body.splitlines():
        match = re.match(r"^\s*cards:\s*(.*)$", line, re.IGNORECASE)
        if match is not None:
            return match.group(1).strip()
    return None


def validate_review_packet(body: str, changed_files: Sequence[str]) -> list[str]:
    errors: list[str] = []
    for heading in REQUIRED_SECTIONS:
        if _section_lines(body, heading) is None:
            errors.append(f"Missing required section: {heading}")

    evidence_lines = _section_lines(body, "## Evidence")
    evidence_table = _evidence_table(evidence_lines or [])
    if evidence_table is None:
        errors.append(
            "Evidence section must contain a Markdown table whose header includes "
            "Claim, Check, Result, Artifact, and Independent"
        )
    else:
        indexes, column_count, rows = evidence_table
        independent_yes = False
        for row_number, row in enumerate(rows, start=1):
            if len(row) != column_count:
                errors.append(
                    f"Evidence table row {row_number} must have {column_count} cells"
                )
                continue
            result = row[indexes["result"]].strip()
            artifact = row[indexes["artifact"]].strip()
            if _result_has_evidence(result) and not _has_artifact_reference(artifact):
                errors.append(
                    f"Evidence table row {row_number} has a result but "
                    "Artifact must contain a link or path"
                )
            if (
                row[indexes["independent"]].strip().casefold().startswith("y")
                and _result_has_evidence(result)
                and _has_artifact_reference(artifact)
            ):
                independent_yes = True

        needs_independent = any(_is_engine_path(path) for path in changed_files)
        if needs_independent and not independent_yes:
            errors.append(
                "Engine-path changes require an Evidence row whose Independent "
                "value starts with Y"
            )

    if any(_is_engine_path(path) for path in changed_files):
        cards = parse_cards_line(body)
        if cards is None:
            errors.append("Engine-path changes require a cards: line")
        else:
            entries = [entry.strip() for entry in cards.split(";")]
            if not cards or (
                cards.casefold() != "none"
                and (
                    any(not entry for entry in entries)
                    or "none" in map(str.casefold, entries)
                )
            ):
                errors.append(
                    "cards: must be `none` or a semicolon-separated list of card names"
                )
            if cards.casefold() == "none" and any(
                _is_per_card_xmage_path(path) for path in changed_files
            ):
                errors.append(CARDS_NONE_PER_CARD_ERROR)

    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--changed-files",
        type=Path,
        required=True,
        help="file containing one changed path per line",
    )
    args = parser.parse_args(argv)

    body = os.environ.get("PR_BODY", "")
    try:
        changed_files = [
            line.strip()
            for line in args.changed_files.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError as error:
        print(f"FAIL: cannot read changed files: {error}", file=sys.stderr)
        return 1

    errors = validate_review_packet(body, changed_files)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print("Review packet passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
