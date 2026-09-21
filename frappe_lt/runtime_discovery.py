from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

from frappe_lt.runtime_extraction import EXPECTED_RUNTIME_APPS, collect_standard_metadata


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
		"reviewed_out_of_scope": sorted(candidate_ids & classified_ids),
	}
