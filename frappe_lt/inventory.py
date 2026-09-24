import hashlib
import html
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path, PurePosixPath

COMPATIBILITY_PATH = Path(__file__).with_name("compatibility.json")
PROVENANCE_PATH = Path(__file__).with_name("provenance.json")
AUTHENTICATED_ARTIFACTS = (
	"inventory_report.json",
	"inventory_report.md",
	"provenance.json",
	"release_inventory.json",
)
QUALITY_GATE_ARTIFACTS = (
	"catalog_partition.json",
	"catalog_segment_ownership_overrides.json",
	"catalog_segments.json",
	"collision_resolutions.json",
	"glossary_selectors.json",
	"translation_exceptions.json",
)
ACCOUNT_CONTEXT_DECISION = "CONTEXT.md#saskaita-account-contextless-collision"
ACCOUNT_COLLISION_LOCATORS = {
	"erpnext:doctype:Account:name",
	"frappe:doctype:Email Account:field:account_section:label",
}
RUNTIME_METADATA_CATEGORIES = (
	"doctype",
	"custom_field",
	"navbar",
	"page",
	"property_setter",
	"report",
	"workflow",
	"workspace",
	"workspace_sidebar",
)


def _json_object(pairs):
	value = {}
	for key, child in pairs:
		if key in value:
			raise ValueError(f"duplicate JSON object member {key!r}")
		value[key] = child
	return value


@dataclass(frozen=True)
class ExtractionEvent:
	source: str
	context: str | None
	app: str
	origin: str
	raw_source: str
	source_location: str
	extractor: str
	line: int | None = None
	stable_locator: str | None = None


def validate_compatibility(manifest: dict, *, allow_legacy_baseline: bool = False) -> dict:
	"""Validate the release compatibility contract used by inventory and verification."""
	schema_version = manifest.get("schema_version")
	if schema_version != 2 and not (allow_legacy_baseline and schema_version == 1):
		raise ValueError("compatibility manifest schema_version must be 2")
	if set(manifest.get("upstream", {})) != {"frappe", "erpnext"}:
		raise ValueError("compatibility manifest must pin exactly frappe and erpnext")
	for app, pin in manifest["upstream"].items():
		if not isinstance(pin.get("version"), str) or not pin["version"]:
			raise ValueError(f"{app} must have a nonempty version")
		if not isinstance(pin.get("commit"), str) or not re.fullmatch("[0-9a-f]{40}", pin["commit"]):
			raise ValueError(f"{app} must have a full commit SHA")
	if manifest.get("schema_versions") != {"inventory": 1, "provenance": 1, "report": 1}:
		raise ValueError("unsupported artifact schema versions")
	if not isinstance(manifest.get("source_date_epoch"), int):
		raise ValueError("source_date_epoch must be an integer")
	if manifest.get("tools") != {"babel": "2.16.0", "python": "3.14"}:
		raise ValueError("compatibility tools must pin Python 3.14 and Babel 2.16.0")
	artifact_sha256 = manifest.get("artifact_sha256")
	if not isinstance(artifact_sha256, dict) or set(artifact_sha256) != set(AUTHENTICATED_ARTIFACTS):
		raise ValueError(f"artifact_sha256 must authenticate exactly {list(AUTHENTICATED_ARTIFACTS)}")
	for name, digest in artifact_sha256.items():
		if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
			raise ValueError(f"artifact_sha256 for {name} must be a SHA-256 digest")
	quality_gate = manifest.get("quality_gate")
	if schema_version == 2 or quality_gate is not None:
		if not isinstance(quality_gate, dict) or quality_gate.get("schema_version") != 1:
			raise ValueError("unsupported Catalog Quality Gate schema")
		quality_digests = quality_gate.get("artifact_sha256")
		if not isinstance(quality_digests, dict) or set(quality_digests) != set(QUALITY_GATE_ARTIFACTS):
			raise ValueError(f"Catalog Quality Gate must authenticate exactly {list(QUALITY_GATE_ARTIFACTS)}")
		for name, digest in quality_digests.items():
			if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
				raise ValueError(f"Catalog Quality Gate digest for {name} must be a SHA-256 digest")
	for field in ("mo_sha256", "inventory_digest"):
		value = manifest.get(field)
		if value is not None and (not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value)):
			raise ValueError(f"{field} must be null or a SHA-256 digest")
	runtime_metadata = manifest.get("runtime_metadata_sha256")
	if not isinstance(runtime_metadata, dict) or set(runtime_metadata) != set(RUNTIME_METADATA_CATEGORIES):
		raise ValueError(f"runtime_metadata_sha256 must cover exactly {list(RUNTIME_METADATA_CATEGORIES)}")
	for category, digest in runtime_metadata.items():
		if (
			not isinstance(category, str)
			or not isinstance(digest, str)
			or not re.fullmatch("[0-9a-f]{64}", digest)
		):
			raise ValueError(f"runtime_metadata_sha256 for {category!r} must be a SHA-256 digest")
	return manifest


def load_compatibility(path: Path = COMPATIBILITY_PATH, *, allow_legacy_baseline: bool = False) -> dict:
	"""Load the release compatibility contract used by inventory and verification."""
	manifest = json.loads(
		path.read_text(encoding="utf-8"),
		object_pairs_hook=_json_object,
		parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON number {value}")),
	)
	return validate_compatibility(manifest, allow_legacy_baseline=allow_legacy_baseline)


def verify_owned_artifacts(path: Path = COMPATIBILITY_PATH) -> dict:
	"""Verify every versioned artifact authenticated by the compatibility commit marker."""
	manifest = load_compatibility(path)
	for name, expected in manifest["artifact_sha256"].items():
		artifact_path = path.parent / name
		try:
			content = artifact_path.read_bytes()
		except OSError as error:
			raise ValueError(f"could not read authenticated artifact {name}: {error}") from error
		actual = hashlib.sha256(content).hexdigest()
		if actual != expected:
			raise ValueError(f"artifact digest mismatch for {name}: expected {expected}; computed {actual}")
	for name, expected in manifest["quality_gate"]["artifact_sha256"].items():
		artifact_path = path.parent / name
		try:
			content = artifact_path.read_bytes()
		except OSError as error:
			raise ValueError(f"could not read authenticated artifact {name}: {error}") from error
		actual = hashlib.sha256(content).hexdigest()
		if actual != expected:
			raise ValueError(f"artifact digest mismatch for {name}: expected {expected}; computed {actual}")
	if manifest["artifact_sha256"]["release_inventory.json"] != manifest.get("inventory_digest"):
		raise ValueError("inventory_digest must equal the authenticated release_inventory.json digest")
	_quality_artifacts_for_inventory(manifest, manifest["inventory_digest"], path.parent)
	return manifest


def validate_tool_versions(
	manifest: dict,
	python_version: tuple[int, int] | None = None,
	babel_version: str | None = None,
) -> dict[str, str]:
	"""Validate the interpreter and Babel used to create deterministic release artifacts."""
	python_version = python_version or sys.version_info[:2]
	babel_version = babel_version or version("Babel")
	expected_python = tuple(int(part) for part in manifest["tools"]["python"].split("."))
	if python_version != expected_python:
		raise ValueError(
			f"Python must be {manifest['tools']['python']}.x; found {'.'.join(map(str, python_version))}"
		)
	if babel_version != manifest["tools"]["babel"]:
		raise ValueError(f"Babel must be {manifest['tools']['babel']}; found {babel_version}")
	return {"python": ".".join(map(str, python_version)), "babel": babel_version}


def canonical_json(value: object) -> bytes:
	return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _normalized_source(source: object) -> str:
	try:
		import frappe
	except ImportError:
		value = str(source)
	else:
		value = frappe.as_unicode(source)
	return value.strip()


def _validate_event(event: ExtractionEvent, upstream: dict) -> None:
	if event.app not in upstream:
		raise ValueError(f"extraction event has unpinned app {event.app!r}")
	if event.origin not in {"source", "runtime"}:
		raise ValueError(f"invalid extraction origin {event.origin!r}")
	if event.context == "":
		raise ValueError("Frappe context must be null or nonempty")
	if not _normalized_source(event.source):
		raise ValueError("normalized source must be nonempty")
	location = PurePosixPath(event.source_location)
	if (
		location.is_absolute()
		or "\\" in event.source_location
		or ".." in location.parts
		or not event.source_location
	):
		raise ValueError(
			f"source location must be an owning-app-relative POSIX path: {event.source_location!r}"
		)


def build_inventory(
	events: list[ExtractionEvent], manifest: dict, runtime_categories: tuple[str, ...] | None = None
) -> dict:
	"""Build a deterministic release inventory from source and runtime events."""
	upstream = manifest["upstream"]
	grouped: dict[tuple[str, str | None], list[ExtractionEvent]] = {}
	for event in events:
		_validate_event(event, upstream)
		key = (_normalized_source(event.source), event.context)
		grouped.setdefault(key, []).append(event)

	entries = []
	for (source, context), key_events in sorted(
		grouped.items(), key=lambda item: (item[0][0], item[0][1] is not None, item[0][1] or "")
	):
		facts = []
		for event in key_events:
			fact = asdict(event)
			fact["source"] = source
			facts.append(fact)
		facts = sorted(
			{canonical_json(fact): fact for fact in facts}.values(),
			key=lambda fact: (
				fact["app"],
				fact["origin"],
				fact["source_location"],
				fact["line"] is None,
				fact["line"] or 0,
				fact["extractor"],
				fact["raw_source"],
				fact["stable_locator"] or "",
			),
		)
		origins = {fact["origin"] for fact in facts}
		locations = [
			{
				"app": fact["app"],
				"extractor": fact["extractor"],
				"line": fact["line"],
				"origin": fact["origin"],
				"path": fact["source_location"],
			}
			for fact in facts
		]
		locators = sorted(
			{
				f"{fact['app']}:{fact['stable_locator']}"
				for fact in facts
				if fact["stable_locator"] is not None
			}
		)
		entry_facts = {
			"key": {"context": context, "source": source},
			"events": facts,
		}
		entry = {
			"apps": sorted({fact["app"] for fact in facts}),
			"extraction_origin": "both" if len(origins) == 2 else next(iter(origins)),
			"key": entry_facts["key"],
			"raw_sources": sorted({fact["raw_source"] for fact in facts}),
			"source_digest": hashlib.sha256(canonical_json(entry_facts)).hexdigest(),
			"source_locations": locations,
			"stable_locators": locators,
			"upstream_versions": {
				app: upstream[app]["version"] for app in sorted({fact["app"] for fact in facts})
			},
		}
		if source == "Account" and context is None:
			missing_locators = ACCOUNT_COLLISION_LOCATORS - set(locators)
			if missing_locators:
				raise ValueError(
					"contextless Account invariant is missing pinned collision locations: "
					+ ", ".join(sorted(missing_locators))
				)
			entry["context_decision"] = ACCOUNT_CONTEXT_DECISION
		entries.append(entry)

	inventory = {
		"entries": entries,
		"schema_version": manifest["schema_versions"]["inventory"],
		"source_date_epoch": manifest["source_date_epoch"],
		"upstream": upstream,
	}
	if runtime_categories is not None:
		inventory["runtime_categories"] = sorted(set(runtime_categories))
		inventory["runtime_category_counts"] = {
			category: sum(event.origin == "runtime" and event.extractor == category for event in events)
			for category in inventory["runtime_categories"]
		}
	return inventory


def _key_tuple(key: dict) -> tuple[str, str | None]:
	if set(key) != {"source", "context"} or not isinstance(key["source"], str):
		raise ValueError(f"invalid Translation Key {key!r}")
	if key["context"] is not None and not isinstance(key["context"], str):
		raise ValueError(f"invalid Translation Key context {key['context']!r}")
	if key["context"] == "":
		raise ValueError("Frappe context must be null or nonempty")
	return key["source"], key["context"]


def validate_provenance(inventory: dict, provenance: dict) -> dict[tuple[str, str | None], dict]:
	"""Validate provenance schema and exact referential integrity against active keys."""
	if provenance.get("schema_version") != 1:
		raise ValueError("provenance schema_version must be 1")
	active_keys = {_key_tuple(entry["key"]) for entry in inventory["entries"]}
	validated = {}
	for record in provenance.get("entries", []):
		key = _key_tuple(record.get("key", {}))
		if key in validated:
			raise ValueError(f"duplicate provenance record for {key!r}")
		if key not in active_keys:
			raise ValueError(f"provenance contains unknown Translation Key {key!r}")

		status = record.get("status")
		origin = record.get("origin")
		translation = record.get("translation")
		exception = record.get("exception")
		if status == "translated":
			if not isinstance(translation, str) or not translation.strip():
				raise ValueError(f"translated key {key!r} requires a nonempty translation")
			if origin not in {"inherited_v15", "new_ai", "corrected_inherited"}:
				raise ValueError(f"translated key {key!r} has invalid origin {origin!r}")
			if exception is not None:
				raise ValueError(f"translated key {key!r} cannot also have an exception")
		elif status == "excepted":
			if not isinstance(exception, str) or not exception.strip():
				raise ValueError(f"excepted key {key!r} requires an explicit exception")
			if origin != "approved_exception":
				raise ValueError(f"excepted key {key!r} must use approved_exception origin")
			if translation is not None:
				raise ValueError(f"excepted key {key!r} cannot also have a translation")
			if "v15_original" in record and (
				not isinstance(record["v15_original"], str) or not record["v15_original"].strip()
			):
				raise ValueError(f"excepted key {key!r} has invalid authenticated v15 original")
		elif status == "missing":
			if translation is not None or exception is not None or origin is not None:
				raise ValueError(f"missing key {key!r} cannot have translation provenance or exception")
		else:
			raise ValueError(f"key {key!r} has unknown coverage status {status!r}")
		validated[key] = record

	missing = active_keys - validated.keys()
	if missing:
		raise ValueError(f"provenance is missing {len(missing)} active Translation Key records")
	return validated


def _validate_baseline(
	inventory: dict, manifest: dict, digest: str, inventory_bytes: bytes | None = None
) -> None:
	if inventory.get("schema_version") != manifest["schema_versions"]["inventory"]:
		raise ValueError("baseline inventory has unsupported schema_version")
	if inventory.get("upstream") != manifest["upstream"]:
		raise ValueError("baseline inventory uses a different pinned upstream pair")
	if inventory.get("source_date_epoch") != manifest["source_date_epoch"]:
		raise ValueError("baseline inventory uses a different source_date_epoch")
	if not isinstance(inventory.get("entries"), list):
		raise ValueError("baseline inventory entries must be a list")
	seen = set()
	for entry in inventory["entries"]:
		key = _key_tuple(entry.get("key", {}))
		if key in seen:
			raise ValueError(f"baseline inventory contains duplicate Translation Key {key!r}")
		seen.add(key)
		if not isinstance(entry.get("apps"), list) or not set(entry["apps"]) <= set(manifest["upstream"]):
			raise ValueError(f"baseline inventory entry {key!r} has invalid apps")
		if not isinstance(entry.get("source_digest"), str) or not re.fullmatch(
			"[0-9a-f]{64}", entry["source_digest"]
		):
			raise ValueError(f"baseline inventory entry {key!r} has invalid source_digest")
	canonical = canonical_json(inventory)
	if inventory_bytes is not None and inventory_bytes != canonical:
		raise ValueError("baseline inventory is not canonical JSON")
	actual_digest = hashlib.sha256(inventory_bytes or canonical).hexdigest()
	if actual_digest != digest:
		raise ValueError(f"baseline digest mismatch: expected {digest}; computed {actual_digest}")
	if manifest["artifact_sha256"]["release_inventory.json"] != digest:
		raise ValueError("baseline manifest does not authenticate its release inventory")


def build_report(
	inventory: dict,
	provenance: dict,
	manifest: dict,
	previous_inventory: dict | None = None,
	previous_digest: str | None = None,
	previous_manifest: dict | None = None,
	previous_inventory_bytes: bytes | None = None,
) -> dict:
	"""Build the machine report; human output must consume only this result."""
	coverage = validate_provenance(inventory, provenance)
	inventory_digest = hashlib.sha256(canonical_json(inventory)).hexdigest()
	if len(
		[value for value in (previous_inventory, previous_digest, previous_manifest) if value is not None]
	) not in {0, 3}:
		raise ValueError("baseline inventory, digest, and manifest must be provided together")
	if previous_inventory is not None:
		_validate_baseline(
			previous_inventory,
			previous_manifest,
			previous_digest,
			previous_inventory_bytes,
		)

	current_by_key = {_key_tuple(entry["key"]): entry for entry in inventory["entries"]}
	old_by_key = (
		{_key_tuple(entry["key"]): entry for entry in previous_inventory["entries"]}
		if previous_inventory is not None
		else {}
	)
	matches = {
		key: key
		for key in current_by_key.keys() & old_by_key.keys()
		if set(current_by_key[key]["apps"]) & set(old_by_key[key]["apps"])
	}
	unmatched_current = current_by_key.keys() - matches.keys()
	unmatched_old = old_by_key.keys() - matches.values()

	current_identities: dict[tuple[str, str | None], set[tuple[str, str | None]]] = {}
	old_identities: dict[tuple[str, str | None], set[tuple[str, str | None]]] = {}
	for key in unmatched_current:
		for locator in current_by_key[key]["stable_locators"]:
			current_identities.setdefault((locator, key[1]), set()).add(key)
	for key in unmatched_old:
		for locator in old_by_key[key]["stable_locators"]:
			old_identities.setdefault((locator, key[1]), set()).add(key)
	proposed_current: dict[tuple[str, str | None], set[tuple[str, str | None]]] = {}
	proposed_old: dict[tuple[str, str | None], set[tuple[str, str | None]]] = {}
	for identity in current_identities.keys() & old_identities.keys():
		if len(current_identities[identity]) != 1 or len(old_identities[identity]) != 1:
			continue
		current_key = next(iter(current_identities[identity]))
		old_key = next(iter(old_identities[identity]))
		proposed_current.setdefault(current_key, set()).add(old_key)
		proposed_old.setdefault(old_key, set()).add(current_key)
	for current_key, old_keys in proposed_current.items():
		if len(old_keys) != 1:
			continue
		old_key = next(iter(old_keys))
		if len(proposed_old[old_key]) == 1:
			matches[current_key] = old_key

	entries = []
	for entry in inventory["entries"]:
		key = _key_tuple(entry["key"])
		record = coverage[key]
		old_key = matches.get(key)
		if old_key is None:
			lifecycle = "new"
		elif old_key != key or old_by_key[old_key]["source_digest"] != entry["source_digest"]:
			lifecycle = "changed"
		else:
			lifecycle = "unchanged"
		report_entry = {
			"coverage": record["status"],
			"key": entry["key"],
			"lifecycle": lifecycle,
		}
		if old_key is not None:
			report_entry["previous_key"] = old_by_key[old_key]["key"]
		entries.append(report_entry)
	matched_old = set(matches.values())
	removed = [
		{
			"key": old_by_key[key]["key"],
			"lifecycle": "removed",
			"source_digest": old_by_key[key]["source_digest"],
		}
		for key in sorted(old_by_key.keys() - matched_old, key=lambda value: (value[0], value[1] or ""))
	]
	coverage_totals = {status: 0 for status in ("excepted", "missing", "translated")}
	lifecycle_totals = {status: 0 for status in ("changed", "new", "removed", "unchanged")}
	for entry in entries:
		coverage_totals[entry["coverage"]] += 1
		lifecycle_totals[entry["lifecycle"]] += 1
	lifecycle_totals["removed"] = len(removed)
	return {
		"baseline": {"inventory_digest": previous_digest, "provided": previous_inventory is not None},
		"entries": entries,
		"inventory_digest": inventory_digest,
		"removed": removed,
		"schema_version": manifest["schema_versions"]["report"],
		"summary": {
			"coverage": coverage_totals,
			"lifecycle": lifecycle_totals,
		},
		"upstream": inventory["upstream"],
	}


def generate_human_report(report: dict) -> str:
	"""Render Markdown solely from the validated machine report."""
	lifecycle = report["summary"]["lifecycle"]
	coverage = report["summary"]["coverage"]
	lines = [
		"# Translation Inventory Report",
		"",
		f"Inventory digest: `{report['inventory_digest']}`",
		"",
		"## Lifecycle",
		"",
		f"- New: {lifecycle['new']}",
		f"- Changed: {lifecycle['changed']}",
		f"- Removed: {lifecycle['removed']}",
		f"- Unchanged: {lifecycle['unchanged']}",
		"",
		"## Coverage",
		"",
		f"- Missing: {coverage['missing']}",
		f"- Translated: {coverage['translated']}",
		f"- Excepted: {coverage['excepted']}",
		"",
		"## Active Translation Keys",
		"",
	]
	columns = ["Source", "Context", "Lifecycle", "Coverage"]
	if report["baseline"]["provided"]:
		columns.append("Previous key")
	lines.extend(
		[
			f"| {' | '.join(columns)} |",
			f"| {' | '.join('---' for _column in columns)} |",
		]
	)
	for entry in report["entries"]:
		source = _markdown_cell(entry["key"]["source"])
		context = _markdown_cell(entry["key"]["context"] or "")
		values = [f"<code>{source}</code>", context, entry["lifecycle"], entry["coverage"]]
		if report["baseline"]["provided"]:
			previous = entry.get("previous_key")
			previous_value = (
				_markdown_cell(json.dumps(previous, ensure_ascii=False, sort_keys=True)) if previous else ""
			)
			values.append(f"<code>{previous_value}</code>" if previous_value else "")
		lines.append(f"| {' | '.join(values)} |")
	lines.extend(["", "## Removed Translation Keys"])
	for entry in report["removed"]:
		key = _markdown_cell(json.dumps(entry["key"], ensure_ascii=False, sort_keys=True))
		lines.append(f"- <code>{key}</code>")
	lines.append("")
	return "\n".join(lines)


def _markdown_cell(value: str) -> str:
	return (
		html.escape(value)
		.replace("|", "&#124;")
		.replace("\r\n", "<br>")
		.replace("\r", "<br>")
		.replace("\n", "<br>")
	)


def write_artifacts(output_dir: Path, artifacts: dict[str, bytes], replace=os.replace) -> None:
	"""Write complete temp files and replace compatibility.json last as the commit marker."""
	if "compatibility.json" not in artifacts:
		raise ValueError("artifact transaction requires compatibility.json commit marker")
	output_dir.mkdir(parents=True, exist_ok=True)
	output_dir = output_dir.resolve()
	paths = {}
	for name in artifacts:
		relative = PurePosixPath(name) if isinstance(name, str) else None
		if (
			relative is None
			or not name
			or "\\" in name
			or relative.is_absolute()
			or str(relative) != name
			or any(part in {"", ".", ".."} for part in relative.parts)
		):
			raise ValueError(f"invalid artifact path {name!r}")
		target = output_dir.joinpath(*relative.parts)
		target.parent.mkdir(parents=True, exist_ok=True)
		if target.resolve().parent != output_dir and output_dir not in target.resolve().parents:
			raise ValueError(f"artifact path escapes output directory: {name}")
		if any(path.is_symlink() for path in (target, *target.parents) if path != output_dir.parent):
			raise ValueError(f"artifact path uses a symlink: {name}")
		paths[name] = target
	temporary = {}
	try:
		for name, content in sorted(artifacts.items()):
			target = paths[name]
			fd, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
			temp = Path(raw_temp)
			with os.fdopen(fd, "wb") as stream:
				stream.write(content)
				stream.flush()
				os.fsync(stream.fileno())
			temporary[name] = (temp, target)
		for name in sorted(artifacts.keys() - {"compatibility.json"}):
			replace(*temporary[name])
		replace(*temporary["compatibility.json"])
		directory_fd = os.open(output_dir, os.O_RDONLY)
		try:
			os.fsync(directory_fd)
		finally:
			os.close(directory_fd)
	finally:
		for temp, _target in temporary.values():
			temp.unlink(missing_ok=True)


def _git_value(path: Path, *arguments: str) -> str:
	try:
		return subprocess.run(
			["git", *arguments], cwd=path, check=True, capture_output=True, text=True
		).stdout.strip()
	except subprocess.CalledProcessError as error:
		raise ValueError(f"could not inspect upstream worktree {path}: {error.stderr.strip()}") from error


def clean_candidate_identity(path: Path | None = None) -> dict:
	"""Return the selected clean frappe_lt commit in its canonical public shape."""
	repository = (path or Path(__file__).parent.parent).resolve()
	commit = _git_value(repository, "rev-parse", "HEAD")
	if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
		raise ValueError("frappe_lt candidate commit is invalid")
	if _git_value(repository, "status", "--porcelain"):
		raise ValueError("frappe_lt candidate worktree must be clean")
	return {"clean": True, "commit": commit}


def _verify_upstream(frappe, manifest: dict) -> None:
	verify_environment(frappe, site=frappe.local.site, require_clean_upstream=True)


def verify_environment(
	frappe,
	*,
	site: str,
	require_clean_upstream: bool = False,
	required_apps: tuple[str, ...] = ("frappe", "erpnext"),
	require_exact_apps: bool = False,
	require_active_catalog: bool = False,
	require_runtime_metadata: bool = False,
	require_active_directory: bool = True,
) -> dict:
	"""Read-only verification of the site, Compatibility pins and full release MO."""
	import erpnext

	if not site or getattr(frappe.local, "site", None) != site:
		raise ValueError(f"command site {site!r} does not match initialized site {frappe.local.site!r}")
	manifest = verify_owned_artifacts()
	tools = validate_tool_versions(manifest)
	installed_apps = frappe.get_installed_apps()
	if require_exact_apps and installed_apps != list(required_apps):
		raise ValueError(f"site must contain exactly {list(required_apps)}; found {installed_apps}")
	positions = []
	for app in required_apps:
		if app not in installed_apps:
			raise ValueError(f"required app {app!r} is not installed on {site}")
		positions.append(installed_apps.index(app))
	if positions != sorted(positions) or len(set(positions)) != len(positions):
		raise ValueError(f"required app order is {list(required_apps)}; installed order is {installed_apps}")

	versions = {"frappe": frappe.__version__, "erpnext": erpnext.__version__}
	upstream = {}
	bench_paths = set()
	for app, pin in manifest["upstream"].items():
		source_path = Path(frappe.get_app_source_path(app)).resolve()
		path = Path(_git_value(source_path, "rev-parse", "--show-toplevel")).resolve()
		bench_paths.add(path.parent.parent)
		commit = _git_value(path, "rev-parse", "HEAD")
		if commit != pin["commit"]:
			raise ValueError(f"{app} commit must be {pin['commit']}; found {commit}")
		if versions[app] != pin["version"]:
			raise ValueError(f"{app} version must be {pin['version']}; found {versions[app]}")
		if require_clean_upstream and (dirty := _git_value(path, "status", "--porcelain")):
			raise ValueError(f"{app} worktree must be clean before extraction; found:\n{dirty}")
		upstream[app] = {"commit": commit, "path": path.as_posix(), "version": versions[app]}
	if len(bench_paths) != 1:
		raise ValueError(
			f"pinned upstream applications are not in one bench: {sorted(map(str, bench_paths))}"
		)
	bench_path = bench_paths.pop()
	active_directory = bench_path / "sites"
	if require_active_directory and Path.cwd().resolve() != active_directory:
		raise ValueError(
			f"active directory must be bench sites directory {active_directory}; found {Path.cwd().resolve()}"
		)
	mo_sha256 = None
	if require_active_catalog:
		from frappe_lt.release_catalog import verify_mo

		# Compatibility's MO digest belongs to the historical Item-only smoke.
		# verify_mo authenticates the independently pinned full release and active MO.
		mo_sha256 = verify_mo()
	if require_runtime_metadata:
		from frappe_lt.runtime_extraction import verify_standard_metadata

		verify_standard_metadata(frappe, manifest["runtime_metadata_sha256"])

	return {
		"active_directory": active_directory.as_posix(),
		"babel": tools["babel"],
		"installed_apps": installed_apps,
		"inventory_digest": manifest["inventory_digest"],
		"mo_sha256": mo_sha256,
		"python": platform.python_version(),
		"site": site,
		"upstream": upstream,
	}


def _load_provenance(path: Path) -> dict:
	provenance = json.loads(path.read_text(encoding="utf-8"))
	for record in provenance["entries"]:
		for field in ("translation", "origin", "exception"):
			if record.get(field) is None:
				record.pop(field, None)
	provenance["entries"].sort(key=lambda record: (record["key"]["source"], record["key"]["context"] or ""))
	return provenance


def _quality_artifacts_for_inventory(
	manifest: dict, inventory_digest: str, root: Path | None = None
) -> dict[str, bytes]:
	root = (root or Path(__file__).parent).resolve()
	artifacts = {}
	quality_objects = {}
	for name, expected in manifest["quality_gate"]["artifact_sha256"].items():
		content = (root / name).read_bytes()
		if hashlib.sha256(content).hexdigest() != expected:
			raise ValueError(f"artifact digest mismatch for {name}")
		quality_artifact = json.loads(
			content,
			object_pairs_hook=_json_object,
			parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON number {value}")),
		)
		if quality_artifact.get("inventory_digest") != inventory_digest:
			raise ValueError(f"{name} does not reference the newly generated Release Inventory digest")
		artifacts[name] = content
		quality_objects[name] = quality_artifact

	registry = quality_objects["catalog_segments.json"]
	records = registry.get("candidates")
	if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
		raise ValueError("Catalog Segment registry candidates must be a list of objects")
	for record in records:
		for path_field, digest_field, label in (
			("candidate", "candidate_sha256", "candidate"),
			("manifest", "manifest_sha256", "segment manifest"),
		):
			name = record.get(path_field)
			expected = record.get(digest_field)
			if name is None and expected is None and path_field == "manifest":
				continue
			relative = PurePosixPath(name) if isinstance(name, str) else None
			if (
				relative is None
				or not name
				or "\\" in name
				or relative.is_absolute()
				or str(relative) != name
				or any(part in {"", ".", ".."} for part in relative.parts)
			):
				raise ValueError(f"invalid registered {label} path {name!r}")
			if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
				raise ValueError(f"registered {label} digest must be a SHA-256 digest")
			path = root.joinpath(*relative.parts)
			for component in (path, *path.parents):
				if component == root.parent:
					break
				if component.is_symlink():
					raise ValueError(f"registered {label} path uses a symlink: {name}")
			resolved = path.resolve()
			if resolved.parent != root and root not in resolved.parents:
				raise ValueError(f"registered {label} path escapes the inventory root: {name}")
			content = path.read_bytes()
			if hashlib.sha256(content).hexdigest() != expected:
				raise ValueError(f"{label} digest mismatch for {record.get('name')!r}")
			if name in artifacts and artifacts[name] != content:
				raise ValueError(f"registered artifact path collision: {name}")
			artifacts[name] = content
	partition = quality_objects["catalog_partition.json"]
	segments = partition.get("segments")
	if not isinstance(segments, list) or not all(isinstance(record, dict) for record in segments):
		raise ValueError("Catalog Segment partition segments must be a list of objects")
	for record in segments:
		name = record.get("manifest")
		expected = record.get("manifest_sha256")
		relative = PurePosixPath(name) if isinstance(name, str) else None
		if (
			relative is None
			or not name
			or "\\" in name
			or relative.is_absolute()
			or str(relative) != name
			or any(part in {"", ".", ".."} for part in relative.parts)
		):
			raise ValueError(f"invalid production segment manifest path {name!r}")
		if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
			raise ValueError("production segment manifest digest must be a SHA-256 digest")
		path = root.joinpath(*relative.parts)
		for component in (path, *path.parents):
			if component == root.parent:
				break
			if component.is_symlink():
				raise ValueError(f"production segment manifest path uses a symlink: {name}")
		resolved = path.resolve()
		if resolved.parent != root and root not in resolved.parents:
			raise ValueError(f"production segment manifest path escapes the inventory root: {name}")
		content = path.read_bytes()
		if hashlib.sha256(content).hexdigest() != expected:
			raise ValueError(f"production segment manifest digest mismatch for {record.get('id')!r}")
		if name in artifacts and artifacts[name] != content:
			raise ValueError(f"registered artifact path collision: {name}")
		artifacts[name] = content
	return artifacts


def run(
	site: str,
	output_dir: str | None = None,
	previous_inventory: str | None = None,
	previous_compatibility: str | None = None,
) -> dict:
	"""Build inventory, comparison, and both reports through the sole public orchestration path."""
	import frappe

	from frappe_lt.runtime_extraction import extract_runtime
	from frappe_lt.source_extraction import extract_sources

	if not site or frappe.local.site != site:
		raise ValueError(f"command site {site!r} does not match initialized site {frappe.local.site!r}")
	manifest = load_compatibility()
	validate_tool_versions(manifest)
	verify_environment(
		frappe,
		site=site,
		require_clean_upstream=True,
		required_apps=("frappe", "erpnext"),
	)
	runtime = extract_runtime(frappe, manifest["runtime_metadata_sha256"])
	events = runtime.events
	events.extend(extract_sources(frappe))
	inventory = build_inventory(events, manifest, runtime.categories)
	provenance = _load_provenance(PROVENANCE_PATH)

	baseline = baseline_digest = baseline_manifest = baseline_bytes = None
	if previous_inventory or previous_compatibility:
		if not previous_inventory or not previous_compatibility:
			raise ValueError("previous inventory and compatibility manifest must be provided together")
		baseline_bytes = Path(previous_inventory).read_bytes()
		baseline = json.loads(baseline_bytes)
		baseline_manifest = load_compatibility(Path(previous_compatibility), allow_legacy_baseline=True)
		baseline_digest = baseline_manifest.get("inventory_digest")
		if not baseline_digest:
			raise ValueError("baseline compatibility manifest has no inventory_digest")

	report = build_report(
		inventory,
		provenance,
		manifest,
		baseline,
		baseline_digest,
		baseline_manifest,
		baseline_bytes,
	)
	human_report = generate_human_report(report).encode()
	artifacts = {
		"inventory_report.json": canonical_json(report),
		"inventory_report.md": human_report,
		"provenance.json": canonical_json(provenance),
		"release_inventory.json": canonical_json(inventory),
	}
	artifact_sha256 = {name: hashlib.sha256(content).hexdigest() for name, content in artifacts.items()}
	new_inventory_digest = artifact_sha256["release_inventory.json"]
	artifacts.update(_quality_artifacts_for_inventory(manifest, new_inventory_digest))
	manifest = {
		**manifest,
		"artifact_sha256": artifact_sha256,
		"inventory_digest": new_inventory_digest,
	}
	if manifest["inventory_digest"] != report["inventory_digest"]:
		raise ValueError("release inventory bytes do not match the machine report digest")
	artifacts["compatibility.json"] = canonical_json(manifest)
	for name, content in artifacts.items():
		if content.startswith(b"\xef\xbb\xbf") or not content.endswith(b"\n") or b"\r\n" in content:
			raise ValueError(f"artifact {name} is not canonical UTF-8/LF")
	target = Path(output_dir) if output_dir else Path(__file__).parent
	write_artifacts(target, artifacts)
	return {
		"artifact_paths": {name: str(target / name) for name in sorted(artifacts)},
		"inventory_digest": report["inventory_digest"],
		"summary": report["summary"],
	}
