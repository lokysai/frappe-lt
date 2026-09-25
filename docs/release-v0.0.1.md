# frappe-lt v0.0.1 beta

This is the first pinned `frappe-lt` 0.x beta. The GitHub Release is marked as a
prerelease while the package and tag use the matching semantic version `0.0.1`.

> **Operator erratum:** the immutable initial Release-note asset omitted the
> `set -euo pipefail` line from its abbreviated shell block. Use this maintained
> version of the procedure. The release source, candidate reports and catalog
> artifacts are unaffected.

## Compatibility and identity

- Release commit: `420d722ca38d20bd066fa817d1334976066beeaa`
- Frappe: `16.34.0` (`c1f1e8ec3708750d7254f7f99d869ffb9886f19f`)
- ERPNext: `16.35.0` (`12cd563fb9a79731f75ae2a45b1446a0a2dd9e74`)
- Release Inventory SHA-256: `111b42527f63668b884a30a0ce421c9e4da9308760baf8a143b1b4349e1107b7`
- Release manifest SHA-256: `e454fbfa8d52fc7362cfd871b5a6abae3b9a09d4b7c3d0f625b73d005e91259d`
- Shipped PO SHA-256: `404c3de26a3bd8e5e2acce5c9e11909eb5d3e7ec6887484d7b06d4d8cf0427a3`
- Compiled MO SHA-256: `e83b10d7662fab60db1b34fff34f714899a17985abbe2d411d0bbed8dcde5085`

The tag points directly to the candidate validated in issue #16. The source archive is
GitHub's archive of that commit. The attached `release-candidate.json` and
`release-candidate.md` are the original finalized reports; no catalog artifact was
regenerated after validation.

## Language review risk

The Lithuanian catalog was generated and audited by AI under the repository's
automated terminology, provenance, token, markup, coverage and runtime gates. It has
**not received a complete human linguistic review**. Operators must treat residual
wording, grammar and domain-terminology risk as a beta limitation even though all
16,535 Release Inventory keys passed the automated release gates.

## Pinned installation

Use an isolated single-site Bench for the abbreviated local sequence below. Use the
same exact pins on staging and production; validate a representative backup on staging
before production. A Bench with multiple sites must instead follow the complete
runbook and preflight, quiesce and prepare every site sharing the MO before the first
mutation. Do not rebuild the catalog or switch to a branch between environments.

```bash
set -euo pipefail

RELEASE_SHA=420d722ca38d20bd066fa817d1334976066beeaa
VERIFIED_APP="$(mktemp -d)"
git -C "$VERIFIED_APP" init
git -C "$VERIFIED_APP" remote add origin https://github.com/lokysai/frappe-lt
git -C "$VERIFIED_APP" fetch --depth 1 origin "$RELEASE_SHA"
git -C "$VERIFIED_APP" checkout --detach FETCH_HEAD
test "$(git -C "$VERIFIED_APP" rev-parse HEAD)" = "$RELEASE_SHA"
test "$(git ls-remote https://github.com/lokysai/frappe-lt \
  'refs/tags/v0.0.1^{}' | cut -f1)" = "$RELEASE_SHA"
test "$(git -C apps/frappe rev-parse HEAD)" = \
  "c1f1e8ec3708750d7254f7f99d869ffb9886f19f"
test "$(git -C apps/erpnext rev-parse HEAD)" = \
  "12cd563fb9a79731f75ae2a45b1446a0a2dd9e74"
test -z "$(git -C apps/frappe status --porcelain)"
test -z "$(git -C apps/erpnext status --porcelain)"
bench get-app frappe_lt "$VERIFIED_APP"
test "$(git -C apps/frappe_lt rev-parse HEAD)" = \
  "$RELEASE_SHA"
bench setup requirements
env/bin/python - <<'PY'
import erpnext
import frappe

assert frappe.__version__ == "16.34.0"
assert erpnext.__version__ == "16.35.0"
PY

PREFLIGHT="$(mktemp)"
bench --site SITE preflight-lithuanian-install \
  --package /private/path/lt-v16-translations.csv | tee "$PREFLIGHT"
env/bin/python - "$PREFLIGHT" <<'PY'
import json
import sys

result = json.load(open(sys.argv[1]))
assert result["state"] == "ready", result
assert result["warnings"] == [], result
PY
# Take and independently verify the normal site backup here.
bench --site SITE prepare-lithuanian-install \
  --package /private/path/lt-v16-translations.csv
bench --site SITE install-app frappe_lt
bench --site SITE resume-lithuanian-install
bench --site SITE execute frappe_lt.verify.run \
  --kwargs '{"site":"SITE","mode":"local"}'
bench --site SITE clear-cache
```

Require `state: verified`, `effective_translation: "Prekė"`, and the digests above
before returning traffic. The guarded install compiles the shipped PO in isolation and
publishes the MO only when every site sharing it is prepared for the same release.
`SOURCE_DATE_EPOCH=1704067200 bench compile-po-to-mo --app frappe_lt --locale lt --force`
is for an isolated release Bench only, never an in-place production fix.

For the complete guarded install sequence, shared-MO constraints, cache clearing and
failure recovery, see
[`release-installation.md`](https://github.com/lokysai/frappe-lt/releases/download/v0.0.1/release-installation.md),
published with this release as an operator asset (SHA-256
`8fbe2e09e3a047b4924c48ca11bdd4837db7ff166e3668ac52888f6b78cc18d3`).

## Updates, restore and corrections

`v0.0.1` has no supported in-place predecessor. A future update must use a new exact
tag and its documented coordinated shared-MO procedure after drift preflight and a
verified backup; an unpinned `bench update` is unsupported.

Before uninstalling, run `bench --site SITE restore-lithuanian-profile`; uninstall is
blocked until guarded restore succeeds. To intentionally retain profile values, use
`bench --site SITE leave-lithuanian-profile --confirm-leave-profile` before uninstall.
Neither route restores legacy database Translation rows; only the verified
pre-install backup can do that.

Corrections must pass review and ship through a new semantic version. Do not retain a
production-only `Translation` override as the permanent correction.

## Evidence

- [Issue #16 validation](https://github.com/lokysai/frappe-lt/issues/16#issuecomment-5827203890)
- [Candidate CI](https://github.com/lokysai/frappe-lt/actions/runs/36093711220)
- [Machine report](https://github.com/lokysai/frappe-lt/releases/download/v0.0.1/release-candidate.json), SHA-256 `84c5d775a3d421ebda078dfbbc243a90cd459d13101f7e148fcfb549dc37d4d1`
- [Human report](https://github.com/lokysai/frappe-lt/releases/download/v0.0.1/release-candidate.md), SHA-256 `1fda0bd9773175846b39cd87b3f20c600c47aea0f2c46ad810b5db23994e0653`

## Fresh pinned smoke

[Tag workflow run 36120343789](https://github.com/lokysai/frappe-lt/actions/runs/36120343789)
completed successfully from `v0.0.1` at the exact release commit. It created a clean
pinned site with Frappe `16.34.0` and ERPNext `16.35.0`, reconstructed and compared the
catalog, installed `frappe_lt`, exercised recovery and a second shared-MO site, and
passed the independent browser runtime job. The installed-site verifier returned
`state: "verified"`, `effective_translation: "Prekė"`, and the Inventory, release and
MO digests recorded above. All application worktrees were clean after the run.
