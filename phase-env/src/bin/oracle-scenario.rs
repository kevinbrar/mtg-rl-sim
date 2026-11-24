use engine::database::CardDatabase;
use engine::game::ability_utils::{
    build_resolved_from_def, build_target_slots, declared_targets_in_chain,
};
use engine::game::combat::AttackTarget;
use engine::game::deck_loading::DeckEntry;
use engine::game::filter::{matches_target_filter, FilterContext};
use engine::game::game_object::GameObject;
use engine::game::mana_abilities::is_mana_ability;
use engine::game::scenario::{GameRunner, GameScenario};
use engine::game::scenario_db::GameScenarioDbExt;
use engine::types::ability::{AdditionalCost, EffectKind, ReplacementMode};
use engine::types::mana::{ManaType, ManaUnit};
use engine::types::{
    actions::{GameAction, OutsideGameSelection, UnlessCostBranch},
    events::GameEvent,
    game_state::{OutsideGameChoiceSource, OutsideGameChoiceStage, PlayerDeckPool},
    parse_counter_type, AbilityCost, AbilityDefinition, AbilityKind, Effect, GameState, ManaColor,
    ObjectId, Phase, PlayerId, ReplacementDefinition, ResolvedAbility, TargetFilter, TargetRef,
    WaitingFor, Zone,
};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet, VecDeque};
use std::path::Path;
use std::sync::Arc;

#[derive(Debug)]
struct OutsideGameScriptChoice {
    player: PlayerId,
    accept: bool,
    labels: Vec<String>,
}

#[derive(Debug)]
struct ManaScriptChoice {
    player: PlayerId,
    sources: Vec<String>,
}

#[derive(Debug)]
struct SurveilScriptChoice {
    player: PlayerId,
    graveyard: Vec<ObjectId>,
}

#[derive(Clone, Debug)]
struct CopyCastChoice {
    player: PlayerId,
    source: ObjectId,
    cards: Vec<ObjectId>,
    /// Script index when the last card is expected to be refused.
    refused: Option<usize>,
}

#[derive(Clone, Debug)]
struct TargetScriptChoice {
    player: PlayerId,
    targets: Vec<ObjectId>,
}

#[derive(Clone, Copy, Debug)]
struct OptionalChoice {
    answer: bool,
    source: Option<ObjectId>,
}

#[derive(Clone, Debug, PartialEq)]
struct TriggerTargetGroup {
    targets: Vec<Option<ObjectId>>,
    /// Script index when the last target is expected to be refused.
    refused: Option<usize>,
}

impl From<Vec<Option<ObjectId>>> for TriggerTargetGroup {
    fn from(targets: Vec<Option<ObjectId>>) -> Self {
        Self {
            targets,
            refused: None,
        }
    }
}

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
        "exile" => names(
            state,
            state.exile.iter().copied().filter(|id| {
                state
                    .objects
                    .get(id)
                    .is_some_and(|object| object.owner.0 as usize == player)
            }),
        ),
        "sideboard" => {
            let player_id = PlayerId(player as u8);
            let mut names = state
                .deck_pool_of(player_id)
                .into_iter()
                .flat_map(|pool| pool.current_sideboard.iter().enumerate())
                .flat_map(|(index, entry)| {
                    let used = state
                        .outside_game_cards_brought_in
                        .iter()
                        .filter(|used| used.player == player_id && used.sideboard_index == index)
                        .map(|used| used.count)
                        .sum::<u32>();
                    std::iter::repeat_n(
                        entry.card.name.clone(),
                        entry.count.saturating_sub(used) as usize,
                    )
                })
                .collect::<Vec<_>>();
            names.sort();
            names
        }
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

fn is_case(object: &GameObject) -> bool {
    object
        .card_types
        .subtypes
        .iter()
        .any(|subtype| normalize_characteristic(subtype) == "Case")
}

fn project(
    state: &GameState,
    labels: &HashMap<ObjectId, String>,
    legal_targets: Option<Vec<String>>,
    reached: bool,
) -> Value {
    let mut players = serde_json::Map::new();
    for index in 0..2 {
        let mut player = json!({
            "life": state.players[index].life,
            "battlefield": cards(state, index, "battlefield"),
            "hand": cards(state, index, "hand"),
            "library": cards(state, index, "library"),
            "graveyard": cards(state, index, "graveyard"),
        });
        let sideboard = cards(state, index, "sideboard");
        if !sideboard.is_empty() {
            player["sideboard"] = json!(sideboard);
        }
        players.insert(format!("P{index}"), player);
    }
    let mut battlefield = state
        .battlefield
        .iter()
        .filter_map(|id| {
            let object = state.objects.get(id)?;
            let attached_to = object.attached_to.as_ref().and_then(|target| match target {
                engine::types::object_data::AttachTarget::Object(host) => {
                    labels.get(host).cloned()
                }
                engine::types::object_data::AttachTarget::Player(_) => None,
            });
            let counters = object
                .counters
                .iter()
                .map(|(kind, count)| (kind.as_str().to_string(), json!(count)))
                .collect::<serde_json::Map<_, _>>();
            let mut projected = json!({
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
            });
            if let Some(attached_to) = attached_to {
                projected["attached_to"] = json!(attached_to);
            }
            if is_case(object) {
                projected["solved"] = json!(
                    object
                        .case_state
                        .as_ref()
                        .expect("Case permanent must carry Case state")
                        .is_solved
                );
            }
            Some(projected)
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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RefusalMode {
    AbsentFromLegalActions,
    OfferedThenErr,
}

fn classify_refusal(offered: bool, attempt_failed: bool) -> Option<RefusalMode> {
    if !offered {
        Some(RefusalMode::AbsentFromLegalActions)
    } else if attempt_failed {
        Some(RefusalMode::OfferedThenErr)
    } else {
        None
    }
}

fn refusal_mode_for_action_error(offered: bool, error: &ClassifiedActError) -> Option<RefusalMode> {
    match error {
        ClassifiedActError::Engine(_) => classify_refusal(offered, true),
        ClassifiedActError::Harness(_) => None,
    }
}

fn refused_attempt_prompt_error(action: &str, waiting_for: &WaitingFor) -> ClassifiedActError {
    ClassifiedActError::Harness(format!(
        "refused {action} reached an unscripted prompt: {waiting_for:?}"
    ))
}

fn refusal_attempt_runner(runner: &GameRunner) -> GameRunner {
    GameRunner::from_state(runner.state().clone())
}

fn attempt_refused_cast(
    runner: &mut GameRunner,
    spell: ObjectId,
    player: PlayerId,
    script_index: usize,
    targets: &[Option<TargetRef>],
    refusal_cost_prompts: &mut Vec<Value>,
) -> Result<(), ClassifiedActError> {
    let cast = GameAction::CastSpell {
        object_id: spell,
        card_id: runner.state().objects[&spell].card_id,
        targets: Vec::new(),
        payment_mode: engine::types::game_state::CastPaymentMode::Auto,
    };
    runner
        .act(cast)
        .map_err(|error| ClassifiedActError::Engine(error.to_string()))?;

    let mut selected_slots = 0;
    for _ in 0..64 {
        match runner.state().waiting_for.clone() {
            WaitingFor::TargetSelection {
                target_slots,
                selection,
                ..
            } => {
                if target_slots.len() != targets.len() {
                    return Err(ClassifiedActError::Harness(format!(
                        "refused cast has {} scripted target slot(s) but the engine offered {}",
                        targets.len(),
                        target_slots.len()
                    )));
                }
                let Some(slot) = target_slots.get(selection.current_slot) else {
                    return Err(ClassifiedActError::Harness(
                        "refused cast target slot is missing".to_string(),
                    ));
                };
                let Some(target) = targets.get(selection.current_slot).cloned() else {
                    return Err(ClassifiedActError::Harness(
                        "refused cast target choice is missing".to_string(),
                    ));
                };
                if target
                    .as_ref()
                    .is_some_and(|target| !slot.legal_targets.contains(target))
                {
                    return Err(ClassifiedActError::Harness(format!(
                        "refused cast target {:?} is not legal for slot {}",
                        target, selection.current_slot
                    )));
                }
                if target.is_none() && !slot.optional {
                    return Err(ClassifiedActError::Harness(format!(
                        "refused cast cannot skip required target slot {}",
                        selection.current_slot
                    )));
                }
                runner
                    .act(GameAction::ChooseTarget { target })
                    .map_err(|error| ClassifiedActError::Engine(error.to_string()))?;
                selected_slots += 1;
            }
            WaitingFor::CollectEvidenceChoice {
                player: chooser,
                cards,
                ..
            } => {
                if chooser != player {
                    return Err(refused_attempt_prompt_error(
                        "cast",
                        &runner.state().waiting_for,
                    ));
                }
                let prompt = format!(
                    "CollectEvidenceCost card choice, {} candidates",
                    cards.len()
                );
                eprintln!("Phase refusal cost prompt: script[{script_index}] {prompt}");
                refusal_cost_prompts.push(json!({
                    "script_index": script_index,
                    "prompt": prompt,
                }));
                runner
                    .act(GameAction::SelectCards { cards })
                    .map_err(|error| ClassifiedActError::Engine(error.to_string()))?;
            }
            WaitingFor::Priority { .. } => {
                if selected_slots != targets.len() {
                    return Err(ClassifiedActError::Harness(format!(
                        "refused cast consumed {selected_slots} of {} scripted target slot(s)",
                        targets.len()
                    )));
                }
                return Ok(());
            }
            ref waiting_for => return Err(refused_attempt_prompt_error("cast", waiting_for)),
        }
    }
    Err(ClassifiedActError::Harness(
        "refused cast exceeded the target-selection action limit".to_string(),
    ))
}

fn cast_target_decision_slot_count_needed(has_targets: bool, expect_refused: bool) -> bool {
    has_targets && !expect_refused
}

fn activation_source_zone_precondition(zone: Zone, source_label: &str) -> Result<(), String> {
    if matches!(zone, Zone::Battlefield | Zone::Graveyard) {
        Ok(())
    } else {
        Err(format!(
            "activate source label {source_label} must be on the battlefield or in the graveyard"
        ))
    }
}

fn record_refusal(
    action_index: usize,
    action: &str,
    mode: RefusalMode,
    before: &Value,
    after: &Value,
    refusals: &mut Vec<usize>,
) -> Result<(), String> {
    if before != after {
        return Err(format!(
            "refused action left state changed for script[{action_index}] {action}\nbefore:\n{}\nafter:\n{}",
            serde_json::to_string_pretty(before).expect("JSON value must serialize"),
            serde_json::to_string_pretty(after).expect("JSON value must serialize"),
        ));
    }
    let mode = match mode {
        RefusalMode::AbsentFromLegalActions => "absent from legal_actions",
        RefusalMode::OfferedThenErr => "offered then Err",
    };
    eprintln!("Phase refusal: script[{action_index}] {action}: {mode}");
    refusals.push(action_index);
    Ok(())
}

fn script_expected_refusal(action: &Value, index: usize) -> Result<bool, String> {
    let Some(expected) = action.get("expect") else {
        return Ok(false);
    };
    if expected.as_str() != Some("refused") {
        return Err(format!("script[{index}].expect must be 'refused'"));
    }
    if !matches!(
        action["action"].as_str(),
        Some("cast" | "activate" | "choose_copies" | "trigger_targets")
    ) {
        return Err(format!(
            "script[{index}].expect is only valid for cast, activate, choose_copies or trigger_targets"
        ));
    }
    if action.get("sacrifice").is_some()
        || action.get("alternative_cost").is_some()
        || action.get("x").is_some()
    {
        return Err(format!(
            "script[{index}].expect cannot be combined with choice scripts"
        ));
    }
    Ok(true)
}

fn ensure_mana_choices_not_refused(actions: &[Value]) -> Result<(), String> {
    for (index, action) in actions.iter().enumerate() {
        if action["action"].as_str() != Some("choose_mana") {
            continue;
        }
        let consumer = actions[index + 1..]
            .iter()
            .find(|candidate| matches!(candidate["action"].as_str(), Some("cast" | "activate")));
        if consumer.is_some_and(|candidate| {
            candidate.get("expect").and_then(Value::as_str) == Some("refused")
        }) {
            return Err(format!(
                "script[{index}].choose_mana cannot precede a refused action"
            ));
        }
    }
    Ok(())
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

fn script_player(action: &Value, action_name: &str) -> Result<PlayerId, String> {
    match action.get("player").and_then(Value::as_str) {
        Some("P0") => Ok(PlayerId(0)),
        Some("P1") => Ok(PlayerId(1)),
        _ => Err(format!("{action_name}.player must be P0 or P1")),
    }
}

fn parse_copy_cast_choice(
    action: &Value,
    labels: &HashMap<ObjectId, String>,
    refused: Option<usize>,
) -> Result<CopyCastChoice, String> {
    let player = script_player(action, "choose_copies")?;
    let source_label = action
        .get("source")
        .and_then(Value::as_str)
        .ok_or("choose_copies needs a source label")?;
    let source = resolve_label(labels, source_label)?;
    let cards = resolve_card_choice_labels(action, labels)?;
    if cards.iter().copied().collect::<HashSet<_>>().len() != cards.len() {
        return Err("choose_copies.cards cannot repeat labels".to_string());
    }
    if refused.is_some() && cards.len() < 2 {
        return Err(
            "refused choose_copies needs an accepted card before the refused card".to_string(),
        );
    }
    Ok(CopyCastChoice {
        player,
        source,
        cards,
        refused,
    })
}

fn parse_target_script_choice(
    action: &Value,
    labels: &HashMap<ObjectId, String>,
    action_name: &str,
) -> Result<TargetScriptChoice, String> {
    let player = script_player(action, action_name)?;
    let targets = action
        .get("targets")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("{action_name}.targets must be an array"))?
        .iter()
        .map(|value| {
            let label = value
                .as_str()
                .ok_or_else(|| format!("{action_name}.targets entries must be strings"))?;
            resolve_label(labels, label)
                .map_err(|_| format!("unknown {action_name} target label {label}"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    if targets.iter().copied().collect::<HashSet<_>>().len() != targets.len() {
        return Err(format!("{action_name}.targets cannot repeat labels"));
    }
    Ok(TargetScriptChoice { player, targets })
}

fn parse_outside_game_choice(action: &Value) -> Result<OutsideGameScriptChoice, String> {
    let player = match action["player"].as_str() {
        Some("P0") => PlayerId(0),
        Some("P1") => PlayerId(1),
        _ => return Err("choose_outside_game.player must be P0 or P1".to_string()),
    };
    let cards = action["cards"]
        .as_array()
        .ok_or("choose_outside_game needs a cards array")?
        .iter()
        .map(|value| {
            value
                .as_str()
                .filter(|label| !label.is_empty())
                .map(str::to_string)
                .ok_or_else(|| "choose_outside_game card entries must be labels".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    let accept = action["accept"]
        .as_bool()
        .ok_or("choose_outside_game.accept must be a boolean")?;
    if !accept && !cards.is_empty() {
        return Err("choose_outside_game.cards must be empty when declining".to_string());
    }
    Ok(OutsideGameScriptChoice {
        player,
        accept,
        labels: cards,
    })
}

fn parse_mana_choice(action: &Value) -> Result<ManaScriptChoice, String> {
    let player = match action["player"].as_str() {
        Some("P0") => PlayerId(0),
        Some("P1") => PlayerId(1),
        _ => return Err("choose_mana.player must be P0 or P1".to_string()),
    };
    let has_common = action.get("sources").is_some();
    let has_phase = action.get("phase_sources").is_some();
    let has_forge = action.get("forge_sources").is_some();
    if has_common && (has_phase || has_forge) || !has_common && !(has_phase && has_forge) {
        return Err(
            "choose_mana needs sources or both phase_sources and forge_sources".to_string(),
        );
    }
    let parse_sources = |key: &str| -> Result<Vec<String>, String> {
        let sources = action[key]
            .as_array()
            .ok_or_else(|| format!("choose_mana.{key} must be an array"))?
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .filter(|source| !source.is_empty())
                    .map(str::to_string)
                    .ok_or_else(|| format!("choose_mana.{key} entries must be labels"))
            })
            .collect::<Result<Vec<_>, _>>()?;
        if sources.is_empty() {
            return Err(format!("choose_mana.{key} must not be empty"));
        }
        if sources.iter().collect::<HashSet<_>>().len() != sources.len() {
            return Err(format!("choose_mana.{key} cannot repeat labels"));
        }
        Ok(sources)
    };
    let sources = if has_common {
        parse_sources("sources")?
    } else {
        parse_sources("forge_sources")?;
        parse_sources("phase_sources")?
    };
    Ok(ManaScriptChoice { player, sources })
}

fn parse_script_player(value: Option<&str>, context: &str) -> Result<PlayerId, String> {
    match value {
        Some("P0") => Ok(PlayerId(0)),
        Some("P1") => Ok(PlayerId(1)),
        _ => Err(format!("{context}.player must be P0 or P1")),
    }
}

fn parse_surveil_choice(
    action: &Value,
    labels: &HashMap<ObjectId, String>,
) -> Result<SurveilScriptChoice, String> {
    let player = parse_script_player(action["player"].as_str(), "choose_surveil")?;
    let graveyard = resolve_card_choice_labels(action, labels)?;
    if graveyard.iter().collect::<HashSet<_>>().len() != graveyard.len() {
        return Err("choose_surveil.cards cannot repeat labels".to_string());
    }
    Ok(SurveilScriptChoice { player, graveyard })
}

#[derive(Debug, Clone)]
struct DiscardScriptChoice {
    cards: Vec<ObjectId>,
}

fn parse_discard_choice(
    action: &Value,
    labels: &HashMap<ObjectId, String>,
) -> Result<(PlayerId, DiscardScriptChoice), String> {
    let player = parse_script_player(action["player"].as_str(), "choose_discard")?;
    let cards = resolve_card_choice_labels(action, labels)?;
    if cards.iter().collect::<HashSet<_>>().len() != cards.len() {
        return Err("choose_discard.cards cannot repeat labels".to_string());
    }
    Ok((player, DiscardScriptChoice { cards }))
}

fn take_discard_choice_group(
    choices: &mut [VecDeque<DiscardScriptChoice>; 2],
    player: PlayerId,
    offered: &[ObjectId],
    count: usize,
    up_to: bool,
    labels: &HashMap<ObjectId, String>,
) -> Result<Vec<ObjectId>, String> {
    let queue = choices
        .get_mut(player.0 as usize)
        .ok_or("DiscardChoice prompt player must be P0 or P1")?;
    let choice = queue.pop_front().ok_or_else(|| {
        format!(
            "P{} DiscardChoice prompt has no choose_discard entry",
            player.0
        )
    })?;
    let min_count = if up_to { 0 } else { count };
    validate_card_choice_group(&choice.cards, offered, min_count, count, labels)?;
    Ok(choice.cards)
}

fn ensure_discard_choices_consumed(
    choices: &[VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    let unconsumed = choices.iter().map(VecDeque::len).sum::<usize>();
    if unconsumed == 0 {
        return Ok(());
    }
    Err(format!(
        "{unconsumed} choose_discard choice(s) were unconsumed when the script ended"
    ))
}

fn consume_forced_discard_choices(
    choices: &mut [VecDeque<DiscardScriptChoice>; 2],
    events: &[GameEvent],
    labels: &HashMap<ObjectId, String>,
) -> Result<(), String> {
    let mut discarded_by_player = [HashSet::new(), HashSet::new()];
    for event in events {
        if let GameEvent::Discarded {
            player_id,
            object_id,
            ..
        } = event
        {
            let Some(discarded) = discarded_by_player.get_mut(player_id.0 as usize) else {
                continue;
            };
            discarded.insert(*object_id);
        }
    }

    if discarded_by_player.iter().all(HashSet::is_empty) {
        return Ok(());
    }

    for (index, queue) in choices.iter_mut().enumerate() {
        let Some(expected) = queue.front() else {
            continue;
        };
        let actual = &discarded_by_player[index];
        if actual.is_empty() {
            continue;
        }
        let expected: HashSet<_> = expected.cards.iter().copied().collect();
        if &expected != actual {
            let render = |ids: &HashSet<ObjectId>| {
                let mut names: Vec<_> = ids
                    .iter()
                    .map(|id| labels.get(id).cloned().unwrap_or_else(|| format!("{id:?}")))
                    .collect();
                names.sort();
                format!("[{}]", names.join(", "))
            };
            let actual_by_player = discarded_by_player
                .iter()
                .enumerate()
                .map(|(player, ids)| format!("P{player} {}", render(ids)))
                .collect::<Vec<_>>()
                .join(", ");
            return Err(format!(
                "P{index} forced discard did not match: scripted {}, actually discarded {actual_by_player}",
                render(&expected),
            ));
        }
        queue.pop_front();
    }
    Ok(())
}

fn consume_resolved_discard_choices(
    pending_events: &mut Vec<GameEvent>,
    events: Vec<GameEvent>,
    choices: &mut [VecDeque<DiscardScriptChoice>; 2],
    labels: &HashMap<ObjectId, String>,
) -> Result<(), String> {
    for event in events {
        let resolution_finished = matches!(event, GameEvent::StackResolved { .. });
        pending_events.push(event);
        if resolution_finished {
            consume_forced_discard_choices(choices, pending_events, labels)?;
            pending_events.clear();
        }
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct TriggerOrderChoice {
    player: PlayerId,
    /// Normalized description prefixes, first to resolve first.
    resolve: Vec<String>,
}

/// `only = "xmage"` records that Phase does not ask for this order (a logged
/// difference). Such an entry is validated but not queued, so a prompt that
/// does appear fails as unscripted and the field must be removed.
fn parse_trigger_order(action: &Value) -> Result<Option<TriggerOrderChoice>, String> {
    let fields = action.as_object().ok_or("order_triggers must be a table")?;
    if fields
        .keys()
        .any(|key| !["action", "player", "resolve", "only"].contains(&key.as_str()))
    {
        return Err("order_triggers supports only player, resolve, and only".to_string());
    }
    let queued = match action.get("only").map(Value::as_str) {
        None | Some(Some("phase")) => true,
        Some(Some("xmage")) => false,
        Some(_) => return Err("order_triggers.only must be phase or xmage".to_string()),
    };
    let player = match action.get("player").and_then(Value::as_str) {
        Some("P0") => PlayerId(0),
        Some("P1") => PlayerId(1),
        _ => return Err("order_triggers.player must be P0 or P1".to_string()),
    };
    let resolve = action
        .get("resolve")
        .and_then(Value::as_array)
        .filter(|entries| entries.len() >= 2)
        .ok_or("order_triggers.resolve must list at least two ability text prefixes")?
        .iter()
        .map(|entry| {
            entry
                .as_str()
                .map(normalize_ability_text)
                .filter(|prefix| !prefix.is_empty())
                .ok_or("order_triggers.resolve entries must be non-empty strings".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(queued.then_some(TriggerOrderChoice { player, resolve }))
}

/// CR 603.3b: map a scripted resolve order onto the engine's placement order.
/// `descriptions` are the prompted triggers; the result lists prompt indices,
/// first placed first (so it resolves last, CR 405.3).
fn trigger_placement_order(
    resolve: &[String],
    descriptions: &[String],
) -> Result<Vec<usize>, String> {
    if resolve.len() != descriptions.len() {
        return Err(format!(
            "order_triggers lists {} abilities but {} triggers are being ordered: {descriptions:?}",
            resolve.len(),
            descriptions.len()
        ));
    }
    let mut placement = Vec::with_capacity(resolve.len());
    for prefix in resolve.iter().rev() {
        let mut matches = descriptions
            .iter()
            .enumerate()
            .filter(|(index, description)| {
                !placement.contains(index) && description.starts_with(prefix.as_str())
            })
            .map(|(index, _)| index);
        let index = matches.next().ok_or_else(|| {
            format!("order_triggers prefix {prefix:?} matches no trigger in {descriptions:?}")
        })?;
        if matches.any(|other| descriptions[other] != descriptions[index]) {
            return Err(format!(
                "order_triggers prefix {prefix:?} is ambiguous in {descriptions:?}"
            ));
        }
        placement.push(index);
    }
    Ok(placement)
}

fn ensure_trigger_orders_consumed(orders: &VecDeque<TriggerOrderChoice>) -> Result<(), String> {
    if orders.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} order_triggers entry(s) were unconsumed when the script ended",
        orders.len()
    ))
}

#[derive(Default)]
struct DiscardChoiceReconciler {
    pending_events: Vec<GameEvent>,
    trigger_orders: VecDeque<TriggerOrderChoice>,
    /// Script indices of `expect = "refused"` choices the engine refused.
    choice_refusals: Vec<usize>,
}

enum ClassifiedActError {
    Engine(String),
    Harness(String),
}

impl std::fmt::Display for ClassifiedActError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Engine(message) | Self::Harness(message) => formatter.write_str(message),
        }
    }
}

impl DiscardChoiceReconciler {
    fn act(
        &mut self,
        runner: &mut GameRunner,
        action: GameAction,
        choices: &mut [VecDeque<DiscardScriptChoice>; 2],
        labels: &HashMap<ObjectId, String>,
    ) -> Result<(), String> {
        self.act_excluding_discarded_cards(runner, action, &[], choices, labels)
    }

    fn act_for_discard_selection(
        &mut self,
        runner: &mut GameRunner,
        selected_cards: Vec<ObjectId>,
        choices: &mut [VecDeque<DiscardScriptChoice>; 2],
        labels: &HashMap<ObjectId, String>,
    ) -> Result<(), String> {
        self.act_excluding_discarded_cards(
            runner,
            GameAction::SelectCards {
                cards: selected_cards.clone(),
            },
            &selected_cards,
            choices,
            labels,
        )
    }

    fn act_excluding_discarded_cards(
        &mut self,
        runner: &mut GameRunner,
        action: GameAction,
        excluded_cards: &[ObjectId],
        choices: &mut [VecDeque<DiscardScriptChoice>; 2],
        labels: &HashMap<ObjectId, String>,
    ) -> Result<(), String> {
        self.act_classified(runner, action, excluded_cards, choices, labels)
            .map_err(|error| error.to_string())
    }

    fn act_classified(
        &mut self,
        runner: &mut GameRunner,
        action: GameAction,
        excluded_cards: &[ObjectId],
        choices: &mut [VecDeque<DiscardScriptChoice>; 2],
        labels: &HashMap<ObjectId, String>,
    ) -> Result<(), ClassifiedActError> {
        let mut events = runner
            .act(action)
            .map_err(|error| ClassifiedActError::Engine(error.to_string()))?
            .events;
        self.order_pending_triggers(runner, &mut events)
            .map_err(ClassifiedActError::Harness)?;
        let events = events
            .into_iter()
            .filter(|event| {
                !matches!(
                    event,
                    GameEvent::Discarded { object_id, .. } if excluded_cards.contains(object_id)
                )
            })
            .collect();
        consume_resolved_discard_choices(&mut self.pending_events, events, choices, labels)
            .map_err(ClassifiedActError::Harness)
    }

    fn advance_until_stack_empty(
        &mut self,
        runner: &mut GameRunner,
        choices: &mut [VecDeque<DiscardScriptChoice>; 2],
        labels: &HashMap<ObjectId, String>,
    ) -> Result<(), String> {
        // Mirrors `GameRunner::advance_until_stack_empty_with_events`, except that
        // trigger-order prompts are answered from the script instead of by identity.
        let mut events = Vec::new();
        for _ in 0..40 {
            if matches!(runner.state().waiting_for, WaitingFor::OrderTriggers { .. }) {
                self.order_pending_triggers(runner, &mut events)?;
                continue;
            }
            if let WaitingFor::EffectZoneChoice {
                cards,
                count,
                min_count,
                up_to,
                effect_kind,
                ..
            } = &runner.state().waiting_for
            {
                if *effect_kind != EffectKind::PutAtLibraryPosition
                    || *up_to
                    || cards.len() < *min_count
                    || cards.len() < *count
                {
                    break;
                }
                let chosen = cards.iter().take(*count).copied().collect();
                match runner.act(GameAction::SelectCards { cards: chosen }) {
                    Ok(result) => events.extend(result.events),
                    Err(_) => break,
                }
                continue;
            }
            if runner.state().stack.is_empty() {
                break;
            }
            match runner.act(GameAction::PassPriority) {
                Ok(result) => events.extend(result.events),
                Err(_) => break,
            }
        }
        consume_resolved_discard_choices(&mut self.pending_events, events, choices, labels)
    }

    /// CR 603.3b: answer every pending trigger-order prompt. Triggers with the
    /// same source and text need no script; any other set must be scripted.
    fn order_pending_triggers(
        &mut self,
        runner: &mut GameRunner,
        events: &mut Vec<GameEvent>,
    ) -> Result<(), String> {
        for _ in 0..16 {
            let WaitingFor::OrderTriggers { player, triggers } = &runner.state().waiting_for else {
                return Ok(());
            };
            let identical = triggers.windows(2).all(|pair| {
                pair[0].source_id == pair[1].source_id && pair[0].description == pair[1].description
            });
            let order = if identical {
                (0..triggers.len()).collect()
            } else {
                let descriptions = triggers
                    .iter()
                    .map(|trigger| normalize_ability_text(&trigger.description))
                    .collect::<Vec<_>>();
                let Some(choice) = self.trigger_orders.pop_front() else {
                    return Err(format!(
                        "unscripted order for non-identical triggers of P{}: {descriptions:?}",
                        player.0
                    ));
                };
                if choice.player != *player {
                    return Err(format!(
                        "order_triggers was scripted for P{} but P{} is ordering triggers",
                        choice.player.0, player.0
                    ));
                }
                trigger_placement_order(&choice.resolve, &descriptions)?
            };
            let result = runner
                .act(GameAction::OrderTriggers { order })
                .map_err(|error| format!("OrderTriggers was rejected: {error}"))?;
            events.extend(result.events);
        }
        Err("too many trigger-order prompts".to_string())
    }
}

fn ensure_surveil_choices_consumed(choices: &VecDeque<SurveilScriptChoice>) -> Result<(), String> {
    if choices.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} choose_surveil choice(s) were unconsumed when the script ended",
        choices.len()
    ))
}

fn consume_mana_choice(
    choices: &mut VecDeque<ManaScriptChoice>,
    player: PlayerId,
    spec: &Value,
) -> Result<Option<ManaScriptChoice>, String> {
    let Some(choice) = choices.pop_front() else {
        return Ok(None);
    };
    if choice.player != player {
        return Err("choose_mana was scripted for a different player than the cast".to_string());
    }
    let key = format!("P{}", player.0);
    let configured_sources = spec["setup"][key.as_str()]["mana_pool"]
        .as_array()
        .ok_or("choose_mana player has no mana_pool setup")?
        .iter()
        .filter_map(|unit| unit["source"].as_str())
        .collect::<HashSet<_>>();
    if choice
        .sources
        .iter()
        .any(|source| !configured_sources.contains(source.as_str()))
    {
        return Err("choose_mana source is not in the player's mana_pool".to_string());
    }
    Ok(Some(choice))
}

fn mana_pool_source_pips(
    state: &GameState,
    player: PlayerId,
) -> Result<HashMap<engine::types::mana::ManaPipId, ObjectId>, String> {
    let mut source_pips = HashMap::new();
    for unit in state.players[player.0 as usize].mana_pool.units() {
        if unit.pip_id == engine::types::mana::ManaPipId(0) {
            return Err(
                "cannot verify scripted mana choice with an unstamped mana unit".to_string(),
            );
        }
        if source_pips.insert(unit.pip_id, unit.source_id).is_some() {
            return Err(
                "cannot verify scripted mana choice with duplicate mana pip IDs".to_string(),
            );
        }
    }
    Ok(source_pips)
}

fn verify_mana_choice_spent(
    choice: &ManaScriptChoice,
    before: &HashMap<engine::types::mana::ManaPipId, ObjectId>,
    after: &HashMap<engine::types::mana::ManaPipId, ObjectId>,
    labels: &HashMap<ObjectId, String>,
) -> Result<(), String> {
    let spent_sources = before
        .iter()
        .filter(|(pip, _)| !after.contains_key(pip))
        .map(|(_, source)| {
            labels
                .get(source)
                .cloned()
                .ok_or_else(|| format!("spent mana source {source:?} has no setup label"))
        })
        .collect::<Result<HashSet<_>, _>>()?;
    let scripted_sources = choice.sources.iter().cloned().collect::<HashSet<_>>();
    if spent_sources != scripted_sources {
        return Err(format!(
            "choose_mana source mismatch: scripted {scripted_sources:?}, spent {spent_sources:?}"
        ));
    }
    Ok(())
}

fn sideboard_deck_entry(card: &str, db: &CardDatabase) -> Result<DeckEntry, String> {
    let card_face = db
        .get_face_by_name(card)
        .ok_or_else(|| format!("sideboard card {card} is absent from the card database"))?;
    Ok(DeckEntry {
        card: card_face.clone(),
        count: 1,
    })
}

fn ensure_outside_game_choices_consumed(
    choices: &VecDeque<OutsideGameScriptChoice>,
) -> Result<(), String> {
    if choices.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} choose_outside_game group(s) were unconsumed when the script ended",
        choices.len()
    ))
}

fn ensure_mana_choices_consumed(choices: &VecDeque<ManaScriptChoice>) -> Result<(), String> {
    if choices.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} choose_mana group(s) were unconsumed when the script ended",
        choices.len()
    ))
}

fn sideboard_choice_indices(
    choice: &OutsideGameScriptChoice,
    labels: &HashMap<String, (PlayerId, usize, String)>,
    owner: PlayerId,
    offered: &[engine::types::game_state::OutsideGameChoiceEntry],
) -> Result<Vec<OutsideGameSelection>, String> {
    choice
        .labels
        .iter()
        .map(|label| {
            let (label_owner, index, name) = labels
                .get(label)
                .ok_or_else(|| format!("unknown sideboard label {label}"))?;
            if *label_owner != owner {
                return Err(format!("sideboard label {label} belongs to another player"));
            }
            if !offered.iter().any(|entry| {
                matches!(
                    &entry.source,
                    OutsideGameChoiceSource::Sideboard { sideboard_index, .. }
                        if sideboard_index == index
                ) && entry.name == *name
            }) {
                return Err(format!("sideboard label {label} is not offered"));
            }
            Ok(OutsideGameSelection::Sideboard {
                sideboard_index: *index,
            })
        })
        .collect()
}

fn take_card_choice_group(
    groups: &mut VecDeque<Vec<ObjectId>>,
    prompt: &str,
) -> Result<Vec<ObjectId>, String> {
    groups.pop_front().ok_or_else(|| {
        format!("card-choice prompt arrived with an empty choose_cards queue: {prompt}")
    })
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
    copy_cast_choices: &mut VecDeque<CopyCastChoice>,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    match runner.state().waiting_for.clone() {
        WaitingFor::EffectZoneChoice {
            player,
            cards,
            count,
            min_count,
            up_to,
            ..
        } => {
            let group = if cards.is_empty() {
                Vec::new()
            } else {
                take_card_choice_group(
                    groups,
                    &format!(
                        "EffectZoneChoice player={player:?} candidates={:?} count={count} min_count={min_count} up_to={up_to}",
                        cards
                            .iter()
                            .map(|id| labels.get(id).cloned().unwrap_or_else(|| format!("{id:?}")))
                            .collect::<Vec<_>>()
                    ),
                )?
            };
            let min_count = if cards.is_empty() {
                0
            } else if up_to {
                min_count
            } else {
                count
            };
            validate_card_choice_group(&group, &cards, min_count, count, labels)?;
            discard_reconciler.act(
                runner,
                GameAction::SelectCards { cards: group },
                discard_choice_groups,
                labels,
            )
        }
        WaitingFor::ChooseFromZoneChoice {
            cards,
            count,
            up_to,
            player,
            source_id,
            ..
        } => {
            let group = if cards.is_empty() {
                Vec::new()
            } else if let Some(choice) = copy_cast_choices.front() {
                if choice.player != player {
                    return Err(
                        "choose_copies prompt was offered to a different player".to_string()
                    );
                }
                if choice.source != source_id {
                    return Err("choose_copies prompt came from a different source".to_string());
                }
                let choice = copy_cast_choices
                    .pop_front()
                    .expect("front copy-cast choice was present");
                match choice.refused {
                    Some(index) => {
                        validate_card_choice_group(&choice.cards, &cards, 0, count, labels)?;
                        let (refused_card, accepted) = choice
                            .cards
                            .split_last()
                            .filter(|(_, accepted)| !accepted.is_empty())
                            .ok_or("refused choose_copies needs an accepted card first")?;
                        // Only a card the engine accepts on its own shows that the
                        // accepted cards caused the refusal.
                        if GameRunner::from_state(runner.state().clone())
                            .act(GameAction::SelectCards {
                                cards: vec![*refused_card],
                            })
                            .is_err()
                        {
                            return Err(format!(
                                "script[{index}] choose_copies refused card is not selectable on its own"
                            ));
                        }
                        let accepted = accepted.to_vec();
                        let before = project(runner.state(), labels, None, true);
                        let prompt = runner.state().waiting_for.clone();
                        if runner
                            .act(GameAction::SelectCards {
                                cards: choice.cards.clone(),
                            })
                            .is_ok()
                        {
                            return Err(format!(
                                "script[{index}] choose_copies was accepted but expected refused"
                            ));
                        }
                        if runner.state().waiting_for != prompt {
                            return Err(format!(
                                "script[{index}] refused choose_copies changed the prompt"
                            ));
                        }
                        let after = project(runner.state(), labels, None, true);
                        record_refusal(
                            index,
                            "choose_copies",
                            RefusalMode::OfferedThenErr,
                            &before,
                            &after,
                            &mut discard_reconciler.choice_refusals,
                        )?;
                        accepted
                    }
                    None => choice.cards,
                }
            } else {
                take_card_choice_group(
                    groups,
                    &format!(
                        "ChooseFromZoneChoice player={player:?} source={source_id:?} candidates={:?} count={count} up_to={up_to}",
                        cards
                            .iter()
                            .map(|id| labels.get(id).cloned().unwrap_or_else(|| format!("{id:?}")))
                            .collect::<Vec<_>>()
                    ),
                )?
            };
            let min_count = if up_to || cards.is_empty() { 0 } else { count };
            validate_card_choice_group(&group, &cards, min_count, count, labels)?;
            discard_reconciler.act(
                runner,
                GameAction::SelectCards { cards: group },
                discard_choice_groups,
                labels,
            )
        }
        WaitingFor::DigChoice {
            player,
            selectable_cards,
            keep_count,
            up_to,
            ..
        } => {
            let group = if selectable_cards.is_empty() {
                Vec::new()
            } else {
                take_card_choice_group(
                    groups,
                    &format!(
                        "DigChoice player={player:?} candidates={:?} keep_count={keep_count} up_to={up_to}",
                        selectable_cards
                            .iter()
                            .map(|id| labels.get(id).cloned().unwrap_or_else(|| format!("{id:?}")))
                            .collect::<Vec<_>>()
                    ),
                )?
            };
            let min_count = if up_to || selectable_cards.is_empty() {
                0
            } else {
                keep_count
            };
            validate_card_choice_group(&group, &selectable_cards, min_count, keep_count, labels)?;
            discard_reconciler.act(
                runner,
                GameAction::SelectCards { cards: group },
                discard_choice_groups,
                labels,
            )
        }
        WaitingFor::DiscardChoice {
            cards,
            count,
            up_to,
            ..
        } => {
            let group = take_card_choice_group(
                groups,
                &format!(
                    "DiscardChoice candidates={:?} count={count} up_to={up_to}",
                    cards
                        .iter()
                        .map(|id| labels.get(id).cloned().unwrap_or_else(|| format!("{id:?}")))
                        .collect::<Vec<_>>()
                ),
            )?;
            let min_count = if up_to { 0 } else { count };
            validate_card_choice_group(&group, &cards, min_count, count, labels)?;
            discard_reconciler.act_for_discard_selection(
                runner,
                group,
                discard_choice_groups,
                labels,
            )
        }
        WaitingFor::CollectEvidenceChoice { cards, .. } => {
            let group = take_card_choice_group(
                groups,
                &format!(
                    "CollectEvidenceChoice candidates={:?}",
                    cards
                        .iter()
                        .map(|id| labels.get(id).cloned().unwrap_or_else(|| format!("{id:?}")))
                        .collect::<Vec<_>>()
                ),
            )?;
            discard_reconciler.act(
                runner,
                GameAction::SelectCards { cards: group },
                discard_choice_groups,
                labels,
            )
        }
        other => Err(format!(
            "cannot apply card choice while waiting for {other:?}"
        )),
    }
}

fn apply_graveyard_target_choice(
    runner: &mut GameRunner,
    choice: TargetScriptChoice,
    labels: &HashMap<ObjectId, String>,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    let WaitingFor::TriggerTargetSelection { player, .. } = &runner.state().waiting_for else {
        return Err("graveyard_targets arrived without a trigger target prompt".to_string());
    };
    if *player != choice.player {
        return Err("graveyard_targets prompt was offered to a different player".to_string());
    }
    for target in &choice.targets {
        if !labels.contains_key(target) {
            return Err("graveyard_targets contains an unlabeled target".to_string());
        }
    }
    discard_reconciler.act(
        runner,
        GameAction::SelectTargets {
            targets: choice.targets.into_iter().map(TargetRef::Object).collect(),
        },
        discard_choice_groups,
        labels,
    )
}

fn apply_copied_spell_target_choice(
    runner: &mut GameRunner,
    choice: TargetScriptChoice,
    labels: &HashMap<ObjectId, String>,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    let WaitingFor::TargetSelection { player, .. } = &runner.state().waiting_for else {
        return Err("copy_targets arrived without a copied-spell target prompt".to_string());
    };
    if *player != choice.player {
        return Err("copy_targets prompt was offered to a different player".to_string());
    }
    let mut groups = VecDeque::from([choice.targets]);
    apply_activation_target_group(
        runner,
        labels,
        &mut groups,
        discard_reconciler,
        discard_choice_groups,
    )?;
    ensure_activation_target_groups_consumed(&groups)
}

fn normalize_ability_text(text: &str) -> String {
    let trimmed = text.trim_start();
    let flavor_prefix = trimmed
        .strip_prefix("<i>")
        .and_then(|text| text.find("</i>").map(|end| &text[end + 4..]))
        .is_some_and(|text| text.trim_start().starts_with("&mdash;"));
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
    let mut stripped = stripped
        .replace("&mdash;", "—")
        .to_lowercase()
        .replace("{this}", "~")
        .trim()
        .to_string();
    if flavor_prefix
        || stripped
            .split_once('—')
            .is_some_and(|(prefix, _)| !prefix.contains(':'))
    {
        if let Some((_, text)) = stripped.split_once('—') {
            stripped = text.trim().to_string();
        }
    }
    let head_end = stripped.find(':').unwrap_or(stripped.len());
    if let Some(self_reference) = stripped[..head_end].find("this ") {
        stripped.replace_range(self_reference..head_end, "~");
    }
    stripped
}

fn loyalty_ability_label(cost: &AbilityCost) -> Option<String> {
    match cost {
        AbilityCost::Loyalty { amount } => Some(if *amount > 0 {
            format!("+{amount}")
        } else {
            amount.to_string()
        }),
        _ => None,
    }
}

fn parse_loyalty_cost_selector(selector: &str) -> Option<String> {
    let normalized = selector
        .to_lowercase()
        .replace('−', "-")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let normalized = normalized.strip_suffix('.').unwrap_or(&normalized);
    if normalized == "0" {
        return Some(normalized.to_string());
    }
    let amount = normalized
        .strip_prefix('+')
        .or_else(|| normalized.strip_prefix('-'))?;
    if amount.is_empty() || !amount.bytes().all(|digit| digit.is_ascii_digit()) {
        return None;
    }
    Some(normalized.to_string())
}

fn select_activation_ability(
    candidates: impl IntoIterator<Item = (usize, AbilityKind, bool, Option<String>, Option<String>)>,
    selector: &str,
) -> Result<usize, String> {
    let cost_selector = parse_loyalty_cost_selector(selector);
    let description_prefix = normalize_ability_text(&selector.replace('−', "-"));
    if selector.trim().is_empty() {
        return Err("activate needs a non-empty ability cost prefix".to_string());
    }
    let mut matches = Vec::new();
    for (index, kind, is_mana_ability, loyalty_cost_label, description) in candidates {
        if kind != AbilityKind::Activated || is_mana_ability {
            continue;
        }
        let matches_selector = if let Some(cost_selector) = &cost_selector {
            loyalty_cost_label
                .as_deref()
                .and_then(parse_loyalty_cost_selector)
                .as_deref()
                == Some(cost_selector)
        } else {
            let description = description
                .ok_or_else(|| format!("activated ability {index} is missing a description"))?;
            normalize_ability_text(&description.replace('−', "-")).starts_with(&description_prefix)
        };
        if matches_selector {
            matches.push(index);
        }
    }
    match matches.as_slice() {
        [index] => Ok(*index),
        [] => Err(format!("no activated ability matches {selector}")),
        _ => Err(format!(
            "multiple activated abilities match {selector}: {matches:?}"
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

fn resolve_cast_target_slots(
    action: &Value,
    labels: &HashMap<ObjectId, String>,
) -> Result<Vec<Option<TargetRef>>, String> {
    let Some(targets) = action.get("targets") else {
        return Ok(Vec::new());
    };
    let targets = targets.as_array().ok_or("cast.targets must be an array")?;
    targets
        .iter()
        .map(|value| {
            let label = value
                .as_str()
                .ok_or("cast.targets entries must be strings")?;
            if label == "@skip" {
                return Ok(None);
            }
            if let Some(player) = label.strip_prefix("@P") {
                let player = match player {
                    "0" => PlayerId(0),
                    "1" => PlayerId(1),
                    _ => {
                        return Err(format!(
                            "cast.targets player token {label} must be @P0 or @P1"
                        ));
                    }
                };
                return Ok(Some(TargetRef::Player(player)));
            }
            if label.starts_with('@') {
                return Err(format!(
                    "cast.targets label {label} uses reserved @ prefix; use @skip, @P0, or @P1"
                ));
            }
            resolve_label(labels, label)
                .map(TargetRef::Object)
                .map(Some)
                .map_err(|_| format!("unknown target label {label}"))
        })
        .collect()
}

fn validate_cast_target_entries(
    entries: &[Option<TargetRef>],
    decision_slot_count: usize,
    engine_targets: &[TargetRef],
) -> Result<(), String> {
    if entries.len() != decision_slot_count {
        return Err(format!(
            "cast.targets has {} entries for {decision_slot_count} target decision slots",
            entries.len()
        ));
    }
    let declared_targets: Vec<TargetRef> = entries.iter().filter_map(Clone::clone).collect();
    if declared_targets == engine_targets {
        return Ok(());
    }
    Err(format!(
        "cast.targets do not match the spell's engine-chosen targets: declared {declared_targets:?}, engine chose {engine_targets:?}"
    ))
}

fn cast_target_decision_slot_count(
    state: &GameState,
    spell: ObjectId,
    player: PlayerId,
    x: Option<u32>,
) -> Result<usize, String> {
    let definition = state
        .objects
        .get(&spell)
        .and_then(|object| object.abilities.first())
        .ok_or_else(|| "cast.targets could not find the spell's ability".to_string())?;
    let mut resolved = build_resolved_from_def(definition, spell, player);
    resolved.kind = AbilityKind::Spell;
    if let Some(x) = x {
        resolved.set_chosen_x_recursive(x);
    }
    let slots = build_target_slots(state, &resolved).map_err(|error| error.to_string())?;
    Ok(slots
        .iter()
        .filter(|slot| !slot.legal_targets.is_empty())
        .count())
}

fn validate_engine_chosen_cast_targets(
    state: &GameState,
    spell: ObjectId,
    entries: &[Option<TargetRef>],
    decision_slot_count: usize,
) -> Result<(), String> {
    let spell_entry = state
        .stack
        .iter()
        .rev()
        .find(|entry| {
            entry.source_id == spell
                && matches!(
                    &entry.kind,
                    engine::types::game_state::StackEntryKind::Spell { .. }
                )
        })
        .ok_or_else(|| "cast.targets could not find the spell's stack entry".to_string())?;
    let ability = spell_entry
        .ability()
        .ok_or_else(|| "cast.targets could not read the spell's stack ability".to_string())?;
    let engine_targets = declared_targets_in_chain(ability);
    validate_cast_target_entries(entries, decision_slot_count, &engine_targets)
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

fn cast_from(action: &Value) -> Result<&str, String> {
    match action.get("from") {
        None => Ok("hand"),
        Some(value) => match value.as_str() {
            Some("hand") => Ok("hand"),
            Some("graveyard") => Ok("graveyard"),
            Some("exile") => Ok("exile"),
            Some(_) | None => Err("cast.from must be hand, graveyard, or exile".to_string()),
        },
    }
}

fn validate_script_cast_from(action: &Value) -> Result<(), String> {
    if action.get("from").is_some() && action.get("action").and_then(Value::as_str) != Some("cast")
    {
        return Err("script.from is only valid for cast actions".to_string());
    }
    if action.get("action").and_then(Value::as_str) == Some("cast") {
        cast_from(action)?;
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum UnlessScriptChoice {
    Sacrifice(ObjectId),
    Discard(ObjectId),
    Decline,
}

fn resolve_script_label(
    labels: &HashMap<ObjectId, String>,
    label: &str,
) -> Result<ObjectId, String> {
    let mut matches = labels
        .iter()
        .filter_map(|(id, candidate)| (candidate == label).then_some(*id));
    let id = matches
        .next()
        .ok_or_else(|| format!("unknown setup label {label}"))?;
    if matches.next().is_some() {
        return Err(format!("ambiguous setup label {label}"));
    }
    Ok(id)
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum CopyTargetChoice {
    Keep,
    Change(Vec<ObjectId>),
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct CopyChoice {
    player: PlayerId,
    accept: bool,
    new_targets: Option<CopyTargetChoice>,
}

fn take_copy_choice(
    choices: &mut VecDeque<CopyChoice>,
    player: PlayerId,
) -> Result<CopyChoice, String> {
    let choice = choices
        .front()
        .cloned()
        .ok_or("Firebender copy prompt arrived without copy_choice")?;
    if choice.player != player {
        return Err("Firebender copy prompt was offered to a different player".to_string());
    }
    choices
        .pop_front()
        .ok_or("Firebender copy prompt arrived without copy_choice".to_string())
}

fn parse_optional_answers(
    action: &Value,
    labels: &HashMap<ObjectId, String>,
) -> Result<Vec<OptionalChoice>, String> {
    let fields = action
        .as_object()
        .ok_or("choose_optional must be a table")?;
    if fields
        .keys()
        .any(|key| !["action", "answers", "source"].contains(&key.as_str()))
    {
        return Err("choose_optional supports only answers and source".to_string());
    }
    let source = action
        .get("source")
        .map(|value| {
            let label = value
                .as_str()
                .ok_or_else(|| "choose_optional.source must be a label".to_string())?;
            resolve_script_label(labels, label)
        })
        .transpose()?;
    let answers = action
        .get("answers")
        .and_then(Value::as_array)
        .ok_or("choose_optional.answers must be an array")?;
    if answers.is_empty() {
        return Err("choose_optional.answers must be a non-empty array".to_string());
    }
    answers
        .iter()
        .map(|answer| match answer.as_str() {
            Some("yes") => Ok(OptionalChoice {
                answer: true,
                source,
            }),
            Some("no") => Ok(OptionalChoice {
                answer: false,
                source,
            }),
            _ => Err("choose_optional.answers entries must be exactly yes or no".to_string()),
        })
        .collect()
}

fn take_optional_choice(
    choices: &mut [VecDeque<OptionalChoice>; 2],
    player: PlayerId,
    source_id: Option<ObjectId>,
) -> Result<bool, String> {
    let player_index = player.0 as usize;
    if player_index >= choices.len() {
        return Err("choose_optional prompt has an invalid player".to_string());
    }
    if choices[player_index].is_empty() {
        return Err(if choices.iter().any(|queue| !queue.is_empty()) {
            "choose_optional answer was queued for a different player".to_string()
        } else {
            "optional prompt arrived without a choose_optional answer".to_string()
        });
    }
    let choice = *choices[player_index]
        .front()
        .ok_or("optional prompt arrived without a choose_optional answer")?;
    if choice.source.is_some() && choice.source != source_id {
        return Err(format!(
            "choose_optional source mismatch: expected {:?}, got {:?}",
            choice.source, source_id
        ));
    }
    choices[player_index].pop_front();
    Ok(choice.answer)
}

fn ensure_optional_choices_consumed(choices: &[VecDeque<OptionalChoice>; 2]) -> Result<(), String> {
    let remaining = choices.iter().map(VecDeque::len).sum::<usize>();
    if remaining == 0 {
        return Ok(());
    }
    Err(format!(
        "{remaining} choose_optional answer(s) were unconsumed when the script ended"
    ))
}

fn parse_copy_choice(
    action: &Value,
    labels: &HashMap<ObjectId, String>,
) -> Result<CopyChoice, String> {
    let fields = action.as_object().ok_or("copy_choice must be a table")?;
    if fields
        .keys()
        .any(|key| !["action", "player", "decision", "new_targets"].contains(&key.as_str()))
    {
        return Err("copy_choice supports only player, decision, and new_targets".to_string());
    }
    let player = match action.get("player").and_then(Value::as_str) {
        Some("P0") => PlayerId(0),
        Some("P1") => PlayerId(1),
        _ => return Err("copy_choice.player must be P0 or P1".to_string()),
    };
    let accept = match action.get("decision").and_then(Value::as_str) {
        Some("accept") => true,
        Some("decline") => false,
        _ => return Err("copy_choice.decision must be accept or decline".to_string()),
    };
    let new_targets = match action.get("new_targets") {
        None => None,
        Some(Value::String(mode)) if mode == "keep" && accept => Some(CopyTargetChoice::Keep),
        Some(Value::Array(targets)) if accept && !targets.is_empty() => {
            Some(CopyTargetChoice::Change(
                targets
                    .iter()
                    .map(|target| {
                        let label = target
                            .as_str()
                            .ok_or("copy_choice.new_targets entries must be labels")?;
                        resolve_script_label(labels, label)
                    })
                    .collect::<Result<Vec<_>, _>>()?,
            ))
        }
        Some(Value::String(_)) if !accept => {
            return Err("copy_choice.new_targets is only valid when accepting the copy".to_string())
        }
        Some(Value::Array(_)) if !accept => {
            return Err("copy_choice.new_targets is only valid when accepting the copy".to_string())
        }
        Some(Value::String(_)) => {
            return Err(
                "copy_choice.new_targets must be keep or a non-empty label array".to_string(),
            )
        }
        Some(Value::Array(_)) => {
            return Err("copy_choice.new_targets must be a non-empty label array".to_string())
        }
        Some(_) => return Err("copy_choice.new_targets must be keep or a label array".to_string()),
    };
    Ok(CopyChoice {
        player,
        accept,
        new_targets,
    })
}

fn take_copy_target_choice(
    pending: &mut Option<(PlayerId, CopyTargetChoice)>,
    player: PlayerId,
) -> Result<CopyTargetChoice, String> {
    let Some((expected_player, _)) = pending.as_ref() else {
        return Err("copy retarget prompt arrived without copy_choice.new_targets".to_string());
    };
    if *expected_player != player {
        return Err("copy retarget prompt was offered to a different player".to_string());
    }
    pending
        .take()
        .map(|(_, choice)| choice)
        .ok_or_else(|| "copy retarget prompt arrived without copy_choice.new_targets".to_string())
}

fn ensure_copy_target_choice_consumed(
    pending: &Option<(PlayerId, CopyTargetChoice)>,
) -> Result<(), String> {
    if pending.is_some() {
        return Err(
            "copy_choice.new_targets was not consumed by a copy retarget prompt".to_string(),
        );
    }
    Ok(())
}

#[cfg(test)]
fn declare_attackers(
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    action: &Value,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    let mut trigger_target_groups = VecDeque::new();
    declare_attackers_with_trigger_targets(
        runner,
        labels,
        action,
        &mut trigger_target_groups,
        discard_reconciler,
        discard_choice_groups,
    )
}

fn declare_attackers_with_trigger_targets(
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    action: &Value,
    trigger_target_groups: &mut VecDeque<TriggerTargetGroup>,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    let active = runner.state().active_player;
    let fields = action.as_object().ok_or("attack must be a table")?;
    if fields
        .keys()
        .any(|key| !["action", "attackers", "through"].contains(&key.as_str()))
    {
        return Err("attack supports only attackers and through".to_string());
    }
    if let Some(through) = action.get("through") {
        if through.as_str() != Some("damage") {
            return Err("attack.through must be \"damage\"".to_string());
        }
    }
    if active != PlayerId(0) {
        return Err("attack action requires P0 active so attackers can attack P1".to_string());
    }
    let entries = action
        .get("attackers")
        .and_then(Value::as_array)
        .ok_or("attack.attackers must be a non-empty label array")?;
    if entries.is_empty() {
        return Err("attack.attackers must be a non-empty label array".to_string());
    }

    runner.advance_to_combat();
    let mut reached_attackers = false;
    for _ in 0..128 {
        match runner.state().waiting_for.clone() {
            WaitingFor::DeclareAttackers { .. } => {
                reached_attackers = true;
                break;
            }
            WaitingFor::TriggerTargetSelection { .. } => apply_trigger_target_group(
                runner,
                labels,
                trigger_target_groups,
                discard_reconciler,
                discard_choice_groups,
            )?,
            WaitingFor::Priority { .. } => discard_reconciler.act(
                runner,
                GameAction::PassPriority,
                discard_choice_groups,
                labels,
            )?,
            waiting_for => {
                return Err(format!(
                    "attack progression encountered an unscripted prompt: {waiting_for:?}"
                ))
            }
        }
    }
    if !reached_attackers {
        return Err("attack progression exceeded the action limit".to_string());
    }
    let (player, valid_attackers) = match &runner.state().waiting_for {
        WaitingFor::DeclareAttackers {
            player,
            valid_attacker_ids,
            ..
        } => (*player, valid_attacker_ids),
        waiting => {
            return Err(format!(
                "attack expected declare-attackers prompt, got {waiting:?}"
            ))
        }
    };
    if player != active {
        return Err("declare-attackers prompt was offered to a different player".to_string());
    }

    let mut seen = HashSet::new();
    let mut attacks = Vec::with_capacity(entries.len());
    for entry in entries {
        let label = entry
            .as_str()
            .ok_or("attack.attackers entries must be labels")?;
        let id = resolve_script_label(labels, label)?;
        if !seen.insert(id) {
            return Err(format!("attack.attackers repeats label {label}"));
        }
        let object = runner
            .state()
            .objects
            .get(&id)
            .ok_or_else(|| format!("attack label {label} is not a battlefield object"))?;
        if object.zone != Zone::Battlefield || object.controller != active {
            return Err(format!(
                "attack label {label} must be a P0 battlefield creature"
            ));
        }
        if !valid_attackers.contains(&id) {
            return Err(format!(
                "attack label {label} is not offered as a legal attacker"
            ));
        }
        attacks.push((id, AttackTarget::Player(PlayerId(1))));
    }
    discard_reconciler.act(
        runner,
        GameAction::DeclareAttackers {
            attacks,
            bands: Vec::new(),
        },
        discard_choice_groups,
        labels,
    )?;
    if runner.state().phase != Phase::DeclareAttackers {
        return Err("attacker declaration advanced past the declare-attackers step".to_string());
    }
    Ok(())
}

fn ensure_attack_resolution_boundary(runner: &GameRunner) -> Result<(), String> {
    if runner.state().phase != Phase::DeclareAttackers
        || !runner.state().stack.is_empty()
        || !matches!(runner.state().waiting_for, WaitingFor::Priority { .. })
    {
        return Err(format!(
            "attack triggers must finish at empty-stack priority in declare attackers; reached phase {:?}, waiting {:?}, stack {}",
            runner.state().phase,
            runner.state().waiting_for,
            runner.state().stack.len()
        ));
    }
    Ok(())
}

fn cast_sacrifice_labels(
    action: &Value,
    labels: &HashMap<ObjectId, String>,
) -> Result<Option<Vec<ObjectId>>, String> {
    let Some(value) = action.get("sacrifice") else {
        return Ok(None);
    };
    if action.get("action").and_then(Value::as_str) != Some("cast") {
        return Err("script.sacrifice is only valid for cast actions".to_string());
    }
    if action.get("x").is_some() {
        return Err("cast.sacrifice cannot be combined with cast.x".to_string());
    }
    let entries = value
        .as_array()
        .ok_or("cast.sacrifice must be an array of setup labels")?;
    let mut selected = Vec::with_capacity(entries.len());
    let mut seen = HashSet::new();
    for entry in entries {
        let label = entry
            .as_str()
            .ok_or("cast.sacrifice entries must be setup labels")?;
        let id = resolve_script_label(labels, label)?;
        if !seen.insert(id) {
            return Err(format!("cast.sacrifice repeats label {label}"));
        }
        selected.push(id);
    }
    Ok(Some(selected))
}

fn ability_cost_contains_sacrifice(cost: &AbilityCost) -> bool {
    let mut contains_sacrifice = false;
    cost.for_each_cost_node(&mut |node| {
        contains_sacrifice |= matches!(node, AbilityCost::Sacrifice(_));
    });
    contains_sacrifice
}

fn additional_cost_contains_sacrifice(cost: &AdditionalCost) -> bool {
    match cost {
        AdditionalCost::Optional { cost, .. } | AdditionalCost::Required(cost) => {
            ability_cost_contains_sacrifice(cost)
        }
        AdditionalCost::Kicker { costs, .. } => costs.iter().any(ability_cost_contains_sacrifice),
        AdditionalCost::Choice(first, second) => {
            ability_cost_contains_sacrifice(first) || ability_cost_contains_sacrifice(second)
        }
    }
}

fn probe_cast_sacrifice_prompt(
    runner: &GameRunner,
    spell: ObjectId,
) -> Result<Option<bool>, String> {
    let action = engine::ai_support::legal_actions(runner.state())
        .into_iter()
        .find(|candidate| {
            matches!(
                candidate,
                GameAction::CastSpell { object_id, .. } if *object_id == spell
            )
        });
    let Some(action) = action else {
        return Ok(None);
    };

    let mut probe = GameRunner::from_state(runner.state().clone());
    probe
        .act(action)
        .map_err(|error| format!("cast sacrifice prompt probe failed: {error}"))?;
    let offered = match &probe.state().waiting_for {
        WaitingFor::OptionalCostChoice { cost, .. } => additional_cost_contains_sacrifice(cost),
        _ => false,
    };
    Ok(Some(offered))
}

fn ensure_cast_sacrifice_prompt_decision(
    sacrifices: Option<&[ObjectId]>,
    prompt_offered: Option<bool>,
) -> Result<(), String> {
    match (sacrifices.is_some(), prompt_offered) {
        (false, Some(true)) => {
            Err("cast offered an optional sacrifice cost without cast.sacrifice".to_string())
        }
        (true, Some(false)) => Err(
            "cast.sacrifice was provided but no optional sacrifice cost prompt was offered"
                .to_string(),
        ),
        _ => Ok(()),
    }
}

fn parse_unless_script_choices(
    action: &Value,
    labels: &HashMap<ObjectId, String>,
) -> Result<(PlayerId, Vec<UnlessScriptChoice>), String> {
    if action.get("action").and_then(Value::as_str) != Some("unless_choices") {
        return Err("unless_choices action is required".to_string());
    }
    let fields = action.as_object().ok_or("unless_choices must be a table")?;
    if fields
        .keys()
        .any(|key| !["action", "player", "choices"].contains(&key.as_str()))
    {
        return Err("unless_choices supports only player and choices".to_string());
    }
    let player = match action.get("player").and_then(Value::as_str) {
        Some("P0") => PlayerId(0),
        Some("P1") => PlayerId(1),
        _ => return Err("unless_choices.player must be P0 or P1".to_string()),
    };
    let entries = action
        .get("choices")
        .and_then(Value::as_array)
        .ok_or("unless_choices.choices must be a non-empty array")?;
    if entries.is_empty() {
        return Err("unless_choices.choices must be a non-empty array".to_string());
    }
    let mut choices = Vec::with_capacity(entries.len());
    for (index, entry) in entries.iter().enumerate() {
        if entry.as_str() == Some("decline") {
            choices.push(UnlessScriptChoice::Decline);
            continue;
        }
        let choice = entry
            .as_object()
            .ok_or("unless_choices entries must be sacrifice/discard tables or decline")?;
        if choice.len() != 1 {
            return Err(format!(
                "unless_choices.choices[{index}] must contain exactly one choice"
            ));
        }
        let (kind, label_value) = choice.iter().next().expect("one entry checked");
        let label = label_value
            .as_str()
            .ok_or("unless_choices labels must be strings")?;
        let id = resolve_script_label(labels, label)?;
        match kind.as_str() {
            "sacrifice" => choices.push(UnlessScriptChoice::Sacrifice(id)),
            "discard" => choices.push(UnlessScriptChoice::Discard(id)),
            _ => {
                return Err(format!(
                    "unless_choices.choices[{index}] must use sacrifice or discard"
                ))
            }
        }
    }
    Ok((player, choices))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum UnlessScriptResponse {
    AutoDecline,
    Decline,
    Pay {
        choice: UnlessScriptChoice,
        index: usize,
    },
}

fn take_unless_script_choice_if_payable(
    choices: &mut VecDeque<UnlessScriptChoice>,
    player: PlayerId,
    payable_indices: &[usize],
    cost_index: impl FnOnce(UnlessScriptChoice) -> Result<usize, String>,
) -> Result<UnlessScriptResponse, String> {
    if payable_indices.is_empty() {
        return Ok(UnlessScriptResponse::AutoDecline);
    }
    let choice = choices
        .front()
        .copied()
        .ok_or_else(|| format!("unless prompt for {player:?} has no scripted choice"))?;
    if choice == UnlessScriptChoice::Decline {
        choices.pop_front();
        return Ok(UnlessScriptResponse::Decline);
    }
    let index = cost_index(choice)?;
    if !payable_indices.contains(&index) {
        return Err(format!(
            "scripted unless branch {choice:?} names unpayable cost index {index} for {player:?}"
        ));
    }
    choices.pop_front();
    Ok(UnlessScriptResponse::Pay { choice, index })
}

fn unless_cost_index(costs: &[AbilityCost], choice: UnlessScriptChoice) -> Result<usize, String> {
    let index = costs.iter().position(|cost| match choice {
        UnlessScriptChoice::Sacrifice(_) => matches!(cost, AbilityCost::Sacrifice(_)),
        UnlessScriptChoice::Discard(_) => matches!(cost, AbilityCost::Discard { .. }),
        UnlessScriptChoice::Decline => false,
    });
    index.ok_or_else(|| format!("scripted unless branch {choice:?} was not offered"))
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum UnlessBranchPayability {
    Payable {
        prompt: &'static str,
        candidates: usize,
    },
    Unpayable {
        reason: String,
    },
}

fn unless_cost_objects_moved(
    before: &GameState,
    after: &GameState,
    player: PlayerId,
    cost: &AbilityCost,
) -> Result<usize, String> {
    let origin = match cost {
        AbilityCost::Sacrifice(_) => Zone::Battlefield,
        AbilityCost::Discard { .. } => Zone::Hand,
        other => {
            return Err(format!(
                "unless payability probe cannot track cost object movement for {other:?}"
            ))
        }
    };
    let mut moved = 0;
    for (id, object) in &before.objects {
        let controlled_by_payer = match origin {
            Zone::Battlefield => object.controller == player,
            Zone::Hand => object.owner == player,
            _ => unreachable!(),
        };
        if object.zone == origin
            && controlled_by_payer
            && after
                .objects
                .get(id)
                .is_none_or(|after_object| after_object.zone != origin)
        {
            moved += 1;
        }
    }
    Ok(moved)
}

fn probe_unless_branch_payability(
    state: &GameState,
    player: PlayerId,
    index: usize,
    cost: &AbilityCost,
) -> Result<UnlessBranchPayability, String> {
    let before = state.clone();
    let mut probe = GameRunner::from_state(before.clone());
    if let Err(error) = probe.act(GameAction::ChooseUnlessCostBranch {
        choice: UnlessCostBranch::Pay { index },
    }) {
        return Ok(UnlessBranchPayability::Unpayable {
            reason: format!("Pay action rejected: {error}"),
        });
    }
    match (&probe.state().waiting_for, cost) {
        (
            WaitingFor::WardSacrificeChoice { permanents, .. },
            AbilityCost::Sacrifice(_),
        ) if permanents.is_empty() => Ok(UnlessBranchPayability::Unpayable {
            reason: "WardSacrificeChoice had no candidates".to_string(),
        }),
        (
            WaitingFor::WardSacrificeChoice { permanents, .. },
            AbilityCost::Sacrifice(_),
        ) => Ok(UnlessBranchPayability::Payable {
            prompt: "WardSacrificeChoice",
            candidates: permanents.len(),
        }),
        (WaitingFor::WardDiscardChoice { cards, .. }, AbilityCost::Discard { .. })
            if cards.is_empty() =>
        {
            Ok(UnlessBranchPayability::Unpayable {
                reason: "WardDiscardChoice had no candidates".to_string(),
            })
        }
        (WaitingFor::WardDiscardChoice { cards, .. }, AbilityCost::Discard { .. }) => {
            Ok(UnlessBranchPayability::Payable {
                prompt: "WardDiscardChoice",
                candidates: cards.len(),
            })
        }
        (
            WaitingFor::Priority { .. } | WaitingFor::GameOver { .. },
            AbilityCost::Sacrifice(_) | AbilityCost::Discard { .. },
        ) if probe.state().stack.is_empty() => {
            let moved = unless_cost_objects_moved(&before, probe.state(), player, cost)?;
            if moved == 0 {
                Ok(UnlessBranchPayability::Unpayable {
                    reason: "step completed without moving a cost object".to_string(),
                })
            } else {
                Ok(UnlessBranchPayability::Payable {
                    prompt: "completed with cost object movement",
                    candidates: moved,
                })
            }
        }
        (waiting, _) => Err(format!(
            "unless payability probe for {player:?} cost index {index} reached unexpected state {waiting:?} with stack length {}",
            probe.state().stack.len()
        )),
    }
}

fn ensure_unless_choice_is_offered(
    label: &str,
    selected: ObjectId,
    offered: &[ObjectId],
) -> Result<(), String> {
    if offered.contains(&selected) {
        return Ok(());
    }
    Err(format!("unless choice label {label} is not offered"))
}

fn ensure_unless_choice_groups_consumed(
    choices: &[VecDeque<UnlessScriptChoice>; 2],
) -> Result<(), String> {
    let remaining = choices.iter().map(VecDeque::len).sum::<usize>();
    if remaining == 0 {
        return Ok(());
    }
    Err(format!(
        "{remaining} unless_choices entry(s) were unconsumed when the script ended"
    ))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum AlternativeCostDecision {
    Accept,
    Decline,
}

fn alternative_cost_decision(action: &Value) -> Result<Option<AlternativeCostDecision>, String> {
    match action.get("alternative_cost") {
        None => Ok(None),
        Some(Value::String(value)) if value == "accept" => {
            Ok(Some(AlternativeCostDecision::Accept))
        }
        Some(Value::String(value)) if value == "decline" => {
            Ok(Some(AlternativeCostDecision::Decline))
        }
        Some(_) => Err("activate.alternative_cost must be accept or decline".to_string()),
    }
}

fn base_activation_cost(
    cost: &engine::types::ability::AbilityCost,
) -> Option<&engine::types::ability::AbilityCost> {
    use engine::types::ability::AbilityCost;

    match cost {
        AbilityCost::OneOf { costs } => costs.first(),
        cost => Some(cost),
    }
}

fn select_activation_cost_branch(
    costs: &[engine::types::ability::AbilityCost],
    base_cost: Option<&engine::types::ability::AbilityCost>,
    decision: AlternativeCostDecision,
) -> Result<usize, String> {
    let base_cost = base_cost.ok_or_else(|| {
        "activated ability has no original cost branch for alternative_cost".to_string()
    })?;
    let base_index = costs.iter().position(|cost| cost == base_cost);
    match decision {
        AlternativeCostDecision::Decline => base_index
            .ok_or_else(|| "the original activation cost branch was not offered".to_string()),
        AlternativeCostDecision::Accept => match base_index {
            Some(base_index) => costs
                .iter()
                .enumerate()
                .find_map(|(index, _)| (index != base_index).then_some(index))
                .ok_or_else(|| {
                    "no payable alternative activation cost branch was offered".to_string()
                }),
            None if !costs.is_empty() => Ok(0),
            None => Err("no payable alternative activation cost branch was offered".to_string()),
        },
    }
}

fn ensure_activation_cost_prompt_matches_field(
    decision: Option<AlternativeCostDecision>,
    prompt_seen: bool,
) -> Result<(), String> {
    match (decision.is_some(), prompt_seen) {
        (true, false) => Err(
            "activate.alternative_cost was provided but no alternative-cost prompt appeared"
                .to_string(),
        ),
        (false, true) => {
            Err("an alternative-cost prompt appeared without activate.alternative_cost".to_string())
        }
        _ => Ok(()),
    }
}

fn apply_trigger_target_group(
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    groups: &mut VecDeque<TriggerTargetGroup>,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    let trigger_entry = runner
        .state()
        .pending_trigger_entry
        .ok_or("trigger target prompt has no pending stack entry")?;
    let group = take_trigger_target_group(groups)?;
    let (group, refused) = match group.refused {
        Some(index) => {
            let (last, accepted) = group
                .targets
                .split_last()
                .ok_or("refused trigger_targets needs the refused target last")?;
            let last = last.ok_or("refused trigger_targets cannot end with @skip")?;
            if accepted.is_empty() {
                return Err("refused trigger_targets needs an accepted target first".to_string());
            }
            if accepted.contains(&None) {
                // A skipped slot could hide a slot that offers the refused target.
                return Err("refused trigger_targets cannot contain @skip".to_string());
            }
            (accepted.to_vec(), Some((index, TargetRef::Object(last))))
        }
        None => (group.targets, None),
    };
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
        if target_index == 0 {
            if let Some((index, target)) = &refused {
                // Only a target that is legal on its own shows that the accepted
                // targets caused the refusal.
                if !slot.legal_targets.contains(target) {
                    return Err(format!(
                        "script[{index}] trigger_targets refused target is not legal on its own"
                    ));
                }
            }
        }
        if target_index >= group.len() {
            if let Some((index, target)) = &refused {
                if slot.legal_targets.contains(target) {
                    return Err(format!(
                        "script[{index}] trigger_targets last target was offered but expected refused"
                    ));
                }
            }
        }
        let (target, consume_choice) = match group.get(target_index).copied() {
            Some(None) if !slot.optional => {
                return Err(format!(
                    "trigger_targets cannot skip required slot {}",
                    selection.current_slot
                ));
            }
            Some(None) => (None, true),
            Some(Some(id)) => (
                trigger_target_for_slot(
                    Some(id),
                    &slot.legal_targets,
                    slot.optional,
                    labels,
                    selection.current_slot,
                )?,
                true,
            ),
            None => (
                trigger_target_for_slot(
                    None,
                    &slot.legal_targets,
                    slot.optional,
                    labels,
                    selection.current_slot,
                )?,
                false,
            ),
        };
        discard_reconciler.act(
            runner,
            GameAction::ChooseTarget { target },
            discard_choice_groups,
            labels,
        )?;
        if consume_choice {
            target_index += 1;
        }
    }
    ensure_trigger_target_group_consumed(group.len(), target_index)?;
    if let Some((index, _)) = refused {
        eprintln!("Phase refusal: script[{index}] trigger_targets: no longer a legal target");
        discard_reconciler.choice_refusals.push(index);
    }
    Ok(())
}

fn take_trigger_target_group(
    groups: &mut VecDeque<TriggerTargetGroup>,
) -> Result<TriggerTargetGroup, String> {
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

fn ensure_trigger_target_groups_consumed(
    groups: &VecDeque<TriggerTargetGroup>,
) -> Result<(), String> {
    if groups.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} trigger target group(s) were unconsumed when the script ended",
        groups.len()
    ))
}

fn ensure_copy_choices_consumed(choices: &VecDeque<CopyChoice>) -> Result<(), String> {
    if choices.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} copy_choice entry(s) were unconsumed when the script ended",
        choices.len()
    ))
}

fn ensure_copy_cast_choices_consumed(choices: &VecDeque<CopyCastChoice>) -> Result<(), String> {
    if choices.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} choose_copies entry(s) were unconsumed when the script ended",
        choices.len()
    ))
}

fn ensure_target_choices_consumed(
    action: &str,
    choices: &VecDeque<TargetScriptChoice>,
) -> Result<(), String> {
    if choices.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} {action} group(s) were unconsumed when the script ended",
        choices.len()
    ))
}

fn ensure_targeted_cast_has_no_pending_trigger_targets(
    action: &Value,
    trigger_target_groups: &VecDeque<TriggerTargetGroup>,
) -> Result<(), String> {
    if action.get("targets").is_some() && !trigger_target_groups.is_empty() {
        return Err(
            "trigger_targets queued before a targeted cast; list them after the action whose trigger consumes them"
                .to_string(),
        );
    }
    Ok(())
}

fn apply_activation_target_group(
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    groups: &mut VecDeque<Vec<ObjectId>>,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    let group = groups
        .pop_front()
        .ok_or("activation target prompt arrived without activate.targets")?;
    let mut target_index = 0;
    loop {
        let WaitingFor::TargetSelection {
            target_slots,
            selection,
            ..
        } = runner.state().waiting_for.clone()
        else {
            break;
        };
        let slot = target_slots
            .get(selection.current_slot)
            .ok_or("activation target slot is missing")?;
        let id = group.get(target_index).copied();
        let target = id.map(TargetRef::Object);
        if target
            .as_ref()
            .is_some_and(|target| !slot.legal_targets.contains(target))
        {
            let label = id
                .and_then(|id| labels.get(&id))
                .map(String::as_str)
                .unwrap_or("<unknown>");
            return Err(format!(
                "activation target label {label} is not legal for slot {}",
                selection.current_slot
            ));
        }
        if target.is_none() && !slot.optional {
            return Err(format!(
                "activation target group has too few targets for required slot {}",
                selection.current_slot
            ));
        }
        let has_target = target.is_some();
        discard_reconciler.act(
            runner,
            GameAction::ChooseTarget { target },
            discard_choice_groups,
            labels,
        )?;
        if has_target {
            target_index += 1;
        }
    }
    if group.len() != target_index {
        return Err(format!(
            "activation target group has {} targets but used {target_index}",
            group.len()
        ));
    }
    Ok(())
}

fn ensure_activation_target_groups_consumed(
    groups: &VecDeque<Vec<ObjectId>>,
) -> Result<(), String> {
    if groups.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} activation target group(s) were unconsumed when the script ended",
        groups.len()
    ))
}

fn apply_copy_target_choice(
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    player: PlayerId,
    choice: CopyTargetChoice,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    let WaitingFor::CopyRetarget {
        player: prompt_player,
        target_slots,
        current_slot: first_slot,
        can_keep_rest,
        announcer_election,
        mode,
        ..
    } = runner.state().waiting_for.clone()
    else {
        return Err("copy target choice arrived without a copy retarget prompt".to_string());
    };
    if prompt_player != player {
        return Err("copy retarget prompt was offered to a different player".to_string());
    }
    if !matches!(
        mode,
        Some(
            engine::types::game_state::CopyChoiceMode::Retarget
                | engine::types::game_state::CopyChoiceMode::Announce
        )
    ) || announcer_election.is_some()
    {
        return Err("copy target choice arrived for an unsupported copy prompt".to_string());
    }
    match choice {
        CopyTargetChoice::Keep => {
            if mode != Some(engine::types::game_state::CopyChoiceMode::Retarget) {
                return Err("copy targets cannot be kept while announcing a cast copy".to_string());
            }
            if !can_keep_rest {
                return Err("copy_choice.new_targets=keep is not legal for this copy".to_string());
            }
            discard_reconciler.act(
                runner,
                GameAction::KeepAllCopyTargets,
                discard_choice_groups,
                labels,
            )?;
        }
        CopyTargetChoice::Change(targets) => {
            let remaining_slots = target_slots
                .len()
                .checked_sub(first_slot)
                .ok_or("copy target slot index is out of range")?;
            if targets.len() != remaining_slots {
                return Err(format!(
                    "copy_choice.new_targets has {} labels for {remaining_slots} remaining target slots",
                    targets.len()
                ));
            }
            for (offset, id) in targets.into_iter().enumerate() {
                let WaitingFor::CopyRetarget {
                    player: prompt_player,
                    target_slots,
                    current_slot,
                    announcer_election,
                    ..
                } = runner.state().waiting_for.clone()
                else {
                    return Err(
                        "copy retarget prompt ended before all scripted targets were chosen"
                            .to_string(),
                    );
                };
                if prompt_player != player
                    || current_slot != first_slot + offset
                    || announcer_election.is_some()
                {
                    return Err("copy retarget prompt changed unexpectedly".to_string());
                }
                let slot = target_slots
                    .get(current_slot)
                    .ok_or("copy target slot is missing")?;
                let target = TargetRef::Object(id);
                if !slot.legal_alternatives.contains(&target) {
                    let label = labels.get(&id).map(String::as_str).unwrap_or("<unknown>");
                    return Err(format!(
                        "copy target label {label} is not legal for slot {current_slot}"
                    ));
                }
                discard_reconciler.act(
                    runner,
                    GameAction::ChooseTarget {
                        target: Some(target),
                    },
                    discard_choice_groups,
                    labels,
                )?;
            }
            if matches!(runner.state().waiting_for, WaitingFor::CopyRetarget { .. }) {
                return Err("copy target labels did not complete the retarget prompt".to_string());
            }
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn pass_until_resolved(
    scenario_name: &str,
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    groups: &mut VecDeque<TriggerTargetGroup>,
    activation_target_groups: &mut VecDeque<Vec<ObjectId>>,
    card_choice_groups: &mut VecDeque<Vec<ObjectId>>,
    copy_cast_choices: &mut VecDeque<CopyCastChoice>,
    graveyard_target_choices: &mut VecDeque<TargetScriptChoice>,
    copy_target_choices: &mut VecDeque<TargetScriptChoice>,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
    surveil_choices: &mut VecDeque<SurveilScriptChoice>,
    sideboard_labels: &HashMap<String, (PlayerId, usize, String)>,
    outside_game_choices: &mut VecDeque<OutsideGameScriptChoice>,
    copy_choices: &mut VecDeque<CopyChoice>,
    optional_choice_groups: &mut [VecDeque<OptionalChoice>; 2],
    unless_choice_groups: &mut [VecDeque<UnlessScriptChoice>; 2],
    discard_reconciler: &mut DiscardChoiceReconciler,
    fail_closed_unscripted_prompts: bool,
) -> Result<(), String> {
    let mut pending_unless_choice = None;
    let mut pending_copy_target_choice = None;
    for _ in 0..128 {
        if fail_closed_unscripted_prompts
            && matches!(
                runner.state().waiting_for,
                WaitingFor::OrderTriggers { .. }
                    | WaitingFor::AssignCombatDamage { .. }
                    | WaitingFor::AssignBlockerDamage { .. }
            )
        {
            return Err(format!(
                "combat progression encountered an unscripted prompt: {:?}",
                runner.state().waiting_for
            ));
        }
        match runner.state().waiting_for.clone() {
            WaitingFor::TriggerTargetSelection { .. } if !graveyard_target_choices.is_empty() => {
                let choice = graveyard_target_choices
                    .pop_front()
                    .expect("front graveyard target choice was present");
                apply_graveyard_target_choice(
                    runner,
                    choice,
                    labels,
                    discard_reconciler,
                    discard_choice_groups,
                )?;
            }
            WaitingFor::TriggerTargetSelection { .. } => {
                apply_trigger_target_group(
                    runner,
                    labels,
                    groups,
                    discard_reconciler,
                    discard_choice_groups,
                )?;
            }
            WaitingFor::TargetSelection { .. } if !copy_target_choices.is_empty() => {
                let choice = copy_target_choices
                    .pop_front()
                    .expect("front copy target choice was present");
                apply_copied_spell_target_choice(
                    runner,
                    choice,
                    labels,
                    discard_reconciler,
                    discard_choice_groups,
                )?;
            }
            WaitingFor::TargetSelection { .. } => {
                apply_activation_target_group(
                    runner,
                    labels,
                    activation_target_groups,
                    discard_reconciler,
                    discard_choice_groups,
                )?;
            }
            WaitingFor::CopyRetarget { player, .. } if !copy_target_choices.is_empty() => {
                let choice = copy_target_choices
                    .pop_front()
                    .expect("front copy target choice was present");
                if choice.player != player {
                    return Err("copy_targets prompt was offered to a different player".to_string());
                }
                apply_copy_target_choice(
                    runner,
                    labels,
                    player,
                    CopyTargetChoice::Change(choice.targets),
                    discard_reconciler,
                    discard_choice_groups,
                )?;
            }
            WaitingFor::CopyRetarget { player, .. } => {
                let choice = take_copy_target_choice(&mut pending_copy_target_choice, player)?;
                apply_copy_target_choice(
                    runner,
                    labels,
                    player,
                    choice,
                    discard_reconciler,
                    discard_choice_groups,
                )?;
            }
            WaitingFor::UnlessPaymentChooseCost { player, costs, .. } => {
                let mut payable_indices = Vec::new();
                for (index, cost) in costs.iter().enumerate() {
                    let payability =
                        probe_unless_branch_payability(runner.state(), player, index, cost)?;
                    if std::env::var_os("PHASE_UNLESS_PROBE_TRACE").is_some() {
                        eprintln!(
                            "unless_probe scenario={scenario_name} player={player:?} index={index} cost={cost:?} result={payability:?}"
                        );
                    }
                    if matches!(payability, UnlessBranchPayability::Payable { .. }) {
                        payable_indices.push(index);
                    }
                }
                let queue = unless_choice_groups
                    .get_mut(player.0 as usize)
                    .ok_or_else(|| format!("unless choice payer {player:?} is unsupported"))?;
                let response = take_unless_script_choice_if_payable(
                    queue,
                    player,
                    &payable_indices,
                    |choice| unless_cost_index(&costs, choice),
                )?;
                match response {
                    UnlessScriptResponse::AutoDecline | UnlessScriptResponse::Decline => {
                        discard_reconciler.act(
                            runner,
                            GameAction::ChooseUnlessCostBranch {
                                choice: UnlessCostBranch::Decline,
                            },
                            discard_choice_groups,
                            labels,
                        )?;
                    }
                    UnlessScriptResponse::Pay { choice, index } => {
                        discard_reconciler.act(
                            runner,
                            GameAction::ChooseUnlessCostBranch {
                                choice: UnlessCostBranch::Pay { index },
                            },
                            discard_choice_groups,
                            labels,
                        )?;
                        pending_unless_choice = Some((player, choice));
                    }
                }
            }
            WaitingFor::WardSacrificeChoice {
                player, permanents, ..
            } => {
                let (expected_player, choice) = pending_unless_choice
                    .take()
                    .ok_or("sacrifice choice prompt has no unless_choices entry")?;
                if player != expected_player {
                    return Err(
                        "unless sacrifice choice was offered to a different player".to_string()
                    );
                }
                let UnlessScriptChoice::Sacrifice(id) = choice else {
                    return Err(
                        "unless sacrifice prompt does not match the scripted choice".to_string()
                    );
                };
                let label = labels
                    .get(&id)
                    .ok_or("unless sacrifice label is not a setup object")?;
                ensure_unless_choice_is_offered(label, id, &permanents)?;
                discard_reconciler.act(
                    runner,
                    GameAction::SelectCards { cards: vec![id] },
                    discard_choice_groups,
                    labels,
                )?;
            }
            WaitingFor::WardDiscardChoice { player, cards, .. } => {
                let (expected_player, choice) = pending_unless_choice
                    .take()
                    .ok_or("discard choice prompt has no unless_choices entry")?;
                if player != expected_player {
                    return Err(
                        "unless discard choice was offered to a different player".to_string()
                    );
                }
                let UnlessScriptChoice::Discard(id) = choice else {
                    return Err(
                        "unless discard prompt does not match the scripted choice".to_string()
                    );
                };
                let label = labels
                    .get(&id)
                    .ok_or("unless discard label is not a setup object")?;
                ensure_unless_choice_is_offered(label, id, &cards)?;
                discard_reconciler.act_for_discard_selection(
                    runner,
                    vec![id],
                    discard_choice_groups,
                    labels,
                )?;
            }
            WaitingFor::DiscardChoice {
                player,
                count,
                cards,
                up_to,
                ..
            } => {
                let selected = take_discard_choice_group(
                    discard_choice_groups,
                    player,
                    &cards,
                    count,
                    up_to,
                    labels,
                )?;
                discard_reconciler.act_for_discard_selection(
                    runner,
                    selected,
                    discard_choice_groups,
                    labels,
                )?;
            }
            WaitingFor::EffectZoneChoice { .. }
            | WaitingFor::ChooseFromZoneChoice { .. }
            | WaitingFor::DigChoice { .. } => {
                apply_card_choice_group(
                    runner,
                    labels,
                    card_choice_groups,
                    copy_cast_choices,
                    discard_reconciler,
                    discard_choice_groups,
                )?;
            }
            WaitingFor::SurveilChoice { player, cards } => {
                let choice = surveil_choices
                    .pop_front()
                    .ok_or("SurveilChoice prompt has no choose_surveil entry")?;
                if choice.player != player {
                    return Err(
                        "choose_surveil prompt was offered to a different player".to_string()
                    );
                }
                validate_card_choice_group(&choice.graveyard, &cards, 0, cards.len(), labels)?;
                let top_cards = cards
                    .iter()
                    .copied()
                    .filter(|id| !choice.graveyard.contains(id))
                    .collect();
                discard_reconciler.act(
                    runner,
                    GameAction::SelectCards { cards: top_cards },
                    discard_choice_groups,
                    labels,
                )?;
            }
            WaitingFor::OptionalEffectChoice {
                player, source_id, ..
            }
            | WaitingFor::OpponentMayChoice {
                player, source_id, ..
            } if optional_choice_groups.iter().any(|queue| !queue.is_empty()) => {
                let accept = take_optional_choice(optional_choice_groups, player, Some(source_id))?;
                discard_reconciler.act(
                    runner,
                    GameAction::DecideOptionalEffect { accept },
                    discard_choice_groups,
                    labels,
                )?;
            }
            WaitingFor::OptionalEffectChoice {
                player, source_id, ..
            } if runner
                .state()
                .objects
                .get(&source_id)
                .is_some_and(|object| object.name == "Firebender Ascension") =>
            {
                let choice = take_copy_choice(copy_choices, player)?;
                if !choice.accept && choice.new_targets.is_some() {
                    return Err(
                        "copy_choice.new_targets is only valid when accepting the copy".to_string(),
                    );
                }
                discard_reconciler.act(
                    runner,
                    GameAction::DecideOptionalEffect {
                        accept: choice.accept,
                    },
                    discard_choice_groups,
                    labels,
                )?;
                if let Some(targets) = choice.new_targets {
                    if pending_copy_target_choice.is_some() {
                        return Err("a copy target choice is already pending".to_string());
                    }
                    pending_copy_target_choice = Some((player, targets));
                }
            }
            WaitingFor::OptionalEffectChoice {
                player, source_id, ..
            } if runner
                .state()
                .objects
                .get(&source_id)
                .is_some_and(|object| object.name == "Extrapolate the Impossible") =>
            {
                let choice = outside_game_choices
                    .front()
                    .ok_or("Extrapolate optional prompt has no choose_outside_game entry")?;
                if choice.player != player {
                    return Err(
                        "choose_outside_game optional choice was offered to a different player"
                            .to_string(),
                    );
                }
                let accept = choice.accept;
                if !accept || choice.labels.is_empty() {
                    outside_game_choices.pop_front();
                }
                discard_reconciler.act(
                    runner,
                    GameAction::DecideOptionalEffect { accept },
                    discard_choice_groups,
                    labels,
                )?;
            }
            WaitingFor::OptionalEffectChoice { .. } if !copy_choices.is_empty() => {
                return Err(
                    "copy_choice was queued but an optional prompt from another source arrived"
                        .to_string(),
                );
            }
            optional @ (WaitingFor::OptionalEffectChoice { .. }
            | WaitingFor::OpponentMayChoice { .. }) => {
                return Err(format!("unscripted optional prompt: {optional:?}"));
            }
            WaitingFor::OutsideGameChoice {
                player,
                choices,
                count,
                stage,
                ..
            } => {
                let choice = outside_game_choices
                    .pop_front()
                    .ok_or("outside-game choice prompt has no choose_outside_game entry")?;
                if !choice.accept || choice.labels.is_empty() {
                    return Err(
                        "outside-game card choice requires an accepted, non-empty script entry"
                            .to_string(),
                    );
                }
                if choice.player != player {
                    return Err(
                        "choose_outside_game choice was offered to a different player".to_string(),
                    );
                }
                let owner = match stage {
                    OutsideGameChoiceStage::ControllerSelects
                    | OutsideGameChoiceStage::RevealPairThenOpponentSelects => player,
                    OutsideGameChoiceStage::OpponentSelects { owner }
                    | OutsideGameChoiceStage::ControllerChoosesOpponent { owner } => owner,
                };
                if matches!(
                    stage,
                    OutsideGameChoiceStage::ControllerChoosesOpponent { .. }
                ) {
                    return Err(
                        "choose_outside_game cannot select an opponent for a revealed pair"
                            .to_string(),
                    );
                }
                let selections =
                    sideboard_choice_indices(&choice, sideboard_labels, owner, &choices)?;
                if selections.len() != count {
                    return Err(format!(
                        "choose_outside_game selected {} cards but the prompt requires {count}",
                        selections.len()
                    ));
                }
                discard_reconciler.act(
                    runner,
                    GameAction::ChooseOutsideGameCards { selections },
                    discard_choice_groups,
                    labels,
                )?;
            }
            WaitingFor::Priority { .. } if fail_closed_unscripted_prompts => {
                discard_reconciler.act(
                    runner,
                    GameAction::PassPriority,
                    discard_choice_groups,
                    labels,
                )?;
            }
            WaitingFor::Priority { .. } => {
                discard_reconciler.advance_until_stack_empty(
                    runner,
                    discard_choice_groups,
                    labels,
                )?;
            }
            other if fail_closed_unscripted_prompts => {
                return Err(format!(
                    "combat progression encountered an unscripted prompt: {other:?}"
                ));
            }
            _ => {
                discard_reconciler.advance_until_stack_empty(
                    runner,
                    discard_choice_groups,
                    labels,
                )?;
            }
        }
        if runner.state().stack.is_empty()
            && !matches!(
                runner.state().waiting_for,
                WaitingFor::TriggerTargetSelection { .. }
                    | WaitingFor::TargetSelection { .. }
                    | WaitingFor::SurveilChoice { .. }
                    | WaitingFor::EffectZoneChoice { .. }
                    | WaitingFor::ChooseFromZoneChoice { .. }
                    | WaitingFor::DigChoice { .. }
                    | WaitingFor::DiscardChoice { .. }
                    | WaitingFor::OptionalEffectChoice { .. }
                    | WaitingFor::OutsideGameChoice { .. }
                    | WaitingFor::CopyRetarget { .. }
                    | WaitingFor::UnlessPaymentChooseCost { .. }
                    | WaitingFor::WardSacrificeChoice { .. }
                    | WaitingFor::WardDiscardChoice { .. }
            )
            && (!fail_closed_unscripted_prompts
                || matches!(runner.state().waiting_for, WaitingFor::Priority { .. }))
        {
            if pending_unless_choice.is_some() {
                return Err("unless cost branch was not followed by its choice prompt".to_string());
            }
            ensure_copy_target_choice_consumed(&pending_copy_target_choice)?;
            ensure_copy_cast_choices_consumed(copy_cast_choices)?;
            ensure_target_choices_consumed("graveyard_targets", graveyard_target_choices)?;
            ensure_target_choices_consumed("copy_targets", copy_target_choices)?;
            return Ok(());
        }
    }
    Err(format!(
        "stack did not resolve within 128 actions; waiting_for={:?}",
        runner.state().waiting_for
    ))
}

#[allow(clippy::too_many_arguments)]
fn activate(
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    action: &Value,
    action_index: usize,
    expect_refused: bool,
    refusals: &mut Vec<usize>,
    activation_target_groups: &mut VecDeque<Vec<ObjectId>>,
    mana_choice_groups: &mut VecDeque<ManaScriptChoice>,
    spec: &Value,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    let source_label = action["source"].as_str().ok_or("activate needs source")?;
    let source_id = resolve_label(labels, source_label)?;
    let prefix = action["ability"].as_str().ok_or("activate needs ability")?;
    let object = runner
        .state()
        .objects
        .get(&source_id)
        .ok_or_else(|| format!("source label {source_label} is not on the battlefield"))?;
    let WaitingFor::Priority { player } = &runner.state().waiting_for else {
        return Err("activate requires a player with priority".to_string());
    };
    let activation_player = if object.zone == Zone::Graveyard {
        object.owner
    } else {
        object.controller
    };
    if *player != activation_player {
        return Err(format!(
            "activate source label {source_label} is not controlled by the player with priority"
        ));
    }
    if expect_refused {
        activation_source_zone_precondition(object.zone, source_label)?;
    }
    let activation_player = *player;
    let mana_choice = if expect_refused {
        None
    } else {
        consume_mana_choice(mana_choice_groups, activation_player, spec)?
    };
    let mana_pool_before = mana_choice
        .as_ref()
        .map(|_| mana_pool_source_pips(runner.state(), activation_player))
        .transpose()?;
    let ability_index = select_activation_ability(
        object.abilities.iter().enumerate().map(|(index, ability)| {
            (
                index,
                ability.kind,
                is_mana_ability(ability),
                ability.cost.as_ref().and_then(loyalty_ability_label),
                ability.description.clone(),
            )
        }),
        prefix,
    )?;
    let alternative_cost = alternative_cost_decision(action)?;
    let base_cost = object.abilities[ability_index]
        .cost
        .as_ref()
        .and_then(base_activation_cost)
        .cloned();
    let target_ids = resolve_target_labels(action, labels)?;
    let stack_size = runner.state().stack.len();
    let activation = GameAction::ActivateAbility {
        source_id,
        ability_index,
        payment_mode: engine::types::game_state::CastPaymentMode::Auto,
    };
    let before = expect_refused.then(|| project(runner.state(), labels, None, true));
    let offered = expect_refused
        && engine::ai_support::legal_actions(runner.state())
            .iter()
            .any(|candidate| {
                matches!(
                    candidate,
                    GameAction::ActivateAbility {
                        source_id: candidate_source,
                        ability_index: candidate_index,
                        ..
                    } if *candidate_source == source_id && *candidate_index == ability_index
                )
            });
    if expect_refused {
        if let Some(mode) = classify_refusal(offered, false) {
            let after = project(runner.state(), labels, None, true);
            return record_refusal(
                action_index,
                "activate",
                mode,
                before.as_ref().expect("refusal projection was captured"),
                &after,
                refusals,
            );
        }
    }
    if expect_refused {
        let mut attempt = refusal_attempt_runner(runner);
        match attempt.act(activation) {
            Ok(_) if !matches!(attempt.state().waiting_for, WaitingFor::Priority { .. }) => {
                return Err(refused_attempt_prompt_error(
                    "activation",
                    &attempt.state().waiting_for,
                )
                .to_string());
            }
            Ok(_) => return Err("expected refusal but activate was accepted".to_string()),
            Err(error) => {
                let classified_error = ClassifiedActError::Engine(error.to_string());
                let Some(mode) = refusal_mode_for_action_error(true, &classified_error) else {
                    return Err(classified_error.to_string());
                };
                let after = project(attempt.state(), labels, None, true);
                record_refusal(
                    action_index,
                    "activate",
                    mode,
                    before.as_ref().expect("refusal projection was captured"),
                    &after,
                    refusals,
                )?;
                return Ok(());
            }
        }
    }
    let act_result = discard_reconciler
        .act(runner, activation, discard_choice_groups, labels)
        .map_err(ClassifiedActError::Harness);
    if let Err(error) = act_result {
        return Err(error.to_string());
    }

    let mut target_index = 0;
    let mut alternative_cost_prompt_seen = false;
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
                discard_reconciler.act(
                    runner,
                    GameAction::ChooseTarget { target },
                    discard_choice_groups,
                    labels,
                )?;
                if selected {
                    target_index += 1;
                }
            }
            WaitingFor::ActivationCostOneOfChoice { costs, .. } => {
                if alternative_cost_prompt_seen {
                    return Err(
                        "activated ability presented multiple alternative-cost prompts".to_string(),
                    );
                }
                let decision = alternative_cost.ok_or_else(|| {
                    "an alternative-cost prompt appeared without activate.alternative_cost"
                        .to_string()
                })?;
                let index = select_activation_cost_branch(&costs, base_cost.as_ref(), decision)?;
                discard_reconciler.act(
                    runner,
                    GameAction::ChooseActivationCostBranch { index },
                    discard_choice_groups,
                    labels,
                )?;
                alternative_cost_prompt_seen = true;
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
    if !matches!(&runner.state().waiting_for, WaitingFor::Priority { .. }) {
        return Err("activation did not finish target selection".to_string());
    }
    if target_index < target_ids.len() {
        let expected_targets = target_ids
            .iter()
            .copied()
            .map(TargetRef::Object)
            .collect::<Vec<_>>();
        let targets_were_selected_implicitly = runner
            .state()
            .stack
            .iter()
            .rev()
            .find(|entry| entry.source_id == source_id && entry.controller == activation_player)
            .and_then(|entry| entry.ability())
            .is_some_and(|ability| ability.targets == expected_targets);
        if !targets_were_selected_implicitly {
            activation_target_groups.push_back(target_ids[target_index..].to_vec());
        }
    }
    ensure_activation_cost_prompt_matches_field(alternative_cost, alternative_cost_prompt_seen)?;
    if runner.state().stack.len() <= stack_size {
        return Err("activated ability did not commit to the stack".to_string());
    }
    if let (Some(choice), Some(before)) = (&mana_choice, &mana_pool_before) {
        verify_mana_choice_spent(
            choice,
            before,
            &mana_pool_source_pips(runner.state(), activation_player)?,
            labels,
        )?;
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
            if ![
                "life",
                "battlefield",
                "hand",
                "library_top",
                "graveyard",
                "sideboard",
                "mana_pool",
                "exile",
            ]
            .contains(&field.as_str())
            {
                return Err(format!("setup.{player}.{field} is unsupported"));
            }
        }
        if let Some(value) = player_setup.get("life") {
            let life = value
                .as_i64()
                .ok_or_else(|| format!("setup.{player}.life must be a 32-bit integer"))?;
            i32::try_from(life)
                .map_err(|_| format!("setup.{player}.life must be a 32-bit integer"))?;
        }
        for field in ["hand", "library_top", "graveyard", "sideboard", "exile"] {
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
                    if label.starts_with('@') {
                        return Err(format!("setup label {label} uses reserved @ prefix"));
                    }
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
                if label.starts_with('@') {
                    return Err(format!("setup label {label} uses reserved @ prefix"));
                }
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
        if let Some(value) = player_setup.get("mana_pool") {
            let mana_pool = value
                .as_array()
                .ok_or_else(|| format!("setup.{player}.mana_pool must be an array"))?;
            let battlefield_labels = player_setup
                .get("battlefield")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|permanent| permanent["label"].as_str())
                .collect::<HashSet<_>>();
            for (index, unit) in mana_pool.iter().enumerate() {
                let context = format!("setup.{player}.mana_pool[{index}]");
                let unit = unit
                    .as_object()
                    .ok_or_else(|| format!("{context} needs color and source"))?;
                if unit.len() != 2 || !unit.contains_key("color") || !unit.contains_key("source") {
                    return Err(format!("{context} needs color and source"));
                }
                if !matches!(
                    unit["color"].as_str(),
                    Some("W" | "U" | "B" | "R" | "G" | "C")
                ) {
                    return Err(format!("{context}.color must be W, U, B, R, G, or C"));
                }
                if !unit["source"]
                    .as_str()
                    .is_some_and(|source| battlefield_labels.contains(source))
                {
                    return Err(format!(
                        "{context}.source must name a card on {player}'s battlefield"
                    ));
                }
            }
        }
    }
    Ok(())
}

fn pass_priority(
    runner: &mut GameRunner,
    action: &Value,
    labels: &HashMap<ObjectId, String>,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    let player = parse_script_player(action["player"].as_str(), "pass_priority")?;
    match &runner.state().waiting_for {
        WaitingFor::Priority { player: current } if *current == player => {}
        WaitingFor::Priority { player: current } => {
            return Err(format!(
                "pass_priority was scripted for {player:?} while {current:?} has priority"
            ));
        }
        other => return Err(format!("pass_priority cannot answer {other:?}")),
    }
    discard_reconciler.act(
        runner,
        GameAction::PassPriority,
        discard_choice_groups,
        labels,
    )
}

fn label_permanent(
    runner: &GameRunner,
    labels: &mut HashMap<ObjectId, String>,
    action: &Value,
) -> Result<(), String> {
    let player = parse_script_player(action["player"].as_str(), "label_permanent")?;
    let card = action["card"]
        .as_str()
        .filter(|card| !card.is_empty())
        .ok_or("label_permanent needs card")?;
    let label = action["label"]
        .as_str()
        .filter(|label| !label.is_empty() && !label.starts_with('@'))
        .ok_or("label_permanent needs a non-empty, non-reserved label")?;
    if labels.values().any(|existing| existing == label) {
        return Err(format!("duplicate script label {label}"));
    }
    let token = action["token"]
        .as_bool()
        .ok_or("label_permanent.token must be a boolean")?;
    let mut matches = runner.state().battlefield.iter().copied().filter(|id| {
        runner.state().objects.get(id).is_some_and(|object| {
            object.controller == player && object.name == card && object.is_token == token
        })
    });
    let object = matches
        .next()
        .ok_or_else(|| format!("no matching {card} permanent for {player:?}"))?;
    if matches.next().is_some() {
        return Err(format!(
            "multiple matching {card} permanents for {player:?}; cannot assign {label}"
        ));
    }
    ensure_permanent_unlabeled(labels, object, label)?;
    labels.insert(object, label.to_string());
    Ok(())
}

fn ensure_permanent_unlabeled(
    labels: &HashMap<ObjectId, String>,
    object: ObjectId,
    label: &str,
) -> Result<(), String> {
    if let Some(old_label) = labels.get(&object) {
        return Err(format!(
            "permanent already labeled {old_label}; cannot assign {label}"
        ));
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn advance_through_combat_damage(
    scenario_name: &str,
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    groups: &mut VecDeque<TriggerTargetGroup>,
    activation_target_groups: &mut VecDeque<Vec<ObjectId>>,
    card_choice_groups: &mut VecDeque<Vec<ObjectId>>,
    copy_cast_choices: &mut VecDeque<CopyCastChoice>,
    graveyard_target_choices: &mut VecDeque<TargetScriptChoice>,
    copy_target_choices: &mut VecDeque<TargetScriptChoice>,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
    surveil_choices: &mut VecDeque<SurveilScriptChoice>,
    sideboard_labels: &HashMap<String, (PlayerId, usize, String)>,
    outside_game_choices: &mut VecDeque<OutsideGameScriptChoice>,
    copy_choices: &mut VecDeque<CopyChoice>,
    optional_choice_groups: &mut [VecDeque<OptionalChoice>; 2],
    unless_choice_groups: &mut [VecDeque<UnlessScriptChoice>; 2],
    discard_reconciler: &mut DiscardChoiceReconciler,
) -> Result<(), String> {
    let active_player = runner.state().active_player;
    for _ in 0..128 {
        let (phase, stack_is_empty, regular_damage_done, waiting_for) = {
            let state = runner.state();
            (
                state.phase,
                state.stack.is_empty(),
                state
                    .combat
                    .as_ref()
                    .is_some_and(|combat| combat.regular_damage_done),
                state.waiting_for.clone(),
            )
        };
        if phase == Phase::CombatDamage
            && stack_is_empty
            && regular_damage_done
            && matches!(
                &waiting_for,
                WaitingFor::Priority { player } if *player == active_player
            )
        {
            return Ok(());
        }
        match waiting_for {
            WaitingFor::Priority { .. } if !stack_is_empty => {
                pass_until_resolved(
                    scenario_name,
                    runner,
                    labels,
                    groups,
                    activation_target_groups,
                    card_choice_groups,
                    copy_cast_choices,
                    graveyard_target_choices,
                    copy_target_choices,
                    discard_choice_groups,
                    surveil_choices,
                    sideboard_labels,
                    outside_game_choices,
                    copy_choices,
                    optional_choice_groups,
                    unless_choice_groups,
                    discard_reconciler,
                    true,
                )?;
            }
            WaitingFor::Priority { .. } => {
                discard_reconciler.act(
                    runner,
                    GameAction::PassPriority,
                    discard_choice_groups,
                    labels,
                )?;
            }
            WaitingFor::DeclareBlockers {
                player: PlayerId(1),
                ..
            } => {
                discard_reconciler.act(
                    runner,
                    GameAction::DeclareBlockers {
                        assignments: Vec::new(),
                    },
                    discard_choice_groups,
                    labels,
                )?;
            }
            _ => {
                pass_until_resolved(
                    scenario_name,
                    runner,
                    labels,
                    groups,
                    activation_target_groups,
                    card_choice_groups,
                    copy_cast_choices,
                    graveyard_target_choices,
                    copy_target_choices,
                    discard_choice_groups,
                    surveil_choices,
                    sideboard_labels,
                    outside_game_choices,
                    copy_choices,
                    optional_choice_groups,
                    unless_choice_groups,
                    discard_reconciler,
                    true,
                )?;
            }
        }
    }
    Err(format!(
        "combat did not reach post-damage priority within 128 actions; phase={:?}, waiting_for={:?}",
        runner.state().phase,
        runner.state().waiting_for
    ))
}

fn advance_to_next_turn(
    runner: &mut GameRunner,
    continue_to_precombat_main: bool,
    labels: &HashMap<ObjectId, String>,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
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
            discard_reconciler.advance_until_stack_empty(runner, discard_choice_groups, labels)?;
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
        discard_reconciler.act(runner, action, discard_choice_groups, labels)?;
    }
    Err("turn did not advance within 128 actions".to_string())
}

fn behold_filter_in_cost(cost: &AbilityCost) -> Option<&TargetFilter> {
    let mut filter = None;
    cost.for_each_cost_node(&mut |node| {
        if let AbilityCost::Behold { filter: found, .. } = node {
            filter = Some(found);
        }
    });
    filter
}

fn behold_filter_in_ability(ability: &AbilityDefinition) -> Option<&TargetFilter> {
    if let Effect::Behold { filter } = ability.effect.as_ref() {
        return Some(filter);
    }
    if let Some(filter) = ability.cost.as_ref().and_then(behold_filter_in_cost) {
        return Some(filter);
    }
    ability
        .sub_ability
        .as_deref()
        .and_then(behold_filter_in_ability)
        .or_else(|| {
            ability
                .else_ability
                .as_deref()
                .and_then(behold_filter_in_ability)
        })
}

fn behold_filter_in_replacement(definition: &ReplacementDefinition) -> Option<&TargetFilter> {
    let cost_filter = match &definition.mode {
        ReplacementMode::MayCost { cost, .. } => behold_filter_in_cost(cost),
        _ => None,
    };
    cost_filter.or_else(|| {
        definition
            .execute
            .as_deref()
            .and_then(behold_filter_in_ability)
    })
}

fn behold_filter_for_land(state: &GameState, source: ObjectId) -> Option<TargetFilter> {
    let object = state.objects.get(&source)?;
    object
        .replacement_definitions
        .iter_all()
        .chain(object.base_replacement_definitions.iter())
        .find_map(behold_filter_in_replacement)
        .cloned()
}

fn is_behold_replacement_prompt(
    waiting_for: &WaitingFor,
    land_id: ObjectId,
    has_behold: bool,
) -> bool {
    if !has_behold {
        return false;
    }
    match waiting_for {
        WaitingFor::ReplacementChoice {
            candidate_count,
            candidates,
            kind,
            ..
        } => {
            *candidate_count == 2
                && *kind == engine::types::game_state::ReplacementChoiceKind::OptionalBranch
                && candidates.len() == 2
                && candidates
                    .iter()
                    .all(|candidate| candidate.source_id == land_id)
        }
        _ => false,
    }
}

fn behold_candidates(
    state: &GameState,
    player: PlayerId,
    source: ObjectId,
    filter: &TargetFilter,
) -> Vec<ObjectId> {
    let context = FilterContext::from_source_with_controller(source, player);
    let mut candidates = state
        .battlefield
        .iter()
        .copied()
        .filter(|id| {
            *id != source
                && state
                    .objects
                    .get(id)
                    .is_some_and(|object| object.controller == player)
                && matches_target_filter(state, *id, filter, &context)
        })
        .collect::<Vec<_>>();
    if let Some(player_state) = state.players.get(player.0 as usize) {
        candidates.extend(
            player_state
                .hand
                .iter()
                .copied()
                .filter(|id| *id != source && matches_target_filter(state, *id, filter, &context)),
        );
    }
    candidates
}

fn ensure_behold_prompt_matches_field(
    has_behold_field: bool,
    prompt_offered: bool,
) -> Result<(), String> {
    match (has_behold_field, prompt_offered) {
        (true, false) => {
            Err("play_land.behold was provided but no behold prompt was offered".to_string())
        }
        (false, true) => {
            Err("play_land behold prompt was offered without a behold field".to_string())
        }
        _ => Ok(()),
    }
}

fn ensure_behold_selection_is_offered(
    label: &str,
    selected: ObjectId,
    offered: &[ObjectId],
) -> Result<(), String> {
    if offered.contains(&selected) {
        Ok(())
    } else {
        Err(format!(
            "behold label {label} is not among the offered candidates"
        ))
    }
}

fn play_land(
    runner: &mut GameRunner,
    labels: &HashMap<ObjectId, String>,
    action: &Value,
    discard_reconciler: &mut DiscardChoiceReconciler,
    discard_choice_groups: &mut [VecDeque<DiscardScriptChoice>; 2],
) -> Result<(), String> {
    let card_label = action["card"]
        .as_str()
        .filter(|label| !label.is_empty())
        .ok_or("play_land needs a hand card label")?;
    let land_id = resolve_label(labels, card_label)
        .map_err(|_| format!("unknown hand label {card_label}"))?;
    let player = runner.state().active_player;
    if !runner.state().players[player.0 as usize]
        .hand
        .contains(&land_id)
    {
        return Err(format!(
            "play_land card label {card_label} is not in the active player's hand"
        ));
    }
    let card_id = runner
        .state()
        .objects
        .get(&land_id)
        .ok_or_else(|| format!("play_land card label {card_label} has no object"))?
        .card_id;
    let behold_filter = behold_filter_for_land(runner.state(), land_id);
    let behold = action
        .get("behold")
        .map(|value| {
            value
                .as_str()
                .filter(|label| !label.is_empty())
                .ok_or("play_land.behold must be a label or decline")
        })
        .transpose()?;

    discard_reconciler.act(
        runner,
        GameAction::PlayLand {
            object_id: land_id,
            card_id,
        },
        discard_choice_groups,
        labels,
    )?;

    let chooser = match &runner.state().waiting_for {
        WaitingFor::ReplacementChoice { player, .. }
            if is_behold_replacement_prompt(
                &runner.state().waiting_for,
                land_id,
                behold_filter.is_some(),
            ) =>
        {
            Some(*player)
        }
        WaitingFor::ReplacementChoice { .. } => {
            return Err("unexpected replacement prompt".to_string());
        }
        _ => None,
    };
    ensure_behold_prompt_matches_field(behold.is_some(), chooser.is_some())?;
    let Some(chooser) = chooser else {
        return Ok(());
    };
    if chooser != player {
        return Err("play_land behold prompt was offered to a different player".to_string());
    }
    let Some(behold) = behold else {
        return Err("play_land behold prompt was offered without a behold field".to_string());
    };

    let decline = behold == "decline";
    discard_reconciler.act(
        runner,
        GameAction::ChooseReplacement {
            index: if decline { 1 } else { 0 },
        },
        discard_choice_groups,
        labels,
    )?;

    if decline {
        if matches!(runner.state().waiting_for, WaitingFor::BeholdChoice { .. }) {
            return Err("declining behold unexpectedly offered a candidate choice".to_string());
        }
        return Ok(());
    }

    let selected_id =
        resolve_label(labels, behold).map_err(|_| format!("unknown behold label {behold}"))?;
    match runner.state().waiting_for.clone() {
        WaitingFor::BeholdChoice {
            player: chooser,
            choices,
        } => {
            if chooser != player {
                return Err("behold candidate choice was offered to a different player".to_string());
            }
            ensure_behold_selection_is_offered(behold, selected_id, &choices)?;
            discard_reconciler.act(
                runner,
                GameAction::SelectCards {
                    cards: vec![selected_id],
                },
                discard_choice_groups,
                labels,
            )?;
        }
        WaitingFor::Priority { player: priority } => {
            let filter = behold_filter.ok_or_else(|| {
                "behold auto-collapse could not be validated because the land has no Behold filter"
                    .to_string()
            })?;
            let candidates = behold_candidates(runner.state(), player, land_id, &filter);
            if candidates.len() != 1
                || ensure_behold_selection_is_offered(behold, selected_id, &candidates).is_err()
            {
                return Err(format!(
                    "behold auto-collapse did not select the sole candidate labeled {behold}"
                ));
            }
            if priority != player {
                return Err(
                    "behold auto-collapse returned priority to a different player".to_string(),
                );
            }
        }
        other => {
            return Err(format!(
                "behold did not reach a candidate choice or single-candidate auto-collapse: {other:?}"
            ));
        }
    }
    Ok(())
}

fn run_one(spec: &Value, db: &CardDatabase) -> Result<Value, String> {
    validate_setup(spec)?;
    let name = spec["name"].as_str().ok_or("scenario name is required")?;
    let active_index = spec["active_player"].as_u64().unwrap_or(0);
    if active_index > 1 {
        return Err("active_player must be 0 or 1".to_string());
    }
    let active = PlayerId(active_index as u8);
    let priority_index = match spec.get("priority_player") {
        None => active_index,
        Some(value) => value.as_u64().ok_or("priority_player must be 0 or 1")?,
    };
    if priority_index > 1 {
        return Err("priority_player must be 0 or 1".to_string());
    }
    let priority = PlayerId(priority_index as u8);
    let step = spec["step"].as_str().unwrap_or("precombat_main");
    let mut scenario = GameScenario::new();
    scenario.at_phase(match step {
        "precombat_main" => Phase::PreCombatMain,
        "postcombat_main" => Phase::PostCombatMain,
        _ => return Err(format!("unsupported step {step}")),
    });
    let mut labels = HashMap::<ObjectId, String>::new();
    let mut sideboard_labels = HashMap::<String, (PlayerId, usize, String)>::new();
    let mut configured = Vec::new();
    let mut library_top = [Vec::new(), Vec::new()];
    let mut sideboards: [Vec<DeckEntry>; 2] = std::array::from_fn(|_| Vec::new());
    for (index, player_library_top) in library_top.iter_mut().enumerate() {
        let key = format!("P{index}");
        let setup = &spec["setup"][&key];
        if let Some(items) = setup["sideboard"].as_array() {
            for item in items {
                let card = item
                    .as_str()
                    .or_else(|| item["card"].as_str())
                    .ok_or_else(|| format!("{key}.sideboard needs card names"))?;
                let sideboard_index = sideboards[index].len();
                sideboards[index].push(sideboard_deck_entry(card, db)?);
                if let Some(label) = item["label"].as_str() {
                    sideboard_labels.insert(
                        label.to_string(),
                        (PlayerId(index as u8), sideboard_index, card.to_string()),
                    );
                }
            }
        }
        for (field, zone) in [
            ("battlefield", Zone::Battlefield),
            ("hand", Zone::Hand),
            ("library_top", Zone::Library),
            ("graveyard", Zone::Graveyard),
            ("exile", Zone::Exile),
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
        if let Some(units) = setup["mana_pool"].as_array() {
            let mana = units
                .iter()
                .map(|unit| {
                    let color = match unit["color"].as_str() {
                        Some("W") => ManaType::White,
                        Some("U") => ManaType::Blue,
                        Some("B") => ManaType::Black,
                        Some("R") => ManaType::Red,
                        Some("G") => ManaType::Green,
                        Some("C") => ManaType::Colorless,
                        _ => return Err("unsupported mana_pool color".to_string()),
                    };
                    let source_label = unit["source"]
                        .as_str()
                        .ok_or_else(|| "mana_pool source must be a label".to_string())?;
                    let source_id = labels
                        .iter()
                        .find_map(|(id, label)| (label == source_label).then_some(*id))
                        .ok_or_else(|| format!("unknown mana_pool source {source_label}"))?;
                    Ok(ManaUnit::new(color, source_id, false, vec![]))
                })
                .collect::<Result<Vec<_>, String>>()?;
            scenario.with_mana_pool(PlayerId(index as u8), mana);
        }
    }
    let mut runner = scenario.build();
    if sideboards.iter().any(|sideboard| !sideboard.is_empty()) {
        runner.state_mut().deck_pools = sideboards
            .into_iter()
            .enumerate()
            .filter(|(_, sideboard)| !sideboard.is_empty())
            .map(|(index, sideboard)| PlayerDeckPool {
                player: PlayerId(index as u8),
                current_sideboard: Arc::new(sideboard),
                ..Default::default()
            })
            .collect();
    }
    {
        let state = runner.state_mut();
        state.active_player = active;
        state.priority_player = priority;
        state.waiting_for = WaitingFor::Priority { player: priority };
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
                TargetRef::Player(player) => Ok(format!("@P{}", player.0)),
            })
            .collect::<Result<Vec<_>, _>>()?;
        targets.sort();
        legal = Some(targets);
    }
    let actions = spec["script"].as_array().cloned().unwrap_or_default();
    ensure_mana_choices_not_refused(&actions)?;
    let mut trigger_target_groups = VecDeque::new();
    let mut activation_target_groups = VecDeque::new();
    let mut card_choice_groups = VecDeque::new();
    let mut copy_cast_choices = VecDeque::new();
    let mut graveyard_target_choices = VecDeque::new();
    let mut copy_target_choices = VecDeque::new();
    let mut discard_choice_groups = [VecDeque::new(), VecDeque::new()];
    let mut surveil_choices = VecDeque::new();
    let mut outside_game_choices = VecDeque::new();
    let mut mana_choice_groups = VecDeque::new();
    let mut copy_choices = VecDeque::new();
    let mut optional_choice_groups = [VecDeque::new(), VecDeque::new()];
    let mut unless_choice_groups = [VecDeque::new(), VecDeque::new()];
    let mut discard_reconciler = DiscardChoiceReconciler::default();
    let mut attack_needs_resolution = false;
    let mut attack_through_damage = false;
    let mut refusals = Vec::new();
    let mut refusal_cost_prompts = Vec::new();
    for (action_index, action) in actions.iter().enumerate() {
        let expect_refused = script_expected_refusal(action, action_index)?;
        validate_script_cast_from(action)?;
        let sacrifice_objects = cast_sacrifice_labels(action, &labels)?;
        if action.get("choices").is_some()
            && action.get("action").and_then(Value::as_str) != Some("unless_choices")
        {
            return Err("script.choices is only valid for unless_choices actions".to_string());
        }
        if action.get("action").and_then(Value::as_str) == Some("unless_choices") {
            parse_unless_script_choices(action, &labels)?;
        }
        match action["action"].as_str().unwrap_or_default() {
            "unless_choices" => {
                let (player, choices) = parse_unless_script_choices(action, &labels)?;
                let queue = unless_choice_groups
                    .get_mut(player.0 as usize)
                    .ok_or("unless_choices.player must be P0 or P1")?;
                queue.extend(choices);
            }
            "play_land" => play_land(
                &mut runner,
                &labels,
                action,
                &mut discard_reconciler,
                &mut discard_choice_groups,
            )?,
            "cast" => {
                ensure_targeted_cast_has_no_pending_trigger_targets(
                    action,
                    &trigger_target_groups,
                )?;
                let card = action["card"].as_str().ok_or("cast needs card")?;
                let player = match action.get("player") {
                    None => runner.state().active_player,
                    Some(value) => parse_script_player(value.as_str(), "cast")?,
                };
                let cast_source = cast_from(action)?;
                if runner.state().priority_player != player {
                    return Err(format!("P{} does not have priority to cast", player.0));
                }
                let mana_choice = if expect_refused {
                    None
                } else {
                    consume_mana_choice(&mut mana_choice_groups, player, spec)?
                };
                let spell = if cast_source == "hand" {
                    let matches = runner.state().players[player.0 as usize]
                        .hand
                        .iter()
                        .copied()
                        .filter(|id| {
                            runner
                                .state()
                                .objects
                                .get(id)
                                .is_some_and(|object| object.name == card)
                        })
                        .collect::<Vec<_>>();
                    matches
                        .first()
                        .copied()
                        .ok_or_else(|| format!("{card} is not in the active player's hand"))?
                } else {
                    let matching_labels = labels
                        .iter()
                        .filter_map(|(id, label)| (label == card).then_some(*id))
                        .collect::<Vec<_>>();
                    if expect_refused && matching_labels.len() != 1 {
                        return Err(format!(
                            "{cast_source} label {card} must resolve to exactly one card"
                        ));
                    }
                    let spell = matching_labels
                        .first()
                        .copied()
                        .ok_or_else(|| format!("unknown {cast_source} label {card}"))?;
                    let player_state = &runner.state().players[player.0 as usize];
                    let source_contains_spell = match cast_source {
                        "graveyard" => player_state.graveyard.contains(&spell),
                        "exile" => runner.state().exile.contains(&spell),
                        _ => unreachable!("hand casts are handled above"),
                    };
                    if !source_contains_spell {
                        return Err(format!(
                            "{card} is not in the active player's {cast_source}"
                        ));
                    }
                    spell
                };
                let offered = (expect_refused || cast_source != "hand")
                    && engine::ai_support::legal_actions(runner.state())
                        .iter()
                        .any(|candidate| {
                            matches!(
                                candidate,
                                GameAction::CastSpell { object_id, .. } if *object_id == spell
                            )
                        });
                if !expect_refused && cast_source != "hand" && !offered {
                    return Err(format!("{card} is not offered as a {cast_source} cast"));
                }
                let sacrifice_prompt = if expect_refused {
                    None
                } else {
                    probe_cast_sacrifice_prompt(&runner, spell)?
                };
                ensure_cast_sacrifice_prompt_decision(
                    sacrifice_objects.as_deref(),
                    sacrifice_prompt,
                )?;
                let has_x_cost = match &runner.state().objects[&spell].mana_cost {
                    engine::types::mana::ManaCost::Cost { shards, .. } => {
                        shards.contains(&engine::types::mana::ManaCostShard::X)
                    }
                    _ => false,
                };
                let x = cast_x(action, has_x_cost)?;
                let target_slots = resolve_cast_target_slots(action, &labels)?;
                let target_decision_slot_count = if cast_target_decision_slot_count_needed(
                    action.get("targets").is_some(),
                    expect_refused,
                ) {
                    Some(cast_target_decision_slot_count(
                        runner.state(),
                        spell,
                        player,
                        x,
                    )?)
                } else {
                    None
                };
                let before = expect_refused.then(|| project(runner.state(), &labels, None, true));
                if expect_refused {
                    if let Some(mode) = classify_refusal(offered, false) {
                        let after = project(runner.state(), &labels, None, true);
                        record_refusal(
                            action_index,
                            "cast",
                            mode,
                            before.as_ref().expect("refusal projection was captured"),
                            &after,
                            &mut refusals,
                        )?;
                        continue;
                    }

                    let mut attempt = refusal_attempt_runner(&runner);
                    match attempt_refused_cast(
                        &mut attempt,
                        spell,
                        player,
                        action_index,
                        &target_slots,
                        &mut refusal_cost_prompts,
                    ) {
                        Ok(()) => {
                            return Err("expected refusal but cast was accepted".to_string());
                        }
                        Err(ClassifiedActError::Harness(error)) => return Err(error),
                        Err(error @ ClassifiedActError::Engine(_)) => {
                            let classified_error = error;
                            let Some(mode) = refusal_mode_for_action_error(true, &classified_error)
                            else {
                                return Err(classified_error.to_string());
                            };
                            let after = project(attempt.state(), &labels, None, true);
                            record_refusal(
                                action_index,
                                "cast",
                                mode,
                                before.as_ref().expect("refusal projection was captured"),
                                &after,
                                &mut refusals,
                            )?;
                            continue;
                        }
                    }
                }
                let mana_pool_before = if mana_choice.is_some() {
                    Some(mana_pool_source_pips(runner.state(), player)?)
                } else {
                    None
                };
                let cast = runner.cast(spell);
                let cast = if let Some(x) = x { cast.x(x) } else { cast };
                let cast = if let Some(sacrifices) = sacrifice_objects.as_ref() {
                    if sacrifices.is_empty() {
                        cast.decline_optional()
                    } else {
                        let x = u32::try_from(sacrifices.len())
                            .map_err(|_| "cast.sacrifice has too many labels")?;
                        cast.accept_optional().x(x).sacrifice_with(sacrifices)
                    }
                } else {
                    cast
                };
                let cast = if action.get("targets").is_some() {
                    cast.target_slot_selections(&target_slots)
                } else {
                    cast
                };
                let committed = cast
                    .commit_with_card_choices(&mut card_choice_groups)
                    .map_err(|error| error.to_string())?;
                if let (Some(choice), Some(before)) = (&mana_choice, &mana_pool_before) {
                    verify_mana_choice_spent(
                        choice,
                        before,
                        &mana_pool_source_pips(committed.state(), player)?,
                        &labels,
                    )?;
                }
                if let Some(sacrifices) = sacrifice_objects.as_ref() {
                    for id in sacrifices {
                        if committed
                            .state()
                            .objects
                            .get(id)
                            .is_some_and(|object| object.zone == Zone::Battlefield)
                        {
                            let label = labels.get(id).map(String::as_str).unwrap_or("<unknown>");
                            return Err(format!(
                                "cast.sacrifice label {label} was not consumed by the cast"
                            ));
                        }
                    }
                }
                if let Some(decision_slot_count) = target_decision_slot_count {
                    validate_engine_chosen_cast_targets(
                        committed.state(),
                        spell,
                        &target_slots,
                        decision_slot_count,
                    )?;
                }
            }
            "activate" => activate(
                &mut runner,
                &labels,
                action,
                action_index,
                expect_refused,
                &mut refusals,
                &mut activation_target_groups,
                &mut mana_choice_groups,
                spec,
                &mut discard_reconciler,
                &mut discard_choice_groups,
            )?,
            "label_permanent" => label_permanent(&runner, &mut labels, action)?,
            "pass_priority" => pass_priority(
                &mut runner,
                action,
                &labels,
                &mut discard_reconciler,
                &mut discard_choice_groups,
            )?,
            "trigger_targets" => {
                let entries = action
                    .get("targets")
                    .and_then(Value::as_array)
                    .ok_or("trigger_targets.targets must be a label array")?;
                let mut targets = Vec::with_capacity(entries.len());
                for entry in entries {
                    let label = entry
                        .as_str()
                        .ok_or("trigger_targets.targets entries must be labels")?;
                    if label == "@skip" {
                        targets.push(None);
                    } else if label.starts_with('@') {
                        return Err(format!(
                            "trigger_targets does not support reserved target {label}"
                        ));
                    } else {
                        targets.push(Some(resolve_script_label(&labels, label)?));
                    }
                }
                if targets.is_empty() {
                    return Err("trigger_targets needs at least one target".to_string());
                }
                trigger_target_groups.push_back(TriggerTargetGroup {
                    targets,
                    refused: expect_refused.then_some(action_index),
                });
            }
            "graveyard_targets" => {
                graveyard_target_choices.push_back(parse_target_script_choice(
                    action,
                    &labels,
                    "graveyard_targets",
                )?);
            }
            "copy_targets" => {
                copy_target_choices.push_back(parse_target_script_choice(
                    action,
                    &labels,
                    "copy_targets",
                )?);
            }
            "copy_choice" => {
                copy_choices.push_back(parse_copy_choice(action, &labels)?);
            }
            "order_triggers" => {
                discard_reconciler
                    .trigger_orders
                    .extend(parse_trigger_order(action)?);
            }
            "choose_optional" => {
                let answers = parse_optional_answers(action, &labels)?;
                optional_choice_groups[runner.state().active_player.0 as usize].extend(answers);
            }
            "attack" => {
                if attack_needs_resolution {
                    return Err("attack must be followed by pass_until_resolved".to_string());
                }
                declare_attackers_with_trigger_targets(
                    &mut runner,
                    &labels,
                    action,
                    &mut trigger_target_groups,
                    &mut discard_reconciler,
                    &mut discard_choice_groups,
                )?;
                attack_needs_resolution = true;
                attack_through_damage = action.get("through").is_some();
            }
            "choose_cards" => {
                card_choice_groups.push_back(resolve_card_choice_labels(action, &labels)?);
            }
            "choose_copies" => {
                copy_cast_choices.push_back(parse_copy_cast_choice(
                    action,
                    &labels,
                    expect_refused.then_some(action_index),
                )?);
            }
            "choose_discard" => {
                let (player, choice) = parse_discard_choice(action, &labels)?;
                discard_choice_groups
                    .get_mut(player.0 as usize)
                    .ok_or("choose_discard.player must be P0 or P1")?
                    .push_back(choice);
            }
            "choose_surveil" => {
                surveil_choices.push_back(parse_surveil_choice(action, &labels)?);
            }
            "choose_outside_game" => {
                outside_game_choices.push_back(parse_outside_game_choice(action)?);
            }
            "choose_mana" => {
                mana_choice_groups.push_back(parse_mana_choice(action)?);
            }
            "choose_replacement" | "choose_entity" => {}
            "pass_until_resolved" => {
                pass_until_resolved(
                    name,
                    &mut runner,
                    &labels,
                    &mut trigger_target_groups,
                    &mut activation_target_groups,
                    &mut card_choice_groups,
                    &mut copy_cast_choices,
                    &mut graveyard_target_choices,
                    &mut copy_target_choices,
                    &mut discard_choice_groups,
                    &mut surveil_choices,
                    &sideboard_labels,
                    &mut outside_game_choices,
                    &mut copy_choices,
                    &mut optional_choice_groups,
                    &mut unless_choice_groups,
                    &mut discard_reconciler,
                    attack_through_damage,
                )?;
                if attack_needs_resolution {
                    if attack_through_damage {
                        advance_through_combat_damage(
                            name,
                            &mut runner,
                            &labels,
                            &mut trigger_target_groups,
                            &mut activation_target_groups,
                            &mut card_choice_groups,
                            &mut copy_cast_choices,
                            &mut graveyard_target_choices,
                            &mut copy_target_choices,
                            &mut discard_choice_groups,
                            &mut surveil_choices,
                            &sideboard_labels,
                            &mut outside_game_choices,
                            &mut copy_choices,
                            &mut optional_choice_groups,
                            &mut unless_choice_groups,
                            &mut discard_reconciler,
                        )?;
                    } else {
                        ensure_attack_resolution_boundary(&runner)?;
                    }
                    attack_needs_resolution = false;
                    attack_through_damage = false;
                }
            }
            "end_turn" | "next_turn" => {
                advance_to_next_turn(
                    &mut runner,
                    action_index + 1 < actions.len(),
                    &labels,
                    &mut discard_reconciler,
                    &mut discard_choice_groups,
                )?;
            }
            other => return Err(format!("unsupported action {other}")),
        }
    }
    ensure_trigger_target_groups_consumed(&trigger_target_groups)?;
    ensure_activation_target_groups_consumed(&activation_target_groups)?;
    ensure_card_choice_groups_consumed(&card_choice_groups)?;
    ensure_copy_cast_choices_consumed(&copy_cast_choices)?;
    ensure_target_choices_consumed("graveyard_targets", &graveyard_target_choices)?;
    ensure_target_choices_consumed("copy_targets", &copy_target_choices)?;
    ensure_discard_choices_consumed(&discard_choice_groups)?;
    ensure_surveil_choices_consumed(&surveil_choices)?;
    ensure_outside_game_choices_consumed(&outside_game_choices)?;
    ensure_mana_choices_consumed(&mana_choice_groups)?;
    ensure_copy_choices_consumed(&copy_choices)?;
    ensure_trigger_orders_consumed(&discard_reconciler.trigger_orders)?;
    ensure_optional_choices_consumed(&optional_choice_groups)?;
    ensure_unless_choice_groups_consumed(&unless_choice_groups)?;
    if attack_needs_resolution {
        return Err("attack action was not followed by pass_until_resolved".to_string());
    }
    let reached = check_reach(runner.state(), &labels, spec.get("reach"))?;
    refusals.extend(std::mem::take(&mut discard_reconciler.choice_refusals));
    refusals.sort_unstable();
    Ok(json!({
        "scenario": name,
        "projection": project(runner.state(), &labels, legal, reached),
        "reach": reached,
        "refusals": refusals,
        "refusal_cost_prompts": refusal_cost_prompts,
    }))
}

const REACH_FIELDS: &[&str] = &[
    "all",
    "player",
    "life",
    "zone",
    "card",
    "label",
    "count",
    "tapped_labels",
    "types",
    "subtypes",
    "colors",
    "power",
    "toughness",
    "damage",
    "power_is_null",
    "toughness_is_null",
    "creature",
    "tapped",
    "attached_to",
    "counters",
    "solved",
    "token",
];

fn validate_reach_fields(reach: &Value) -> Result<(), String> {
    let Some(fields) = reach.as_object() else {
        return Err("reach must be an object".to_string());
    };
    for key in fields.keys() {
        if !REACH_FIELDS.contains(&key.as_str()) {
            return Err(format!("unsupported reach field {key}"));
        }
    }
    Ok(())
}

fn check_reach(
    state: &GameState,
    labels: &HashMap<ObjectId, String>,
    reach: Option<&Value>,
) -> Result<bool, String> {
    let Some(reach) = reach else {
        return Ok(true);
    };
    validate_reach_fields(reach)?;
    if let Some(checks) = reach.get("all") {
        if reach.as_object().is_none_or(|fields| fields.len() != 1) {
            return Err("reach.all cannot be combined with other reach fields".to_string());
        }
        let Some(checks) = checks.as_array() else {
            return Err("reach.all must be an array of reach assertions".to_string());
        };
        if checks.is_empty() {
            return Err("reach.all must contain at least one assertion".to_string());
        }
        for check in checks {
            if !check_reach(state, labels, Some(check))? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    let player = reach["player"].as_str().unwrap_or("P0");
    let index = player
        .strip_prefix('P')
        .and_then(|value| value.parse::<usize>().ok())
        .ok_or("reach player must be P0 or P1")?;
    if index > 1 {
        return Err("reach player must be P0 or P1".to_string());
    }
    if let Some(expected_life) = reach.get("life") {
        if reach.as_object().is_some_and(|fields| {
            fields
                .keys()
                .any(|key| !["player", "life"].contains(&key.as_str()))
        }) {
            return Err("reach.life can only be combined with player".to_string());
        }
        let expected_life = expected_life
            .as_i64()
            .ok_or("reach.life must be an integer")?;
        return Ok(state.players[index].life as i64 == expected_life);
    }
    let zone = reach["zone"].as_str().unwrap_or("graveyard");
    let card = reach["card"].as_str().ok_or("reach needs card")?;
    let labeled_id = reach
        .get("label")
        .map(|value| {
            let label = value
                .as_str()
                .ok_or("reach.label must be a battlefield label")?;
            resolve_label(labels, label)
        })
        .transpose()?;
    if labeled_id.is_some() && zone != "battlefield" {
        return Err("reach.label is only valid for battlefield permanents".to_string());
    }
    if reach.get("solved").is_some() && zone != "battlefield" {
        return Err("reach.solved is only valid for battlefield Cases".to_string());
    }
    let expected_count = reach
        .get("count")
        .map(|value| {
            value
                .as_u64()
                .and_then(|count| usize::try_from(count).ok())
                .ok_or("reach.count must be a non-negative integer")
        })
        .transpose()?;
    let in_zone = cards(state, index, zone).iter().any(|name| name == card);
    if expected_count.is_none() && !in_zone {
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
        if expected_count.is_some_and(|expected| {
            cards(state, index, zone)
                .iter()
                .filter(|name| *name == card)
                .count()
                != expected
        }) {
            return Ok(false);
        }
        return Ok(![
            "types",
            "subtypes",
            "colors",
            "power",
            "toughness",
            "damage",
            "creature",
            "tapped",
            "attached_to",
            "counters",
            "token",
        ]
        .iter()
        .any(|key| reach.get(key).is_some()));
    }

    let mut matching_count = 0;
    for id in &state.battlefield {
        if labeled_id.is_some_and(|expected| *id != expected) {
            continue;
        }
        let Some(object) = state
            .objects
            .get(id)
            .filter(|object| object.controller.0 as usize == index && object.name == card)
        else {
            continue;
        };
        if matches_reach_characteristics(object, reach, labels)? {
            matching_count += 1;
        }
    }
    Ok(expected_count.map_or(matching_count > 0, |expected| matching_count == expected))
}

fn matches_reach_characteristics(
    object: &GameObject,
    reach: &Value,
    labels: &HashMap<ObjectId, String>,
) -> Result<bool, String> {
    if let Some(expected) = reach.get("token") {
        let Some(expected) = expected.as_bool() else {
            return Err("reach.token must be a boolean".to_string());
        };
        if object.is_token != expected {
            return Ok(false);
        }
    }
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
    if let Some(expected) = reach.get("damage") {
        let expected = expected
            .as_u64()
            .ok_or("reach.damage must be a non-negative integer")?;
        if json!(object.damage_marked) != json!(expected) {
            return Ok(false);
        }
    }
    if let Some(expected) = reach.get("solved") {
        let Some(expected) = expected.as_bool() else {
            return Err("reach.solved must be a boolean".to_string());
        };
        if !is_case(object) {
            return Err("reach.solved is only valid for Case permanents".to_string());
        }
        let actual = object
            .case_state
            .as_ref()
            .ok_or("Case permanent has no Case state")?
            .is_solved;
        if actual != expected {
            return Ok(false);
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
    if let Some(expected) = reach.get("attached_to") {
        let expected = expected
            .as_str()
            .ok_or("reach.attached_to must be a battlefield label")?;
        let expected_id = labels
            .iter()
            .find_map(|(id, label)| (label == expected).then_some(*id))
            .ok_or_else(|| format!("unknown battlefield label {expected}"))?;
        if object.attached_to
            != Some(engine::types::object_data::AttachTarget::Object(
                expected_id,
            ))
        {
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

    fn empty_discard_context() -> (DiscardChoiceReconciler, [VecDeque<DiscardScriptChoice>; 2]) {
        (
            DiscardChoiceReconciler::default(),
            [VecDeque::new(), VecDeque::new()],
        )
    }

    fn pass_until_resolved_with_discard_choices(
        runner: &mut GameRunner,
        labels: &HashMap<ObjectId, String>,
        discard_choices: &mut [VecDeque<DiscardScriptChoice>; 2],
        discard_reconciler: &mut DiscardChoiceReconciler,
    ) -> Result<(), String> {
        let mut trigger_target_groups = VecDeque::new();
        let mut activation_target_groups = VecDeque::new();
        let mut card_choice_groups = VecDeque::new();
        let mut copy_cast_choices = VecDeque::new();
        let mut graveyard_target_choices = VecDeque::new();
        let mut copy_target_choices = VecDeque::new();
        let mut surveil_choices = VecDeque::new();
        let sideboard_labels = HashMap::new();
        let mut outside_game_choices = VecDeque::new();
        let mut copy_choices = VecDeque::new();
        let mut optional_choices = [VecDeque::new(), VecDeque::new()];
        let mut unless_choices = [VecDeque::new(), VecDeque::new()];

        pass_until_resolved(
            "discard reconciliation test",
            runner,
            labels,
            &mut trigger_target_groups,
            &mut activation_target_groups,
            &mut card_choice_groups,
            &mut copy_cast_choices,
            &mut graveyard_target_choices,
            &mut copy_target_choices,
            discard_choices,
            &mut surveil_choices,
            &sideboard_labels,
            &mut outside_game_choices,
            &mut copy_choices,
            &mut optional_choices,
            &mut unless_choices,
            discard_reconciler,
            false,
        )
    }

    #[test]
    fn behold_prompt_presence_must_match_the_script_field() {
        assert!(ensure_behold_prompt_matches_field(false, false).is_ok());
        assert!(ensure_behold_prompt_matches_field(true, true).is_ok());
        assert!(ensure_behold_prompt_matches_field(true, false).is_err());
        assert!(ensure_behold_prompt_matches_field(false, true).is_err());
    }

    #[test]
    fn choose_optional_answers_are_exact_fifo_and_player_scoped() {
        let answers = parse_optional_answers(
            &json!({
                "action": "choose_optional",
                "answers": ["yes", "no"]
            }),
            &HashMap::new(),
        )
        .unwrap();
        let mut choices = [VecDeque::from(answers), VecDeque::new()];

        assert!(take_optional_choice(&mut choices, PlayerId(0), None).unwrap());
        assert!(!take_optional_choice(&mut choices, PlayerId(0), None).unwrap());
        assert!(ensure_optional_choices_consumed(&choices).is_ok());
        assert_eq!(
            take_optional_choice(&mut choices, PlayerId(1), None).unwrap_err(),
            "optional prompt arrived without a choose_optional answer"
        );

        let mut wrong_player = [
            VecDeque::from([OptionalChoice {
                answer: true,
                source: None,
            }]),
            VecDeque::new(),
        ];
        assert_eq!(
            take_optional_choice(&mut wrong_player, PlayerId(1), None).unwrap_err(),
            "choose_optional answer was queued for a different player"
        );
        assert!(ensure_optional_choices_consumed(&wrong_player).is_err());
    }

    #[test]
    fn choose_optional_source_mismatch_fails_without_consuming_the_answer() {
        let source = ObjectId(7);
        let labels = HashMap::from([(source, "key".to_string())]);
        let parsed = parse_optional_answers(
            &json!({
                "action": "choose_optional",
                "source": "key",
                "answers": ["yes"]
            }),
            &labels,
        )
        .unwrap();
        assert!(parsed[0].answer);
        assert_eq!(parsed[0].source, Some(source));

        let mut choices = [
            VecDeque::from([OptionalChoice {
                answer: true,
                source: Some(source),
            }]),
            VecDeque::new(),
        ];

        assert!(take_optional_choice(&mut choices, PlayerId(0), Some(ObjectId(8))).is_err());
        assert_eq!(choices[0].len(), 1);
        assert!(take_optional_choice(&mut choices, PlayerId(0), Some(source)).unwrap());
    }

    #[test]
    fn choose_optional_rejects_noncanonical_answers_and_unconsumed_answers() {
        for answer in ["Yes", "NO", "accept", ""] {
            let action = json!({"action": "choose_optional", "answers": [answer]});
            assert!(parse_optional_answers(&action, &HashMap::new()).is_err());
        }
        assert!(parse_optional_answers(
            &json!({
                "action": "choose_optional",
                "answers": []
            }),
            &HashMap::new()
        )
        .is_err());
        assert!(parse_optional_answers(
            &json!({
                "action": "choose_optional",
                "answers": ["yes"],
                "player": "P0"
            }),
            &HashMap::new()
        )
        .is_err());

        let choices = [
            VecDeque::from([OptionalChoice {
                answer: true,
                source: None,
            }]),
            VecDeque::new(),
        ];
        assert!(ensure_optional_choices_consumed(&choices).is_err());
    }

    #[test]
    fn behold_label_must_name_an_offered_candidate() {
        let selected = ObjectId(1);
        assert!(ensure_behold_selection_is_offered("jace", selected, &[selected]).is_ok());
        assert!(ensure_behold_selection_is_offered("jace", selected, &[ObjectId(2)]).is_err());
    }

    #[test]
    fn behold_filter_is_found_in_may_cost_replacement() {
        let definition: ReplacementDefinition = serde_json::from_value(json!({
            "event": "Moved",
            "mode": {
                "type": "MayCost",
                "cost": {
                    "type": "Behold",
                    "count": 1,
                    "filter": {
                        "type": "Typed",
                        "type_filters": [{"Subtype": "Jace"}],
                        "controller": null,
                        "properties": []
                    },
                    "action": "ChooseOrReveal"
                }
            }
        }))
        .unwrap();

        assert!(behold_filter_in_replacement(&definition).is_some());
    }

    #[test]
    fn behold_prompt_requires_the_played_lands_optional_replacement() {
        let land_id = ObjectId(1);
        let prompt = WaitingFor::ReplacementChoice {
            player: PlayerId(0),
            candidate_count: 2,
            candidates: vec![
                engine::types::game_state::ReplacementCandidateSummary {
                    source_id: land_id,
                    source_name: "Theorist's Sanctum".to_string(),
                    description: "Pay cost".to_string(),
                },
                engine::types::game_state::ReplacementCandidateSummary {
                    source_id: land_id,
                    source_name: "Theorist's Sanctum".to_string(),
                    description: "Decline".to_string(),
                },
            ],
            kind: engine::types::game_state::ReplacementChoiceKind::OptionalBranch,
            last_applied_decides: false,
            remember_identity: None,
        };

        assert!(is_behold_replacement_prompt(&prompt, land_id, true));
        assert!(!is_behold_replacement_prompt(&prompt, ObjectId(2), true));
        assert!(!is_behold_replacement_prompt(&prompt, land_id, false));
    }

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
    fn refusal_classification_distinguishes_absent_failed_and_accepted_actions() {
        assert_eq!(
            classify_refusal(false, false),
            Some(RefusalMode::AbsentFromLegalActions)
        );
        assert_eq!(
            classify_refusal(true, true),
            Some(RefusalMode::OfferedThenErr)
        );
        assert_eq!(classify_refusal(true, false), None);
    }

    #[test]
    fn only_engine_action_errors_count_as_offered_then_err_refusals() {
        assert_eq!(
            refusal_mode_for_action_error(
                true,
                &ClassifiedActError::Engine("ActionNotAllowed: insufficient evidence".to_string())
            ),
            Some(RefusalMode::OfferedThenErr)
        );
        assert_eq!(
            refusal_mode_for_action_error(
                true,
                &ClassifiedActError::Harness(
                    "ActionNotAllowed: missing scripted choice".to_string()
                )
            ),
            None
        );
    }

    #[test]
    fn refused_attempt_prompts_fail_closed() {
        let prompt_error = refused_attempt_prompt_error(
            "cast",
            &WaitingFor::MeldPairChoice {
                player: PlayerId(0),
                choices: Vec::new(),
            },
        );
        assert!(prompt_error.to_string().contains("unscripted prompt"));
        assert_eq!(refusal_mode_for_action_error(true, &prompt_error), None);
    }

    #[test]
    fn refused_activation_attempt_keeps_turn_tracking_on_its_clone() {
        let runner = GameScenario::new().build();
        let mut attempt = refusal_attempt_runner(&runner);
        attempt
            .state_mut()
            .activated_abilities_this_turn
            .insert((ObjectId(1), 0), 1);
        assert!(runner.state().activated_abilities_this_turn.is_empty());
    }

    #[test]
    fn refused_activation_rejects_sources_outside_battlefield_or_graveyard() {
        assert!(activation_source_zone_precondition(Zone::Battlefield, "source").is_ok());
        assert!(activation_source_zone_precondition(Zone::Graveyard, "source").is_ok());
        let error = activation_source_zone_precondition(Zone::Hand, "source").unwrap_err();
        assert!(error.contains("must be on the battlefield or in the graveyard"));
    }

    #[test]
    fn expected_refused_cast_skips_target_cost_preflight() {
        assert!(!cast_target_decision_slot_count_needed(true, true));
        assert!(cast_target_decision_slot_count_needed(true, false));
        assert!(!cast_target_decision_slot_count_needed(false, false));
    }

    #[test]
    fn refusal_requires_unchanged_projection_and_reports_both_states() {
        let before = json!({"players": {"P0": {"hand": ["Spell"]}}});
        let same = json!({"players": {"P0": {"hand": ["Spell"]}}});
        let mut refusals = Vec::new();

        record_refusal(
            2,
            "cast",
            RefusalMode::AbsentFromLegalActions,
            &before,
            &same,
            &mut refusals,
        )
        .unwrap();
        assert_eq!(refusals, vec![2]);

        let after = json!({"players": {"P0": {"hand": []}}});
        let error = record_refusal(
            4,
            "activate",
            RefusalMode::OfferedThenErr,
            &before,
            &after,
            &mut Vec::new(),
        )
        .unwrap_err();
        assert!(error.contains("refused action left state changed"));
        assert!(error.contains(&serde_json::to_string_pretty(&before).unwrap()));
        assert!(error.contains(&serde_json::to_string_pretty(&after).unwrap()));
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
    fn accepts_exile_setup() {
        let scenario = json!({
            "name": "exile setup",
            "setup": {
                "P0": {"exile": ["Walking Corpse"]}
            }
        });

        assert!(validate_setup(&scenario).is_ok());
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
    fn permanent_cannot_be_labeled_twice() {
        let id = ObjectId(17);
        let labels = HashMap::from([(id, "old".to_string())]);

        assert_eq!(
            ensure_permanent_unlabeled(&labels, id, "new"),
            Err("permanent already labeled old; cannot assign new".to_string())
        );
        assert!(ensure_permanent_unlabeled(&labels, ObjectId(18), "new").is_ok());
    }

    #[test]
    fn cast_target_slots_require_labels_or_the_explicit_skip_token() {
        let id = ObjectId(17);
        let labels = HashMap::from([(id, "artifact".to_string())]);

        assert_eq!(
            resolve_cast_target_slots(&json!({"targets": ["artifact", "@skip", "@P1"]}), &labels),
            Ok(vec![
                Some(TargetRef::Object(id)),
                None,
                Some(TargetRef::Player(PlayerId(1))),
            ])
        );
        assert!(
            resolve_cast_target_slots(&json!({"targets": ["missing"]}), &labels)
                .unwrap_err()
                .contains("unknown target label")
        );
        assert!(
            resolve_cast_target_slots(&json!({"targets": ["@other"]}), &labels)
                .unwrap_err()
                .contains("@skip, @P0, or @P1")
        );
        assert!(
            resolve_cast_target_slots(&json!({"targets": ["@P2"]}), &labels)
                .unwrap_err()
                .contains("must be @P0 or @P1")
        );
    }

    #[test]
    fn cast_target_entries_match_auto_selected_stack_targets() {
        let target = TargetRef::Object(ObjectId(17));
        let other = TargetRef::Object(ObjectId(18));

        assert!(validate_cast_target_entries(
            &[Some(target.clone())],
            1,
            std::slice::from_ref(&target)
        )
        .is_ok());
        assert!(
            validate_cast_target_entries(&[Some(other)], 1, std::slice::from_ref(&target)).is_err()
        );
        assert!(validate_cast_target_entries(&[], 1, std::slice::from_ref(&target)).is_err());
        assert!(validate_cast_target_entries(
            &[Some(target.clone()), Some(TargetRef::Object(ObjectId(19)))],
            1,
            std::slice::from_ref(&target)
        )
        .is_err());
        assert!(validate_cast_target_entries(
            &[None, Some(target.clone())],
            2,
            std::slice::from_ref(&target)
        )
        .is_ok());
        assert!(validate_cast_target_entries(&[Some(target.clone()), None], 1, &[target]).is_err());
        let player = TargetRef::Player(PlayerId(1));
        assert!(validate_cast_target_entries(&[Some(player.clone())], 1, &[player]).is_ok());
        assert!(validate_cast_target_entries(
            &[Some(TargetRef::Player(PlayerId(1)))],
            1,
            &[TargetRef::Player(PlayerId(0))]
        )
        .is_err());
    }

    #[test]
    fn cast_target_validation_reads_the_spell_stack_after_auto_selection() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let artifact = scenario
                    .add_artifact_from_oracle(PlayerId(1), "Target Artifact", "")
                    .id();
                let spell = scenario
                    .add_spell_to_hand_from_oracle(
                        PlayerId(0),
                        "Shatter",
                        true,
                        "Destroy target artifact.",
                    )
                    .with_mana_cost(engine::types::mana::ManaCost::generic(0))
                    .id();
                let selections = [Some(TargetRef::Object(artifact))];
                let mut runner = scenario.build();
                let decision_slot_count =
                    cast_target_decision_slot_count(runner.state(), spell, PlayerId(0), None)
                        .unwrap();
                assert_eq!(decision_slot_count, 1);
                let committed = runner
                    .cast(spell)
                    .target_slot_selections(&selections)
                    .commit();

                assert!(matches!(
                    &committed.state().waiting_for,
                    WaitingFor::Priority { .. }
                ));
                assert!(validate_engine_chosen_cast_targets(
                    committed.state(),
                    spell,
                    &selections,
                    decision_slot_count
                )
                .is_ok());
                assert!(validate_engine_chosen_cast_targets(
                    committed.state(),
                    spell,
                    &[Some(TargetRef::Object(ObjectId(u64::MAX)))],
                    decision_slot_count
                )
                .is_err());
                assert!(validate_engine_chosen_cast_targets(
                    committed.state(),
                    spell,
                    &[Some(TargetRef::Object(artifact)), None],
                    decision_slot_count
                )
                .is_err());
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn cast_target_preflight_uses_new_active_player_after_next_turn() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let _artifact = scenario
                    .add_artifact_from_oracle(PlayerId(0), "P0 artifact", "")
                    .id();
                let spell = scenario
                    .add_spell_to_hand_from_oracle(
                        PlayerId(1),
                        "Shatter",
                        true,
                        "Destroy target artifact an opponent controls.",
                    )
                    .with_mana_cost(engine::types::mana::ManaCost::generic(0))
                    .id();
                scenario.with_library_top(PlayerId(1), &["P1 draw"]);
                let mut runner = scenario.build();
                let initial_active = runner.state().active_player;
                assert_eq!(initial_active, PlayerId(0));

                let (mut discard_reconciler, mut discard_choices) = empty_discard_context();
                advance_to_next_turn(
                    &mut runner,
                    true,
                    &HashMap::new(),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();

                let player = runner.state().active_player;
                assert_eq!(player, PlayerId(1));
                assert_eq!(
                    cast_target_decision_slot_count(runner.state(), spell, player, None),
                    Ok(1)
                );
                assert!(cast_target_decision_slot_count(
                    runner.state(),
                    spell,
                    initial_active,
                    None
                )
                .is_err());
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn setup_labels_cannot_use_the_reserved_at_prefix() {
        let graveyard = json!({
            "setup": {"P0": {"graveyard": [{"card": "Walking Corpse", "label": "@skip"}]}}
        });
        assert!(validate_setup(&graveyard)
            .unwrap_err()
            .contains("reserved @ prefix"));

        let battlefield = json!({
            "setup": {"P0": {"battlefield": [{"card": "Walking Corpse", "label": "@skip"}]}}
        });
        assert!(validate_setup(&battlefield)
            .unwrap_err()
            .contains("reserved @ prefix"));
    }

    #[test]
    fn activation_selector_requires_one_label_prefix_match() {
        let candidates = vec![
            (
                0,
                AbilityKind::Activated,
                true,
                None,
                Some("{T}: Add {G}".to_string()),
            ),
            (
                1,
                AbilityKind::Activated,
                false,
                None,
                Some("{2}{G}{W}: {this} becomes a Llama".to_string()),
            ),
            (
                2,
                AbilityKind::Activated,
                false,
                None,
                Some("<i>Solved</i> &mdash; Sacrifice this Case: Draw a card.".to_string()),
            ),
        ];
        assert_eq!(
            select_activation_ability(candidates.clone(), "{2}{G}{W}:"),
            Ok(1)
        );
        assert!(select_activation_ability(candidates.clone(), "{3}:").is_err());
        assert_eq!(
            select_activation_ability(candidates.clone(), "Sacrifice this Case"),
            Ok(2)
        );

        let multiple = vec![
            (
                0,
                AbilityKind::Activated,
                false,
                None,
                Some("{T}: First".to_string()),
            ),
            (
                1,
                AbilityKind::Activated,
                false,
                None,
                Some("{T}: Second".to_string()),
            ),
        ];
        assert!(select_activation_ability(multiple, "{T}:")
            .unwrap_err()
            .contains("multiple activated abilities"));

        let mana_without_description = vec![
            (0, AbilityKind::Activated, true, None, None),
            (
                1,
                AbilityKind::Activated,
                false,
                None,
                Some("{2}{U}, {T}: Empower Jace 2.".to_string()),
            ),
        ];
        assert_eq!(
            select_activation_ability(mana_without_description, "{2}{U}, {T}:"),
            Ok(1)
        );

        let missing = vec![(0, AbilityKind::Activated, false, None, None)];
        assert!(select_activation_ability(missing, "{T}:")
            .unwrap_err()
            .contains("missing a description"));

        assert_eq!(
            loyalty_ability_label(&AbilityCost::Loyalty { amount: 2 }).as_deref(),
            Some("+2")
        );
    }

    #[test]
    fn loyalty_cost_selector_matches_exact_normalized_labels() {
        let candidates = vec![
            (
                0,
                AbilityKind::Activated,
                false,
                loyalty_ability_label(&AbilityCost::Loyalty { amount: 2 }),
                None,
            ),
            (
                1,
                AbilityKind::Activated,
                false,
                loyalty_ability_label(&AbilityCost::Loyalty { amount: -1 }),
                None,
            ),
            (
                2,
                AbilityKind::Activated,
                false,
                loyalty_ability_label(&AbilityCost::Loyalty { amount: -10 }),
                None,
            ),
        ];

        assert_eq!(select_activation_ability(candidates.clone(), "-1"), Ok(1));
        assert_eq!(select_activation_ability(candidates.clone(), "−1"), Ok(1));
        assert_eq!(select_activation_ability(candidates.clone(), "-10"), Ok(2));
        assert_eq!(select_activation_ability(candidates, "+2"), Ok(0));
    }

    #[test]
    fn loyalty_description_prefix_is_supported_and_must_be_unique() {
        let candidates = vec![
            (
                0,
                AbilityKind::Activated,
                false,
                loyalty_ability_label(&AbilityCost::Loyalty { amount: -1 }),
                Some("−1: Surveil 1.".to_string()),
            ),
            (
                1,
                AbilityKind::Activated,
                false,
                loyalty_ability_label(&AbilityCost::Loyalty { amount: -3 }),
                Some("−3: Draw a card.".to_string()),
            ),
        ];
        assert_eq!(
            select_activation_ability(candidates.clone(), "−1: Surveil"),
            Ok(0)
        );

        let ambiguous = vec![
            (
                0,
                AbilityKind::Activated,
                false,
                None,
                Some("0: Draw a card and gain life.".to_string()),
            ),
            (
                1,
                AbilityKind::Activated,
                false,
                None,
                Some("0: Draw a card and scry 1.".to_string()),
            ),
        ];
        assert!(select_activation_ability(ambiguous, "0: Draw")
            .unwrap_err()
            .contains("multiple activated abilities"));
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
    fn cast_sacrifice_labels_validate_scripted_choices() {
        let object = ObjectId(17);
        let labels = HashMap::from([(object, "fodder".to_string())]);

        assert_eq!(
            cast_sacrifice_labels(&json!({"action": "cast", "sacrifice": ["fodder"]}), &labels),
            Ok(Some(vec![object]))
        );
        assert_eq!(
            cast_sacrifice_labels(&json!({"action": "cast", "sacrifice": []}), &labels),
            Ok(Some(vec![]))
        );
        assert!(cast_sacrifice_labels(
            &json!({"action": "cast", "sacrifice": ["missing"]}),
            &labels
        )
        .unwrap_err()
        .contains("unknown setup label"));
        assert!(cast_sacrifice_labels(
            &json!({"action": "cast", "sacrifice": ["fodder", "fodder"]}),
            &labels
        )
        .unwrap_err()
        .contains("repeats label"));
        assert!(
            cast_sacrifice_labels(&json!({"action": "activate", "sacrifice": []}), &labels)
                .unwrap_err()
                .contains("only valid for cast")
        );
    }

    #[test]
    fn cast_sacrifice_prompt_rejects_a_missing_script_entry() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                for _ in 0..6 {
                    scenario.add_basic_land(PlayerId(0), ManaColor::Black);
                }
                let spell = scenario
                    .add_creature_to_hand_from_oracle(
                        PlayerId(0),
                        "Rottenmouth Viper",
                        5,
                        5,
                        "As an additional cost to cast this spell, you may sacrifice any number of nonland permanents. This spell costs {1} less to cast for each permanent sacrificed this way. Whenever this creature enters or attacks, put a blight counter on it. Then for each blight counter on it, each opponent loses 4 life unless that player sacrifices a nonland permanent of their choice or discards a card.",
                    )
                    .with_mana_cost(engine::types::mana::ManaCost::Cost {
                        shards: vec![engine::types::mana::ManaCostShard::Black],
                        generic: 5,
                    })
                    .id();
                let runner = scenario.build();

                let prompt = probe_cast_sacrifice_prompt(&runner, spell).unwrap();
                assert_eq!(prompt, Some(true));
                assert!(ensure_cast_sacrifice_prompt_decision(None, prompt)
                    .unwrap_err()
                    .contains("without cast.sacrifice"));
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn cast_sacrifice_script_requires_the_prompt_even_when_empty() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let spell = scenario
                    .add_spell_to_hand_from_oracle(PlayerId(0), "Simple Spell", true, "")
                    .with_mana_cost(engine::types::mana::ManaCost::generic(0))
                    .id();
                let runner = scenario.build();
                let prompt = probe_cast_sacrifice_prompt(&runner, spell).unwrap();

                assert_eq!(prompt, Some(false));
                assert!(ensure_cast_sacrifice_prompt_decision(Some(&[]), prompt)
                    .unwrap_err()
                    .contains("no optional sacrifice cost prompt"));
                assert!(ensure_cast_sacrifice_prompt_decision(None, prompt).is_ok());
                assert!(ensure_cast_sacrifice_prompt_decision(Some(&[]), Some(true)).is_ok());
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn unless_choices_fail_closed_for_missing_extra_and_illegal_choices() {
        let selected = ObjectId(17);
        let labels = HashMap::from([(selected, "fodder".to_string())]);
        let mut queues = [VecDeque::new(), VecDeque::new()];

        assert!(take_unless_script_choice_if_payable(
            &mut queues[1],
            PlayerId(1),
            &[0],
            test_unless_choice_index,
        )
        .unwrap_err()
        .contains("no scripted choice"));

        let action = json!({
            "action": "unless_choices",
            "player": "P1",
            "choices": [{"sacrifice": "fodder"}, "decline"]
        });
        let (player, choices) = parse_unless_script_choices(&action, &labels).unwrap();
        assert_eq!(player, PlayerId(1));
        queues[1].extend(choices);
        assert_eq!(
            take_unless_script_choice_if_payable(
                &mut queues[1],
                player,
                &[0],
                test_unless_choice_index,
            ),
            Ok(UnlessScriptResponse::Pay {
                choice: UnlessScriptChoice::Sacrifice(selected),
                index: 0,
            })
        );
        assert!(ensure_unless_choice_groups_consumed(&queues)
            .unwrap_err()
            .contains("unconsumed"));
        assert_eq!(
            take_unless_script_choice_if_payable(
                &mut queues[1],
                player,
                &[0, 1],
                test_unless_choice_index,
            ),
            Ok(UnlessScriptResponse::Decline)
        );
        assert!(ensure_unless_choice_groups_consumed(&queues).is_ok());

        assert!(
            ensure_unless_choice_is_offered("fodder", selected, &[ObjectId(18)])
                .unwrap_err()
                .contains("not offered")
        );
        assert!(
            unless_cost_index(&[], UnlessScriptChoice::Sacrifice(selected))
                .unwrap_err()
                .contains("was not offered")
        );
        assert!(parse_unless_script_choices(
            &json!({
                "action": "unless_choices",
                "player": "P1",
                "choices": [{"discard": "missing"}]
            }),
            &labels
        )
        .unwrap_err()
        .contains("unknown setup label"));
        assert!(parse_unless_script_choices(
            &json!({
                "action": "unless_choices",
                "player": "P1",
                "choices": [{"sacrifice": "fodder", "discard": "fodder"}]
            }),
            &labels
        )
        .unwrap_err()
        .contains("exactly one choice"));
    }

    fn test_unless_choice_index(choice: UnlessScriptChoice) -> Result<usize, String> {
        match choice {
            UnlessScriptChoice::Sacrifice(_) => Ok(0),
            UnlessScriptChoice::Discard(_) => Ok(1),
            UnlessScriptChoice::Decline => Err("decline has no cost index".to_string()),
        }
    }

    #[test]
    fn unless_unpayable_prompt_auto_declines_without_consuming_a_choice() {
        let mut queue = VecDeque::new();

        assert_eq!(
            take_unless_script_choice_if_payable(
                &mut queue,
                PlayerId(1),
                &[],
                test_unless_choice_index,
            ),
            Ok(UnlessScriptResponse::AutoDecline)
        );
        assert!(queue.is_empty());
    }

    #[test]
    fn unless_script_entry_without_payable_branches_remains_unconsumed() {
        let mut queues = [
            VecDeque::new(),
            VecDeque::from([UnlessScriptChoice::Discard(ObjectId(17))]),
        ];

        assert_eq!(
            take_unless_script_choice_if_payable(
                &mut queues[1],
                PlayerId(1),
                &[],
                test_unless_choice_index,
            ),
            Ok(UnlessScriptResponse::AutoDecline)
        );
        assert!(ensure_unless_choice_groups_consumed(&queues)
            .unwrap_err()
            .contains("unconsumed"));
    }

    #[test]
    fn unless_script_entry_for_an_unpayable_branch_fails_without_consuming_it() {
        let mut queue = VecDeque::from([UnlessScriptChoice::Discard(ObjectId(17))]);

        assert!(take_unless_script_choice_if_payable(
            &mut queue,
            PlayerId(1),
            &[0],
            test_unless_choice_index,
        )
        .unwrap_err()
        .contains("unpayable cost index 1"));
        assert_eq!(
            queue,
            VecDeque::from([UnlessScriptChoice::Discard(ObjectId(17))])
        );
    }

    #[test]
    fn expected_refusal_is_limited_to_actions_and_refusable_choices() {
        for action in ["cast", "activate", "choose_copies", "trigger_targets"] {
            let script = json!({ "action": action, "expect": "refused" });
            assert_eq!(script_expected_refusal(&script, 0), Ok(true));
        }
        let plain = json!({ "action": "choose_copies" });
        assert_eq!(script_expected_refusal(&plain, 0), Ok(false));
        let unsupported = json!({ "action": "choose_cards", "expect": "refused" });
        assert!(script_expected_refusal(&unsupported, 2)
            .unwrap_err()
            .contains("script[2].expect is only valid for"));
    }

    #[test]
    fn refused_copy_choice_needs_a_refused_card() {
        let source = ObjectId(7);
        let card = ObjectId(8);
        let labels = HashMap::from([
            (source, "source".to_string()),
            (card, "grave.card".to_string()),
        ]);
        let empty =
            json!({ "action": "choose_copies", "player": "P0", "source": "source", "cards": [] });
        assert!(parse_copy_cast_choice(&empty, &labels, Some(3))
            .unwrap_err()
            .contains("needs an accepted card before the refused card"));
        assert_eq!(
            parse_copy_cast_choice(&empty, &labels, None)
                .unwrap()
                .refused,
            None
        );
        let one = json!({
            "action": "choose_copies", "player": "P0", "source": "source", "cards": ["grave.card"]
        });
        assert!(parse_copy_cast_choice(&one, &labels, Some(3))
            .unwrap_err()
            .contains("needs an accepted card before the refused card"));
        let two = json!({
            "action": "choose_copies", "player": "P0", "source": "source",
            "cards": ["source", "grave.card"]
        });
        let choice = parse_copy_cast_choice(&two, &labels, Some(3)).unwrap();
        assert_eq!(choice.cards, vec![source, card]);
        assert_eq!(choice.refused, Some(3));
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
        let mut groups = VecDeque::from(vec![
            TriggerTargetGroup::from(vec![Some(target)]),
            TriggerTargetGroup::from(vec![Some(second_target)]),
        ]);
        assert_eq!(
            take_trigger_target_group(&mut groups),
            Ok(vec![Some(target)].into())
        );
        assert_eq!(
            take_trigger_target_group(&mut groups),
            Ok(vec![Some(second_target)].into())
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

        let groups = VecDeque::from(vec![TriggerTargetGroup::from(vec![Some(target)])]);
        assert!(ensure_trigger_target_groups_consumed(&groups)
            .unwrap_err()
            .contains("unconsumed"));
    }

    #[test]
    fn copy_choices_fail_closed_for_missing_wrong_actor_and_unconsumed_choices() {
        let mut choices = VecDeque::new();
        assert!(take_copy_choice(&mut choices, PlayerId(0))
            .unwrap_err()
            .contains("without copy_choice"));

        choices.push_back(CopyChoice {
            player: PlayerId(0),
            accept: true,
            new_targets: None,
        });
        assert!(take_copy_choice(&mut choices, PlayerId(1))
            .unwrap_err()
            .contains("different player"));
        assert_eq!(choices.len(), 1);
        assert_eq!(
            take_copy_choice(&mut choices, PlayerId(0)),
            Ok(CopyChoice {
                player: PlayerId(0),
                accept: true,
                new_targets: None,
            })
        );
        assert!(choices.is_empty());

        choices.push_back(CopyChoice {
            player: PlayerId(0),
            accept: false,
            new_targets: None,
        });
        assert!(ensure_copy_choices_consumed(&choices)
            .unwrap_err()
            .contains("unconsumed"));
    }

    #[test]
    fn copy_target_choices_fail_closed_for_missing_wrong_player_and_unconsumed_choices() {
        let mut pending = None;
        assert!(take_copy_target_choice(&mut pending, PlayerId(0))
            .unwrap_err()
            .contains("without copy_choice.new_targets"));

        pending = Some((PlayerId(0), CopyTargetChoice::Keep));
        assert!(take_copy_target_choice(&mut pending, PlayerId(1))
            .unwrap_err()
            .contains("different player"));
        assert_eq!(
            take_copy_target_choice(&mut pending, PlayerId(0)),
            Ok(CopyTargetChoice::Keep)
        );
        assert!(ensure_copy_target_choice_consumed(&pending).is_ok());

        pending = Some((PlayerId(0), CopyTargetChoice::Change(vec![ObjectId(17)])));
        assert!(ensure_copy_target_choice_consumed(&pending)
            .unwrap_err()
            .contains("not consumed"));
    }

    #[test]
    fn copy_choice_targets_keep_or_change_by_label() {
        let original = ObjectId(17);
        let changed = ObjectId(18);
        let labels = HashMap::from([
            (original, "defender-a".to_string()),
            (changed, "defender-b".to_string()),
        ]);
        let choice = parse_copy_choice(
            &json!({
                "action": "copy_choice",
                "player": "P0",
                "decision": "accept",
                "new_targets": "keep"
            }),
            &labels,
        )
        .unwrap();
        assert_eq!(
            choice,
            CopyChoice {
                player: PlayerId(0),
                accept: true,
                new_targets: Some(CopyTargetChoice::Keep),
            }
        );

        let changed_choice = parse_copy_choice(
            &json!({
                "action": "copy_choice",
                "player": "P0",
                "decision": "accept",
                "new_targets": ["defender-b"]
            }),
            &labels,
        )
        .unwrap();
        assert_eq!(
            changed_choice.new_targets,
            Some(CopyTargetChoice::Change(vec![changed]))
        );
        assert!(parse_copy_choice(
            &json!({
                "action": "copy_choice",
                "player": "P0",
                "decision": "accept",
                "new_targets": ["same"]
            }),
            &HashMap::from([
                (original, "same".to_string()),
                (changed, "same".to_string())
            ]),
        )
        .unwrap_err()
        .contains("ambiguous"));
        assert!(parse_copy_choice(
            &json!({
                "action": "copy_choice",
                "player": "P0",
                "decision": "decline",
                "new_targets": ["defender-b"]
            }),
            &labels,
        )
        .unwrap_err()
        .contains("only valid when accepting"));
    }

    #[test]
    fn attack_action_uses_ready_setup_creature_and_stops_before_blockers() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let attacker = scenario.add_creature(PlayerId(0), "Ready Bear", 2, 2).id();
                let mut runner = scenario.build();
                assert!(!runner.state().objects[&attacker].summoning_sick);
                let labels = HashMap::from([(attacker, "attacker".to_string())]);
                let (mut discard_reconciler, mut discard_choices) = empty_discard_context();
                declare_attackers(
                    &mut runner,
                    &labels,
                    &json!({"action": "attack", "attackers": ["attacker"]}),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();
                assert_eq!(runner.state().phase, Phase::DeclareAttackers);

                let mut trigger_targets = VecDeque::new();
                let mut activation_targets = VecDeque::new();
                let mut card_choices = VecDeque::new();
                let mut copy_cast_choices = VecDeque::new();
                let mut graveyard_target_choices = VecDeque::new();
                let mut copy_target_choices = VecDeque::new();
                let mut discard_choices = [VecDeque::new(), VecDeque::new()];
                let mut surveil_choices = VecDeque::new();
                let sideboard_labels = HashMap::new();
                let mut outside_game_choices = VecDeque::new();
                let mut copy_choices = VecDeque::new();
                let mut optional_choices = [VecDeque::new(), VecDeque::new()];
                let mut unless_choices = [VecDeque::new(), VecDeque::new()];
                pass_until_resolved(
                    "attack runner test",
                    &mut runner,
                    &labels,
                    &mut trigger_targets,
                    &mut activation_targets,
                    &mut card_choices,
                    &mut copy_cast_choices,
                    &mut graveyard_target_choices,
                    &mut copy_target_choices,
                    &mut discard_choices,
                    &mut surveil_choices,
                    &sideboard_labels,
                    &mut outside_game_choices,
                    &mut copy_choices,
                    &mut optional_choices,
                    &mut unless_choices,
                    &mut discard_reconciler,
                    false,
                )
                .unwrap();
                ensure_attack_resolution_boundary(&runner).unwrap();
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn attack_through_damage_reaches_active_player_priority_after_combat_damage() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let attacker = scenario.add_creature(PlayerId(0), "Ready Bear", 2, 2).id();
                let mut runner = scenario.build();
                let labels = HashMap::from([(attacker, "attacker".to_string())]);
                let (mut discard_reconciler, mut discard_choices) = empty_discard_context();
                declare_attackers(
                    &mut runner,
                    &labels,
                    &json!({
                        "action": "attack",
                        "attackers": ["attacker"],
                        "through": "damage"
                    }),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();

                let mut trigger_targets = VecDeque::new();
                let mut activation_targets = VecDeque::new();
                let mut card_choices = VecDeque::new();
                let mut copy_cast_choices = VecDeque::new();
                let mut graveyard_target_choices = VecDeque::new();
                let mut copy_target_choices = VecDeque::new();
                let mut surveil_choices = VecDeque::new();
                let sideboard_labels = HashMap::new();
                let mut outside_game_choices = VecDeque::new();
                let mut copy_choices = VecDeque::new();
                let mut optional_choices = [VecDeque::new(), VecDeque::new()];
                let mut unless_choices = [VecDeque::new(), VecDeque::new()];
                pass_until_resolved(
                    "attack through damage test",
                    &mut runner,
                    &labels,
                    &mut trigger_targets,
                    &mut activation_targets,
                    &mut card_choices,
                    &mut copy_cast_choices,
                    &mut graveyard_target_choices,
                    &mut copy_target_choices,
                    &mut discard_choices,
                    &mut surveil_choices,
                    &sideboard_labels,
                    &mut outside_game_choices,
                    &mut copy_choices,
                    &mut optional_choices,
                    &mut unless_choices,
                    &mut discard_reconciler,
                    true,
                )
                .unwrap();
                advance_through_combat_damage(
                    "attack through damage test",
                    &mut runner,
                    &labels,
                    &mut trigger_targets,
                    &mut activation_targets,
                    &mut card_choices,
                    &mut copy_cast_choices,
                    &mut graveyard_target_choices,
                    &mut copy_target_choices,
                    &mut discard_choices,
                    &mut surveil_choices,
                    &sideboard_labels,
                    &mut outside_game_choices,
                    &mut copy_choices,
                    &mut optional_choices,
                    &mut unless_choices,
                    &mut discard_reconciler,
                )
                .unwrap();

                assert_eq!(runner.state().phase, Phase::CombatDamage);
                assert!(runner.state().stack.is_empty());
                assert_eq!(
                    runner.state().waiting_for,
                    WaitingFor::Priority {
                        player: PlayerId(0)
                    }
                );
                assert_eq!(runner.state().players[1].life, 18);
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn attack_action_rejects_scripted_blocks_and_unknown_through_modes() {
        let mut scenario = GameScenario::new();
        scenario.at_phase(Phase::PreCombatMain);
        let attacker = scenario.add_creature(PlayerId(0), "Ready Bear", 2, 2).id();
        let mut runner = scenario.build();
        let labels = HashMap::from([(attacker, "attacker".to_string())]);
        let (mut discard_reconciler, mut discard_choices) = empty_discard_context();
        let blocks_error = declare_attackers(
            &mut runner,
            &labels,
            &json!({
                "action": "attack",
                "attackers": ["attacker"],
                "blocks": []
            }),
            &mut discard_reconciler,
            &mut discard_choices,
        )
        .unwrap_err();
        assert!(blocks_error.contains("supports only attackers and through"));
        let through_error = declare_attackers(
            &mut runner,
            &labels,
            &json!({
                "action": "attack",
                "attackers": ["attacker"],
                "through": "combat"
            }),
            &mut discard_reconciler,
            &mut discard_choices,
        )
        .unwrap_err();
        assert!(through_error.contains("attack.through must be \"damage\""));
    }

    #[test]
    fn attack_action_rejects_empty_unknown_and_ambiguous_attackers() {
        let mut scenario = GameScenario::new();
        scenario.at_phase(Phase::PreCombatMain);
        let attacker = scenario.add_creature(PlayerId(0), "Ready Bear", 2, 2).id();
        let labels = HashMap::from([
            (attacker, "same".to_string()),
            (ObjectId(18), "same".to_string()),
        ]);
        let mut runner = scenario.build();
        let (mut discard_reconciler, mut discard_choices) = empty_discard_context();
        assert!(declare_attackers(
            &mut runner,
            &labels,
            &json!({"action": "attack", "attackers": []}),
            &mut discard_reconciler,
            &mut discard_choices,
        )
        .unwrap_err()
        .contains("non-empty"));
        assert!(declare_attackers(
            &mut runner,
            &labels,
            &json!({"action": "attack", "attackers": ["missing"]}),
            &mut discard_reconciler,
            &mut discard_choices,
        )
        .unwrap_err()
        .contains("unknown setup label"));
        assert!(declare_attackers(
            &mut runner,
            &labels,
            &json!({"action": "attack", "attackers": ["same"]}),
            &mut discard_reconciler,
            &mut discard_choices,
        )
        .unwrap_err()
        .contains("ambiguous setup label"));
    }

    #[test]
    fn attack_action_uses_the_current_player_after_turn_advancement() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let attacker = scenario.add_creature(PlayerId(0), "Ready Bear", 2, 2).id();
                scenario.with_library_top(PlayerId(0), &["P0 draw"]);
                scenario.with_library_top(PlayerId(1), &["P1 draw"]);
                let mut runner = scenario.build();
                {
                    let state = runner.state_mut();
                    state.active_player = PlayerId(1);
                    state.priority_player = PlayerId(1);
                    state.waiting_for = WaitingFor::Priority {
                        player: PlayerId(1),
                    };
                }
                let (mut discard_reconciler, mut discard_choices) = empty_discard_context();
                advance_to_next_turn(
                    &mut runner,
                    true,
                    &HashMap::new(),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();
                assert_eq!(runner.state().active_player, PlayerId(0));

                let labels = HashMap::from([(attacker, "attacker".to_string())]);
                declare_attackers(
                    &mut runner,
                    &labels,
                    &json!({"action": "attack", "attackers": ["attacker"]}),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();
                assert_eq!(runner.state().phase, Phase::DeclareAttackers);
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn targeted_cast_rejects_pending_trigger_target_groups() {
        let action = json!({"action": "cast", "targets": ["artifact"]});
        let groups = VecDeque::from([TriggerTargetGroup::from(vec![Some(ObjectId(17))])]);

        assert_eq!(
            ensure_targeted_cast_has_no_pending_trigger_targets(&action, &groups),
            Err(
                "trigger_targets queued before a targeted cast; list them after the action whose trigger consumes them"
                    .to_string()
            )
        );
    }

    #[test]
    fn targeted_cast_order_guard_allows_untargeted_actions_or_empty_queue() {
        let groups = VecDeque::from([TriggerTargetGroup::from(vec![Some(ObjectId(17))])]);
        assert!(ensure_targeted_cast_has_no_pending_trigger_targets(
            &json!({"action": "cast"}),
            &groups
        )
        .is_ok());

        assert!(ensure_targeted_cast_has_no_pending_trigger_targets(
            &json!({"action": "cast", "targets": ["artifact"]}),
            &VecDeque::new()
        )
        .is_ok());
    }

    #[test]
    fn card_choice_prompt_fails_when_queue_is_empty() {
        assert!(take_card_choice_group(&mut VecDeque::new(), "test prompt")
            .unwrap_err()
            .contains("empty choose_cards queue"));
    }

    #[test]
    fn collect_evidence_choice_uses_fail_closed_card_choice_queue() {
        assert!(take_card_choice_group(&mut VecDeque::new(), "test prompt")
            .unwrap_err()
            .contains("empty choose_cards queue"));

        let mut empty_group = VecDeque::from([Vec::new()]);
        assert!(take_card_choice_group(&mut empty_group, "test prompt")
            .unwrap()
            .is_empty());
        assert!(empty_group.is_empty());

        let groups = VecDeque::from(vec![vec![ObjectId(17)]]);
        assert!(ensure_card_choice_groups_consumed(&groups)
            .unwrap_err()
            .contains("unconsumed"));
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

        assert!(ensure_card_choice_groups_consumed(&groups)
            .unwrap_err()
            .contains("unconsumed"));
        assert!(ensure_card_choice_groups_consumed(&VecDeque::new()).is_ok());
    }

    #[test]
    fn discard_choices_are_player_scoped_and_fail_closed() {
        let first = ObjectId(17);
        let second = ObjectId(18);
        let labels = HashMap::from([
            (first, "p0.hand.nonland".to_string()),
            (second, "p0.drawn.land".to_string()),
        ]);
        let choice = DiscardScriptChoice {
            cards: vec![first, second],
        };
        let offered = [first, second];

        let mut wrong_player = [VecDeque::new(), VecDeque::from([choice.clone()])];
        assert!(take_discard_choice_group(
            &mut wrong_player,
            PlayerId(0),
            &offered,
            2,
            false,
            &labels,
        )
        .unwrap_err()
        .contains("no choose_discard entry"));
        assert!(ensure_discard_choices_consumed(&wrong_player)
            .unwrap_err()
            .contains("unconsumed"));

        let mut missing = [VecDeque::new(), VecDeque::new()];
        assert!(
            take_discard_choice_group(&mut missing, PlayerId(0), &offered, 2, false, &labels,)
                .unwrap_err()
                .contains("no choose_discard entry")
        );

        let mut extra = [VecDeque::from([choice.clone(), choice]), VecDeque::new()];
        assert_eq!(
            take_discard_choice_group(&mut extra, PlayerId(0), &offered, 2, false, &labels,)
                .unwrap(),
            offered
        );
        assert!(ensure_discard_choices_consumed(&extra)
            .unwrap_err()
            .contains("unconsumed"));
    }

    #[test]
    fn forced_discard_returned_by_an_act_consumes_the_script() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let spell = scenario
                    .add_spell_to_hand_from_oracle(
                        PlayerId(0),
                        "Mind Rot",
                        false,
                        "Target player discards two cards.",
                    )
                    .with_mana_cost(engine::types::mana::ManaCost::generic(0))
                    .id();
                let first = scenario.add_card_to_hand(PlayerId(1), "Discarded one");
                let second = scenario.add_card_to_hand(PlayerId(1), "Discarded two");
                let mut runner = scenario.build();
                let targets = [Some(TargetRef::Player(PlayerId(1)))];
                let committed = runner.cast(spell).target_slot_selections(&targets).commit();
                drop(committed);

                let labels = HashMap::from([
                    (first, "p1.hand.one".to_string()),
                    (second, "p1.hand.two".to_string()),
                ]);
                let mut choices = [
                    VecDeque::new(),
                    VecDeque::from([DiscardScriptChoice {
                        cards: vec![first, second],
                    }]),
                ];
                let mut reconciler = DiscardChoiceReconciler::default();
                reconciler
                    .act(&mut runner, GameAction::PassPriority, &mut choices, &labels)
                    .expect("P0 passes priority");
                reconciler
                    .act(&mut runner, GameAction::PassPriority, &mut choices, &labels)
                    .expect("P1 passes and the exact-count discard resolves");

                assert!(ensure_discard_choices_consumed(&choices).is_ok());
                assert_eq!(runner.state().objects[&first].zone, Zone::Graveyard);
                assert_eq!(runner.state().objects[&second].zone, Zone::Graveyard);
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn interactive_discard_does_not_match_the_next_forced_discard_script() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let interactive_spell = scenario
                    .add_spell_to_hand_from_oracle(
                        PlayerId(0),
                        "Interactive discard",
                        false,
                        "Target player discards a card.",
                    )
                    .with_mana_cost(engine::types::mana::ManaCost::generic(0))
                    .id();
                let forced_spell = scenario
                    .add_spell_to_hand_from_oracle(
                        PlayerId(0),
                        "Forced discard",
                        false,
                        "Target player discards two cards.",
                    )
                    .with_mana_cost(engine::types::mana::ManaCost::generic(0))
                    .id();
                let interactive_card = scenario.add_card_to_hand(PlayerId(1), "Interactive card");
                let forced_card_one = scenario.add_card_to_hand(PlayerId(1), "Forced card one");
                let forced_card_two = scenario.add_card_to_hand(PlayerId(1), "Forced card two");
                let mut runner = scenario.build();
                let targets = [Some(TargetRef::Player(PlayerId(1)))];
                let labels = HashMap::from([
                    (interactive_card, "p1.interactive".to_string()),
                    (forced_card_one, "p1.forced.one".to_string()),
                    (forced_card_two, "p1.forced.two".to_string()),
                ]);
                let mut choices = [
                    VecDeque::new(),
                    VecDeque::from([
                        DiscardScriptChoice {
                            cards: vec![interactive_card],
                        },
                        DiscardScriptChoice {
                            cards: vec![forced_card_one, forced_card_two],
                        },
                    ]),
                ];
                let mut reconciler = DiscardChoiceReconciler::default();

                let committed = runner
                    .cast(interactive_spell)
                    .target_slot_selections(&targets)
                    .commit();
                drop(committed);
                pass_until_resolved_with_discard_choices(
                    &mut runner,
                    &labels,
                    &mut choices,
                    &mut reconciler,
                )
                .expect("interactive discard leaves the next scripted set queued");

                assert_eq!(choices[1].len(), 1);
                assert_eq!(
                    runner.state().objects[&interactive_card].zone,
                    Zone::Graveyard
                );
                assert_eq!(runner.state().objects[&forced_card_one].zone, Zone::Hand);
                assert_eq!(runner.state().objects[&forced_card_two].zone, Zone::Hand);

                let committed = runner
                    .cast(forced_spell)
                    .target_slot_selections(&targets)
                    .commit();
                drop(committed);
                pass_until_resolved_with_discard_choices(
                    &mut runner,
                    &labels,
                    &mut choices,
                    &mut reconciler,
                )
                .expect("the forced discard consumes its own scripted set");

                assert!(ensure_discard_choices_consumed(&choices).is_ok());
                assert_eq!(
                    runner.state().objects[&forced_card_one].zone,
                    Zone::Graveyard
                );
                assert_eq!(
                    runner.state().objects[&forced_card_two].zone,
                    Zone::Graveyard
                );
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn forced_discard_events_consume_an_exact_scripted_set() {
        let first = ObjectId(17);
        let second = ObjectId(18);
        let labels = HashMap::from([
            (first, "p0.hand.nonland".to_string()),
            (second, "p0.drawn.land".to_string()),
        ]);
        let mut choices = [
            VecDeque::from([DiscardScriptChoice {
                cards: vec![first, second],
            }]),
            VecDeque::new(),
        ];
        let mut pending_events = Vec::new();

        consume_resolved_discard_choices(
            &mut pending_events,
            vec![
                GameEvent::Discarded {
                    player_id: PlayerId(0),
                    object_id: second,
                    source_id: None,
                },
                GameEvent::Discarded {
                    player_id: PlayerId(0),
                    object_id: first,
                    source_id: None,
                },
                GameEvent::StackResolved {
                    object_id: ObjectId(19),
                },
            ],
            &mut choices,
            &labels,
        )
        .unwrap();

        assert!(ensure_discard_choices_consumed(&choices).is_ok());
    }

    #[test]
    fn forced_discard_events_reject_a_different_scripted_set() {
        let first = ObjectId(17);
        let expected = ObjectId(18);
        let actual = ObjectId(19);
        let labels = HashMap::from([
            (first, "p0.hand.nonland".to_string()),
            (expected, "p0.drawn.land".to_string()),
            (actual, "p0.extra".to_string()),
        ]);
        let mut choices = [
            VecDeque::from([DiscardScriptChoice {
                cards: vec![first, expected],
            }]),
            VecDeque::new(),
        ];
        let mut pending_events = Vec::new();

        let error = consume_resolved_discard_choices(
            &mut pending_events,
            vec![
                GameEvent::Discarded {
                    player_id: PlayerId(0),
                    object_id: first,
                    source_id: None,
                },
                GameEvent::Discarded {
                    player_id: PlayerId(0),
                    object_id: actual,
                    source_id: None,
                },
                GameEvent::StackResolved {
                    object_id: ObjectId(20),
                },
            ],
            &mut choices,
            &labels,
        )
        .unwrap_err();

        assert!(error.contains("scripted [p0.drawn.land, p0.hand.nonland]"));
        assert!(error.contains("actually discarded P0 [p0.extra, p0.hand.nonland]"));
    }

    #[test]
    fn forced_discard_leaves_another_players_script_queued() {
        let discarded = ObjectId(17);
        let scripted = ObjectId(18);
        let labels = HashMap::from([
            (discarded, "p0.discarded".to_string()),
            (scripted, "p1.discarded".to_string()),
        ]);
        let mut choices = [
            VecDeque::new(),
            VecDeque::from([DiscardScriptChoice {
                cards: vec![scripted],
            }]),
        ];
        let mut pending_events = Vec::new();

        consume_resolved_discard_choices(
            &mut pending_events,
            vec![
                GameEvent::Discarded {
                    player_id: PlayerId(0),
                    object_id: discarded,
                    source_id: None,
                },
                GameEvent::StackResolved {
                    object_id: ObjectId(19),
                },
            ],
            &mut choices,
            &labels,
        )
        .unwrap();

        assert_eq!(choices[1].len(), 1);
        assert_eq!(choices[1].front().unwrap().cards, vec![scripted]);
    }

    #[test]
    fn forced_discard_script_remains_unconsumed_when_resolution_discards_nothing() {
        let card = ObjectId(17);
        let mut choices = [
            VecDeque::from([DiscardScriptChoice { cards: vec![card] }]),
            VecDeque::new(),
        ];
        let mut pending_events = Vec::new();

        consume_resolved_discard_choices(
            &mut pending_events,
            vec![GameEvent::StackResolved {
                object_id: ObjectId(18),
            }],
            &mut choices,
            &HashMap::new(),
        )
        .unwrap();

        assert!(ensure_discard_choices_consumed(&choices)
            .unwrap_err()
            .contains("unconsumed"));
    }

    #[test]
    fn discard_choice_script_requires_player_and_unique_labels() {
        let id = ObjectId(17);
        let labels = HashMap::from([(id, "p0.hand.card".to_string())]);

        assert!(
            parse_discard_choice(&json!({"player": "P2", "cards": []}), &labels)
                .unwrap_err()
                .contains("player must be P0 or P1")
        );
        assert!(parse_discard_choice(
            &json!({"player": "P0", "cards": ["p0.hand.card", "p0.hand.card"]}),
            &labels,
        )
        .unwrap_err()
        .contains("cannot repeat"));
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
                let mut groups = VecDeque::from(vec![TriggerTargetGroup::from(vec![Some(first_target)]), TriggerTargetGroup::from(vec![Some(second_target)])]);
                let (mut discard_reconciler, mut discard_choices) = empty_discard_context();

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
                apply_trigger_target_group(
                    &mut runner,
                    &labels,
                    &mut groups,
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();
                let (prompt_source_2, prompt_entry_2) = prompt_identity(&runner);
                assert!([first_source, second_source].contains(&prompt_source_2));
                assert_ne!(prompt_source_1, prompt_source_2);
                assert_ne!(prompt_entry_1, prompt_entry_2);
                apply_trigger_target_group(
                    &mut runner,
                    &labels,
                    &mut groups,
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();

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
                let (mut discard_reconciler, mut discard_choices) = empty_discard_context();
                let mut activation_targets = VecDeque::new();

                activate(
                    &mut runner,
                    &labels,
                    &json!({
                            "source": "source",
                            "ability": "{T}:",
                        "targets": ["target"]
                    }),
                    0,
                    false,
                    &mut Vec::new(),
                    &mut activation_targets,
                    &mut VecDeque::new(),
                    &json!({}),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();

                assert_eq!(runner.state().stack.len(), stack_size + 1);
                assert!(activation_targets.is_empty());
                assert_eq!(
                    runner
                        .state()
                        .stack
                        .last()
                        .and_then(|entry| entry.ability())
                        .unwrap()
                        .targets,
                    vec![TargetRef::Object(target)]
                );
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
    fn alternative_cost_decisions_select_the_original_or_alternative_branch() {
        use engine::types::ability::AbilityCost;
        use engine::types::mana::ManaCost;

        let base = AbilityCost::Mana {
            cost: ManaCost::generic(1),
        };
        let alternative = AbilityCost::Mana {
            cost: ManaCost::generic(0),
        };
        let costs = vec![base.clone(), alternative];

        assert_eq!(
            select_activation_cost_branch(&costs, Some(&base), AlternativeCostDecision::Accept),
            Ok(1)
        );
        assert_eq!(
            select_activation_cost_branch(&costs, Some(&base), AlternativeCostDecision::Decline),
            Ok(0)
        );
        assert!(select_activation_cost_branch(
            &[base.clone()],
            Some(&base),
            AlternativeCostDecision::Accept
        )
        .is_err());

        let zero = AbilityCost::Mana {
            cost: ManaCost::generic(0),
        };
        let equal_costs = vec![zero.clone(), zero.clone()];
        assert_eq!(
            select_activation_cost_branch(
                &equal_costs,
                Some(&zero),
                AlternativeCostDecision::Accept
            ),
            Ok(1)
        );
        assert_eq!(
            select_activation_cost_branch(
                &equal_costs,
                Some(&zero),
                AlternativeCostDecision::Decline
            ),
            Ok(0)
        );
    }

    #[test]
    fn alternative_cost_accepts_the_first_offered_branch_when_base_is_unpayable() {
        use engine::types::ability::AbilityCost;
        use engine::types::mana::ManaCost;

        let base = AbilityCost::Mana {
            cost: ManaCost::generic(1),
        };
        let only_payable_branch = AbilityCost::Mana {
            cost: ManaCost::generic(0),
        };

        assert_eq!(
            select_activation_cost_branch(
                std::slice::from_ref(&only_payable_branch),
                Some(&base),
                AlternativeCostDecision::Accept
            ),
            Ok(0)
        );
        assert!(select_activation_cost_branch(
            std::slice::from_ref(&only_payable_branch),
            Some(&base),
            AlternativeCostDecision::Decline
        )
        .is_err());
        assert!(
            select_activation_cost_branch(&[], Some(&base), AlternativeCostDecision::Accept)
                .is_err()
        );
    }

    #[test]
    fn alternative_cost_field_and_prompt_must_appear_together() {
        assert!(ensure_activation_cost_prompt_matches_field(None, false).is_ok());
        assert!(ensure_activation_cost_prompt_matches_field(
            Some(AlternativeCostDecision::Accept),
            true
        )
        .is_ok());
        assert!(ensure_activation_cost_prompt_matches_field(None, true).is_err());
        assert!(ensure_activation_cost_prompt_matches_field(
            Some(AlternativeCostDecision::Accept),
            false
        )
        .is_err());
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
    fn reach_life_assertion_does_not_require_a_card() {
        let runner = GameScenario::new().build();
        let reach = json!({"player": "P0", "life": 20});

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

                let (mut discard_reconciler, mut discard_choices) = empty_discard_context();
                advance_to_next_turn(
                    &mut runner,
                    false,
                    &HashMap::new(),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();

                assert_ne!(runner.state().turn_number, turn);
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn non_identical_triggers_without_a_scripted_order_fail_the_run() {
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

                let (mut discard_reconciler, mut discard_choices) = empty_discard_context();
                advance_to_next_turn(
                    &mut runner,
                    true,
                    &HashMap::new(),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();

                assert_ne!(runner.state().turn_number, turn);
                assert_eq!(runner.state().active_player, PlayerId(1));
                assert_eq!(runner.state().phase, Phase::PreCombatMain);
                assert_eq!(runner.state().players[1].hand.len(), 1);

                let unscripted = advance_to_next_turn(
                    &mut runner,
                    true,
                    &HashMap::new(),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap_err();
                assert!(unscripted.contains("unscripted order for non-identical triggers of P0"));
                discard_reconciler
                    .trigger_orders
                    .push_back(TriggerOrderChoice {
                        player: PlayerId(1),
                        resolve: vec!["whenever you draw".to_string(), "whenever".to_string()],
                    });
                let wrong_player = discard_reconciler
                    .order_pending_triggers(&mut runner, &mut Vec::new())
                    .unwrap_err();
                assert!(wrong_player.contains("scripted for P1 but P0 is ordering"));
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

                let (mut discard_reconciler, mut discard_choices) = empty_discard_context();
                advance_to_next_turn(
                    &mut runner,
                    true,
                    &HashMap::new(),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();

                assert_ne!(runner.state().turn_number, turn);
                assert_eq!(runner.state().active_player, PlayerId(1));
                assert_eq!(runner.state().phase, Phase::PreCombatMain);
                assert_eq!(runner.state().players[1].hand.len(), 1);

                // The two draw triggers differ, so their order must be scripted.
                discard_reconciler
                    .trigger_orders
                    .push_back(TriggerOrderChoice {
                        player: PlayerId(0),
                        resolve: vec![
                            "whenever you draw a card, you gain 3".to_string(),
                            "whenever you draw a card, you gain 2".to_string(),
                        ],
                    });
                advance_to_next_turn(
                    &mut runner,
                    true,
                    &HashMap::new(),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();

                assert_eq!(runner.state().active_player, PlayerId(0));
                assert_eq!(runner.state().phase, Phase::PreCombatMain);
                assert_eq!(runner.state().players[0].hand.len(), 1);
                assert_eq!(runner.state().players[0].life, 25);
                assert!(discard_reconciler.trigger_orders.is_empty());
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

                let (mut discard_reconciler, mut discard_choices) = empty_discard_context();
                advance_to_next_turn(
                    &mut runner,
                    false,
                    &HashMap::new(),
                    &mut discard_reconciler,
                    &mut discard_choices,
                )
                .unwrap();

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

        assert!(matches_reach_characteristics(
            object,
            &json!({"counters": {"ingenuity": 2}}),
            &HashMap::new()
        )
        .unwrap());
        assert!(matches_reach_characteristics(
            object,
            &json!({"counters": {"energy": 0}}),
            &HashMap::new()
        )
        .unwrap());
        assert!(!matches_reach_characteristics(
            object,
            &json!({"counters": {"ingenuity": 0}}),
            &HashMap::new()
        )
        .unwrap());
    }

    #[test]
    fn reach_rejects_unknown_fields_including_loyalty() {
        let runner = GameScenario::new().build();
        let reach = json!({
            "zone": "battlefield",
            "card": "Jace",
            "loyalty": 4
        });

        assert_eq!(
            check_reach(runner.state(), &HashMap::new(), Some(&reach)),
            Err("unsupported reach field loyalty".to_string())
        );
    }

    #[test]
    fn reach_solved_is_case_only_and_requires_boolean() {
        let mut scenario = GameScenario::new();
        let creature = scenario.add_creature(PlayerId(0), "Reach Bear", 2, 2).id();
        let runner = scenario.build();
        let object = &runner.state().objects[&creature];

        assert_eq!(
            matches_reach_characteristics(object, &json!({"solved": false}), &HashMap::new()),
            Err("reach.solved is only valid for Case permanents".to_string())
        );
        assert_eq!(
            matches_reach_characteristics(object, &json!({"solved": "false"}), &HashMap::new()),
            Err("reach.solved must be a boolean".to_string())
        );
        assert_eq!(
            check_reach(
                runner.state(),
                &HashMap::new(),
                Some(&json!({"zone": "graveyard", "card": "Case", "solved": false}))
            ),
            Err("reach.solved is only valid for battlefield Cases".to_string())
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

#[cfg(test)]
mod outside_game_choice_tests {
    use super::*;
    use engine::types::game_state::OutsideGameChoiceEntry;
    use engine::types::CardFace;
    use serde_json::json;

    #[test]
    fn sideboard_deck_entry_preserves_database_card_attributes() {
        let path =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../phase/client/public/card-data.json");
        let db = CardDatabase::from_export(&path).unwrap();
        let expected = db.get_face_by_name("Giant Growth").unwrap();

        let entry = sideboard_deck_entry("Giant Growth", &db).unwrap();

        assert_eq!(
            serde_json::to_value(&entry.card).unwrap(),
            serde_json::to_value(expected).unwrap()
        );
    }

    #[test]
    fn outside_game_script_requires_a_player_and_card_labels() {
        assert!(
            parse_outside_game_choice(&json!({"player": "P2", "cards": ["card"]}))
                .unwrap_err()
                .contains("player must be P0 or P1")
        );
        assert!(
            parse_outside_game_choice(&json!({"player": "P0", "cards": [1]}))
                .unwrap_err()
                .contains("card entries must be labels")
        );
        assert!(
            parse_outside_game_choice(&json!({"player": "P0", "accept": true, "cards": []}))
                .unwrap()
                .labels
                .is_empty()
        );
        assert!(parse_outside_game_choice(
            &json!({"player": "P0", "accept": false, "cards": ["sideboard.card"]})
        )
        .is_err());

        let mut choices = VecDeque::from([OutsideGameScriptChoice {
            player: PlayerId(0),
            accept: true,
            labels: vec!["sideboard.card".to_string()],
        }]);
        assert!(ensure_outside_game_choices_consumed(&choices)
            .unwrap_err()
            .contains("unconsumed"));
        choices.pop_front();
        assert!(ensure_outside_game_choices_consumed(&choices).is_ok());
    }

    #[test]
    fn outside_game_labels_must_map_to_offered_sideboard_slots() {
        let choice = OutsideGameScriptChoice {
            player: PlayerId(1),
            accept: true,
            labels: vec!["sideboard.divination".to_string()],
        };
        let labels = HashMap::from([(
            "sideboard.divination".to_string(),
            (PlayerId(0), 1, "Divination".to_string()),
        )]);
        let offered = [OutsideGameChoiceEntry {
            source: OutsideGameChoiceSource::Sideboard {
                sideboard_index: 1,
                card: CardFace::default(),
            },
            count: 1,
            name: "Divination".to_string(),
        }];

        assert_eq!(
            sideboard_choice_indices(&choice, &labels, PlayerId(0), &offered).unwrap(),
            vec![OutsideGameSelection::Sideboard { sideboard_index: 1 }]
        );
        assert!(sideboard_choice_indices(
            &choice,
            &labels,
            PlayerId(0),
            &[OutsideGameChoiceEntry {
                source: OutsideGameChoiceSource::Sideboard {
                    sideboard_index: 2,
                    card: CardFace::default(),
                },
                count: 1,
                name: "Opt".to_string(),
            }]
        )
        .unwrap_err()
        .contains("not offered"));
        assert!(
            sideboard_choice_indices(&choice, &labels, PlayerId(1), &offered)
                .unwrap_err()
                .contains("belongs to another player")
        );
    }

    #[test]
    fn mana_choice_sources_must_be_unique_and_configured_for_the_caster() {
        let oracle_specific = parse_mana_choice(&json!({
            "player": "P0",
            "phase_sources": ["phase.island"],
            "forge_sources": ["forge.island"]
        }))
        .unwrap();
        assert_eq!(oracle_specific.sources, vec!["phase.island"]);
        assert!(parse_mana_choice(&json!({
            "player": "P0",
            "phase_sources": ["phase.island"]
        }))
        .is_err());
        assert!(parse_mana_choice(&json!({
            "player": "P0",
            "sources": ["shared.island"],
            "phase_sources": ["phase.island"],
            "forge_sources": ["forge.island"]
        }))
        .is_err());

        let choice = parse_mana_choice(&json!({
            "player": "P0",
            "sources": ["mana.white", "mana.blue"]
        }))
        .unwrap();
        let mut choices = VecDeque::from([choice]);
        let spec = json!({
            "setup": {
                "P0": {
                    "mana_pool": [
                        {"color": "W", "source": "mana.white"},
                        {"color": "U", "source": "mana.blue"}
                    ]
                }
            }
        });

        let choice = consume_mana_choice(&mut choices, PlayerId(0), &spec)
            .unwrap()
            .unwrap();
        assert!(ensure_mana_choices_consumed(&choices).is_ok());
        let before = HashMap::from([
            (engine::types::mana::ManaPipId(1), ObjectId(1)),
            (engine::types::mana::ManaPipId(2), ObjectId(2)),
        ]);
        let labels = HashMap::from([
            (ObjectId(1), "mana.white".to_string()),
            (ObjectId(2), "mana.blue".to_string()),
        ]);
        assert!(verify_mana_choice_spent(&choice, &before, &HashMap::new(), &labels).is_ok());
        assert!(verify_mana_choice_spent(
            &choice,
            &before,
            &HashMap::from([(engine::types::mana::ManaPipId(1), ObjectId(1))]),
            &labels
        )
        .unwrap_err()
        .contains("source mismatch"));
        assert!(parse_mana_choice(&json!({
            "player": "P0",
            "sources": ["mana.white", "mana.white"]
        }))
        .is_err());
        assert!(parse_mana_choice(&json!({"player": "P0", "sources": []})).is_err());
    }
}

#[cfg(test)]
mod surveil_script_choice_tests {
    use super::*;

    #[test]
    fn pass_until_resolved_consumes_scripted_surveil_choice() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let mut scenario = GameScenario::new();
                scenario.at_phase(Phase::PreCombatMain);
                let top_card = scenario.add_card_to_library_top(PlayerId(0), "Top Card");
                let spell = scenario
                    .add_spell_to_hand_from_oracle(PlayerId(0), "Surveil Spell", true, "Surveil 1.")
                    .id();
                let mut runner = scenario.build();
                runner.cast(spell).commit();

                let labels = HashMap::from([(top_card, "library.top".to_string())]);
                let mut surveil_choices = VecDeque::from([parse_surveil_choice(
                    &json!({"player": "P0", "cards": ["library.top"]}),
                    &labels,
                )
                .unwrap()]);
                let mut trigger_targets = VecDeque::new();
                let mut activation_targets = VecDeque::new();
                let mut card_choices = VecDeque::new();
                let mut copy_cast_choices = VecDeque::new();
                let mut graveyard_target_choices = VecDeque::new();
                let mut copy_target_choices = VecDeque::new();
                let mut discard_choices = [VecDeque::new(), VecDeque::new()];
                let sideboard_labels = HashMap::new();
                let mut outside_game_choices = VecDeque::new();
                let mut copy_choices = VecDeque::new();
                let mut optional_choices = [VecDeque::new(), VecDeque::new()];
                let mut unless_choices = [VecDeque::new(), VecDeque::new()];
                let mut discard_reconciler = DiscardChoiceReconciler::default();

                pass_until_resolved(
                    "surveil script test",
                    &mut runner,
                    &labels,
                    &mut trigger_targets,
                    &mut activation_targets,
                    &mut card_choices,
                    &mut copy_cast_choices,
                    &mut graveyard_target_choices,
                    &mut copy_target_choices,
                    &mut discard_choices,
                    &mut surveil_choices,
                    &sideboard_labels,
                    &mut outside_game_choices,
                    &mut copy_choices,
                    &mut optional_choices,
                    &mut unless_choices,
                    &mut discard_reconciler,
                    false,
                )
                .unwrap();

                assert!(surveil_choices.is_empty());
                assert_eq!(runner.state().objects[&top_card].zone, Zone::Graveyard);
            })
            .unwrap()
            .join()
            .unwrap();
    }

    #[test]
    fn scripted_surveil_cards_resolve_by_label_and_must_be_consumed() {
        let labels = HashMap::from([(ObjectId(1), "top-card".to_string())]);
        let choice =
            parse_surveil_choice(&json!({"player": "P0", "cards": ["top-card"]}), &labels).unwrap();

        assert_eq!(choice.player, PlayerId(0));
        assert_eq!(choice.graveyard, vec![ObjectId(1)]);
        assert!(ensure_surveil_choices_consumed(&VecDeque::new()).is_ok());
        assert!(ensure_surveil_choices_consumed(&VecDeque::from([choice]))
            .unwrap_err()
            .contains("unconsumed"));
        let other_player_choice =
            parse_surveil_choice(&json!({"player": "P1", "cards": ["top-card"]}), &labels).unwrap();
        assert_eq!(other_player_choice.player, PlayerId(1));
    }
}

#[cfg(test)]
mod combat_damage_card_choice_tests {
    use super::*;

    fn damage_step_choice_runner() -> (GameRunner, HashMap<ObjectId, String>, ObjectId, ObjectId) {
        let mut scenario = GameScenario::new();
        scenario.at_phase(Phase::CombatDamage);
        let first = scenario.add_card_to_library_top(PlayerId(0), "First Choice");
        let second = scenario.add_card_to_library_top(PlayerId(0), "Second Choice");
        let spell = scenario
            .add_spell_to_hand_from_oracle(
                PlayerId(0),
                "Damage Step Choice",
                true,
                "Look at the top two cards of your library. Put one of them into your hand and the rest into your graveyard.",
            )
            .id();
        let mut runner = scenario.build();
        runner.cast(spell).commit();
        let labels = HashMap::from([
            (first, "top.first".to_string()),
            (second, "top.second".to_string()),
        ]);
        (runner, labels, first, second)
    }

    fn resolve_damage_step_choice(
        runner: &mut GameRunner,
        labels: &HashMap<ObjectId, String>,
        card_choices: &mut VecDeque<Vec<ObjectId>>,
    ) -> Result<(), String> {
        let mut trigger_targets = VecDeque::new();
        let mut activation_targets = VecDeque::new();
        let mut copy_cast_choices = VecDeque::new();
        let mut graveyard_target_choices = VecDeque::new();
        let mut copy_target_choices = VecDeque::new();
        let mut discard_choices = [VecDeque::new(), VecDeque::new()];
        let mut surveil_choices = VecDeque::new();
        let sideboard_labels = HashMap::new();
        let mut outside_game_choices = VecDeque::new();
        let mut copy_choices = VecDeque::new();
        let mut optional_choices = [VecDeque::new(), VecDeque::new()];
        let mut unless_choices = [VecDeque::new(), VecDeque::new()];
        let mut discard_reconciler = DiscardChoiceReconciler::default();

        pass_until_resolved(
            "combat-damage card choice test",
            runner,
            labels,
            &mut trigger_targets,
            &mut activation_targets,
            card_choices,
            &mut copy_cast_choices,
            &mut graveyard_target_choices,
            &mut copy_target_choices,
            &mut discard_choices,
            &mut surveil_choices,
            &sideboard_labels,
            &mut outside_game_choices,
            &mut copy_choices,
            &mut optional_choices,
            &mut unless_choices,
            &mut discard_reconciler,
            true,
        )
    }

    #[test]
    fn damage_step_choice_consumes_choose_cards_and_empty_queue_fails_closed() {
        std::thread::Builder::new()
            .stack_size(16 * 1024 * 1024)
            .spawn(|| {
                let (mut runner, labels, first, second) = damage_step_choice_runner();
                let mut choices = VecDeque::from([vec![first]]);

                resolve_damage_step_choice(&mut runner, &labels, &mut choices).unwrap();

                assert!(choices.is_empty());
                assert_eq!(runner.state().objects[&first].zone, Zone::Hand);
                assert_eq!(runner.state().objects[&second].zone, Zone::Graveyard);

                let (mut runner, labels, _, _) = damage_step_choice_runner();
                let error = resolve_damage_step_choice(&mut runner, &labels, &mut VecDeque::new())
                    .unwrap_err();
                assert!(error.contains("empty choose_cards queue"));
            })
            .unwrap()
            .join()
            .unwrap();
    }
}

#[cfg(test)]
mod trigger_order_tests {
    use super::*;

    fn texts(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[test]
    fn resolve_order_is_reversed_into_stack_placement() {
        let pending = texts(&["whenever you attack, a", "whenever ~ attacks, b", "at c"]);
        assert_eq!(
            trigger_placement_order(
                &texts(&["whenever ~ attacks", "at c", "whenever you"]),
                &pending
            ),
            Ok(vec![0, 2, 1])
        );
    }

    #[test]
    fn identical_descriptions_are_each_used_once() {
        let pending = texts(&["draw a card", "gain 1 life", "draw a card"]);
        assert_eq!(
            trigger_placement_order(&texts(&["draw", "gain", "draw"]), &pending),
            Ok(vec![0, 1, 2])
        );
    }

    #[test]
    fn wrong_count_missing_and_ambiguous_prefixes_are_rejected() {
        let pending = texts(&["whenever you attack", "whenever you discard"]);
        let error =
            |resolve: &[&str]| trigger_placement_order(&texts(resolve), &pending).unwrap_err();
        assert!(error(&["whenever you attack"]).contains("lists 1 abilities but 2"));
        assert!(error(&["whenever you attack", "when you do"]).contains("matches no trigger"));
        assert!(error(&["whenever you attack", "whenever you"]).contains("is ambiguous"));
        let duplicated = texts(&["draw a card", "draw a card", "draw two cards"]);
        assert!(
            trigger_placement_order(&texts(&["draw a", "draw a", "draw"]), &duplicated)
                .unwrap_err()
                .contains("is ambiguous")
        );
        assert!(
            error(&["whenever you attack", "whenever you attack"]).contains("matches no trigger")
        );
    }

    #[test]
    fn order_triggers_is_parsed_and_validated() {
        let parsed = parse_trigger_order(&json!({
            "action": "order_triggers",
            "player": "P1",
            "resolve": ["Whenever {this} attacks", "Put a"],
        }));
        assert_eq!(
            parsed,
            Ok(Some(TriggerOrderChoice {
                player: PlayerId(1),
                resolve: texts(&["whenever ~ attacks", "put a"]),
            }))
        );
        let unprompted = json!({
            "action": "order_triggers", "player": "P0", "resolve": ["a", "b"],
            "only": "xmage",
        });
        assert_eq!(parse_trigger_order(&unprompted), Ok(None));
        let phase_only = json!({
            "action": "order_triggers", "player": "P0", "resolve": ["a", "b"],
            "only": "phase",
        });
        assert!(matches!(parse_trigger_order(&phase_only), Ok(Some(_))));
        for invalid in [
            json!({"action": "order_triggers", "player": "P2", "resolve": ["a", "b"]}),
            json!({"action": "order_triggers", "player": "P0", "resolve": ["a"]}),
            json!({"action": "order_triggers", "player": "P0", "resolve": ["a", " "]}),
            json!({"action": "order_triggers", "player": "P0", "resolve": ["a", "b"], "x": 1}),
            json!({"action": "order_triggers", "player": "P0", "resolve": ["a", "b"],
                "only": "forge"}),
        ] {
            assert!(parse_trigger_order(&invalid).is_err(), "{invalid}");
        }
    }

    #[test]
    fn leftover_trigger_orders_fail_the_script() {
        assert_eq!(ensure_trigger_orders_consumed(&VecDeque::new()), Ok(()));
        let leftover = VecDeque::from([TriggerOrderChoice {
            player: PlayerId(0),
            resolve: texts(&["a", "b"]),
        }]);
        assert!(ensure_trigger_orders_consumed(&leftover)
            .unwrap_err()
            .contains("1 order_triggers entry(s) were unconsumed"));
    }
}
