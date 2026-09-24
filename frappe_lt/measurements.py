"""Pinned-bench, paired server translation-load measurements for release evidence."""

import hashlib
import json
import math
import os
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

METADATA = (
	"compatibility.json",
	"release_catalog.json",
	"release_inventory.json",
	"provenance.json",
	"catalog_partition.json",
	"catalog_segment_ownership_overrides.json",
	"catalog_segments.json",
	"collision_resolutions.json",
	"glossary_selectors.json",
	"translation_exceptions.json",
	"inventory_report.json",
	"inventory_report.md",
)


def nearest_rank(values, percentile=0.95):
	if not values or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
		raise ValueError("nonempty finite measurement values required")
	return sorted(values)[math.ceil(percentile * len(values)) - 1]


def artifact_sizes(root, mo):
	paths = [mo, *(root / name for name in METADATA)]
	paths += sorted((root / "catalog_segments").glob("*.json"))
	paths += sorted((root / "catalog_candidates").glob("*.json"))
	if len(paths) != len(set(paths)) or not all(path.is_file() for path in paths):
		raise ValueError("missing or repeated release artifact")
	results = [
		{
			"path": str(path),
			"bytes": path.stat().st_size,
			"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
		}
		for path in [*paths, root / "locale" / "lt.po"]
	]
	return {
		"files": results,
		"mo_and_metadata_bytes": sum(item["bytes"] for item in results[:-1]),
		"limit_bytes": 5 * 1024 * 1024,
	}


def measure(baseline, enabled, *, pairs=20):
	"""Alternate sites; reset caches for cold, prime outside the timed warm load."""
	import frappe
	from frappe.translate import clear_cache, get_all_translations

	if pairs < 20 or baseline == enabled:
		raise ValueError("two distinct sites and at least 20 pairs required")
	result = []
	for index in range(pairs):
		for variant, site in (
			(("baseline", baseline), ("enabled", enabled))
			if index % 2 == 0
			else (("enabled", enabled), ("baseline", baseline))
		):
			frappe.init(site=site)
			frappe.connect()
			try:
				apps = frappe.get_installed_apps()
				expected = ["frappe", "erpnext"] + (["frappe_lt"] if variant == "enabled" else [])
				if apps != expected:
					raise ValueError("benchmark site app order differs from the pinned variant")
				for temperature in ("cold", "warm"):
					clear_cache()
					frappe.clear_cache()
					if temperature == "warm":
						get_all_translations("lt")
						if not frappe.cache.hget("merged_translations", "lt"):
							raise ValueError("warm translation cache was not primed")
					start = time.perf_counter_ns()
					loaded = get_all_translations("lt")
					duration = (time.perf_counter_ns() - start) / 1_000_000
					if not loaded or (variant == "enabled" and loaded.get("Item") != "Prekė"):
						raise ValueError("translation load did not return the expected catalog")
					result.append(
						{
							"pair": index + 1,
							"variant": variant,
							"temperature": temperature,
							"at_utc": datetime.now(UTC).isoformat(),
							"duration_ms": duration,
						}
					)
			finally:
				frappe.db.rollback()
				frappe.destroy()
	return result


def report(samples, *, root, mo, baseline, enabled):
	from frappe_lt.release_catalog import verify_mo, verify_release

	release = verify_release()
	if verify_mo(mo) != release["mo_sha256"]:
		raise ValueError("compiled MO differs from the release")
	groups = {
		(variant, temperature): [
			sample["duration_ms"]
			for sample in samples
			if sample["variant"] == variant and sample["temperature"] == temperature
		]
		for variant in ("baseline", "enabled")
		for temperature in ("cold", "warm")
	}
	if len({len(values) for values in groups.values()}) != 1 or len(groups["baseline", "warm"]) < 20:
		raise ValueError("missing independent samples")
	pair_ids = set(range(1, len(groups["baseline", "warm"]) + 1))
	if any(
		{
			sample["pair"]
			for sample in samples
			if sample["variant"] == variant and sample["temperature"] == temperature
		}
		!= pair_ids
		for variant, temperature in groups
	):
		raise ValueError("missing or repeated pair IDs")
	warm = {
		variant: {
			sample["pair"]: sample["duration_ms"]
			for sample in samples
			if sample["variant"] == variant and sample["temperature"] == "warm"
		}
		for variant in ("baseline", "enabled")
	}
	if set(warm["baseline"]) != set(warm["enabled"]) or len(warm["baseline"]) != len(
		groups["baseline", "warm"]
	):
		raise ValueError("unpaired warm samples")
	differences = [
		{"pair": pair, "overhead_ms": warm["enabled"][pair] - warm["baseline"][pair]}
		for pair in sorted(warm["baseline"])
	]
	import babel

	artifacts = artifact_sizes(root, mo)
	compatibility = json.loads((root / "compatibility.json").read_bytes())
	return {
		"schema_version": 1,
		"scenario": "frappe.translate.get_all_translations('lt'); cold: clear translation and site cache; warm: clear then prime once outside timing; alternating sites and order per pair",
		"sites": {"baseline": baseline, "enabled": enabled},
		"python": platform.python_version(),
		"babel": babel.__version__,
		"host": {"platform": platform.platform(), "load_average_at_report": os.getloadavg()},
		"started_utc": samples[0]["at_utc"],
		"finished_utc": datetime.now(UTC).isoformat(),
		"release": release,
		"upstream_pins": compatibility["upstream"],
		"samples": samples,
		"p95_ms": {
			f"{variant}_{temperature}": nearest_rank(values)
			for (variant, temperature), values in groups.items()
		},
		"warm_pairs": differences,
		"paired_warm_p95_overhead_ms": nearest_rank([entry["overhead_ms"] for entry in differences]),
		"artifacts": artifacts,
		"size_review_required": artifacts["mo_and_metadata_bytes"] > artifacts["limit_bytes"],
	}


def main():
	if len(sys.argv) != 4:
		raise SystemExit(
			"usage: python -m frappe_lt.measurements BASELINE_SITE ENABLED_SITE OUTPUT.json (from bench/sites)"
		)
	baseline, enabled, output = sys.argv[1:]
	root = Path(__file__).parent
	mo = Path("assets/locale/lt/LC_MESSAGES/frappe_lt.mo")
	samples = measure(baseline, enabled)
	data = report(samples, root=root, mo=mo, baseline=baseline, enabled=enabled)
	Path(output).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	if data["paired_warm_p95_overhead_ms"] >= 50:
		raise SystemExit("paired warm-load p95 overhead is not below 50 ms; see raw evidence")


if __name__ == "__main__":
	main()
