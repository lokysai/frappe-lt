# frappe_lt

`frappe_lt` is a GPL-3.0 Frappe v16 app for Lithuanian translations. The Translation Catalog is assembled from authenticated Frappe, ERPNext finance/commerce, and ERPNext operations Catalog Segments into one Lithuanian PO and bench-shared MO. The original contextless `Item` -> `Prekė` check is a historical smoke test, not proof of full Translation Coverage or a completed deployment.

[`CONTEXT.md`](CONTEXT.md) is the authoritative translation context. It was bootstrapped from `lokysai/Fab` commit `a40dc5f554df751a4227a192d55590fdd1eda4ed`; subsequent context maintenance happens only in this repository.

## Compatibility

The original smoke target was pinned exactly:

| Component | Version | Commit |
| --- | --- | --- |
| Frappe | 16.34.0 | `c1f1e8ec3708750d7254f7f99d869ffb9886f19f` |
| ERPNext | 16.35.0 | `12cd563fb9a79731f75ae2a45b1446a0a2dd9e74` |
| Python | 3.14.x | n/a |
| Babel | 2.16.0 | n/a |

ERPNext is required and must already be installed on every target site before `frappe_lt`. The release pin also binds the complete Release Inventory, segment candidates, PO and MO; see [release and installation runbook](docs/release-installation.md).

## Installation

Use the [release and installation runbook](docs/release-installation.md) from a bench with ERPNext already installed. Pin the app and compatible upstream versions, obtain the original private legacy CSV, and run the read-only preflight after `get-app` and before any site mutation. Prepare enters maintenance and saves the migration plan; `install-app` runs migration, MO and profile phases; resume and verify must complete before an operator returns traffic. A second site on the same bench shares the MO and requires a matching release digest.

## Verification

The final installation result must be `verified` while the site remains in maintenance. It checks exact Release Inventory Translation Keys (including Frappe Context), authenticated PO/MO digests, completed legacy migration report/cache, applied Lithuanian profile and catalog precedence. From the bench root, run the non-compiling full-catalog verifier after a matching MO exists:

```bash
bench --site SITE execute frappe_lt.verify.run \
  --kwargs '{"site":"SITE","mode":"local"}'
```

This checks completed migration evidence and profile, full installed-app translation precedence, and the historical `Item` -> `Prekė` smoke. It warns about a database override in local mode (`ci` blocks one). It does **not** compile or repair the MO. See the [runbook](docs/release-installation.md) for status/resume and manual traffic return; the [smoke report](docs/smoke-report.md) records the earlier one-key build only.

## Release inventory

The machine-readable compatibility contract is [`frappe_lt/compatibility.json`](frappe_lt/compatibility.json). It binds upstream commits and versions, artifact schema versions, Python/Babel versions, `SOURCE_DATE_EPOCH`, clean-install runtime metadata signatures, and the Release Inventory digest. Its `mo_sha256` describes the historical smoke MO. The full-catalog [`frappe_lt/release_catalog.json`](frappe_lt/release_catalog.json) binds all three authenticated candidates, the exact inventory key count, PO digest and **full** MO digest; its canonical SHA-256 must match the independent release pin in `frappe_lt/release_catalog.py`. See the [runbook](docs/release-installation.md#release-pin-and-catalog-build) for the publish and compile sequence.

Generate all release artifacts from a clean site after installing ERPNext but before installing `frappe_lt`:

```bash
bench --site inventory.localhost build-translation-inventory
```

The one public orchestration function is `frappe_lt.inventory.run`; the Bench command is only its pre-install transport. It requires exactly `frappe` and `erpnext`, rejects metadata that differs from the authenticated clean-install signatures (while accepting the exact system-generated Custom Fields and Property Setters), rejects dirty or unpinned upstream worktrees, checks the pinned Python and Babel versions, and validates every record in [`frappe_lt/provenance.json`](frappe_lt/provenance.json). Missing, unknown, duplicate, or contradictory provenance blocks generation before any artifacts are written. A successful run atomically writes these owned artifacts:

- `frappe_lt/release_inventory.json`
- `frappe_lt/inventory_report.json`
- `frappe_lt/inventory_report.md`
- `frappe_lt/provenance.json`
- `frappe_lt/catalog_segments.json`
- `frappe_lt/collision_resolutions.json`
- `frappe_lt/glossary_selectors.json`
- `frappe_lt/translation_exceptions.json`
- `frappe_lt/compatibility.json`, replaced last as the commit marker

For release comparison, pass both `--previous-inventory` and `--previous-compatibility`. The prior manifest digest must match the exact prior inventory bytes.

The [Frappe v15 origin baseline](docs/v15-frappe-origin-import.md) records exact-key inherited text before the Frappe segment's translation review. It does not register a reviewed Frappe catalog candidate.

## Legacy database Translation migration

Before enabling `frappe_lt`, supply the **original** private `Fab@/LT_vertimas/output/lt-v16-translations.csv` from the old import (7,968 records, SHA-256 `490cf32b7d2e17406d012a993e7897659ef3fa7f71a4255721f947579e3aad0d`). The importer used `frappe.utils.sanitize_html` on `Translated Text` before storing it. Neither v15 source CSV nor reviewed catalog provenance is an acceptable replacement. Missing or altered CSV blocks migration. Do not put this file or site exception policy in the repository or public reports.

For standalone migration work on a pinned site, the #13 commands are:

```bash
bench --site development.localhost preflight-legacy-translations --package /private/path/lt-v16-translations.csv
bench --site development.localhost apply-legacy-translations --package /private/path/lt-v16-translations.csv --run-id RUN_ID_FROM_PREFLIGHT
```

Use `--site-exceptions /private/path/site-exceptions.json` on **both** commands when needed. The file has `{"schema_version":1,"entries":[{"key":{"source":"Exact English source","context":null},"source_digest":"<Release Inventory source digest>","approver":"Name","reason":"Why English is intentional","revoked":false}]}`. The policy is separate from catalog Translation Exceptions. It permits intentional English at an active key only; Preserved Tokens and HTML equivalence still apply. A revoked or stale entry cannot waive the gate. English-valued extras outside the Release Inventory are reported; English seen in actual rendered output must also pass runtime validation before deployment.

Pass the same `--site-exceptions` path to `validate-lithuanian-runtime` to validate an approved, visible database-origin English value. The runtime report records the policy digest; a later verifier must pass `site_exception_path` to `validate_machine_report` with the same current policy. Revocation, a changed digest, unapproved English or English from an application still blocks validation.

The preflight reads the site without deleting anything. Its private `*.planned.json` includes exact names and stored comparison values, the run ID, duplicate/override classification and blocked findings. Reports reside under `sites/<site>/private/frappe_lt_legacy_migration/` (mode `0700`; report mode `0600`), with a 16 MiB report limit and a 50,000-row scan limit. Only counts and status are printed to the console. A changed plan or conflicting duplicate requires a fresh preflight. Apply locks and rechecks the full snapshot, removes exact matches in one SQL transaction, and clears Translation/boot cache after commit. If final reporting or cache clearing fails after commit, apply exits `3`; rerun **the same run ID** to finish recovery before deployment. Exit `0` means success/no-op, `1` means blocked/stale, `2` DB failure and `3` report/cache or uncertain-commit failure. Preflight report errors fail the command. Keep the original package, the original site exception policy (if used) and the private plan until finalization completes. A policy change during recovery finishes cache/report work but requires a fresh preflight before deployment. For an integrated #15 install, use the runbook's `preflight-lithuanian-install` and `prepare-lithuanian-install` instead of running standalone apply in parallel with the install hook.

Uninstall does **not** restore the deleted legacy database rows. Preserved site overrides continue to take precedence over application translations until explicitly changed by the site administrator.

## Catalog quality gate

`frappe_lt.catalog_quality.run` is the sole candidate-generation path. The Bench transport accepts only a candidate authenticated by [`frappe_lt/catalog_segments.json`](frappe_lt/catalog_segments.json):

```bash
bench catalog-quality-gate --candidate NAME --output /tmp/candidate.po --report /tmp/catalog-quality.json
```

Without a segment manifest, the candidate must cover the complete Release Inventory. A segment candidate must exactly cover its registered, non-overlapping manifest. The gate validates coverage, Preserved Tokens, strict HTML equivalence, Significant Whitespace, Translation Exceptions, collision resolutions, and literal Glossary Selectors before parsing and compiling through the installed Frappe v16 gettext implementation in a disposable workspace. It publishes the requested candidate PO atomically only after every check and compilation succeeds; it never publishes the application `frappe_lt/locale/lt.po`.

Reports are deterministic canonical schema-versioned JSON. Catalog Quality Gate report schema version `2` omits runtime duration so identical inputs produce identical bytes. Exit `0` means success, exit `1` means trusted input with catalog-quality errors, and exit `2` means untrusted input or a tool/filesystem failure. `--fail-fast` writes one error when the report destination remains writable. Inputs are limited to 32 MiB per artifact, 64 MiB across authenticated reads, 100,000 entries per list, 1,000 candidates, 10,000 selectors, 100,000 glossary forms, and 256 KiB per string. Findings are limited to 500,000 and reports to 32 MiB; larger inputs or outputs are rejected. CI authenticates the registry, runs every registered candidate, and always exercises the same trust path with an authenticated temporary candidate when the frozen production registry is empty.

## Running-interface validation

Run the reviewed browser and output denominator only against the pinned local release site:

```bash
bench --site development.localhost export-lithuanian-runtime-candidates \
  --output /tmp/runtime-candidate-snapshot.json
bench --site development.localhost validate-lithuanian-runtime
# Explicitly retain the same redacted text evidence for passing scenarios too:
bench --site development.localhost validate-lithuanian-runtime --diagnostic-sampling
```

The export command runs the same read-only preflight and discovery boundary, then atomically writes a canonical versioned snapshot containing the exact pinned upstream versions and commits, inventory digest, candidates, and coverage disposition. The validation command validates the strict [`runtime_scenarios.json`](frappe_lt/runtime_scenarios.json), [`runtime_role_profiles.json`](frappe_lt/runtime_role_profiles.json), and [`runtime_candidate_classifications.json`](frappe_lt/runtime_candidate_classifications.json) contracts before mutation. Both runtime entry points require exact installed apps and authenticate every clean-install standard-metadata category from Compatibility before discovery, control construction, fixture mutation, or browser work. Validation then discovers standard Frappe and ERPNext candidates without executing them, reports every unreviewed Runtime Coverage Gap, acquires one mutable-site lease, recovers a stale durable journal, creates only run-marked identities and fixtures, and invokes Frappe's supported `run-ui-tests` transport. Neither command creates a site backup.

Every document mutation has a durable before-image. Cleanup runs after pass, failure, blocked readiness, and browser errors; a later run recovers a process killed after a journaled mutation. Scenario failures, Blocked Runtime Scenarios, Runtime Coverage Gaps, English Fallback, Functional Layout Defects, and Runtime Cleanup Failures remain separate report facts. Credentials, cookies, and capability tokens are stored only in a mode-`0600` private browser plan and are deleted during cleanup.

The default report is written below `sites/development.localhost/private/frappe_lt_runtime/reports/<run-id>/`. `runtime-report.json` is the bounded canonical machine artifact and `runtime-report.md` is generated only from it. The report records exact browser, Cypress, Node, and relevant plugin versions, plus a bounded redacted Cypress log on tool or transport failure. Heavy Cypress evidence is disabled. By default, only failed or blocked scenarios publish allowlisted redacted JSON, HTML, or text below `evidence/<scenario-id>/`; `--diagnostic-sampling` explicitly enables the same bounded evidence for passing scenarios. Accepted evidence must use deterministic names, remain below the run root without symlinks, match its SHA-256 digest and exact byte count, and satisfy per-artifact, per-scenario, and per-run limits. Exit `0` requires no blocking facts; exit `1` means the report safely recorded at least one blocking fact. An evidence or report publication failure exits nonzero.

Runtime CI checks the validated report against its actual pass/fail and blocking findings, without assuming the old one-key catalog's English Fallback persists. [#16](https://github.com/lokysai/frappe-lt/issues/16) owns final running-interface release validation. Preserve raw evidence and resolve any English Fallback, Blocked Runtime Scenario or Runtime Coverage Gap before release.

The final beta candidate uses the fail-stop [release-candidate validation runbook](docs/release-candidate-validation.md). `capture-lithuanian-release-candidate` binds one clean commit and the authenticated release/catalog/runtime contracts before mutation. `finalize-lithuanian-release-candidate` consumes the fixed #15/#10 artifacts, independently checks runtime residue, and publishes bounded `release-candidate.md` followed atomically by the canonical `release-candidate.json` completion marker. These commands do not replace the installer, migration classifier, Catalog Quality Gate, browser runner, or external operator backup.

See the [one-time smoke report](docs/smoke-report.md) for the original one-key browser evidence, and [measurement protocol](docs/release-measurements.md) for metadata-size and performance evidence required by #15.

## Tests

Inside the bench environment:

```bash
env/bin/python -m unittest frappe_lt.tests.test_verify
env/bin/python -m unittest frappe_lt.tests.test_inventory
env/bin/python -m unittest frappe_lt.tests.test_catalog_quality
env/bin/python -m unittest frappe_lt.tests.test_runtime_validation
bench --site development.localhost run-tests --app frappe_lt
```

## License

GPL-3.0-only. See [LICENSE](LICENSE).
