# ERPNext v16 finance and commerce catalog — origin and reviewed candidate

The frozen `erpnext-finance-commerce` Catalog Segment contains **4,907** Active
Translation Keys (Accounts, Assets, CRM, Selling and Buying Source Locations and
reviewed ownership overrides). Before v15 import, its authenticated
provenance recorded **4,906 missing**, **one `new_ai` translation** (`Item → Prekė`)
and **zero exceptions or inherited entries**. The Release Inventory and partition
manifests retain their original key membership.

The two pinned Fab@ v15 CSVs provide **2,592 distinct, non-empty, exact Translation
Key matches** in the segment (including `Item → Prekė`), with no conflicting
duplicates. The authenticated originals are retained in `provenance.json` as
`inherited_v15` (or as `v15_original` for reviewed exceptions). Their canonical
original-set SHA-256 remains
`dbb132ad648c072fd30feacd3a6b7b2c17b32d329d4de9c16f7764d1347ecc21`.
Immediately after import and the first `IBAN` exception, provenance stood at
**2,591 inherited, 2,315 missing, one excepted**. The pre-registration import
baseline used:

```sh
"$VENV_PYTHON" -m frappe_lt.v15_origin_import \
  frappe_lt/compatibility.json \
  "$FAB_SOURCE_DIR/frappe-v15-lt.csv" \
  "$FAB_SOURCE_DIR/erpnext-v15-lt.csv" \
  --segment erpnext-finance-commerce
```

At import time, the finance import checked the then-registered Frappe and
operations candidates through the Catalog Quality Gate before and after
publishing the new provenance, using the pinned Frappe gettext compiler.
If publication or a post-publication check raises an error or catchable
interruption, the previous authenticated artifact set is restored. After a
hard process termination, commit-marker verification rejects an incomplete
publication on the next run. The import is byte-identical when repeated with
the same CSVs.

Authentication of a v15 original alone did not approve its translation. For
example, `Lead → Vadovauti` and `Account Number → Paskyros numeris` were
semantically wrong under `CONTEXT.md`. The reviewed finance candidate corrects
them while retaining the original texts for audit.

The first exact-key exception was `IBAN`: it is the international
bank-account identifier in all eight Bank Account, Bank Guarantee, Employee and
Payment Request Source Locations. It stays unchanged, with the authenticated
v15 original preserved and a reviewed, source-digest-bound exception. Three
additional technical exceptions were approved during candidate review below.

The contextless `Lead` key has been checked against its **23** CRM, customer,
communications and workspace Source Locations. All refer to a prospective
person or organization; the v15 original `Vadovauti` is a verb and is not an
acceptable CRM label. A reviewed Glossary Selector now binds
`Potencialus klientas` to the exact key and source digest, rejecting that
original and the other misleading literal forms. The candidate records
`Potencialus klientas` with `meaning_correction` review evidence.

Ten more exact-key selectors now pin `CONTEXT.md` forms for General Ledger,
Journal Entry, Cost Center, Invoice, Valuation Rate, Sales Invoice, Purchase
Invoice, Opportunity, Quotation and Purchase Order. Their inherited originals
remain authenticated; the candidate applies the required corrections. The
Invoice selector deliberately uses only the accepted form because the old
`faktūra` is also a substring of the correct
`Sąskaita faktūra`; forbidding that literal would reject the right result.

Two representative output keys are also reviewed at their actual Source
Locations: the Cheque Print Template's account-number `<label>` names a bank
account, while the CRM Email Campaign request for a `Lead {0}` names a
potential customer. Exact-key selectors require the appropriate Lithuanian
forms; regression checks cover preserved HTML attributes and the `{0}` token.
Both keys now have reviewed candidate translations.

Other targeted Source Location findings: `Debit` and `Credit` appear in account
balance rules, journal lines, bank reconciliation and ledger reports, where
their v15 originals `Debetas` and `Kreditas` preserve the accounting sides.
`Rate` occurs in item, sales, purchase and share-unit pricing locations (the
contextless key needs one shared price label), whereas `Exchange Rate` and
`Tax Rate` name currency conversion and tax percentage separately. The old
`Valuation Rate → Vertinimo Balsuok` is wrong in stock, asset capitalization,
manufacturing and sales-cost Source Locations; its selector requires
`Apskaitinė vieneto vertė`. The candidate applies this correction; `Debit`,
`Credit`, `Exchange Rate` and `Tax Rate` are accepted as-is on their reviewed
keys.

## Issue #14 reviewed candidate (2026-09-23)

The authenticated finance manifest SHA-256 is
`12aa36ed3df75d884b229f755d44f5fcafbe7db47f1e375f82107830b9e19127`.
Each of its **4,907 distinct exact Translation Keys** matches the Release
Inventory source digest. The keys have **20,642** recorded Source Locations in
total; **two** have a non-null Frappe Context. `Account` without a Frappe Context
is owned by Frappe, not this segment. The **4,907-key, key-by-key reviewed**
candidate is registered as `erpnext-finance-commerce` at
`frappe_lt/catalog_candidates/erpnext-finance-commerce.json` alongside `frappe`
and `erpnext-operations`. Its registered SHA-256 is
`2cc1adfcbb126b34a43067408dc63a5a47ae7a2a92c84a9bb7948c81f312cf7d`.
Each entry has a translation, source digest and reviewed reason with a
content-bound run ID. The `/tmp/opencode/finance-reviewed-*.jsonl` batches
record the per-key decisions that were registered; there is **no standalone
candidate generator**. The registered candidate is the artifact to check.

Current authenticated provenance is **2,590 inherited, 2,313 missing, four
excepted** (4,907 total). The 2,313 missing origin records correspond to
reviewed `new_translation` entries in the candidate; they do not mean 2,313
candidate translations are absent. The **2,592** authenticated v15 originals
are still 2,590 inherited records plus `v15_original` on `IBAN` and `BOM`.
Candidate review reasons, counted from the registered entries:

| Review reason | Keys |
| --- | ---: |
| `accepted_as_is` | 733 |
| `foreign_language_correction` | 37 |
| `grammar_correction` | 550 |
| `meaning_correction` | 759 |
| `punctuation_correction` | 21 |
| `terminology_correction` | 490 |
| `new_translation` | 2,313 |
| `approved_translation_exception` | 4 |
| **Total** | **4,907** |

The five correction reasons total **1,857 corrected inherited** entries;
candidate Translation Coverage is **4,907/4,907** with **four** exceptions.
Besides `IBAN`, the three additional reviewed technical exceptions retain
`[{0}] {1}` (Financial Report Template context), `<li>{}</li>` (HTML fragment)
and `BOM` (technical abbreviation) unchanged. `BOM` also preserves its
authenticated v15 original; the first two have no v15 exact-key original.

Source Location checks against the pinned inventory, in addition to the
findings above:

| Exact Translation Key | v15 original (before review) | Source Location finding and registered candidate (after review) |
| --- | --- | --- |
| `Account Number` | `Paskyros numeris` | `Account.account_number`, the account tree and trial-balance reports identify an accounting account number: **Apskaitos sąskaitos numeris**. |
| `General Ledger` | `Bendra Ledgeris` | Financial Reports workspace and `accounts/report/general_ledger` identify the **Didžioji knyga**. |
| `Journal Entry` | `žurnalo įrašą` | Journal Entry document, asset adjustments and reconciliation references name the document **Bendrojo žurnalo įrašas**. |
| `Cost Center` | `kaina centras` | The Cost Center accounting dimension occurs in invoices, assets and ledger entries; use **Sąnaudų centras**. |
| `Sales Invoice` / `Purchase Invoice` | `pardavimų sąskaita faktūra` / `pirkimo sąskaita faktūra` | Sales and Purchase Invoice documents require **Pardavimo sąskaita faktūra** / **Pirkimo sąskaita faktūra** (the latter corrects capitalization). |
| `Lead` | `Vadovauti` | All 23 locations name the CRM record or its relationship to a customer, email campaign or issue; use **Potencialus klientas**. |
| `Opportunity` / `Quotation` | `Galimybė` / `Pasiūlymas` | CRM records and sales-document references require **Pardavimo galimybė** / **Komercinis pasiūlymas**. |
| `Sales Order` / `Purchase Order` | `Pardavimo užsakymas` / `Pirkimo užsąkymas` | Selling and Buying documents distinguish the accepted sales term from the corrected **Pirkimo užsakymas**. |
| `Debit` / `Credit` | `Debetas` / `Kreditas` | Journal lines, Bank Reconciliation, General Ledger and Trial Balance preserve these accounting sides unchanged. |
| `Rate` / `Exchange Rate` / `Tax Rate` | `Kaina` / `Valiutos kursas` / `Mokesčio tarifas` | Item and document price fields use **Vieneto kaina**; the currency and tax terms remain unchanged. |
| `<label class="control-label" style="margin-bottom: 0px;">Account Number Settings</label>` | missing | Cheque Print Template bank-account settings need `<label class="control-label" style="margin-bottom: 0px;">Banko sąskaitos numerio nustatymai</label>`, preserving the entire markup. |
| `Please set an email id for the Lead {0}` | `Prašome nustatyti kliento el. Pašto adresą {0}` | CRM Email Campaign line 64 names a prospective customer; `Nustatykite potencialaus kliento {0} el. pašto adresą` preserves `{0}`. |

Local finance-candidate regression checks run the Catalog Quality Gate with a
stub compiler and verify **4,907/4,907 coverage**, reviewed evidence, reason
totals, source bindings, markup/tokens and deterministic output. This is a
local stub-compiler gate result, **not** confirmation of compilation by pinned
Frappe. The real pinned gettext compiler gate remains pending CI in a
Frappe-enabled environment; this local environment has no importable `frappe`
package. No active PO or MO was changed by candidate registration.
