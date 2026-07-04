# Backend Text Adventure Game PRD

## Source

Primary source: `backend-design.md` v1.0. The backend document is the contract authority when it differs from the frontend document.

## Goal

Implement a hackathon-ready backend for an AI co-created reincarnation text adventure. Players join as characters, submit actions or oracles, and poll a shared story feed. Approved inputs advance a global 60-round cycle, create structured acts, update hidden hope, trigger fixed node events, and eventually settle into a new cycle.

## Technical Approach

- Use a single-process backend with SQLite persistence.
- Keep the first implementation dependency-light and locally runnable in this scaffold-only repo.
- Separate game/domain logic from HTTP routing so later LLM, printer, mailer, or FastAPI adapters can replace deterministic fallbacks without changing contracts.
- Store all prompt text under `server/prompts/`.
- Preserve the API shapes in `backend-design.md` section 10.

## Required Data Model

Create SQLite tables matching the backend design:

- `world`
- `characters`
- `acts`
- `oracle_pool`
- `triples`
- `print_queue`
- `inputs_log`

Startup must initialize missing schema and seed `world` row id `1` with cycle `1`, round `0`, hope `40`, status `running`.

## Required API

Base `/api`, JSON, CORS enabled.

- `POST /api/join`
  - Request: `{ "selfDesc": string, "email"?: string }`
  - Response: `{ "charId": string, "name": string, "profile": string, "location": string, "cycle": int }`
  - Creates a human character, assigns one of the three default locations, writes a character-card print queue item, and logs the input.

- `POST /api/act`
  - Request: `{ "charId": string, "text": string, "kind": "action" | "narration" | "oracle", "scope"?: "global" | "loc_x" }`
  - Accepted response: `{ "accepted": true, "round": int, "oracleStatus"?: "applied" | "pooled" }`
  - Rejected response: `{ "accepted": false, "reason": string }` with HTTP 200.
  - Validates character existence/status, world status, kind/scope, and content length.
  - Approved inputs increment the global round synchronously, create an act, update hope, and append chronicle/personal data.
  - Oracle quota: one applied oracle per `(cycle, round, scope)`, extras are pooled. Oracles must not cancel, advance, or delay node events.

- `POST /api/leave`
  - Request: `{ "charId": string }`
  - Response: `{ "cardUrl": "/card/c_x" }`
  - Marks character leaving/ended, stores an ending, creates ending act and print queue item, and exposes the card data.

- `GET /api/story?after=<actId>`
  - Response contains world status, three locations with directives, up to 50 acts with `id > after`, and all characters.
  - `hopeHint` is `low`, `mid`, or `high`, never the numeric hope.

- `GET /api/me/:charId`
  - Response: profile, status, location, personal timeline, ending, echo.
  - Unknown characters return the standard NOT_FOUND error envelope.

- `GET /api/card/:charId`
  - Response: `{ "name", "profile", "ending", "cycle", "qrUrl" }`.
  - Unknown characters return the standard NOT_FOUND error envelope.

## World Mechanics

- Locations: `loc_shelter` / `避难所`, `loc_ruins` / `废墟`, `loc_observatory` / `观测站`.
- Approved acts increment `world.round`; rejected inputs do not.
- Fixed node events happen at rounds 20, 40, and 60 and cannot be cancelled or rescheduled.
- Node events force directives for all locations and enqueue `apocalypse` print jobs.
- Round 60 triggers settlement:
  - Set status `settling` while resolving.
  - Create a `settlement` act based on hope threshold 50.
  - End active characters with short endings.
  - Carry echo fragments to a subset of characters.
  - Start the next cycle with round 0, hope 40, status `running`.
- Deterministic fallback weaving is acceptable for this implementation: echo user input into a structured narrative, enforce schema in code, and keep the pipe unblocked when no LLM is configured.
- Heartbeat support must exist as callable backend logic; automatic background timers can be deferred if tests cover the mechanic without making test runs slow.
- Cross-location seeds, triples, print queue, and ending-card skills should be represented in persistence even if external side effects are stubs.

## Error Contract

Errors use:

```json
{ "error": { "code": "REJECTED|QUOTA|NOT_FOUND|SETTLING|INTERNAL", "message": "世界内话术" } }
```

Business moderation rejections for `POST /api/act` return HTTP 200 with `{ "accepted": false, "reason": "..." }`.

## Testing Requirements

- Add tests before production code.
- Cover database initialization, join, act, story polling, oracle quota, node event generation, settlement rollover, leave/card/me, and HTTP contract behavior.
- Use real SQLite databases in temporary directories for tests.

## Out of Scope For First Pass

- Real LLM provider integration.
- Real email delivery.
- Real thermal printer driver.
- WebSocket, Redis, message queue, Neo4j.
- Frontend implementation.

## Current Implementation Status

Completed in this pass:

- Dependency-free Python backend package under `server/`.
- SQLite schema initialization for all required tables.
- Deterministic `GameService` covering join, act, oracle, heartbeat, leave, story, me, and card flows.
- HTTP adapter for the required JSON routes with CORS and standard error envelopes.
- Fixed node events at rounds 20, 40, and 60.
- Settlement rollover with new-cycle opening acts and directive reset.
- Print queue persistence and bulletin backlog pruning.
- Print worker method that marks pending jobs printed in queue order while skipping dropped jobs.
- Mail queue persistence with whitelist/simulated status, per-hour act mail frequency limiting, and ending mail exemption.
- Idle heartbeat scheduler method with injectable clock for tests.
- Weaver schema processing for deterministic or injected results, including validation fallback.
- Character location state changes, structured triple writes, and cross-location seed queue persistence/consumption.
- Background runtime supervisor that starts heartbeat and print loops from `server.app.run()`.
- Per-location worker runtime with one queue/thread per location; same-location jobs serialize while different locations can start in parallel.
- HTTP `/api/act` production entrypoint now acknowledges after validation/round increment and dispatches act weaving to location workers.
- Dependency-free HTTP JSON LLM gateway with env-based endpoint configuration, prompt loading, injected-test doubles, and deterministic fallback when no provider is configured.
- Real LLM call integration points for moderation, character generation, location weaving with one retry, ending generation, and settlement echo generation.
- Dependency-free SMTP mail transport adapter with env-based configuration, background mail queue processing, retry-on-failure semantics, and simulated-row preservation for non-whitelisted demo recipients.
- Mail reply ingestion via service method and `POST /api/mail/reply`, converting replies from known character emails into narration inputs.
- Dependency-free command printer driver with env-based configuration, structured ticket payloads for thermal-printer bridge scripts, and retry-on-failure queue semantics.
- HTTP demo hardening with a dependency-free sliding-window rate limiter for mutating routes and a request body size guard that rejects oversized JSON before dispatch.
- Prompt placeholder files under `server/prompts/`.
- `unittest` coverage for core service and HTTP contracts.

Remaining beyond this first pass:

- No known backend gaps remain in the current PRD scope.
