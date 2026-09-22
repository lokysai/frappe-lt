import hashlib
import json
import re
from pathlib import PurePosixPath

from frappe_lt.inventory import canonical_json

CLASSIFIER_SCHEMA_VERSION = 1
SEGMENT_IDS = ("erpnext-finance-commerce", "erpnext-operations", "frappe")
FINANCE_MODULES = frozenset({"accounts", "assets", "buying", "crm", "selling"})
OVERRIDE_FIELDS = {"key", "reason", "review", "reviewed", "segment_id", "source_digest"}
SHA256 = re.compile(r"[0-9a-f]{64}")
SOURCE_SUFFIXES = {
	"erpnext.gettext.extractors.incoterms": {".csv"},
	"erpnext.gettext.extractors.lines_from_txt_file": {".txt"},
	"erpnext.gettext.extractors.uom_data": {".json"},
	"extract_babel_python": {".py"},
	"frappe.gettext.extractors.desktop_icon": {".json"},
	"frappe.gettext.extractors.doctype": {".json"},
	"frappe.gettext.extractors.html_template": {".html", ".js", ".ts", ".tsx", ".vue"},
	"frappe.gettext.extractors.javascript": {".js"},
	"frappe.gettext.extractors.module_onboarding": {".json"},
	"frappe.gettext.extractors.navbar": {".py"},
	"frappe.gettext.extractors.onboarding_step": {".json"},
	"frappe.gettext.extractors.report": {".json"},
	"frappe.gettext.extractors.web_form": {".json"},
	"frappe.gettext.extractors.workspace": {".json"},
	"frappe.gettext.extractors.workspace_sidebar": {".json"},
}
RUNTIME_EXTRACTORS = frozenset(
	{
		"custom_field",
		"doctype",
		"navbar",
		"page",
		"property_setter",
		"report",
		"workflow",
		"workspace",
		"workspace_sidebar",
	}
)
CLASSIFIER = {
	"finance_modules": sorted(FINANCE_MODULES),
	"runtime_extractors": sorted(RUNTIME_EXTRACTORS),
	"schema_version": CLASSIFIER_SCHEMA_VERSION,
	"source_extractors": {
		extractor: sorted(suffixes) for extractor, suffixes in sorted(SOURCE_SUFFIXES.items())
	},
	"source_location_forms": [
		{"app": "frappe", "module": "frappe", "origin": "source", "path_roots": ["cypress", "frappe"]},
		{"app": "frappe", "module": "frappe", "origin": "runtime", "path_root": "metadata"},
		{
			"app": "erpnext",
			"minimum_path_depth": 3,
			"module_path_component": 1,
			"origin": "source",
			"path_root": "erpnext",
		},
		{"app": "erpnext", "module": None, "origin": "source", "path_depth": 2, "path_root": "erpnext"},
		{"app": "erpnext", "module": "banking", "origin": "source", "path_root": "banking"},
		{"app": "erpnext", "module": None, "origin": "runtime", "path_root": "metadata"},
	],
}
OVERRIDES_ARTIFACT_FIELDS = {
	"classifier_schema_version",
	"entries",
	"inventory_digest",
	"schema_version",
}
PARTITION_FIELDS = {
	"classifier",
	"inventory_digest",
	"ownership_override_sha256",
	"schema_version",
	"segments",
}
PARTITION_SEGMENT_FIELDS = {"id", "manifest", "manifest_sha256"}
MANIFEST_FIELDS = {
	"classifier_schema_version",
	"inventory_digest",
	"keys",
	"schema_version",
	"segment_id",
}


def _key_tuple(entry):
	if not isinstance(entry, dict):
		raise ValueError("Translation Key owner must be an object")
	key = entry.get("key")
	if (
		not isinstance(key, dict)
		or set(key) != {"context", "source"}
		or not isinstance(key["source"], str)
		or not key["source"]
		or (key["context"] is not None and (not isinstance(key["context"], str) or not key["context"]))
	):
		raise ValueError("Translation Key is invalid")
	return key["source"], key["context"]


def _validate_location(location):
	if not isinstance(location, dict) or set(location) != {"app", "extractor", "line", "origin", "path"}:
		raise ValueError("Source Location fields do not match a documented Source Location form")
	app = location["app"]
	origin = location["origin"]
	path = location["path"]
	if app not in {"frappe", "erpnext"} or origin not in {"runtime", "source"}:
		raise ValueError("Source Location does not match a documented Source Location form")
	if (
		not isinstance(path, str)
		or not path
		or "\\" in path
		or PurePosixPath(path).is_absolute()
		or str(PurePosixPath(path)) != path
		or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
	):
		raise ValueError("Source Location path does not match a documented Source Location form")
	parts = path.split("/")
	line = location["line"]
	if origin == "runtime":
		extractor = location["extractor"]
		if (
			extractor not in RUNTIME_EXTRACTORS
			or line is not None
			or len(parts) < 2
			or parts[:2] != ["metadata", extractor]
		):
			raise ValueError("runtime Source Location does not match a documented Source Location form")
		return
	extractor = location["extractor"]
	if (
		extractor not in SOURCE_SUFFIXES
		or PurePosixPath(path).suffix not in SOURCE_SUFFIXES[extractor]
		or (line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1))
	):
		raise ValueError("source Source Location does not match a documented Source Location form")
	valid_roots = {"cypress", "frappe"} if app == "frappe" else {"banking", "erpnext"}
	if parts[0] not in valid_roots or (parts[0] == "erpnext" and len(parts) < 2):
		raise ValueError("source path does not match a documented Source Location form")


def _validate_inventory(inventory, inventory_digest):
	if (
		not isinstance(inventory, dict)
		or inventory.get("schema_version") != 1
		or not isinstance(inventory.get("entries"), list)
		or not isinstance(inventory_digest, str)
		or not SHA256.fullmatch(inventory_digest)
	):
		raise ValueError("Release Inventory partition input is invalid")
	seen = set()
	for entry in inventory["entries"]:
		try:
			key = _key_tuple(entry)
		except (KeyError, TypeError) as error:
			raise ValueError("Release Inventory partition key is invalid") from error
		if key in seen:
			raise ValueError(f"duplicate Release Inventory partition key {key!r}")
		seen.add(key)
		if entry.get("apps") not in (["erpnext"], ["frappe"], ["erpnext", "frappe"]):
			raise ValueError(f"Release Inventory apps are invalid for {key!r}")
		if not isinstance(entry.get("source_locations"), list) or not SHA256.fullmatch(
			entry.get("source_digest", "")
		):
			raise ValueError(f"Release Inventory partition entry is invalid for {key!r}")
		for location in entry["source_locations"]:
			_validate_location(location)
		location_apps = {location["app"] for location in entry["source_locations"]}
		if not location_apps or location_apps != set(entry["apps"]):
			raise ValueError(
				f"Release Inventory apps must exactly match nonempty Source Locations for {key!r}"
			)


def _erpnext_modules(entry):
	modules = set()
	for location in entry["source_locations"]:
		if location.get("app") != "erpnext" or location.get("origin") != "source":
			continue
		parts = location.get("path", "").split("/")
		if len(parts) >= 3 and parts[0] == "erpnext" and parts[1]:
			modules.add(parts[1])
		elif len(parts) >= 2 and parts[0] == "banking":
			modules.add("banking")
	return modules


def _automatic_segment(entry):
	if "frappe" in entry["apps"]:
		return "frappe"
	modules = _erpnext_modules(entry)
	if modules & FINANCE_MODULES:
		return "erpnext-finance-commerce"
	if modules:
		return "erpnext-operations"
	return None


def _validate_overrides(inventory, overrides):
	entries = {_key_tuple(entry): entry for entry in inventory["entries"]}
	validated = {}
	for record in overrides:
		if not isinstance(record, dict) or set(record) != OVERRIDE_FIELDS:
			raise ValueError("ownership override fields are invalid")
		key = _key_tuple(record)
		if key in validated:
			raise ValueError(f"duplicate ownership override for {key!r}")
		entry = entries.get(key)
		if entry is None:
			raise ValueError(f"ownership override contains unknown key {key!r}")
		if _automatic_segment(entry) is not None:
			raise ValueError(f"unnecessary ownership override for {key!r}")
		if record["source_digest"] != entry["source_digest"] or not SHA256.fullmatch(record["source_digest"]):
			raise ValueError(f"stale ownership override for {key!r}")
		if record["segment_id"] not in {"erpnext-finance-commerce", "erpnext-operations"}:
			raise ValueError(f"unknown segment in ownership override for {key!r}")
		if record["reviewed"] is not True:
			raise ValueError(f"ownership override must be reviewed for {key!r}")
		for field in ("reason", "review"):
			value = record[field]
			if not isinstance(value, str) or not value.strip() or value != value.strip():
				raise ValueError(f"ownership override {field} is invalid for {key!r}")
		validated[key] = record
	return validated


def build_partition(inventory, inventory_digest, overrides):
	"""Build the deterministic three-way production ownership partition."""
	_validate_inventory(inventory, inventory_digest)
	overrides_by_key = _validate_overrides(inventory, overrides)
	segments = {segment_id: [] for segment_id in SEGMENT_IDS}
	for entry in inventory["entries"]:
		key = _key_tuple(entry)
		segment_id = _automatic_segment(entry)
		if segment_id is None:
			override = overrides_by_key.get(key)
			if override is None:
				raise ValueError(f"unclassifiable ERPNext-only key requires an ownership override: {key!r}")
			segment_id = override["segment_id"]
		segments[segment_id].append({"key": entry["key"], "source_digest": entry["source_digest"]})
	return {
		"classifier": CLASSIFIER,
		"inventory_digest": inventory_digest,
		"schema_version": 1,
		"segments": [
			{
				"id": segment_id,
				"keys": sorted(segments[segment_id], key=canonical_json),
			}
			for segment_id in SEGMENT_IDS
		],
	}


def build_partition_artifacts(inventory, inventory_digest, ownership_overrides):
	"""Return the canonical authenticated production partition artifact set."""
	if (
		not isinstance(ownership_overrides, dict)
		or set(ownership_overrides) != OVERRIDES_ARTIFACT_FIELDS
		or ownership_overrides["schema_version"] != 1
		or ownership_overrides["classifier_schema_version"] != CLASSIFIER_SCHEMA_VERSION
		or ownership_overrides["inventory_digest"] != inventory_digest
		or not isinstance(ownership_overrides["entries"], list)
	):
		raise ValueError("Catalog Segment Ownership Override artifact is invalid or stale")
	built = build_partition(inventory, inventory_digest, ownership_overrides["entries"])
	artifacts = {
		"catalog_segment_ownership_overrides.json": canonical_json(ownership_overrides),
	}
	partition_segments = []
	for segment in built["segments"]:
		path = f"catalog_segments/{segment['id']}.json"
		manifest = {
			"classifier_schema_version": CLASSIFIER_SCHEMA_VERSION,
			"inventory_digest": inventory_digest,
			"keys": segment["keys"],
			"schema_version": 1,
			"segment_id": segment["id"],
		}
		content = canonical_json(manifest)
		artifacts[path] = content
		partition_segments.append(
			{
				"id": segment["id"],
				"manifest": path,
				"manifest_sha256": hashlib.sha256(content).hexdigest(),
			}
		)
	override_digest = hashlib.sha256(artifacts["catalog_segment_ownership_overrides.json"]).hexdigest()
	partition = {
		"classifier": CLASSIFIER,
		"inventory_digest": inventory_digest,
		"ownership_override_sha256": override_digest,
		"schema_version": 1,
		"segments": partition_segments,
	}
	artifacts["catalog_partition.json"] = canonical_json(partition)
	return artifacts


def _json_object(pairs):
	value = {}
	for key, child in pairs:
		if key in value:
			raise ValueError(f"duplicate JSON object member {key!r}")
		value[key] = child
	return value


def _canonical_object(content, label):
	if not isinstance(content, bytes):
		raise ValueError(f"{label} must be bytes")
	value = json.loads(content, object_pairs_hook=_json_object)
	if not isinstance(value, dict) or canonical_json(value) != content:
		raise ValueError(f"{label} is not canonical repository JSON")
	return value


def validate_partition_artifacts(inventory, inventory_digest, artifacts):
	"""Authenticate and semantically validate the frozen production partition."""
	expected_paths = {
		"catalog_partition.json",
		"catalog_segment_ownership_overrides.json",
		*(f"catalog_segments/{segment_id}.json" for segment_id in SEGMENT_IDS),
	}
	if not isinstance(artifacts, dict) or set(artifacts) != expected_paths:
		raise ValueError("production partition artifact set is incomplete or has extras")
	overrides = _canonical_object(
		artifacts["catalog_segment_ownership_overrides.json"],
		"Catalog Segment Ownership Override",
	)
	partition = _canonical_object(artifacts["catalog_partition.json"], "Catalog Segment partition")
	if set(partition) != PARTITION_FIELDS or partition.get("schema_version") != 1:
		raise ValueError("Catalog Segment partition schema is invalid")
	if partition["inventory_digest"] != inventory_digest:
		raise ValueError("Catalog Segment partition has a stale inventory digest")
	if partition["classifier"] != CLASSIFIER:
		raise ValueError("Catalog Segment partition classifier is unsupported")
	override_digest = hashlib.sha256(artifacts["catalog_segment_ownership_overrides.json"]).hexdigest()
	if partition["ownership_override_sha256"] != override_digest:
		raise ValueError("Catalog Segment partition ownership override digest mismatch")
	expected = build_partition_artifacts(inventory, inventory_digest, overrides)
	segment_records = partition["segments"]
	if (
		not isinstance(segment_records, list)
		or [record.get("id") for record in segment_records if isinstance(record, dict)] != list(SEGMENT_IDS)
		or any(set(record) != PARTITION_SEGMENT_FIELDS for record in segment_records)
	):
		raise ValueError("Catalog Segment partition segment registry is invalid")
	inventory_entries = {_key_tuple(entry): entry for entry in inventory["entries"]}
	claimed = {}
	for record in segment_records:
		segment_id = record["id"]
		path = f"catalog_segments/{segment_id}.json"
		if record["manifest"] != path:
			raise ValueError("Catalog Segment partition manifest path is invalid")
		content = artifacts[path]
		if (
			not SHA256.fullmatch(record.get("manifest_sha256", ""))
			or hashlib.sha256(content).hexdigest() != record["manifest_sha256"]
		):
			raise ValueError("Catalog Segment manifest digest mismatch")
		manifest = _canonical_object(content, f"Catalog Segment manifest {segment_id}")
		if (
			set(manifest) != MANIFEST_FIELDS
			or manifest.get("schema_version") != 1
			or manifest.get("classifier_schema_version") != CLASSIFIER_SCHEMA_VERSION
			or manifest.get("segment_id") != segment_id
			or not isinstance(manifest.get("keys"), list)
		):
			raise ValueError(f"Catalog Segment manifest schema is invalid for {segment_id}")
		if manifest["inventory_digest"] != inventory_digest:
			raise ValueError(f"Catalog Segment manifest has a stale inventory digest for {segment_id}")
		for selected in manifest["keys"]:
			if not isinstance(selected, dict) or set(selected) != {"key", "source_digest"}:
				raise ValueError("Catalog Segment manifest selector fields are invalid")
			key = _key_tuple(selected)
			if key in claimed:
				raise ValueError(f"Catalog Segment manifests overlap at {key!r}")
			entry = inventory_entries.get(key)
			if entry is None:
				raise ValueError(f"Catalog Segment manifest has an extra key {key!r}")
			if selected["source_digest"] != entry["source_digest"]:
				raise ValueError(f"Catalog Segment source digest mismatch for {key!r}")
			claimed[key] = segment_id
	missing = inventory_entries.keys() - claimed.keys()
	if missing:
		raise ValueError(f"Catalog Segment manifests have a gap of {len(missing)} keys")
	for path in expected_paths:
		if artifacts[path] != expected[path]:
			raise ValueError(f"production partition ownership or canonical bytes differ for {path}")
	return {
		record["id"]: _canonical_object(artifacts[record["manifest"]], record["manifest"])
		for record in segment_records
	}
