# State Management

> How state is managed in this project.

---

## Overview

Use React local state for the current frontend. Do not introduce a global state
library until multiple unrelated component trees need to share mutable state.
The app currently keeps the mock world, view mode, phone flow phase, selected
story tab, and compose form state in `App.tsx`.

---

## State Categories

- Local UI state: `useState` in the owning component.
- Derived state: `useMemo` only when grouping/filtering data for render.
- Persistent identity: `localStorage` key `world_charId`.
- URL state: `?view=wall|phone|card` selects the visible showcase surface and
  is updated with `history.replaceState`.
- Mock server state: `MockStoryState` from `src/mock/story.ts`.

---

## When to Use Global State

Promote state only when prop drilling or duplicated fetch/polling logic becomes
the bigger problem. Until then, prefer typed helper functions over a store.

---

## Server State

The mock engine is the temporary server-state source. When real API integration
is added, preserve the `frontend-design.md` polling contract:

- Story polling uses `/api/story?after=<actId>`.
- H5 pauses polling while `document.hidden`.
- H5 persists `world_charId`; story cursor persistence can use
  `world_lastActId` when implemented.
- Business rejections from `/api/act` are rendered as in-world text and should
  not use generic technical error copy.

---

## Common Mistakes

- Avoid storing view-only grouped arrays as state; derive them from the current
  story snapshot.
- Avoid mutating React state objects in place. Clone the mock world before
  passing it to mutating mock helpers.
