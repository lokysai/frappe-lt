# ERPNext v16 finance and commerce catalog — origin baseline

The frozen `erpnext-finance-commerce` Catalog Segment contains **4,907** Active
Translation Keys (Accounts, Assets, CRM, Selling and Buying Source Locations and
reviewed ownership overrides). Before review and v15 import, its authenticated
provenance recorded **4,906 missing**, **one `new_ai` translation** (`Item → Prekė`)
and **zero exceptions or inherited entries**. The Release Inventory and partition
manifests retain their original key membership.

The two pinned Fab@ v15 CSVs provide **2,592 distinct, non-empty, exact Translation
Key matches** in the segment (including `Item → Prekė`), with no conflicting
duplicates. The authenticated originals are now in `provenance.json` as
`inherited_v15` (or preserved as `v15_original` for a reviewed exception);
**2,315 keys remain missing**. Their canonical original-set SHA-256
is `dbb132ad648c072fd30feacd3a6b7b2c17b32d329d4de9c16f7764d1347ecc21`.
The baseline can be reproduced before finance candidate registration with:

```sh
"$VENV_PYTHON" -m frappe_lt.v15_origin_import \
  frappe_lt/compatibility.json \
  "$FAB_SOURCE_DIR/frappe-v15-lt.csv" \
  "$FAB_SOURCE_DIR/erpnext-v15-lt.csv" \
  --segment erpnext-finance-commerce
```

The finance import checks both already-registered candidates through the
Catalog Quality Gate before and after publishing the new provenance, using
the pinned Frappe gettext compiler; run it in a Frappe-enabled environment.
If publication or a post-publication check raises an error or catchable
interruption, the previous authenticated artifact set is restored. After a
hard process termination, commit-marker verification rejects an incomplete
publication on the next run. The import is byte-identical when repeated with
the same CSVs.

These texts are **unreviewed v15 originals**. In particular, old translations
such as `Lead → Vadovauti` and `Account Number → Paskyros numeris` require semantic
correction under `CONTEXT.md`; they must not be registered as accepted inherited
translations merely because the original text is authenticated. The Catalog
Quality Gate continues to validate both registered Frappe and operations
candidates against the refreshed provenance. The finance candidate review and
publication are still outstanding.

One exact-key exception has been reviewed so far: `IBAN` is the international
bank-account identifier in all eight Bank Account, Bank Guarantee, Employee and
Payment Request Source Locations. It stays unchanged, with the authenticated
v15 original preserved and a reviewed, source-digest-bound exception. The
current origin counts are **2,591 inherited, one excepted, 2,315 missing**;
this is provenance progress, not a claim of completed candidate review.

The contextless `Lead` key has been checked against its **23** CRM, customer,
communications and workspace Source Locations. All refer to a prospective
person or organization; the v15 original `Vadovauti` is a verb and is not an
acceptable CRM label. A reviewed Glossary Selector now binds
`Potencialus klientas` to the exact key and source digest, rejecting that
original and the other misleading literal forms. Its candidate translation
and correction evidence must still be recorded when the finance candidate is
ready.

Ten more exact-key selectors now pin `CONTEXT.md` forms for General Ledger,
Journal Entry, Cost Center, Invoice, Valuation Rate, Sales Invoice, Purchase
Invoice, Opportunity, Quotation and Purchase Order. Their inherited originals
are still intact; the selectors identify concrete corrections to require at
candidate review. The Invoice selector deliberately uses only the accepted
form because the old `faktūra` is also a substring of the correct
`Sąskaita faktūra`; forbidding that literal would reject the right result.

Two representative output keys are also reviewed at their actual Source
Locations: the Cheque Print Template's account-number `<label>` names a bank
account, while the CRM Email Campaign request for a `Lead {0}` names a
potential customer. Exact-key selectors require the appropriate Lithuanian
forms; a regression test checks a reviewed example of each against preserved
HTML attributes and the `{0}` token. Both keys still await candidate review
evidence and an approved candidate translation.

Other targeted Source Location findings: `Debit` and `Credit` appear in account
balance rules, journal lines, bank reconciliation and ledger reports, where
their v15 originals `Debetas` and `Kreditas` preserve the accounting sides.
`Rate` occurs in item, sales, purchase and share-unit pricing locations (the
contextless key needs one shared price label), whereas `Exchange Rate` and
`Tax Rate` name currency conversion and tax percentage separately. The old
`Valuation Rate → Vertinimo Balsuok` is wrong in stock, asset capitalization,
manufacturing and sales-cost Source Locations; its selector requires
`Apskaitinė vieneto vertė`. This is a targeted review finding, not approval
of the other inherited strings.
