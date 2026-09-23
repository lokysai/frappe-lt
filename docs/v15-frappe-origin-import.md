# Frappe v15 origin baseline (issue #12)

Run **before** reviewing or registering a Frappe candidate, from the repo root,
using a Python environment with the app's dependencies installed (for example,
the bench's `env/bin/python`):

```sh
"$VENV_PYTHON" -m frappe_lt.v15_origin_import \
  frappe_lt/compatibility.json \
  "$FAB_SOURCE_DIR/frappe-v15-lt.csv" \
  "$FAB_SOURCE_DIR/erpnext-v15-lt.csv"
```

Set `FAB_SOURCE_DIR` to the directory containing the two original CSVs.
The CSVs must be the original Fab@ files tracked at commit
`a9f6da80a96f41df8291eee35e78c68c9cf3870a`; their SHA-256 digests are
pinned in the importer. It accepts headerless `source,translation[,context]` rows
(empty context means null), matches only the frozen Frappe segment's **exact**
Translation Keys, and preserves the v15 translation text in `provenance.json`.
It rejects conflicting translations for a Frappe key, even across files.
Before import the Frappe segment has **6,344 missing, 0 inherited, 0 excepted**;
after import it has **3,346 inherited, 2,998 missing, 0 excepted**. A repeated
run with the same inputs produces the same bytes.
The complete imported `(Translation Key, original text)` set is pinned by
SHA-256 `7c343b91fef46a7574c1f0e32397b39b06834be600b848b1355fded1e758da1a`
in the regression test, so CI checks all 3,346 saved originals without
requiring the Fab@ repository at runtime.

These imported texts are **unreviewed originals**. For example, the saved
`User → Vartotojas`, `Submit → Pateikti`, and `Account → sąskaita` values do
not satisfy `CONTEXT.md`; a completed Frappe candidate must correct them,
record the correction reasons, and pass the Catalog Quality Gate before it
can be registered. Keys without an exact v15 match require `new_translation`
evidence rather than an invented inherited origin.

The importer verifies the authenticated release, partition, registered
candidates, and reports before changing anything. It rebuilds only
`provenance.json`, `inventory_report.json`, `inventory_report.md`, and their
digests in `compatibility.json`, then re-authenticates. The Release Inventory,
segment manifests, and candidate registry remain frozen. Run the tests with
`"$VENV_PYTHON" -m unittest frappe_lt.tests.test_v15_origin_import -q`. Tests use
local CSV fixtures and do not need the original Fab@ files.
