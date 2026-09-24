# #15 release-size and performance measurement protocol

Status: **artifact bytes measured locally; pinned Bench latency evidence pending a CI run**. The `verify` GitHub Actions job now creates a pinned ERPNext-only baseline site beside its verified `frappe_lt` site and uploads `/tmp/frappe-lt-release-measurements.json` as a `release-measurements-*` artifact. The job fails when the paired warm p95 reaches 50 ms. Inspect the raw artifact, host load, pin and size data before recording a release decision; do not interpret a repository file size, the smoke MO digest, or a unit-test duration as release performance evidence.

## Artifact size, in bytes

Measure the actual deployed MO after a clean compile of the *pinned full PO*, not an isolated candidate or stale smoke MO. Define the inventory of shipped metadata **before** measuring and publish the per-file byte counts and SHA-256 values, so the sum is auditable. At minimum count all shipped release-identifying and catalog-support artifacts: `compatibility.json`, `release_catalog.json`, `release_inventory.json`, `provenance.json`, `catalog_partition.json`, `catalog_segment_ownership_overrides.json`, `catalog_segments.json`, each `catalog_segments/*.json`, `collision_resolutions.json`, `glossary_selectors.json`, `translation_exceptions.json`, and any shipped candidate JSON (which contains review metadata as well as translation content). Include `inventory_report.json` and `inventory_report.md` if distributed with the release; do not drop large manifests just to fit a limit. Record the PO separately as source-catalog payload; state explicitly whether it is part of the shipped metadata budget, and report a second total including it if so. Avoid counting the same file twice through globs.

From the bench root after successful full-PO compilation, capture the following output alongside the commit and release manifest digest. This example counts all listed artifacts shipped in this repo, including the report and the complete candidate JSON; it reports the PO separately:

```bash
env/bin/python - <<'PY'
import hashlib
from pathlib import Path

root = Path('apps/frappe_lt/frappe_lt')
mo = Path('sites/assets/locale/lt/LC_MESSAGES/frappe_lt.mo')
names = (
    'compatibility.json', 'release_catalog.json', 'release_inventory.json',
    'provenance.json', 'catalog_partition.json',
    'catalog_segment_ownership_overrides.json', 'catalog_segments.json',
    'collision_resolutions.json', 'glossary_selectors.json',
    'translation_exceptions.json', 'inventory_report.json', 'inventory_report.md',
)
metadata = [root / name for name in names]
metadata += sorted((root / 'catalog_segments').glob('*.json'))
metadata += sorted((root / 'catalog_candidates').glob('*.json'))
files = [mo, *metadata]
if len(files) != len(set(files)) or not all(p.is_file() for p in files):
    raise SystemExit('missing or repeated shipped artifact; resolve scope first')
for path in [*files, root / 'locale/lt.po']:
    data = path.read_bytes()
    print(len(data), hashlib.sha256(data).hexdigest(), path)
total = sum(path.stat().st_size for path in files)
print('MO + listed metadata (bytes):', total)
print('Limit (5 MiB in bytes):', 5 * 1024 * 1024)
PY
```

The #15 budget is **MO plus named metadata <= 5 MiB (5 × 1,048,576 bytes)**. If the measured total is above the limit, preserve the raw inventory and record a reviewed decision with evidence; do not silently redefine metadata, delete required provenance, or claim a pass. Capture a second inventory after any proposed optimization and rerun the full catalog gate/digest check.

Local artifact inventory on 2026-09-23, using the pinned Babel 2.16.0 serializer (the pinned Frappe Bench compile still needs checking): generated MO **1,525,232 bytes**, SHA-256 `e83b10d7662fab60db1b34fff34f714899a17985abbe2d411d0bbed8dcde5085`, matching `release_catalog.json`; the named metadata files above total **31,736,907 bytes**. MO plus metadata is **33,262,139 bytes (31.72 MiB)**, exceeding 5 MiB by **28,019,259 bytes**. The separate shipped PO is **1,565,863 bytes**; including it raises the total to **34,828,002 bytes**. The largest contributors are the 16,064,185-byte Release Inventory and three candidate files totalling 8,102,685 bytes. A decision to change packaging or approve a measured exception remains pending pinned Bench size and latency evidence; this is not a size-gate pass.

## Cold and warm load overhead

Run on one pinned bench and site, using the same Frappe/ERPNext commits, Python, Babel, hardware/container limits, matched DB snapshot, language, users, roles, network path, browser build (if applicable), and representative page/translation workload in both conditions. Record the app order for each variant: baseline = Frappe + ERPNext without `frappe_lt`; enabled = the fully installed and verified release, after #13 migration and #8 profile. Explain how equivalent non-catalog changes are controlled (for example a matched site snapshot and identical login/page fixtures). Disable background jobs that would contaminate samples and leave them disabled in both conditions. Record wall-clock UTC start/end, host load and exact commands or automation revision.

1. Define a stable Lithuanian load scenario before collecting timings, e.g. authenticated first navigation to the standard `/app/item` page; time the **same** start/end event on both conditions (request start to completed page load, or one documented server translation-load call). Do not mix browser wall time with a server-only baseline. Keep the site's translation caches, browser session and any HTTP caches under explicit control. Record individual raw durations in milliseconds and whether each sample was cold or warm.
2. Obtain **at least 20 independent cold samples for each variant**: for each sample clear server Translation/boot cache, start a clean browser/session or request client when relevant, and reset the scenario to the documented cold state. Obtain **at least 20 independent warm samples for each variant**: prime the caches with an unmeasured load, then measure a fresh load under the same warmed state. Do not present 20 repeated calls within a single unresolved cache state as 20 independent cold samples. Randomize or alternate baseline/enabled order in matched pairs to limit drift; record pair ID, variant, warm/cold classification, reset and priming method, timestamp, and measured duration.
3. For each of the >=20 matched **warm-load** pairs compute `enabled_ms - baseline_ms` (retain negative values). Sort these paired differences ascending and report the nearest-rank p95: at exactly 20 pairs use the **19th** sorted difference, or in general the value at rank `ceil(0.95 * n)` (1-indexed). The #15 criterion is **p95 < 50 ms**; 50.0 ms is not below the bound. Also report both variants' raw cold/warm distributions and p95, the pair IDs and the benchmark method. Do not subtract two independently computed p95s and call that the paired p95.

Keep the raw baseline and enabled measurements, scenario fixture/automation, cache reset and priming logs, environment/commit/pin details, per-artifact sizes and digests, and the calculation together as release evidence. Failed or missing samples should be documented and rerun, not silently omitted. This file defines the method only; actual size and latency results must come from a pinned bench run.

The CI scenario is the Frappe v16 server translation load `get_all_translations("lt")`. Each of 20 pairs alternates the order of the two sites. A cold observation clears Translation and site caches immediately before timing; a warm observation clears, then primes once outside the timed interval. The timed call returns the complete merged dictionary; each sample records UTC, duration, site variant, temperature and pair ID. The baseline has the same pinned Frappe/ERPNext versions and the same additional `Inventory Custom Probe` Translation as the enabled site, with no `frappe_lt` app. The enabled site has passed install/resume and full release verification. These are **server-call** measurements, not browser page-load measurements; the measured interval excludes cache resets, priming, connection setup and serialization of the evidence. The job reports nearest-rank p95 of the 20 paired warm differences (including negative differences) and raw values for both cold/warm variants. Run locally from `bench/sites` with `../env/bin/python -m frappe_lt.measurements baseline_site test_site /tmp/frappe-lt-release-measurements.json` after preparing both sites. The over-budget metadata remains `size_review_required` in the artifact. The chosen direction is to optimize the shipped metadata after reviewing pinned-bench evidence; any repackaging must preserve the authenticated Release Inventory, candidate bindings, Catalog Quality Gate, and preflight checks, then be remeasured and repinned before approval.
