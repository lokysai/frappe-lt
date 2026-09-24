# Beta release-candidate validation (#16)

This runbook validates one reviewed `frappe_lt` commit against `development.localhost`. It reuses the #15 installer and #10 runtime commands. It does not install, classify migration rows, calculate catalog quality, or run a second browser path.

## Fixed evidence layout

Run from the Bench root. Replace `CANDIDATE_SHA` only with the full clean `frappe_lt` commit selected for this run. The final publication path is immutable:

```text
sites/development.localhost/private/frappe_lt_release_candidate/CANDIDATE_SHA/release-candidate.json
```

The directory must contain exactly these required inputs before finalization:

```text
candidate.json
preflight.json
prepare.json
install.json
install-status.json
migration.json
verify.json
runtime-candidates.json
runtime/runtime-report.json
performance.json
runtime-residue.json
tests.json
```

`release-candidate.md` is the human derivative. Finalization writes it first, then atomically writes `release-candidate.json` last as the completion marker. Absence of the machine file means the run was not published.

## Fail-stop sequence

Use a shell with `set -euo pipefail`. Do not continue after any nonzero command, malformed output, digest mismatch, blocked state, or missing artifact.

```bash
set -euo pipefail

SITE=development.localhost
BENCH_ROOT="$(pwd -P)"
APP_ROOT="$BENCH_ROOT/apps/frappe_lt"
CANDIDATE_SHA="$(git -C "$APP_ROOT" rev-parse HEAD)"
EVIDENCE="$BENCH_ROOT/sites/$SITE/private/frappe_lt_release_candidate/$CANDIDATE_SHA"
PACKAGE=/private/path/lt-v16-translations.csv

test "$(git -C "$APP_ROOT" status --porcelain)" = ""
test "${#CANDIDATE_SHA}" -eq 40
install -d -m 700 "$EVIDENCE"

bench --site "$SITE" capture-lithuanian-release-candidate \
  --output-dir "$EVIDENCE"

bench --site "$SITE" preflight-lithuanian-install \
  --package "$PACKAGE" > "$EVIDENCE/preflight.json"
```

Download the successful GitHub CI artifact for this exact commit and attempt, verify its bounded Actions API provenance, and place its sole report at the fixed input name. Do not rename evidence from another commit; finalization re-queries the bounded GitHub Actions run API and requires repository `lokysai/frappe-lt`, workflow `.github/workflows/ci.yml`, the exact candidate SHA and attempt, and `conclusion: success` in addition to all three green categories. GitHub unavailability blocks publication:

```bash
RUN_ID=123456789
RUN_ATTEMPT=1
gh api "repos/lokysai/frappe-lt/actions/runs/$RUN_ID/attempts/$RUN_ATTEMPT" \
  --jq 'select(.repository.full_name == "lokysai/frappe-lt" and .path == ".github/workflows/ci.yml" and .head_sha == "'"$CANDIDATE_SHA"'" and .run_attempt == '"$RUN_ATTEMPT"' and .conclusion == "success") | .id' \
  | grep -Fx "$RUN_ID"
gh run download "$RUN_ID" --repo lokysai/frappe-lt \
  --name "candidate-tests-$CANDIDATE_SHA-$RUN_ATTEMPT" --dir /tmp/frappe-lt-candidate-tests
install -m 600 /tmp/frappe-lt-candidate-tests/frappe-lt-tests.json "$EVIDENCE/tests.json"
```

If a site exception policy is required, add the same unchanged `--site-exceptions /private/path/site-exceptions.json` option to both `preflight-lithuanian-install` and `prepare-lithuanian-install`, and later to runtime validation and finalization.

Stop here. The operator must now create and independently verify the normal target-site backup under the site's established backup procedure. Record that operational approval outside the public evidence directory. The runtime suite never creates a backup, and there is no separate "runtime backup" step.

Only after the external backup is confirmed may mutation begin:

```bash
bench --site "$SITE" prepare-lithuanian-install \
  --package "$PACKAGE" > "$EVIDENCE/prepare.json"

bench --site "$SITE" install-app frappe_lt
bench --site "$SITE" resume-lithuanian-install > "$EVIDENCE/install.json"
bench --site "$SITE" show-lithuanian-install-status > "$EVIDENCE/install-status.json"
RUN_ID="$(python -c 'import json,sys; print(json.load(sys.stdin)["run_id"])' < "$EVIDENCE/install-status.json")"
install -m 600 \
  "sites/$SITE/private/frappe_lt_legacy_migration/$RUN_ID.final.json" \
  "$EVIDENCE/migration.json"

bench --site "$SITE" execute frappe_lt.verify.run \
  --kwargs '{"site":"development.localhost","mode":"local"}' \
  > "$EVIDENCE/verify.json"
```

`prepare-lithuanian-install` begins the maintenance boundary. Keep jobs, writers, and external user ingress quiesced through final publication. `install.json` must say `verified`, while `install-status.json` must prove committed or no-op migration, matching MO, applied profile, and maintenance mode. Do not expose the private migration plan, migration rows, legacy CSV, or site-exception contents.

After the verified state, disable Frappe maintenance only so the reviewed browser users can reach the local site; do not return external traffic yet:

```bash
restore_maintenance() {
  bench --site "$SITE" set-config maintenance_mode 1 --parse
}
trap restore_maintenance EXIT
bench --site "$SITE" set-config maintenance_mode 0 --parse

bench --site "$SITE" export-lithuanian-runtime-candidates \
  --output "$EVIDENCE/runtime-candidates.json"

bench --site "$SITE" validate-lithuanian-runtime \
  --output-dir "$EVIDENCE/runtime"

bench --site "$SITE" execute frappe_lt.runtime_control.assert_no_runtime_residue \
  > "$EVIDENCE/runtime-residue.json"

bench --site "$SITE" set-config maintenance_mode 1 --parse
trap - EXIT
```

If a private site exception policy was used, pass the same unchanged path as `--site-exceptions` to `validate-lithuanian-runtime` and finalization. The policy path and contents are never copied into public evidence.

Create `performance.json` using the existing paired measurement path from `bench/sites`. Prepare `baseline_site` from the same pinned Bench and the same clean ERPNext site state as the target before `frappe_lt` installation, including the same non-catalog fixtures and configuration. Prove it remains exactly `frappe,erpnext`; the enabled site must be exactly `frappe,erpnext,frappe_lt`. This is the matched ERPNext-only baseline, not an arbitrary existing site. The schema binds the compiled MO byte count to the count emitted by the safe verifier as well as its release digest:

```bash
(
  cd sites
  ../env/bin/python -m frappe_lt.measurements \
    baseline_site development.localhost \
    "$EVIDENCE/performance.json"
)
```

An older measurement is reusable only when its schema proves the same clean `frappe_lt` commit, current release/inventory/PO/MO artifacts, exact benchmark protocol, and unchanged raw-sample digest. Otherwise this command must be rerun. Absolute paths are not written to performance evidence.

Finalize only after every preceding command is green:

```bash
bench --site "$SITE" finalize-lithuanian-release-candidate \
  --evidence-dir "$EVIDENCE"
```

When a site policy was used:

```bash
bench --site "$SITE" finalize-lithuanian-release-candidate \
  --evidence-dir "$EVIDENCE" \
  --site-exceptions /private/path/site-exceptions.json
```

Require `status: pass` and verify that `$EVIDENCE/release-candidate.json` exists. Only then may the operator return jobs, writers, and external user traffic. If any step fails, keep traffic quiesced, retain the evidence directory for diagnosis, use the existing #15 status/resume recovery, and never fabricate or manually edit a green artifact.

## Publication policy

The final machine index is bounded canonical JSON. It contains only allowlisted version, Translation Coverage, Translation Exception, corrected inherited, route, desktop/mobile, layout, performance, and cleanup summaries. Every consumed artifact is referenced by a run-relative path, exact byte count, and SHA-256.

Finalization rejects missing or changed inputs, unknown schema fields, non-green states, mismatched commits/releases/contracts, absolute or traversing paths, symlinks, stale runtime or measurement evidence, nonempty residue, and partial publication. It does not copy credentials, cookies, secrets, absolute Bench paths, private migration rows, or site-exception contents into the public index.

Issue #16 remains open until this sequence is run on a real `development.localhost` Bench. Repository tests and CI artifacts are prerequisites, not fabricated target-site release-candidate evidence.
