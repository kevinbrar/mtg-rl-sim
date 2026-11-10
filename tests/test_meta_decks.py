from urllib.parse import unquote

from driver import meta_decks


def test_fetch_printing_requires_registered_card_number(tmp_path, monkeypatch):
    sets_dir = tmp_path / "Mage.Sets" / "src" / "mage" / "sets"
    sets_dir.mkdir(parents=True)
    (sets_dir / "StarTrek.java").write_text(
        'class StarTrek { StarTrek() { super("Star Trek", "TRK"); '
        'cards.add(new SetCardInfo("Swamp", 321, Rarity.LAND, Swamp.class)); } }'
    )
    (sets_dir / "SecretLairDrop.java").write_text(
        'class SecretLairDrop { SecretLairDrop() { super("Secret Lair Drop", "SLD"); '
        'cards.add(new SetCardInfo("Inspiring Vantage", "1605*", Rarity.RARE, Vantage.class)); } }'
    )
    printings_by_name = meta_decks.supported_printings(tmp_path)
    scryfall_printings = {
        "Swamp": [
            {"set": "TRK", "collector_number": "564"},
            {"set": "TRK", "collector_number": "321"},
        ],
        "Inspiring Vantage": [
            {"set": "SLD", "collector_number": "1605★"},
        ],
    }

    def fake_scryfall(url):
        if "/cards/named?" in url:
            name = unquote(url.split("exact=", 1)[1])
            return {
                "name": name,
                "prints_search_uri": f"printings:{name}",
                "type_line": "",
                "legalities": {"standard": "legal"},
            }
        name = unquote(url.split(":", 1)[1].split("&", 1)[0])
        return {"data": scryfall_printings[name], "has_more": False}

    monkeypatch.setattr(meta_decks, "scryfall", fake_scryfall)
    monkeypatch.setattr(meta_decks.time, "sleep", lambda _: None)

    swamp = meta_decks.fetch_printing("Swamp", printings_by_name)
    vantage = meta_decks.fetch_printing("Inspiring Vantage", printings_by_name)

    assert (swamp["set"], swamp["collector_number"]) == ("TRK", "321")
    assert (vantage["set"], vantage["collector_number"]) == ("SLD", "1605*")


def test_canonical_card_name_uses_scryfall_name():
    cache = {"kili the resourceful": {"name": "Kíli the Resourceful"}}

    assert meta_decks.canonical_card_name("Kili the Resourceful", cache) == (
        "Kíli the Resourceful"
    )
