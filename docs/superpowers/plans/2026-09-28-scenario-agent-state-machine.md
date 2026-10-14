# Scenario Agent State Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed collector flow with a real-model scenario analyst that maintains a prompt card until explicit confirmation, then runs isolated role dialogue.

**Architecture:** `ScenarioStore` owns the durable `drafting -> ready -> dialogue` state. `ScenarioRuntime` uses one analyst prompt for the first two states and a separate roleplay prompt for dialogue. The React page renders the current state and calls a dedicated start endpoint; CSS fixes native scrollbar and resize controls.

**Tech Stack:** Python, FastAPI, SQLite, React, Vite, Node test runner, pytest, Playwright.

## Global Constraints

- Production prompts and code contain no example-specific convenience-store scene or fixed dialogue.
- Scenario memory stays isolated from the main chat and from other scenarios.
- Unit tests may use deterministic providers, but completion requires a real configured model call.
- Existing unrelated working-tree changes must be preserved.

---

### Task 1: Durable three-state session model

**Files:**
- Modify: `agent_core/scenario_store.py`
- Test: `tests/test_scenario_runtime.py`

**Interfaces:**
- Produces: `ScenarioStore.set_analysis(...)` and `ScenarioStore.start_dialogue(...)`.
- State values: `drafting`, `ready`, `dialogue`.

- [ ] Add failing tests proving a new session starts in `drafting`, analyst output can move it to `ready`, refinements stay in setup history, and only explicit start moves it to `dialogue`.
- [ ] Run the focused tests and confirm they fail because the new state methods do not exist.
- [ ] Add the SQLite migration and minimal state methods, keeping old `collecting` rows usable.
- [ ] Rerun the focused tests and confirm they pass.

### Task 2: Real-model analyst prompt and explicit start API

**Files:**
- Modify: `agent_core/scenario_runtime.py`
- Modify: `agent_core/scenario_api.py`
- Test: `tests/test_scenario_runtime.py`
- Test: `tests/test_scenario_api.py`

**Interfaces:**
- Analyst JSON: `reply`, `scene_prompt`, `scene_summary`, `status` (`discussing` or `ready`).
- Endpoint: `POST /api/scenarios/{scenario_id}/start` with `account_id`.

- [ ] Add failing tests proving setup messages go to the analyst model, its visible reply is stored, its whole card replaces the prior draft, no example scene appears in the request, and dialogue cannot start before `ready`.
- [ ] Run focused tests and confirm the expected failures.
- [ ] Replace the fixed field collector with the analyst call and implement the start transition.
- [ ] Run runtime and API tests until green.

### Task 3: State-aware React flow

**Files:**
- Modify: `fronted/arcueid_daily_50/fronteddd/src/pages/ScenarioPage.jsx`
- Modify: `fronted/arcueid_daily_50/fronteddd/src/scenarioView.js`
- Modify: `fronted/arcueid_daily_50/fronteddd/src/scenarioRequest.js`
- Test: `fronted/arcueid_daily_50/fronteddd/src/scenarioView.test.js`
- Test: `fronted/arcueid_daily_50/fronteddd/src/scenarioRequest.test.js`

**Interfaces:**
- Consumes snapshot `phase` values and the start endpoint.
- Produces a visible current-card preview and an explicit `开始情景` action.

- [ ] Add failing view/request tests for `drafting`, `ready`, and `dialogue` behavior.
- [ ] Run the focused Node tests and confirm failures.
- [ ] Implement the minimal helpers and page controls; keep analyst messages visible before start.
- [ ] Rerun focused and full frontend tests.

### Task 4: Theme-native scroll surfaces

**Files:**
- Modify: `fronted/arcueid_daily_50/fronteddd/src/styles.css`

- [ ] Add scoped scrollbar colors for scenario message and textarea surfaces in both themes.
- [ ] Disable textarea resize handles and keep internal scrolling.
- [ ] Build the frontend and inspect desktop plus narrow viewport screenshots for clipping and native white scrollbars.

### Task 5: Real-model end-to-end verification and lessons

**Files:**
- Modify: `README.md`
- Modify: `LESSONS_LEARNED.md`
- Modify: `fronted/arcueid_daily_50/LESSONS_LEARNED.md`

- [ ] Run backend scenario tests, frontend tests, and the frontend build.
- [ ] Start the actual `web_api.py`; verify `/api/health` reports a configured provider.
- [ ] Exercise two unrelated non-example scenarios through discuss, refine, start, and dialogue; confirm requests produce scene-specific results.
- [ ] Run browser QA for the theme fixes and capture screenshots outside the repository.
- [ ] Record the reusable lesson that demo providers must never occupy the production development port or count as integration proof.
