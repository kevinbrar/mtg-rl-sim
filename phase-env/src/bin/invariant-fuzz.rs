//! invariant-fuzz: seeded random games over the phase-rs engine, checked for
//! engine-level invariants that no rules oracle is needed to judge.
//!
//! Usage: invariant-fuzz <data-root> <deck-a.txt> <deck-b.txt> <first-seed> <games>
//!
//! One JSON line per game on stdout, then a summary line. Checked after every
//! applied action:
//!   - zone integrity: an object is in at most one zone list, the list agrees
//!     with the object's own `zone`, and every listed id exists;
//!   - offered actions apply: a candidate the engine issued is not rejected
//!     and not stale;
//!   - progress: a non-final state has an acting player and a candidate;
//!   - no panic, and the game ends within the decision cap.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet, VecDeque};
use std::panic::{catch_unwind, AssertUnwindSafe};

use engine::ai_support::{apply_ai_action_proposal, AiDecisionContract, AiProposalApplication};
use engine::database::CardDatabase;
use engine::game::deck_loading::{
    load_and_hydrate_decks, resolve_deck_list, DeckList, PlayerDeckList,
};
use engine::game::engine::start_game_with_starting_player;
use engine::types::game_state::{GameState, StackEntryKind, WaitingFor};
use engine::types::identifiers::ObjectId;
use engine::types::player::PlayerId;
use engine::types::zones::Zone;
use serde_json::{json, Value};

const MAX_DECISIONS: usize = 20_000;
const TRACE_LEN: usize = 40;

thread_local! {
    /// The last decisions of the running game, for a panic or violation report.
    static TRACE: RefCell<VecDeque<String>> = const { RefCell::new(VecDeque::new()) };
    static PANIC_LOCATION: RefCell<String> = const { RefCell::new(String::new()) };
}

fn trace_push(entry: String) {
    TRACE.with(|trace| {
        let mut trace = trace.borrow_mut();
        if trace.len() == TRACE_LEN {
            trace.pop_front();
        }
        trace.push_back(entry);
    });
}

fn trace_take() -> Vec<String> {
    TRACE.with(|trace| trace.borrow_mut().drain(..).collect())
}

/// SplitMix64: a fixed, dependency-free generator so a seed replays exactly.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    fn below(&mut self, bound: usize) -> usize {
        (self.next() % bound as u64) as usize
    }
}

fn zone_lists(state: &GameState) -> Vec<(Zone, String, Vec<ObjectId>)> {
    let mut lists = Vec::new();
    for player in &state.players {
        let who = player.id.0;
        lists.push((
            Zone::Hand,
            format!("P{who} hand"),
            player.hand.iter().copied().collect(),
        ));
        lists.push((
            Zone::Library,
            format!("P{who} library"),
            player.library.iter().copied().collect(),
        ));
        lists.push((
            Zone::Graveyard,
            format!("P{who} graveyard"),
            player.graveyard.iter().copied().collect(),
        ));
    }
    lists.push((
        Zone::Battlefield,
        "battlefield".to_string(),
        state.battlefield.iter().copied().collect(),
    ));
    lists.push((
        Zone::Exile,
        "exile".to_string(),
        state.exile.iter().copied().collect(),
    ));
    lists.push((
        Zone::Command,
        "command zone".to_string(),
        state.command_zone.iter().copied().collect(),
    ));
    lists.push((
        Zone::Stack,
        "stack".to_string(),
        state
            .stack
            .iter()
            .filter(|entry| matches!(entry.kind, StackEntryKind::Spell { .. }))
            .filter(|entry| !is_pending_cast(state, entry.id))
            .map(|entry| entry.id)
            .collect(),
    ));
    lists
}

/// phase-rs announces a spell by pushing its stack entry while the card stays
/// in its origin zone until the cast is finalized (CR 601.2a moves it first).
/// That known representation is counted separately, not as a violation; the
/// card must still be consistent in its origin zone.
fn is_pending_cast(state: &GameState, id: ObjectId) -> bool {
    state
        .objects
        .get(&id)
        .is_some_and(|object| object.zone != Zone::Stack)
}

fn pending_casts(state: &GameState) -> usize {
    state
        .stack
        .iter()
        .filter(|entry| matches!(entry.kind, StackEntryKind::Spell { .. }))
        .filter(|entry| is_pending_cast(state, entry.id))
        .count()
}

/// Returns one message per broken zone invariant in `state`.
fn zone_violations(state: &GameState) -> Vec<String> {
    let mut violations = Vec::new();
    let mut seen: HashMap<ObjectId, String> = HashMap::new();
    for (zone, label, ids) in zone_lists(state) {
        for id in ids {
            let name = state
                .objects
                .get(&id)
                .map(|object| object.name.clone())
                .unwrap_or_default();
            if let Some(first) = seen.insert(id, label.clone()) {
                violations.push(format!(
                    "object {} ({name}) is listed in {first} and in {label}",
                    id.0
                ));
            }
            match state.objects.get(&id) {
                None => violations.push(format!("{label} lists missing object {}", id.0)),
                Some(object) if object.zone != zone => violations.push(format!(
                    "object {} ({name}) is listed in {label} but its zone is {:?}",
                    id.0, object.zone
                )),
                Some(_) => {}
            }
        }
    }
    for (id, object) in state.objects.iter() {
        let listed = matches!(
            object.zone,
            Zone::Hand | Zone::Library | Zone::Graveyard | Zone::Battlefield | Zone::Exile
        );
        if listed && !seen.contains_key(id) {
            violations.push(format!(
                "object {} ({}) has zone {:?} but is in no zone list",
                id.0, object.name, object.zone
            ));
        }
    }
    violations
}

struct GameReport {
    seed: u64,
    decisions: usize,
    turns: u32,
    outcome: &'static str,
    /// Decisions taken while a spell was announced but still in its origin zone.
    pending_cast_decisions: usize,
    violations: Vec<Value>,
}

fn violation(kind: &str, decision: usize, state: &GameState, detail: String) -> Value {
    json!({
        "kind": kind,
        "decision": decision,
        "turn": state.turn_number,
        "waiting_for": waiting_for_name(&state.waiting_for),
        "detail": detail,
        "recent": TRACE.with(|trace| {
            trace.borrow().iter().rev().take(8).rev().cloned().collect::<Vec<_>>()
        }),
    })
}

fn waiting_for_name(waiting_for: &WaitingFor) -> String {
    let debug = format!("{waiting_for:?}");
    debug
        .split(|c: char| !c.is_alphanumeric())
        .next()
        .unwrap_or_default()
        .to_string()
}

fn play_game(db: &CardDatabase, decks: &[Vec<String>; 2], seed: u64) -> GameReport {
    let deck_list = DeckList {
        booster_pack_pool: None,
        player: PlayerDeckList {
            main_deck: decks[0].clone(),
            ..Default::default()
        },
        opponent: PlayerDeckList {
            main_deck: decks[1].clone(),
            ..Default::default()
        },
        ai_decks: vec![],
        ai_difficulties: vec![],
        draft_set_codes: vec![],
    };
    let payload = resolve_deck_list(db, &deck_list);
    let mut state = GameState::new_two_player(seed);
    load_and_hydrate_decks(&mut state, &payload, Some(db));
    start_game_with_starting_player(&mut state, PlayerId((seed % 2) as u8));

    let mut rng = Rng(seed);
    let mut report = GameReport {
        seed,
        decisions: 0,
        turns: 0,
        outcome: "decision_cap",
        pending_cast_decisions: 0,
        violations: Vec::new(),
    };
    // One report per distinct message keeps a persistent fault from flooding.
    let mut reported: HashSet<String> = HashSet::new();
    for decision in 0..MAX_DECISIONS {
        report.decisions = decision;
        report.turns = state.turn_number;
        for message in zone_violations(&state) {
            if reported.insert(message.clone()) {
                report
                    .violations
                    .push(violation("zone", decision, &state, message));
            }
        }
        if pending_casts(&state) > 0 {
            report.pending_cast_decisions += 1;
            // A cast still pending once a player has priority was never finished.
            if matches!(
                state.waiting_for,
                WaitingFor::Priority { .. } | WaitingFor::GameOver { .. }
            ) && reported.insert("pending_cast_at_priority".to_string())
            {
                report.violations.push(violation(
                    "pending_cast_at_priority",
                    decision,
                    &state,
                    "a spell is on the stack but its card never left its origin zone".to_string(),
                ));
            }
        }
        if matches!(state.waiting_for, WaitingFor::GameOver { .. }) {
            report.outcome = "game_over";
            return report;
        }
        let Some(owner) = state.waiting_for.acting_players().first().copied() else {
            report.outcome = "stuck";
            report.violations.push(violation(
                "no_actor",
                decision,
                &state,
                "non-final state has no acting player".to_string(),
            ));
            return report;
        };
        let contract = AiDecisionContract::issue(&state, owner);
        if contract.candidates.is_empty() {
            report.outcome = "stuck";
            report.violations.push(violation(
                "no_candidate",
                decision,
                &state,
                "non-final state offers no action".to_string(),
            ));
            return report;
        }
        let action = contract.candidates[rng.below(contract.candidates.len())]
            .action
            .clone();
        let action_json = serde_json::to_string(&action).unwrap_or_default();
        let action_for_signature = action.clone();
        let source = action
            .related_object_ids()
            .first()
            .and_then(|id| state.objects.get(id))
            .map(|object| object.name.clone())
            .unwrap_or_default();
        let shown: String = action_json.chars().take(240).collect();
        trace_push(format!(
            "{decision} t{} life {}/{} P{} {} [{source}] {shown}",
            state.turn_number,
            state.players[0].life,
            state.players[1].life,
            owner.0,
            waiting_for_name(&state.waiting_for)
        ));
        match apply_ai_action_proposal(&mut state, &contract, contract.authorized_actor, action) {
            AiProposalApplication::AppliedAction { .. }
            | AiProposalApplication::AppliedStackPass { .. } => {}
            AiProposalApplication::Stale => {
                report.outcome = "stuck";
                report.violations.push(violation(
                    "offered_action_stale",
                    decision,
                    &state,
                    action_json,
                ));
                return report;
            }
            AiProposalApplication::Rejected { rejection } => {
                let names: Vec<String> = rejection
                    .related_object_ids
                    .iter()
                    .filter_map(|id| state.objects.get(id))
                    .map(|object| object.name.clone())
                    .collect();
                let battlefield: Vec<String> = state
                    .battlefield
                    .iter()
                    .filter_map(|id| state.objects.get(id))
                    .map(|object| {
                        format!(
                            "P{} {}{}",
                            object.controller.0,
                            object.name,
                            if object.tapped { " (tapped)" } else { "" }
                        )
                    })
                    .collect();
                let message = format!(
                    "{action_json} rejected: {} [{:?}] objects {names:?} battlefield {battlefield:?} waiting_for {}",
                    rejection.message,
                    rejection.code,
                    serde_json::to_string(&state.waiting_for).unwrap_or_default()
                );
                let action_type = serde_json::to_value(&action_for_signature)
                    .ok()
                    .and_then(|value| value["type"].as_str().map(str::to_string))
                    .unwrap_or_default();
                let signature = format!(
                    "offered_action_rejected|{}|{action_type}|{:?}|{}",
                    waiting_for_name(&state.waiting_for),
                    rejection.code,
                    names.join(";")
                );
                if reported.insert(signature.clone()) {
                    let mut entry = violation("offered_action_rejected", decision, &state, message);
                    entry["signature"] = json!(signature);
                    report.violations.push(entry);
                }
            }
        }
    }
    report.violations.push(violation(
        "decision_cap",
        MAX_DECISIONS,
        &state,
        format!(
            "game not over after {MAX_DECISIONS} decisions; phase {:?}, active P{}, stack {:?}",
            state.phase,
            state.active_player.0,
            state
                .stack
                .iter()
                .map(|entry| state
                    .objects
                    .get(&entry.source_id)
                    .map(|object| object.name.clone())
                    .unwrap_or_default())
                .collect::<Vec<_>>()
        ),
    ));
    report
}

fn read_deck(path: &str) -> Vec<String> {
    std::fs::read_to_string(path)
        .unwrap_or_else(|e| panic!("cannot read deck {path}: {e}"))
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .flat_map(|line| {
            let (count, name) = line
                .split_once(' ')
                .unwrap_or_else(|| panic!("deck line needs `N Card Name`: {line}"));
            let count: usize = count
                .parse()
                .unwrap_or_else(|_| panic!("deck line needs a count: {line}"));
            std::iter::repeat_n(name.to_string(), count)
        })
        .collect()
}

/// One signature per line; blank lines and `#` comments are ignored. Each entry
/// is an offered-but-rejected action already recorded in `STATUS.md`.
fn read_known_signatures(path: &str) -> HashSet<String> {
    std::fs::read_to_string(path)
        .unwrap_or_else(|e| panic!("cannot read known signatures {path}: {e}"))
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .map(str::to_string)
        .collect()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 6 && args.len() != 7 {
        eprintln!(
            "usage: invariant-fuzz <data-root> <deck-a.txt> <deck-b.txt> <first-seed> <games> [known-signatures.txt]"
        );
        std::process::exit(2);
    }
    let db = CardDatabase::from_export(
        std::path::Path::new(&args[1])
            .join("card-data.json")
            .as_path(),
    )
    .unwrap_or_else(|e| panic!("failed to load card database from {}: {e}", args[1]));
    let decks = [read_deck(&args[2]), read_deck(&args[3])];
    let first_seed: u64 = args[4].parse().expect("first-seed must be an integer");
    let games: u64 = args[5].parse().expect("games must be an integer");

    let known: HashSet<String> = args
        .get(6)
        .map(|path| read_known_signatures(path))
        .unwrap_or_default();

    std::panic::set_hook(Box::new(|info| {
        let location = info.location().map(|l| l.to_string()).unwrap_or_default();
        PANIC_LOCATION.with(|slot| *slot.borrow_mut() = location);
    }));
    let trace_wanted = std::env::var_os("FUZZ_TRACE").is_some();
    let mut totals: HashMap<String, u64> = HashMap::new();
    let mut games_with_violations = 0u64;
    for seed in first_seed..first_seed + games {
        trace_take();
        let result = catch_unwind(AssertUnwindSafe(|| play_game(&db, &decks, seed)));
        let line = match result {
            Ok(mut report) => {
                let mut unknown = 0;
                for v in &mut report.violations {
                    let is_known = v["signature"].as_str().is_some_and(|s| known.contains(s));
                    v["known"] = json!(is_known);
                    let kind = v["kind"].as_str().unwrap().to_string();
                    if is_known {
                        *totals.entry(format!("known:{kind}")).or_default() += 1;
                    } else {
                        unknown += 1;
                        *totals.entry(kind).or_default() += 1;
                    }
                }
                *totals
                    .entry(format!("outcome:{}", report.outcome))
                    .or_default() += 1;
                if unknown > 0 {
                    games_with_violations += 1;
                }
                json!({
                    "seed": report.seed,
                    "outcome": report.outcome,
                    "decisions": report.decisions,
                    "turns": report.turns,
                    "pending_cast_decisions": report.pending_cast_decisions,
                    "violations": report.violations,
                })
            }
            Err(payload) => {
                let message = payload
                    .downcast_ref::<String>()
                    .cloned()
                    .or_else(|| payload.downcast_ref::<&str>().map(|s| s.to_string()))
                    .unwrap_or_else(|| "non-string panic".to_string());
                *totals.entry("panic".to_string()).or_default() += 1;
                games_with_violations += 1;
                let location = PANIC_LOCATION.with(|slot| slot.borrow().clone());
                json!({"seed": seed, "outcome": "panic", "violations": [{"kind": "panic", "detail": message, "location": location}]})
            }
        };
        let mut line = line;
        if trace_wanted {
            line["trace"] = json!(trace_take());
        }
        println!("{line}");
    }
    println!(
        "{}",
        json!({"summary": true, "games": games, "games_with_violations": games_with_violations, "totals": totals})
    );
    if games_with_violations > 0 {
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine::game::game_object::GameObject;
    use engine::types::identifiers::CardId;

    fn state_with_hand_card() -> (GameState, ObjectId) {
        let mut state = GameState::new_two_player(1);
        let id = ObjectId(900);
        state.objects.insert(
            id,
            GameObject::new(id, CardId(1), PlayerId(0), "Probe".to_string(), Zone::Hand),
        );
        state.players[0].hand.push_back(id);
        (state, id)
    }

    #[test]
    fn a_consistent_state_has_no_zone_violation() {
        let (state, _) = state_with_hand_card();
        assert_eq!(zone_violations(&state), Vec::<String>::new());
    }

    #[test]
    fn a_card_listed_in_two_zones_is_reported() {
        let (mut state, id) = state_with_hand_card();
        state.players[0].graveyard.push_back(id);
        let violations = zone_violations(&state);
        assert!(violations
            .iter()
            .any(|v| v.contains("is listed in P0 hand and in P0 graveyard")));
        assert!(violations
            .iter()
            .any(|v| v.contains("P0 graveyard but its zone is Hand")));
    }

    #[test]
    fn a_card_in_no_zone_list_is_reported() {
        let (mut state, _) = state_with_hand_card();
        state.players[0].hand.clear();
        assert_eq!(
            zone_violations(&state),
            vec!["object 900 (Probe) has zone Hand but is in no zone list".to_string()]
        );
    }

    #[test]
    fn a_listed_id_without_an_object_is_reported() {
        let (mut state, _) = state_with_hand_card();
        state.exile.push_back(ObjectId(901));
        assert_eq!(
            zone_violations(&state),
            vec!["exile lists missing object 901".to_string()]
        );
    }

    #[test]
    fn the_generator_replays_a_seed() {
        let mut a = Rng(7);
        let mut b = Rng(7);
        let first: Vec<usize> = (0..5).map(|_| a.below(10)).collect();
        assert_eq!(first, (0..5).map(|_| b.below(10)).collect::<Vec<_>>());
        assert!(first.iter().all(|value| *value < 10));
    }
}
