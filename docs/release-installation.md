# Full-catalog release and installation (#15)

This is the operator sequence for the Frappe v16 Lithuanian Translation Catalog. Run commands from the **bench root**, replace `SITE` and `OTHER_SITE` with real site names, and pin `frappe_lt` to a reviewed release commit/tag rather than a moving branch. `docs/smoke-report.md` records the earlier one-`Item` smoke; the current `frappe_lt.verify.run` checks the full pinned catalog without compiling it.

## Release pin and catalog build

The release uses three authenticated, non-overlapping Catalog Segments: `frappe`, `erpnext-finance-commerce`, and `erpnext-operations`. Each candidate is bound by `frappe_lt/catalog_segments.json` to a segment manifest and the Release Inventory. The complete catalog has exactly one translation per Translation Key (Source Phrase **and** optional Frappe Context); it must cover every Release Inventory key with no extras, duplicates, fuzzy entries, or unreviewed English. Run the existing Catalog Quality Gate on the *combined* catalog: it enforces the glossary, Translation Exceptions, Preserved Tokens, whitespace and HTML rules and compiles a disposable PO/MO. A failed build must leave the active PO/MO untouched.

For release engineering, in an **isolated bench** with the pinned Frappe/ERPNext and Python/Babel from `frappe_lt/compatibility.json`:

1. Authenticate the Compatibility artifacts, segment candidates and Release Inventory. `frappe_lt.release_catalog.prepare(manifest_path)` builds/gates the entire catalog in isolation and writes a canonical `release_catalog.json` containing candidate bindings, inventory count and the expected PO/MO SHA-256 values. Review it and pin its *file* SHA-256 independently in `frappe_lt.release_catalog.PINNED_RELEASE_SHA256`. Ship that manifest and its exact PO bytes with the app. Compatibility's `mo_sha256` refers to the old smoke build; the **full** MO digest for installer, environment checks, runtime validation and verifier is the independently pinned release catalog value. CI checks the common inventory digest, then checks PO/MO against the release manifest, without comparing the two different MO digests.
2. With the pin fixed, `frappe_lt.release_catalog.assemble(po_path, mo_path)` regenerates the whole catalog from the three candidates, compiles via Frappe gettext, checks the pinned manifest and its expected digests, then publishes the PO/MO pair. Use staging paths in the isolated release bench first. Do not regenerate inventory or segment manifests during a site preflight; that would change the release being validated.
3. Confirm `sha256sum apps/frappe_lt/frappe_lt/release_catalog.json apps/frappe_lt/frappe_lt/locale/lt.po` against the pin and manifest respectively. `frappe_lt.release_catalog.verify_release()` is the read-only PO/key/context/digest check; `frappe_lt.release_catalog.verify_mo()` checks the bench MO once compiled. The MO is generated at `sites/assets/locale/lt/LC_MESSAGES/frappe_lt.mo` and is **bench-wide**, not a per-site file. CI should rebuild from the authenticated segments, byte-compare the shipped PO, compare a newly compiled MO digest, and test failures without replacing the active artifacts. An upstream Active Translation Key drift must block the deployment gate.

Release packaging must settle the pin **before** installation. Do not substitute the historical smoke MO SHA-256 for the full-catalog value. See [measurement protocol](release-measurements.md) for evidence required before declaring #15 ready.

## Site deployment

Prerequisites: ERPNext is already installed on `SITE`; all sites sharing this bench's MO are identified. Keep the original **private** `Fab@/LT_vertimas/output/lt-v16-translations.csv` (7,968 records, SHA-256 `490cf32b7d2e17406d012a993e7897659ef3fa7f71a4255721f947579e3aad0d`) available at a stable private path, along with the optional site-specific exception policy. Neither the v15 CSV nor a catalog candidate can replace it. Back up the site using the normal site backup process before maintenance. Use the *same* absolute `--package` and `--site-exceptions` paths in preflight and prepare; keep their bytes unchanged through final verify.

1. Pin Frappe/ERPNext and `frappe_lt` at the release versions, then fetch the app and install its requirements. Confirm the app pin and authenticated release artifacts before site mutation. The pinned smoke versions were Frappe `16.34.0` (`c1f1e8ec3708750d7254f7f99d869ffb9886f19f`) and ERPNext `16.35.0` (`12cd563fb9a79731f75ae2a45b1446a0a2dd9e74`); the live release manifest, not a moving `version-16` branch, determines the actual deployment pin.

   ```bash
   bench get-app frappe_lt https://github.com/lokysai/frappe-lt --branch RELEASE_TAG
   bench setup requirements
   bench --site SITE list-apps --format json
   ```

2. Run the **read-only** bench preflight *after* `get-app`, *before* `install-app`. It checks the authenticated release, ERPNext installation/app order, major/patch versions, real target Active Translation Keys including Frappe Context against the full Release Inventory, standard metadata and the #13 site override/migration safety classification. Unsupported majors fail. A v16 patch pair not listed in Compatibility is warned and allowed only on a successful **exact** target-inventory match; failed extraction or any mismatch fails closed. Record the returned release/inventory/MO digests and warning codes. Repeat for each site; preflight must not compile, publish, or rewrite the release artifacts or site data.

   ```bash
   bench --site SITE preflight-lithuanian-install \
     --package /private/path/lt-v16-translations.csv
   # If a policy is required, add the same --site-exceptions /private/path/site-exceptions.json
   # to BOTH preflight and prepare (and repeat consistently for each site).
   ```

3. Quiesce user traffic, jobs and writers according to the bench's normal operating procedure. Prepare authenticates the inputs, asks #13 for a private migration plan/run ID, saves only release/policy identifiers and enables `maintenance_mode=1` in the site config. Check the returned `state: prepared` and retain the run ID and private plan. Do this for *both* sites before changing the shared MO. Do not proceed if prepare fails or maintenance is off.

   ```bash
   bench --site SITE prepare-lithuanian-install \
     --package /private/path/lt-v16-translations.csv
   bench --site SITE show-lithuanian-install-status
   ```

4. Check the existing bench-shared MO before `install-app`. On a single-site bench the hook can create an absent MO from the **full shipped PO** after #13 recovery; if it already matches the release digest, reuse it. On a two-site bench with an absent MO, create it **now**, after both sites enter maintenance and both release pins agree, or the install hook will block automatic creation. Use the recorded `source_date_epoch` from Compatibility (replace the placeholder), compare the new SHA-256 to `release_catalog.json`, and run `frappe_lt.release_catalog.verify_mo()` when the Frappe environment is initialized. The Compatibility MO digest is a smoke baseline; do not compare it to the full release MO. Never force a compile over an existing incompatible MO while another site uses it. If the hook later reports `SHARED_MO_INCOMPATIBLE`, keep maintenance on, inspect all sites' release pins, plan a bench-wide MO change, then resume. On a single-site bench verify the hook's output instead of recompiling.

   ```bash
   SOURCE_DATE_EPOCH=PINNED_EPOCH bench compile-po-to-mo --app frappe_lt --locale lt --force
   sha256sum sites/assets/locale/lt/LC_MESSAGES/frappe_lt.mo
   ```

5. With **every site sharing the MO** held in maintenance, install on `SITE`. The install hook repeats the authenticated preflight before application-owned mutation, commits the Frappe install when needed, asks #13 to apply or recover its migration (including the **post-SQL-commit** report and Translation/boot-cache clear), then verifies the MO, applies/resumes the #8 Lithuanian profile and performs the final full-catalog verify and cache clear. `install-app` success alone does not authorize traffic.

   ```bash
   bench --site SITE install-app frappe_lt
   bench --site SITE show-lithuanian-install-status
   ```

6. If interrupted after `install-app` recorded the app, leave maintenance enabled and inspect state. Once the original inputs and compatible MO are restored, run the status-driven resume, not another blind `install-app` or a new #13 migration plan. It follows the real committed SQL marker, finishes report/cache recovery under the same run ID, then retries MO, profile and final verify. Recheck status after every attempt.

   ```bash
   bench --site SITE show-lithuanian-install-status
   bench --site SITE resume-lithuanian-install
   bench --site SITE show-lithuanian-install-status
   ```

7. After `install-app` has registered the app, run resume even when the hook appeared to succeed to obtain an explicit `state: verified` result. Require matching pinned PO/MO and entire Release Inventory with exact Frappe Context, completed #13 migration report/cache, `APPLIED` #8 profile, and expected app translation precedence. Run the separate non-compiling full-catalog check below on each site after the MO is in place. The `Item` -> `Prekė` check is additional only. Verify the other site against the **same MO** and finish its install/resume before release. Then manually return user traffic: check no failed phase remains and final cache clear succeeded; set `maintenance_mode` to `0` for each verified site, restart/resume paused workers or ingress, and perform a fresh-session Lithuanian UI check. Never clear maintenance on an incomplete or failed verify.

   ```bash
   bench --site SITE resume-lithuanian-install
   bench --site SITE execute frappe_lt.verify.run \
     --kwargs '{"site":"SITE","mode":"local"}'
   bench --site SITE clear-cache
   bench --site SITE set-config maintenance_mode 0 --parse
   ```

## Two sites, one MO

For `SITE` and `OTHER_SITE` on one bench, preflight both separately: their site overrides and migration plans can differ. Prepare both and remove both from traffic before compiling or changing the shared MO. The desired release manifest and digest must agree for every site. If the shared MO already has the pinned digest, install/verify the first site, then install/verify the second. If it is absent, create it during this coordinated maintenance window using the compile command above and check its digest **before** the first install; otherwise the two-site install hook blocks automatic creation. If it differs, stop and coordinate a compatible bench-wide release (or isolate the sites in separate benches); a site-specific `--force` compile is not safe. Final cache clear and verify are site-specific; return traffic for each only after it individually passes.

## Failures and recovery

| Point of failure | Safe next step |
| --- | --- |
| Release pin, source extraction, major, target inventory, site overrides, or private CSV fails preflight | Stop before prepare/install. Correct the release or site policy; rerun read-only preflight. Do not regenerate or replace release artifacts to make a failed site pass. |
| Prepare fails before maintenance or prepared inputs are missing/changed | Check `show-lithuanian-install-status`, repair the original private inputs and maintenance plan, and repeat a fresh safe preflight/prepare only if no migration was committed. Do not fabricate a run ID. |
| SQL transaction fails or commit is uncertain | Keep maintenance. Inspect #13's private report/SQL marker and use the same plan/run ID via resume after fixing the cause; never infer rollback from a missing final report. |
| SQL committed but report or post-commit cache clear failed | Keep maintenance. Retain CSV, policy, plan and run ID; resume so #13 completes recovery without deleting rows again. Profile and catalog verification remain blocked. |
| Install stopped before Frappe registered `frappe_lt` | Keep maintenance and inspect the saved plan and #13 SQL state. Resume requires the app to be installed; retry `install-app` only if there was no committed migration and the original preflight/plan remain valid. If state is ambiguous, resolve it from #13's durable evidence before retrying. |
| Shared MO absent/different, compile fails, or digest differs | Keep all affected sites in maintenance; reconcile release pins, compile during coordinated downtime only, compare digest, then resume. Do not replace a valid MO for another live site. |
| Profile apply, full verify, final cache clear, or command process fails | Keep maintenance even if Frappe lists `frappe_lt` as installed. Inspect status, fix the cause, resume; return traffic only after verified state. Concurrent attempts are blocked. |

The CLI emits statuses and error codes without private Translation values. Keep migration reports under the site's private directory; do not paste private plans, CSV rows, or site exception contents into public logs. A policy change after migration recovery requires a new safe plan before deployment. Removing the app does **not** restore migrated database Translation rows.

## Current checkout blockers

As inspected on 2026-09-23, Compatibility contains the one-`Item` smoke MO digest (`056750…`), while the full-catalog manifest expects `e83b10…`. The release gate and active-catalog checks use the independently pinned full release MO. The original private CSV is **not in this repository** (the locally available original has the pinned #13 SHA-256). For CI install and runtime jobs, configure the `FRAPPE_LT_ORIGINAL_CSV_URL` Actions secret with a private HTTPS download URL for the genuine `lt-v16-translations.csv`; CI downloads it to a temporary private directory, checks its SHA-256 against `frappe_lt.legacy_migration.PACKAGE_SHA256`, and removes it on exit. Without the secret or with wrong bytes, those jobs fail with a blocker before preflight/prepare. No pinned Bench install or performance dataset is available here. MO plus the named metadata also exceeds 5 MiB; see [measurements](release-measurements.md).
