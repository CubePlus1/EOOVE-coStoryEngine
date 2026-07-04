# Quality Guidelines

> Code quality standards for backend development.

---

## Scenario: V4 AI Hackathon Backend Integrations

### 1. Scope / Trigger

- Trigger: backend work that adds HTTP routes, SQLite schema, queue workers,
  external integrations, demo hardening, or prompt contracts for the v4 AI
  hackathon engine.
- Keep the backend dependency-light: use Python stdlib first (`http.server`,
  `sqlite3`, `threading`, `smtplib`, `subprocess`) and inject adapters so tests
  remain deterministic.
- Current product loop is v4 AI hackathon project execution: edition -> phase
  -> tick -> team claims idea -> agents complete tasks and commits -> HTML
  artifact updates -> pitch -> awards -> next edition. Conversation is
  collaboration telemetry, not the primary product. Do not reintroduce v1
  cycle/round/hope/oracle/node/settlement semantics, v2 chapter sandwich
  mechanics, or v3 repair-zone template/batch story/rule-ledger mechanics.

### 2. Signatures

- HTTP handler factory: `create_handler(service, static_root=None)`.
- Core service:
  `GameService(db_path="server/db.sqlite", llm=None, mail_transport=None, printer_driver=None, economy_mode=True)`.
- Production wrapper: `AsyncGameService(game, weave_runtime=None)`.
- Public API routes:
  - `POST /api/idea { text, investorName?, email? } -> { ideaId, receiptNo }`
  - `GET /api/idea/:id -> { ideaId, receiptNo, status, teamName?, projectId?, currentForm, progress, currentBug, artifact?, gossip, review?, rank? }`
  - `GET /api/world?after=<eventId> -> { edition, agents, conversations, projects, events }`
  - `GET /api/project/:id -> { projectId, ideaId, receiptNo, teamName, ideaText, currentForm, progress, currentBug, tasks, commits, artifact }`
  - `GET /api/artifact/:id -> { artifactId, ideaId, version, type, title, summary, contentType, body }`
  - `GET /api/agent/:id -> { card, intent, memories }`
  - `POST /api/host { action: "start"|"skip_phase"|"finale", phase? } -> { edition }`
  - `GET /api/print/pending?limit=<n> -> [{ ticketId, kind, payload }]`
  - `POST /api/print/ack { ticketIds: [int] } -> { acked }`
  - `POST /api/mail/reply { email, text } -> { accepted }`
  - `GET /api/admin -> { storyBackground, beatIntervalSeconds, generationPaused, stats }`
  - `POST /api/admin { storyBackground?, beatIntervalSeconds?, generationPaused? } -> admin state`
  - `POST /api/admin/reset { confirm: "RESET" } -> admin state with reset: true`
- Removed API routes must return JSON `NOT_FOUND`: `/api/template`,
  `/api/join`, `/api/batch/:id`, `/api/story`, `/api/finale`, `/api/act`,
  `/api/leave`, `/api/me/:id`, `/api/card/:id`.
- Queue/runtime methods:
  - `maybe_beat(now=None, idle_seconds=25) -> dict | None`
  - `tick(now=None) -> { advanced, edition }`
  - `process_next_print_job() -> dict | None`
  - `process_next_mail_job() -> dict | None`
  - `process_next_weave_job() -> None`
- External adapters:
  - LLM gateway methods may remain broad for adapter compatibility, but v4 idea
    moderation is local sensitive-word/length validation.
  - LLM requests include `model` and default to `gpt-5.4-mini`; do not send
    `thinking` or `reasoning` fields.
  - Mail transport: `send(message)`.
  - Printer driver: `print_ticket(ticket)`.

### 3. Contracts

- API errors use `{ "error": { "code": "...", "message": "..." } }`.
- v4 phases are `opening`, `early_dev`, `mid_crisis`, `deadline`, `pitch`,
  `awards`. The initial demo budget is exactly 15 minutes per edition:
  2 + 4 + 3 + 2 + 3 + 1 minutes.
- `POST /api/idea` validation is local and must complete before inserting an
  idea or enqueuing a receipt ticket. Accepted ideas get `pooled` status,
  receipt number `E<edition>-I<seq>`, an `idea_received` event, and a `receipt`
  print ticket.
- `GET /api/world` uses an event cursor: return events where `id > after`,
  ordered ascending, with each event carrying its `id` and `type`.
- A tick must keep the simulation moving without an LLM endpoint:
  - claim waiting ideas into teams,
  - split project tasks across agents,
  - update project progress and bugs,
  - write project commits,
  - create or update a self-contained HTML artifact,
  - create conversations/gossip,
  - write agent memories and intents,
  - handle pitch reviews and awards when in those phases.
- SQLite v4 schema owns:
  - `editions(id, no, phase, phase_ends_at, started_at)`
  - `agents(id, name, persona, stack, catchphrase, role, team_id, location, intent, talking)`
  - `teams(id, edition_id, name, member_ids_json, idea_id)`
  - `ideas(id, text, investor_name, email, receipt_no, status, team_id, current_form, progress, current_bug, review, rank, created_at)`
  - `memories(id, agent_id, text, importance, created_at)`
  - `conversations(id, edition_id, location, agent_ids_json, lines_json, created_at)`
  - `events(id, edition_id, type, payload_json, created_at)`
  - `project_tasks(id, idea_id, team_id, title, owner_agent_id, status, output, created_at, updated_at)`
  - `project_commits(id, idea_id, agent_id, message, diff_summary, artifact_id, created_at)`
  - `artifacts(id, idea_id, version, type, title, summary, body, content_type, created_at, updated_at)`
  - `admin_settings(id, story_background, beat_interval_seconds, generation_paused)`
  - `print_queue(id, kind, payload_json, status)`
  - `mail_queue(id, idea_id, email, kind, in_world_reason, status, created_at)`
  - `inputs_log(id, type, payload_json, verdict, created_at)`
- Legacy pre-v4 tables may be archived with `_pre_v4_backup` suffix during
  initialization; do not silently reuse old columns.
- Print proxy endpoints do not require token authentication. Keep `token`
  parameters only as ignored compatibility arguments if needed.
- Host controls currently do not require a token. If auth is introduced later,
  tests and README must change in the same commit.
- Admin settings control the live demo: `generation_paused` blocks project ticks,
  `beat_interval_seconds` controls background tick timing, and
  `story_background` is persisted for operator context.
- Story reset is dangerous and must require exact `confirm: "RESET"`; reset
  clears v4 world state and queues but preserves admin settings, then reseeds a
  fresh first edition.
- Queue failure semantics:
  - Printer driver failure leaves `print_queue.status = 'pending'` for retry.
  - Mail transport failure leaves `mail_queue.status = 'pending'` for retry.
  - Simulated mail rows are retained as `simulated` and must not block later
    pending mail.
- Production static serving:
  - `python3 -m server.app` serves `/api/*` routes first.
  - Non-API `GET` requests are served from `EOOVE_STATIC_ROOT` (default `dist`).
  - Unknown non-API routes without a file extension fall back to `index.html`
    for the SPA.
  - Unknown `/api/*` routes must stay JSON `NOT_FOUND`; never return
    `index.html`.
  - Missing static assets with a file extension must return JSON `NOT_FOUND`,
    not `index.html`.

### 4. Validation & Error Matrix

- Oversized HTTP body -> HTTP 413 with `REJECTED`; do not dispatch to game
  logic.
- Mutating route rate limit exceeded -> HTTP 429 with `QUOTA`; do not create
  ideas, events, or queue rows.
- Unknown idea/agent/team -> `NOT_FOUND`.
- Invalid idea payload, empty idea, idea longer than 30 characters, or sensitive
  idea text -> `REJECTED` and no mutation.
- Admin reset without exact `RESET` confirmation -> `REJECTED`.
- Admin beat interval outside 1..3600 seconds -> `REJECTED`.
- Print ack without `ticketIds` list -> `REJECTED`.
- LLM timeout or malformed conversation output -> deterministic fallback; keep
  the pipeline unblocked.
- Unknown `GET /api/*` route -> HTTP 404 `NOT_FOUND` JSON envelope even when
  static serving is enabled.
- Missing static asset with a file extension -> HTTP 404 `NOT_FOUND`; unknown
  SPA route without extension -> `index.html`.

### 5. Good/Base/Bad Cases

- Good: adapter injected in tests, fake printer raises once, queue row remains
  pending, second call succeeds.
- Good: `POST /api/idea` rejects invalid text before writing `ideas`,
  `print_queue`, or `events`.
- Good: `tick()` with no LLM endpoint still produces claim, project update,
  tasks, commits, artifact, conversation, memory, and gossip evidence.
- Base: no external env configured, deterministic fallback still supports local
  demo flow.
- Bad: external side effect happens before persistence validation or failure
  marks a queue row as completed.
- Bad: adding old fields (`round`, `cycle`, `hopeHint`, `rules`, `stories`,
  `batchId`, `templateId`, `charId`) to v4 API responses.
- Bad: treating generated dialogue as the only output; every claimed project
  must have a visible artifact URL.
- Bad: reintroducing token requirements for `/api/print/pending` or
  `/api/print/ack` without an explicit product decision and matching tests.

### 6. Tests Required

- Route contract tests for every public endpoint, including error envelopes.
- Real temporary SQLite databases for service tests.
- Schema tests for v4 tables and absence of active legacy `characters`.
- Red/green tests for changed behavior:
  - idea submission, receipt number, and receipt print ticket,
  - receipt print payload includes unique tracking id and QR URL,
  - local idea rejection with no mutation,
  - next-tick idea claiming, task split, commit log, artifact creation, and
    first reaction/gossip,
  - project/artifact API contract,
  - world event cursor and agent memory detail,
  - host skip to pitch/awards and certificate/leaderboard output,
  - admin pause/start, beat interval, background injection, and reset
    confirmation,
  - print endpoints without auth and print/mail failure retry,
  - removed v3 routes returning JSON `NOT_FOUND`.
- Async production wrapper tests for behavior that differs from synchronous
  `GameService`.
- Demo hardening tests must assert rejected requests do not mutate world state.
- Static serving tests must assert `/` and real assets are served from `dist`,
  unknown SPA routes fall back to `index.html`, unknown `/api/*` routes return
  JSON `NOT_FOUND`, and missing assets do not fall back to `index.html`.

### 7. Wrong vs Correct

#### Wrong

```python
row = next_print_job()
mark_printed(row)
printer.print_ticket(row)
```

This loses jobs when the printer fails after the row is marked complete.

#### Correct

```python
row = next_print_job()
printer.print_ticket(ticket)
mark_printed(row)
```

Only mark queue rows complete after the external side effect succeeds. If the
adapter raises, leave the row pending for retry.

#### Wrong

```python
return {"world": {"cycle": 1, "round": 3, "hopeHint": "mid"}, "locations": []}
```

This leaks removed reincarnation mechanics back into the API.

#### Correct

```python
return {
    "edition": {"no": 1, "phase": "early_dev", "phaseEndsAt": 1783152000},
    "agents": [],
    "conversations": [],
    "projects": [],
    "events": [],
}
```

V4 responses are framed around the hackathon edition, agents, projects, and
event cursor.
