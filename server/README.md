# EOOVE Backend

Dependency-free Python backend for the hackathon open-world text adventure game.

## Run

```bash
python3 -m server.app
```

The server listens on `127.0.0.1:8000` and stores SQLite data at `server/db.sqlite`.

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
