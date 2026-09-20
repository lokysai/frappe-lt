# Translation Context

This file is the authoritative, narrowly scoped bootstrap glossary for issue #2. It derives its terminology from `lokysai/Fab` issue #1; broader catalog, migration, profile, and browser-automation decisions remain outside this slice.

## Scope

- Supported applications: pinned Frappe v16 and ERPNext v16 only.
- A **Translation Key** is source text plus optional Frappe Context.
- A **native catalog** is the app's gettext PO source compiled by Frappe to MO.
- **App precedence** means later installed apps override an earlier app's translation for the same Translation Key.
- A **database override** is a `Translation` row loaded after application catalogs. It can mask app origin.
- **English Fallback** is source text shown because no active Lithuanian translation resolved.

## Protected Term

| Translation Key | Lithuanian | Context | Rule |
| --- | --- | --- | --- |
| `Item` | `Prekė` | none | Exact translation required. |

`Item` means the ERPNext stock master concept in this slice. Avoid `Elementas`, `Daiktas`, and variants with added words.

## Catalog Contract

- `frappe_lt/locale/lt.po` contains exactly one non-header message.
- The message is singular, non-fuzzy, translated, and has no gettext/Frappe context.
- The exact mapping is `Item` -> `Prekė`.
- No CSV translation catalog, custom loader, DocType, fixture, install service, or catalog synchronization logic belongs to issue #2.
- The generated MO is reproducible build output and is not versioned.

## Runtime Proof

Proof separates app origin from effective runtime behavior. Frappe and ERPNext must not supply contextless Lithuanian `Item`; adding `frappe_lt` must yield `Prekė`. A contextless Lithuanian database row for `Item` invalidates effective-runtime origin proof even when its text happens to equal `Prekė`.
