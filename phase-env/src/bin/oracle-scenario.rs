use engine::database::CardDatabase;
use engine::game::ability_utils::build_target_slots;
use engine::game::scenario::GameScenario;
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

fn cards(state: &GameState, player: usize, zone: &str) -> Vec<String> {
    let p = &state.players[player];
    match zone {
        "hand" => names(state, p.hand.iter().copied()),
        "library" => names(state, p.library.iter().copied()),
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
        for (id, item) in &configured {
            let object = state.objects.get_mut(id).expect("setup object exists");
            object.tapped = item["tapped"].as_bool().unwrap_or(false);
            object.damage_marked = item["damage"].as_u64().unwrap_or(0) as u32;
            if let Some(counters) = item["counters"].as_object() {
                for (name, count) in counters {
                    object.counters.insert(
                        parse_counter_type(name),
                        count.as_u64().ok_or("counter count must be non-negative")? as u32,
                    );
                }
            }
        }
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
                let turn = runner.state().turn_number;
                for _ in 0..64 {
                    if runner.state().turn_number != turn {
                        break;
                    }
                    if !runner.state().stack.is_empty() {
                        runner.advance_until_stack_empty();
                    } else {
                        runner
                            .act(GameAction::PassPriority)
                            .map_err(|error| error.to_string())?;
                    }
                }
                if runner.state().turn_number == turn {
                    return Err("turn did not advance within 64 priority passes".to_string());
                }
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
}
