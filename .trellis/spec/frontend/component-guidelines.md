# Component Guidelines

> How components are built in this project.

---

## Overview

Build typed function components with explicit prop objects. Keep the first
screen usable and product-like; do not ship a marketing-only landing page for
this project.

---

## Component Structure

For small components in `App.tsx`, define them below the main `App` component.
Extract to separate files when they grow independent behavior or are reused.

---

## Props Conventions

Use inline prop object types for small local components:

```tsx
function WallHeader({ story, progress }: { story: MockStoryState; progress: number }) {
  return <header>...</header>;
}
```

Use exported interfaces only when the prop type is shared across files.

---

## Styling Patterns

Use global CSS in `src/styles.css` for this app. Favor semantic class names,
OKLCH color tokens, responsive constraints, and `prefers-reduced-motion`
fallbacks.

---

## Accessibility

- All buttons need visible text or an accessible label.
- Preserve visible `:focus-visible` rings.
- Interactive controls must be at least 44px tall on touch targets.
- Do not rely on hover for core functionality.

---

## Common Mistakes

- Long Chinese display text can overflow in fixed mobile formats. Compose
  deliberate line breaks or use tighter mobile display scales instead of
  relying on browser CJK wrapping.
