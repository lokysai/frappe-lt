import hashlib
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

from frappe_lt.inventory import canonical_json, verify_environment
from frappe_lt.runtime_contracts import load_contracts
from frappe_lt.runtime_extraction import EXPECTED_RUNTIME_APPS, collect_standard_metadata

CANDIDATE_SNAPSHOT_SCHEMA_VERSION = 1
MAX_CANDIDATE_SNAPSHOT_BYTES = 16 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class RuntimeCandidate:
	id: str
	type: str
	app: str
	identity: str


def _candidate(kind: str, identity: str, app: str) -> RuntimeCandidate:
	if app not in EXPECTED_RUNTIME_APPS:
		raise ValueError(f"runtime candidate belongs to out-of-scope app {app!r}")
	return RuntimeCandidate(
		id=f"{kind}:{quote(identity, safe='._-')}",
		type=kind,
		app=app,
		identity=identity,
	)


def _scope_app(frappe, module: str) -> str | None:
	if get_module_app := getattr(frappe, "get_module_app", None):
		app = get_module_app(module)
	else:
		app = frappe.local.module_app[frappe.scrub(module)]
	return app if app in EXPECTED_RUNTIME_APPS else None


def _metadata_candidates(frappe) -> list[RuntimeCandidate]:
	metadata = collect_standard_metadata(frappe)
	details = {
		row["name"]: row
		for row in frappe.get_all(
			"DocType",
			filters={"custom": 0},
			fields=["name", "issingle", "istable"],
			order_by="name asc",
		)
	}
	candidates = []
	for doctype in metadata["doctype"]["DocType"]:
		app = _scope_app(frappe, doctype["module"])
		if app is None:
			continue
		detail = details.get(doctype["name"])
		if detail is None:
			raise ValueError(f"DocType discovery details missing for {doctype['name']!r}")
		if detail.get("istable"):
			continue
		kind = "doctype:settings" if detail.get("issingle") else "doctype:list"
		candidates.append(_candidate(kind, doctype["name"], app))
		if not detail.get("issingle"):
			candidates.append(_candidate("doctype:form", doctype["name"], app))
	for page in metadata["page"]["Page"]:
		if app := _scope_app(frappe, page["module"]):
			candidates.append(_candidate("page", page["name"], app))
	for report in metadata["report"]["Report"]:
		if app := _scope_app(frappe, report["module"]):
			candidates.append(_candidate("report", report["name"], app))
	return candidates


def _portal_candidates(frappe) -> list[RuntimeCandidate]:
	candidates = []
	for app in EXPECTED_RUNTIME_APPS:
		root = Path(frappe.get_app_path(app))
		www = root / "www"
		if not www.is_dir():
			continue
		for path in sorted((*www.rglob("*.py"), *www.rglob("*.html"))):
			if any(part.startswith(("_", ".")) for part in path.relative_to(www).parts):
				continue
			relative = path.relative_to(www).with_suffix("")
			if relative.name == "index":
				relative = relative.parent
			route = relative.as_posix().strip("/")
			if route:
				candidates.append(_candidate("portal:route", route, app))
	if not candidates:
		raise ValueError("portal collector unexpectedly returned no standard routes")
	return candidates


def _print_candidates(frappe) -> list[RuntimeCandidate]:
	doctypes = {}
	for row in frappe.get_all(
		"DocType",
		filters={"custom": 0, "istable": 0, "issingle": 0},
		fields=["name", "module"],
		order_by="name asc",
	):
		if app := _scope_app(frappe, row["module"]):
			doctypes[row["name"]] = app
	formats = frappe.get_all(
		"Print Format",
		filters={"standard": "Yes", "disabled": 0},
		fields=["name", "doc_type"],
		order_by="doc_type asc, name asc",
	)
	candidates = [_candidate("output:print", doctype, app) for doctype, app in sorted(doctypes.items())]
	for record in formats:
		if app := doctypes.get(record["doc_type"]):
			candidates.append(
				_candidate("output:print-format", f"{record['doc_type']}/{record['name']}", app)
			)
	if not candidates:
		raise ValueError("print collector unexpectedly returned no standard outputs")
	return candidates


def _email_candidates(frappe) -> list[RuntimeCandidate]:
	candidates = []
	for app in EXPECTED_RUNTIME_APPS:
		root = Path(frappe.get_app_path(app)) / "templates" / "emails"
		if not root.is_dir():
			continue
		for path in sorted(root.rglob("*.html")):
			candidates.append(
				_candidate("output:email", path.relative_to(root).with_suffix("").as_posix(), app)
			)
	if not candidates:
		raise ValueError("email collector unexpectedly returned no standard templates")
	return candidates


def discover(frappe, collectors=None) -> dict:
	"""Discover standard candidates without executing any discovered route or output."""
	collectors = collectors or {
		"metadata": _metadata_candidates,
		"portal": _portal_candidates,
		"print": _print_candidates,
		"email": _email_candidates,
	}
	all_candidates = []
	counts = {}
	for name in sorted(collectors):
		try:
			candidates = collectors[name](frappe)
		except Exception as error:
			raise ValueError(f"runtime discovery collector {name!r} failed: {error}") from error
		if not isinstance(candidates, list) or not candidates:
			raise ValueError(f"runtime discovery collector {name!r} unexpectedly returned no candidates")
		if not all(isinstance(candidate, RuntimeCandidate) for candidate in candidates):
			raise ValueError(f"runtime discovery collector {name!r} returned malformed candidates")
		counts[name] = len(candidates)
		all_candidates.extend(candidates)
	by_id = {}
	for candidate in all_candidates:
		previous = by_id.setdefault(candidate.id, candidate)
		if previous != candidate:
			raise ValueError(f"conflicting runtime candidate identity {candidate.id!r}")
	return {
		"candidates": [asdict(by_id[candidate_id]) for candidate_id in sorted(by_id)],
		"collector_counts": counts,
	}


def coverage(discovery: dict, scenarios: dict, classifications: dict) -> dict:
	candidate_ids = {candidate["id"] for candidate in discovery["candidates"]}
	scenario_ids = {scenario["candidate_id"] for scenario in scenarios["scenarios"]}
	classified_ids = {classification["candidate_id"] for classification in classifications["classifications"]}
	unknown_scenarios = sorted(scenario_ids - candidate_ids)
	if unknown_scenarios:
		raise ValueError(
			f"Runtime Scenario Manifest references candidates not found by discovery: {unknown_scenarios}"
		)
	unknown_classifications = sorted(classified_ids - candidate_ids)
	if unknown_classifications:
		raise ValueError(
			f"candidate classifier references candidates not found by discovery: {unknown_classifications}"
		)
	return {
		"covered": sorted(candidate_ids & scenario_ids),
		"gaps": sorted(candidate_ids - scenario_ids - classified_ids),
		"reviewed_exclusions": sorted(candidate_ids & classified_ids),
	}


def _exact(value: object, fields: set[str], label: str) -> dict:
	if not isinstance(value, dict) or set(value) != fields:
		raise ValueError(f"{label} fields must be exactly {sorted(fields)}")
	return value


def validate_candidate_snapshot(value: object) -> dict:
	"""Validate the complete, versioned candidate-export boundary."""
	value = _exact(
		value,
		{"coverage", "discovery", "environment", "schema_version", "site"},
		"candidate snapshot",
	)
	if value["schema_version"] != CANDIDATE_SNAPSHOT_SCHEMA_VERSION:
		raise ValueError("unsupported candidate snapshot schema")
	if value["site"] != "development.localhost":
		raise ValueError("candidate snapshot site must be development.localhost")

	environment = _exact(
		value["environment"],
		{"babel", "installed_apps", "inventory_digest", "python", "upstream"},
		"candidate snapshot environment",
	)
	if environment["installed_apps"] != ["frappe", "erpnext", "frappe_lt"]:
		raise ValueError("candidate snapshot installed app order is invalid")
	if not isinstance(environment["python"], str) or not environment["python"].startswith("3.14."):
		raise ValueError("candidate snapshot Python version is invalid")
	if not isinstance(environment["babel"], str) or not environment["babel"]:
		raise ValueError("candidate snapshot Babel version is invalid")
	if not isinstance(environment["inventory_digest"], str) or not SHA256.fullmatch(
		environment["inventory_digest"]
	):
		raise ValueError("candidate snapshot inventory digest is invalid")
	upstream = _exact(environment["upstream"], set(EXPECTED_RUNTIME_APPS), "candidate snapshot upstream")
	for app in EXPECTED_RUNTIME_APPS:
		pin = _exact(upstream[app], {"commit", "version"}, f"candidate snapshot {app} pin")
		if not isinstance(pin["commit"], str) or not COMMIT.fullmatch(pin["commit"]):
			raise ValueError(f"candidate snapshot {app} commit is invalid")
		if not isinstance(pin["version"], str) or not pin["version"]:
			raise ValueError(f"candidate snapshot {app} version is invalid")

	discovery_result = _exact(
		value["discovery"], {"candidates", "collector_counts"}, "candidate snapshot discovery"
	)
	counts = discovery_result["collector_counts"]
	if not isinstance(counts, dict) or set(counts) != {"email", "metadata", "portal", "print"}:
		raise ValueError("candidate snapshot collector counts are invalid")
	if any(isinstance(count, bool) or not isinstance(count, int) or count < 1 for count in counts.values()):
		raise ValueError("candidate snapshot collectors must each produce candidates")
	candidates = discovery_result["candidates"]
	if not isinstance(candidates, list):
		raise ValueError("candidate snapshot candidates must be a list")
	ids = []
	for candidate in candidates:
		candidate = _exact(candidate, {"app", "id", "identity", "type"}, "runtime candidate")
		if candidate["app"] not in EXPECTED_RUNTIME_APPS:
			raise ValueError("candidate snapshot contains an out-of-scope app")
		if not all(
			isinstance(candidate[field], str) and candidate[field] for field in ("id", "identity", "type")
		):
			raise ValueError("candidate snapshot candidate identity is invalid")
		if candidate["id"] != f"{candidate['type']}:{quote(candidate['identity'], safe='._-')}":
			raise ValueError("candidate snapshot candidate id is inconsistent")
		ids.append(candidate["id"])
	if ids != sorted(set(ids)):
		raise ValueError("candidate snapshot candidates must use unique canonical order")

	coverage_result = _exact(
		value["coverage"], {"covered", "gaps", "reviewed_exclusions"}, "candidate snapshot coverage"
	)
	groups = []
	for name in ("covered", "gaps", "reviewed_exclusions"):
		group = coverage_result[name]
		if (
			not isinstance(group, list)
			or group != sorted(set(group))
			or not all(isinstance(candidate_id, str) for candidate_id in group)
		):
			raise ValueError(f"candidate snapshot {name} must use unique canonical order")
		groups.append(set(group))
	if any(left & right for index, left in enumerate(groups) for right in groups[index + 1 :]):
		raise ValueError("candidate snapshot coverage groups must be disjoint")
	if set().union(*groups) != set(ids):
		raise ValueError("candidate snapshot coverage must partition every discovered candidate")
	return value


def export_candidate_snapshot(
	site: str,
	output_path: str,
	*,
	frappe_module=None,
	replace=os.replace,
) -> dict:
	"""Preflight and atomically export pinned runtime discovery without site mutation."""
	if site != "development.localhost":
		raise ValueError("candidate snapshot target must be development.localhost")
	frappe = frappe_module
	if frappe is None:
		import frappe as frappe_module

		frappe = frappe_module
	contracts = load_contracts()
	environment = verify_environment(
		frappe,
		site=site,
		require_clean_upstream=True,
		required_apps=("frappe", "erpnext", "frappe_lt"),
	)
	discovery_result = discover(frappe)
	coverage_result = coverage(discovery_result, contracts["scenarios"], contracts["classifications"])
	snapshot = validate_candidate_snapshot(
		{
			"coverage": coverage_result,
			"discovery": discovery_result,
			"environment": {
				"babel": environment["babel"],
				"installed_apps": environment["installed_apps"],
				"inventory_digest": environment["inventory_digest"],
				"python": environment["python"],
				"upstream": {
					app: {"commit": pin["commit"], "version": pin["version"]}
					for app, pin in sorted(environment["upstream"].items())
				},
			},
			"schema_version": CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
			"site": site,
		}
	)
	content = canonical_json(snapshot)
	if len(content) > MAX_CANDIDATE_SNAPSHOT_BYTES:
		raise ValueError(f"candidate snapshot exceeds {MAX_CANDIDATE_SNAPSHOT_BYTES} bytes")
	requested_output = Path(output_path).absolute()
	if requested_output.is_symlink():
		raise ValueError("candidate snapshot output must not be a symlink")
	output = requested_output.parent.resolve() / requested_output.name
	if not output.parent.is_dir():
		raise ValueError(f"candidate snapshot output directory does not exist: {output.parent}")
	temporary = None
	try:
		fd, raw_path = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
		temporary = Path(raw_path)
		with os.fdopen(fd, "wb") as stream:
			stream.write(content)
			stream.flush()
			os.fsync(stream.fileno())
		replace(temporary, output)
		directory_fd = os.open(output.parent, os.O_RDONLY)
		try:
			os.fsync(directory_fd)
		finally:
			os.close(directory_fd)
	finally:
		if temporary is not None:
			temporary.unlink(missing_ok=True)
	return {
		"candidate_count": len(snapshot["discovery"]["candidates"]),
		"output": str(output),
		"schema_version": CANDIDATE_SNAPSHOT_SCHEMA_VERSION,
		"sha256": hashlib.sha256(content).hexdigest(),
	}
