//! phase-env: line-delimited-JSON driver over the phase-rs engine.
//!
//! Usage: phase-env <data-root>   (data-root holds card-data.json)
//!
//! Protocol (stdin/stdout, one JSON object per line):
//!   <- {"type":"new_game","seed":N,"player":{"main_deck":["4 Island",...]},
//!        "opponent":{"main_deck":[...]}}
//!   -> {"type":"decision","turn":N,"waiting_for":"Priority","owner":0,"actor":0,
//!        "candidates":[<serde GameAction>, ...], "state":{...}}
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
use engine::game::engine::start_game;
use engine::types::game_state::{GameState, WaitingFor};
use serde::Serialize;
use serde_json::{json, Value};

const MAX_DECISIONS: usize = 20_000;

#[derive(Serialize)]
struct Out {
    #[serde(flatten)]
    v: Value,
}

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

fn waiting_for_label(state: &GameState) -> String {
    // Debug-print the discriminant only (drop payloads for a stable label).
    let dbg = format!("{:?}", state.waiting_for);
    dbg.split([' ', '{', '(']).next().unwrap_or(&dbg).to_string()
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
    let payload = resolve_deck_list(db, &deck_list);

    let mut state = GameState::new_two_player(seed);
    load_and_hydrate_decks(&mut state, &payload, Some(db));
    start_game(&mut state);

    let mut line = String::new();
    for _ in 0..MAX_DECISIONS {
        if let WaitingFor::GameOver { winner } = &state.waiting_for {
            emit(
                out,
                json!({
                    "type": "game_over",
                    "winner": winner.map(|w| w.0),
                    "turns": state.turn_number,
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
                "waiting_for": waiting_for_label(&state),
                "owner": contract.semantic_owner.0,
                "actor": contract.authorized_actor.0,
                "candidates": candidates,
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
                emit(out, json!({"type":"error","reason":format!("bad json: {e}")}));
                continue;
            }
        };
        let action = if let Some(a) = req.get("action") {
            match serde_json::from_value::<engine::types::actions::GameAction>(a.clone()) {
                Ok(a) => a,
                Err(e) => {
                    emit(out, json!({"type":"error","reason":format!("bad action: {e}")}));
                    continue;
                }
            }
        } else {
            let idx = req["index"].as_u64().unwrap_or(usize::MAX as u64) as usize;
            match contract.candidates.get(idx) {
                Some(c) => c.action.clone(),
                None => {
                    emit(out, json!({"type":"error","reason":format!("index {idx} out of range")}));
                    continue;
                }
            }
        };
        match apply_ai_action_proposal(&mut state, &contract, contract.authorized_actor, action) {
            AiProposalApplication::AppliedAction { .. } => {
                emit(out, json!({"type":"applied","ok":true}))
            }
            AiProposalApplication::AppliedStackPass { .. } => {
                emit(out, json!({"type":"applied","ok":true,"reason":"stack_pass"}))
            }
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
    let db = CardDatabase::from_export(std::path::Path::new(&data_root).join("card-data.json").as_path())
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
                emit(&mut out, json!({"type":"error","reason":format!("bad json: {e}")}));
                continue;
            }
        };
        match req["type"].as_str() {
            Some("new_game") => run_game(&mut out, &mut inp, &db, &req),
            Some("quit") => return,
            _ => emit(&mut out, json!({"type":"error","reason":"expected new_game or quit"})),
        }
    }
}
