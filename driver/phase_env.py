"""Python client for the phase-env JSONL driver."""
from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

PHASE_ENV_BIN = Path(__file__).resolve().parent.parent / "target" / "release" / "phase-env"


class PhaseEnv:
    def __init__(self, data_root: str, binary: str | Path = PHASE_ENV_BIN):
        self.proc = subprocess.Popen(
            [str(binary), data_root],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        ready = self._read()
        assert ready["type"] == "ready", ready

    def _read(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise EOFError("phase-env exited")
        return json.loads(line)

    def _write(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    @staticmethod
    def _expand(deck):
        # entries may be "4 Island" (MTGO count) or bare names
        out = []
        for e in deck:
            parts = e.split(" ", 1)
            if parts[0].isdigit() and len(parts) == 2:
                out += [parts[1]] * int(parts[0])
            else:
                out.append(e)
        return out

    def new_game(self, player_deck: list[str], opponent_deck: list[str], seed: int = 0):
        self._write(
            {
                "type": "new_game",
                "seed": seed,
                "player": {"main_deck": self._expand(player_deck)},
                "opponent": {"main_deck": self._expand(opponent_deck)},
            }
        )
        return self._read()

    def choose(self, index: int):
        self._write({"type": "choose", "index": index})
        return self._read()

    def play(self, player_deck, opponent_deck, seed=0, policy=None, record=False):
        """Play one game. policy(msg) -> candidate index. Returns (result, trace)."""
        policy = policy or (lambda msg: random.randrange(max(1, len(msg["candidates"]))))
        trace = []
        msg = self.new_game(player_deck, opponent_deck, seed)
        while True:
            if record:
                trace.append(msg)
            if msg["type"] == "game_over":
                return msg, trace
            if msg["type"] in ("blocked", "error"):
                return msg, trace
            if msg["type"] == "applied":
                msg = self._read()
                continue
            if msg["type"] != "decision":
                return msg, trace
            msg = self.choose(policy(msg))
            if record:
                trace.append(msg)

    def close(self):
        try:
            self._write({"type": "quit"})
        except Exception:
            pass
        self.proc.terminate()
