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
    winner = summary.get("winner")
    if (
        summary.get("status") not in TERMINAL_STATUSES
        or summary.get("divergences") != 0
        or not isinstance(winner, dict)
        or winner.get("phase") != winner.get("xmage")
    ):
        return 1
    return 0


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


def policy_candidate(decision: dict, rng: random.Random) -> tuple[int, dict]:
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
            raise RuntimeError(f"No no-blockers action in phase candidates: {decision}")
        return empty[0]

    supported: list[tuple[int, dict]] = []
    for index, action in enumerate(candidates):
        kind = action.get("type")
        if kind in {"PlayLand", "CastSpell", "PassPriority", "SelectCards"}:
            supported.append((index, action))
    if not supported:
        raise RuntimeError(f"No supported phase action at decision: {decision}")
    return rng.choice(supported)


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
    raise RuntimeError(f"Trivial phase decision has no pass or empty combat action: {decision}")


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
            raise RuntimeError(f"Could not map phase candidate to hand card: {action}, {decision['state']}")
        actions.append(("land" if kind == "PlayLand" else "cast", normalize_name(name)))
    return Counter(actions)


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
    if result.get("action_type") != "GAME_SELECT" or cards or can_attack:
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
                    action_tasks[seat] = asyncio.create_task(_call(session, "pass_priority"))
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


def _legal_action_diff(decision: dict, xmage_result: dict) -> dict:
    phase_cards = _card_action_set(decision)
    xmage_cards, xmage_can_attack = _xmage_legal_actions(xmage_result)
    phase_can_attack = _phase_can_attack(decision)
    if phase_cards == xmage_cards and phase_can_attack == xmage_can_attack:
        return {}
    return {
        "phase": {
            "cards": sorted([{"action": action, "name": name, "count": count} for (action, name), count in phase_cards.items()],
                            key=lambda item: (item["action"], item["name"])),
            "attacking_possible": phase_can_attack,
            "candidates": decision["candidates"],
        },
        "xmage": {
            "cards": sorted([{"action": action, "name": name, "count": count} for (action, name), count in xmage_cards.items()],
                            key=lambda item: (item["action"], item["name"])),
            "attacking_possible": xmage_can_attack,
            "choices": xmage_result.get("choices", []),
            "combat_phase": xmage_result.get("combat_phase"),
        },
    }


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
) -> asyncio.Task:
    kind = action["type"]
    data = action.get("data", {})
    if kind == "PassPriority":
        return asyncio.create_task(_call(session, "pass_priority"))
    if kind in {"PlayLand", "CastSpell"}:
        object_id = data.get("object_id")
        card_name = next(
            (
                item["name"]
                for player in decision["state"]["players"]
                for item in player.get("hand_cards", [])
                if item["id"] == object_id
            ),
            None,
        )
        if card_name is None:
            raise RuntimeError(f"Could not map phase object {object_id} to a hand card: {decision['state']}")
        desired_action = "land" if kind == "PlayLand" else "cast"
        matching = [
            choice
            for choice in xmage_result.get("choices", [])
            if normalize_name(choice.get("name", "")) == normalize_name(card_name)
            and choice.get("action") == desired_action
        ]
        if not matching:
            raise RuntimeError(f"No XMage {desired_action} choice for {card_name}: {xmage_result}")
        choice = matching[0]
        return asyncio.create_task(_call(session, "choose_action", {"choice": str(choice["index"])}))
    if kind == "SelectCards":
        card_ids = data.get("cards", [])
        if len(card_ids) != 1:
            raise RuntimeError(f"v0 policy expected a single-card selection: {action}")
        hand_cards = decision["state"]["players"][decision["owner"]]["hand_cards"]
        card = next((card for card in hand_cards if card["id"] == card_ids[0]), None)
        if card is None:
            raise RuntimeError(f"Could not map phase selection to a hand card: {action}")
        matching = [
            choice
            for choice in xmage_result.get("choices", [])
            if normalize_name(choice.get("name", "")) == normalize_name(card["name"])
        ]
        if not matching:
            raise RuntimeError(f"XMage has no matching card choice for {card['name']}: {xmage_result}")
        return asyncio.create_task(
            _call(session, "choose_action", {"choice": str(matching[0]["index"])})
        )
    if kind == "DeclareAttackers":
        attacks = data.get("attacks", [])
        if attacks:
            return asyncio.create_task(_call(session, "choose_action", {"attackers": "all"}))
        return asyncio.create_task(_call(session, "choose_action", {"choice": "no"}))
    if kind == "DeclareBlockers":
        if data.get("assignments"):
            raise RuntimeError("v0 policy must not declare blockers")
        return asyncio.create_task(_call(session, "choose_action", {"choice": "no"}))
    raise RuntimeError(f"Unsupported phase action: {action}")


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
                        "phase_candidates": decision["candidates"],
                        "xmage_choices": xmage_result.get("choices", []),
                        "xmage_pending": xmage_result,
                    }
                    (run_dir / "divergence.json").write_text(json.dumps(report, indent=2) + "\n")
                    return {"type": "divergence", "run_dir": run_dir, "step": steps, "kind": "state"}

                legal_diff = _legal_action_diff(decision, xmage_result)
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

                index, action = policy_candidate(decision, rng)
                pending_results.pop(owner, None)
                action_tasks[owner] = await _apply_xmage_action(sessions[owner], action, decision, xmage_result)
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
    return parser.parse_args()


def main() -> int:
    _reexec_with_mage_bench_python()
    result = asyncio.run(run_game(parse_args()))
    print(json.dumps(result, default=str, indent=2))
    return summary_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
