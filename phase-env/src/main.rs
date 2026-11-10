//! phase-env: line-delimited-JSON driver over the phase-rs engine.
//!
//! Usage: phase-env <data-root>   (data-root holds card-data.json)
//!
//! Protocol (stdin/stdout, one JSON object per line):
//!   <- {"type":"new_game","seed":N,"player":{"main_deck":["4 Island",...]},
//!        "opponent":{"main_deck":[...]}}
//!   -> {"type":"decision","turn":N,"waiting_for":<serde WaitingFor>,"owner":0,"actor":0,
//!        "candidates":[<serde GameAction>, ...],"objects":{...},"state":{...}}
//!   <- {"type":"choose","index":K}
//!   -> {"type":"applied","ok":true,"reason":"action"}  (then next "decision")
//!   -> {"type":"game_over","winner":0|"draw"|null,"turns":N}
//!
//! "candidates" are raw serialized `GameAction`s from the engine's issued
//! AiDecisionContract; a client may submit an index OR a verbatim `action`
//! ({"type":"choose","action":{...}}), which is re-validated by the engine.

use std::io::{BufRead, BufReader, Write};

use engine::ai_support::{apply_ai_action_proposal, AiDecisionContract, AiProposalApplication};
use engine::database::CardDatabase;
use engine::game::deck_loading::{
    load_and_hydrate_decks, resolve_deck_list, DeckList, PlayerDeckList,
};
use engine::game::engine::start_game_with_starting_player;
use engine::types::actions::{GameAction, MulliganChoice};
use engine::types::game_state::{GameState, WaitingFor};
use engine::types::identifiers::ObjectId;
use engine::types::player::PlayerId;
use engine::types::zones::Zone;
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};

const MAX_DECISIONS: usize = 20_000;

fn name_of(state: &GameState, id: &engine::types::identifiers::ObjectId) -> String {
    state
        .objects
        .get(id)
        .map(|o| o.name.clone())
        .unwrap_or_else(|| format!("#{id:?}"))
}

fn snapshot(state: &GameState) -> Value {
    let players: Vec<Value> = state
        .players
        .iter()
        .map(|p| {
            let names = |v: &im::Vector<engine::types::identifiers::ObjectId>| {
                v.iter().map(|id| name_of(state, id)).collect::<Vec<_>>()
            };
            json!({
                "id": p.id.0,
                "life": p.life,
                "hand": names(&p.hand),
                "hand_cards": p.hand.iter().map(|id| json!({
                    "id": id.0,
                    "name": name_of(state, id),
                })).collect::<Vec<_>>(),
                "library_count": p.library.len(),
                "library": names(&p.library),
                "graveyard": names(&p.graveyard),
            })
        })
        .collect();
    let battlefield: Vec<Value> = state
        .battlefield
        .iter()
        .map(|id| {
            let obj = state.objects.get(id);
            json!({
                "id": id.0,
                "name": obj.map(|o| o.name.clone()).unwrap_or_default(),
                "controller": obj.map(|o| o.controller.0),
                "tapped": obj.map(|o| o.tapped),
            })
        })
        .collect();
    let stack: Vec<Value> = state
        .stack
        .iter()
        .map(|e| {
            json!({
                "controller": e.controller.0,
                "name": name_of(state, &e.source_id),
            })
        })
        .collect();
    json!({
        "turn": state.turn_number,
        "active_player": state.active_player.0,
        "phase": format!("{:?}", state.phase),
        "players": players,
        "battlefield": battlefield,
        "stack": stack,
    })
}

fn normalized_name(name: &str) -> String {
    name.trim().to_lowercase()
}

fn order_opening_zones(state: &mut GameState, deck_lists: [&[String]; 2]) -> Vec<Value> {
    let mut unresolved = Vec::new();
    for (index, deck_list) in deck_lists.iter().enumerate() {
        let player = &state.players[index];
        let mut ids_by_name: HashMap<String, VecDeque<ObjectId>> = HashMap::new();
        for id in player.hand.iter().chain(player.library.iter()) {
            if let Some(object) = state.objects.get(id) {
                ids_by_name
                    .entry(normalized_name(&object.name))
                    .or_default()
                    .push_back(*id);
            }
        }

        let mut ordered_ids = Vec::with_capacity(deck_list.len());
        for name in *deck_list {
            match ids_by_name
                .get_mut(&normalized_name(name))
                .and_then(VecDeque::pop_front)
            {
                Some(id) => ordered_ids.push(id),
                None => unresolved.push(json!({"player": index, "name": name})),
            }
        }

        let hand_len = ordered_ids.len().min(7);
        let mut hand = im::Vector::new();
        let mut library = im::Vector::new();
        for id in ordered_ids.iter().take(hand_len) {
            hand.push_back(*id);
            state
                .objects
                .get_mut(id)
                .expect("ordered hand object must exist")
                .zone = Zone::Hand;
        }
        // Draws take library.front(), so retain deck-list order.
        for id in ordered_ids.iter().skip(hand_len) {
            library.push_back(*id);
            state
                .objects
                .get_mut(id)
                .expect("ordered library object must exist")
                .zone = Zone::Library;
        }
        state.players[index].hand = hand;
        state.players[index].library = library;
    }
    unresolved
}

fn keep_opening_hands(state: &mut GameState) {
    for _ in 0..2 {
        if !matches!(&state.waiting_for, WaitingFor::MulliganDecision { .. }) {
            break;
        }
        let owner = state
            .waiting_for
            .acting_players()
            .first()
            .copied()
            .expect("mulligan decision must have an acting player");
        let contract = AiDecisionContract::issue(state, owner);
        let keep = contract
            .candidates
            .iter()
            .find(|candidate| {
                matches!(
                    &candidate.action,
                    GameAction::MulliganDecision {
                        choice: MulliganChoice::Keep
                    }
                )
            })
            .expect("mulligan decision must offer Keep")
            .action
            .clone();
        match apply_ai_action_proposal(state, &contract, contract.authorized_actor, keep) {
            AiProposalApplication::AppliedAction { .. } => {}
            other => panic!("failed to keep opening hand: {other:?}"),
        }
    }
    assert!(
        !matches!(&state.waiting_for, WaitingFor::MulliganDecision { .. }),
        "both players must keep their opening hands"
    );
}

fn waiting_for_label(state: &GameState) -> String {
    // Debug-print the discriminant only (drop payloads for a stable label).
    let dbg = format!("{:?}", state.waiting_for);
    dbg.split([' ', '{', '('])
        .next()
        .unwrap_or(&dbg)
        .to_string()
}

fn objects_for_candidates(
    state: &GameState,
    candidates: &[engine::types::actions::GameAction],
) -> Value {
    let mut objects = serde_json::Map::new();
    for id in candidates.iter().flat_map(GameAction::related_object_ids) {
        let object = state
            .objects
            .get(&id)
            .expect("candidate object reference must exist in game state");
        objects.insert(
            id.0.to_string(),
            json!({
                "name": object.name,
                "zone": format!("{:?}", object.zone),
                "controller": object.controller.0,
            }),
        );
    }
    Value::Object(objects)
}

fn emit(out: &mut impl Write, v: Value) {
    writeln!(out, "{}", serde_json::to_string(&v).unwrap()).unwrap();
    out.flush().unwrap();
}

fn run_game(out: &mut impl Write, inp: &mut impl BufRead, db: &CardDatabase, spec: &Value) {
    let seed = spec["seed"].as_u64().unwrap_or(0);
    let deck_list = DeckList {
        booster_pack_pool: None,
        player: serde_json::from_value::<PlayerDeckList>(spec["player"].clone())
            .unwrap_or_default(),
        opponent: serde_json::from_value::<PlayerDeckList>(spec["opponent"].clone())
            .unwrap_or_default(),
        ai_decks: vec![],
        ai_difficulties: vec![],
        draft_set_codes: vec![],
    };
    let ordered_library = spec["ordered_library"].as_bool().unwrap_or(false);
    let player_names = deck_list.player.main_deck.clone();
    let opponent_names = deck_list.opponent.main_deck.clone();
    let payload = resolve_deck_list(db, &deck_list);

    let mut state = GameState::new_two_player(seed);
    load_and_hydrate_decks(&mut state, &payload, Some(db));
    start_game_with_starting_player(&mut state, PlayerId(0));
    let unresolved = if ordered_library {
        order_opening_zones(&mut state, [&player_names, &opponent_names])
    } else {
        Vec::new()
    };
    keep_opening_hands(&mut state);
    emit(
        out,
        json!({
            "type": "started",
            "ordered_library": ordered_library,
            "starting_player": 0,
            "unresolved": unresolved,
            "state": snapshot(&state),
        }),
    );

    let mut line = String::new();
    for _ in 0..MAX_DECISIONS {
        if let WaitingFor::GameOver { winner } = &state.waiting_for {
            emit(
                out,
                json!({
                    "type": "game_over",
                    "winner": winner.map(|w| w.0),
                    "turns": state.turn_number,
                    "state": snapshot(&state),
                }),
            );
            return;
        }
        let actors = state.waiting_for.acting_players();
        if actors.is_empty() {
            emit(
                out,
                json!({
                    "type": "blocked",
                    "reason": "no acting players",
                    "waiting_for": waiting_for_label(&state),
                    "state": snapshot(&state),
                }),
            );
            return;
        }
        let owner = actors[0];
        let contract = AiDecisionContract::issue(&state, owner);
        let actions: Vec<GameAction> = contract
            .candidates
            .iter()
            .map(|candidate| candidate.action.clone())
            .collect();
        let candidates: Vec<Value> = contract
            .candidates
            .iter()
            .map(|c| serde_json::to_value(&c.action).unwrap_or(json!(null)))
            .collect();
        emit(
            out,
            json!({
                "type": "decision",
                "turn": state.turn_number,
                "waiting_for": serde_json::to_value(&state.waiting_for).unwrap(),
                "owner": contract.semantic_owner.0,
                "actor": contract.authorized_actor.0,
                "candidates": candidates,
                "objects": objects_for_candidates(&state, &actions),
                "state": snapshot(&state),
            }),
        );

        line.clear();
        if inp.read_line(&mut line).unwrap_or(0) == 0 {
            return; // stdin closed
        }
        let req: Value = match serde_json::from_str(line.trim()) {
            Ok(v) => v,
            Err(e) => {
                emit(
                    out,
                    json!({"type":"error","reason":format!("bad json: {e}")}),
                );
                continue;
            }
        };
        let action = if let Some(a) = req.get("action") {
            match serde_json::from_value::<engine::types::actions::GameAction>(a.clone()) {
                Ok(a) => a,
                Err(e) => {
                    emit(
                        out,
                        json!({"type":"error","reason":format!("bad action: {e}")}),
                    );
                    continue;
                }
            }
        } else {
            let idx = req["index"].as_u64().unwrap_or(usize::MAX as u64) as usize;
            match contract.candidates.get(idx) {
                Some(c) => c.action.clone(),
                None => {
                    emit(
                        out,
                        json!({"type":"error","reason":format!("index {idx} out of range")}),
                    );
                    continue;
                }
            }
        };
        match apply_ai_action_proposal(&mut state, &contract, contract.authorized_actor, action) {
            AiProposalApplication::AppliedAction { .. } => emit(
                out,
                json!({"type":"applied","ok":true,"state":snapshot(&state)}),
            ),
            AiProposalApplication::AppliedStackPass { .. } => emit(
                out,
                json!({"type":"applied","ok":true,"reason":"stack_pass","state":snapshot(&state)}),
            ),
            AiProposalApplication::Stale => {
                emit(out, json!({"type":"applied","ok":false,"reason":"stale"}))
            }
            AiProposalApplication::Rejected { rejection } => emit(
                out,
                json!({"type":"applied","ok":false,"reason":format!("rejected: {rejection:?}")}),
            ),
        }
    }
    emit(out, json!({"type":"error","reason":"decision cap reached"}));
}

fn main() {
    let data_root = std::env::args()
        .nth(1)
        .expect("usage: phase-env <data-root> (dir holding card-data.json)");
    let db = CardDatabase::from_export(
        std::path::Path::new(&data_root)
            .join("card-data.json")
            .as_path(),
    )
    .unwrap_or_else(|e| panic!("failed to load card database from {data_root}: {e}"));

    let stdin = std::io::stdin();
    let mut inp = BufReader::new(stdin.lock());
    let stdout = std::io::stdout();
    let mut out = stdout.lock();

    emit(&mut out, json!({"type":"ready","cards":db.card_count()}));
    let mut line = String::new();
    while inp.read_line(&mut line).unwrap_or(0) > 0 {
        let trimmed = line.trim().to_string();
        line.clear();
        if trimmed.is_empty() {
            continue;
        }
        let req: Value = match serde_json::from_str(&trimmed) {
            Ok(v) => v,
            Err(e) => {
                emit(
                    &mut out,
                    json!({"type":"error","reason":format!("bad json: {e}")}),
                );
                continue;
            }
        };
        match req["type"].as_str() {
            Some("new_game") => run_game(&mut out, &mut inp, &db, &req),
            Some("quit") => return,
            _ => emit(
                &mut out,
                json!({"type":"error","reason":"expected new_game or quit"}),
            ),
        }
    }
}
