# Quality Guidelines

> Code quality standards for backend development.

---

## Scenario: Text Adventure Backend Integrations

### 1. Scope / Trigger

- Trigger: backend work that adds HTTP routes, queue workers, external integrations, or demo hardening.
- Keep the backend dependency-light: use Python stdlib first (`http.server`, `sqlite3`, `threading`, `smtplib`, `subprocess`) and inject adapters so tests remain deterministic.

### 2. Signatures

- HTTP handler factory: `create_handler(service)`.
- Core service: `GameService(db_path="server/db.sqlite", llm=None, mail_transport=None, printer_driver=None)`.
- Production wrapper: `AsyncGameService(game, location_runtime)`.
- Queue workers:
  - `process_next_print_job() -> dict | None`
  - `process_next_mail_job() -> dict | None`
  - `process_async_location_job(payload) -> dict`
- External adapters:
  - LLM gateway methods: `generate_character`, `moderate`, `weave`, `generate_ending`, `generate_echo`.
  - Mail transport: `send(message)`.
  - Printer driver: `print_ticket(ticket)`.

### 3. Contracts

- API errors use `{ "error": { "code": "...", "message": "..." } }`.
- Business moderation rejection for `POST /api/act` remains HTTP 200 with `{ "accepted": false, "reason": "..." }`.
- Queue failure semantics:
  - Printer driver failure leaves `print_queue.status = 'pending'` for retry.
  - Mail transport failure leaves `mail_queue.status = 'pending'` for retry.
  - Simulated mail rows are retained as `simulated` and must not block later pending mail.
- Environment keys:
  - `EOOVE_LLM_ENDPOINT`, `EOOVE_LLM_API_KEY`
  - `MAIL_WHITELIST`
  - `EOOVE_SMTP_HOST`, `EOOVE_SMTP_PORT`, `EOOVE_SMTP_USERNAME`, `EOOVE_SMTP_PASSWORD`, `EOOVE_MAIL_FROM`, `EOOVE_SMTP_TLS`
  - `EOOVE_PRINTER_COMMAND`, `EOOVE_PRINTER_TIMEOUT`

### 4. Validation & Error Matrix

- Oversized HTTP body -> HTTP 413 with `REJECTED`; do not dispatch to game logic.
- Mutating route rate limit exceeded -> HTTP 429 with `QUOTA`; do not advance `world.round`.
- Unknown character/email -> `NOT_FOUND`.
- World settling or terminal async settlement pending -> `SETTLING`.
- Invalid action kind/scope/text -> `REJECTED`.
- LLM timeout or malformed output -> deterministic fallback; keep the pipeline unblocked.

### 5. Good/Base/Bad Cases

- Good: adapter injected in tests, fake raises once, queue row remains pending, second call succeeds.
- Base: no external env configured, deterministic fallback still supports local demo flow.
- Bad: external side effect happens before persistence validation or failure marks a queue row as completed.

### 6. Tests Required

- Route contract tests for every public endpoint, including error envelopes.
- Real temporary SQLite databases for service tests.
- Red/green tests for new queue or integration behavior:
  - success path,
  - failure/retry path,
  - unconfigured fallback path.
- Async production wrapper tests for behavior that differs from synchronous `GameService`, especially `/api/act`, mail replies, and round-60 settlement locking.
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
