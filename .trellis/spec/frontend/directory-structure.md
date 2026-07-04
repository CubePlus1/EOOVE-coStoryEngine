# Directory Structure

> How frontend code is organized in this project.

---

## Overview

The frontend is a Vite + React + TypeScript app rooted at `src/`. The first
implementation is intentionally compact: the showcase application lives in
`src/App.tsx`, global styles live in `src/styles.css`, and deterministic mock
story mechanics live under `src/mock/`.

---

## Directory Layout

```
src/
├── App.tsx              # Main multi-view experience
├── main.tsx             # React entry point
├── styles.css           # Global visual system and responsive rules
└── mock/
    ├── story.ts         # Typed mock world engine
    └── story.test.ts    # Engine contract tests
```

---

## Module Organization

- Keep page-level composition in `App.tsx` while the app is small.
- Move reusable or growing sections into `src/components/` once a component is
  used in more than one place or makes `App.tsx` hard to scan.
- Keep mock/server-contract simulators under `src/mock/` with colocated tests.
- Do not put generated build output or dependencies in git; `.gitignore` covers
  `dist/`, `node_modules/`, and TypeScript build info.

---

## Naming Conventions

- React components use PascalCase exports.
- Mock/domain helpers use camelCase functions and explicit exported types.
- Tests use `*.test.ts` beside the module they verify.

---

## Examples

- `src/mock/story.ts` is the current example for typed frontend domain logic
  that mirrors backend API concepts without calling the backend.
