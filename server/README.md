# EOOVE coStory Engine Backend

Dependency-light Python backend for the v3 "宇宙临时维修区" story engine.

The backend owns batching, AI resident fill-in, full-story weaving, the rule
ledger, finale generation, mail queues, and the pull-based print queue. The
frontend talks to this service through JSON APIs under `/api/*`.

## Run

```bash
python3 -m server.app
```

Defaults:

- Host: `127.0.0.1`
- Port: `8000`
- SQLite DB: `server/db.sqlite`
- Static root: `dist`

Example:

```bash
EOOVE_HOST=0.0.0.0 EOOVE_PORT=8000 EOOVE_DB_PATH=server/demo.sqlite python3 -m server.app
```

## Environment

- `EOOVE_HOST`: HTTP bind host.
- `EOOVE_PORT`: HTTP bind port.
- `EOOVE_DB_PATH`: SQLite path.
- `EOOVE_STATIC_ROOT`: built frontend directory served for non-API routes.
- `EOOVE_LLM_ENDPOINT`: optional JSON LLM endpoint.
- `EOOVE_LLM_API_KEY`: optional bearer token for the LLM endpoint.
- `EOOVE_LLM_MODEL`: defaults to `gpt-5.4-mini`; requests do not send `thinking` or `reasoning`.
- `MAIL_WHITELIST`: comma-separated addresses allowed for real mail sending.
- `EOOVE_SMTP_HOST`, `EOOVE_SMTP_PORT`, `EOOVE_SMTP_USERNAME`, `EOOVE_SMTP_PASSWORD`, `EOOVE_MAIL_FROM`, `EOOVE_SMTP_TLS`: SMTP settings.
- `EOOVE_PRINTER_COMMAND`: optional local print command for direct processing.
- `EOOVE_PRINTER_TIMEOUT`: direct print command timeout.

## V3 Flow

1. A player draws a template with `GET /api/template`.
2. The player joins with `POST /api/join`.
3. Join returns a `batchId` and `etaSeconds`; the client polls `GET /api/batch/:batchId`.
4. A batch completes when it has 3 human players, or when the 45-second window expires and AI residents fill it to 3.
5. The backend weaves one complete repair story, stores it in `stories`, creates one new universe rule in `rules`, and enqueues a `report` print ticket.
6. `GET /api/story?after=<storyId>` returns the world state, all rules, and new stories after the cursor.
7. `POST /api/finale` manually triggers the finale and enqueues a `finale` print ticket.

If the LLM endpoint is not configured or returns malformed data, deterministic fallback story generation keeps the demo running.

## API

All responses are JSON. Errors use:

```json
{"error":{"code":"REJECTED","message":"..."}}
```

### Template

```http
GET /api/template
```

Response:

```json
{
  "templateId": "t_001",
  "name": "煎饼侠王",
  "profile": "煎饼侠王: ...",
  "tags": ["莽", "馋", "轴"],
  "tagOptions": ["莽", "馋", "轴", "怪"]
}
```

### Join

```http
POST /api/join
Content-Type: application/json

{
  "templateId": "t_001",
  "edits": {"name": "铜锅侠", "tagSwap": "怪"},
  "origin": "赛博大唐",
  "quirk": "会给螺丝念诗",
  "email": "hero@example.com"
}
```

Response:

```json
{
  "charId": "c_ab12cd34",
  "batchId": "b_1234abcd",
  "etaSeconds": 45,
  "name": "铜锅侠",
  "profile": "铜锅侠: ...",
  "origin": "赛博大唐",
  "quirk": "会给螺丝念诗"
}
```

Join validation is local. Sensitive words and names/tags longer than 8 chars are rejected without mutating state.

### Batch Polling

```http
GET /api/batch/b_1234abcd
```

Response while gathering:

```json
{
  "status": "gathering",
  "countdown": 32,
  "members": [
    {"charId": "c_ab12cd34", "name": "铜锅侠", "origin": "赛博大唐", "type": "human"}
  ]
}
```

Response when done:

```json
{
  "status": "done",
  "countdown": 0,
  "members": [],
  "storyId": 1
}
```

### Story Stream

```http
GET /api/story?after=0
```

Response:

```json
{
  "world": {"phase": "running", "repairCount": 1, "finaleTarget": 10},
  "rules": [{"id": 1, "text": "自本次修复起,恐龙雨天优先购票"}],
  "stories": [
    {
      "id": 1,
      "kind": "repair",
      "incident": "第三维修舱的月亮突然开始漏电...",
      "segments": [{"text": "...", "focusCharIds": ["c_ab12cd34"]}],
      "members": [{"charId": "c_ab12cd34", "name": "铜锅侠", "origin": "赛博大唐", "type": "human"}],
      "rule": "自本次修复起,恐龙雨天优先购票",
      "personal": {"c_ab12cd34": "你在维修区被点名..."}
    }
  ]
}
```

The `after` cursor is a story id. `rules` is returned in full because the screen needs the ledger.

### Character

```http
GET /api/me/:charId
GET /api/card/:charId
POST /api/leave
```

`POST /api/leave` body:

```json
{"charId":"c_ab12cd34"}
```

Leaving marks the character ended and enqueues an `ending` ticket.

### Admin

```http
GET /api/admin
POST /api/admin
POST /api/admin/reset
```

`POST /api/admin` accepts:

```json
{
  "storyBackground": "维修区今晚只修会唱歌的裂缝。",
  "beatIntervalSeconds": 30,
  "generationPaused": false
}
```

`POST /api/admin/reset` requires exact confirmation:

```json
{"confirm":"RESET"}
```

Reset clears characters, batches, stories, rules, print/mail queues, and input logs, while preserving admin settings.

### Finale

```http
POST /api/finale
Content-Type: application/json

{}
```

No authentication is required. The response is the same shape as `GET /api/story`, with `world.phase = "finale"`.

### Print Proxy

The cloud server never pushes to a local printer. A booth laptop runs a local agent that polls these endpoints over outbound HTTP(S). No token or auth header is required.

Fetch pending tickets:

```http
GET /api/print/pending?limit=5
```

Response:

```json
[
  {
    "ticketId": 1,
    "kind": "charcard",
    "payload": {"charId": "c_ab12cd34", "name": "铜锅侠"}
  }
]
```

Ack printed tickets:

```http
POST /api/print/ack
Content-Type: application/json

{"ticketIds":[1,2,3]}
```

Response:

```json
{"acked":3}
```

Semantics:

- At-least-once delivery.
- Ack is idempotent.
- Local agent should keep its own printed-id set to avoid duplicate physical prints.
- If pending backlog exceeds 20, the backend drops `report` tickets first and keeps `charcard`, `ending`, and `finale`.

Minimal polling agent sketch:

```python
import json
import time
from urllib import request

BASE = "http://127.0.0.1:8000"

while True:
    tickets = json.load(request.urlopen(f"{BASE}/api/print/pending?limit=5"))
    printed = []
    for ticket in tickets:
        print(json.dumps(ticket, ensure_ascii=False))
        printed.append(ticket["ticketId"])
    if printed:
        body = json.dumps({"ticketIds": printed}).encode("utf-8")
        req = request.Request(
            f"{BASE}/api/print/ack",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        request.urlopen(req).read()
    time.sleep(2)
```

## Data Files

- `server/prompts/incidents.json`: incident pool and ending templates.
- `server/prompts/weaver.txt`: LLM instruction for whole-story repair weaving.
- `server/prompts/character.txt`, `ending.txt`, `moderation.txt`, `echo.txt`: retained prompt files for adapters/backward compatibility.

## Database

SQLite is initialized automatically on startup. The v3 core tables are:

- `world`: phase and repair counters.
- `templates`: reusable character template pool.
- `characters`: human and AI resident cards.
- `batches`, `batch_members`: gathering windows.
- `stories`: complete repair stories.
- `rules`: universe rule ledger.
- `print_queue`, `mail_queue`, `inputs_log`.

Older v1/v2 tables are archived with `_v2_backup` suffix during initialization instead of being silently reused.

## Test

```bash
python3 -m unittest tests/test_backend_game.py
python3 -m compileall server tests
```

## Notes

- `POST /api/act` is intentionally removed and returns `NOT_FOUND`.
- The backend does not expose v1 `cycle/round/hope/location` or v2 `acts/clue/twist/reveal/chapter fill` mechanics.
- Non-API GETs can serve a built frontend from `EOOVE_STATIC_ROOT`; unknown `/api/*` routes always return JSON `NOT_FOUND`.
