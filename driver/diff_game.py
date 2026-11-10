"""Run one seeded, lockstep phase-rs/XMage differential game."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from phase_env import PhaseEnv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAGE_BENCH = Path("/home/ubuntu/repos/eng/mage-bench")
PLAYER_NAMES = ("DiffA", "DiffB")
MAX_TURNS = 30
TERMINAL_STATUSES = frozenset(
    ("completed", "completed_terminal_projection_unavailable")
)
MISSING = ("missing",)
COUNTER_NAMES = {
    "+1/+1": "+1/+1",
    "p1p1": "+1/+1",
    "plus1plus1": "+1/+1",
    "-1/-1": "-1/-1",
    "m1m1": "-1/-1",
    "minus1minus1": "-1/-1",
    "age": "age",
    "charge": "charge",
    "defense": "defense",
    "energy": "energy",
    "experience": "experience",
    "fade": "fade",
    "finality": "finality",
    "first strike": "first strike",
    "double strike": "double strike",
    "deathtouch": "deathtouch",
    "vigilance": "vigilance",
    "hexproof": "hexproof",
    "lifelink": "lifelink",
    "decayed": "decayed",
    "exalted": "exalted",
    "trample": "trample",
    "flying": "flying",
    "menace": "menace",
    "shadow": "shadow",
    "haste": "haste",
    "reach": "reach",
    "indestructible": "indestructible",
    "flood": "flood",
    "level": "level",
    "loyalty": "loyalty",
    "lore": "lore",
    "oil": "oil",
    "poison": "poison",
    "quest": "quest",
    "rad": "rad",
    "shield": "shield",
    "stun": "stun",
    "time": "time",
}


def _reexec_with_mage_bench_python() -> None:
    if os.environ.get("DIFF_GAME_MCP_REEXEC"):
        return
    mage_bench = Path(os.environ.get("MAGE_BENCH_ROOT", DEFAULT_MAGE_BENCH))
    interpreter = mage_bench / ".venv" / "bin" / "python"
    if not interpreter.is_file():
        raise RuntimeError(f"Mage-bench Python environment not found: {interpreter}")
    if Path(sys.prefix) == interpreter.parent.parent:
        return
    env = os.environ.copy()
    env["DIFF_GAME_MCP_REEXEC"] = "1"
    os.execve(interpreter, [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]], env)


def normalize_name(value: str) -> str:
    front = value.split("//", 1)[0].strip()
    decomposed = unicodedata.normalize("NFKD", front)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.casefold().split())


def summary_exit_code(summary: dict) -> int:
    if summary.get("status") == "unmapped_prompt":
        return 2
    winner = summary.get("winner")
    if (
        summary.get("status") not in TERMINAL_STATUSES
        or summary.get("divergences") != 0
        or not isinstance(winner, dict)
        or winner.get("phase") != winner.get("xmage")
    ):
        return 1
    return 0


class UnmappedPromptError(RuntimeError):
    pass


class ObjectBindings:
    def __init__(self) -> None:
        self.phase_to_xmage: dict[tuple[int, int], str] = {}
        self.xmage_to_phase: dict[str, tuple[int, int]] = {}

    def bind(self, phase_id: int, incarnation: int, xmage_id: str) -> None:
        phase_identity = (phase_id, incarnation)
        phase_bound = self.phase_to_xmage.get(phase_identity)
        xmage_bound = self.xmage_to_phase.get(xmage_id)
        if (phase_bound not in (None, xmage_id)) or (
            xmage_bound not in (None, phase_identity)
        ):
            raise UnmappedPromptError(
                f"Object binding is not bijective: phase={phase_identity}, "
                f"xmage={xmage_id}, phase_to_xmage={self.phase_to_xmage}, "
                f"xmage_to_phase={self.xmage_to_phase}"
            )
        self.phase_to_xmage[phase_identity] = xmage_id
        self.xmage_to_phase[xmage_id] = phase_identity

    def prune(self, phase_state: dict, xmage_state: dict) -> None:
        phase_battlefield = phase_state.get("battlefield")
        players = _xmage_players(xmage_state)
        if not isinstance(phase_battlefield, list) or not players:
            raise UnmappedPromptError(
                f"Cannot prune object bindings without both battlefield views: "
                f"phase={phase_state}, xmage={xmage_state}"
            )
        phase_incarnations = {}
        for item in phase_battlefield:
            if not isinstance(item, dict) or item.get("id") is None:
                raise UnmappedPromptError(
                    f"Invalid Phase battlefield object metadata: {item}"
                )
            incarnation = item.get("incarnation")
            if not isinstance(incarnation, int) or isinstance(incarnation, bool):
                raise UnmappedPromptError(
                    f"Missing or invalid Phase battlefield incarnation: {item}"
                )
            phase_incarnations[item["id"]] = incarnation
        xmage_ids = set()
        for player in players:
            for permanent in player.get("battlefield", []):
                short_id = permanent.get("id")
                if short_id is None:
                    raise UnmappedPromptError(
                        f"XMage battlefield permanent omitted its stable id: {permanent}"
                    )
                xmage_ids.add(str(short_id))
        for phase_identity, xmage_id in list(self.phase_to_xmage.items()):
            phase_id, incarnation = phase_identity
            if (
                phase_incarnations.get(phase_id) != incarnation
                or xmage_id not in xmage_ids
            ):
                del self.phase_to_xmage[phase_identity]
                del self.xmage_to_phase[xmage_id]


def _unmapped_prompt_result(
    run_dir: Path,
    run_id: str,
    seed: int,
    step: int,
    reason: str,
    decision: dict,
    xmage_result: dict,
    phase_projection: dict,
    xmage_projection: dict,
) -> dict:
    report = {
        "run_id": run_id,
        "seed": seed,
        "status": "unmapped_prompt",
        "kind": "unmapped_prompt",
        "step": step,
        "reason": reason,
        "prompts": {
            "phase": {
                "waiting_for": decision.get("waiting_for"),
                "objects": decision.get("objects"),
                "state": decision.get("state"),
                "candidates": decision.get("candidates"),
            },
            "xmage": xmage_result,
        },
        "last_state": {
            "phase": phase_projection,
            "xmage": xmage_projection,
        },
    }
    (run_dir / "divergence.json").write_text(json.dumps(report, indent=2) + "\n")
    return {
        "type": "unmapped_prompt",
        "status": "unmapped_prompt",
        "run_dir": run_dir,
        "step": step,
        "reason": reason,
    }


def expand_deck(path: Path) -> list[str]:
    entries = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        entries.append(stripped)
    cards = PhaseEnv._expand(entries)
    if len(cards) != 60:
        raise ValueError(f"{path} contains {len(cards)} cards; the v0 deck must contain 60")
    return cards


def read_printings() -> dict[str, dict]:
    return json.loads((ROOT / "oracle" / "decks" / "scryfall-printings.json").read_text())


def validate_v0_deck(cards: list[str], printings: dict[str, dict], path: Path) -> None:
    counts = Counter(normalize_name(card) for card in cards)
    basics = set()
    creatures = set()
    for name, count in counts.items():
        printing = printings.get(name)
        if printing is None:
            raise ValueError(f"No cached Scryfall printing for {name!r} in {path}")
        if printing["standard"] != "legal":
            raise ValueError(f"{printing['name']} is not Standard-legal")
        type_line = printing["type_line"]
        if "Basic Land" in type_line:
            basics.add(name)
            continue
        if "Creature" not in type_line:
            raise ValueError(f"{printing['name']} is not a basic land or creature")
        if re.search(
            r"\b(when|whenever|target|choose|you may)\b|at the beginning",
            printing["oracle_text"],
            re.IGNORECASE,
        ):
            raise ValueError(f"{printing['name']} has unsupported triggered or choice text")
        creatures.add(name)
    basic_count = sum(counts[name] for name in basics)
    creature_count = sum(counts[name] for name in creatures)
    if basic_count != 24 or creature_count != 36 or not 4 <= len(creatures) <= 6 or len(basics) != 1:
        raise ValueError(
            f"{path} must contain 24 of one basic land and 36 copies across 4–6 creatures"
        )


def write_xmage_deck(cards: list[str], printings: dict[str, dict], path: Path, *, reverse: bool) -> None:
    sequence = list(reversed(cards)) if reverse else cards
    lines = []
    for card in sequence:
        printing = printings.get(normalize_name(card))
        if printing is None:
            raise ValueError(f"No cached Scryfall printing for {card!r}")
        lines.append(f"1 [{printing['set']}:{printing['collector_number']}] {card}")
    path.write_text("\n".join(lines) + "\n")


def projection_from_phase(state: dict) -> dict:
    return {
        "players": [
            {
                "life": player["life"],
                "hand": sorted(normalize_name(name) for name in player["hand"]),
                "library_count": player["library_count"],
                "graveyard": sorted(normalize_name(name) for name in player["graveyard"]),
            }
            for player in state["players"]
        ],
        "battlefield": sorted(
            (
                normalize_name(item["name"]),
                item["controller"],
                item["tapped"],
            )
            for item in state["battlefield"]
        ),
        "turn": state["turn"],
        "active_player": state["active_player"],
    }


def projection_from_xmage(states: list[dict]) -> dict:
    available = [state for state in states if state.get("available")]
    if not available:
        raise RuntimeError(f"XMage has no published game state: {states}")
    state = available[0]
    player_states: list[dict] = []
    seat_by_name = {name: index for index, name in enumerate(PLAYER_NAMES)}
    by_name: dict[str, dict] = {}
    for seat, per_seat in enumerate(states):
        if not per_seat.get("available"):
            continue
        for player in per_seat.get("players", []):
            name = player.get("name")
            if name in PLAYER_NAMES and name == PLAYER_NAMES[seat]:
                by_name[name] = player
            elif name in PLAYER_NAMES:
                by_name.setdefault(name, player)
    for name in PLAYER_NAMES:
        player = by_name.get(name)
        if player is None:
            raise RuntimeError(f"XMage state omitted player {name}: {state}")
        player_states.append(
            {
                "life": player["life"],
                "hand": sorted(
                    normalize_name(card["name"] if isinstance(card, dict) else card)
                    for card in player.get("hand") or []
                ),
                "library_count": player["library_size"],
                "graveyard": sorted(
                    normalize_name(card["name"] if isinstance(card, dict) else card)
                    for card in player.get("graveyard") or []
                ),
            }
        )
    battlefield = []
    for name in PLAYER_NAMES:
        player = by_name[name]
        for permanent in player.get("battlefield") or []:
            battlefield.append(
                (
                    normalize_name(permanent["name"]),
                    seat_by_name[name],
                    bool(permanent.get("tapped", False)),
                )
            )
    active = state.get("active_player")
    active_seat = seat_by_name.get(active, active)
    return {
        "players": player_states,
        "battlefield": sorted(battlefield),
        "turn": _normalize_xmage_turn(state["turn"], active_seat),
        "active_player": active_seat,
    }


def compare_projection(left: dict, right: dict) -> dict:
    diff = {}
    for key in left:
        if left[key] != right.get(key):
            diff[key] = {"phase": left[key], "xmage": right.get(key)}
    return diff


def _policy_candidate_v0(decision: dict, rng: random.Random) -> tuple[int, dict]:
    candidates = decision["candidates"]
    attacks = [
        (index, action)
        for index, action in enumerate(candidates)
        if action.get("type") == "DeclareAttackers"
        and action.get("data", {}).get("attacks")
    ]
    if attacks:
        empty = [
            (index, action)
            for index, action in enumerate(candidates)
            if action.get("type") == "DeclareAttackers"
            and not action.get("data", {}).get("attacks")
        ]
        full = max(attacks, key=lambda item: len(item[1]["data"]["attacks"]))
        choices = empty + [full] if empty else [full]
        return rng.choice(choices)

    blockers = [
        (index, action)
        for index, action in enumerate(candidates)
        if action.get("type") == "DeclareBlockers"
        and action.get("data", {}).get("assignments")
    ]
    if blockers:
        empty = [
            (index, action)
            for index, action in enumerate(candidates)
            if action.get("type") == "DeclareBlockers"
            and not action.get("data", {}).get("assignments")
        ]
        if not empty:
            raise UnmappedPromptError(f"No no-blockers action in phase candidates: {decision}")
        return empty[0]

    supported: list[tuple[int, dict]] = []
    for index, action in enumerate(candidates):
        kind = action.get("type")
        if kind in {"PlayLand", "CastSpell", "PassPriority", "SelectCards"}:
            supported.append((index, action))
    if not supported:
        raise UnmappedPromptError(f"No supported phase action at decision: {decision}")
    return rng.choice(supported)


def policy_candidate(
    decision: dict, rng: random.Random, *, v0: bool = False
) -> tuple[int, dict]:
    if v0:
        return _policy_candidate_v0(decision, rng)
    candidates = list(enumerate(decision["candidates"]))
    preferred = [
        item
        for item in candidates
        if item[1].get("type") in {"CastSpell", "ActivateAbility"}
    ]
    other = [item for item in candidates if item not in preferred]
    if preferred and other:
        return rng.choice(preferred if rng.random() < 0.8 else other)
    return rng.choice(candidates)


def _is_meaningful_decision(decision: dict) -> bool:
    for action in decision["candidates"]:
        kind = action.get("type")
        data = action.get("data", {})
        if kind in {"PlayLand", "CastSpell"}:
            return True
        if kind == "DeclareAttackers" and data.get("attacks"):
            return True
        if kind == "DeclareBlockers" and data.get("assignments"):
            return True
        if kind not in {"PassPriority", "DeclareAttackers", "DeclareBlockers"}:
            return True
    return False


def _trivial_action_index(decision: dict) -> tuple[int, dict]:
    for index, action in enumerate(decision["candidates"]):
        if action.get("type") == "PassPriority":
            return index, action
    for index, action in enumerate(decision["candidates"]):
        if action.get("type") == "DeclareAttackers" and not action.get("data", {}).get("attacks"):
            return index, action
        if action.get("type") == "DeclareBlockers" and not action.get("data", {}).get("assignments"):
            return index, action
    raise UnmappedPromptError(f"Trivial phase decision has no pass or empty combat action: {decision}")


def _card_action_set(decision: dict) -> Counter:
    names_by_id = {
        card["id"]: card["name"]
        for player in decision["state"]["players"]
        for card in player.get("hand_cards", [])
    }
    actions = []
    for action in decision["candidates"]:
        kind = action.get("type")
        if kind not in {"PlayLand", "CastSpell"}:
            continue
        data = action.get("data", {})
        object_id = data.get("object_id", data.get("card_id"))
        name = names_by_id.get(object_id)
        if name is None:
            raise UnmappedPromptError(f"Could not map phase candidate to hand card: {action}, {decision['state']}")
        actions.append(("land" if kind == "PlayLand" else "cast", normalize_name(name)))
    return Counter(actions)


def _xmage_players(state: dict) -> list[dict]:
    players = state.get("players", state.get("board", []))
    return players if isinstance(players, list) else []


def _xmage_battlefield_by_id(state: dict) -> dict[str, tuple[dict, dict]]:
    entries = {}
    for player in _xmage_players(state):
        for permanent in player.get("battlefield", []):
            if not isinstance(permanent, dict) or permanent.get("id") is None:
                continue
            short_id = str(permanent["id"])
            if short_id in entries:
                raise UnmappedPromptError(
                    f"Duplicate XMage battlefield id {short_id}: "
                    f"{entries[short_id]}, {(player, permanent)}"
                )
            entries[short_id] = (player, permanent)
    return entries


def _counter_signature(counters: object, side: str) -> tuple:
    if not isinstance(counters, dict):
        raise UnmappedPromptError(f"Invalid {side} counter metadata: {counters}")
    normalized = []
    for name, count in counters.items():
        key = normalize_name(str(name))
        if key not in COUNTER_NAMES and not re.fullmatch(r"[+-]\d+/[+-]\d+", key):
            raise UnmappedPromptError(
                f"Unknown {side} counter name {name!r}; counters={counters}"
            )
        if not isinstance(count, int) or isinstance(count, bool):
            raise UnmappedPromptError(
                f"Invalid {side} counter count for {name!r}: {count!r}"
            )
        normalized.append((COUNTER_NAMES.get(key, key), count))
    return tuple(sorted(normalized))


def _attached_signature(
    obj: dict,
    side: str,
    decision: dict,
    xmage_state: dict,
    bindings: ObjectBindings,
    player: dict | None = None,
) -> object:
    if "attached_to" not in obj:
        return None if side == "xmage" else MISSING
    attached_to = obj["attached_to"]
    if attached_to is None:
        return None
    if side == "phase":
        try:
            phase_attached_id = int(attached_to)
        except (TypeError, ValueError) as exc:
            raise UnmappedPromptError(
                f"Invalid Phase attachment id {attached_to!r}: {obj}"
            ) from exc
        attached = decision.get("objects", {}).get(str(attached_to))
        if not isinstance(attached, dict):
            raise UnmappedPromptError(
                f"Phase attachment metadata is missing for {attached_to}: {obj}"
            )
        attached_incarnation = attached.get("incarnation")
        if not isinstance(attached_incarnation, int) or isinstance(
            attached_incarnation, bool
        ):
            raise UnmappedPromptError(
                f"Missing Phase attachment incarnation: {attached}"
            )
        bound_id = bindings.phase_to_xmage.get(
            (phase_attached_id, attached_incarnation)
        )
        if bound_id is not None:
            return ("bound", bound_id)
        return (
            "unbound",
            _object_signature(
                attached,
                "phase",
                decision,
                xmage_state,
                bindings,
                include_attachment=False,
            ),
        )
    attached_id = str(attached_to)
    phase_id = bindings.xmage_to_phase.get(attached_id)
    if phase_id is not None:
        return ("bound", attached_id)
    entry = _xmage_battlefield_by_id(xmage_state).get(attached_id)
    if entry is None:
        raise UnmappedPromptError(
            f"XMage attachment id {attached_id} is absent from the battlefield: "
            f"state={xmage_state}"
        )
    attached_player, attached = entry
    if attached_player.get("name") not in PLAYER_NAMES:
        raise UnmappedPromptError(f"Unknown XMage attachment controller: {entry}")
    return (
        "unbound",
        _object_signature(
            attached,
            "xmage",
            decision,
            xmage_state,
            bindings,
            player=attached_player,
            include_attachment=False,
        ),
    )


def _object_signature(
    obj: dict,
    side: str,
    decision: dict,
    xmage_state: dict,
    bindings: ObjectBindings,
    player: dict | None = None,
    include_attachment: bool = True,
) -> tuple:
    if not isinstance(obj.get("name"), str) or "name" not in obj:
        raise UnmappedPromptError(f"Missing {side} object name: {obj}")
    if side == "phase":
        controller = obj.get("controller", MISSING)
        if not isinstance(controller, int) or isinstance(controller, bool):
            raise UnmappedPromptError(f"Invalid Phase object controller: {obj}")
    else:
        if player is None or player.get("name") not in PLAYER_NAMES:
            raise UnmappedPromptError(f"Unknown XMage object controller: {player}")
        controller = PLAYER_NAMES.index(player["name"])

    signature = [
        normalize_name(str(obj.get("name", ""))),
        controller,
    ]
    for field in ("tapped", "power", "toughness", "damage", "summoning_sick"):
        if field not in obj:
            signature.append(MISSING)
        elif field in {"power", "toughness"}:
            value = obj[field]
            if isinstance(value, bool):
                raise UnmappedPromptError(f"Invalid {side} {field}: {obj}")
            try:
                signature.append(int(value))
            except (TypeError, ValueError):
                signature.append(
                    None if value is None else normalize_name(str(value))
                )
        elif field in {"tapped", "summoning_sick"}:
            if not isinstance(obj[field], bool):
                raise UnmappedPromptError(f"Invalid {side} {field}: {obj}")
            signature.append(obj[field])
        elif field == "damage":
            if not isinstance(obj[field], int) or isinstance(obj[field], bool):
                raise UnmappedPromptError(f"Invalid {side} damage: {obj}")
            signature.append(obj[field])
    signature.append(
        _counter_signature(obj["counters"], side)
        if "counters" in obj
        else MISSING
    )
    for field in ("token", "face_down"):
        if field in obj and not isinstance(obj[field], bool):
            raise UnmappedPromptError(f"Invalid {side} {field}: {obj}")
        signature.append(obj[field] if field in obj else MISSING)
    if include_attachment:
        signature.append(
            _attached_signature(
                obj, side, decision, xmage_state, bindings, player=player
            )
        )
    return tuple(signature)


def _short_id_sequence(short_id: str) -> int:
    match = re.fullmatch(r"[pl](\d+)", short_id)
    if not match:
        raise UnmappedPromptError(f"Unrecognized XMage short id: {short_id}")
    return int(match.group(1))


def _resolve_battlefield_choice(
    kind: str,
    key: tuple,
    phase_id: int,
    phase_object: dict,
    decision: dict,
    result: dict,
    matching: list[dict],
    bindings: ObjectBindings | None,
    xmage_state: dict | None,
) -> dict:
    if bindings is None:
        return _unique_semantic_choice(kind, key, decision, result, matching)
    if not matching:
        return _unique_semantic_choice(kind, key, decision, result, matching)

    phase_battlefield_incarnations = {
        item.get("id"): item.get("incarnation")
        for item in decision.get("state", {}).get("battlefield", [])
        if isinstance(item, dict)
    }
    incarnation = phase_object.get("incarnation")
    if not isinstance(incarnation, int) or isinstance(incarnation, bool):
        raise UnmappedPromptError(
            f"Missing Phase object incarnation while resolving {kind}: "
            f"phase_object={phase_object}"
        )
    phase_identity = (phase_id, incarnation)
    bound_id = bindings.phase_to_xmage.get(phase_identity)
    if bound_id is not None:
        choice = next(
            (item for item in matching if str(item.get("id")) == bound_id), None
        )
        if choice is None:
            raise UnmappedPromptError(
                f"Bound XMage object is not among matching {kind} choices: "
                f"phase_id={phase_identity}, bound_id={bound_id}, "
                f"phase_choices={decision.get('candidates', [])}; "
                f"xmage_choices={result.get('choices', [])}"
            )
        return choice

    if (
        phase_battlefield_incarnations.get(phase_id) != incarnation
        or xmage_state is None
    ):
        raise UnmappedPromptError(
            f"Battlefield state is missing while resolving {kind}: "
            f"phase_id={phase_identity}, "
            f"phase_choices={decision.get('candidates', [])}; "
            f"xmage_choices={result.get('choices', [])}; xmage_state={xmage_state}"
        )
    xmage_battlefield = _xmage_battlefield_by_id(xmage_state)
    for choice in matching:
        choice_id = choice.get("id")
        if choice_id is None or str(choice_id) not in xmage_battlefield:
            raise UnmappedPromptError(
                f"Ambiguous {kind} choice id is absent from get_game_state "
                f"battlefields: choice={choice}; xmage_choices={result.get('choices', [])}; "
                f"get_game_state={xmage_state}"
            )

    available = [
        choice
        for choice in matching
        if str(choice["id"]) not in bindings.xmage_to_phase
    ]
    if len(matching) == 1:
        choice = matching[0]
        bindings.bind(phase_id, incarnation, str(choice["id"]))
        return choice

    phase_signature = _object_signature(
        phase_object, "phase", decision, xmage_state, bindings
    )
    xmage_signatures = []
    for choice in available:
        player, permanent = xmage_battlefield[str(choice["id"])]
        signature = _object_signature(
            permanent, "xmage", decision, xmage_state, bindings, player=player
        )
        xmage_signatures.append((choice, signature))
    equivalent = [
        (choice, signature)
        for choice, signature in xmage_signatures
        if signature == phase_signature
    ]
    if not equivalent:
        raise UnmappedPromptError(
            f"No matching {kind} object signature for phase_id={phase_identity}: "
            f"phase_signatures={[{'id': phase_identity, 'signature': phase_signature}]}; "
            f"xmage_signatures={[{'id': choice.get('id'), 'signature': signature} for choice, signature in xmage_signatures]}; "
            f"phase_choices={decision.get('candidates', [])}; "
            f"xmage_choices={result.get('choices', [])}"
        )
    choice, _ = min(
        equivalent, key=lambda item: _short_id_sequence(str(item[0]["id"]))
    )
    bindings.bind(phase_id, incarnation, str(choice["id"]))
    return choice


def _phase_ability_key(action: dict, decision: dict) -> tuple:
    data = action.get("data", {})
    source = decision.get("objects", {}).get(str(data.get("source_id")), {})
    ability = next(
        (
            item
            for item in source.get("abilities", [])
            if item.get("index") == data.get("ability_index")
        ),
        {},
    )
    return (
        normalize_name(source.get("name", "")),
        source.get("controller"),
        normalize_name(source.get("zone", "")),
        normalize_name(ability.get("text") or ""),
    )


def _xmage_ability_key(
    choice: dict,
    result: dict,
    ability_text: str | None = None,
    xmage_state: dict | None = None,
) -> tuple:
    name = normalize_name(choice.get("name", ""))
    sources = set()
    for player in _xmage_players(xmage_state or result):
        if player.get("name") not in PLAYER_NAMES:
            continue
        for zone, cards in player.items():
            if not isinstance(cards, list):
                continue
            if zone not in {"battlefield", "hand", "graveyard", "exile", "command"}:
                continue
            if any(
                card.get("id") == choice.get("id")
                and normalize_name(card.get("name", "")) == name
                for card in cards
            ):
                sources.add((PLAYER_NAMES.index(player["name"]), normalize_name(zone)))
    if len(sources) != 1:
        return name, None, None, ""
    controller, zone = sources.pop()
    if ability_text is None:
        abilities = choice.get("playable_abilities", [])
        ability_text = abilities[0] if len(abilities) == 1 else ""
    text = ability_text
    if "<i>" in text:
        text = text.split("<i>", 1)[0]
    text = re.sub(r"<[^>]*>", "", text).removesuffix("...")
    return name, controller, zone, normalize_name(text)


def _unique_semantic_choice(
    kind: str,
    key: tuple | str,
    decision: dict,
    result: dict,
    matching: list[dict],
) -> dict:
    if len(matching) != 1:
        reason = "Ambiguous" if matching else "No"
        raise UnmappedPromptError(
            f"{reason} {kind} semantic match for {key}: "
            f"phase_choices={decision.get('candidates', [])}; "
            f"xmage_choices={result.get('choices', [])}"
        )
    return matching[0]


def _phase_hand_card(object_id: int, decision: dict) -> tuple[dict, int]:
    waiting_owner = decision.get("waiting_for", {}).get("data", {}).get("player")
    owner = decision.get("owner")
    if owner is None:
        owner = waiting_owner
    if (
        not isinstance(owner, int)
        or not 0 <= owner < len(PLAYER_NAMES)
        or (waiting_owner is not None and waiting_owner != owner)
    ):
        raise UnmappedPromptError(f"Could not identify Phase hand owner: {decision}")
    card = decision.get("objects", {}).get(str(object_id))
    if (
        not isinstance(card, dict)
        or card.get("zone") != "Hand"
        or card.get("controller") != owner
    ):
        raise UnmappedPromptError(
            f"Phase object {object_id} is not a known hand card for "
            f"{PLAYER_NAMES[owner]}: {card}"
        )
    return card, owner


def _xmage_hand_owner(result: dict) -> tuple[int, dict]:
    players = [
        player
        for player in result.get("board", [])
        if player.get("is_you")
    ]
    if len(players) != 1 or players[0].get("name") not in PLAYER_NAMES:
        raise UnmappedPromptError(f"Could not identify XMage hand owner: {result}")
    player = players[0]
    return PLAYER_NAMES.index(player["name"]), player


def _xmage_hand_choice(
    card_name: str,
    owner: int,
    action: str | None,
    decision: dict,
    result: dict,
) -> dict:
    xmage_owner, player = _xmage_hand_owner(result)
    key = (normalize_name(card_name), owner, "hand")
    if xmage_owner != owner:
        raise UnmappedPromptError(
            f"Phase/XMage hand owners differ for {key}: "
            f"phase_choices={decision.get('candidates', [])}; "
            f"xmage_choices={result.get('choices', [])}"
        )
    hand = player.get("hand")
    if not isinstance(hand, list):
        raise UnmappedPromptError(f"XMage hand metadata is missing for {key}: {result}")
    hand_ids = {
        card.get("id")
        for card in hand
        if normalize_name(card.get("name", "")) == normalize_name(card_name)
    }
    matching = [
        choice
        for choice in result.get("choices", [])
        if normalize_name(choice.get("name", "")) == normalize_name(card_name)
        and (action is None or choice.get("action") == action)
        and choice.get("id") in hand_ids
    ]
    if not matching:
        return _unique_semantic_choice("hand card", key, decision, result, matching)
    # Same-name hand cards have no per-copy state; CR 400.7 makes each a new
    # object on zone change, with characteristics from the card.
    return matching[0]


def _phase_activation_set(decision: dict) -> Counter:
    return Counter(
        _phase_ability_key(action, decision)
        for action in decision["candidates"]
        if action.get("type") == "ActivateAbility"
    )


def _phase_target_key(action: dict, decision: dict) -> tuple:
    if action.get("type") == "SelectCards":
        cards = action.get("data", {}).get("cards", [])
        if len(cards) != 1:
            raise UnmappedPromptError(
                f"Unsupported Phase card-selection candidate: {action}"
            )
        card, owner = _phase_hand_card(cards[0], decision)
        return ("card", normalize_name(card["name"]), owner, "hand")
    if action.get("type") == "ChooseTarget":
        target = action.get("data", {}).get("target")
    elif action.get("type") == "SelectTargets":
        targets = action.get("data", {}).get("targets")
        if not isinstance(targets, list) or len(targets) != 1:
            raise UnmappedPromptError(f"Unsupported Phase target selection: {action}")
        target = targets[0]
    else:
        raise UnmappedPromptError(f"Unsupported Phase target action: {action}")
    if not isinstance(target, dict) or len(target) != 1:
        raise UnmappedPromptError(f"Unsupported Phase target reference: {target}")
    if "Player" in target:
        player = target["Player"]
        if (
            not isinstance(player, int)
            or isinstance(player, bool)
            or not 0 <= player < len(PLAYER_NAMES)
        ):
            raise UnmappedPromptError(f"Unsupported Phase player target: {target}")
        return ("player", normalize_name(PLAYER_NAMES[player]))
    if "Object" not in target:
        raise UnmappedPromptError(f"Unsupported Phase target reference: {target}")
    obj = decision.get("objects", {}).get(str(target["Object"]))
    if not isinstance(obj, dict) or not all(
        key in obj for key in ("name", "controller", "zone")
    ):
        raise UnmappedPromptError(
            f"Phase target object metadata is missing for {target}: {obj}"
        )
    return (
        "permanent",
        normalize_name(obj.get("name", "")),
        obj.get("controller"),
        normalize_name(obj.get("zone", "")),
    )


def _xmage_target_key(
    choice: dict, result: dict, xmage_state: dict | None = None
) -> tuple:
    if choice.get("target_type") == "card":
        owner, player = _xmage_hand_owner(result)
        hand = player.get("hand")
        if not isinstance(hand, list):
            raise UnmappedPromptError(f"XMage hand metadata is missing: {result}")
        if any(
            card.get("id") == choice.get("id")
            and normalize_name(card.get("name", ""))
            == normalize_name(choice.get("name", ""))
            for card in hand
        ):
            return ("card", normalize_name(choice.get("name", "")), owner, "hand")
        return ("card", normalize_name(choice.get("name", "")), None, "unknown")
    if choice.get("target_type") == "player":
        return ("player", normalize_name(choice.get("name", "")))
    if choice.get("target_type") != "permanent":
        return ("unknown", normalize_name(choice.get("name", "")))
    name = normalize_name(choice.get("name", ""))
    battlefield = [
        (player, card)
        for player in _xmage_players(xmage_state or result)
        for card in player.get("battlefield", [])
    ]
    if any(card.get("id") is not None for _, card in battlefield):
        choice_id = choice.get("id")
        matches = [
            (player, card)
            for player, card in battlefield
            if card.get("id") == choice_id
            and normalize_name(card.get("name", "")) == name
        ]
    else:
        matches = [
            (player, card)
            for player, card in battlefield
            if normalize_name(card.get("name", "")) == name
        ]
    if (
        len(matches) != 1
        or matches[0][0].get("name") not in PLAYER_NAMES
    ):
        raise UnmappedPromptError(
            f"Could not identify XMage permanent source for {choice}: "
            f"xmage_choices={result.get('choices', [])}"
        )
    controller = PLAYER_NAMES.index(matches[0][0]["name"])
    return ("permanent", name, controller, "battlefield")


def _phase_target_set(decision: dict) -> Counter:
    return Counter(
        _phase_target_key(action, decision)
        for action in decision["candidates"]
        if action.get("type")
        in {"ChooseTarget", "SelectTargets", "SelectCards"}
    )


def _xmage_target_set(result: dict, xmage_state: dict | None = None) -> Counter:
    if result.get("action_type") != "GAME_TARGET" or "(Scry)" in result.get("message", ""):
        return Counter()
    return Counter(
        _xmage_target_key(choice, result, xmage_state)
        for choice in result.get("choices", [])
        if choice.get("target_type") in {"permanent", "player", "card"}
    )


def _xmage_activation_set(result: dict, xmage_state: dict | None = None) -> Counter:
    return Counter(
        _xmage_ability_key(choice, result, ability_text, xmage_state)
        for choice in result.get("choices", [])
        if choice.get("action") == "activate"
        for ability_text in choice.get("playable_abilities", [])
    )


def _xmage_activation_choice(
    action: dict,
    decision: dict,
    result: dict,
    bindings: ObjectBindings | None = None,
    xmage_state: dict | None = None,
) -> dict:
    if (
        decision.get("waiting_for", {}).get("type") != "Priority"
        or result.get("action_type") != "GAME_SELECT"
    ):
        raise UnmappedPromptError(
            f"Activation prompt kinds do not match: {decision.get('waiting_for')}, "
            f"{result.get('action_type')}"
        )
    key = _phase_ability_key(action, decision)
    matching = [
        choice
        for choice in result.get("choices", [])
        if choice.get("action") == "activate"
        and any(
            _xmage_ability_key(choice, result, ability_text, xmage_state) == key
            for ability_text in choice.get("playable_abilities", [])
        )
    ]
    source_id = action.get("data", {}).get("source_id")
    source = decision.get("objects", {}).get(str(source_id), {})
    if normalize_name(source.get("zone", "")) == "battlefield":
        choice = _resolve_battlefield_choice(
            "activation",
            key,
            source_id,
            source,
            decision,
            result,
            matching,
            bindings,
            xmage_state,
        )
    else:
        choice = _unique_semantic_choice("activation", key, decision, result, matching)
    if len(choice.get("playable_abilities", [])) > 1:
        raise UnmappedPromptError(
            f"XMage activation requires an ability choice for {key}: "
            f"phase_choices={decision.get('candidates', [])}; "
            f"xmage_choices={result.get('choices', [])}"
        )
    return choice


def _xmage_target_choice(
    action: dict,
    decision: dict,
    result: dict,
    bindings: ObjectBindings | None = None,
    xmage_state: dict | None = None,
) -> dict:
    if (
        decision.get("waiting_for", {}).get("type") != "TargetSelection"
        or result.get("action_type") != "GAME_TARGET"
    ):
        raise UnmappedPromptError(
            f"Target prompt kinds do not match: "
            f"{decision.get('waiting_for')}, {result.get('action_type')}"
        )
    key = _phase_target_key(action, decision)
    matching = [
        choice
        for choice in result.get("choices", [])
        if _xmage_target_key(choice, result, xmage_state) == key
    ]
    target = action.get("data", {}).get("target")
    if action.get("type") == "SelectTargets":
        targets = action.get("data", {}).get("targets")
        target = targets[0] if isinstance(targets, list) and len(targets) == 1 else None
    if (
        isinstance(target, dict)
        and set(target) == {"Object"}
        and key[0] == "permanent"
        and key[3] == "battlefield"
    ):
        phase_id = target["Object"]
        phase_object = decision.get("objects", {}).get(str(phase_id), {})
        return _resolve_battlefield_choice(
            "target",
            key,
            phase_id,
            phase_object,
            decision,
            result,
            matching,
            bindings,
            xmage_state,
        )
    return _unique_semantic_choice("target", key, decision, result, matching)


def _phase_can_attack(decision: dict) -> bool:
    return any(
        action.get("type") == "DeclareAttackers"
        and bool(action.get("data", {}).get("attacks"))
        for action in decision["candidates"]
    )


def _xmage_legal_actions(result: dict) -> tuple[Counter, bool]:
    cards = Counter(
        (
            choice["action"],
            normalize_name(choice["name"]),
        )
        for choice in result.get("choices", [])
        if choice.get("action") in {"land", "cast"} and choice.get("name")
    )
    combat_phase = _normalize_phase(result.get("combat_phase"))
    can_attack = combat_phase in {"attackers", "declareattackers"} and any(
        choice.get("choice_type") == "attacker" for choice in result.get("choices", [])
    )
    return cards, can_attack


def _decision_metadata(result: dict) -> dict:
    context = result.get("context") or ""
    turn_match = re.match(r"T(\d+)\s+(.+)", context)
    board = result.get("board") or []
    active = next((player.get("name") for player in board if player.get("is_active")), None)
    actor = next((player.get("name") for player in board if player.get("is_you")), None)
    turn = int(turn_match.group(1)) if turn_match else None
    if turn is not None and active in PLAYER_NAMES:
        turn = _normalize_xmage_turn(turn, PLAYER_NAMES.index(active))
    step = None
    if turn_match:
        context_step = turn_match.group(2).split(" (", 1)[0].strip()
        step = context_step.rsplit("/", 1)[-1].strip()
    return {
        "turn": turn,
        "active_player": active,
        "actor": actor,
        "step": _normalize_phase(step),
        "context": context,
    }


def _decision_point_diff(decision: dict, result: dict) -> dict:
    metadata = _decision_metadata(result)
    state = decision["state"]
    expected = {
        "turn": state["turn"],
        "active_player": PLAYER_NAMES[state["active_player"]],
        "actor": PLAYER_NAMES[decision["owner"]],
        "step": _normalize_phase(state["phase"]),
    }
    actual = {
        "turn": metadata["turn"],
        "active_player": metadata["active_player"],
        "actor": metadata["actor"],
        "step": metadata["step"],
    }
    return {
        "phase": expected,
        "xmage": actual,
    } if expected != actual else {}


def _matching_trivial_combat_stop(result: dict, skipped_stops: list[dict]) -> dict | None:
    metadata = _decision_metadata(result)
    combat_phase = _normalize_phase(result.get("combat_phase"))
    if combat_phase == "attackers":
        combat_phase = "declareattackers"
    elif combat_phase == "blockers":
        combat_phase = "declareblockers"
    if combat_phase not in {"declareattackers", "declareblockers"}:
        return None
    return next(
        (
            stop
            for stop in reversed(skipped_stops)
            if stop["kind"] == combat_phase
            and stop["turn"] == metadata["turn"]
            and stop["active_player"] == metadata["active_player"]
            and stop["step"] == metadata["step"]
        ),
        None,
    )


def _parse_tool_result(result) -> dict:
    if result.is_error:
        raise RuntimeError(f"MCP tool error: {result.content}")
    text = next((item.text for item in result.content if hasattr(item, "text")), None)
    if text is None:
        raise RuntimeError(f"MCP tool returned no text: {result}")
    return json.loads(text)


async def _call(session, name: str, arguments: dict | None = None) -> dict:
    return _parse_tool_result(await session.call_tool(name, arguments or {}))


async def _connect_xmage(urls: list[str]):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    stack = AsyncExitStack()
    sessions = []
    for url in urls:
        http_client = create_mcp_http_client(timeout=httpx2.Timeout(30.0, read=None))
        await stack.enter_async_context(http_client)
        read, write = await stack.enter_async_context(streamable_http_client(url, http_client=http_client))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        sessions.append(session)
    return stack, sessions


async def _get_state(session) -> dict:
    return await _call(session, "get_game_state")


async def _wait_for_xmage_update(
    sessions: list,
    previous_states: list[dict],
    expected_projection: dict,
) -> list[dict]:
    previous_sequences = [state.get("game_seq") for state in previous_states]
    if any(sequence is None for sequence in previous_sequences):
        raise RuntimeError(f"XMage state omitted game_seq: {previous_states}")
    deadline = time.monotonic() + 8
    stable_sequences = None
    stable_polls = 0
    latest_states = previous_states
    latest_projection = projection_from_xmage(latest_states)
    while time.monotonic() < deadline:
        states = await asyncio.gather(*(_get_state(session) for session in sessions))
        latest_states = states
        if all(state.get("available") for state in states):
            sequences = [state.get("game_seq") for state in states]
            if all(
                sequence is not None and sequence > previous
                for sequence, previous in zip(sequences, previous_sequences)
            ):
                latest_projection = projection_from_xmage(states)
                if latest_projection == expected_projection:
                    return states
                if sequences == stable_sequences:
                    stable_polls += 1
                else:
                    stable_sequences = sequences
                    stable_polls = 0
                if stable_polls >= 2:
                    return states
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"XMage state did not advance after a phase action; "
        f"previous={previous_sequences}, states={latest_states}, projection={latest_projection}, "
        f"expected={expected_projection}"
    )


def _normalize_phase(phase: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (phase or "").casefold())


def _normalize_xmage_turn(turn: int, active_player: int) -> int:
    return turn * 2 - 1 + active_player


async def _phase_advance_to_meaningful(
    phase: PhaseEnv,
    decision: dict,
    skipped_stops: list[dict],
    normalization_events: list[dict],
) -> dict:
    while decision["type"] == "decision" and not _is_meaningful_decision(decision):
        index, action = _trivial_action_index(decision)
        state = decision["state"]
        kind = action["type"]
        if kind == "PassPriority":
            skipped_stops.append(
                {
                    "kind": "priority",
                    "turn": state["turn"],
                    "active_player": PLAYER_NAMES[state["active_player"]],
                    "step": _normalize_phase(state["phase"]),
                }
            )
        elif kind in {"DeclareAttackers", "DeclareBlockers"}:
            stop = {
                "kind": "declareattackers" if kind == "DeclareAttackers" else "declareblockers",
                "turn": state["turn"],
                "active_player": PLAYER_NAMES[state["active_player"]],
                "step": _normalize_phase(state["phase"]),
            }
            skipped_stops.append(stop)
            normalization_events.append({"engine": "phase", "action": action, **stop})
        applied = await asyncio.to_thread(phase.choose, index)
        if applied.get("type") != "applied" or not applied.get("ok"):
            raise RuntimeError(f"phase rejected automatic stop {action}: {applied}")
        decision = await asyncio.to_thread(phase._read)
    return decision


def _matching_trivial_priority_stop(
    result: dict, decision: dict, skipped_stops: list[dict]
) -> dict | None:
    metadata = _decision_metadata(result)
    cards, can_attack = _xmage_legal_actions(result)
    has_activated_abilities = any(
        choice.get("action") == "activate" for choice in result.get("choices", [])
    )
    if (
        result.get("action_type") != "GAME_SELECT"
        or cards
        or can_attack
        or has_activated_abilities
    ):
        return None
    matching = next(
        (
            stop
            for stop in reversed(skipped_stops)
            if stop["kind"] == "priority"
            and stop["turn"] == metadata["turn"]
            and stop["active_player"] == metadata["active_player"]
            and stop["step"] == metadata["step"]
        ),
        None,
    )
    if matching is not None:
        return matching
    if result.get("response_type") == "boolean" and _decision_point_diff(decision, result):
        return {
            "kind": "priority",
            "turn": metadata["turn"],
            "active_player": metadata["active_player"],
            "step": metadata["step"],
        }
    return None


async def _sync_xmage_to_decision(
    sessions: list,
    decision: dict,
    action_tasks: dict[int, asyncio.Task],
    pending_results: dict[int, dict],
    skipped_stops: list[dict],
    normalization_events: list[dict],
    timeout: float = 45,
) -> tuple[dict | None, dict]:
    owner = decision["owner"]
    deadline = time.monotonic() + timeout
    last_results: dict[int, dict] = {}
    while time.monotonic() < deadline:
        for seat, session in enumerate(sessions):
            if seat in pending_results:
                result = pending_results[seat]
            else:
                task = action_tasks.get(seat)
                if task is not None:
                    if not task.done():
                        continue
                    del action_tasks[seat]
                    result = task.result()
                else:
                    result = await _call(session, "get_action_choices")
                if result.get("success") is False:
                    raise RuntimeError(f"XMage rejected a previous choice: {result}")
                last_results[seat] = result
                if result.get("game_over"):
                    pending_results[seat] = result
                elif not result.get("action_pending"):
                    action_tasks[seat] = asyncio.create_task(
                        _call(session, "pass_priority")
                    )
                    continue

            if result.get("game_over"):
                continue
            if not result.get("action_pending"):
                pending_results.pop(seat, None)
                continue

            stop = _matching_trivial_combat_stop(result, skipped_stops)
            if stop is not None:
                normalization_events.append(
                    {
                        "engine": "xmage",
                        "rule": "decline_combat_prompt_without_phase_action",
                        "context": result.get("context"),
                        "combat_phase": result.get("combat_phase"),
                    }
                )
                pending_results.pop(seat, None)
                action_tasks[seat] = asyncio.create_task(
                    _call(session, "choose_action", {"choice": "no"})
                )
                continue

            stop = _matching_trivial_priority_stop(result, decision, skipped_stops)
            if stop is not None:
                normalization_events.append(
                    {
                        "engine": "xmage",
                        "rule": "pass_priority_without_phase_action",
                        "context": result.get("context"),
                    }
                )
                pending_results.pop(seat, None)
                action_tasks[seat] = asyncio.create_task(_call(session, "pass_priority"))
                continue

            point_diff = _decision_point_diff(decision, result)
            if point_diff:
                if seat == owner:
                    return result, point_diff
                pending_results[seat] = result
                continue
            pending_results[seat] = result

        if owner in pending_results:
            result = pending_results[owner]
            if result.get("game_over"):
                return result, {"game_over": True}
            point_diff = _decision_point_diff(decision, result)
            if not point_diff:
                return result, {}
            return result, point_diff
        await asyncio.sleep(0.05)
    return None, {
        "timeout": timeout,
        "owner": PLAYER_NAMES[owner],
        "last_results": last_results,
        "pending_seats": sorted(pending_results),
    }


def _legal_action_diff(
    decision: dict, xmage_result: dict, xmage_state: dict | None = None
) -> dict:
    waiting_for = decision.get("waiting_for", {})
    phase_scry = (
        isinstance(waiting_for, dict) and waiting_for.get("type") == "ScryChoice"
    )
    xmage_scry = (
        xmage_result.get("action_type") == "GAME_TARGET"
        and "(Scry)" in xmage_result.get("message", "")
    )
    if phase_scry or xmage_scry:
        if not phase_scry or not xmage_scry:
            return {
                "phase": {"waiting_for": waiting_for, "candidates": decision["candidates"]},
                "xmage": xmage_result,
            }
        waiting_player = waiting_for.get("data", {}).get("player")
        if "owner" in decision and decision["owner"] != waiting_player:
            return {
                "phase": {"owner": decision["owner"], "waiting_for": waiting_for},
                "xmage": xmage_result,
            }
        phase_choices, xmage_choices = _scry_legal_choices(decision, xmage_result)
        if phase_choices != xmage_choices:
            return {
                "phase": {"scry_choices": sorted(phase_choices)},
                "xmage": {"scry_choices": sorted(xmage_choices), "prompt": xmage_result},
            }
        return {}
    phase_cards = _card_action_set(decision)
    xmage_cards, xmage_can_attack = _xmage_legal_actions(xmage_result)
    phase_abilities = _phase_activation_set(decision)
    xmage_abilities = _xmage_activation_set(xmage_result, xmage_state)
    phase_targets = _phase_target_set(decision)
    xmage_targets = _xmage_target_set(xmage_result, xmage_state)
    phase_can_attack = _phase_can_attack(decision)
    if (
        phase_cards == xmage_cards
        and phase_abilities == xmage_abilities
        and phase_targets == xmage_targets
        and phase_can_attack == xmage_can_attack
    ):
        return {}
    return {
        "phase": {
            "cards": sorted([{"action": action, "name": name, "count": count} for (action, name), count in phase_cards.items()],
                            key=lambda item: (item["action"], item["name"])),
            "attacking_possible": phase_can_attack,
            "abilities": sorted(
                [
                    {"key": key, "count": count}
                    for key, count in phase_abilities.items()
                ],
                key=lambda item: str(item["key"]),
            ),
            "targets": sorted(
                [{"key": key, "count": count} for key, count in phase_targets.items()],
                key=lambda item: str(item["key"]),
            ),
            "candidates": decision["candidates"],
        },
        "xmage": {
            "cards": sorted([{"action": action, "name": name, "count": count} for (action, name), count in xmage_cards.items()],
                            key=lambda item: (item["action"], item["name"])),
            "attacking_possible": xmage_can_attack,
            "abilities": sorted(
                [
                    {"key": key, "count": count}
                    for key, count in xmage_abilities.items()
                ],
                key=lambda item: str(item["key"]),
            ),
            "targets": sorted(
                [{"key": key, "count": count} for key, count in xmage_targets.items()],
                key=lambda item: str(item["key"]),
            ),
            "choices": xmage_result.get("choices", []),
            "combat_phase": xmage_result.get("combat_phase"),
        },
    }


def _scry_legal_choices(decision: dict, xmage_result: dict) -> tuple[set, set]:
    waiting = decision["waiting_for"]["data"]
    owner = waiting.get("player")
    if not isinstance(owner, int) or len(waiting.get("cards", [])) != 1:
        raise UnmappedPromptError(f"Unsupported Phase scry prompt: {waiting}")
    card_id = waiting["cards"][0]
    obj = decision.get("objects", {}).get(str(card_id))
    if (
        not isinstance(obj, dict)
        or obj.get("controller") != owner
        or obj.get("zone") != "Library"
    ):
        raise UnmappedPromptError(f"Phase scry object lacks library metadata: {obj}")
    card_key = ("bottom", normalize_name(obj["name"]), owner, "Library", "Scry")
    phase_choices = set()
    for action in decision["candidates"]:
        if action.get("type") != "SelectCards":
            continue
        cards = action.get("data", {}).get("cards")
        if cards == []:
            phase_choices.add(("keep",))
        elif cards == [card_id]:
            phase_choices.add(card_key)
        else:
            raise UnmappedPromptError(f"Unsupported Phase scry candidate: {action}")
    if not phase_choices:
        raise UnmappedPromptError(f"Phase scry prompt has no SelectCards candidates: {decision}")
    board = xmage_result.get("board", [])
    if not any(
        item.get("is_you") and item.get("name") == PLAYER_NAMES[owner]
        for item in board
    ):
        raise UnmappedPromptError(f"XMage scry prompt is not for {PLAYER_NAMES[owner]}: {xmage_result}")
    xmage_choices = {
        ("bottom", normalize_name(choice["name"]), owner, "Library", "Scry")
        for choice in xmage_result.get("choices", [])
        if choice.get("target_type") == "card" and choice.get("name")
    }
    if not xmage_result.get("required", True) and xmage_result.get("can_cancel"):
        xmage_choices.add(("keep",))
    return phase_choices, xmage_choices


def _scry_selection_payload(action: dict, decision: dict, xmage_result: dict) -> dict:
    if action not in decision["candidates"]:
        raise UnmappedPromptError(f"Selected Phase action is not a candidate: {action}")
    phase_choices, xmage_choices = _scry_legal_choices(decision, xmage_result)
    if phase_choices != xmage_choices:
        raise UnmappedPromptError(
            f"Phase/XMage scry choices differ: {phase_choices} vs {xmage_choices}"
        )
    card_ids = action.get("data", {}).get("cards", [])
    if not card_ids:
        if ("keep",) not in phase_choices:
            raise UnmappedPromptError(f"Phase scry has no keep choice: {action}")
        return {"choice": "no"}
    if len(card_ids) != 1:
        raise UnmappedPromptError(f"Unsupported Phase scry selection: {action}")
    obj = decision["objects"].get(str(card_ids[0]))
    matching = [
        choice
        for choice in xmage_result.get("choices", [])
        if choice.get("target_type") == "card"
        and normalize_name(choice.get("name", "")) == normalize_name(obj["name"])
    ]
    choice = _unique_semantic_choice(
        "scry card", obj["name"], decision, xmage_result, matching
    )
    return {"choice": str(choice["id"])}


async def _keep_opening_hands(sessions: list) -> dict[int, asyncio.Task]:
    deadline = time.monotonic() + 180
    ready_polls = 0
    action_tasks: dict[int, asyncio.Task] = {}
    last_choices = []
    last_states = []
    while time.monotonic() < deadline:
        pending = []
        last_choices = [None] * len(sessions)
        for index, session in enumerate(sessions):
            task = action_tasks.get(index)
            if task is not None and not task.done():
                last_choices[index] = {"choose_action_pending": True}
                continue
            if task is not None:
                del action_tasks[index]
                result = task.result()
                if result.get("success") is False:
                    raise RuntimeError(f"XMage rejected a pregame choice: {result}")
            else:
                result = await _call(session, "get_action_choices")
            last_choices[index] = result
            if result.get("action_pending"):
                pending.append((index, result))
        if not pending:
            states = [await _get_state(session) for session in sessions]
            last_states = states
            hand_sizes = {
                player.get("name"): player.get("hand_size")
                for state in states
                if state.get("available")
                for player in state.get("players", [])
            }
            if all(state.get("available") for state in states) and all(
                hand_sizes.get(name) == 7 for name in PLAYER_NAMES
            ):
                ready_polls += 1
                if ready_polls >= 8:
                    return action_tasks
            else:
                ready_polls = 0
            await asyncio.sleep(0.25)
            continue
        ready_polls = 0
        progressed = False
        for index, result in pending:
            session = sessions[index]
            message = (result.get("message") or "").casefold()
            if (
                result.get("action_type") == "GAME_SELECT"
                and (result.get("context") or "").startswith("T1 Precombat Main")
                and all(
                    any(
                        player.get("name") == name and player.get("hand_size") == 7
                        for player in result.get("board", [])
                    )
                    for name in PLAYER_NAMES
                )
            ):
                return action_tasks
            if result.get("action_type") == "GAME_TARGET" and "starting player" in message:
                choices = [
                    choice
                    for choice in result.get("choices", [])
                    if normalize_name(choice.get("name", "")) == normalize_name(PLAYER_NAMES[0])
                ]
                if len(choices) != 1:
                    raise RuntimeError(f"XMage did not offer {PLAYER_NAMES[0]} as starting player: {result}")
                action_tasks[index] = asyncio.create_task(
                    _call(session, "choose_action", {"choice": str(choices[0]["index"])})
                )
                progressed = True
                continue
            if result.get("action_type") != "GAME_ASK":
                raise RuntimeError(f"Unexpected XMage pregame action: {result}")
            if "mulligan" in message:
                answer = "no"
            elif "play" in message or "draw" in message:
                answer = "yes"
            else:
                raise RuntimeError(f"Unexpected XMage pregame prompt: {result}")
            action_tasks[index] = asyncio.create_task(
                _call(session, "choose_action", {"choice": answer})
            )
            progressed = True
        if not progressed:
            await asyncio.sleep(0.25)
    raise TimeoutError(
        f"XMage did not complete starting-player and Keep-7 decisions; "
        f"choices={last_choices}, states={last_states}"
    )


async def _apply_xmage_action(
    session,
    action: dict,
    decision: dict,
    xmage_result: dict,
    bindings: ObjectBindings | None = None,
) -> asyncio.Task:
    kind = action["type"]
    data = action.get("data", {})
    if kind == "PassPriority":
        return asyncio.create_task(_call(session, "pass_priority"))
    if kind in {"PlayLand", "CastSpell"}:
        object_id = data.get("object_id", data.get("card_id"))
        card, owner = _phase_hand_card(object_id, decision)
        desired_action = "land" if kind == "PlayLand" else "cast"
        choice = _xmage_hand_choice(
            card["name"], owner, desired_action, decision, xmage_result
        )
        return asyncio.create_task(_call(session, "choose_action", {"choice": str(choice["index"])}))
    if kind == "ActivateAbility":
        xmage_state = await _get_state(session)
        if bindings is not None:
            bindings.prune(decision.get("state", {}), xmage_state)
        choice = _xmage_activation_choice(
            action, decision, xmage_result, bindings, xmage_state
        )
        return asyncio.create_task(
            _call(session, "choose_action", {"choice": str(choice["index"])})
        )
    if kind in {"ChooseTarget", "SelectTargets"}:
        xmage_state = await _get_state(session)
        if bindings is not None:
            bindings.prune(decision.get("state", {}), xmage_state)
        choice = _xmage_target_choice(
            action, decision, xmage_result, bindings, xmage_state
        )
        return asyncio.create_task(
            _call(session, "choose_action", {"choice": str(choice["index"])})
        )
    if kind == "SelectCards":
        card_ids = data.get("cards", [])
        if decision.get("waiting_for", {}).get("type") == "ScryChoice":
            return asyncio.create_task(
                _call(session, "choose_action", _scry_selection_payload(action, decision, xmage_result))
            )
        if len(card_ids) != 1:
            raise UnmappedPromptError(f"Policy cannot map multi-card selection: {action}")
        card, owner = _phase_hand_card(card_ids[0], decision)
        choice = _xmage_hand_choice(card["name"], owner, None, decision, xmage_result)
        return asyncio.create_task(
            _call(session, "choose_action", {"choice": str(choice["index"])})
        )
    if kind == "DeclareAttackers":
        attacks = data.get("attacks", [])
        if attacks:
            return asyncio.create_task(_call(session, "choose_action", {"attackers": "all"}))
        return asyncio.create_task(_call(session, "choose_action", {"choice": "no"}))
    if kind == "DeclareBlockers":
        if data.get("assignments"):
            raise UnmappedPromptError("Policy does not map blocker assignments")
        return asyncio.create_task(_call(session, "choose_action", {"choice": "no"}))
    raise UnmappedPromptError(f"Unsupported phase action: {action}")


async def _start_mage_bench(
    *,
    mage_bench: Path,
    config_path: Path,
    log_path: Path,
    game_logs: Path,
    chooser: str,
) -> tuple[subprocess.Popen, object]:
    path = os.environ.get("PATH", "")
    local_bin = str(Path.home() / ".local" / "bin")
    env = os.environ.copy()
    env["PATH"] = f"{local_bin}{os.pathsep}{path}"
    env["XMAGE_AI_PUPPETEER_CHOOSING_PLAYER"] = chooser
    log_file = log_path.open("w")
    proc = subprocess.Popen(
        ["uv", "run", "python", "-m", "magebench.cli", "--config", str(config_path), "--skip-compile"],
        cwd=mage_bench,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            urls = sorted(game_logs.glob("game_*/Diff?_mcp_url.txt"))
            if len(urls) >= 2:
                by_name = {path.name.split("_", 1)[0]: path.read_text().strip() for path in urls}
                if all(name in by_name for name in PLAYER_NAMES):
                    return proc, log_file
            if proc.poll() is not None:
                log_file.flush()
                raise RuntimeError(f"mage-bench exited ({proc.returncode}); see {log_path}")
            await asyncio.sleep(0.5)
        raise TimeoutError(f"Timed out waiting for external MCP URLs under {game_logs}")
    except BaseException:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        log_file.close()
        raise


@asynccontextmanager
async def _suppress_broken_resource_after_terminal(
    stack: AsyncExitStack,
    terminal_summary_recorded: Callable[[], bool],
):
    from anyio import BrokenResourceError

    try:
        async with stack:
            yield
    except* BrokenResourceError:
        if not terminal_summary_recorded():
            raise


async def _advance_xmage_to_terminal(
    sessions: list,
    action_tasks: dict[int, asyncio.Task],
    expected_projection: dict,
    fallback_result: dict | None,
    fallback_states: list[dict],
    timeout: float = 45,
) -> tuple[list[dict], dict | None]:
    deadline = time.monotonic() + timeout
    last_result = fallback_result
    while time.monotonic() < deadline:
        for seat, session in enumerate(sessions):
            task = action_tasks.get(seat)
            if task is None or not task.done():
                continue
            del action_tasks[seat]
            try:
                result = task.result()
            except Exception as exc:
                return _result_states(last_result), {"seat": PLAYER_NAMES[seat], "error": str(exc)}
            last_result = result
            if result.get("success") is False:
                return _result_states(result), {"seat": PLAYER_NAMES[seat], "result": result}
            if result.get("game_over"):
                if not result.get("board"):
                    return fallback_states, {"terminal_projection_unavailable": result}
                return _result_states(result), None
            if result.get("action_pending"):
                combat_phase = _normalize_phase(result.get("combat_phase"))
                cards, can_attack = _xmage_legal_actions(result)
                if combat_phase in {
                    "attackers",
                    "declareattackers",
                    "blockers",
                    "declareblockers",
                } or cards or can_attack:
                    return _result_states(result), {"seat": PLAYER_NAMES[seat], "result": result}
            action_tasks[seat] = asyncio.create_task(_call(session, "pass_priority"))
        try:
            states = await asyncio.gather(*(_get_state(session) for session in sessions))
        except Exception as exc:
            if last_result is None:
                raise
            if not last_result.get("board"):
                return fallback_states, {"bridge_error": str(exc)}
            return _result_states(last_result), {"bridge_error": str(exc)}
        if projection_from_xmage(states) == expected_projection:
            return states, None
        for seat, session in enumerate(sessions):
            task = action_tasks.get(seat)
            if task is None:
                action_tasks[seat] = asyncio.create_task(_call(session, "pass_priority"))
        await asyncio.sleep(0.05)
    return states, {"timeout": timeout}


def _result_states(result: dict | None) -> list[dict]:
    if result is None or not result.get("board"):
        raise RuntimeError(f"XMage action result has no board snapshot: {result}")
    metadata = _decision_metadata(result)
    turn_match = re.match(r"T(\d+)\s+", result.get("context") or "")
    state = {
        "available": True,
        "turn": int(turn_match.group(1)) if turn_match else metadata["turn"],
        "active_player": metadata["active_player"],
        "players": result["board"],
    }
    return [state, state]


def _xmage_winner_from_logs(game_logs: Path) -> int | None:
    for path in game_logs.glob("game_*/Diff?_mcp.log"):
        text = path.read_text(errors="replace")
        winner = re.search(r"Game over: Player (.+?) is the winner", text)
        if winner:
            return PLAYER_NAMES.index(winner.group(1))
    return None


async def run_game(args: argparse.Namespace) -> dict:
    mage_bench = args.mage_bench.resolve()
    decks = [args.player_deck.resolve(), args.opponent_deck.resolve()]
    phase_decks = [expand_deck(path) for path in decks]
    printings = read_printings()
    if args.v0:
        for cards, path in zip(phase_decks, decks):
            validate_v0_deck(cards, printings, path)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = ROOT / "runs" / run_id
    run_dir.mkdir(parents=True)
    game_logs = run_dir / "mage-bench-logs"
    game_logs.mkdir()
    xmage_decks = [run_dir / f"player-{index}.dck" for index in range(2)]
    for cards, path in zip(phase_decks, xmage_decks):
        write_xmage_deck(cards, printings, path, reverse=args.xmage_library_top == "last")

    config_path = run_dir / "mage-bench.json"
    config_path.write_text(
        json.dumps(
            {
                "gameType": "Two Player Duel",
                "deckType": "Constructed - Standard",
                "skipInitShuffling": True,
                "skipPostGamePrompts": True,
                "logDir": str(game_logs),
                "players": [
                    {"type": "external", "name": name, "deck": str(deck)}
                    for name, deck in zip(PLAYER_NAMES, xmage_decks)
                ],
            },
            indent=2,
        )
        + "\n"
    )
    process = None
    log_file = None
    game_completed = False
    terminal_summary_written = False
    action_tasks: dict[int, asyncio.Task] = {}
    phase = None
    try:
        process, log_file = await _start_mage_bench(
            mage_bench=mage_bench,
            config_path=config_path,
            log_path=run_dir / "mage-bench.log",
            game_logs=game_logs,
            chooser=PLAYER_NAMES[0],
        )
        url_paths = sorted(game_logs.glob("game_*/Diff?_mcp_url.txt"))
        urls_by_name = {path.name.split("_", 1)[0]: path.read_text().strip() for path in url_paths}
        urls = [urls_by_name[name] for name in PLAYER_NAMES]
        stack, sessions = await _connect_xmage(urls)
        async with _suppress_broken_resource_after_terminal(
            stack, lambda: terminal_summary_written
        ):
            action_tasks = await _keep_opening_hands(sessions)
            phase = PhaseEnv(str(args.data_root.resolve()))
            rng = random.Random(args.seed)
            decision = phase.new_game(phase_decks[0], phase_decks[1], seed=args.seed, ordered_library=True)
            for player, deck in zip(phase.started["state"]["players"], phase_decks):
                if len(player["hand"]) != 7:
                    raise RuntimeError(f"phase-rs did not keep seven cards: {player}")
                if Counter(map(normalize_name, player["hand"])) != Counter(
                    map(normalize_name, deck[:7])
                ):
                    raise RuntimeError(f"phase-rs opening hand is not the first seven deck entries: {player}")

            normalization_events: list[dict] = []
            skipped_stops: list[dict] = []
            pending_results: dict[int, dict] = {}
            object_bindings = ObjectBindings()
            phase_projection = projection_from_phase(phase.started["state"])
            xmage_states = await asyncio.gather(*(_get_state(session) for session in sessions))
            xmage_projection = projection_from_xmage(xmage_states)
            if any(len(player["hand"]) != 7 for player in xmage_projection["players"]):
                raise RuntimeError(f"XMage did not keep seven cards: {xmage_projection['players']}")
            diff = compare_projection(phase_projection, xmage_projection)
            if diff:
                report = {
                    "run_id": run_id,
                    "seed": args.seed,
                    "kind": "state",
                    "step": 0,
                    "state": {
                        "phase": phase_projection,
                        "xmage": xmage_projection,
                        "diff": diff,
                    },
                    "phase_candidates": [],
                    "xmage_choices": [],
                }
                (run_dir / "divergence.json").write_text(json.dumps(report, indent=2) + "\n")
                return {"type": "divergence", "run_dir": run_dir, "step": 0, "kind": "state", "diff": diff}

            decision = await _phase_advance_to_meaningful(
                phase, decision, skipped_stops, normalization_events
            )
            steps = 0
            last_xmage_states = xmage_states
            last_action = None
            last_xmage_pending = None
            while decision["type"] == "decision":
                if not _is_meaningful_decision(decision):
                    raise RuntimeError(f"phase trivial-stop normalization failed: {decision}")
                owner = decision["owner"]
                xmage_result, point_diff = await _sync_xmage_to_decision(
                    sessions,
                    decision,
                    action_tasks,
                    pending_results,
                    skipped_stops,
                    normalization_events,
                )
                if xmage_result is None:
                    report = {
                        "run_id": run_id,
                        "seed": args.seed,
                        "kind": "decision_point",
                        "step": steps,
                        "state": {
                            "phase": decision["state"],
                            "xmage": await asyncio.gather(*(_get_state(session) for session in sessions)),
                        },
                        "phase_candidates": decision["candidates"],
                        "xmage_choices": None,
                        "reason": point_diff,
                    }
                    (run_dir / "divergence.json").write_text(json.dumps(report, indent=2) + "\n")
                    return {
                        "type": "divergence",
                        "run_dir": run_dir,
                        "step": steps,
                        "kind": "decision_point",
                    }
                if xmage_result.get("game_over"):
                    report = {
                        "run_id": run_id,
                        "seed": args.seed,
                        "kind": "decision_point",
                        "step": steps,
                        "state": {
                            "phase": decision["state"],
                            "xmage": xmage_result,
                        },
                        "phase_candidates": decision["candidates"],
                        "xmage_choices": xmage_result,
                        "reason": "XMage game ended before the phase decision point",
                    }
                    (run_dir / "divergence.json").write_text(json.dumps(report, indent=2) + "\n")
                    return {
                        "type": "divergence",
                        "run_dir": run_dir,
                        "step": steps,
                        "kind": "decision_point",
                    }

                if point_diff:
                    report = {
                        "run_id": run_id,
                        "seed": args.seed,
                        "kind": "decision_point",
                        "step": steps,
                        "state": {
                            "phase": decision["state"],
                            "xmage": _decision_metadata(xmage_result),
                            "diff": point_diff,
                        },
                        "phase_candidates": decision["candidates"],
                        "xmage_choices": xmage_result.get("choices", []),
                        "xmage_pending": xmage_result,
                        "prompts": {
                            "phase": {
                                "waiting_for": decision.get("waiting_for"),
                                "objects": decision.get("objects"),
                                "candidates": decision["candidates"],
                            },
                            "xmage": xmage_result,
                        },
                        "skipped_stops": skipped_stops,
                        "normalization_events": normalization_events,
                    }
                    (run_dir / "divergence.json").write_text(json.dumps(report, indent=2) + "\n")
                    return {
                        "type": "divergence",
                        "run_dir": run_dir,
                        "step": steps,
                        "kind": "decision_point",
                    }

                if steps:
                    xmage_states = await _wait_for_xmage_update(
                        sessions,
                        last_xmage_states,
                        projection_from_phase(decision["state"]),
                    )
                else:
                    xmage_states = await asyncio.gather(*(_get_state(session) for session in sessions))
                phase_projection = projection_from_phase(decision["state"])
                xmage_projection = projection_from_xmage(xmage_states)
                state_diff = compare_projection(phase_projection, xmage_projection)
                if state_diff:
                    report = {
                        "run_id": run_id,
                        "seed": args.seed,
                        "kind": "state",
                        "step": steps,
                        "state": {
                            "phase": phase_projection,
                            "xmage": xmage_projection,
                            "diff": state_diff,
                        },
                        "prompts": {
                            "phase": {
                                "waiting_for": decision.get("waiting_for"),
                                "objects": decision.get("objects"),
                                "candidates": decision["candidates"],
                            },
                            "xmage": xmage_result,
                        },
                        "phase_candidates": decision["candidates"],
                        "xmage_choices": xmage_result.get("choices", []),
                        "xmage_pending": xmage_result,
                    }
                    (run_dir / "divergence.json").write_text(json.dumps(report, indent=2) + "\n")
                    return {"type": "divergence", "run_dir": run_dir, "step": steps, "kind": "state"}

                try:
                    legal_diff = _legal_action_diff(
                        decision, xmage_result, xmage_states[owner]
                    )
                except UnmappedPromptError as exc:
                    return _unmapped_prompt_result(
                        run_dir,
                        run_id,
                        args.seed,
                        steps,
                        str(exc),
                        decision,
                        xmage_result,
                        phase_projection,
                        xmage_projection,
                    )
                if legal_diff:
                    report = {
                        "run_id": run_id,
                        "seed": args.seed,
                        "kind": "legal_actions",
                        "step": steps,
                        "state": {
                            "phase": phase_projection,
                            "xmage": xmage_projection,
                        },
                        "legal_actions": legal_diff,
                        "prompts": {
                            "phase": {
                                "waiting_for": decision.get("waiting_for"),
                                "objects": decision.get("objects"),
                                "candidates": decision["candidates"],
                            },
                            "xmage": xmage_result,
                        },
                        "phase_candidates": decision["candidates"],
                        "xmage_choices": xmage_result.get("choices", []),
                    }
                    (run_dir / "divergence.json").write_text(json.dumps(report, indent=2) + "\n")
                    return {
                        "type": "divergence",
                        "run_dir": run_dir,
                        "step": steps,
                        "kind": "legal_actions",
                    }

                if decision["state"]["turn"] >= MAX_TURNS:
                    summary = {
                        "run_id": run_id,
                        "seed": args.seed,
                        "status": "turn_cap",
                        "divergences": 0,
                        "turns": decision["state"]["turn"],
                        "steps": steps,
                        "winner": {"phase": None, "xmage": None},
                        "normalization_events": normalization_events,
                        "final_projection": xmage_projection,
                    }
                    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
                    game_completed = True
                    return {"type": "summary", "run_dir": run_dir, **summary}

                try:
                    index, action = policy_candidate(decision, rng, v0=args.v0)
                    action_task = await _apply_xmage_action(
                        sessions[owner],
                        action,
                        decision,
                        xmage_result,
                        object_bindings,
                    )
                except UnmappedPromptError as exc:
                    return _unmapped_prompt_result(
                        run_dir,
                        run_id,
                        args.seed,
                        steps,
                        str(exc),
                        decision,
                        xmage_result,
                        phase_projection,
                        xmage_projection,
                    )
                pending_results.pop(owner, None)
                action_tasks[owner] = action_task
                last_action = action
                last_xmage_pending = xmage_result
                applied = await asyncio.to_thread(phase.choose, index)
                if applied.get("type") != "applied" or not applied.get("ok"):
                    raise RuntimeError(f"phase rejected policy action {action}: {applied}")
                steps += 1
                decision = await asyncio.to_thread(phase._read)
                decision = await _phase_advance_to_meaningful(
                    phase, decision, skipped_stops, normalization_events
                )
                if decision["type"] == "game_over":
                    break
                last_xmage_states = xmage_states

            if decision["type"] != "game_over":
                raise RuntimeError(f"phase game stopped unexpectedly: {decision}")
            game_completed = True
            phase_state = projection_from_phase(decision["state"])
            xmage_states, terminal_issue = await _advance_xmage_to_terminal(
                sessions, action_tasks, phase_state, last_xmage_pending, last_xmage_states
            )
            xmage_state = projection_from_xmage(xmage_states)
            final_diff = compare_projection(phase_state, xmage_state)
            terminal_unavailable = terminal_issue and (
                terminal_issue.get("terminal_projection_unavailable")
                or "Bridge processor is shut down" in terminal_issue.get("bridge_error", "")
            )
            if terminal_unavailable:
                summary = {
                    "run_id": run_id,
                    "seed": args.seed,
                    "status": "completed_terminal_projection_unavailable",
                    "divergences": 0,
                    "turns": decision["turns"],
                    "steps": steps,
                    "winner": {
                        "phase": decision["winner"],
                        "xmage": _xmage_winner_from_logs(game_logs),
                    },
                    "normalization_events": normalization_events,
                    "final_projection": xmage_state,
                    "terminal_result": terminal_issue,
                }
                (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
                terminal_summary_written = True
                return {"type": "summary", "run_dir": run_dir, **summary}
            if final_diff:
                report = {
                    "run_id": run_id,
                    "seed": args.seed,
                    "kind": "state",
                    "step": steps,
                    "state": {"phase": phase_state, "xmage": xmage_state, "diff": final_diff},
                    "phase_candidates": [],
                    "xmage_choices": (last_xmage_pending or {}).get("choices", []),
                    "last_action": last_action,
                    "last_xmage_pending": last_xmage_pending,
                    "reason": terminal_issue,
                }
                (run_dir / "divergence.json").write_text(json.dumps(report, indent=2) + "\n")
                return {"type": "divergence", "run_dir": run_dir, "step": steps, "kind": "state"}
            game_log = await _call(sessions[0], "get_game_log")
            winner = re.search(
                r"\bPlayer\s+(.+?)\s+is the winner\b",
                game_log.get("log", ""),
                re.IGNORECASE,
            )
            if winner:
                xmage_winner = PLAYER_NAMES.index(winner.group(1))
            elif re.search(r"\bgame is a draw\b", game_log.get("log", ""), re.IGNORECASE):
                xmage_winner = None
            else:
                raise RuntimeError(f"Could not determine XMage winner from game log: {game_log}")
            summary = {
                "run_id": run_id,
                "seed": args.seed,
                "status": "completed",
                "divergences": 0,
                "turns": decision["turns"],
                "steps": steps,
                "winner": {"phase": decision["winner"], "xmage": xmage_winner},
                "normalization_events": normalization_events,
                "final_projection": xmage_state,
            }
            (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            terminal_summary_written = True
            return {"type": "summary", "run_dir": run_dir, **summary}
    finally:
        for task in action_tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*action_tasks.values(), return_exceptions=True)
        if phase is not None:
            phase.close()
        if process is not None and process.poll() is None and game_completed:
            try:
                process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                pass
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if log_file is not None:
            log_file.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("player_deck", type=Path)
    parser.add_argument("opponent_deck", type=Path)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--data-root", type=Path, default=Path.home() / "repos" / "eng" / "pdata" / "v")
    parser.add_argument("--mage-bench", type=Path, default=Path(os.environ.get("MAGE_BENCH_ROOT", DEFAULT_MAGE_BENCH)))
    parser.add_argument("--xmage-library-top", choices=("first", "last"), default="first")
    parser.add_argument("--v0", action="store_true")
    return parser.parse_args()


def main() -> int:
    _reexec_with_mage_bench_python()
    result = asyncio.run(run_game(parse_args()))
    print(json.dumps(result, default=str, indent=2))
    return summary_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
