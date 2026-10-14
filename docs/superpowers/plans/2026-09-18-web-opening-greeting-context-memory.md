# Web Opening Greeting Context and Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Web opening greeting use same-role automatic memory, current local time, and opportunistic live weather while always asking what the user wants to do without delaying or fabricating the greeting.

**Architecture:** Keep opening generation as a read-only path. Add a `module_shared` memory visibility for high-confidence Web-extracted facts, a tab-scoped browser location session, and an `OpeningContextService` that reads time and cached weather synchronously while refreshing weather in the background. The language model receives only structured time, optional weather, confirmed facts, and persona context; it receives no tools and cannot execute actions during a greeting.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLite/FTS5, Qdrant client, httpx, React, browser Geolocation API, `sessionStorage`, pytest, Node test runner.

## Global Constraints

- Opening weather is optional: location bootstrap may add at most 500 ms before the opening request; cache misses never block the greeting.
- Opening generation retains its existing 4-second model timeout and never exposes an error state to the user.
- A greeting must ask what the user wants to do next; the fixed fallback must satisfy the same rule.
- The opening path may read time, fresh browser location, cached weather, confirmed facts, persona, and safe capability descriptions only. It must not expose or execute tools.
- Raw conversation messages, candidates, expired records, and cold history are never opening material.
- Automatic facts are shared across conversations only within the same `owner_id + channel + module_id`; they never cross accounts, channels, or characters.
- Browser location permission remains controlled by the browser. Denial, timeout, missing HTTPS support, or unavailable weather silently removes weather from the greeting.
- Exact coordinates are never logged or persisted by the weather cache; cache keys use coordinates rounded to one decimal place and expire after 15 minutes.
- Preserve the existing visit-id idempotency, fact cooldown, and two-plain-greetings frequency gate.

---

### Task 1: Add same-role cross-conversation automatic memory

**Files:**
- Modify: `agent_core/memory_writes.py`
- Modify: `agent_core/memory_repositories.py`
- Modify: `agent_core/memory_retrieval.py`
- Test: `tests/test_memory_writes.py`
- Test: `tests/test_memory_retrieval.py`
- Test: `tests/test_opening_greeting.py`

**Interfaces:**
- Produces: visibility value `module_shared`, scoped by `owner_id + channel + module_id`.
- Produces: `MemoryRepositories.confirmed_opening_facts(scope: Scope, *, limit: int = 5, now: datetime | None = None) -> list[dict[str, Any]]`.
- Preserves: `shared` remains account-and-channel wide; `module` remains exact-conversation private.

- [ ] **Step 1: Write failing visibility and migration tests**

Add tests proving that a high-confidence record saved by `save_extracted_memories()` uses `module_shared`, a low-confidence candidate remains `module`, and initialization migrates only legacy confirmed Web-extracted facts:

```python
def test_web_extracted_confirmed_fact_is_same_role_shared(self) -> None:
    writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
    [record] = writer.save_extracted_memories(
        self.scope,
        [ExtractedMemory("用户喜欢红茶", "user.preference.likes", 0.95, 0.8, "long_term")],
        turn_id="turn-1",
    )
    self.assertEqual(record["visibility"], "module_shared")

def test_web_extracted_candidate_stays_conversation_private(self) -> None:
    writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
    [record] = writer.save_extracted_memories(
        self.scope,
        [ExtractedMemory("用户似乎有点累", "user.state.mood", 0.6, 0.5, "instant")],
        turn_id="turn-2",
    )
    self.assertEqual(record["visibility"], "module")
```

- [ ] **Step 2: Run the new memory-write tests and confirm they fail**

Run: `python -m pytest tests/test_memory_writes.py -k "same_role_shared or conversation_private" -q`

Expected: FAIL because `module_shared` is not accepted or produced yet.

- [ ] **Step 3: Implement the new visibility and legacy migration**

Update `MemoryWriter` so `module_shared` is valid only for `kind='fact'` and `status='confirmed'`. In `save_extracted_memories()`, select it only when `memory.confidence >= CONFIRMED_THRESHOLD`; keep candidates private. Scope conflict replacement as follows:

```python
if visibility == "shared":
    return "owner_id=? AND channel=? AND visibility='shared' AND conflict_key=?", (scope.owner_id, scope.channel, conflict_key)
if visibility == "module_shared":
    return "owner_id=? AND channel=? AND module_id=? AND visibility='module_shared' AND conflict_key=?", (scope.owner_id, scope.channel, scope.module_id, conflict_key)
return "owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND visibility='module' AND conflict_key=?", (*_scope(scope), conflict_key)
```

During repository initialization, migrate rows matching all of these conditions: `visibility='module'`, `kind='fact'`, `status='confirmed'`, valid JSON metadata, and `metadata.source='web.async_memory_extraction'`. Do not migrate manual module-private facts or candidates. Existing vector payload backfill must publish the new visibility after migration.

- [ ] **Step 4: Write failing retrieval-boundary tests**

Cover both normal retrieval and opening selection with these exact arrangements:

- Save a `module_shared` fact under `Scope("user", "web", "old", "companion")`; assert it is returned for `Scope("user", "web", "new", "companion")`.
- Repeat retrieval with a different owner, channel, and module one at a time; assert all three result sets exclude the fact.
- Insert one valid `shared` fact, one valid same-role `module_shared` fact, one `module` fact, one candidate, one expired fact, and one cold fact; assert `confirmed_opening_facts()` returns exactly the first two IDs.

- [ ] **Step 5: Implement shared authorization in SQL, FTS, and vector retrieval**

Add the `module_shared` branch to FTS scope filtering, Qdrant payload filtering, `_warm_allowed()`, and `_cold_allowed()`. Implement `confirmed_opening_facts()` with a pre-`LIMIT` expiry predicate and a second Python datetime check. Order by fact formation time (`created_at DESC`), not embedding-driven `updated_at`.

- [ ] **Step 6: Run focused memory tests**

Run: `python -m pytest tests/test_memory_writes.py tests/test_memory_retrieval.py tests/test_opening_greeting.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the memory boundary**

```bash
git add agent_core/memory_writes.py agent_core/memory_repositories.py agent_core/memory_retrieval.py tests/test_memory_writes.py tests/test_memory_retrieval.py tests/test_opening_greeting.py
git commit -m "fix(memory): share automatic facts within a web character"
```

---

### Task 2: Make location sharing default for one browser tab

**Files:**
- Create: `fronted/arcueid_daily_50/fronteddd/src/locationSharing.js`
- Create: `fronted/arcueid_daily_50/fronteddd/src/locationSharing.test.js`
- Modify: `fronted/arcueid_daily_50/fronteddd/src/pages/ChatPage.jsx`

**Interfaces:**
- Produces: `readTabLocationSharing(storage: Storage | null) -> boolean`, defaulting to `true` unless the current tab explicitly stored `false`.
- Produces: `writeTabLocationSharing(storage: Storage | null, enabled: boolean) -> void`.
- Produces: `startLocationSession(options) -> { openingReady: Promise<string>, stop: () => void }` where `openingReady` settles within 500 ms with `shared`, `timeout`, `denied`, or `unavailable`.

- [ ] **Step 1: Write failing tab-preference and deadline tests**

Use an in-memory storage and fake geolocation implementation to assert:

```javascript
test('a new tab defaults to sharing', () => {
  assert.equal(readTabLocationSharing(fakeStorage()), true)
})

test('manual disable survives refresh only in the same tab', () => {
  const storage = fakeStorage()
  writeTabLocationSharing(storage, false)
  assert.equal(readTabLocationSharing(storage), false)
})

```

For the deadline case, inject a fake `setTimeout` that captures the callback, call it, await `openingReady`, assert the value is `"timeout"`, and assert the fake geolocation watch has not been cleared.

- [ ] **Step 2: Run the frontend unit test and confirm it fails**

Run: `node --test src/locationSharing.test.js`

Working directory: `fronted/arcueid_daily_50/fronteddd`

Expected: FAIL because the helper module does not exist.

- [ ] **Step 3: Implement the tab-scoped location session**

Use the key `arcueid-location-sharing-v1` in `sessionStorage`. `startLocationSession()` must:

- call `navigator.geolocation.watchPosition()` with `enableHighAccuracy: true`, `maximumAge: 10000`, and `timeout: 10000`;
- await the first successful `/api/location` POST before resolving `openingReady='shared'`;
- resolve the opening promise after 500 ms without stopping the watch;
- retain the existing 15-second/30-meter upload suppression and backend 10-second rate limit compatibility;
- resolve denial or unsupported APIs without throwing;
- clear the watch and call `DELETE /api/location` on `stop()`.

- [ ] **Step 4: Wire location readiness ahead of the opening request**

Initialize `locationSharing` through `readTabLocationSharing(safeSessionStorage())`. Start the location session before requesting the opening greeting and await only its `openingReady` promise. Manual toggles write the tab preference. A denied permission changes the status to `denied`, stores `false` for the current tab, and does not retry on refresh.

Do not mark the visit greeted until the opening endpoint responds, preserving existing visit idempotency.

- [ ] **Step 5: Run frontend behavior tests**

Run: `node --test src/locationSharing.test.js src/openingGreeting.test.js`

Working directory: `fronted/arcueid_daily_50/fronteddd`

Expected: PASS, including the 500 ms deadline, manual disable, cleanup, and existing opening identity checks.

- [ ] **Step 6: Commit the tab location behavior**

```bash
git add fronted/arcueid_daily_50/fronteddd/src/locationSharing.js fronted/arcueid_daily_50/fronteddd/src/locationSharing.test.js fronted/arcueid_daily_50/fronteddd/src/pages/ChatPage.jsx
git commit -m "feat(web): bootstrap tab location before opening greeting"
```

---

### Task 3: Add cached opportunistic weather context

**Files:**
- Create: `agent_core/opening_context.py`
- Create: `agent_core_adapters/open_meteo.py`
- Create: `tests/test_opening_context.py`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `WeatherSnapshot(condition: str, temperature_c: float, apparent_temperature_c: float | None, observed_at: str)`.
- Produces: `OpeningEnvironment(local_time: str, timezone: str, weather: WeatherSnapshot | None)`.
- Produces: `WeatherProvider.current(latitude: float, longitude: float) -> Awaitable[WeatherSnapshot]`.
- Produces: `OpeningContextService.collect(scope: Scope, *, timezone_name: str | None = None) -> Awaitable[OpeningEnvironment]`.
- Produces: `OpeningContextService.prefetch(scope: Scope) -> None`, which deduplicates refreshes by coarse location key.

- [ ] **Step 1: Write failing environment and cache tests**

Cover these exact behaviors with a fake clock, location store, and provider:

- No location: `collect()` returns the injected local time and `weather is None`; provider call count remains zero.
- Cache miss: the first `collect()` returns `weather is None`; after yielding to the event loop the provider call count becomes one.
- Fresh cache: a second `collect()` returns the provider's exact `WeatherSnapshot` and does not increment the call count.
- Expiry: advance the fake clock from 899 to 901 seconds; the former returns cached weather and the latter schedules one refresh while returning no stale weather.
- Deduplication: call `prefetch()` three times for coordinates that round to the same one-decimal key; assert one provider call and one in-flight task.
- Provider failure: make `current()` raise `TimeoutError`; await task completion and assert the next `collect()` still returns `weather is None` without raising.
- Invalid timezone: pass `"Not/AZone"`; assert a non-empty fallback timezone and local time are returned.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `python -m pytest tests/test_opening_context.py -q`

Expected: FAIL because the context service and adapter do not exist.

- [ ] **Step 3: Implement `OpeningContextService`**

Use `zoneinfo.ZoneInfo` for an optional browser IANA timezone and fall back to the server's local timezone when absent or invalid. The service must always return time. It may return weather only when `LocationStore.get(scope)` is fresh and the matching cache entry is younger than 900 seconds.

Use `(round(latitude, 1), round(longitude, 1))` as the in-memory cache key. On cache miss, schedule one background refresh with `asyncio.create_task()` and return immediately with `weather=None`. Keep strong references to in-flight tasks, remove them on completion, and provide `async close()` for lifespan shutdown.

- [ ] **Step 4: Implement the structured weather adapter**

Use Open-Meteo's current-weather endpoint with latitude, longitude, `temperature_2m`, `apparent_temperature`, and `weather_code`. Set an adapter timeout of 3 seconds because refresh is background-only. Map WMO codes into bounded Chinese conditions such as `晴`, `多云`, `雾`, `小雨`, `雨`, `雪`, and `雷雨`; never pass arbitrary provider text into the model.

`httpx` is already used by the project, so do not introduce another HTTP client. Add only a version declaration if `requirements.txt` does not already guarantee the installed `httpx` version.

- [ ] **Step 5: Verify cache, timeout, and sanitization behavior**

Run: `python -m pytest tests/test_opening_context.py -q`

Expected: PASS. Tests must assert that coordinates do not appear in `repr(OpeningEnvironment)`, log messages, or cached values exposed to the greeting layer.

- [ ] **Step 6: Commit the environment reader**

```bash
git add agent_core/opening_context.py agent_core_adapters/open_meteo.py tests/test_opening_context.py requirements.txt
git commit -m "feat(opening): add opportunistic cached weather context"
```

---

### Task 4: Restrict and enrich opening generation

**Files:**
- Modify: `agent_core/opening_greeting.py`
- Modify: `agent_core/opening_recall.py`
- Test: `tests/test_opening_greeting.py`

**Interfaces:**
- Consumes: `confirmed_opening_facts()` from Task 1.
- Consumes: `OpeningContextService.collect()` from Task 3.
- Changes: `OpeningGreetingService.generate(scope: Any, *, visit_id: str, absence_tier: str = DEFAULT_ABSENCE_TIER, now: datetime | None = None, timezone_name: str | None = None) -> Awaitable[str]`.
- Preserves: provider call uses `tools=None` and the existing 4-second timeout.

- [ ] **Step 1: Replace raw-topic tests with confirmed-memory-only tests**

Delete expectations that a new conversation sees recent raw messages. Add assertions that `conversation_repository.recent_user_messages()` is never called, even when it contains recent meaningful text, and that only `shared` or same-role `module_shared` confirmed facts become cues.

- [ ] **Step 2: Write failing time, weather, and action-question tests**

Add provider-spy tests proving all of the following:

- A fixed `OpeningEnvironment("2026-09-18T15:30:00+08:00", "Asia/Shanghai", WeatherSnapshot("小雨", 21.0, 20.0, "2026-09-18T15:15:00+08:00"))` appears in the user prompt with no coordinates.
- With `weather=None`, the prompt contains the local time section and no current-weather section.
- The provider spy records `tools is None`, `max_tokens == 96`, and the configured temperature.
- Provider output `{"greeting":"下午好，今天过得好吗？","used_cue_id":null,"cue_key":null}` is rejected and returns the fixed fallback because it does not ask for the next action.
- Provider output `{"greeting":"外面有点凉，今天想先做什么？","used_cue_id":null,"cue_key":null}` records a plain greeting event, not a fact event.
- An environment reader that raises returns a valid time-only action question; a confirmed-memory reader that raises skips the provider and returns the fixed fallback.

- [ ] **Step 3: Remove raw conversation retrieval from production material collection**

Stop injecting `conversation_repository` in production and make `collect_material()` read only `confirmed_opening_facts()`. Keep legacy topic recall-event parsing for old database rows, but do not create new topic cues or topic events.

An unavailable or failed environment reader is not a memory-integrity failure: continue with local time or a plain prompt. A failed confirmed-memory query remains a hard fallback and skips the model, preserving the anti-fabrication boundary.

- [ ] **Step 4: Add structured environment prompt sections**

The prompt must state:

```text
- 必须自然地问用户现在想做什么、想聊什么，或准备先处理什么。
- 可以自由选择是否提时间、天气或一条允许使用的确认事实，不要机械罗列。
- 只有【当前天气】存在时才能提天气；不存在时禁止猜测。
- 只能根据给出的事实说话，不得补充地点、原因、结果或用户状态。
- 你可以按人物设定灵活措辞，但不能调用工具、承诺已经执行任务或暴露系统能力名称。
```

Keep the JSON decision protocol. Time and weather do not set `used_cue_id`; only a confirmed-memory reference does, so weather does not consume fact cooldown.

- [ ] **Step 5: Enforce the next-action question and update fallback copy**

Add a validator that requires a Chinese question mark and one of a narrow set of action-invitation shapes (`想做`, `想聊`, `想先`, `准备`, `打算`, `从哪开始`, `怎么安排`). Reject generic questions such as “今天过得好吗？”. Change the fixed fallback to:

```python
DEFAULT_GREETING = "哦，来了啊。今天想先做点什么？"
```

The fallback remains within the existing 4-35 character range and avoids customer-service language.

- [ ] **Step 6: Narrow recall database error handling**

Do not treat every `sqlite3.OperationalError` as an empty recall history. Suppress only the known “table does not exist” compatibility case; let lock, corruption, permission, and malformed-query failures reach the existing fixed-fallback path.

- [ ] **Step 7: Run the complete opening suite**

Run: `python -m pytest tests/test_opening_greeting.py tests/test_opening_context.py -q`

Expected: PASS for confirmed-memory boundaries, environment prompting, question enforcement, recall cooldown, idempotency, and all fallback paths.

- [ ] **Step 8: Commit the generation policy**

```bash
git add agent_core/opening_greeting.py agent_core/opening_recall.py tests/test_opening_greeting.py tests/test_opening_context.py
git commit -m "feat(opening): generate contextual action-oriented greetings"
```

---

### Task 5: Wire Web API lifecycle and verify the full flow

**Files:**
- Modify: `web_api.py`
- Modify: `fronted/arcueid_daily_50/fronteddd/src/openingGreeting.js`
- Modify: `fronted/arcueid_daily_50/fronteddd/src/openingGreeting.test.js`
- Modify: `tests/test_web_api.py`
- Modify: `README.md`
- Modify: `LESSONS_LEARNED.md`

**Interfaces:**
- Changes request body: optional `timezone` containing an IANA timezone name, maximum 64 characters.
- Preserves response body: `greeting`, `conversation_id`, `visit_id`, and existing fallback behavior.
- Wires one process-level `OpeningContextService` to the existing location store and opening service.

- [ ] **Step 1: Write failing API integration tests**

Add tests for these exact API outcomes:

- Post `timezone="Asia/Shanghai"`; assert the opening-service spy receives that exact value.
- Post a valid location while the weather-provider fake is blocked on an event; assert `/api/location` returns before the event is released and one refresh is in flight.
- Post an invalid timezone longer than 64 characters and assert HTTP 422; post a short unknown IANA name and assert the endpoint still returns a greeting through the safe fallback.
- Make weather refresh fail and assert the opening endpoint still returns HTTP 200 with a next-action question.
- Start one blocked refresh, run lifespan shutdown, and assert `OpeningContextService.close()` cancels or joins the task and leaves no tracked tasks.

Add a frontend request test asserting `Intl.DateTimeFormat().resolvedOptions().timeZone` is sent when available and omitted when unavailable.

- [ ] **Step 2: Run Web API and frontend request tests to verify failure**

Run: `python -m pytest tests/test_web_api.py -k "opening or location" -q`

Run: `node --test src/openingGreeting.test.js src/locationSharing.test.js`

Working directory for the second command: `fronted/arcueid_daily_50/fronteddd`

Expected: FAIL on the missing timezone and context-service wiring.

- [ ] **Step 3: Wire the backend service**

Add `timezone: str | None` to `OpeningGreetingRequest`, validate the length and IANA syntax without exposing validation internals, and pass it to `OpeningGreetingService.generate()`.

Construct a single `OpeningContextService` from the existing `_location_store` and `OpenMeteoWeatherProvider`. Inject it into `OpeningGreetingService`. After a successful `/api/location` update, call `prefetch(scope)` and return immediately; never await weather. On shutdown, await `OpeningContextService.close()` so background tasks do not leak.

- [ ] **Step 4: Send browser timezone and preserve identity checks**

Extend `requestOpeningGreeting()` to include the browser timezone when available. Do not change visit-id generation, request cancellation, response identity comparison, or the rule that one visit receives one delivered greeting.

- [ ] **Step 5: Add observability without sensitive data**

Record structured counters or log fields for `weather_cache_hit`, `weather_cache_miss`, `weather_refresh_failed`, `location_deadline_timeout`, and total opening duration. Never log coordinates, raw weather payloads, memory contents, or browser permission details beyond a stable reason code.

- [ ] **Step 6: Run focused and full regression tests**

Run:

```bash
python -m pytest tests/test_memory_writes.py tests/test_memory_retrieval.py tests/test_opening_context.py tests/test_opening_greeting.py tests/test_location.py tests/test_web_api.py -q
```

Then run:

```bash
npm test
npm run build
```

Working directory for frontend commands: `fronted/arcueid_daily_50/fronteddd`.

Expected: all tests pass; the frontend build succeeds; no opening request exceeds its existing model timeout because weather refresh never lies on the critical path.

- [ ] **Step 7: Perform manual acceptance checks**

Verify these scenarios in a fresh browser tab:

1. Permission granted quickly: opening waits no more than about 500 ms for location; it may use already cached weather.
2. First weather cache miss: opening asks what the user wants to do without mentioning weather; background refresh completes for later use.
3. Permission prompt left unanswered: opening proceeds after about 500 ms.
4. Permission denied or insecure browser context: opening proceeds without weather and does not repeatedly prompt in the same tab.
5. Manual location disable: refresh in the same tab remains disabled; a genuinely new tab defaults to enabled.
6. Confirmed same-role fact exists: it may be referenced subject to cooldown; raw chat text never appears as source material.
7. Weather provider is offline: opening remains available, asks the next-action question, and exposes no provider error.

- [ ] **Step 8: Document the behavior and durable lesson**

Update `README.md` with the tab-scoped permission behavior, 500 ms location deadline, 15-minute weather cache, and memory visibility boundaries. Append a dated `LESSONS_LEARNED.md` entry covering the root causes: automatic facts were written with a scope the opening reader could not see, and live network data must not be placed serially on the first-paint path. State the prevention action: share authorization policies and keep optional context behind bounded, cached readers.

- [ ] **Step 9: Commit the integrated Web flow**

```bash
git add web_api.py fronted/arcueid_daily_50/fronteddd/src/openingGreeting.js fronted/arcueid_daily_50/fronteddd/src/openingGreeting.test.js tests/test_web_api.py README.md LESSONS_LEARNED.md
git commit -m "feat(web): wire fast contextual opening greetings"
```

---

## Acceptance Criteria

- A new tab attempts browser location sharing by default, but browser permission remains authoritative.
- The opening request starts after at most 500 ms of location bootstrap and retains the existing 4-second model timeout.
- Weather is used only from a fresh 15-minute cache; cache misses refresh in the background and never delay the current opening.
- Every accepted model greeting and every fixed fallback asks what the user wants to do next.
- The model may phrase the greeting freely from persona, time, optional weather, and confirmed facts, but receives no callable tools.
- High-confidence Web-extracted facts work automatically in later conversations for the same character without manual saving.
- No raw chat, candidate, expired record, cold history, guessed weather, exact coordinate, or cross-character fact can reach the opening prompt.
- Existing visit idempotency, recall cooldown, account isolation, and failure fallback remain intact.
