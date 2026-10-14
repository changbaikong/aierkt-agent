# Scenario Persona Editor Implementation Plan

> **For agentic workers:** Execute inline with test-first checkpoints. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a user-editable per-scenario persona card for Chatbot mode with an explicit one-time LLM analysis action.

**Architecture:** Extend the existing prompt-card storage and revision contract with structured persona fields and three stable behavior choices. The analysis endpoint returns a draft only; deterministic prompt composition injects saved persona text only into Chatbot mode, while roleplay continues to use only its scenario card.

**Tech Stack:** FastAPI, Pydantic, SQLite, React 18, Node test runner, pytest.

## Global Constraints

- Existing scenarios migrate without changing their current dialogue behavior.
- Persona editing and prompt injection are available only in Chatbot mode; roleplay uses only the scenario card.
- Persona analysis receives no built-in example persona and never saves automatically.
- Direct edits and ordinary dialogue do not invoke the persona analyzer.
- Safety, tools, memory, stickers, and output protocols remain outside the editable persona.
- Preserve unrelated dirty-worktree changes.

---

### Task 1: Structured persona persistence and prompt composition

**Files:** `agent_core/scenario_store.py`, `agent_core/scenario_runtime.py`, `tests/test_scenario_runtime.py`

- [x] Write failing migration, save, validation, and dialogue-injection tests.
- [x] Add default persona fields, three validated choices, revision-safe persistence, and deterministic non-empty prompt composition for Chatbot only.
- [x] Run the focused runtime tests and keep empty-persona requests byte-compatible in behavior.

### Task 2: Explicit analysis API

**Files:** `agent_core/scenario_api.py`, `agent_core/scenario_runtime.py`, `tests/test_scenario_api.py`

- [x] Write failing tests for draft-only analysis, owner isolation, input limits, fixed JSON, missing-field defaults, and absence of example personas.
- [x] Add `POST /api/scenarios/{scenario_id}/persona/analyze`; use a minimal extraction system instruction and return persona, preferences, and warnings without persistence.
- [x] Run focused API tests and prove saving afterward does not make a second model call.

### Task 3: User-editable persona interface

**Files:** `fronted/arcueid_daily_50/fronteddd/src/pages/ScenarioPromptCards.jsx`, `fronted/arcueid_daily_50/fronteddd/src/scenarioRequest.js`, `fronted/arcueid_daily_50/fronteddd/src/scenarioView.js`, matching tests and `styles.css`

- [x] Write failing helper tests for defaults, analysis result normalization, and save payloads.
- [x] In Chatbot mode, add the two-section editor, source-text analysis action, seven text fields, three simple selectors, warnings, counts, save state, and mobile/night styling; keep roleplay as a scenario-only editor.
- [x] Run frontend tests and production build.

### Task 4: Completion verification and reusable lesson

- [x] Run all scenario backend tests, the full frontend test suite, and frontend build.
- [x] Inspect the prompt request evidence proving roleplay excludes persona, Chatbot includes it, and legacy empty-persona scenarios remain compatible.
- [x] Append dated entries to `LESSONS_LEARNED.md` covering draft-only analysis, deterministic prompt assembly, and mode isolation.
