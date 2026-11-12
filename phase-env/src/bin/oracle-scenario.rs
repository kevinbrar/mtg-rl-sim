use engine::database::CardDatabase;
use engine::game::ability_utils::build_target_slots;
use engine::game::game_object::GameObject;
use engine::game::scenario::{GameRunner, GameScenario};
use engine::game::scenario_db::GameScenarioDbExt;
use engine::types::{
    actions::GameAction, parse_counter_type, AbilityKind, GameState, ManaColor, ObjectId, Phase,
    PlayerId, ResolvedAbility, TargetRef, WaitingFor, Zone,
};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet, VecDeque};
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
        .ok_or_else(|| format!("unknown setup label {label}"))
}

fn resolve_card_choice_labels(
    action: &Value,
    labels: &HashMap<ObjectId, String>,
) -> Result<Vec<ObjectId>, String> {
    action
        .get("cards")
        .and_then(Value::as_array)
        .ok_or("choose_cards needs cards")?
        .iter()
        .map(|value| {
            let label = value.as_str().ok_or("card choice labels must be strings")?;
            resolve_label(labels, label).map_err(|_| format!("unknown card label {label}"))
        })
        .collect()
}

fn take_card_choice_group(groups: &mut VecDeque<Vec<ObjectId>>) -> Result<Vec<ObjectId>, String> {
    groups
        .pop_front()
        .ok_or_else(|| "card-choice prompt arrived with an empty choose_cards queue".to_string())
}

fn validate_card_choice_group(
    group: &[ObjectId],
    offered: &[ObjectId],
    min_count: usize,
    max_count: usize,
    labels: &HashMap<ObjectId, String>,
) -> Result<(), String> {
    if group.len() < min_count {
        return Err(format!(
            "card choice group has {} cards but the prompt requires at least {min_count}",
            group.len()
        ));
    }
    if group.len() > max_count {
        return Err(format!(
            "card choice group has {} cards but the prompt allows at most {max_count}",
            group.len()
        ));
    }
    for id in group {
        if !offered.contains(id) {
            let label = labels.get(id).map(String::as_str).unwrap_or("<unknown>");
            return Err(format!("card choice label {label} is not offered"));
        }
    }
    Ok(())
}

fn ensure_card_choice_groups_consumed(groups: &VecDeque<Vec<ObjectId>>) -> Result<(), String> {
    if groups.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} choose_cards group(s) were unconsumed when the script ended",
        groups.len()
    ))
}

fn apply_card_choice_group(
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    groups: &mut VecDeque<Vec<ObjectId>>,
) -> Result<(), String> {
    match runner.state().waiting_for.clone() {
        WaitingFor::EffectZoneChoice {
            cards,
            count,
            min_count,
            up_to,
            ..
        } => {
            let group = take_card_choice_group(groups)?;
            let min_count = if up_to { min_count } else { count };
            validate_card_choice_group(&group, &cards, min_count, count, labels)?;
            runner
                .act(GameAction::SelectCards { cards: group })
                .map(|_| ())
                .map_err(|error| error.to_string())
        }
        WaitingFor::ChooseFromZoneChoice {
            cards,
            count,
            up_to,
            ..
        } => {
            let group = take_card_choice_group(groups)?;
            let min_count = if up_to { 0 } else { count };
            validate_card_choice_group(&group, &cards, min_count, count, labels)?;
            runner
                .act(GameAction::SelectCards { cards: group })
                .map(|_| ())
                .map_err(|error| error.to_string())
        }
        WaitingFor::DiscardChoice {
            cards,
            count,
            up_to,
            ..
        } => {
            let group = take_card_choice_group(groups)?;
            let min_count = if up_to { 0 } else { count };
            validate_card_choice_group(&group, &cards, min_count, count, labels)?;
            runner
                .act(GameAction::SelectCards { cards: group })
                .map(|_| ())
                .map_err(|error| error.to_string())
        }
        other => Err(format!(
            "cannot apply card choice while waiting for {other:?}"
        )),
    }
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

fn cast_x(action: &Value, has_x_cost: bool) -> Result<Option<u32>, String> {
    match (has_x_cost, action.get("x")) {
        (true, None) => Err("cast of an X-cost spell requires x".to_string()),
        (false, Some(_)) => Err("x is only valid for a spell with an X cost".to_string()),
        (_, None) => Ok(None),
        (true, Some(value)) => {
            let x = value
                .as_u64()
                .ok_or("cast.x must be a non-negative integer")?;
            let x = u32::try_from(x).map_err(|_| "cast.x exceeds the supported range")?;
            Ok(Some(x))
        }
    }
}

fn apply_trigger_target_group(
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    groups: &mut VecDeque<Vec<ObjectId>>,
) -> Result<(), String> {
    let trigger_entry = runner
        .state()
        .pending_trigger_entry
        .ok_or("trigger target prompt has no pending stack entry")?;
    let group = take_trigger_target_group(groups)?;
    let mut target_index = 0;
    loop {
        let WaitingFor::TriggerTargetSelection {
            target_slots,
            selection,
            ..
        } = runner.state().waiting_for.clone()
        else {
            break;
        };
        if runner.state().pending_trigger_entry != Some(trigger_entry) {
            break;
        }
        let slot = target_slots
            .get(selection.current_slot)
            .ok_or("trigger target slot is missing")?;
        let target = trigger_target_for_slot(
            group.get(target_index).copied(),
            &slot.legal_targets,
            slot.optional,
            labels,
            selection.current_slot,
        )?;
        let has_target = target.is_some();
        runner
            .act(GameAction::ChooseTarget { target })
            .map_err(|error| error.to_string())?;
        if has_target {
            target_index += 1;
        }
    }
    ensure_trigger_target_group_consumed(group.len(), target_index)
}

fn take_trigger_target_group(
    groups: &mut VecDeque<Vec<ObjectId>>,
) -> Result<Vec<ObjectId>, String> {
    groups.pop_front().ok_or_else(|| {
        "trigger target prompt arrived with an empty trigger_targets queue".to_string()
    })
}

fn trigger_target_for_slot(
    id: Option<ObjectId>,
    legal_targets: &[TargetRef],
    optional: bool,
    labels: &HashMap<ObjectId, String>,
    slot_index: usize,
) -> Result<Option<TargetRef>, String> {
    let target = id.map(TargetRef::Object);
    if target
        .as_ref()
        .is_some_and(|target| !legal_targets.contains(target))
    {
        let label = id
            .and_then(|id| labels.get(&id))
            .map(String::as_str)
            .unwrap_or("<unknown>");
        return Err(format!(
            "trigger target label {label} is not legal for slot {slot_index}"
        ));
    }
    if target.is_none() && !optional {
        return Err(format!(
            "trigger target group has too few targets for required slot {slot_index}"
        ));
    }
    Ok(target)
}

fn ensure_trigger_target_group_consumed(group_len: usize, used: usize) -> Result<(), String> {
    if group_len != used {
        return Err(format!(
            "trigger target group has {group_len} targets but used {used}"
        ));
    }
    Ok(())
}

fn ensure_trigger_target_groups_consumed(groups: &VecDeque<Vec<ObjectId>>) -> Result<(), String> {
    if groups.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} trigger target group(s) were unconsumed when the script ended",
        groups.len()
    ))
}

fn pass_until_resolved(
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    groups: &mut VecDeque<Vec<ObjectId>>,
    card_choice_groups: &mut VecDeque<Vec<ObjectId>>,
) -> Result<(), String> {
    for _ in 0..128 {
        match runner.state().waiting_for {
            WaitingFor::TriggerTargetSelection { .. } => {
                apply_trigger_target_group(runner, labels, groups)?;
            }
            WaitingFor::EffectZoneChoice { .. }
            | WaitingFor::ChooseFromZoneChoice { .. }
            | WaitingFor::DiscardChoice { .. } => {
                apply_card_choice_group(runner, labels, card_choice_groups)?;
            }
            _ => runner.advance_until_stack_empty(),
        }
        if runner.state().stack.is_empty()
            && !matches!(
                runner.state().waiting_for,
                WaitingFor::TriggerTargetSelection { .. }
                    | WaitingFor::EffectZoneChoice { .. }
                    | WaitingFor::ChooseFromZoneChoice { .. }
                    | WaitingFor::DiscardChoice { .. }
                    | WaitingFor::OptionalEffectChoice { .. }
            )
        {
            return Ok(());
        }
    }
    Err("stack did not resolve within 128 actions".to_string())
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
    let mut labels = HashSet::new();
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
                if card
                    .keys()
                    .any(|key| !["card", "label"].contains(&key.as_str()))
                    || !card.get("card").is_some_and(Value::is_string)
                {
                    return Err(format!(
                        "setup.{player}.{field}[{index}] needs card and optional label"
                    ));
                }
                if card["card"].as_str().is_none_or(str::is_empty) {
                    return Err(format!("setup.{player}.{field}[{index}] needs a card"));
                }
                if let Some(label) = card.get("label") {
                    let label = label
                        .as_str()
                        .filter(|label| !label.is_empty())
                        .ok_or_else(|| {
                            format!(
                                "setup.{player}.{field}[{index}].label must be a non-empty string"
                            )
                        })?;
                    if !labels.insert(label.to_string()) {
                        return Err(format!("duplicate setup label {label}"));
                    }
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
                let label = permanent["label"].as_str().unwrap();
                if label.is_empty() || !labels.insert(label.to_string()) {
                    return Err(format!("duplicate or empty setup label {label}"));
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

fn advance_to_next_turn(
    runner: &mut GameRunner,
    continue_to_precombat_main: bool,
) -> Result<(), String> {
    let turn = runner.state().turn_number;
    let mut next_turn = None;
    for _ in 0..128 {
        let (current_turn, phase, waiting_for, stack_empty) = {
            let state = runner.state();
            (
                state.turn_number,
                state.phase,
                state.waiting_for.clone(),
                state.stack.is_empty(),
            )
        };
        if current_turn != turn {
            if !continue_to_precombat_main {
                return Ok(());
            }
            let expected_turn = *next_turn.get_or_insert(current_turn);
            if current_turn != expected_turn {
                return Err("turn advanced again before reaching precombat main".to_string());
            }
            if phase == Phase::PreCombatMain
                && stack_empty
                && matches!(waiting_for, WaitingFor::Priority { .. })
            {
                return Ok(());
            }
        }
        if matches!(waiting_for, WaitingFor::OrderTriggers { .. })
            || matches!(waiting_for, WaitingFor::Priority { .. }) && !stack_empty
        {
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
    Err("turn did not advance within 128 actions".to_string())
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
                if let Some(label) = item["label"].as_str() {
                    labels.insert(id, label.to_string());
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
    let actions = spec["script"].as_array().cloned().unwrap_or_default();
    let mut trigger_target_groups = VecDeque::new();
    let mut card_choice_groups = VecDeque::new();
    for (action_index, action) in actions.iter().enumerate() {
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
                let has_x_cost = match &runner.state().objects[&spell].mana_cost {
                    engine::types::mana::ManaCost::Cost { shards, .. } => {
                        shards.contains(&engine::types::mana::ManaCostShard::X)
                    }
                    _ => false,
                };
                let x = cast_x(action, has_x_cost)?;
                let target_ids = resolve_target_labels(action, &labels)?;
                let cast = runner.cast(spell);
                let cast = if let Some(x) = x { cast.x(x) } else { cast };
                let _ = cast.target_objects(&target_ids).commit();
            }
            "activate" => activate(&mut runner, &labels, action)?,
            "trigger_targets" => {
                let targets = resolve_target_labels(action, &labels)?;
                if targets.is_empty() {
                    return Err("trigger_targets needs at least one target".to_string());
                }
                trigger_target_groups.push_back(targets);
            }
            "choose_cards" => {
                card_choice_groups.push_back(resolve_card_choice_labels(action, &labels)?);
            }
            "pass_until_resolved" => {
                pass_until_resolved(
                    &mut runner,
                    &labels,
                    &mut trigger_target_groups,
                    &mut card_choice_groups,
                )?;
            }
            "end_turn" | "next_turn" => {
                advance_to_next_turn(&mut runner, action_index + 1 < actions.len())?;
            }
            other => return Err(format!("unsupported action {other}")),
        }
    }
    ensure_trigger_target_groups_consumed(&trigger_target_groups)?;
    ensure_card_choice_groups_consumed(&card_choice_groups)?;
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
    if zone != "battlefield" {
        return Ok(![
            "types",
            "subtypes",
            "colors",
            "power",
            "toughness",
            "creature",
            "tapped",
            "counters",
        ]
        .iter()
        .any(|key| reach.get(key).is_some()));
    }

    for id in &state.battlefield {
        let Some(object) = state
            .objects
            .get(id)
            .filter(|object| object.controller.0 as usize == index && object.name == card)
        else {
            continue;
        };
        if matches_reach_characteristics(object, reach)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn matches_reach_characteristics(object: &GameObject, reach: &Value) -> Result<bool, String> {
    for (key, actual) in [
        (
            "types",
            sorted(
                object
                    .card_types
                    .core_types
                    .iter()
                    .map(|kind| normalize_characteristic(&kind.to_string())),
            ),
        ),
        (
            "subtypes",
            sorted(
                object
                    .card_types
                    .subtypes
                    .iter()
                    .map(|subtype| normalize_characteristic(subtype)),
            ),
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
                        .map(|value| {
                            if key == "colors" {
                                value.to_string()
                            } else {
                                normalize_characteristic(value)
                            }
                        })
                        .ok_or_else(|| format!("reach.{key} values must be strings"))
                })
                .collect::<Result<Vec<_>, _>>()?;
            expected.sort();
            if expected != actual {
                return Ok(false);
            }
        }
    }
    if let Some(expected) = reach.get("counters") {
        let Some(expected) = expected.as_object() else {
            return Err("reach.counters must be an object".to_string());
        };
        for (name, count) in expected {
            let Some(expected) = count.as_u64() else {
                return Err(format!(
                    "reach.counters.{name} must be a non-negative integer"
                ));
            };
            let actual = object
                .counters
                .get(&parse_counter_type(name))
                .copied()
                .unwrap_or_default();
            if u64::from(actual) != expected {
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
            .contains("unknown setup label"));
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
    fn cast_x_requires_an_explicit_non_negative_value_only_for_x_costs() {
        assert_eq!(cast_x(&json!({}), false), Ok(None));
        assert_eq!(cast_x(&json!({"x": 0}), true), Ok(Some(0)));
        assert!(cast_x(&json!({"x": 2}), false)
            .unwrap_err()
            .contains("only valid"));
        assert!(cast_x(&json!({}), true).unwrap_err().contains("requires x"));
        assert!(cast_x(&json!({"x": -1}), true)
            .unwrap_err()
            .contains("non-negative"));
    }

    #[test]
    fn trigger_target_groups_fail_closed_when_empty_illegal_or_unconsumed() {
        let mut groups = VecDeque::new();
        assert!(take_trigger_target_group(&mut groups)
            .unwrap_err()
            .contains("empty trigger_targets queue"));

        let target = ObjectId(17);
        let labels = HashMap::from([(target, "p1.creature".to_string())]);
        let second_target = ObjectId(18);
        let mut groups = VecDeque::from(vec![vec![target], vec![second_target]]);
        assert_eq!(take_trigger_target_group(&mut groups), Ok(vec![target]));
        assert_eq!(
            take_trigger_target_group(&mut groups),
            Ok(vec![second_target])
        );

        assert!(
            trigger_target_for_slot(Some(target), &[], false, &labels, 0,)
                .unwrap_err()
                .contains("not legal")
        );
        assert!(trigger_target_for_slot(None, &[], false, &labels, 0)
            .unwrap_err()
            .contains("too few targets"));
        assert!(ensure_trigger_target_group_consumed(2, 1)
            .unwrap_err()
            .contains("used 1"));

        let groups = VecDeque::from(vec![vec![target]]);
        assert!(ensure_trigger_target_groups_consumed(&groups)
            .unwrap_err()
            .contains("unconsumed"));
    }

    #[test]
    fn card_choice_prompt_fails_when_queue_is_empty() {
        assert!(take_card_choice_group(&mut VecDeque::new())
            .unwrap_err()
            .contains("empty choose_cards queue"));
    }

    #[test]
    fn card_choice_group_rejects_a_label_not_offered_by_the_prompt() {
        let selected = ObjectId(17);
        let offered = ObjectId(18);
        let labels = HashMap::from([(selected, "p0.library.creature".to_string())]);

        assert!(
            validate_card_choice_group(&[selected], &[offered], 0, 1, &labels)
                .unwrap_err()
                .contains("p0.library.creature is not offered")
        );
    }

    #[test]
    fn card_choice_group_rejects_more_cards_than_the_prompt_allows() {
        let first = ObjectId(17);
        let second = ObjectId(18);
        let labels = HashMap::new();

        assert!(
            validate_card_choice_group(&[first, second], &[first, second], 0, 1, &labels)
                .unwrap_err()
                .contains("allows at most 1")
        );
    }

    #[test]
    fn card_choice_groups_left_at_script_end_fail_closed() {
        let groups = VecDeque::from(vec![vec![ObjectId(17)]]);

        assert!(
            ensure_card_choice_groups_consumed(&groups)
                .unwrap_err()
                .contains("unconsumed")
        );
        assert!(ensure_card_choice_groups_consumed(&VecDeque::new()).is_ok());
    }

    #[test]
    fn empty_up_to_zone_choice_group_is_valid_for_the_real_prompt() {
        let first = ObjectId(17);

        assert_eq!(
            validate_card_choice_group(&[], &[first], 0, 1, &HashMap::new()),
            Ok(())
        );
    }

    #[test]
    fn simultaneous_trigger_target_prompts_consume_groups_in_fifo_order() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let trigger_text = "Whenever a creature enters the battlefield under your control, tap target creature an opponent controls.";
                let first_source = scenario
                    .add_enchantment_from_oracle(PlayerId(0), "First trigger", trigger_text)
                    .id();
                let second_source = scenario
                    .add_enchantment_from_oracle(PlayerId(0), "Second trigger", trigger_text)
                    .id();
                let first_target = scenario
                    .add_creature(PlayerId(1), "First target", 1, 1)
                    .id();
                let second_target = scenario
                    .add_creature(PlayerId(1), "Second target", 1, 1)
                    .id();
                let entering_creature = scenario
                    .add_creature_to_hand(PlayerId(0), "Triggering creature", 1, 1)
                    .with_mana_cost(engine::types::mana::ManaCost::generic(0))
                    .id();
                let mut runner = scenario.build();
                let labels = HashMap::from([
                    (first_target, "first".to_string()),
                    (second_target, "second".to_string()),
                ]);
                let mut groups = VecDeque::from(vec![vec![first_target], vec![second_target]]);

                runner.cast(entering_creature).commit();
                runner.advance_until_stack_empty();
                let prompt_identity = |runner: &GameRunner| match &runner.state().waiting_for {
                    WaitingFor::TriggerTargetSelection {
                        source_id: Some(source),
                        ..
                    } => (
                        *source,
                        runner
                            .state()
                            .pending_trigger_entry
                            .expect("trigger prompt has a pending stack entry"),
                    ),
                    waiting_for => panic!("expected trigger target prompt, got {waiting_for:?}"),
                };

                let (prompt_source_1, prompt_entry_1) = prompt_identity(&runner);
                assert!([first_source, second_source].contains(&prompt_source_1));
                apply_trigger_target_group(&mut runner, &labels, &mut groups).unwrap();
                let (prompt_source_2, prompt_entry_2) = prompt_identity(&runner);
                assert!([first_source, second_source].contains(&prompt_source_2));
                assert_ne!(prompt_source_1, prompt_source_2);
                assert_ne!(prompt_entry_1, prompt_entry_2);
                apply_trigger_target_group(&mut runner, &labels, &mut groups).unwrap();

                assert!(groups.is_empty());
                let targets_by_source = runner
                    .state()
                    .stack
                    .iter()
                    .filter_map(|entry| match &entry.kind {
                        engine::types::game_state::StackEntryKind::TriggeredAbility {
                            ability,
                            ..
                        } => Some((entry.source_id, ability.targets.clone())),
                        _ => None,
                    })
                    .collect::<HashMap<_, _>>();
                assert_eq!(
                    targets_by_source.get(&prompt_source_1),
                    Some(&vec![TargetRef::Object(first_target)])
                );
                assert_eq!(
                    targets_by_source.get(&prompt_source_2),
                    Some(&vec![TargetRef::Object(second_target)])
                );
            })
            .unwrap()
            .join()
            .unwrap();
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

        assert_eq!(
            check_reach(runner.state(), &labels, Some(&reach)),
            Ok(false)
        );
        runner.state_mut().objects.get_mut(&forest).unwrap().tapped = true;
        assert_eq!(check_reach(runner.state(), &labels, Some(&reach)), Ok(true));
    }

    #[test]
    fn reach_normalizes_expected_and_actual_types_and_subtypes() {
        let mut scenario = GameScenario::new();
        let creature = scenario
            .add_creature(PlayerId(0), "Normalized fixture", 2, 2)
            .id();
        let mut runner = scenario.build();
        runner
            .state_mut()
            .objects
            .get_mut(&creature)
            .unwrap()
            .card_types
            .subtypes
            .push("Human".to_string());
        let reach = json!({
            "player": "P0",
            "zone": "battlefield",
            "card": "Normalized fixture",
            "types": ["creature"],
            "subtypes": ["human"]
        });

        assert_eq!(
            check_reach(runner.state(), &HashMap::new(), Some(&reach)),
            Ok(true)
        );
    }

    #[test]
    fn reach_matches_any_same_name_permanent_with_expected_characteristics() {
        let mut scenario = GameScenario::new();
        scenario.add_creature(PlayerId(0), "Duplicate fixture", 1, 1);
        scenario.add_creature(PlayerId(0), "Duplicate fixture", 3, 3);
        let runner = scenario.build();
        let reach = json!({
            "player": "P0",
            "zone": "battlefield",
            "card": "Duplicate fixture",
            "power": 3,
            "toughness": 3
        });

        assert_eq!(
            check_reach(runner.state(), &HashMap::new(), Some(&reach)),
            Ok(true)
        );
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

                advance_to_next_turn(&mut runner, false).unwrap();

                assert_ne!(runner.state().turn_number, turn);
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn end_turn_reaches_precombat_main_after_draw_and_trigger_resolution() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let _draw_trigger = scenario
                    .add_creature_from_oracle(
                        PlayerId(0),
                        "Draw Trigger Creature",
                        0,
                        2,
                        "Whenever you draw a card, you gain 2 life.",
                    )
                    .id();
                let _second_draw_trigger = scenario
                    .add_creature_from_oracle(
                        PlayerId(0),
                        "Second Draw Trigger Creature",
                        0,
                        2,
                        "Whenever you draw a card, you gain 3 life.",
                    )
                    .id();
                scenario.with_library_top(PlayerId(0), &["P0 draw"]);
                scenario.with_library_top(PlayerId(1), &["P1 draw"]);
                let mut runner = scenario.build();
                let turn = runner.state().turn_number;

                advance_to_next_turn(&mut runner, true).unwrap();

                assert_ne!(runner.state().turn_number, turn);
                assert_eq!(runner.state().active_player, PlayerId(1));
                assert_eq!(runner.state().phase, Phase::PreCombatMain);
                assert_eq!(runner.state().players[1].hand.len(), 1);

                advance_to_next_turn(&mut runner, true).unwrap();

                assert_eq!(runner.state().active_player, PlayerId(0));
                assert_eq!(runner.state().phase, Phase::PreCombatMain);
                assert_eq!(runner.state().players[0].hand.len(), 1);
                assert_eq!(runner.state().players[0].life, 25);
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn terminal_end_turn_stops_before_the_next_draw_step() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                scenario.with_library_top(PlayerId(1), &["P1 draw"]);
                let p1_land = scenario.add_basic_land(PlayerId(1), ManaColor::Green);
                let mut runner = scenario.build();
                runner.state_mut().objects.get_mut(&p1_land).unwrap().tapped = true;
                let turn = runner.state().turn_number;

                advance_to_next_turn(&mut runner, false).unwrap();

                assert_ne!(runner.state().turn_number, turn);
                assert_eq!(runner.state().phase, Phase::Upkeep);
                assert!(!runner.state().objects[&p1_land].tapped);
                assert!(runner.state().players[1].hand.is_empty());
                assert_eq!(runner.state().players[1].library.len(), 1);
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
    fn reach_matches_counter_counts_and_treats_missing_counters_as_zero() {
        let mut scenario = GameScenario::new();
        let creature = scenario
            .add_creature(PlayerId(0), "Counter Bear", 2, 2)
            .id();
        let mut runner = scenario.build();
        runner
            .state_mut()
            .objects
            .get_mut(&creature)
            .unwrap()
            .counters
            .insert(parse_counter_type("ingenuity"), 2);
        let object = &runner.state().objects[&creature];

        assert!(
            matches_reach_characteristics(object, &json!({"counters": {"ingenuity": 2}})).unwrap()
        );
        assert!(
            matches_reach_characteristics(object, &json!({"counters": {"energy": 0}})).unwrap()
        );
        assert!(
            !matches_reach_characteristics(object, &json!({"counters": {"ingenuity": 0}})).unwrap()
        );
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
