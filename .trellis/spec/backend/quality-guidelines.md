# Quality Guidelines

> Code quality standards for backend development.

---

## Scenario: Open World Text Adventure Backend Integrations

### 1. Scope / Trigger

- Trigger: backend work that adds HTTP routes, SQLite schema, queue workers, external integrations, or demo hardening.
- Keep the backend dependency-light: use Python stdlib first (`http.server`, `sqlite3`, `threading`, `smtplib`, `subprocess`) and inject adapters so tests remain deterministic.
- Current product loop is v2 open world: template card draw -> join -> first act -> global beats -> clue progression -> twist/reveal. Do not reintroduce v1 cycle/round/hope/location/oracle/node/settlement semantics.

### 2. Signatures

- HTTP handler factory: `create_handler(service)`.
- Core service: `GameService(db_path="server/db.sqlite", llm=None, mail_transport=None, printer_driver=None, legends_path=None)`.
- Production wrapper: `AsyncGameService(game, weave_runtime=None)`.
- Public API routes:
  - `GET /api/template -> { templateId, name, profile, tags, tagOptions }`
  - `POST /api/join { templateId, edits?: { name?, tagSwap? }, email? } -> { charId, name, profile, tags, firstActId }`
  - `GET /api/story?after=<actId> -> { world, acts, characters }`
  - `GET /api/me/:charId`
  - `GET /api/card/:charId`
  - `POST /api/leave`
  - `POST /api/mail/reply`
  - `GET /api/admin -> { storyBackground, beatIntervalSeconds, generationPaused, stats }`
  - `POST /api/admin { storyBackground?, beatIntervalSeconds?, generationPaused? } -> admin state`
  - `POST /api/admin/reset { confirm: "RESET" } -> admin state with reset: true`
- Removed API route: `POST /api/act` must return `NOT_FOUND`.
- Queue workers:
  - `enqueue_beat(now=None) -> dict`
  - `process_next_weave_job() -> dict | None`
  - `process_next_print_job() -> dict | None`
  - `process_next_mail_job() -> dict | None`
- External adapters:
  - LLM gateway methods still exist, but v2 join moderation is local sensitive-word/length validation. Weaver output must not include v1 `directive`, `hopeDelta`, `crossLocation`, or `oracleApplied`.
  - LLM requests include `model` and default to `gpt-5.4-mini`; do not send `thinking` or `reasoning` fields.
  - Mail transport: `send(message)`.
  - Printer driver: `print_ticket(ticket)`.

### 3. Contracts

- API errors use `{ "error": { "code": "...", "message": "..." } }`.
- v2 world response is `{ legend, clueCount, nextTwistAt, nextRevealAt, actSeq }`.
- v2 act response includes `id`, `seq`, `type`, `narrative`, `chronicle`, `involved`, `importance`; it must not include `location` or `directive`.
- v2 act types are `act | twist | reveal | beat`.
- SQLite v2 schema owns:
  - `world(id, legend_index, legend_text, clue_count, act_seq)`
  - `templates(id, name, profile, tags_json, used)`
  - `characters(..., tags_json, last_seen_act, joined_at)`
  - `acts(seq, type, narrative, chronicle, involved_json, personal_json, importance, print_json, created_at)`
  - `weave_queue`
  - `admin_settings(id, story_background, beat_interval_seconds, generation_paused)`
- Legacy v1 tables may be archived with `_v1_backup` suffix during initialization; do not silently reuse v1 columns.
- Clue progression: each story act increments `clue_count`; clue 8 inserts a `twist` act and updates `world.legend_text`; clue 20 inserts a `reveal`, enqueues a reveal ticket, rotates to the next legend, and resets clue count to 0.
- Admin settings control the live demo: `generation_paused` blocks beat enqueueing, `beat_interval_seconds` controls background beat timing, and `story_background` is injected into the weaver context.
- Story reset is a dangerous operation and must require exact `confirm: "RESET"`; reset clears story state and queues but preserves admin settings.
- Queue failure semantics:
  - Printer driver failure leaves `print_queue.status = 'pending'` for retry.
  - Mail transport failure leaves `mail_queue.status = 'pending'` for retry.
  - Simulated mail rows are retained as `simulated` and must not block later pending mail.
- Environment keys:
  - `EOOVE_HOST`, `EOOVE_PORT`, `EOOVE_DB_PATH`
  - `EOOVE_LLM_ENDPOINT`, `EOOVE_LLM_API_KEY`, `EOOVE_LLM_MODEL`
  - `MAIL_WHITELIST`
  - `EOOVE_SMTP_HOST`, `EOOVE_SMTP_PORT`, `EOOVE_SMTP_USERNAME`, `EOOVE_SMTP_PASSWORD`, `EOOVE_MAIL_FROM`, `EOOVE_SMTP_TLS`
  - `EOOVE_PRINTER_COMMAND`, `EOOVE_PRINTER_TIMEOUT`

### 4. Validation & Error Matrix

- Oversized HTTP body -> HTTP 413 with `REJECTED`; do not dispatch to game logic.
- Mutating route rate limit exceeded -> HTTP 429 with `QUOTA`; do not create characters, acts, or queue rows.
- Unknown character/email/template -> `NOT_FOUND`.
- Invalid join payload or edits -> `REJECTED`.
- Join edit sensitive word or >8 chars -> `{ error: { code: "REJECTED", message: "这个名字被世界吞掉了,换一个吧" } }`.
- Admin reset without exact `RESET` confirmation -> `REJECTED`.
- Admin beat interval outside 1..3600 seconds -> `REJECTED`.
- LLM timeout or malformed weaver output -> deterministic fallback; keep the pipeline unblocked.

### 5. Good/Base/Bad Cases

- Good: adapter injected in tests, fake raises once, queue row remains pending, second call succeeds.
- Good: `POST /api/join` consumes exactly one template and returns `firstActId` for same-page reveal.
- Base: no external env configured, deterministic fallback still supports local demo flow.
- Bad: external side effect happens before persistence validation or failure marks a queue row as completed.
- Bad: adding v1 fields (`round`, `cycle`, `hopeHint`, `locations`, `directive`) to `/api/story`.

### 6. Tests Required

- Route contract tests for every public endpoint, including error envelopes.
- Real temporary SQLite databases for service tests.
- Schema tests for v2 world/templates and absence of `oracle_pool`.
- Red/green tests for changed behavior:
  - template draw and join consumption,
  - local join edit rejection with no mutation,
  - story response shape,
  - twist/reveal clue progression,
  - global beat queue and cold character selection,
  - admin pause/start, beat interval, background injection, and reset confirmation,
  - print/mail failure retry.
- Async production wrapper tests for behavior that differs from synchronous `GameService`.
- Demo hardening tests must assert rejected requests do not mutate world state.

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

Only mark queue rows complete after the external side effect succeeds. If the adapter raises, leave the row pending for retry.

#### Wrong

```python
return {"world": {"cycle": 1, "round": 3, "hopeHint": "mid"}, "locations": []}
```

This leaks removed v1 mechanics back into the API.

#### Correct

```python
return {"world": {"legend": legend, "clueCount": 3, "nextTwistAt": 8, "nextRevealAt": 20, "actSeq": 42}}
```
