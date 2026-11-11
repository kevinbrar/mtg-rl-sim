use engine::database::CardDatabase;
use engine::game::ability_utils::build_target_slots;
use engine::game::scenario::{GameRunner, GameScenario};
use engine::game::scenario_db::GameScenarioDbExt;
use engine::types::{
    actions::GameAction, parse_counter_type, AbilityKind, GameState, ObjectId, Phase, PlayerId,
    ResolvedAbility, TargetRef, WaitingFor, Zone,
};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::Path;

fn card_name(state: &GameState, id: ObjectId) -> String {
    state.objects.get(&id).expect("object exists").name.clone()
}

fn names(state: &GameState, ids: impl IntoIterator<Item = ObjectId>) -> Vec<String> {
    let mut names = ids
        .into_iter()
        .map(|id| card_name(state, id))
        .collect::<Vec<_>>();
    names.sort();
    names
}

fn ordered_names(state: &GameState, ids: impl IntoIterator<Item = ObjectId>) -> Vec<String> {
    ids.into_iter().map(|id| card_name(state, id)).collect()
}

fn cards(state: &GameState, player: usize, zone: &str) -> Vec<String> {
    let p = &state.players[player];
    match zone {
        "hand" => names(state, p.hand.iter().copied()),
        "library" => ordered_names(state, p.library.iter().copied()),
        "graveyard" => names(state, p.graveyard.iter().copied()),
        "battlefield" => names(
            state,
            state.battlefield.iter().copied().filter(|id| {
                state
                    .objects
                    .get(id)
                    .is_some_and(|object| object.controller.0 as usize == player)
            }),
        ),
        _ => panic!("unsupported zone {zone}"),
    }
}

fn project(
    state: &GameState,
    labels: &HashMap<ObjectId, String>,
    legal_targets: Option<Vec<String>>,
    reached: bool,
) -> Value {
    let players = (0..2)
        .map(|index| {
            (
                format!("P{index}"),
                json!({
                    "life": state.players[index].life,
                    "battlefield": cards(state, index, "battlefield"),
                    "hand": cards(state, index, "hand"),
                    "library": cards(state, index, "library"),
                    "graveyard": cards(state, index, "graveyard"),
                }),
            )
        })
        .collect::<serde_json::Map<_, _>>();
    let mut battlefield = state
        .battlefield
        .iter()
        .filter_map(|id| {
            let object = state.objects.get(id)?;
            let counters = object
                .counters
                .iter()
                .map(|(kind, count)| (kind.as_str().to_string(), json!(count)))
                .collect::<serde_json::Map<_, _>>();
            Some(json!({
                "label": labels.get(id),
                "name": &object.name,
                "controller": format!("P{}", object.controller.0),
                "tapped": object.tapped,
                "damage": object.damage_marked,
                "counters": counters,
                "power": object.power,
                "toughness": object.toughness,
                "loyalty": object.counters.get(&parse_counter_type("loyalty")),
            }))
        })
        .collect::<Vec<_>>();
    battlefield
        .sort_by_key(|permanent| permanent["label"].as_str().unwrap_or_default().to_string());
    json!({
        "players": players,
        "battlefield": battlefield,
        "exile": names(state, state.exile.iter().copied()),
        "legal_targets": legal_targets,
        "reach": reached,
    })
}

fn apply_configured_permanents(
    state: &mut GameState,
    configured: &[(ObjectId, Value)],
) -> Result<(), String> {
    let mut has_counters = false;
    for (id, item) in configured {
        let object = state.objects.get_mut(id).expect("setup object exists");
        object.tapped = item["tapped"].as_bool().unwrap_or(false);
        object.damage_marked = item["damage"].as_u64().unwrap_or(0) as u32;
        if let Some(counters) = item["counters"].as_object() {
            has_counters |= !counters.is_empty();
            for (name, count) in counters {
                object.counters.insert(
                    parse_counter_type(name),
                    count.as_u64().ok_or("counter count must be non-negative")? as u32,
                );
            }
        }
    }
    if has_counters {
        engine::game::layers::mark_layers_full(state);
        engine::game::layers::flush_layers(state);
    }
    Ok(())
}

fn advance_to_next_turn(runner: &mut GameRunner) -> Result<(), String> {
    let turn = runner.state().turn_number;
    for _ in 0..64 {
        if runner.state().turn_number != turn {
            return Ok(());
        }
        let waiting_for = runner.state().waiting_for.clone();
        if matches!(waiting_for, WaitingFor::Priority { .. }) && !runner.state().stack.is_empty() {
            runner.advance_until_stack_empty();
            continue;
        }
        let action = match waiting_for {
            WaitingFor::Priority { .. } => GameAction::PassPriority,
            WaitingFor::DeclareAttackers { .. } => GameAction::DeclareAttackers {
                attacks: Vec::new(),
                bands: Vec::new(),
            },
            WaitingFor::DeclareBlockers { .. } => GameAction::DeclareBlockers {
                assignments: Vec::new(),
            },
            other => {
                return Err(format!("cannot advance turn while waiting for {other:?}"));
            }
        };
        runner.act(action).map_err(|error| error.to_string())?;
    }
    Err("turn did not advance within 64 actions".to_string())
}

fn run_one(spec: &Value, db: &CardDatabase) -> Result<Value, String> {
    let name = spec["name"].as_str().ok_or("scenario name is required")?;
    let active_index = spec["active_player"].as_u64().unwrap_or(0);
    if active_index > 1 {
        return Err("active_player must be 0 or 1".to_string());
    }
    let active = PlayerId(active_index as u8);
    let step = spec["step"].as_str().unwrap_or("precombat_main");
    if step != "precombat_main" {
        return Err(format!("unsupported step {step}"));
    }
    let mut scenario = GameScenario::new();
    scenario.at_phase(Phase::PreCombatMain);
    let mut labels = HashMap::<ObjectId, String>::new();
    let mut configured = Vec::new();
    let mut library_top = [Vec::new(), Vec::new()];
    for (index, player_library_top) in library_top.iter_mut().enumerate() {
        let key = format!("P{index}");
        let setup = &spec["setup"][&key];
        for (field, zone) in [
            ("battlefield", Zone::Battlefield),
            ("hand", Zone::Hand),
            ("library_top", Zone::Library),
            ("graveyard", Zone::Graveyard),
        ] {
            let Some(items) = setup[field].as_array() else {
                continue;
            };
            for item in items {
                let card = item
                    .as_str()
                    .or_else(|| item["card"].as_str())
                    .ok_or_else(|| format!("{key}.{field} needs card names"))?;
                let id = scenario.add_real_card(PlayerId(index as u8), card, zone, db);
                if field == "library_top" {
                    player_library_top.push(id);
                }
                if field == "battlefield" {
                    let label = item["label"]
                        .as_str()
                        .ok_or_else(|| format!("{key}.battlefield needs labels"))?;
                    labels.insert(id, label.to_string());
                    configured.push((id, item.clone()));
                }
            }
        }
    }
    let mut runner = scenario.build();
    {
        let state = runner.state_mut();
        state.active_player = active;
        state.priority_player = active;
        state.waiting_for = WaitingFor::Priority { player: active };
        for (index, (player, player_library_top)) in
            state.players.iter_mut().zip(&library_top).enumerate()
        {
            let key = format!("P{index}");
            let setup = &spec["setup"][&key];
            player.life = setup["life"].as_i64().unwrap_or(20) as i32;
            player.library = player_library_top.iter().copied().collect();
        }
        apply_configured_permanents(state, &configured)?;
    }
    let mut legal = None;
    if let Some(card) = spec["legal_targets_of"].as_str() {
        let spell = runner.state().players[active.0 as usize]
            .hand
            .iter()
            .copied()
            .find(|id| {
                runner
                    .state()
                    .objects
                    .get(id)
                    .is_some_and(|object| object.name == card)
            })
            .ok_or_else(|| format!("{card} is not in the active player's hand"))?;
        let definition = runner.state().objects[&spell]
            .abilities
            .first()
            .ok_or_else(|| format!("{card} has no spell ability"))?;
        let mut resolved = ResolvedAbility::new(
            definition.effect.as_ref().clone(),
            Vec::new(),
            spell,
            active,
        );
        resolved.kind = AbilityKind::Spell;
        let slots =
            build_target_slots(runner.state(), &resolved).map_err(|error| error.to_string())?;
        if slots.len() != 1 {
            return Err(format!(
                "{card} has {} target slots; one is required",
                slots.len()
            ));
        }
        let mut targets = slots[0]
            .legal_targets
            .iter()
            .map(|target| match target {
                TargetRef::Object(id) => labels
                    .get(id)
                    .cloned()
                    .ok_or_else(|| format!("target {:?} has no setup label", id)),
                TargetRef::Player(_) => Err("player targets are unsupported".to_string()),
            })
            .collect::<Result<Vec<_>, _>>()?;
        targets.sort();
        legal = Some(targets);
    }
    for action in spec["script"].as_array().into_iter().flatten() {
        match action["action"].as_str().unwrap_or_default() {
            "cast" => {
                let card = action["card"].as_str().ok_or("cast needs card")?;
                let player = runner.state().active_player;
                let spell = runner.state().players[player.0 as usize]
                    .hand
                    .iter()
                    .copied()
                    .find(|id| {
                        runner
                            .state()
                            .objects
                            .get(id)
                            .is_some_and(|object| object.name == card)
                    })
                    .ok_or_else(|| format!("{card} is not in the active player's hand"))?;
                let target_ids = action["targets"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|label| {
                        let label = label.as_str().ok_or("target label must be a string")?;
                        labels
                            .iter()
                            .find(|(_, value)| value == &label)
                            .map(|(id, _)| *id)
                            .ok_or_else(|| format!("unknown target label {label}"))
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                let _ = runner.cast(spell).target_objects(&target_ids).commit();
            }
            "pass_until_resolved" => {
                runner.advance_until_stack_empty();
            }
            "end_turn" | "next_turn" => {
                advance_to_next_turn(&mut runner)?;
            }
            other => return Err(format!("unsupported action {other}")),
        }
    }
    let reached = if let Some(reach) = spec.get("reach") {
        let player = reach["player"].as_str().unwrap_or("P0");
        let zone = reach["zone"].as_str().unwrap_or("graveyard");
        let card = reach["card"].as_str().ok_or("reach needs card")?;
        let index = player
            .strip_prefix('P')
            .and_then(|value| value.parse::<usize>().ok())
            .ok_or("reach player must be P0 or P1")?;
        if index > 1 {
            return Err("reach player must be P0 or P1".to_string());
        }
        cards(runner.state(), index, zone)
            .iter()
            .any(|name| name == card)
    } else {
        true
    };
    Ok(json!({
        "scenario": name,
        "projection": project(runner.state(), &labels, legal, reached),
        "reach": reached,
    }))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let data_path = args.next().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "card-data path required")
    })?;
    let spec_path = args.next().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "scenario JSON required")
    })?;
    let database = CardDatabase::from_export(Path::new(&data_path))?;
    let spec: Value = serde_json::from_slice(&std::fs::read(spec_path)?)?;
    let results = spec["scenarios"]
        .as_array()
        .ok_or("scenarios must be an array")?
        .iter()
        .map(|scenario| match run_one(scenario, &database) {
            Ok(result) => result,
            Err(error) => json!({"scenario": scenario["name"], "error": error}),
        })
        .collect::<Vec<_>>();
    println!("{}", serde_json::to_string(&results)?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn projection_includes_reach_and_sorted_zones() {
        let runner = GameScenario::new().build();
        let value = project(runner.state(), &HashMap::new(), None, true);
        assert_eq!(value["players"]["P0"]["life"], 20);
        assert_eq!(value["players"]["P1"]["graveyard"], json!([]));
        assert_eq!(value["exile"], json!([]));
        assert_eq!(value["reach"], json!(true));
    }

    #[test]
    fn turn_advancement_declares_no_attackers_with_ready_creature() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let creature = scenario
                    .add_creature(PlayerId(0), "Untapped Bear", 2, 2)
                    .id();
                let mut runner = scenario.build();
                assert!(!runner.state().objects[&creature].tapped);
                assert!(!runner.state().objects[&creature].summoning_sick);
                let turn = runner.state().turn_number;

                advance_to_next_turn(&mut runner).unwrap();

                assert_ne!(runner.state().turn_number, turn);
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn setup_plus_one_counter_recomputes_effective_power_and_toughness() {
        let mut scenario = GameScenario::new();
        let creature = scenario
            .add_creature(PlayerId(0), "Counter Bear", 2, 2)
            .id();
        let mut runner = scenario.build();
        let configured = vec![(creature, json!({"counters": {"+1/+1": 1}}))];

        apply_configured_permanents(runner.state_mut(), &configured).unwrap();

        let labels = HashMap::from([(creature, "p0.bf.0".to_string())]);
        let value = project(runner.state(), &labels, None, true);
        assert_eq!(value["battlefield"][0]["power"], json!(3));
        assert_eq!(value["battlefield"][0]["toughness"], json!(3));
    }

    #[test]
    fn projection_preserves_library_order_with_top_first() {
        let mut scenario = GameScenario::new();
        scenario.add_spell_to_library_top(PlayerId(0), "Bottom Card", true);
        scenario.add_spell_to_library_top(PlayerId(0), "Top Card", true);
        let runner = scenario.build();

        let value = project(runner.state(), &HashMap::new(), None, true);

        assert_eq!(
            value["players"]["P0"]["library"],
            json!(["Top Card", "Bottom Card"])
        );
    }
}
