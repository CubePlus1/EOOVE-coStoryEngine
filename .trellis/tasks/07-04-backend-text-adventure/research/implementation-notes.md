# Implementation Notes

## Stack Decision

The repo currently has no backend code, no dependency manifest, and no installed Python web/test dependencies. Python 3.9 is available. The first backend implementation should therefore use:

- `sqlite3` for persistence.
- `http.server` for a small JSON API adapter.
- `unittest` for tests.

This keeps the hackathon backend runnable without package installation and leaves the game engine independent from the HTTP layer for a later FastAPI or LLM integration.

## Cross-Layer Contract

The frontend polls `/api/story?after=<actId>` and stores `world_charId` locally. The backend must keep response shapes stable:

- act `involved` field is a list of character ids.
- story response includes all characters and up to 50 incremental acts.
- personal timeline is derived from `acts.personal_json`.
- `hopeHint` is a band, not the numeric `hope`.

## Deterministic Fallbacks

Until LLM integration is configured, deterministic generation should provide:

- character names/profiles from the self description;
- narrative text that reflects the submitted action/oracle/heartbeat;
- bounded hope deltas;
- non-empty involved arrays;
- directive, chronicle, and personal text.
