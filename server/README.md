# EOOVE Backend

Dependency-free Python backend for the hackathon open-world text adventure game.

## Run

```bash
python3 -m server.app
```

The server listens on `127.0.0.1:8000` and stores SQLite data at `server/db.sqlite`.

You can override runtime settings with environment variables:

- `EOOVE_HOST`
- `EOOVE_PORT`
- `EOOVE_DB_PATH`
- `EOOVE_LLM_ENDPOINT`
- `EOOVE_LLM_API_KEY`
- `EOOVE_LLM_MODEL` (defaults to `gpt-5.4-mini`; LLM requests do not send `thinking` or `reasoning` fields)
- `MAIL_WHITELIST`
- `EOOVE_SMTP_HOST`
- `EOOVE_SMTP_PORT`
- `EOOVE_SMTP_USERNAME`
- `EOOVE_SMTP_PASSWORD`
- `EOOVE_MAIL_FROM`
- `EOOVE_SMTP_TLS`
- `EOOVE_PRINTER_COMMAND`
- `EOOVE_PRINTER_TIMEOUT`

## Test

```bash
python3 -m unittest tests/test_backend_game.py
```

## API

The implemented JSON routes match the v2 open-world backend contract:

- `GET /api/template`
- `POST /api/join`
- `POST /api/leave`
- `GET /api/story?after=<actId>`
- `GET /api/me/:charId`
- `GET /api/card/:charId`
- `POST /api/mail/reply`

`POST /api/act` is intentionally removed in v2. Real LLM, mail, and printer integrations are represented by deterministic fallbacks and persisted queues.
