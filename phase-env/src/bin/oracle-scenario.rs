use engine::database::CardDatabase;
use engine::game::ability_utils::build_target_slots;
use engine::game::scenario::{GameRunner, GameScenario};
use engine::game::scenario_db::GameScenarioDbExt;
use engine::types::{
    actions::GameAction, parse_counter_type, AbilityKind, GameState, ManaColor, ObjectId, Phase,
    PlayerId, ResolvedAbility, TargetRef, WaitingFor, Zone,
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
                "token": object.is_token,
                "controller": format!("P{}", object.controller.0),
                "tapped": object.tapped,
                "types": sorted(object.card_types.core_types.iter().map(|kind| normalize_characteristic(&kind.to_string()))),
                "subtypes": sorted(object.card_types.subtypes.iter().map(|kind| normalize_characteristic(kind))),
                "colors": sorted(object.effective_colors().iter().map(|color| match color {
                    ManaColor::White => "W".to_string(),
                    ManaColor::Blue => "U".to_string(),
                    ManaColor::Black => "B".to_string(),
                    ManaColor::Red => "R".to_string(),
                    ManaColor::Green => "G".to_string(),
                })),
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

fn sorted(values: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut values = values.into_iter().collect::<Vec<_>>();
    values.sort();
    values
}

fn normalize_characteristic(value: &str) -> String {
    value
        .split_whitespace()
        .map(|word| {
            let mut chars = word.chars();
            chars
                .next()
                .map(|first| {
                    format!(
                        "{}{}",
                        first.to_uppercase().collect::<String>(),
                        chars.as_str().to_lowercase()
                    )
                })
                .unwrap_or_default()
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn resolve_label(labels: &HashMap<ObjectId, String>, label: &str) -> Result<ObjectId, String> {
    labels
        .iter()
        .find(|(_, value)| value.as_str() == label)
        .map(|(id, _)| *id)
        .ok_or_else(|| format!("unknown battlefield label {label}"))
}

fn normalize_ability_text(text: &str) -> String {
    let text = text.split("<i>").next().unwrap_or(text);
    let mut stripped = String::new();
    let mut in_tag = false;
    for character in text.chars() {
        match character {
            '<' => in_tag = true,
            '>' => in_tag = false,
            _ if !in_tag => stripped.push(character),
            _ => {}
        }
    }
    stripped
        .to_lowercase()
        .replace("{this}", "~")
        .trim()
        .to_string()
}

fn select_activation_ability(
    candidates: impl IntoIterator<Item = (usize, AbilityKind, Option<String>)>,
    prefix: &str,
) -> Result<usize, String> {
    let prefix = normalize_ability_text(prefix);
    if prefix.is_empty() {
        return Err("activate needs a non-empty ability cost prefix".to_string());
    }
    let mut matches = Vec::new();
    for (index, kind, description) in candidates {
        if kind != AbilityKind::Activated {
            continue;
        }
        let description = description
            .ok_or_else(|| format!("activated ability {index} is missing a description"))?;
        if normalize_ability_text(&description).starts_with(&prefix) {
            matches.push(index);
        }
    }
    match matches.as_slice() {
        [index] => Ok(*index),
        [] => Err(format!("no activated ability starts with {prefix}")),
        _ => Err(format!(
            "multiple activated abilities start with {prefix}: {matches:?}"
        )),
    }
}

fn resolve_target_labels(
    action: &Value,
    labels: &HashMap<ObjectId, String>,
) -> Result<Vec<ObjectId>, String> {
    action["targets"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|label| {
            let label = label.as_str().ok_or("target label must be a string")?;
            resolve_label(labels, label).map_err(|_| format!("unknown target label {label}"))
        })
        .collect()
}

fn activate(
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    action: &Value,
) -> Result<(), String> {
    let source_label = action["source"].as_str().ok_or("activate needs source")?;
    let source_id = resolve_label(labels, source_label)?;
    let prefix = action["ability"].as_str().ok_or("activate needs ability")?;
    let object = runner
        .state()
        .objects
        .get(&source_id)
        .ok_or_else(|| format!("source label {source_label} is not on the battlefield"))?;
    let ability_index = select_activation_ability(
        object
            .abilities
            .iter()
            .enumerate()
            .map(|(index, ability)| (index, ability.kind, ability.description.clone())),
        prefix,
    )?;
    let target_ids = resolve_target_labels(action, labels)?;
    let stack_size = runner.state().stack.len();
    runner
        .act(GameAction::ActivateAbility {
            source_id,
            ability_index,
            payment_mode: engine::types::game_state::CastPaymentMode::Auto,
        })
        .map_err(|error| error.to_string())?;

    let mut target_index = 0;
    for _ in 0..64 {
        match runner.state().waiting_for.clone() {
            WaitingFor::TargetSelection {
                target_slots,
                selection,
                ..
            } => {
                let slot = target_slots
                    .get(selection.current_slot)
                    .ok_or("activation target slot is missing")?;
                let target = target_ids.get(target_index).copied().map(TargetRef::Object);
                if target
                    .as_ref()
                    .is_some_and(|target| !slot.legal_targets.contains(target))
                {
                    return Err(format!(
                        "target label {} is not legal for activation slot {}",
                        labels[&target_ids[target_index]], selection.current_slot
                    ));
                }
                let selected = target.is_some();
                runner
                    .act(GameAction::ChooseTarget { target })
                    .map_err(|error| error.to_string())?;
                if selected {
                    target_index += 1;
                }
            }
            WaitingFor::Priority { .. } => break,
            WaitingFor::ManaPayment { .. } => {
                return Err("CastPaymentMode::Auto did not pay the activation cost".to_string());
            }
            other => {
                return Err(format!("activation cannot handle waiting state {other:?}"));
            }
        }
    }
    if target_index != target_ids.len() {
        return Err(format!(
            "activation received {} targets but used {target_index}",
            target_ids.len()
        ));
    }
    if runner.state().stack.len() <= stack_size {
        return Err("activated ability did not commit to the stack".to_string());
    }
    if !matches!(&runner.state().waiting_for, WaitingFor::Priority { .. }) {
        return Err("activation did not finish target selection".to_string());
    }
    Ok(())
}

fn apply_configured_permanents(
    state: &mut GameState,
    configured: &[(ObjectId, Value)],
) -> Result<(), String> {
    let mut has_counters = false;
    for (id, item) in configured {
        let object = state.objects.get_mut(id).expect("setup object exists");
        object.tapped = item["tapped"].as_bool().unwrap_or(false);
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

fn validate_setup(scenario: &Value) -> Result<(), String> {
    let setup = scenario["setup"]
        .as_object()
        .ok_or("setup must be an object")?;
    for (player, player_setup) in setup {
        if player != "P0" && player != "P1" {
            return Err(format!("setup.{player} is unsupported"));
        }
        let player_setup = player_setup
            .as_object()
            .ok_or_else(|| format!("setup.{player} must be an object"))?;
        for field in player_setup.keys() {
            if !["life", "battlefield", "hand", "library_top", "graveyard"]
                .contains(&field.as_str())
            {
                return Err(format!("setup.{player}.{field} is unsupported"));
            }
        }
        for field in ["hand", "library_top", "graveyard"] {
            let Some(value) = player_setup.get(field) else {
                continue;
            };
            let items = value
                .as_array()
                .ok_or_else(|| format!("setup.{player}.{field} must be an array"))?;
            for (index, item) in items.iter().enumerate() {
                if let Some(card) = item.as_str() {
                    if card.is_empty() {
                        return Err(format!("setup.{player}.{field}[{index}] needs a card"));
                    }
                    continue;
                }
                let card = item
                    .as_object()
                    .ok_or_else(|| format!("setup.{player}.{field}[{index}] needs a card"))?;
                if card.len() != 1 || !card.contains_key("card") || !card["card"].is_string() {
                    return Err(format!(
                        "setup.{player}.{field}[{index}] only supports card"
                    ));
                }
            }
        }
        if let Some(value) = player_setup.get("battlefield") {
            let battlefield = value
                .as_array()
                .ok_or_else(|| format!("setup.{player}.battlefield must be an array"))?;
            for (index, permanent) in battlefield.iter().enumerate() {
                let context = format!("{player}.battlefield[{index}]");
                let permanent = permanent
                    .as_object()
                    .ok_or_else(|| format!("{context} must be an object"))?;
                if permanent.contains_key("damage") {
                    return Err(format!(
                        "{context}.damage is unsupported by the XMage scenario test API"
                    ));
                }
                for field in permanent.keys() {
                    if !["label", "card", "tapped", "counters"].contains(&field.as_str()) {
                        return Err(format!("{context}.{field} is unsupported"));
                    }
                }
                if !permanent.get("label").is_some_and(Value::is_string)
                    || !permanent.get("card").is_some_and(Value::is_string)
                {
                    return Err(format!("{context} needs label and card"));
                }
                if permanent
                    .get("tapped")
                    .is_some_and(|value| !value.is_boolean())
                {
                    return Err(format!("{context}.tapped must be a boolean"));
                }
                if permanent
                    .get("counters")
                    .is_some_and(|value| !value.is_object())
                {
                    return Err(format!("{context}.counters must be an object"));
                }
            }
        }
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
    validate_setup(spec)?;
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
                let target_ids = resolve_target_labels(action, &labels)?;
                let _ = runner.cast(spell).target_objects(&target_ids).commit();
            }
            "activate" => activate(&mut runner, &labels, action)?,
            "pass_until_resolved" => {
                runner.advance_until_stack_empty();
            }
            "end_turn" | "next_turn" => {
                advance_to_next_turn(&mut runner)?;
            }
            other => return Err(format!("unsupported action {other}")),
        }
    }
    let reached = check_reach(runner.state(), &labels, spec.get("reach"))?;
    Ok(json!({
        "scenario": name,
        "projection": project(runner.state(), &labels, legal, reached),
        "reach": reached,
    }))
}

fn check_reach(
    state: &GameState,
    labels: &HashMap<ObjectId, String>,
    reach: Option<&Value>,
) -> Result<bool, String> {
    let Some(reach) = reach else {
        return Ok(true);
    };
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
    let in_zone = cards(state, index, zone).iter().any(|name| name == card);
    if !in_zone {
        return Ok(false);
    }
    if let Some(tapped_labels) = reach.get("tapped_labels") {
        let Some(tapped_labels) = tapped_labels.as_array() else {
            return Err("reach.tapped_labels must be an array".to_string());
        };
        for label in tapped_labels {
            let label = label
                .as_str()
                .ok_or("reach.tapped_labels must be strings")?;
            let id = resolve_label(labels, label)?;
            if !state.objects.get(&id).is_some_and(|object| object.tapped) {
                return Ok(false);
            }
        }
    }
    let Some(object) = (zone == "battlefield")
        .then(|| {
            state.battlefield.iter().find_map(|id| {
                state
                    .objects
                    .get(id)
                    .filter(|object| object.controller.0 as usize == index && object.name == card)
            })
        })
        .flatten()
    else {
        return Ok(![
            "types",
            "subtypes",
            "colors",
            "power",
            "toughness",
            "creature",
            "tapped",
        ]
        .iter()
        .any(|key| reach.get(key).is_some()));
    };

    for (key, actual) in [
        (
            "types",
            sorted(object.card_types.core_types.iter().map(ToString::to_string)),
        ),
        (
            "subtypes",
            sorted(object.card_types.subtypes.iter().cloned()),
        ),
        (
            "colors",
            sorted(object.effective_colors().iter().map(|color| match color {
                ManaColor::White => "W".to_string(),
                ManaColor::Blue => "U".to_string(),
                ManaColor::Black => "B".to_string(),
                ManaColor::Red => "R".to_string(),
                ManaColor::Green => "G".to_string(),
            })),
        ),
    ] {
        if let Some(expected) = reach.get(key) {
            let Some(expected) = expected.as_array() else {
                return Err(format!("reach.{key} must be an array"));
            };
            let mut expected = expected
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .map(str::to_string)
                        .ok_or_else(|| format!("reach.{key} values must be strings"))
                })
                .collect::<Result<Vec<_>, _>>()?;
            expected.sort();
            if expected != actual {
                return Ok(false);
            }
        }
    }
    for key in ["power", "toughness"] {
        if let Some(expected) = reach.get(key) {
            let actual = if key == "power" {
                json!(object.power)
            } else {
                json!(object.toughness)
            };
            if expected != &actual {
                return Ok(false);
            }
        }
    }
    if reach
        .get("power_is_null")
        .is_some_and(|expected| expected != &json!(object.power.is_none()))
        || reach
            .get("toughness_is_null")
            .is_some_and(|expected| expected != &json!(object.toughness.is_none()))
    {
        return Ok(false);
    }
    if let Some(expected) = reach.get("creature") {
        let Some(expected) = expected.as_bool() else {
            return Err("reach.creature must be a boolean".to_string());
        };
        let actual = object
            .card_types
            .core_types
            .iter()
            .any(|kind| kind.to_string() == "Creature");
        if actual != expected
            || !actual && (object.power.is_some() || object.toughness.is_some())
            || actual && (object.power.is_none() || object.toughness.is_none())
        {
            return Ok(false);
        }
    }
    if let Some(expected) = reach.get("tapped") {
        let Some(expected) = expected.as_bool() else {
            return Err("reach.tapped must be a boolean".to_string());
        };
        if object.tapped != expected {
            return Ok(false);
        }
    }
    Ok(true)
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
    let scenarios = spec["scenarios"]
        .as_array()
        .ok_or("scenarios must be an array")?;
    for scenario in scenarios {
        validate_setup(scenario)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidInput, error))?;
    }
    let results = scenarios
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
    fn rejects_setup_damage_fixtures() {
        let scenario = json!({
            "setup": {
                "P0": {
                    "battlefield": [
                        {"label": "p0.bf.0", "card": "Walking Corpse", "damage": 0}
                    ]
                }
            }
        });

        let error = validate_setup(&scenario).unwrap_err();

        assert!(error.contains("P0.battlefield[0].damage is unsupported"));
    }

    #[test]
    fn rejects_setup_fields_the_runners_cannot_express() {
        let scenario = json!({
            "name": "unsupported setup",
            "setup": {
                "P0": {"exile": ["Walking Corpse"]}
            }
        });

        let error = validate_setup(&scenario).unwrap_err();

        assert!(error.contains("setup.P0.exile is unsupported"));
    }

    #[test]
    fn resolves_setup_labels_and_rejects_unknown_labels() {
        let id = ObjectId(17);
        let labels = HashMap::from([(id, "prairie".to_string())]);

        assert_eq!(resolve_label(&labels, "prairie"), Ok(id));
        assert!(resolve_label(&labels, "unknown")
            .unwrap_err()
            .contains("unknown battlefield label"));
    }

    #[test]
    fn activation_selector_requires_one_described_prefix_match() {
        let candidates = vec![
            (0, AbilityKind::Activated, Some("{T}: Add {G}".to_string())),
            (
                1,
                AbilityKind::Activated,
                Some("{2}{G}{W}: {this} becomes a Llama".to_string()),
            ),
        ];
        assert_eq!(
            select_activation_ability(candidates.clone(), "{2}{G}{W}:"),
            Ok(1)
        );
        assert!(select_activation_ability(candidates.clone(), "{3}:").is_err());

        let multiple = vec![
            (0, AbilityKind::Activated, Some("{T}: First".to_string())),
            (1, AbilityKind::Activated, Some("{T}: Second".to_string())),
        ];
        assert!(select_activation_ability(multiple, "{T}:")
            .unwrap_err()
            .contains("multiple activated abilities"));

        let missing = vec![(0, AbilityKind::Activated, None)];
        assert!(select_activation_ability(missing, "{T}:")
            .unwrap_err()
            .contains("missing a description"));
    }

    #[test]
    fn activation_waits_for_target_selection_before_stack_commit() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let source = scenario
                    .add_creature_from_oracle(
                        PlayerId(0),
                        "Targeted activation fixture",
                        1,
                        1,
                        "{T}: This creature deals 1 damage to target creature.",
                    )
                    .id();
                let target = scenario
                    .add_creature(PlayerId(1), "Target creature", 1, 1)
                    .id();
                let mut runner = scenario.build();
                let labels = HashMap::from([
                    (source, "source".to_string()),
                    (target, "target".to_string()),
                ]);
                let stack_size = runner.state().stack.len();

                activate(
                    &mut runner,
                    &labels,
                    &json!({
                        "source": "source",
                        "ability": "{T}:",
                        "targets": ["target"]
                    }),
                )
                .unwrap();

                assert_eq!(runner.state().stack.len(), stack_size + 1);
                assert!(matches!(
                    runner.state().waiting_for,
                    WaitingFor::Priority { .. }
                ));
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn graveyard_reach_checks_tapped_labels() {
        let mut scenario = GameScenario::new();
        scenario.add_creature_to_graveyard(PlayerId(0), "Graveyard fixture", 1, 1);
        let forest = scenario.add_basic_land(PlayerId(0), ManaColor::Green);
        let mut runner = scenario.build();
        let labels = HashMap::from([(forest, "forest".to_string())]);
        let reach = json!({
            "player": "P0",
            "zone": "graveyard",
            "card": "Graveyard fixture",
            "tapped_labels": ["forest"]
        });

        assert_eq!(check_reach(runner.state(), &labels, Some(&reach)), Ok(false));
        runner.state_mut().objects.get_mut(&forest).unwrap().tapped = true;
        assert_eq!(check_reach(runner.state(), &labels, Some(&reach)), Ok(true));
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

    #[test]
    fn projection_identifies_tokens_without_changing_their_name() {
        let mut scenario = GameScenario::new();
        let rat = scenario.add_creature(PlayerId(0), "Rat", 1, 1).id();
        let mut runner = scenario.build();
        runner.state_mut().objects.get_mut(&rat).unwrap().is_token = true;

        let value = project(runner.state(), &HashMap::new(), None, true);

        assert_eq!(value["battlefield"][0]["name"], json!("Rat"));
        assert_eq!(value["battlefield"][0]["token"], json!(true));
    }
}
