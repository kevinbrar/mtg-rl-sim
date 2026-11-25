import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "run_invariant_fuzz",
    Path(__file__).resolve().parent.parent / "scripts/run_invariant_fuzz.py",
)
fuzz = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fuzz)


def game(seed, violations, decisions=10):
    return {"seed": seed, "decisions": decisions, "violations": violations}


def test_known_violations_are_counted_but_do_not_fail():
    known = {"kind": "offered_action_rejected", "known": True, "detail": "x"}
    summary = fuzz.summarize({"a--b": [game(1, [known]), {"summary": True}]})
    assert summary["games"] == 1
    assert summary["decisions"] == 10
    assert summary["totals"] == {"known:offered_action_rejected": 1}
    assert summary["failures"] == []


def test_unknown_violation_and_panic_fail_with_pair_and_seed():
    unknown = {"kind": "zone", "known": False, "detail": "two zones"}
    panic = {"kind": "panic", "detail": "boom"}
    summary = fuzz.summarize(
        {"a--b": [game(3, [unknown])], "c--d": [{"seed": 9, "violations": [panic]}]}
    )
    assert summary["games"] == 2
    assert summary["totals"] == {"zone": 1, "panic": 1}
    assert [(f["pair"], f["seed"], f["kind"]) for f in summary["failures"]] == [
        ("a--b", 3, "zone"),
        ("c--d", 9, "panic"),
    ]
