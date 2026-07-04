# EOOVE coStory Engine Backend

Dependency-light Python backend for the v4 "hackathon inside a hackathon"
project engine.

The backend owns the simulated hackathon workspace: editions, phases, AI
hackers, AI judges, teams, submitted ideas, project tasks, commit logs,
generated demo artifacts, agent conversations, memories, print tickets, mail
notifications, and admin controls. The frontend talks to this service through
JSON APIs under `/api/*`.

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

The app uses local SQLite storage by default. If `EOOVE_DB_PATH` points to a
file, the backend creates parent directories and persists project state across
process restarts.

## Environment

- `EOOVE_HOST`: HTTP bind host.
- `EOOVE_PORT`: HTTP bind port.
- `EOOVE_DB_PATH`: SQLite path.
- `EOOVE_STATIC_ROOT`: built frontend directory served for non-API routes.
- `EOOVE_LLM_ENDPOINT`: optional JSON LLM endpoint.
- `EOOVE_LLM_API_KEY`: optional bearer token for the LLM endpoint.
- `EOOVE_LLM_MODEL`: defaults to `gpt-5.4-mini`; requests do not send
  `thinking` or `reasoning`.
- `MAIL_WHITELIST`: comma-separated addresses allowed for real mail sending.
- `EOOVE_SMTP_HOST`, `EOOVE_SMTP_PORT`, `EOOVE_SMTP_USERNAME`,
  `EOOVE_SMTP_PASSWORD`, `EOOVE_MAIL_FROM`, `EOOVE_SMTP_TLS`: SMTP settings.
- `EOOVE_PRINTER_COMMAND`: optional local print command for direct processing.
- `EOOVE_PRINTER_TIMEOUT`: direct print command timeout in seconds.

## Product Loop

One edition is a simulated AI hackathon. The backend seeds AI hackers and
judges, forms teams, accepts audience ideas, then advances through six phases:

1. `opening` - 2 minutes
2. `early_dev` - 4 minutes
3. `mid_crisis` - 3 minutes
4. `deadline` - 2 minutes
5. `pitch` - 3 minutes
6. `awards` - 1 minute

One edition is budgeted to 15 minutes for the first demo build.

Audience flow:

1. Submit an idea with optional investor name and email.
2. Receive an idea id and receipt number immediately.
3. A receipt print ticket is queued.
4. On the next tick, AI teams claim waiting ideas and split project tasks.
5. Agents write task outputs and commit logs while updating a real demo
   artifact.
6. Each claimed idea gets a self-contained HTML demo URL that can be opened or
   embedded.
7. During `pitch` and `awards`, judges review the current artifact; ideas
   receive reviews, ranks, certificate print tickets, leaderboard tickets, and
   optional mail notifications.

The backend keeps the demo running without an LLM endpoint. If the LLM is not
configured or returns malformed output, deterministic fallback still writes
tasks, commits, HTML artifacts, conversations, memories, intents, project
updates, and events.

## API

All responses are JSON. API errors use this envelope:

```json
{"error":{"code":"REJECTED","message":"..."}}
```

Unknown `/api/*` routes return JSON `NOT_FOUND`, even when static file serving
is enabled.

### Submit Idea

```http
POST /api/idea
Content-Type: application/json

{
  "text": "给猫做相亲App",
  "investorName": "七色",
  "email": "hero@example.com"
}
```

Response:

```json
{
  "ideaId": 1,
  "receiptNo": "E01-I0001"
}
```

Validation is local and happens before persistence or printing:

- `text` is required.
- `text` must be at most 30 characters.
- Sensitive content is rejected with `REJECTED`.
- `investorName` and `email` are optional.

### Track Idea

```http
GET /api/idea/1
```

Response:

```json
{
  "status": "developing",
  "projectId": 1,
  "teamName": "泡面独角兽",
  "currentForm": "给猫做相亲App",
  "progress": 18,
  "currentBug": "给猫做相亲App 的原型会把猫的照片识别成需求文档",
  "artifact": {
    "artifactId": 1,
    "type": "html",
    "title": "给猫做相亲App Demo",
    "summary": "泡面独角兽 做出的可演示原型: 给猫做相亲App",
    "version": 2,
    "url": "/api/artifact/1"
  },
  "gossip": ["泡面角有人提到了你的idea: 给猫做相亲App"],
  "review": null,
  "rank": null
}
```

Idea status values include `pooled`, `developing`, `pivoted`, `presented`,
`awarded`, and `carried_over`.

### Idea Demo Page

```http
GET /idea/1
```

This backend-rendered HTML page is the URL encoded in receipt QR codes. Before
the project is complete it shows the idea status, team, progress bar, A2A task
movement, commits, and the current temporary artifact link if one exists. Once
progress reaches 100, the same route returns the final self-contained HTML demo.

Receipt and certificate QR codes use:

```text
https://eoove.tianmiao.fun/idea/{id}
```

Override the public host with `EOOVE_PUBLIC_BASE_URL` for non-production demos.

### World Stream

```http
GET /api/world?after=0
```

Response:

```json
{
  "edition": {"no": 1, "phase": "early_dev", "phaseEndsAt": 1783152000},
  "agents": [
    {
      "id": "h_backend",
      "name": "后端仔·倔",
      "location": "工位区A",
      "teamId": "team_1",
      "talking": false
    }
  ],
  "conversations": [
    {
      "id": 1,
      "location": "泡面咖啡角",
      "lines": [{"speakerId": "h_backend", "text": "..."}]
    }
  ],
  "projects": [
    {
      "projectId": 1,
      "ideaId": 1,
      "teamName": "泡面独角兽",
      "ideaText": "给猫做相亲App",
      "currentForm": "给猫做相亲App",
      "progress": 18,
      "currentBug": "...",
      "artifact": {"artifactId": 1, "type": "html", "url": "/api/artifact/1"}
    }
  ],
  "events": [
    {"id": 1, "type": "idea_received", "ideaId": 1, "receiptNo": "E01-I0001"}
  ]
}
```

The `after` cursor is an event id. The endpoint returns up to 100 events with
`id > after`.

### Project Detail

```http
GET /api/project/1
```

Response:

```json
{
  "projectId": 1,
  "ideaId": 1,
  "receiptNo": "E01-I0001",
  "teamName": "泡面独角兽",
  "ideaText": "给猫做相亲App",
  "currentForm": "给猫做相亲App",
  "progress": 18,
  "currentBug": "...",
  "tasks": [
    {
      "id": 1,
      "title": "产品定义",
      "ownerAgentId": "h_backend",
      "status": "done",
      "output": "MVP 不是完整实现...,而是展示用户输入后得到一个可解释结果。"
    }
  ],
  "commits": [
    {
      "id": 1,
      "agentId": "h_backend",
      "message": "完成产品定义",
      "diffSummary": "MVP 不是完整实现...",
      "artifactId": null,
      "createdAt": 1783152000
    }
  ],
  "artifact": {
    "artifactId": 1,
    "type": "html",
    "title": "给猫做相亲App Demo",
    "summary": "泡面独角兽 做出的可演示原型: 给猫做相亲App",
    "version": 2,
    "url": "/api/artifact/1"
  }
}
```

The project id is currently the idea id. Tasks, commits, and artifacts are
stored in SQLite and survive process restarts.

### Artifact

```http
GET /api/artifact/1
```

Response:

```json
{
  "artifactId": 1,
  "ideaId": 1,
  "version": 2,
  "type": "html",
  "title": "给猫做相亲App Demo",
  "summary": "泡面独角兽 做出的可演示原型: 给猫做相亲App",
  "contentType": "text/html; charset=utf-8",
  "body": "<!doctype html>..."
}
```

Artifacts are actual self-contained HTML demos. They are not guaranteed to be a
complete or perfectly faithful implementation of the submitted idea, but every
claimed project must have something visible and runnable.

### Agent Detail

```http
GET /api/agent/h_backend
```

Response:

```json
{
  "card": {
    "id": "h_backend",
    "name": "后端仔·倔",
    "persona": "Rust原教旨",
    "stack": "Rust",
    "catchphrase": "这个用Rust重写一遍就好了",
    "role": "hacker"
  },
  "intent": "想偷看三号桌的进度",
  "memories": [
    {"id": 1, "text": "听说新点子是给猫做相亲App...", "importance": 3}
  ]
}
```

Unknown agents return `NOT_FOUND`.

### Host Controls

```http
POST /api/host
Content-Type: application/json

{"action":"skip_phase","phase":"pitch"}
```

Supported actions:

- `start`: dangerous reset through the host path.
- `skip_phase`: jump to the provided phase; if omitted or invalid, jump to the
  next phase.
- `finale`: jump to `awards` and generate awards.

No host token is currently required by this backend.

### Admin

```http
GET /api/admin
POST /api/admin
POST /api/admin/reset
```

`GET /api/admin` returns:

```json
{
  "storyBackground": "",
  "beatIntervalSeconds": 25,
  "generationPaused": false,
  "stats": {
    "ideas": 1,
    "agents": 12,
    "events": 4,
    "pendingPrintJobs": 1
  }
}
```

`POST /api/admin` accepts partial updates:

```json
{
  "storyBackground": "今晚的AI黑客松发生在评委休息区旁边。",
  "beatIntervalSeconds": 30,
  "generationPaused": false
}
```

`beatIntervalSeconds` must be between `1` and `3600`.

`POST /api/admin/reset` requires exact confirmation:

```json
{"confirm":"RESET"}
```

Reset clears editions, agents, teams, ideas, project tasks, project commits,
artifacts, memories, conversations, events, print queue, mail queue, and input
logs, then starts a fresh first edition. Admin settings are preserved.

### Print Proxy

The cloud server never needs inbound access to a local printer. A booth laptop
or local agent polls these endpoints over outbound HTTP(S). No token or auth
header is required.

Fetch pending tickets:

```http
GET /api/print/pending?limit=5
```

Response:

```json
[
  {
    "ticketId": 1,
    "kind": "receipt",
    "payload": {
      "ideaId": 1,
      "receiptNo": "E01-I0001",
      "idea": "给猫做相亲App",
      "investorName": "七色",
      "editionNo": 1,
      "trackingId": "E01-I0001",
      "qrUrl": "https://eoove.tianmiao.fun/idea/1"
    }
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

Print semantics:

- Pending tickets are delivered at least once.
- Ack is idempotent.
- The local print agent should keep its own printed-id set to avoid duplicate
  physical prints.
- Pending backlog is capped; when it exceeds 20, old `receipt` tickets are
  marked `dropped` first.

Minimal polling agent:

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

### Mail Reply

```http
POST /api/mail/reply
Content-Type: application/json

{"email":"hero@example.com","text":"继续跟进这个项目"}
```

Current response:

```json
{"accepted":true}
```

Outbound mail rows are queued when an idea with email is claimed or awarded.
Real SMTP send only happens when the recipient is in `MAIL_WHITELIST`; otherwise
rows are stored as `simulated`.

## Background Runtime

`python3 -m server.app` starts three background loops:

- Tick loop: calls `maybe_beat()` using `beatIntervalSeconds`.
- Print loop: calls `process_next_print_job()`.
- Mail loop: calls `process_next_mail_job()`.

`generationPaused = true` stops project ticks but does not stop print or mail
processing.

## LLM Payload

When `EOOVE_LLM_ENDPOINT` is configured, the backend posts JSON like:

```json
{
  "model": "gpt-5.4-mini",
  "task": "weaver",
  "prompt": "...",
  "payload": {"task": "conversation", "mode": "economy"}
}
```

The backend deliberately does not include `thinking` or `reasoning` fields.

For conversation weaving, a valid result may include:

```json
{
  "lines": [{"speakerId": "h_backend", "text": "..."}],
  "memories": {"h_backend": "Heard about the new idea."},
  "intents": {"h_backend": "Wants to inspect another team's demo."}
}
```

Malformed or missing results fall back to deterministic local dialogue. Project
tasks, commits, and artifacts are still produced even when there is no LLM.

## Database

SQLite is initialized automatically on startup. The v4 core tables are:

- `editions`: edition number, phase, phase end timestamp.
- `agents`: AI hacker and judge cards, team, location, intent.
- `teams`: team membership and claimed idea.
- `ideas`: submitted idea, investor metadata, receipt, status, progress, bug,
  review, and rank.
- `project_tasks`: agent-owned work items for each project.
- `project_commits`: agent commit log and artifact release history.
- `artifacts`: self-contained HTML demos and metadata.
- `memories`: per-agent memory stream.
- `conversations`: generated dialogue lines.
- `events`: incremental world stream cursor.
- `admin_settings`: story background, tick interval, pause flag.
- `print_queue`: receipt, certificate, and leaderboard tickets.
- `mail_queue`: pending, sent, or simulated mail jobs.
- `inputs_log`: accepted input audit trail.

Legacy pre-v4 tables such as `world`, `templates`, `characters`, `batches`,
`stories`, `rules`, `acts`, and `oracle_pool` are archived with a
`_pre_v4_backup` suffix during initialization instead of being silently reused.

## Removed Routes

The v4 backend removed the v3 repair-zone and batch APIs. These routes should
return JSON `NOT_FOUND`:

- `GET /api/template`
- `POST /api/join`
- `GET /api/batch/:id`
- `GET /api/story`
- `POST /api/finale`
- `POST /api/act`
- `POST /api/leave`
- `GET /api/me/:id`
- `GET /api/card/:id`

Use `/api/idea`, `/api/world`, `/api/agent/:id`, `/api/host`, `/api/admin`, and
the print endpoints instead.

## Test

```bash
python3 -m unittest tests/test_backend_game.py
python3 -m compileall server tests
git diff --check
```
