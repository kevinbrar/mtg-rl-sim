#!/usr/bin/env python3
"""Check aggregate Cargo test results in a captured test log."""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path


RESULT_RE = re.compile(
    r"test result:\s+(?P<status>\S+)\.\s+"
    r"(?P<passed>\d+)\s+passed;\s+(?P<failed>\d+)\s+failed;"
)
RUNNING_RE = re.compile(r"Running (.+?) \(target/")


def summarize(log_text: str) -> tuple[dict[str, int], list[str]]:
    counts: dict[str, int] = defaultdict(int)
    errors: list[str] = []
    current_target = "unattributed target"
    result_count = 0

    for line_number, line in enumerate(log_text.splitlines(), start=1):
        stripped = line.strip()
        running = RUNNING_RE.search(stripped)
        if running:
            current_target = running.group(1)
        elif stripped.startswith("Doc-tests "):
            current_target = stripped.removeprefix("Doc-tests ")

        if "test result:" not in line:
            continue

        result_count += 1
        result = RESULT_RE.search(line)
        if not result:
            errors.append(
                f"line {line_number}: could not parse test result: {stripped}"
            )
            continue

        status = result.group("status")
        passed = int(result.group("passed"))
        failed = int(result.group("failed"))
        counts[current_target] += passed
        if status != "ok" or failed:
            errors.append(
                f"line {line_number}: {current_target} result was "
                f"{status} ({passed} passed, {failed} failed)"
            )

    if result_count == 0:
        errors.append("no test result lines found")

    return dict(counts), errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="captured cargo test output")
    parser.add_argument("--min", type=int, required=True, help="minimum passed tests")
    args = parser.parse_args()

    try:
        log_text = args.log.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        print(f"FAIL: cannot read {args.log}: {error}", file=sys.stderr)
        return 1

    counts, errors = summarize(log_text)
    total = sum(counts.values())

    print("Passed tests by binary:")
    for target, count in counts.items():
        print(f"  {target}: {count:,}")
    print(f"Passed total: {total:,} (minimum {args.min:,})")

    if total < args.min:
        errors.append(f"passed-test total {total} is below minimum {args.min}")
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
