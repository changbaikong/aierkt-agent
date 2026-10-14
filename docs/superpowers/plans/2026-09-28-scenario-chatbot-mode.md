# 情景 Chatbot 模式 Implementation Plan

> **For agentic workers:** Execute the tasks inline in this session with test-first checkpoints.

**Goal:** Add a selectable Chatbot branch to the scenario page while preserving the existing roleplay branch and sticker support.

**Architecture:** Store a per-scenario `dialogue_mode` with `roleplay` as the backward-compatible default. The setup/analysis phase stays shared; only the formal dialogue prompt and presentation label branch. Chatbot mode reuses the existing stateful history, summary, sticker catalog, cooldown, and validation rules, while explicitly preventing the scene character from becoming the assistant identity.

**Tech Stack:** FastAPI/Pydantic, SQLite-backed scenario store, React 18/Vite, Node test runner, pytest.

## Global Constraints

- Existing roleplay behavior and existing scenario records must remain compatible.
- Chatbot mode is selectable before formal dialogue starts and is locked once dialogue begins.
- Chatbot replies may still emit verified stickers subject to the existing cooldown and catalog checks.
- Do not touch unrelated dirty-worktree changes.

---

### Task 1: Scenario mode persistence and API

**Files:**
- Modify: `agent_core/scenario_store.py`
- Modify: `agent_core/scenario_api.py`
- Test: `tests/test_scenario_runtime.py`, `tests/test_scenario_api.py`

- [x] Add a migrated `dialogue_mode` column defaulting to `roleplay`, expose it in snapshots and list entries, and add validated mode read/update methods.
- [x] Add a `PUT /api/scenarios/{scenario_id}/mode` request with safe account validation; reject unknown modes, running turns, and already-started dialogue.
- [x] Write failing tests for legacy migration, mode updates, locking, and API validation.
- [x] Run the focused tests, implement the smallest storage/API change, then rerun them.

### Task 2: Chatbot dialogue branch with stickers

**Files:**
- Modify: `agent_core/scenario_runtime.py`
- Test: `tests/test_scenario_runtime.py`

- [x] Add a failing runtime test that selects Chatbot mode, starts the scenario, sends a turn, and asserts the model receives the direct-chat identity boundary and current card/history.
- [x] Keep the existing summary, sticker catalog, cooldown, JSON validation, and bubble persistence path shared by both modes.
- [x] Change only the system instruction and presentation metadata for Chatbot mode; allow valid sticker IDs and store them exactly as roleplay does.
- [x] Run focused runtime tests, then the complete scenario runtime suite.

### Task 3: Scenario page mode control and labels

**Files:**
- Modify: `fronted/arcueid_daily_50/fronteddd/src/pages/ScenarioPage.jsx`
- Modify: `fronted/arcueid_daily_50/fronteddd/src/scenarioView.js`
- Modify: `fronted/arcueid_daily_50/fronteddd/src/styles.css`
- Test: `fronted/arcueid_daily_50/fronteddd/src/scenarioView.test.js`

- [x] Add a compact two-option mode control in the scenario header, defaulting from the snapshot and saving through the new endpoint.
- [x] Disable mode changes while loading, a turn is running, or formal dialogue has started; show Chatbot as the assistant label/status when active.
- [x] Keep the existing prompt-card, scenario switcher, attachments, and retry controls unchanged.
- [x] Add helper tests for mode labels/status and run the frontend test suite and production build.

### Task 4: Verification and project lesson

- [x] Run all scenario Python tests and frontend tests/build.
- [x] Smoke-check Chatbot with a real model and temporary scenario data; verify the running API exposes the mode endpoint, and cover roleplay plus sticker output in deterministic tests.
- [x] Append a dated reusable lesson to `LESSONS_LEARNED.md` without altering unrelated entries.
