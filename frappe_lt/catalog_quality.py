import hashlib
import json
import math
import os
import re
import tempfile
import time
import unicodedata
from collections import Counter, deque
from pathlib import Path

import html5lib
from html5lib.constants import tokenTypes

from frappe_lt.inventory import QUALITY_GATE_ARTIFACTS, canonical_json, validate_compatibility
from frappe_lt.po import build_po, compile_po, parse_po

SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_CANDIDATES = 1_000
MAX_SELECTORS = 10_000
MAX_FORMS = 100_000
MAX_NORMALIZED_GLOSSARY_CHARS = 500_000
MAX_GLOSSARY_MATCHES = 500_000
MAX_ERRORS = 500_000
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_STRING_BYTES = 256 * 1024
QUALITY_ARTIFACTS = QUALITY_GATE_ARTIFACTS
TOKEN_PATTERNS = (
	("javascript", re.compile(r"\$\{[A-Za-z_$][\w.$]*(?:\[[^\]\r\n{}]+\])?}")),
	("python", re.compile(r"%\([A-Za-z_]\w*\)[#0+ \-]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa](?!\w)")),
	(
		"printf",
		re.compile(
			r"%(?:"
			r"\d+\$[#0+ \-']*(?:\d+|\*\d+\$)?(?:\.(?:\d+|\*\d+\$)?)?|"
			r"[#0+ \-']*(?:\d+|\*)?(?:\.(?:\d+|\*)?)?"
			r")[diouxXeEfFgGcrsa](?!\w)"
		),
	),
	(
		"brace",
		re.compile(r"{(?:|\d+|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[[^\]\r\n{}]+\])*(?:![rsa])?)(?::[^{}\r\n]+)?}"),
	),
	("dollar", re.compile(r"\$[A-Za-z_]\w*")),
)
SAFE_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")
DIGEST = re.compile(r"[0-9a-f]{64}")
TRANSLATABLE_ATTRIBUTES = {"alt", "aria-label", "placeholder", "title"}
REFERENCE = re.compile(
	r"(?<![\w])(?:"
	r"[A-Za-z][A-Za-z0-9+.-]*:(?://)?[^\s<>\"']+|"
	r"//[^\s<>\"']+|"
	r"(?:\.\.?/|/)[^\s<>\"']*|"
	r"(?:[\w.~!$&()*+,;=:@%-]+/)+[\w.~!$&()*+,;=:@%-]+(?:[?#][^\s<>\"']*)?|"
	r"[?#][^\s<>\"']+"
	r")"
)
HTML_NEWLINE_SENTINELS = {"\r\n": "\ue000", "\r": "\ue001", "\n": "\ue002"}
HTML_NEWLINE_SENTINEL_CODEPOINTS = {ord(value) for value in HTML_NEWLINE_SENTINELS.values()}
NUMERIC_CHARACTER_REFERENCE = re.compile(r"&#(?:[xX]([0-9a-fA-F]+)|([0-9]+));?")
REQUIRED_COLLISION_LOCATORS = {
	"CONTEXT.md#saskaita-account-contextless-collision": {
		"erpnext:doctype:Account:name",
		"frappe:doctype:Email Account:field:account_section:label",
	}
}
VOID_ELEMENTS = {
	"area",
	"base",
	"br",
	"col",
	"embed",
	"hr",
	"img",
	"input",
	"link",
	"meta",
	"param",
	"source",
	"track",
	"wbr",
}


def _read_bytes(path: Path, budget: list[int] | None = None) -> bytes:
	if path.stat().st_size > MAX_ARTIFACT_BYTES:
		raise ValueError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes: {path.name}")
	content = path.read_bytes()
	if budget is not None:
		budget[0] += len(content)
		if budget[0] > MAX_TOTAL_ARTIFACT_BYTES:
			raise ValueError(f"authenticated artifacts exceed {MAX_TOTAL_ARTIFACT_BYTES} aggregate bytes")
	return content


def _validate_input_limits(value, label: str) -> None:
	if isinstance(value, str):
		if len(value.encode()) > MAX_STRING_BYTES:
			raise ValueError(f"{label} contains a string exceeding {MAX_STRING_BYTES} bytes")
	elif isinstance(value, dict):
		for key, child in value.items():
			_validate_input_limits(key, label)
			_validate_input_limits(child, label)
	elif isinstance(value, list):
		if len(value) > MAX_ENTRIES:
			raise ValueError(f"{label} contains more than {MAX_ENTRIES} entries")
		for child in value:
			_validate_input_limits(child, label)
	elif isinstance(value, float) and not math.isfinite(value):
		raise ValueError(f"{label} contains a non-finite number")


def _object(pairs):
	value = {}
	for key, child in pairs:
		if key in value:
			raise ValueError(f"duplicate JSON object member {key!r}")
		value[key] = child
	return value


def _read_json(path: Path, budget: list[int] | None = None) -> tuple[dict, bytes]:
	content = _read_bytes(path, budget)
	value = json.loads(
		content,
		object_pairs_hook=_object,
		parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON number {value}")),
	)
	if not isinstance(value, dict):
		raise ValueError(f"{path.name} must contain a JSON object")
	_validate_input_limits(value, path.name)
	return value, content


def _digest(content: bytes) -> str:
	return hashlib.sha256(content).hexdigest()


def _replace_bytes(
	path: Path,
	content: bytes,
	*,
	write=lambda stream, value: stream.write(value),
	fsync=os.fsync,
	replace=os.replace,
	remove=lambda path: path.unlink(missing_ok=True),
) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	previous = path.read_bytes() if path.is_file() else None
	fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
	temporary = Path(temporary_name)
	replaced = False
	try:
		with os.fdopen(fd, "wb") as stream:
			write(stream, content)
			stream.flush()
			fsync(stream.fileno())
		replace(temporary, path)
		replaced = True
		directory_fd = os.open(path.parent, os.O_RDONLY)
		try:
			fsync(directory_fd)
		finally:
			os.close(directory_fd)
	except Exception:
		if replaced:
			try:
				if previous is None:
					remove(path)
				else:
					rollback_fd, rollback_name = tempfile.mkstemp(
						prefix=f".{path.name}.rollback.", suffix=".tmp", dir=path.parent
					)
					rollback = Path(rollback_name)
					try:
						with os.fdopen(rollback_fd, "wb") as stream:
							write(stream, previous)
							stream.flush()
							fsync(stream.fileno())
						replace(rollback, path)
					finally:
						rollback.unlink(missing_ok=True)
				directory_fd = os.open(path.parent, os.O_RDONLY)
				try:
					fsync(directory_fd)
				finally:
					os.close(directory_fd)
			except Exception as rollback_error:
				raise RuntimeError(f"publication rollback failed: {rollback_error}") from rollback_error
		raise
	finally:
		temporary.unlink(missing_ok=True)


def _safe_path(root: Path, name: str) -> Path:
	if (
		not isinstance(name, str)
		or not name
		or "\\" in name
		or Path(name).is_absolute()
		or any(part in {"", ".", ".."} for part in Path(name).parts)
	):
		raise ValueError(f"invalid artifact path {name!r}")
	root = root.resolve()
	lexical = root / name
	for parent in (lexical, *lexical.parents):
		if parent == root.parent:
			break
		if parent.is_symlink():
			raise ValueError(f"artifact path uses a symlink: {name}")
	path = lexical.resolve()
	if path.parent != root and root not in path.parents:
		raise ValueError(f"artifact path escapes the trust root: {name}")
	return path


def _key_tuple(key: dict) -> tuple[str, str | None]:
	if not isinstance(key, dict) or set(key) != {"source", "context"}:
		raise ValueError(f"invalid Translation Key {key!r}")
	source = key["source"]
	context = key["context"]
	if not isinstance(source, str) or not source:
		raise ValueError("Translation Key source must be nonempty text")
	if context is not None and (not isinstance(context, str) or not context):
		raise ValueError("Translation Key context must be null or nonempty text")
	return source, context


def _require_digest(value, label: str) -> str:
	if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
		raise ValueError(f"{label} must be a SHA-256 digest")
	return value


def _reviewed(record: dict, label: str) -> None:
	if record.get("reviewed") is not True:
		raise ValueError(f"{label} must be reviewed")
	review = record.get("review")
	if not isinstance(review, str) or not review.strip() or review != review.strip():
		raise ValueError(f"{label} review evidence must be normalized nonblank text")


def _validate_inventory(inventory: dict) -> dict[tuple[str, str | None], dict]:
	if inventory.get("schema_version") != 1 or not isinstance(inventory.get("entries"), list):
		raise ValueError("invalid Release Inventory schema")
	entries = {}
	for entry in inventory["entries"]:
		if not isinstance(entry, dict):
			raise ValueError("Release Inventory entries must be objects")
		key = _key_tuple(entry.get("key"))
		if key in entries:
			raise ValueError(f"Release Inventory contains duplicate key {key!r}")
		_require_digest(entry.get("source_digest"), f"Release Inventory source digest for {key!r}")
		if not isinstance(entry.get("source_locations"), list) or not isinstance(
			entry.get("stable_locators"), list
		):
			raise ValueError(f"Release Inventory locators are invalid for {key!r}")
		entries[key] = entry
	return entries


def _validate_destination(path: Path, label: str, suffix: str) -> Path:
	path = Path(path)
	if path.suffix.lower() != suffix:
		raise ValueError(f"{label} destination must have a {suffix} suffix")
	absolute = path.absolute()
	for component in (absolute, *absolute.parents):
		if component.is_symlink():
			raise ValueError(f"{label} destination must not use symlinks")
	resolved = absolute.resolve()
	active_locale = Path(__file__).with_name("locale").resolve()
	if resolved == active_locale or active_locale in resolved.parents:
		raise ValueError(f"{label} destination must not be inside the active application locale")
	if resolved.suffix.lower() == ".mo":
		raise ValueError(f"{label} destination must not be an active MO")
	for active in active_locale.glob("*"):
		if active.is_file() and resolved.exists() and os.path.samefile(resolved, active):
			raise ValueError(f"{label} destination aliases an active locale artifact")
	return resolved


def _validate_destinations(output_path: Path, report_path: Path) -> tuple[Path, Path]:
	report = _validate_destination(report_path, "report", ".json")
	output = _validate_destination(output_path, "candidate", ".po")
	if output == report or (output.exists() and report.exists() and os.path.samefile(output, report)):
		raise ValueError("candidate and report destinations must differ")
	return output, report


def _load_trusted(
	candidate_name: str | None, compatibility_path: Path
) -> tuple[dict, dict, dict | None, dict[str, dict]]:
	root = compatibility_path.parent
	budget = [0]
	compatibility, _ = _read_json(compatibility_path, budget)
	validate_compatibility(compatibility)
	owned_digests = compatibility.get("artifact_sha256")
	for name, expected in owned_digests.items():
		_require_digest(expected, f"authenticated artifact digest for {name!r}")
		content = _read_bytes(_safe_path(root, name), budget)
		if _digest(content) != expected:
			raise ValueError(f"artifact digest mismatch for {name}")
	quality = compatibility.get("quality_gate")
	if not isinstance(quality, dict) or quality.get("schema_version") != SCHEMA_VERSION:
		raise ValueError("unsupported Catalog Quality Gate schema")
	digests = quality.get("artifact_sha256")
	if not isinstance(digests, dict) or set(digests) != set(QUALITY_ARTIFACTS):
		raise ValueError("Catalog Quality Gate artifacts are not fully authenticated")
	artifacts = {}
	for name, expected in digests.items():
		_require_digest(expected, f"Catalog Quality Gate digest for {name!r}")
		artifact, content = _read_json(_safe_path(root, name), budget)
		if _digest(content) != expected:
			raise ValueError(f"artifact digest mismatch for {name}")
		if artifact.get("schema_version") != SCHEMA_VERSION:
			raise ValueError(f"unsupported schema for {name}")
		artifacts[name] = artifact

	inventory, inventory_bytes = _read_json(root / "release_inventory.json", budget)
	inventory_digest = _digest(inventory_bytes)
	inventory_entries = _validate_inventory(inventory)
	if compatibility.get("inventory_digest") != inventory_digest:
		raise ValueError("Release Inventory digest mismatch")
	for name, artifact in artifacts.items():
		if artifact.get("inventory_digest") != inventory_digest:
			raise ValueError(f"{name} is stale for the authenticated Release Inventory")
	registry = artifacts["catalog_segments.json"]
	records = registry.get("candidates")
	if not isinstance(records, list):
		raise ValueError("Catalog Segment registry candidates must be a list")
	if len(records) > MAX_CANDIDATES:
		raise ValueError(f"Catalog Segment registry exceeds {MAX_CANDIDATES} candidates")
	if not all(isinstance(record, dict) for record in records):
		raise ValueError("Catalog Segment registry candidates must be objects")
	if any(
		not isinstance(record.get("name"), str) or not SAFE_SLUG.fullmatch(record["name"])
		for record in records
	):
		raise ValueError("Catalog Segment candidate names must be safe short slugs")
	if len({record["name"] for record in records}) != len(records):
		raise ValueError("Catalog Segment registry contains duplicate candidate names")
	candidate_paths = [_safe_path(root, record.get("candidate")) for record in records]
	if len(set(candidate_paths)) != len(candidate_paths):
		raise ValueError("Catalog Segment registry contains duplicate or aliased candidate paths")
	manifests = {}
	claimed_keys = {}
	registered_candidates_by_name = {}
	candidate_directory = registry.get("candidate_directory")
	directory = _safe_path(root, candidate_directory) if candidate_directory is not None else None
	if directory is not None and any(path.parent != directory for path in candidate_paths):
		raise ValueError("registered candidate paths must be directly inside candidate_directory")
	for registered, candidate_path in zip(records, candidate_paths, strict=True):
		registered_candidate, registered_bytes = _read_json(candidate_path, budget)
		_require_digest(registered.get("candidate_sha256"), "registered candidate digest")
		if _digest(registered_bytes) != registered["candidate_sha256"]:
			raise ValueError(f"candidate digest mismatch for {registered.get('name')!r}")
		if registered_candidate.get("schema_version") != SCHEMA_VERSION:
			raise ValueError(f"unsupported candidate schema for {registered.get('name')!r}")
		if registered_candidate.get("provenance_schema_version") != SCHEMA_VERSION:
			raise ValueError(f"unsupported candidate provenance schema for {registered.get('name')!r}")
		candidate_entries = registered_candidate.get("entries")
		if not isinstance(candidate_entries, list):
			raise ValueError(f"candidate entries must be a list for {registered.get('name')!r}")
		for candidate_entry in candidate_entries:
			if not isinstance(candidate_entry, dict):
				raise ValueError("candidate entries must be objects")
			_key_tuple(candidate_entry.get("key"))
			_require_digest(candidate_entry.get("source_digest"), "candidate source digest")
			if not isinstance(candidate_entry.get("translation"), str) or not isinstance(
				candidate_entry.get("flags"), list
			):
				raise ValueError("candidate translation and flags have invalid types")
			provenance = candidate_entry.get("provenance")
			if (
				not isinstance(provenance, dict)
				or provenance.get("origin")
				not in {"inherited_v15", "new_ai", "corrected_inherited", "approved_unchanged_exception"}
				or provenance.get("review_status") != "reviewed"
				or not isinstance(provenance.get("review"), str)
				or not provenance["review"].strip()
				or provenance["review"] != provenance["review"].strip()
			):
				raise ValueError(
					f"candidate contains unauthenticated provenance for {registered.get('name')!r}"
				)
		if registered_candidate.get("inventory_digest") != inventory_digest:
			raise ValueError(f"candidate {registered.get('name')!r} is stale")
		registered_candidates_by_name[registered["name"]] = registered_candidate
		manifest_name = registered.get("manifest")
		if manifest_name is None:
			if registered.get("manifest_sha256") is not None:
				raise ValueError("whole-catalog candidate must not have a manifest digest")
			manifests[registered["name"]] = None
			continue
		manifest, manifest_bytes = _read_json(_safe_path(root, manifest_name), budget)
		_require_digest(registered.get("manifest_sha256"), "segment manifest digest")
		if _digest(manifest_bytes) != registered["manifest_sha256"]:
			raise ValueError(f"segment manifest digest mismatch for {registered.get('name')!r}")
		if manifest.get("schema_version") != SCHEMA_VERSION:
			raise ValueError(f"unsupported segment manifest schema for {registered.get('name')!r}")
		if manifest.get("inventory_digest") != inventory_digest:
			raise ValueError(f"segment manifest {registered.get('name')!r} is stale")
		keys = manifest.get("keys")
		if not isinstance(keys, list):
			raise ValueError("segment manifest keys must be a list")
		seen = set()
		for selected in keys:
			if not isinstance(selected, dict):
				raise ValueError("segment selectors must be objects")
			key = _key_tuple(selected.get("key", {}))
			if key in seen:
				raise ValueError(f"segment {registered.get('name')!r} contains duplicate keys")
			seen.add(key)
			if key not in inventory_entries:
				raise ValueError(f"segment contains a key outside the Release Inventory: {key!r}")
			_require_digest(selected.get("source_digest"), "segment source digest")
			if selected["source_digest"] != inventory_entries[key]["source_digest"]:
				raise ValueError(f"segment source digest mismatch for {key!r}")
			if key in claimed_keys:
				raise ValueError(
					f"Catalog Segments {claimed_keys[key]!r} and {registered.get('name')!r} overlap"
				)
			claimed_keys[key] = registered.get("name")
		manifests[registered["name"]] = manifest
	if directory is not None:
		registered_paths = set(candidate_paths)
		unregistered = sorted(
			path.relative_to(directory).as_posix()
			for path in directory.rglob("*.json")
			if path not in registered_paths
		)
		if unregistered:
			raise ValueError(f"unregistered candidates: {', '.join(unregistered)}")
	if candidate_name is None:
		_validate_quality_records(artifacts, inventory_entries)
		return inventory, {}, None, artifacts
	matches = [record for record in records if record.get("name") == candidate_name]
	if len(matches) != 1:
		raise ValueError(f"candidate {candidate_name!r} is not uniquely registered")
	candidate = registered_candidates_by_name[candidate_name]
	_validate_quality_records(artifacts, inventory_entries)
	return inventory, candidate, manifests[candidate_name], artifacts


def registered_candidates(compatibility_path: Path | None = None) -> list[str]:
	"""Authenticate the complete registry and return its stable candidate order."""
	compatibility_path = compatibility_path or Path(__file__).with_name("compatibility.json")
	_inventory, _candidate, _segment, artifacts = _load_trusted(None, compatibility_path)
	return sorted(record["name"] for record in artifacts["catalog_segments.json"]["candidates"])


def _validate_quality_records(artifacts: dict[str, dict], inventory: dict) -> None:
	exceptions = artifacts["translation_exceptions.json"].get("entries")
	resolutions = artifacts["collision_resolutions.json"].get("entries")
	selectors = artifacts["glossary_selectors.json"].get("entries")
	if not all(isinstance(value, list) for value in (exceptions, resolutions, selectors)):
		raise ValueError("quality artifact entries must be lists")
	if len(selectors) > MAX_SELECTORS:
		raise ValueError(f"Glossary Selector entries exceed {MAX_SELECTORS}")
	seen_exceptions = set()
	for exception in exceptions:
		if not isinstance(exception, dict):
			raise ValueError("Translation Exception entries must be objects")
		_reviewed(exception, "Translation Exception")
		key = _key_tuple(exception.get("key"))
		if key in seen_exceptions:
			raise ValueError(f"duplicate Translation Exception for {key!r}")
		seen_exceptions.add(key)
		if key not in inventory or exception.get("source_digest") != inventory[key]["source_digest"]:
			raise ValueError(f"stale Translation Exception for {key!r}")
		_require_digest(exception.get("source_digest"), "Translation Exception source digest")
	seen_resolutions = set()
	for resolution in resolutions:
		if not isinstance(resolution, dict):
			raise ValueError("collision resolutions must be objects")
		_reviewed(resolution, "collision resolution")
		key = _key_tuple(resolution.get("key"))
		if key in seen_resolutions:
			raise ValueError(f"duplicate collision resolution for {key!r}")
		seen_resolutions.add(key)
		entry = inventory.get(key)
		if entry is None or resolution.get("source_digest") != entry["source_digest"]:
			raise ValueError(f"stale collision resolution for {key!r}")
		_require_digest(resolution.get("source_digest"), "collision source digest")
		if not isinstance(resolution.get("translation"), str) or not resolution["translation"].strip():
			raise ValueError("collision translation must be nonblank")
		locators = resolution.get("stable_locators")
		required_locators = REQUIRED_COLLISION_LOCATORS.get(entry.get("context_decision"))
		if required_locators is not None and (
			not isinstance(locators, list) or set(locators) != required_locators
		):
			raise ValueError(f"collision locator set must exactly match for {key!r}")
	seen_ids = set()
	form_count = 0
	normalized_form_chars = 0
	context = Path(__file__).parent.parent / "CONTEXT.md"
	context_text = context.read_text(encoding="utf-8")
	for selector in selectors:
		if not isinstance(selector, dict):
			raise ValueError("Glossary Selector entries must be objects")
		_reviewed(selector, "Glossary Selector")
		selector_id = selector.get("id")
		if not isinstance(selector_id, str) or not selector_id.strip() or selector_id != selector_id.strip():
			raise ValueError("Glossary Selector id must be normalized nonblank text")
		if selector_id in seen_ids:
			raise ValueError("Glossary Selector ids must be unique")
		seen_ids.add(selector_id)
		glossary_id = selector.get("glossary_id")
		if not isinstance(glossary_id, str) or not glossary_id.startswith("CONTEXT.md#"):
			raise ValueError("Glossary Selector glossary_id must reference CONTEXT.md")
		anchor = glossary_id.removeprefix("CONTEXT.md#")
		if f'<a id="{anchor}"></a>' not in context_text:
			raise ValueError(f"unknown stable glossary anchor {glossary_id!r}")
		key = _key_tuple(selector.get("key"))
		entry = inventory.get(key)
		if (
			entry is None
			or selector.get("source") != key[0]
			or selector.get("context") != key[1]
			or selector.get("source_digest") != entry["source_digest"]
		):
			raise ValueError(f"stale or mismatched Glossary Selector {selector_id!r}")
		_require_digest(selector.get("source_digest"), "Glossary Selector source digest")
		locators = selector.get("stable_locators")
		if locators is not None and (
			not isinstance(locators, list) or set(locators) != set(entry.get("stable_locators", []))
		):
			raise ValueError(f"Glossary Selector locators must exactly match for {selector_id!r}")
		accepted = selector.get("accepted_forms")
		forbidden = selector.get("forbidden_forms")
		if (
			not isinstance(accepted, list)
			or not isinstance(forbidden, list)
			or not accepted
			or selector.get("unicode_normalization") not in {"none", "NFC", "NFD", "NFKC", "NFKD"}
			or not isinstance(selector.get("case_sensitive"), bool)
			or any("regex" in field.lower() for field in selector)
			or not all(isinstance(form, str) and form.strip() for form in accepted + forbidden)
		):
			raise ValueError("invalid reviewed Glossary Selector")
		form_count += len(accepted) + len(forbidden)
		policy = (selector["case_sensitive"], selector["unicode_normalization"])
		normalized_form_chars += sum(len(_normalize_literal(form, *policy)) for form in accepted + forbidden)
		if normalized_form_chars > MAX_NORMALIZED_GLOSSARY_CHARS:
			raise ValueError(
				"Glossary Selector normalized forms exceed "
				f"{MAX_NORMALIZED_GLOSSARY_CHARS} aggregate characters"
			)
	if form_count > MAX_FORMS:
		raise ValueError(f"Glossary Selector forms exceed {MAX_FORMS}")


def _token_at(value: str, index: int) -> tuple[str | None, int, bool]:
	for prefix, closing in (("{{", "}}"), ("{%", "%}"), ("{#", "#}")):
		if value.startswith(prefix, index):
			end = value.find(closing, index + 2)
			return ("jinja", end + 2, False) if end >= 0 else (None, len(value), True)
	for prefix in ("http://", "https://", "ftp://", "mailto:"):
		if value.startswith(prefix, index):
			end = index + len(prefix)
			while end < len(value) and value[end] not in "\t\r\n <>\"'":
				end += 1
			return ("url", end, end == index + len(prefix))
	for family, pattern in TOKEN_PATTERNS:
		match = pattern.match(value, index)
		if match:
			return family, match.end(), False
	character = value[index]
	if value.startswith(("}}", "%}", "#}"), index):
		return None, index + 2, True
	if value.startswith("%(", index) or value.startswith("${", index):
		return None, index + 2, True
	if character == "%" and index + 1 < len(value) and value[index + 1].isalpha():
		return None, index + 2, True
	if character == "%" and re.match(
		r"%(?:[#0+\-'.*]|\d+\$|\d+(?:\.\d*)?[A-Za-z](?!\w)| +[A-Za-z](?!\w))",
		value[index:],
	):
		return None, index + 1, True
	if (
		character == "{"
		and index + 1 < len(value)
		and (value[index + 1].isalnum() or value[index + 1] == "_")
	):
		return None, index + 2, True
	if character == "$" and index + 1 < len(value) and value[index + 1].isdigit():
		return None, index + 2, True
	return None, index + 1, False


def _tokens(value: str) -> tuple[Counter, bool]:
	tokens = Counter()
	unknown = False
	plain_braces = 0
	index = 0
	while index < len(value):
		if value.startswith(("%%", "$$"), index):
			tokens[("escaped", value[index : index + 2])] += 1
			index += 2
			continue
		if value[index] == "\\" and index + 1 < len(value):
			family, end, malformed = _token_at(value, index + 1)
			if family is not None and not malformed:
				tokens[("escaped", value[index:end])] += 1
				index = end
				continue
			if value[index + 1] in "{}%$#":
				tokens[("escaped", value[index : index + 2])] += 1
				index += 2
				continue
		family, end, malformed = _token_at(value, index)
		if family is not None:
			tokens[(family, value[index:end])] += 1
		elif not malformed and value[index] == "{":
			plain_braces += 1
		elif not malformed and value[index] == "}":
			if plain_braces:
				plain_braces -= 1
			else:
				malformed = True
		unknown = unknown or malformed
		index = end
	return tokens, unknown or plain_braces > 0


def _whitespace_signature(value: str) -> tuple:
	leading = re.match(r"[ \t\r\n]*", value).group()
	trailing = re.search(r"[ \t\r\n]*$", value).group()
	end = len(value) - len(trailing) if trailing else len(value)
	inside = value[len(leading) : end]
	significant_inside = tuple(match.group() for match in re.finditer(r"\r\n|\r|\n|\t| {2,}", inside))
	return leading, significant_inside, trailing


class _RecordingHTMLParser(html5lib.HTMLParser):
	def __init__(self):
		super().__init__(tree=html5lib.getTreeBuilder("etree"), strict=False, namespaceHTMLElements=False)
		self.raw_tokens = []

	def mainLoop(self):
		class RecordingTokenStream:
			def __init__(stream, tokenizer):
				stream.tokenizer = iter(tokenizer)
				stream.stream = tokenizer.stream

			def __iter__(stream):
				return stream

			def __next__(stream):
				token = next(stream.tokenizer)
				if token["type"] in {tokenTypes["StartTag"], tokenTypes["EndTag"]}:
					self.raw_tokens.append((token["type"], token["name"], token.get("selfClosing", False)))
				return token

		self.tokenizer = RecordingTokenStream(self.tokenizer)
		return super().mainLoop()


class _HTMLFragment:
	def __init__(self, root=None, error=None):
		self.root = root
		self.error = error


def _has_reserved_newline_sentinel(value: str) -> bool:
	if any(sentinel in value for sentinel in HTML_NEWLINE_SENTINELS.values()):
		return True
	for match in NUMERIC_CHARACTER_REFERENCE.finditer(value):
		codepoint = int(match.group(1), 16) if match.group(1) is not None else int(match.group(2))
		if codepoint in HTML_NEWLINE_SENTINEL_CODEPOINTS:
			return True
	return False


def _encode_html_newlines(value: str) -> str:
	def encode(content: str) -> str:
		return re.sub(r"\r\n|\r|\n", lambda match: HTML_NEWLINE_SENTINELS[match.group()], content)

	result = []
	text_start = 0
	index = 0
	while index < len(value):
		if value.startswith("<!--", index):
			result.append(encode(value[text_start:index]))
			end = value.find("-->", index + 4)
			if end < 0:
				result.extend(("<!--", encode(value[index + 4 :])))
				return "".join(result)
			result.extend(("<!--", encode(value[index + 4 : end]), "-->"))
			index = end + 3
			text_start = index
			continue

		if value[index] != "<":
			index += 1
			continue
		marker = index + 1
		if marker < len(value) and value[marker] == "/":
			marker += 1
		if marker >= len(value) or not (value[marker].isalpha() or value[marker] in "!?"):
			index += 1
			continue

		result.append(encode(value[text_start:index]))
		tag = []
		quote = None
		cursor = index
		while cursor < len(value):
			character = value[cursor]
			if quote is not None and character in "\r\n":
				newline = "\r\n" if value.startswith("\r\n", cursor) else character
				tag.append(HTML_NEWLINE_SENTINELS[newline])
				cursor += len(newline)
				continue
			tag.append(character)
			cursor += 1
			if quote is not None:
				if character == quote:
					quote = None
			elif character in "\"'":
				quote = character
			elif character == ">":
				break
		result.append("".join(tag))
		index = cursor
		text_start = index
	result.append(encode(value[text_start:]))
	return "".join(result)


def _decode_html_newlines(value: str | None) -> str | None:
	if value is None:
		return None
	for newline, sentinel in HTML_NEWLINE_SENTINELS.items():
		value = value.replace(sentinel, newline)
	return value


def _decode_tree_newlines(root) -> None:
	for node in root.iter():
		node.text = _decode_html_newlines(node.text)
		node.tail = _decode_html_newlines(node.tail)
		for name, value in node.attrib.items():
			node.attrib[name] = _decode_html_newlines(value)


def _parse_html(value: str) -> _HTMLFragment:
	try:
		if _has_reserved_newline_sentinel(value):
			return _HTMLFragment(error="reserved newline sentinel in HTML")
		parser = _RecordingHTMLParser()
		root = parser.parseFragment(_encode_html_newlines(value))
		if parser.errors:
			return _HTMLFragment(error="; ".join(error[1] for error in parser.errors))
		stack = []
		for token_type, tag, self_closing in parser.raw_tokens:
			if token_type == tokenTypes["StartTag"]:
				if tag not in VOID_ELEMENTS and not self_closing:
					stack.append(tag)
			elif tag in VOID_ELEMENTS or not stack or stack[-1] != tag:
				return _HTMLFragment(error=f"unexpected closing tag </{tag}>")
			else:
				stack.pop()
		if stack:
			return _HTMLFragment(error=f"unclosed tag <{stack[-1]}>")
		_decode_tree_newlines(root)
		return _HTMLFragment(root=root)
	except (ValueError, AssertionError, TypeError) as error:
		return _HTMLFragment(error=str(error))


def _contains_html(value: str) -> bool:
	for index, character in enumerate(value):
		if (
			character == "&"
			and index + 1 < len(value)
			and (value[index + 1].isalnum() or value[index + 1] == "#")
		):
			cursor = index + 2
			while cursor < len(value) and value[cursor].isalnum():
				cursor += 1
			if cursor < len(value) and value[cursor] == ";":
				return True
		if character != "<":
			continue
		cursor = index + 1
		if cursor < len(value) and value[cursor] == "/":
			cursor += 1
		while cursor < len(value) and value[cursor].isspace():
			cursor += 1
		if cursor < len(value) and (value[cursor].isalpha() or value[cursor] in "!?"):
			return True
	return False


def _references(value: str) -> Counter:
	return Counter(match.group().rstrip(".,;:!)]}") for match in REFERENCE.finditer(value))


def _element_content(node):
	if node.text:
		yield node.text
	for child in node:
		if isinstance(child.tag, str):
			yield child
		else:
			yield f"<!--{child.text or ''}-->"
		if child.tail:
			yield child.tail


def _node_signature(node, mode: str) -> tuple:
	content = []
	for item in _element_content(node):
		if isinstance(item, str):
			content.append(("text", _whitespace_signature(item) if mode == "whitespace" else None))
		else:
			content.append(("node", _node_signature(item, mode)))
	if mode == "attributes":
		attrs = []
		for name, value in node.attrib.items():
			value = value or ""
			if name in TRANSLATABLE_ATTRIBUTES:
				value = (
					"translated",
					_whitespace_signature(value),
					tuple(sorted(_references(value).items())),
				)
			attrs.append((name, value))
	else:
		attrs = ()
	return node.tag, tuple(sorted(attrs)), tuple(content)


def _fragment_signature(fragment: _HTMLFragment, mode: str) -> tuple:
	leading = []
	branches = []
	current = None
	for item in _element_content(fragment.root):
		if isinstance(item, str):
			value = _whitespace_signature(item) if mode == "whitespace" else None
			if current is None:
				leading.append(value)
			else:
				current[1].append(value)
		else:
			current = [_node_signature(item, mode), []]
			branches.append(current)
	return tuple(leading), Counter((branch[0], tuple(branch[1])) for branch in branches)


def _html_errors(source: str, translation: str, parse_html=_parse_html) -> list[str]:
	if not _contains_html(source) and not _contains_html(translation):
		return (
			[]
			if _whitespace_signature(source) == _whitespace_signature(translation)
			else ["SIGNIFICANT_WHITESPACE_MISMATCH"]
		)
	source_fragment = parse_html(source)
	if source_fragment.error:
		return ["HTML_SOURCE_INVALID"]
	translation_fragment = parse_html(translation)
	if translation_fragment.error:
		return ["HTML_TRANSLATION_INVALID"]
	if _fragment_signature(source_fragment, "structure") != _fragment_signature(
		translation_fragment, "structure"
	):
		return ["HTML_EQUIVALENCE_MISMATCH"]
	errors = []
	if _fragment_signature(source_fragment, "attributes") != _fragment_signature(
		translation_fragment, "attributes"
	):
		errors.append("HTML_ATTRIBUTE_MISMATCH")
	if _fragment_signature(source_fragment, "whitespace") != _fragment_signature(
		translation_fragment, "whitespace"
	):
		errors.append("SIGNIFICANT_WHITESPACE_MISMATCH")
	return errors


def _error(
	code: str,
	key: tuple[str, str | None] = ("", None),
	entry: dict | None = None,
	selector_id: str | None = None,
) -> dict:
	entry = entry or {}
	locations = entry.get("source_locations", [])
	source_digest = entry.get("source_digest")
	sort_key = [
		key[0] or "",
		key[1] or "",
		code,
		selector_id or "",
		source_digest or "",
		canonical_json(locations).decode(),
	]
	result = {
		"code": code,
		"frappe_context": key[1],
		"sort_key": sort_key,
		"source_digest": source_digest,
		"source_locations": locations,
		"translation_key": {"source": key[0], "context": key[1]},
	}
	if selector_id is not None:
		result["selector_id"] = selector_id
	return result


def _result(
	candidate_name: str,
	started: float,
	clock,
	key_count: int,
	errors: list[dict],
	notices: list[dict] | None = None,
) -> dict:
	notices = notices or []
	if len(errors) + len(notices) > MAX_ERRORS and not (
		len(errors) == 1 and errors[0].get("code") == "UNTRUSTED_INPUT_OR_TOOL_FAILURE"
	):
		raise ValueError(f"quality findings exceed {MAX_ERRORS}")
	errors.sort(key=lambda item: item["sort_key"])
	notices.sort(key=lambda item: item["sort_key"])
	return {
		"candidate": candidate_name,
		"duration_seconds": clock() - started,
		"errors": errors,
		"exit_code": 1 if errors else 0,
		"notices": notices,
		"schema_version": SCHEMA_VERSION,
		"summary": {"errors": len(errors), "keys": key_count, "notices": len(notices)},
	}


def _report_bytes(result: dict) -> bytes:
	content = canonical_json(result)
	if len(content) > MAX_REPORT_BYTES:
		raise ValueError(f"quality report exceeds {MAX_REPORT_BYTES} bytes")
	return content


def _normalize_literal(value: str, case_sensitive: bool, normalization: str) -> str:
	if normalization != "none":
		value = unicodedata.normalize(normalization, value)
	if not case_sensitive:
		value = value.casefold()
	return value


class _LiteralIndex:
	def __init__(self, forms: dict[str, set[tuple[str, str]]]):
		if sum(map(len, forms)) > MAX_NORMALIZED_GLOSSARY_CHARS:
			raise ValueError(
				"Glossary Selector normalized forms exceed "
				f"{MAX_NORMALIZED_GLOSSARY_CHARS} aggregate characters"
			)
		self.transitions = [{}]
		self.failures = [0]
		self.outputs = [set()]
		self.output_links = [0]
		for form, selector_ids in forms.items():
			state = 0
			for character in form:
				state = self.transitions[state].setdefault(character, len(self.transitions))
				if state == len(self.transitions):
					self.transitions.append({})
					self.failures.append(0)
					self.outputs.append(set())
					self.output_links.append(0)
			self.outputs[state].update(selector_ids)
		queue = deque(self.transitions[0].values())
		while queue:
			state = queue.popleft()
			for character, target in self.transitions[state].items():
				queue.append(target)
				fallback = self.failures[state]
				while fallback and character not in self.transitions[fallback]:
					fallback = self.failures[fallback]
				self.failures[target] = self.transitions[fallback].get(character, 0)
				failure = self.failures[target]
				self.output_links[target] = failure if self.outputs[failure] else self.output_links[failure]

	def matches(self, value: str, budget: list[int]) -> set[tuple[str, str]]:
		matches = set()
		seen_terminals = set()
		state = 0
		for character in value:
			while state and character not in self.transitions[state]:
				state = self.failures[state]
			state = self.transitions[state].get(character, 0)
			terminal = state if self.outputs[state] else self.output_links[state]
			while terminal and terminal not in seen_terminals:
				seen_terminals.add(terminal)
				for match in self.outputs[terminal]:
					if match in matches:
						continue
					if budget[0] >= MAX_GLOSSARY_MATCHES:
						raise ValueError(f"glossary matches exceed {MAX_GLOSSARY_MATCHES}")
					budget[0] += 1
					matches.add(match)
				terminal = self.output_links[terminal]
		return matches


def _run(
	candidate_name: str,
	output_path: Path,
	report_path: Path,
	*,
	compatibility_path: Path | None = None,
	compile_candidate=None,
	clock=time.monotonic,
	fail_fast=False,
	po_parser=parse_po,
	write=lambda stream, value: stream.write(value),
	fsync=os.fsync,
	replace=os.replace,
	remove=lambda path: path.unlink(missing_ok=True),
) -> dict:
	"""Validate, compile, and atomically publish one registered candidate."""
	started = clock()
	compatibility_path = compatibility_path or Path(__file__).with_name("compatibility.json")
	inventory, candidate, segment, artifacts = _load_trusted(candidate_name, Path(compatibility_path))
	inventory_entries = _validate_inventory(inventory)
	if segment is None:
		expected = inventory_entries
	else:
		expected = {}
		for selected in segment["keys"]:
			key = _key_tuple(selected["key"])
			if key not in inventory_entries:
				raise ValueError(f"segment contains a key outside the Release Inventory: {key!r}")
			entry = inventory_entries[key]
			if selected.get("source_digest") != entry.get("source_digest"):
				raise ValueError(f"segment source digest mismatch for {key!r}")
			expected[key] = entry
	grouped = {}
	for entry in candidate["entries"]:
		grouped.setdefault(_key_tuple(entry["key"]), []).append(entry)
	errors = []
	exceptions = artifacts["translation_exceptions.json"]["entries"]
	approved_exceptions = {
		(_key_tuple(exception.get("key", {})), exception.get("source_digest")) for exception in exceptions
	}
	resolutions = {
		_key_tuple(resolution["key"]): resolution
		for resolution in artifacts["collision_resolutions.json"]["entries"]
	}
	selectors = artifacts["glossary_selectors.json"]["entries"]
	actual = {}
	for key, records in grouped.items():
		inventory_entry = expected.get(key)
		if inventory_entry is not None:
			record_error_codes = set()
			for record in records:
				if record.get("source_digest") != inventory_entry.get("source_digest"):
					record_error_codes.add("SOURCE_DIGEST_MISMATCH")
				if not record.get("translation"):
					record_error_codes.add("EMPTY_TRANSLATION")
				if "fuzzy" in record.get("flags", []):
					record_error_codes.add("FUZZY_TRANSLATION")
			for code in record_error_codes:
				errors.append(_error(code, key, inventory_entry))
		if len(records) > 1:
			translations = {record.get("translation") for record in records}
			code = "DUPLICATE_TRANSLATION_KEY" if len(translations) == 1 else "CONFLICTING_TRANSLATION"
			errors.append(_error(code, key, expected.get(key, records[0])))
			if len(translations) != 1:
				continue
		actual[key] = min(records, key=canonical_json)
	for key in expected.keys() - grouped.keys():
		errors.append(_error("MISSING_TRANSLATION_KEY", key, expected[key]))
	for key in grouped.keys() - expected.keys():
		errors.append(_error("EXTRA_TRANSLATION_KEY", key, grouped[key][0]))
	token_cache = {}
	html_cache = {}

	def cached_tokens(value):
		if value not in token_cache:
			token_cache[value] = _tokens(value)
		return token_cache[value]

	def cached_html(value):
		if value not in html_cache:
			html_cache[value] = _parse_html(value)
		return html_cache[value]

	for key in actual.keys() & expected.keys():
		entry = actual[key]
		if (
			entry.get("translation") == key[0]
			and (
				key,
				expected[key].get("source_digest"),
			)
			not in approved_exceptions
		):
			errors.append(_error("IDENTICAL_TRANSLATION_WITHOUT_EXCEPTION", key, expected[key]))
		if expected[key].get("context_decision"):
			resolution = resolutions.get(key)
			resolved = resolution is not None and resolution.get("translation") == entry.get("translation")
			if not resolved:
				errors.append(_error("UNRESOLVED_CONTEXTLESS_COLLISION", key, expected[key]))
		source_tokens, source_unknown = cached_tokens(key[0])
		translation_tokens, translation_unknown = cached_tokens(entry.get("translation", ""))
		if source_unknown or translation_unknown:
			errors.append(_error("UNKNOWN_TOKEN_SYNTAX", key, expected[key]))
		elif source_tokens != translation_tokens:
			errors.append(_error("PRESERVED_TOKEN_MISMATCH", key, expected[key]))
		for code in _html_errors(key[0], entry.get("translation", ""), cached_html):
			errors.append(_error(code, key, expected[key]))
	notices = []
	selected = set()
	forms_by_policy = {}
	for selector in selectors:
		key = _key_tuple(selector.get("key", {}))
		inventory_entry = expected.get(key)
		policy = (selector["case_sensitive"], selector["unicode_normalization"])
		for form in selector["accepted_forms"]:
			normalized = _normalize_literal(form, *policy)
			forms_by_policy.setdefault(policy, {}).setdefault(normalized, set()).add(
				("accepted", selector["id"])
			)
		for form in selector["forbidden_forms"]:
			normalized = _normalize_literal(form, *policy)
			forms_by_policy.setdefault(policy, {}).setdefault(normalized, set()).add(
				("forbidden", selector["id"])
			)
		if inventory_entry is not None:
			selected.add((selector["id"], key))
	indexes = {policy: _LiteralIndex(forms) for policy, forms in forms_by_policy.items()}
	matches_by_key = {}
	match_budget = [0]
	for key, candidate_entry in actual.items():
		translation = candidate_entry.get("translation", "")
		matches = set()
		for policy, index in indexes.items():
			normalized = _normalize_literal(translation, *policy)
			matches.update(index.matches(normalized, match_budget))
		matches_by_key[key] = matches
	for selector in selectors:
		key = _key_tuple(selector["key"])
		inventory_entry = expected.get(key)
		if inventory_entry is None or key not in actual:
			continue
		matches = matches_by_key[key]
		if ("accepted", selector["id"]) not in matches or ("forbidden", selector["id"]) in matches:
			errors.append(_error("GLOSSARY_SELECTOR_VIOLATION", key, inventory_entry, selector["id"]))
	for key, matches in matches_by_key.items():
		for kind, selector_id in sorted(matches):
			if kind != "forbidden":
				continue
			if (selector_id, key) in selected:
				continue
			notice = _error(
				"GLOSSARY_REVIEW_SUGGESTION",
				key,
				expected.get(key, actual[key]),
				selector_id,
			)
			notices.append(notice)
	if errors:
		errors.sort(key=lambda item: item["sort_key"])
		quality_errors = errors[:1] if fail_fast else errors
		result = _result(candidate_name, started, clock, len(expected), quality_errors, notices)
		_validate_destinations(output_path, report_path)
		_replace_bytes(report_path, _report_bytes(result))
		return result
	po_bytes = build_po(list(actual.values()))
	with tempfile.TemporaryDirectory(prefix="frappe-lt-quality-") as directory:
		workspace = Path(directory)
		po_path = workspace / "apps" / "frappe_lt" / "frappe_lt" / "locale" / "lt.po"
		po_path.parent.mkdir(parents=True)
		po_path.write_bytes(po_bytes)
		po_parser(po_path)
		compile_po(po_path, workspace, compiler=compile_candidate)
	result = _result(candidate_name, started, clock, len(expected), [], notices)
	_validate_destinations(output_path, report_path)
	_replace_bytes(report_path, _report_bytes(result))
	_validate_destinations(output_path, report_path)
	_replace_bytes(
		output_path,
		po_bytes,
		write=write,
		fsync=fsync,
		replace=replace,
		remove=remove,
	)
	return result


def run(
	candidate_name: str,
	output_path: Path,
	report_path: Path,
	*,
	compatibility_path: Path | None = None,
	compile_candidate=None,
	clock=time.monotonic,
	fail_fast=False,
	po_parser=parse_po,
	write=lambda stream, value: stream.write(value),
	fsync=os.fsync,
	replace=os.replace,
	remove=lambda path: path.unlink(missing_ok=True),
) -> dict:
	"""Run the public gate and convert trust/tool failures to canonical exit-2 reports."""
	started = clock()
	output_path = Path(output_path)
	report_path = Path(report_path)
	try:
		report_path = _validate_destination(report_path, "report", ".json")
	except Exception as error:
		failure = _error("UNTRUSTED_INPUT_OR_TOOL_FAILURE")
		failure["detail"] = f"{type(error).__name__}: {error}"
		result = _result(candidate_name, started, clock, 0, [failure])
		result["exit_code"] = 2
		return result
	try:
		output_path, report_path = _validate_destinations(output_path, report_path)
	except Exception as error:
		failure = _error("UNTRUSTED_INPUT_OR_TOOL_FAILURE")
		failure["detail"] = f"{type(error).__name__}: {error}"
		result = _result(candidate_name, started, clock, 0, [failure])
		result["exit_code"] = 2
		try:
			_replace_bytes(report_path, _report_bytes(result))
		except Exception:
			pass
		return result
	try:
		return _run(
			candidate_name,
			Path(output_path),
			Path(report_path),
			compatibility_path=compatibility_path,
			compile_candidate=compile_candidate,
			clock=clock,
			fail_fast=fail_fast,
			po_parser=po_parser,
			write=write,
			fsync=fsync,
			replace=replace,
			remove=remove,
		)
	except Exception as error:
		failure = _error("UNTRUSTED_INPUT_OR_TOOL_FAILURE")
		failure["detail"] = f"{type(error).__name__}: {error}"
		result = _result(candidate_name, started, clock, 0, [failure])
		result["exit_code"] = 2
		try:
			_validate_destination(report_path, "report", ".json")
			_replace_bytes(report_path, _report_bytes(result))
		except Exception:
			pass
		return result
