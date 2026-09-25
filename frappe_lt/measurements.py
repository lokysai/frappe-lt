"""Pinned-bench, paired server translation-load measurements for release evidence."""

import hashlib
import json
import math
import os
import platform
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from frappe_lt.inventory import canonical_json, clean_candidate_identity
from frappe_lt.runtime_contracts import safe_relative_path

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
SHIP_LIMIT_BYTES = 35 * 1024 * 1024
WARM_P95_OVERHEAD_LIMIT_MS = 50
PROTOCOL = {
	"behavior": "frappe.translate.get_all_translations('lt')",
	"cold_reset": "clear translation and site cache immediately before timing",
	"minimum_pairs": 20,
	"order": "alternate baseline and enabled first by pair",
	"schema_version": 1,
	"warm_prime": "clear caches and load once outside timing",
}
SCENARIO = "frappe.translate.get_all_translations('lt'); cold: clear translation and site cache; warm: clear then prime once outside timing; alternating sites and order per pair"
SAFE_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
SAFE_HOST = re.compile(r"[A-Za-z0-9 ._+()/,:-]{1,512}\Z")
BASELINE_SITE = "baseline_site"
ENABLED_SITE = "development.localhost"


def nearest_rank(values, percentile=0.95):
	if not values or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
		raise ValueError("nonempty finite measurement values required")
	return sorted(values)[math.ceil(percentile * len(values)) - 1]


def artifact_sizes(root, mo):
	paths = [mo, *(root / name for name in METADATA)]
	paths += sorted((root / "catalog_segments").glob("*.json"))
	paths += sorted((root / "catalog_candidates").glob("*.json"))
	if (
		len(paths) != len(set(paths))
		or not all(path.is_file() for path in paths)
		or any(path.is_symlink() for path in paths)
	):
		raise ValueError("missing or repeated release artifact")
	po = root / "locale" / "lt.po"
	if not po.is_file() or po.is_symlink():
		raise ValueError("missing or repeated release artifact")
	results = [
		{
			"path": ("compiled/frappe_lt.mo" if path == mo else path.relative_to(root).as_posix()),
			"bytes": path.stat().st_size,
			"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
		}
		for path in [*paths, po]
	]
	return {
		"files": results,
		"mo_and_metadata_bytes": sum(item["bytes"] for item in results[:-1]),
		"shipped_bytes": sum(item["bytes"] for item in results),
		"limit_bytes": SHIP_LIMIT_BYTES,
	}


def measure(baseline, enabled, *, pairs=PROTOCOL["minimum_pairs"]):
	"""Alternate sites; reset caches for cold, prime outside the timed warm load."""
	import frappe
	from frappe.translate import clear_cache, get_all_translations

	if pairs < PROTOCOL["minimum_pairs"] or baseline == enabled:
		raise ValueError(f"two distinct sites and at least {PROTOCOL['minimum_pairs']} pairs required")
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


def _candidate(root):
	return clean_candidate_identity(root.parent)


def _utc(value, label):
	if not isinstance(value, str) or len(value) > 64:
		raise ValueError(f"{label} timestamp is invalid")
	try:
		parsed = datetime.fromisoformat(value)
	except ValueError as error:
		raise ValueError(f"{label} timestamp is invalid") from error
	if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
		raise ValueError(f"{label} timestamp must be UTC")
	return parsed


def _statistics(samples):
	if not isinstance(samples, list) or any(
		not isinstance(sample, dict)
		or set(sample) != {"at_utc", "duration_ms", "pair", "temperature", "variant"}
		or isinstance(sample["pair"], bool)
		or not isinstance(sample["pair"], int)
		or sample["pair"] < 1
		or sample["variant"] not in {"baseline", "enabled"}
		or sample["temperature"] not in {"cold", "warm"}
		or not isinstance(sample["at_utc"], str)
		or isinstance(sample["duration_ms"], bool)
		or not isinstance(sample["duration_ms"], (int, float))
		or not math.isfinite(sample["duration_ms"])
		or sample["duration_ms"] < 0
		for sample in samples
	):
		raise ValueError("measurement samples are malformed")
	for sample in samples:
		_utc(sample["at_utc"], "measurement")
	groups = {
		(variant, temperature): [
			sample["duration_ms"]
			for sample in samples
			if sample["variant"] == variant and sample["temperature"] == temperature
		]
		for variant in ("baseline", "enabled")
		for temperature in ("cold", "warm")
	}
	if (
		len({len(values) for values in groups.values()}) != 1
		or len(groups["baseline", "warm"]) < PROTOCOL["minimum_pairs"]
	):
		raise ValueError("missing independent samples")
	pairs = len(groups["baseline", "warm"])
	expected_order = [
		(pair, variant, temperature)
		for pair in range(1, pairs + 1)
		for variant in (("baseline", "enabled") if pair % 2 else ("enabled", "baseline"))
		for temperature in ("cold", "warm")
	]
	if [(item["pair"], item["variant"], item["temperature"]) for item in samples] != expected_order:
		raise ValueError("measurement samples do not follow the canonical paired order")
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
	return {
		"p95_ms": {
			f"{variant}_{temperature}": nearest_rank(values)
			for (variant, temperature), values in groups.items()
		},
		"warm_pairs": differences,
		"paired_warm_p95_overhead_ms": nearest_rank([entry["overhead_ms"] for entry in differences]),
	}


def report(samples, *, root, mo, baseline, enabled, candidate=None):
	from frappe_lt.release_catalog import verify_mo, verify_release

	release = verify_release()
	verified_mo_sha256, verified_mo_bytes = verify_mo(mo, with_size=True)
	if verified_mo_sha256 != release["mo_sha256"]:
		raise ValueError("compiled MO differs from the release")
	statistics = _statistics(samples)
	import babel

	artifacts = artifact_sizes(root, mo)
	if artifacts["files"][0] != {
		"bytes": verified_mo_bytes,
		"path": "compiled/frappe_lt.mo",
		"sha256": verified_mo_sha256,
	}:
		raise ValueError("compiled MO changed after verification")
	compatibility = json.loads((root / "compatibility.json").read_bytes())
	candidate = candidate or _candidate(root)
	if (
		set(candidate) != {"clean", "commit"}
		or candidate["clean"] is not True
		or not isinstance(candidate["commit"], str)
		or not re.fullmatch(r"[0-9a-f]{40}", candidate["commit"])
	):
		raise ValueError("measurement candidate must be one clean full commit")
	protocol = {
		**PROTOCOL,
		"sha256": hashlib.sha256(canonical_json(PROTOCOL)).hexdigest(),
	}
	return {
		"schema_version": 3,
		"scenario": SCENARIO,
		"candidate": candidate,
		"sites": {"baseline": baseline, "enabled": enabled},
		"python": platform.python_version(),
		"babel": babel.__version__,
		"host": {"platform": platform.platform(), "load_average_at_report": os.getloadavg()},
		"started_utc": samples[0]["at_utc"],
		"finished_utc": datetime.now(UTC).isoformat(),
		"release": release,
		"upstream_pins": compatibility["upstream"],
		"samples": samples,
		"samples_sha256": hashlib.sha256(canonical_json(samples)).hexdigest(),
		"protocol": protocol,
		**statistics,
		"artifacts": artifacts,
		"size_review_required": artifacts["shipped_bytes"] > artifacts["limit_bytes"],
	}


def validate_report(
	value,
	*,
	candidate,
	release,
	expected_mo_bytes,
	root=None,
	po_sha256=None,
	expected_sites=None,
):
	"""Validate green measurement evidence for exact-candidate reuse."""
	fields = {
		"artifacts",
		"babel",
		"candidate",
		"finished_utc",
		"host",
		"p95_ms",
		"paired_warm_p95_overhead_ms",
		"protocol",
		"python",
		"release",
		"samples",
		"samples_sha256",
		"scenario",
		"schema_version",
		"sites",
		"size_review_required",
		"started_utc",
		"upstream_pins",
		"warm_pairs",
	}
	if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != 3:
		raise ValueError("performance report fields or schema are invalid")
	if isinstance(expected_mo_bytes, bool) or not isinstance(expected_mo_bytes, int) or expected_mo_bytes < 1:
		raise ValueError("trusted compiled MO byte count is invalid")
	if (
		not isinstance(candidate, dict)
		or set(candidate) != {"clean", "commit"}
		or candidate.get("clean") is not True
		or not isinstance(candidate.get("commit"), str)
		or re.fullmatch(r"[0-9a-f]{40}", candidate["commit"]) is None
		or value["candidate"] != candidate
	):
		raise ValueError("performance candidate does not match the selected clean commit")
	if (
		not isinstance(release, dict)
		or set(release) != {"inventory_digest", "mo_sha256", "release_digest"}
		or any(
			not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
			for digest in release.values()
		)
		or value["release"] != release
	):
		raise ValueError("performance release does not match the selected release")
	upstream = value["upstream_pins"]
	if not isinstance(upstream, dict) or set(upstream) != {"erpnext", "frappe"}:
		raise ValueError("performance upstream pins are invalid")
	for pin in upstream.values():
		if (
			not isinstance(pin, dict)
			or set(pin) != {"commit", "version"}
			or not isinstance(pin["commit"], str)
			or re.fullmatch(r"[0-9a-f]{40}", pin["commit"]) is None
			or not isinstance(pin["version"], str)
			or SAFE_VERSION.fullmatch(pin["version"]) is None
		):
			raise ValueError("performance upstream pins are invalid")
	if value["scenario"] != SCENARIO:
		raise ValueError("performance scenario differs from the canonical protocol")
	sites = value["sites"]
	if (
		not isinstance(sites, dict)
		or set(sites) != {"baseline", "enabled"}
		or sites["baseline"] == sites["enabled"]
		or any(not isinstance(site, str) or SAFE_SITE.fullmatch(site) is None for site in sites.values())
	):
		raise ValueError("performance sites must be distinct safe site names")
	if expected_sites is not None and sites != expected_sites:
		raise ValueError("performance sites differ from the required matched pair")
	if any(
		not isinstance(value[field], str) or SAFE_VERSION.fullmatch(value[field]) is None
		for field in ("babel", "python")
	):
		raise ValueError("performance tool versions are invalid")
	host = value["host"]
	if (
		not isinstance(host, dict)
		or set(host) != {"load_average_at_report", "platform"}
		or not isinstance(host["platform"], str)
		or SAFE_HOST.fullmatch(host["platform"]) is None
		or not isinstance(host["load_average_at_report"], (list, tuple))
		or len(host["load_average_at_report"]) != 3
		or any(
			isinstance(number, bool)
			or not isinstance(number, (int, float))
			or not math.isfinite(number)
			or number < 0
			for number in host["load_average_at_report"]
		)
	):
		raise ValueError("performance host facts are invalid")
	started = _utc(value["started_utc"], "performance start")
	finished = _utc(value["finished_utc"], "performance finish")
	if finished < started:
		raise ValueError("performance timestamps are reversed")
	protocol = value["protocol"]
	if not isinstance(protocol, dict) or set(protocol) != set(PROTOCOL) | {"sha256"}:
		raise ValueError("performance protocol fields are invalid")
	if {key: protocol[key] for key in PROTOCOL} != PROTOCOL or protocol["sha256"] != hashlib.sha256(
		canonical_json(PROTOCOL)
	).hexdigest():
		raise ValueError("performance protocol digest is invalid")
	if (
		not isinstance(value["samples"], list)
		or value["samples_sha256"] != hashlib.sha256(canonical_json(value["samples"])).hexdigest()
	):
		raise ValueError("performance raw measurement digest is invalid")
	statistics = _statistics(value["samples"])
	if value["started_utc"] != value["samples"][0]["at_utc"]:
		raise ValueError("performance start does not match the first sample")
	if any(_utc(sample["at_utc"], "measurement") > finished for sample in value["samples"]):
		raise ValueError("performance finish precedes a sample")
	if any(value[field] != statistics[field] for field in statistics):
		raise ValueError("performance calculations do not match raw measurements")
	if (
		isinstance(value["paired_warm_p95_overhead_ms"], bool)
		or not isinstance(value["paired_warm_p95_overhead_ms"], (int, float))
		or not math.isfinite(value["paired_warm_p95_overhead_ms"])
		or value["paired_warm_p95_overhead_ms"] >= WARM_P95_OVERHEAD_LIMIT_MS
		or value["size_review_required"] is not False
	):
		raise ValueError("performance report is not green")
	artifacts = value["artifacts"]
	if not isinstance(artifacts, dict) or set(artifacts) != {
		"files",
		"limit_bytes",
		"mo_and_metadata_bytes",
		"shipped_bytes",
	}:
		raise ValueError("performance artifact inventory fields are invalid")
	if not isinstance(artifacts["files"], list) or not artifacts["files"]:
		raise ValueError("performance artifact inventory is empty")
	paths = []
	for item in artifacts["files"]:
		if not isinstance(item, dict) or set(item) != {"bytes", "path", "sha256"}:
			raise ValueError("performance artifact reference fields are invalid")
		safe_relative_path(item["path"])
		if (
			isinstance(item["bytes"], bool)
			or not isinstance(item["bytes"], int)
			or item["bytes"] < 1
			or not isinstance(item["sha256"], str)
			or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
		):
			raise ValueError("performance artifact reference is invalid")
		paths.append(item["path"])
	if paths != list(dict.fromkeys(paths)):
		raise ValueError("performance artifact paths are repeated")
	if paths[-1:] != ["locale/lt.po"]:
		raise ValueError("performance PO artifact must be reported separately and last")
	if (
		artifacts["mo_and_metadata_bytes"] != sum(item["bytes"] for item in artifacts["files"][:-1])
		or artifacts["shipped_bytes"] != sum(item["bytes"] for item in artifacts["files"])
		or artifacts["limit_bytes"] != SHIP_LIMIT_BYTES
		or value["size_review_required"] != (artifacts["shipped_bytes"] > artifacts["limit_bytes"])
	):
		raise ValueError("performance artifact size totals are inconsistent")
	by_path = {item["path"]: item for item in artifacts["files"]}
	if (
		by_path.get("compiled/frappe_lt.mo")
		!= {
			"bytes": expected_mo_bytes,
			"path": "compiled/frappe_lt.mo",
			"sha256": release["mo_sha256"],
		}
		or by_path.get("release_catalog.json", {}).get("sha256") != release["release_digest"]
		or by_path.get("release_inventory.json", {}).get("sha256") != release["inventory_digest"]
	):
		raise ValueError("performance release artifact provenance is incomplete")
	root = Path(root or Path(__file__).parent)
	expected_paths = [
		"compiled/frappe_lt.mo",
		*METADATA,
		*(path.relative_to(root).as_posix() for path in sorted((root / "catalog_segments").glob("*.json"))),
		*(path.relative_to(root).as_posix() for path in sorted((root / "catalog_candidates").glob("*.json"))),
		"locale/lt.po",
	]
	if paths != expected_paths:
		raise ValueError("performance artifact inventory is not the exact shipped inventory")
	for item in artifacts["files"]:
		if item["path"] == "compiled/frappe_lt.mo":
			continue
		path = root / item["path"]
		if path.is_symlink() or not path.is_file():
			raise ValueError(f"current shipped artifact is missing or unsafe: {item['path']}")
		content = path.read_bytes()
		if item["bytes"] != len(content) or item["sha256"] != hashlib.sha256(content).hexdigest():
			raise ValueError(f"current shipped artifact changed: {item['path']}")
	if po_sha256 is not None and by_path["locale/lt.po"]["sha256"] != po_sha256:
		raise ValueError("performance PO differs from the captured candidate PO")
	return value


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
	Path(output).write_bytes(canonical_json(data))
	if data["paired_warm_p95_overhead_ms"] >= WARM_P95_OVERHEAD_LIMIT_MS:
		raise SystemExit(
			f"paired warm-load p95 overhead is not below {WARM_P95_OVERHEAD_LIMIT_MS} ms; see raw evidence"
		)
	if data["size_review_required"]:
		raise SystemExit("reviewed shipped-size limit was exceeded; see raw evidence")


if __name__ == "__main__":
	main()
