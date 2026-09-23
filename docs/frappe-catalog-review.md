# Frappe v16 Catalog Segment review — issue #12

This review covers the **6,344** frozen Frappe Translation Keys in
`frappe_lt/catalog_segments/frappe.json` (Release Inventory digest
`111b42527f63668b884a30a0ce421c9e4da9308760baf8a143b1b4349e1107b7`).
All 570 keys also found in ERPNext remain owned by Frappe. The Release Inventory,
partition and three segment manifests retain their original key membership.
The completed candidate is `frappe_lt/catalog_candidates/frappe.json`; only the
Catalog Quality Gate can publish its PO, outside the active application locale.

## Origin and decisions

Before review, the authenticated baseline had **3,346 inherited v15 originals**
and **2,998 missing keys**, without Frappe exceptions. Original texts are retained
in `provenance.json`: translated records retain `translation`, and an inherited
record approved as an exception retains `v15_original`. The v15 original-set
digest is still asserted by `test_v15_origin_import.py`.

| Reviewed decision | Keys |
| --- | ---: |
| Accepted inherited unchanged | 1,018 |
| Corrected inherited: terminology | 414 |
| Corrected inherited: grammar | 899 |
| Corrected inherited: meaning | 821 |
| Corrected inherited: punctuation | 50 |
| Corrected inherited: foreign-language fragment | 72 |
| New Lithuanian translation | 2,902 |
| Approved, exact-key Translation Exception | 168 |
| **Total / covered** | **6,344 / 6,344 (100%)** |

The **2,256 corrected inherited** entries carry a specific reason and explanation;
every entry has `reviewed` status, `opencode` agent, `openai/gpt-6-sol` model and
a deterministic SHA-256 run ID. Among the 168 reviewed exceptions, **72** had an
authenticated v15 original and **96** were previously missing. Each approved
exception is bound to its exact key, Source Digest, review and authenticated
`approved_exception` origin. Technical values include HTTP methods, paper-size
codes, product names and code-only templates. Malformed upstream HTML fragments
remain unchanged only with a reviewed exact-key exception. The shared contextless
`Account` is **Sąskaita**, *not* an exception.

## Focused source-location review

Each table row counts unique Frappe Translation Keys with at least one relevant
Source Location or Source Phrase; categories overlap. These are focused review
counts, not additional coverage denominators. The classification uses case-folded
source paths and metadata DocType paths, plus whole-word source searches:

| Review area | Keys | Accepted | Corrected | New | Exceptions |
| --- | ---: | ---: | ---: | ---: | ---: |
| Authentication (login, password, OTP, OAuth; auth/twofactor sources) | 420 | 63 | 162 | 183 | 12 |
| Permissions (permission manager, user permissions, access denied) | 259 | 24 | 124 | 111 | 0 |
| Deletion (delete_doc, data deletion, delete messages) | 165 | 24 | 48 | 92 | 1 |
| Document states/actions (draft, submit, cancel, amend) | 127 | 11 | 47 | 69 | 0 |
| Contextless `Account` (accounting + Email Account locations) | 1 | 0 | 1 | 0 | 0 |
| Portal (portal, web form sources and metadata) | 204 | 32 | 93 | 78 | 1 |
| Print (printing, print format, printer metadata) | 331 | 53 | 127 | 109 | 42 |
| Email (email sources, templates, account/queue/notification metadata) | 528 | 76 | 233 | 193 | 26 |
| Administration (user, role, system settings, custom field sources) | 600 | 82 | 306 | 199 | 13 |

Specific Source Locations were checked for `Submit` versus portal form submission,
`User` versus `Employee`, security/permission denials, OTP re-registration,
deletion prompts and the accounting/email `Account` collision. The real-key
regression tests check the corrected meanings and polite action text. The Catalog
Quality Gate checks exact membership, source digests, tokens, HTML, whitespace,
glossary selectors, collisions, approved exceptions, PO parsing and gettext
compilation. CI compiles this and the registered operations candidate with the
pinned Frappe v16 toolchain, then repeats the Frappe gate and compares PO/report
bytes. No partial PO is installed or published to users.
