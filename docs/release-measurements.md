# #15 release-size and performance measurement protocol

Status: **pinned Bench latency and reviewed-size gate passed** in [run 35973885985](evidence/issue-15-35973885985.md), with all 80 durations and per-file digests retained. [The original 5 MiB miss](evidence/issue-15-35958794756.md) remains recorded. The `verify` GitHub Actions job creates a pinned ERPNext-only baseline site beside its verified `frappe_lt` site and uploads `/tmp/frappe-lt-release-measurements.json` as a `release-measurements-*` artifact. The job fails when the paired warm p95 reaches 50 ms or the reviewed shipped-size limit is exceeded. Inspect the raw artifact, host load, pin and size data before recording a release decision; do not interpret a repository file size, the smoke MO digest, or a unit-test duration as release performance evidence.

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
shipped = total + (root / 'locale/lt.po').stat().st_size
print('Full shipped total including PO (bytes):', shipped)
print('Reviewed full-shipment limit (35 MiB in bytes):', 35 * 1024 * 1024)
PY
```

The original #15 target was **MO plus named metadata <= 5 MiB (5 × 1,048,576 bytes)**. The pinned measurement showed it was unmeetable with the authenticated, independently shipped Release Inventory, candidates and review evidence intact. On 2026-09-24 the operator approved revising the acceptance budget to **MO + all named metadata + shipped PO <= 35 MiB (36,700,160 bytes)**. This counts *more* artifacts than the original target, including the PO. It is a measured release-size decision, not a claim that the 5 MiB goal was met: the 5 MiB miss remains in the evidence below. The CI measurement gate now fails if this full shipped total exceeds 35 MiB. A changed release still needs its own pinned-Bench measurement, full catalog gate and digest checks; the original per-file inventory is retained for comparison.

The pinned Bench on 2026-09-24 confirmed the MO is **1,525,232 bytes**, SHA-256 `e83b10d7662fab60db1b34fff34f714899a17985abbe2d411d0bbed8dcde5085`, matching `release_catalog.json`; the named metadata files above total **31,736,907 bytes**. MO plus metadata is **33,262,139 bytes (31.72 MiB)**, exceeding the original 5 MiB target by **28,019,259 bytes**. The separate shipped PO is **1,565,863 bytes**; including it raises the total to **34,828,002 bytes**, **1,872,158 bytes below the reviewed 35 MiB ceiling**. The largest contributors are the 16,064,185-byte Release Inventory and three candidate files totalling 8,102,685 bytes. A local estimate of independent gzip compression of all named metadata was 5,737,810 bytes *before* the 1,525,232-byte MO; independent xz compression of just the ten largest files was 4,412,440 bytes before MO and the other files. Meeting 5 MiB would therefore require a coordinated release-format/reader migration and repinning, not merely compressing the large individual JSON files. The original run did not enforce the revised ceiling; [run 35973885985](evidence/issue-15-35973885985.md) enforced it on the pinned Bench with the same release digests and measured paired warm p95 **2.126167 ms**.

## Cold and warm load overhead

Run on one pinned bench and site, using the same Frappe/ERPNext commits, Python, Babel, hardware/container limits, matched DB snapshot, language, users, roles, network path, browser build (if applicable), and representative page/translation workload in both conditions. Record the app order for each variant: baseline = Frappe + ERPNext without `frappe_lt`; enabled = the fully installed and verified release, after #13 migration and #8 profile. Explain how equivalent non-catalog changes are controlled (for example a matched site snapshot and identical login/page fixtures). Disable background jobs that would contaminate samples and leave them disabled in both conditions. Record wall-clock UTC start/end, host load and exact commands or automation revision.

1. Define a stable Lithuanian load scenario before collecting timings, e.g. authenticated first navigation to the standard `/app/item` page; time the **same** start/end event on both conditions (request start to completed page load, or one documented server translation-load call). Do not mix browser wall time with a server-only baseline. Keep the site's translation caches, browser session and any HTTP caches under explicit control. Record individual raw durations in milliseconds and whether each sample was cold or warm.
2. Obtain **at least 20 independent cold samples for each variant**: for each sample clear server Translation/boot cache, start a clean browser/session or request client when relevant, and reset the scenario to the documented cold state. Obtain **at least 20 independent warm samples for each variant**: prime the caches with an unmeasured load, then measure a fresh load under the same warmed state. Do not present 20 repeated calls within a single unresolved cache state as 20 independent cold samples. Randomize or alternate baseline/enabled order in matched pairs to limit drift; record pair ID, variant, warm/cold classification, reset and priming method, timestamp, and measured duration.
3. For each of the >=20 matched **warm-load** pairs compute `enabled_ms - baseline_ms` (retain negative values). Sort these paired differences ascending and report the nearest-rank p95: at exactly 20 pairs use the **19th** sorted difference, or in general the value at rank `ceil(0.95 * n)` (1-indexed). The #15 criterion is **p95 < 50 ms**; 50.0 ms is not below the bound. Also report both variants' raw cold/warm distributions and p95, the pair IDs and the benchmark method. Do not subtract two independently computed p95s and call that the paired p95.

Keep the raw baseline and enabled measurements, scenario fixture/automation, cache reset and priming logs, environment/commit/pin details, per-artifact sizes and digests, and the calculation together as release evidence. Measurement schema 3 requires the compiled MO entry to match both the authenticated release digest and the byte count returned by the safe active-MO verifier. Failed or missing samples should be documented and rerun, not silently omitted. This file defines the method only; actual size and latency results must come from a pinned bench run.

The CI scenario is the Frappe v16 server translation load `get_all_translations("lt")`. Each of 20 pairs alternates the order of the two sites. A cold observation clears Translation and site caches immediately before timing; a warm observation clears, then primes once outside the timed interval. The timed call returns the complete merged dictionary; each sample records UTC, duration, site variant, temperature and pair ID. CI creates `baseline_site` fresh on the same pinned Bench, installs only ERPNext, applies the same non-catalog `Inventory Custom Probe` fixture used by the enabled site, and proves its exact app order is `frappe,erpnext`. The enabled site is independently proved as `frappe,erpnext,frappe_lt` after install/resume and full release verification. Thus the comparison is against a prepared matched ERPNext-only site, not an unexplained arbitrary site. These are **server-call** measurements, not browser page-load measurements; the measured interval excludes cache resets, priming, connection setup and serialization of the evidence. The job reports nearest-rank p95 of the 20 paired warm differences (including negative differences) and raw values for both cold/warm variants. Run locally from `bench/sites` with `../env/bin/python -m frappe_lt.measurements baseline_site test_site /tmp/frappe-lt-release-measurements.json` only after reproducing that preparation and proving both exact app lists. The artifact reports MO plus named metadata and the full shipped total including PO, and `size_review_required` compares the latter with the reviewed ceiling.
