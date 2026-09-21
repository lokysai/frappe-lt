# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root.
- **`docs/adr/`** for ADRs that touch the area about to be changed.

If these files don't exist, **proceed silently**. Documentation is created lazily when terms or decisions are resolved.

## Layout

This is a single-context repository:

```
/
├── CONTEXT.md
├── docs/adr/
└── frappe_lt/
```

## Use the glossary's vocabulary

When output names a domain concept, use the term defined in `CONTEXT.md`. Do not drift to synonyms the glossary explicitly avoids.

If a needed concept isn't in the glossary, reconsider the terminology or note a genuine documentation gap.

## Flag ADR conflicts

Surface any conflict with an existing ADR explicitly rather than silently overriding it.
