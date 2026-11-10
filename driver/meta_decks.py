"""Export MTGGoldfish Standard main boards from the phase feed and cache Scryfall printings.

Usage: python driver/meta_decks.py "Azorius Control" "Dimir Midrange"
       python driver/meta_decks.py --refresh-printings
Writes oracle/decks/<slug>.txt (``N Card Name`` lines) and chooses the newest
Scryfall printing registered in the pinned mage-bench source.
"""

import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEED = ROOT / "phase" / "client" / "public" / "feeds" / "mtggoldfish-standard.json"
DECKS = ROOT / "oracle" / "decks"
CACHE = DECKS / "scryfall-printings.json"
MAGE_BENCH_ROOT = Path(
    os.environ.get("MAGE_BENCH_ROOT", "/home/ubuntu/repos/eng/mage-bench")
)


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return value.split("//")[0].strip().lower()


def canonical_card_name(name: str, cache: dict) -> str:
    printing = cache.get(normalize_name(name))
    return printing["name"] if printing else name


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def scryfall(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "mtg-rl-sim/0.1", "Accept": "application/json"}
    )
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code != 429:
                raise
            time.sleep(2**attempt)
    raise RuntimeError(f"Scryfall kept rate-limiting {url}")


def normalize_collector_number(value: str) -> str:
    number = value.strip().replace("★", "*")
    return str(int(number)) if number.isdecimal() else number.casefold()


def supported_printings(mage_bench_root: Path) -> dict[str, dict[tuple[str, str], str]]:
    sets_dir = mage_bench_root / "Mage.Sets" / "src" / "mage" / "sets"
    if not sets_dir.is_dir():
        raise FileNotFoundError(f"XMage set sources not found: {sets_dir}")

    code_pattern = re.compile(r'\bsuper\(\s*"[^"]+"\s*,\s*"([A-Z0-9]+)"')
    card_pattern = re.compile(
        r'\bnew\s+SetCardInfo\s*\(\s*"([^"]+)"\s*,\s*(?:"([^"]+)"|(\d+))'
    )
    printings: dict[str, dict[tuple[str, str], str]] = {}
    for path in sets_dir.glob("*.java"):
        source = path.read_text()
        code_match = code_pattern.search(source)
        if not code_match:
            continue
        code = code_match.group(1)
        for match in card_pattern.finditer(source):
            number = match.group(2) or match.group(3)
            key = (code, normalize_collector_number(number))
            printings.setdefault(normalize_name(match.group(1)), {})[key] = number
    if not printings:
        raise RuntimeError(f"No XMage card printings found in {sets_dir}")
    return printings


def fetch_printing(
    name: str, printings_by_name: dict[str, dict[tuple[str, str], str]]
) -> dict:
    card = scryfall(
        "https://api.scryfall.com/cards/named?exact=" + urllib.parse.quote(name)
    )
    time.sleep(0.2)
    prints_url = card["prints_search_uri"] + "&order=released&dir=desc"
    registered = printings_by_name.get(normalize_name(card["name"]), {})
    choice = None
    collector_number = None
    while prints_url:
        prints = scryfall(prints_url)
        time.sleep(0.2)
        for printing in prints["data"]:
            key = (
                printing.get("set", "").upper(),
                normalize_collector_number(printing.get("collector_number", "")),
            )
            if key in registered:
                choice = printing
                collector_number = registered[key]
                break
        if choice is not None or not prints.get("has_more"):
            break
        prints_url = prints["next_page"]
    faces = card.get("card_faces") or [card]
    return {
        "name": card["name"],
        "set": choice["set"].upper() if choice else None,
        "collector_number": collector_number,
        "type_line": card.get("type_line", ""),
        "oracle_text": "\n".join(f.get("oracle_text", "") for f in faces),
        "standard": card["legalities"].get("standard"),
    }


def main(names: list[str]) -> None:
    printings_by_name = supported_printings(MAGE_BENCH_ROOT)
    cache = json.load(open(CACHE)) if CACHE.exists() else {}
    if names == ["--refresh-printings"]:
        card_names = {
            line.split(maxsplit=1)[1]
            for deck in DECKS.glob("*.txt")
            for line in deck.read_text().splitlines()
            if line.strip()
        }
        cache = {}
        no_supported_printings = []
        for name in sorted(card_names):
            cache[normalize_name(name)] = fetch_printing(name, printings_by_name)
            printing = cache[normalize_name(name)]
            if printing["set"] is None:
                no_supported_printings.append(name)
                print(f"  {name} -> no supported printing")
            else:
                print(f"  {name} -> {printing['set']}:{printing['collector_number']}")
        for deck in DECKS.glob("*.txt"):
            lines = []
            for line in deck.read_text().splitlines():
                parts = line.split(maxsplit=1)
                if len(parts) == 2 and parts[0].isdigit():
                    lines.append(f"{parts[0]} {canonical_card_name(parts[1], cache)}")
                else:
                    lines.append(line)
            deck.write_text("\n".join(lines) + "\n")
        CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")
        print(
            f"cards with no supported printing: {', '.join(no_supported_printings) or 'none'}"
        )
        return

    feed = json.load(open(FEED))
    by_name = {d["name"]: d for d in feed["decks"]}
    selected_cards = {}
    selected_decks = {}
    for deck_name in names:
        deck = by_name[deck_name]
        assert sum(c["count"] for c in deck["main"]) == 60, deck_name
        selected_decks[deck_name] = deck
        for c in deck["main"]:
            selected_cards[normalize_name(c["name"])] = c["name"]
    for key, name in sorted(selected_cards.items()):
        cache[key] = fetch_printing(name, printings_by_name)
        print(
            f"  {name} -> {cache[key]['set']}:{cache[key]['collector_number']} ({cache[key]['standard']})"
        )
    for deck_name, deck in selected_decks.items():
        lines = [
            f"{c['count']} {canonical_card_name(c['name'], cache)}"
            for c in deck["main"]
        ]
        out = DECKS / f"{slug(deck_name)}.txt"
        out.write_text("\n".join(lines) + "\n")
        print(f"wrote {out} ({len(lines)} entries)")
    CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
