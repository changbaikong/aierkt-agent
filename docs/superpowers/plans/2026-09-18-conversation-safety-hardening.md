# Conversation Safety Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the verified routing, intent continuation, conversation cleanup, opening-memory ledger, and steward-event boundary gaps without changing unrelated chat behavior.

**Architecture:** Keep authoritative decisions in deterministic server code. LLMs may fill parameters, select a whitelisted opening cue, or phrase a sentence, but they may not choose execution strength, bypass filesystem scope, or self-report which memory was used. Conversation lifecycle cleanup owns both durable state and in-process caches.

**Tech Stack:** Python 3.11+, asyncio, FastAPI, SQLite, pytest, React 18, Node test runner.

## Global Constraints

- Work in the existing dirty tree; never revert or overwrite unrelated user changes.
- Follow TDD for every task: add the regression test, observe the expected failure, implement the minimum fix, then rerun the focused suite.
- Do not change public HTTP response shapes or frontend event names.
- Do not add a new dependency, database, background service, or migration framework.
- Opening selector and narrator calls receive `tools=None`; neither may execute actions.
- A personalized opening is never returned before its server-selected cue is recorded successfully.
- Preserve existing visit-id idempotency, 7/30-day cue cooldowns, two-plain-greeting frequency gate, intent TTL, and intent turn limit.
- Do not commit unless the assigning user explicitly requests commits. The commit commands below are optional checkpoints.

---

### Task 1: Constrain LLM fallback routing to the matched rule

**Files:**
- Modify: `agent_core/routing.py`
- Test: `tests/test_routing.py`
- Test: `tests/test_route_guardrails.py`
- Test: `tests/matrices/test_matrix_routing.py`

**Interfaces:**
- Change private call to `_ask_classifier(self, text: str, rule: _Rule) -> RouteDecision | None`.
- Preserve `TaskRouter.decide()` and `RouteDecision` public shapes.

- [ ] **Step 1: Add failing guardrail tests**

Add these cases using a stub classifier:

```python
async def test_llm_cannot_upgrade_confirm_first_rule_to_hard() -> None:
    async def classifier(text, *, candidates):
        return {"structured": True, "task_type": "system.volume.adjust", "input": {"percent": 30}}

    decision = await TaskRouter(registry=full_registry(), classifier=classifier).decide("调一下音量")
    assert decision.task_type == "system.volume.adjust"
    assert decision.strength is RouteStrength.SOFT
    assert not decision.arguments


async def test_llm_completed_directory_is_rechecked_against_allowed_roots(tmp_path) -> None:
    outside = tmp_path.parent / "outside"

    async def classifier(text, *, candidates):
        return {"structured": True, "task_type": "file.organize", "input": {"directory": str(outside)}}

    decision = await TaskRouter(
        registry=full_registry(), classifier=classifier, allowed_roots=[tmp_path]
    ).decide("整理这个目录")
    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.DIRECT
    assert "不在允许范围" in decision.reason


async def test_llm_cannot_switch_uncertain_rule_to_another_task_type() -> None:
    async def classifier(text, *, candidates):
        return {"structured": True, "task_type": "system.volume.adjust", "input": {"percent": 30}}

    decision = await TaskRouter(registry=full_registry(), classifier=classifier).decide("整理这个目录")
    assert decision.strength is RouteStrength.NONE
    assert decision.source == "llm"
```

- [ ] **Step 2: Run the new tests and verify the current code fails**

Run:

```powershell
python -m pytest tests/test_routing.py tests/test_route_guardrails.py tests/matrices/test_matrix_routing.py -k "llm and (upgrade or allowed_roots or switch)" -q
```

Expected before the fix: volume and outside-root cases incorrectly return HARD.

- [ ] **Step 3: Implement the constrained fallback**

Pass the uncertain `_Rule` object into `_ask_classifier`. Inside it:

1. Reject `raw.task_type` when it differs from `rule.task_type`; do not silently accept the alternate type.
2. Validate the required fields for `rule.task_type`.
3. Call `_directory_allowed(values.get("directory"))`; on failure return `_out_of_roots(rule, values)`.
4. If `rule.hard` is false, return `_confirm_first(rule)` even when every parameter is present.
5. Only a hard rule that passes all checks may return `_hard(text, rule, values)`.

Do not construct a separate HARD `RouteDecision` in `_ask_classifier`; reuse `_hard` so rule and LLM paths share the same arguments contract.

- [ ] **Step 4: Run all routing suites**

```powershell
python -m pytest tests/test_routing.py tests/test_route_guardrails.py tests/matrices/test_matrix_routing.py tests/test_public_boundary.py -q
```

Expected: PASS, including existing file-list classifier completion and screenshot/volume confirmation behavior.

- [ ] **Step 5: Optional checkpoint commit**

```powershell
git add agent_core/routing.py tests/test_routing.py tests/test_route_guardrails.py tests/matrices/test_matrix_routing.py
git commit -m "fix(routing): preserve guardrails after classifier fallback"
```

---

### Task 2: Resume pending intents from slot-only answers

**Files:**
- Modify: `agent_core/intent.py`
- Modify: `agent_core/loop.py`
- Test: `tests/test_intent_gate.py`

**Interfaces:**
- Add `extract_pending_slots(user_text: str, policy: IntentPolicy, missing_slots: Sequence[str]) -> dict[str, str]`.
- Add `is_pending_decline(user_text: str) -> bool`.
- Preserve `RuleIntentClassifier.classify()` and store schemas.

- [ ] **Step 1: Add failing continuation tests**

Cover these exact conversations:

```python
async def test_location_only_reply_resumes_pending_weather(self) -> None:
    # Seed pending weather_query with no location, then send only "北京".
    # Expected tool call: {"query": "北京 天气"}; pending_intent becomes None.

async def test_decline_clears_pending_intent_without_calling_tool(self) -> None:
    # Seed the same state, then send "算了，不查了".
    # Expected: no tool call; active_intent is conversation; pending_intent is None.

async def test_unrecognized_short_reply_keeps_pending_and_reasks(self) -> None:
    # Seed the same state, then send "不知道".
    # Expected: no tool call; pending_intent remains weather_query; reply asks for a city.

async def test_query_only_reply_resumes_pending_music(self) -> None:
    # Seed pending music_query with no query, then send only "周杰伦".
    # Expected netease search query is 周杰伦.
```

Keep the existing “我说的是上班的事” test passing as an explicit topic switch.

- [ ] **Step 2: Run the four tests and verify failure**

```powershell
python -m pytest tests/test_intent_gate.py -k "location_only or decline_clears or unrecognized_short or query_only" -q
```

Expected before the fix: “北京” and “周杰伦” are classified as conversation and do not resume the tool request.

- [ ] **Step 3: Implement conservative slot-only extraction**

In `agent_core/intent.py`:

- `is_pending_decline()` recognizes explicit cancellation/topic-switch forms such as `算了`, `不用`, `不查了`, `取消`, `不是`, `我说的是`, and `继续聊`.
- For `location`, trim polite/action wrappers and a trailing `天气`; accept 2-32 characters of Chinese, Latin letters, digits, spaces, middle dots, or hyphens. Reject decline phrases, question sentences, URLs, path separators, and strings containing tool-action verbs unrelated to location.
- For `query`, trim action terms and trailing music nouns using the existing helpers; accept 1-80 non-control characters.
- Unknown slot names return no value instead of guessing.

Do not give slot-only answers a new classifier score. They are valid only because a fresh `pending_intent` already defines what the answer means.

- [ ] **Step 4: Change the pending-intent branch order**

In `AgentLoop._run_intent_gate` use this order:

1. Load the policy and existing slots.
2. If `is_pending_decline(text)`, store a conversation decision with no pending intent and continue as chat.
3. Classify a full repeated request; when it matches the pending intent, merge normal slots.
4. Otherwise call `extract_pending_slots()` only for currently missing slots and merge the result.
5. If every required slot is present, execute `_run_intent_query()` and clear pending only on the existing success path.
6. If still incomplete, ask the existing clarification again and save the pending state. Do not fall through into generic conversation classification.

- [ ] **Step 5: Run the intent suite**

```powershell
python -m pytest tests/test_intent_gate.py -q
```

Expected: PASS for slot-only continuation, explicit decline, repeated clarification, tool failure handling, TTL, and scope isolation.

- [ ] **Step 6: Optional checkpoint commit**

```powershell
git add agent_core/intent.py agent_core/loop.py tests/test_intent_gate.py
git commit -m "fix(intent): resume pending requests from slot answers"
```

---

### Task 3: Clear intent state with the conversation lifecycle

**Files:**
- Modify: `agent_core/intent.py`
- Modify: `agent_core/conversation_manager.py`
- Modify: `web_api.py`
- Test: `tests/test_intent_gate.py`
- Test: `tests/test_conversation_manager.py`
- Test: `tests/test_web_api.py`

**Interfaces:**
- Add optional `intent_state_store: IntentStateStore | None` to `ConversationManager.__init__`.
- Add `async ConversationManager.forget(scope: Scope) -> None`.
- Add `SQLiteIntentStateStore.clear_orphans() -> int`.

- [ ] **Step 1: Add failing lifecycle tests**

Tests must prove both database and cache behavior:

```python
def test_clear_history_also_clears_intent_state():
    # Save pending state, call manager.clear_history(scope), then assert store.load(scope) is None.

async def test_delete_endpoint_forgets_cached_intent_state():
    # Prime SQLiteIntentStateStore._cache through load(), delete the conversation,
    # then assert the same store instance and a fresh store instance both return None.

def test_clear_orphans_removes_only_deleted_conversation_states():
    # Keep one active and one archived parent conversation; delete a third parent row.
    # clear_orphans() removes exactly the third state and its cache entry.
```

- [ ] **Step 2: Run lifecycle tests and verify failure**

```powershell
python -m pytest tests/test_intent_gate.py tests/test_conversation_manager.py tests/test_web_api.py -k "intent_state or clear_orphans or forgets_cached" -q
```

- [ ] **Step 3: Inject the store into `ConversationManager`**

Store the optional dependency as `self.intent_state_store`. `clear_history(scope)` calls `clear(scope)` after the conversation repository transaction succeeds, then clears resident history. Store-clear failure must be logged and must not restore deleted messages.

Implement `forget(scope)` for use only after the API has confirmed the conversation is not busy and the repository delete succeeded:

- clear intent state through the injected store;
- remove the scope from `_states` under `_state_lock`;
- remove its in-process lock;
- cancel and await an idle worker if one exists.

- [ ] **Step 4: Add orphan cleanup to the SQLite store**

`clear_orphans()` deletes `agent_core_intent_state` rows for which no matching row exists in `agent_core_conversations`, matching all four scope columns. After the transaction, remove the same keys from `_cache`. Archived conversations still have parent rows and must remain.

- [ ] **Step 5: Wire Web runtime ownership**

Pass the existing `intent_state_store` into `ConversationManager`. After `ConversationRepository.delete(scope)` succeeds, `await manager.forget(scope)` before returning HTTP 200. In the maintenance loop, call repository cleanup first and then `intent_state_store.clear_orphans()`.

Do not make `ConversationRepository` import the intent module; lifecycle orchestration belongs to the manager/runtime and this avoids a storage-layer cycle.

- [ ] **Step 6: Run conversation and Web tests**

```powershell
python -m pytest tests/test_intent_gate.py tests/test_conversation_manager.py tests/test_conversation_repository.py tests/test_web_api.py -q
```

- [ ] **Step 7: Optional checkpoint commit**

```powershell
git add agent_core/intent.py agent_core/conversation_manager.py web_api.py tests/test_intent_gate.py tests/test_conversation_manager.py tests/test_web_api.py
git commit -m "fix(conversation): clear intent state across lifecycle"
```

---

### Task 4: Split opening-memory selection from narration

**Files:**
- Modify: `agent_core/opening_greeting.py`
- Modify: `agent_core/opening_recall.py`
- Test: `tests/test_opening_greeting.py`

**Interfaces:**
- Add `CueSelection(selected_cue_id: str | None)` as an internal frozen dataclass.
- Add `parse_cue_selection(text: str, *, allowed_cue_ids: Iterable[str]) -> CueSelection | None`.
- Keep `OpeningGreetingService.generate(...) -> str` and the HTTP response unchanged.
- Production narration no longer trusts model-supplied `used_cue_id` or `cue_key`.

- [ ] **Step 1: Add failing selector and isolation tests**

Cover all of these:

- Selector accepts one whitelisted ID or `null`; unknown IDs, extra prose, missing fields, and non-string IDs fail closed.
- Hard filtering occurs before selection: cooldown IDs/keys, low-value replies, duplicate text, system-kind messages, and one-off tool commands are absent from the selector prompt.
- Selector receives at most 6 cues, `tools=None`, `temperature=0`, and a small output budget (48 tokens maximum).
- Narrator receives exactly one selected cue and no other cue summaries or recent cooldown keys.
- Selector `null`, timeout, or malformed output causes a narrator request containing no cue summaries and no recent cue keys.
- When a cue is selected, narrator output cannot change its ID; a fabricated `used_cue_id` field is ignored or rejected.
- A selected cue plus valid greeting writes that exact cue ID/key before returning the greeting.
- Failed personalized ledger write returns `DEFAULT_GREETING`.

- [ ] **Step 2: Run the new opening tests and verify failure**

```powershell
python -m pytest tests/test_opening_greeting.py -k "selector or narration_isolation or selected_cue or ledger" -q
```

- [ ] **Step 3: Add deterministic hard filtering and ranking**

Keep the existing source, age, low-value, duplicate, frequency, and cooldown filters. Add a narrow `is_one_off_command()` exclusion for obvious weather/search/playback/file-operation utterances; do not build a general intent classifier inside opening code.

Rank remaining cues before truncating to 6:

1. recent conversation topics that are not commands, newest first;
2. confirmed facts ordered by `importance` descending, then `occurred_at` descending;
3. stable `cue_id` as the final tie-breaker so tests are reproducible.

This rank only prepares the shortlist. It does not decide whether a topic is natural enough to mention.

- [ ] **Step 4: Implement the selector call**

The selector system instruction must say:

```text
只选择一个适合在开场时自然提起的话题。优先仍可能有后续的计划、生活变化和用户主动分享的近况；不要选临时查询、工具命令、普通寒暄或无关的敏感细节。没有合适内容就选 null。只输出 JSON，不要写开场白。
```

The response schema is exactly:

```json
{"selected_cue_id":"message:123"}
```

or:

```json
{"selected_cue_id":null}
```

Validate against the shortlist. Timeout, provider error, or invalid output means `null`, never “pick the first one”.

- [ ] **Step 5: Isolate the narration prompt**

If selection is `null`, build the narrator messages without candidate summaries and without `recent_keys`. If a cue is selected, include only that cue and instruct the narrator to use it without adding facts. The narrator returns only `{"greeting": "..."}`; service code attaches the selected cue ID and a deterministic cue key derived from the selected cue summary.

Do not show cooling keys to the narrator. They are server-side exclusion data; exposing them lets a plain greeting repeat an old event even when no cue was selected.

- [ ] **Step 6: Preserve write-before-return semantics**

For personalized narration, call the existing recall store with the server-selected cue ID/key before returning text. For plain narration, record `GREETING_PLAIN`. If personalized recording fails, return `DEFAULT_GREETING`; do not retry with the already exposed cue in context.

- [ ] **Step 7: Run the full opening suite**

```powershell
python -m pytest tests/test_opening_greeting.py -q
```

Expected: existing visit cache, scope isolation, cooldown, frequency gate, cleanup, prompt-injection, and fallback tests remain green alongside the new two-stage tests.

- [ ] **Step 8: Optional checkpoint commit**

```powershell
git add agent_core/opening_greeting.py agent_core/opening_recall.py tests/test_opening_greeting.py
git commit -m "fix(opening): isolate cue selection from narration"
```

---

### Task 5: Whitelist public steward event fields

**Files:**
- Modify: `web_api.py`
- Test: `tests/test_public_boundary.py`
- Test: `tests/test_web_api.py`
- Verify: `fronted/arcueid_daily_50/fronteddd/src/chatStreamState.test.js`

**Interfaces:**
- Preserve event name `steward.progress`.
- Preserve frontend-required fields: `turn_id`, `run_id`, `plan_id`, `step_id`, `phase`, `content`, `kind`, and `dedupe_key`.

- [ ] **Step 1: Add a failing public-boundary test**

```python
def test_steward_progress_public_data_is_allowlisted() -> None:
    data = _public_event_data(EventType.STEWARD_PROGRESS, {
        "turn_id": "t", "run_id": "r", "phase": "accepted",
        "content": "我先看看。", "kind": "steward_progress", "dedupe_key": "d",
        "traceback": "secret", "error": "internal", "provider_payload": {"x": 1},
    })
    assert data == {
        "turn_id": "t", "run_id": "r", "plan_id": "", "step_id": "",
        "phase": "accepted", "content": "我先看看。",
        "kind": "steward_progress", "dedupe_key": "d",
    }
```

- [ ] **Step 2: Run the boundary test and verify failure**

```powershell
python -m pytest tests/test_public_boundary.py -k steward_progress -q
```

- [ ] **Step 3: Add the explicit event branch**

In `_public_event_data`, return a freshly constructed dict for `EventType.STEWARD_PROGRESS`; never mutate or pass through the input dict. Convert identifiers and text fields to strings. Do not include any unknown keys.

- [ ] **Step 4: Verify backend and frontend event behavior**

```powershell
python -m pytest tests/test_public_boundary.py tests/test_web_api.py tests/test_steward.py -q
npm test -- --test-name-pattern="steward"
```

Run the npm command from `fronted/arcueid_daily_50/fronteddd`.

- [ ] **Step 5: Optional checkpoint commit**

```powershell
git add web_api.py tests/test_public_boundary.py tests/test_web_api.py
git commit -m "fix(web): allowlist steward progress event fields"
```

---

### Task 6: Integrated verification and durable project note

**Files:**
- Modify: `LESSONS_LEARNED.md`

- [ ] **Step 1: Run the focused backend regression set**

```powershell
python -m pytest tests/test_steward.py tests/test_opening_greeting.py tests/test_intent_gate.py tests/test_routing.py tests/test_route_guardrails.py tests/test_public_boundary.py tests/test_conversation_manager.py tests/test_conversation_repository.py tests/test_web_api.py tests/test_loop_events_integration.py -q
```

Expected: zero failures. Report exact pass/skip counts from the fresh run.

- [ ] **Step 2: Run the routing matrix**

```powershell
python -m pytest tests/matrices/test_matrix_routing.py -q
```

- [ ] **Step 3: Run frontend tests and production build**

From `fronted/arcueid_daily_50/fronteddd`:

```powershell
npm test
npm run build
```

- [ ] **Step 4: Re-run the four original reproductions**

Verify and record these exact outcomes:

- “调一下音量” plus classifier percent returns SOFT.
- “整理这个目录” plus classifier outside path returns DIRECT/SOFT.
- Pending weather plus “北京” calls weather search and clears pending state.
- After clear/delete, both the existing SQLite store instance and a new instance return no intent state.

- [ ] **Step 5: Append the implementation lesson**

Add a dated `LESSONS_LEARNED.md` entry with issue, cause, resolution, and prevention. The durable point is: model-declared metadata is not an authority boundary; server code must own selected IDs, execution strength, filesystem checks, and lifecycle invalidation.

- [ ] **Step 6: Review the final diff**

```powershell
git diff --check
git diff -- agent_core/routing.py agent_core/intent.py agent_core/loop.py agent_core/conversation_manager.py agent_core/opening_greeting.py agent_core/opening_recall.py web_api.py tests
```

Confirm no unrelated dirty-tree changes were included or reverted.

## Acceptance Checklist

- [ ] Classifier fallback cannot change the matched task type.
- [ ] Confirmation-required rules never become HARD after LLM parameter completion.
- [ ] LLM-supplied directories receive the same root check as rule-extracted directories.
- [ ] Slot-only replies resume fresh pending intents; explicit declines cancel them.
- [ ] Clear, delete, and automatic cleanup invalidate durable and cached intent state.
- [ ] Selector sees at most 6 hard-filtered cues; narrator sees at most 1 cue.
- [ ] A plain narrator prompt contains no candidate or cooldown text.
- [ ] Personalized greeting ledger identity comes from server selection, not model output.
- [ ] Steward SSE exposes only its documented public fields.
- [ ] Focused backend tests, routing matrix, frontend tests, and frontend build all pass.

