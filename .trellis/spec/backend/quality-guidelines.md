# Quality Guidelines

> Code quality standards for backend development.

---

## Scenario: Open World Text Adventure Backend Integrations

### 1. Scope / Trigger

- Trigger: backend work that adds HTTP routes, SQLite schema, queue workers, external integrations, or demo hardening.
- Keep the backend dependency-light: use Python stdlib first (`http.server`, `sqlite3`, `threading`, `smtplib`, `subprocess`) and inject adapters so tests remain deterministic.
- Current product loop is v3 cosmic repair zone: template card draw -> join current batch -> batch gathers 3 humans or times out -> AI residents fill to 3 -> one whole repair story is woven -> one new rule is added to the rule ledger. Do not reintroduce v1 cycle/round/hope/location/oracle/node/settlement semantics or v2 acts/clue/twist/reveal/chapter fill semantics.

### 2. Signatures

- HTTP handler factory: `create_handler(service)`.
- Core service: `GameService(db_path="server/db.sqlite", llm=None, mail_transport=None, printer_driver=None, legends_path=None, incidents_path=None, print_token=None)`.
- Production wrapper: `AsyncGameService(game, weave_runtime=None)`.
- Public API routes:
  - `GET /api/template -> { templateId, name, profile, tags, tagOptions }`
  - `POST /api/join { templateId, edits?: { name?, tagSwap? }, origin?, quirk?, email? } -> { charId, batchId, etaSeconds, name, profile, origin, quirk }`
  - `GET /api/batch/:batchId -> { status, countdown, members, storyId? }`
  - `GET /api/story?after=<storyId> -> { world, rules, stories }`
  - `GET /api/me/:charId`
  - `GET /api/card/:charId`
  - `POST /api/leave`
  - `POST /api/mail/reply`
  - `GET /api/admin -> { storyBackground, beatIntervalSeconds, generationPaused, stats }`
  - `POST /api/admin { storyBackground?, beatIntervalSeconds?, generationPaused? } -> admin state`
  - `POST /api/admin/reset { confirm: "RESET" } -> admin state with reset: true`
  - `GET /api/print/pending?limit=5`
  - `POST /api/print/ack { ticketIds: [] }`
  - `POST /api/finale`
- Removed API route: `POST /api/act` must return `NOT_FOUND`.
- Queue workers:
  - `process_next_print_job() -> dict | None`
  - `process_next_mail_job() -> dict | None`
- External adapters:
  - LLM gateway methods still exist, but v2 join moderation is local sensitive-word/length validation. Weaver output must not include v1 `directive`, `hopeDelta`, `crossLocation`, or `oracleApplied`.
  - LLM requests include `model` and default to `gpt-5.4-mini`; do not send `thinking` or `reasoning` fields.
  - Mail transport: `send(message)`.
  - Printer driver: `print_ticket(ticket)`.

### 3. Contracts

- API errors use `{ "error": { "code": "...", "message": "..." } }`.
- v3 world response is `{ phase, repairCount, finaleTarget }`.
- v3 story response includes `id`, `kind`, `incident`, `segments`, `members`, `rule`, `personal`; it must not include `acts`, `location`, or `directive`.
- SQLite v3 schema owns:
  - `world(id, phase, repair_count, finale_target)`
  - `templates(id, name, profile, tags_json, used)`
  - `characters(..., tags_json, origin, quirk, last_seen_story, joined_at)`
  - `batches`, `batch_members`
  - `stories(kind, incident, segments_json, members_json, rule_id, personal_json, created_at)`
  - `rules(story_id, text, created_at)`
  - `admin_settings(id, story_background, beat_interval_seconds, generation_paused)`
- Legacy v1 tables may be archived with `_v1_backup` suffix during initialization; do not silently reuse v1 columns.
- Print proxy is pull-based: cloud never pushes to local printers. Pending/ack/finale endpoints do not require authentication; ack is idempotent. Pending queue preserves `charcard`, `report`, and `finale`; when backlog exceeds 20, drop `report` first.
- Admin settings control the live demo: `generation_paused` is retained for admin pause state, `beat_interval_seconds` controls background runtime timing, and `story_background` is injected into the weaver context.
- Story reset is a dangerous operation and must require exact `confirm: "RESET"`; reset clears story state and queues but preserves admin settings.
- Queue failure semantics:
  - Printer driver failure leaves `print_queue.status = 'pending'` for retry.
  - Mail transport failure leaves `mail_queue.status = 'pending'` for retry.
  - Simulated mail rows are retained as `simulated` and must not block later pending mail.
- Environment keys:
  - `EOOVE_HOST`, `EOOVE_PORT`, `EOOVE_DB_PATH`
  - `EOOVE_STATIC_ROOT` for the built frontend directory served by `server.app`
  - `EOOVE_LLM_ENDPOINT`, `EOOVE_LLM_API_KEY`, `EOOVE_LLM_MODEL`
  - `MAIL_WHITELIST`
  - `EOOVE_SMTP_HOST`, `EOOVE_SMTP_PORT`, `EOOVE_SMTP_USERNAME`, `EOOVE_SMTP_PASSWORD`, `EOOVE_MAIL_FROM`, `EOOVE_SMTP_TLS`
  - `EOOVE_PRINTER_COMMAND`, `EOOVE_PRINTER_TIMEOUT`
- Production static serving:
  - `python3 -m server.app` serves `/api/*` routes first.
  - Non-API `GET` requests are served from `EOOVE_STATIC_ROOT` (default `dist`).
  - Unknown non-API routes without a file extension fall back to `index.html` for the SPA.
  - Unknown `/api/*` routes must stay JSON `NOT_FOUND`; never return `index.html`.
  - Missing static assets with a file extension (for example `/assets/missing.js`) must return JSON `NOT_FOUND`, not `index.html`.

### 4. Validation & Error Matrix

- Oversized HTTP body -> HTTP 413 with `REJECTED`; do not dispatch to game logic.
- Mutating route rate limit exceeded -> HTTP 429 with `QUOTA`; do not create characters, stories, batches, or queue rows.
- Unknown character/email/template -> `NOT_FOUND`.
- Invalid join payload or edits -> `REJECTED`.
- Join edit sensitive word or >8 chars -> `{ error: { code: "REJECTED", message: "这个名字被世界吞掉了,换一个吧" } }`.
- Admin reset without exact `RESET` confirmation -> `REJECTED`.
- Admin beat interval outside 1..3600 seconds -> `REJECTED`.
- LLM timeout or malformed weaver output -> deterministic fallback; keep the pipeline unblocked.
- Unknown `GET /api/*` route -> HTTP 404 `NOT_FOUND` JSON envelope even when static serving is enabled.
- Missing static asset with a file extension -> HTTP 404 `NOT_FOUND`; unknown SPA route without extension -> `index.html`.

### 5. Good/Base/Bad Cases

- Good: adapter injected in tests, fake raises once, queue row remains pending, second call succeeds.
- Good: `POST /api/join` consumes exactly one template and returns `batchId` plus `etaSeconds` for the gathering page.
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
  - batch gathering, timeout AI fill, full-story weaving, rule ledger,
  - print proxy pending/ack without authentication,
  - admin pause/start, beat interval, background injection, and reset confirmation,
  - print/mail failure retry.
- Async production wrapper tests for behavior that differs from synchronous `GameService`.
- Demo hardening tests must assert rejected requests do not mutate world state.
- Static serving tests must assert `/` and real assets are served from `dist`, unknown SPA routes fall back to `index.html`, unknown `/api/*` routes return JSON `NOT_FOUND`, and missing assets do not fall back to `index.html`.

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
return {"world": {"phase": "running", "repairCount": 3, "finaleTarget": 10}}
```
