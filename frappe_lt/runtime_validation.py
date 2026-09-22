import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from itertools import pairwise
from pathlib import Path
from urllib.parse import quote, quote_plus

from frappe_lt.inventory import _json_object, canonical_json, verify_environment
from frappe_lt.runtime_contracts import SCHEMA_VERSION as RUNTIME_CONTRACT_SCHEMA_VERSION
from frappe_lt.runtime_contracts import load_contracts, safe_relative_path
from frappe_lt.runtime_control import (
	SiteControl,
	_active_translation_key,
	_active_translation_keys,
	redact_sensitive,
)
from frappe_lt.runtime_discovery import coverage, discover

REPORT_SCHEMA_VERSION = 5
BROWSER_SCHEMA_VERSION = 6
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_PER_ARTIFACT = 2 * 1024 * 1024
MAX_EVIDENCE_PER_SCENARIO = 8 * 1024 * 1024
MAX_EVIDENCE_PER_RUN = 32 * 1024 * 1024
MAX_FINDINGS = 500_000
SHA256 = re.compile(r"[0-9a-f]{64}")
DIAGNOSTIC_ID = re.compile(r"hmac-sha256:([0-9a-f]{32}):([0-9a-f]{64})\Z")
EVIDENCE_MIME = {
	"browser": {"application/json": "browser.json"},
	"email": {"text/plain": "email.txt"},
	"portal": {"text/html": "portal.html"},
	"print": {"text/html": "print.html"},
}
HARNESS_CONTRACT = {
	"blocked-readiness-contract": ("scenario did not prove readiness", False),
	"blocked-timeout-contract": ("scenario exceeded 100 ms", True),
}


class BrowserRunnerError(RuntimeError):
	def __init__(self, message: str, diagnostic: str):
		super().__init__(message)
		self.diagnostic = diagnostic


def _redact(value: object) -> str:
	return redact_sensitive(value)


def _redact_browser_diagnostic(value: bytes, plan: dict) -> str:
	text = value.decode("utf-8", "replace")
	secrets = []

	def collect(candidate):
		if isinstance(candidate, str) and candidate:
			secrets.append(candidate)
		elif isinstance(candidate, dict):
			for nested in candidate.values():
				collect(nested)
		elif isinstance(candidate, list):
			for nested in candidate:
				collect(nested)

	collect(plan.get("credentials", {}))
	collect(plan.get("fixtures", {}))
	collect(plan.get("token"))
	for secret in sorted(set(secrets), key=len, reverse=True):
		for encoded in {secret, quote(secret, safe=""), quote_plus(secret, safe="")}:
			text = text.replace(encoded, "[REDACTED]")
	return redact_sensitive(text, max_chars=65 * 1024)


def _exact(value: object, fields: set[str], label: str) -> dict:
	if not isinstance(value, dict) or set(value) != fields:
		raise ValueError(f"{label} fields must be exactly {sorted(fields)}")
	return value


def _read_json(path: Path) -> dict:
	try:
		if path.stat().st_size > MAX_REPORT_BYTES:
			raise ValueError(f"browser result exceeds {MAX_REPORT_BYTES} bytes")
		return json.loads(
			path.read_bytes(),
			object_pairs_hook=_json_object,
			parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON number {value}")),
		)
	except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
		raise ValueError(f"could not read browser result: {error}") from error


def _validate_attempts(value: object, status: str) -> list[dict]:
	if not isinstance(value, list) or not value:
		raise ValueError("scenario must record every attempt")
	for number, attempt in enumerate(value, start=1):
		attempt = _exact(
			attempt,
			{"duration_ms", "error", "kind", "number", "outcome"},
			"scenario attempt",
		)
		if attempt["number"] != number or attempt["kind"] not in {"initial", "setup", "transport"}:
			raise ValueError("scenario attempts are malformed")
		if number == 1 and attempt["kind"] != "initial":
			raise ValueError("first scenario attempt must be initial")
		if (
			isinstance(attempt["duration_ms"], bool)
			or not isinstance(attempt["duration_ms"], int)
			or not 0 <= attempt["duration_ms"] <= 600_000
		):
			raise ValueError("scenario attempt duration is invalid")
		if attempt["outcome"] not in {
			"assertion_failure",
			"blocked",
			"pass",
			"setup_failure",
			"transport_failure",
		}:
			raise ValueError("scenario attempt outcome is invalid")
		failed = attempt["outcome"] != "pass"
		if (failed and not (isinstance(attempt["error"], str) and attempt["error"])) or (
			not failed and attempt["error"] is not None
		):
			raise ValueError("scenario attempt error must describe every non-pass outcome")
	for previous, retried in pairwise(value):
		if previous["outcome"] == "assertion_failure":
			raise ValueError("assertion failures must not be retried")
		expected_retry = {
			"setup_failure": "setup",
			"transport_failure": "transport",
		}.get(previous["outcome"])
		if retried["kind"] != expected_retry:
			raise ValueError("only setup or transport failures may be retried")
	expected_outcome = {"blocked": "blocked", "fail": "assertion_failure", "pass": "pass"}[status]
	if value[-1]["outcome"] != expected_outcome:
		raise ValueError("final scenario attempt outcome does not match scenario status")
	return value


def _validate_harness_contract(value: object) -> list[dict]:
	if not isinstance(value, list):
		raise ValueError("browser harness contract must be a list")
	seen = set()
	ids = []
	for result in value:
		result = _exact(
			result,
			{
				"attempts",
				"blocked_reason",
				"duration_ms",
				"error",
				"evidence",
				"fallbacks",
				"id",
				"layouts",
				"ready",
				"status",
			},
			"browser harness result",
		)
		scenario_id = result["id"]
		if scenario_id not in HARNESS_CONTRACT or scenario_id in seen:
			raise ValueError(f"unknown or duplicate browser harness result {scenario_id!r}")
		seen.add(scenario_id)
		ids.append(scenario_id)
		expected_reason, expected_ready = HARNESS_CONTRACT[scenario_id]
		if (
			result["status"] != "blocked"
			or result["blocked_reason"] != expected_reason
			or result["ready"] is not expected_ready
			or result["error"] is not None
			or result["evidence"] != []
			or result["fallbacks"] != []
			or result["layouts"] != []
			or isinstance(result["duration_ms"], bool)
			or not isinstance(result["duration_ms"], int)
			or not 0 <= result["duration_ms"] <= 600_000
		):
			raise ValueError(f"browser harness result {scenario_id!r} is malformed")
		_validate_attempts(result["attempts"], result["status"])
		if (
			len(result["attempts"]) != 1
			or result["attempts"][0]["duration_ms"] != result["duration_ms"]
			or result["attempts"][0]["error"] != expected_reason
		):
			raise ValueError(f"browser harness attempt {scenario_id!r} is malformed")
	if ids != list(HARNESS_CONTRACT):
		raise ValueError("browser harness contract must contain exactly the canonical synthetic records")
	return value


def _validate_fallback(
	value: object,
	scenario: dict,
	active_keys: frozenset[tuple[str, str | None]],
	diagnostic_key: bytes | None,
) -> dict:
	scenario_id = scenario["id"]
	value = _exact(
		value,
		{
			"active",
			"effective",
			"excluded",
			"exclusion_id",
			"key",
			"raw_source",
			"render_status",
			"schema_version",
			"scenario_id",
			"source",
			"target",
			"visible",
		},
		"lookup evidence",
	)
	if value["schema_version"] != 1:
		raise ValueError("unsupported lookup evidence schema")
	if value["scenario_id"] != scenario_id:
		raise ValueError("lookup evidence scenario_id does not match its scenario")
	if not isinstance(value["active"], bool):
		raise ValueError("lookup evidence active must be boolean")
	key = _exact(value["key"], {"context", "source"}, "Translation Key")
	if not isinstance(key["source"], str) or not key["source"].strip():
		raise ValueError("Translation Key source must be nonempty text")
	if key["source"] != key["source"].strip():
		raise ValueError("Translation Key source must use Frappe outer-whitespace normalization")
	if not isinstance(value["raw_source"], str) or value["raw_source"].strip() != key["source"]:
		raise ValueError("lookup raw_source must normalize to its separate Translation Key source")
	if key["context"] is not None and (not isinstance(key["context"], str) or not key["context"]):
		raise ValueError("Frappe Context must be nonempty text or null")
	for field in ("effective", "source"):
		if not isinstance(value[field], str):
			raise ValueError(f"lookup evidence {field} must be text")
	if value["source"] not in {"database", "erpnext", "frappe", "frappe_lt", "merged", "missing"}:
		raise ValueError("lookup evidence source is invalid")
	target = _exact(value["target"], {"type", "value"}, "render target")
	if (
		target["type"] not in {"locator", "output_interval"}
		or not isinstance(target["value"], str)
		or not target["value"]
	):
		raise ValueError("render target is invalid")
	if (
		len(key["source"].encode()) > 256 * 1024
		or len(value["raw_source"].encode()) > 256 * 1024
		or len(value["effective"].encode()) > 256 * 1024
	):
		raise ValueError("lookup evidence text exceeds its byte limit")
	if (key["context"] is not None and len(key["context"].encode()) > 4096) or len(
		target["value"].encode()
	) > 4096:
		raise ValueError("lookup context or render target exceeds its byte limit")
	if not isinstance(value["visible"], bool) or not isinstance(value["excluded"], bool):
		raise ValueError("lookup evidence visibility and exclusion must be boolean")
	if value["render_status"] not in {"ambiguous", "unique", "unrendered"}:
		raise ValueError("lookup evidence render_status is invalid")
	if value["visible"] != (value["render_status"] != "unrendered"):
		raise ValueError("lookup evidence visibility and render_status are inconsistent")
	if value["excluded"] != (value["exclusion_id"] is not None):
		raise ValueError("lookup evidence exclusion decision is inconsistent")
	if value["excluded"]:
		if value["render_status"] != "unique":
			raise ValueError("only uniquely correlated lookup evidence may be excluded")
		exclusions = {exclusion["id"]: exclusion["target"] for exclusion in scenario["expected_exclusions"]}
		exclusion_target = exclusions.get(value["exclusion_id"])
		output_interval = target["type"] == "output_interval" and re.fullmatch(
			r"(?:email:subject-body|print:http-body):[0-9]+-[0-9]+", target["value"]
		)
		if (exclusion_target in {"output:fixture-values", "output:recipient"} and not output_interval) or (
			exclusion_target not in {"output:fixture-values", "output:recipient"}
			and exclusion_target != target["value"]
		):
			raise ValueError("lookup evidence exclusion is not an exact reviewed scenario exclusion")
	is_active = _active_translation_key(key["source"], key["context"], active_keys) is not None
	if value["active"] != is_active:
		raise ValueError("lookup evidence active disagrees with the authenticated Release Inventory")
	diagnostic_match = DIAGNOSTIC_ID.fullmatch(key["source"])
	if not is_active and (
		diagnostic_match is None
		or key["context"] is not None
		or value["raw_source"] != key["source"]
		or value["effective"] != key["source"]
		or value["source"] != "missing"
	):
		raise ValueError("inactive lookup must contain only its trusted diagnostic identifier")
	if not is_active:
		if diagnostic_key is None:
			raise ValueError("inactive lookup cannot be authenticated without its runtime evidence key")
		nonce, signature = diagnostic_match.groups()
		expected = hmac.new(
			diagnostic_key,
			f"{scenario_id}\0{nonce}".encode(),
			hashlib.sha256,
		).hexdigest()
		if not hmac.compare_digest(signature, expected):
			raise ValueError("inactive lookup diagnostic identifier failed authentication")
		if (
			target["type"] == "locator"
			and not value["excluded"]
			and target["value"] != f"diagnostic:{key['source']}"
		):
			raise ValueError("inactive lookup locator must contain only its diagnostic identifier")
	return value


def _is_blocking_fallback(finding: dict) -> bool:
	return (
		finding["active"]
		and finding["render_status"] != "unrendered"
		and finding["visible"]
		and not finding["excluded"]
		and (finding["source"] == "missing" or finding["effective"].strip() == finding["key"]["source"])
	)


def _is_inactive_inventory_lookup(finding: dict) -> bool:
	return (
		not finding["active"]
		and finding["render_status"] != "unrendered"
		and finding["visible"]
		and not finding["excluded"]
	)


def _is_functional_layout(finding: dict) -> bool:
	return finding["severity"] == "functional"


def _validate_layout(value: object, scenario_id: str) -> dict:
	value = _exact(value, {"detail", "kind", "scenario_id", "severity", "target"}, "layout finding")
	if value["scenario_id"] != scenario_id or value["severity"] not in {"cosmetic", "functional"}:
		raise ValueError("layout finding identity or severity is invalid")
	if value["kind"] not in {"clipped", "covered", "horizontal_overflow", "unusable", "wrapping"}:
		raise ValueError("layout finding kind is invalid")
	if not all(isinstance(value[field], str) and value[field] for field in ("detail", "target")):
		raise ValueError("layout finding detail and target must be nonempty text")
	if len(value["detail"].encode()) > 4096 or len(value["target"].encode()) > 4096:
		raise ValueError("layout finding text exceeds its byte limit")
	return value


def _validate_toolchain(value: object) -> dict:
	value = _exact(
		value,
		{"browser", "cypress", "node", "plugins", "schema_version"},
		"toolchain",
	)
	if value["schema_version"] != 1:
		raise ValueError("unsupported toolchain schema")
	for name in ("browser", "cypress", "node"):
		fields = {"name", "version"} if name == "browser" else {"version"}
		entry = _exact(value[name], fields, f"toolchain {name}")
		if any(
			not isinstance(entry[field], str)
			or not re.fullmatch(r"[A-Za-z0-9 ._+()/:-]{1,256}", entry[field])
			for field in fields
		):
			raise ValueError(f"toolchain {name} values must be nonempty text")
	plugins = value["plugins"]
	if not isinstance(plugins, dict) or list(plugins) != sorted(plugins):
		raise ValueError("toolchain plugins must be a canonically ordered object")
	if any(
		not isinstance(name, str)
		or not re.fullmatch(r"[A-Za-z0-9@/_.-]{1,160}", name)
		or not isinstance(version, str)
		or not re.fullmatch(r"[A-Za-z0-9 ._+()/:-]{1,256}", version)
		for name, version in plugins.items()
	):
		raise ValueError("toolchain plugin names and versions are invalid")
	return value


def _validate_evidence_structure(value: object, scenario_id: str) -> dict:
	value = _exact(value, {"bytes", "kind", "mime", "path", "sha256"}, "evidence artifact")
	if value["kind"] not in EVIDENCE_MIME:
		raise ValueError("evidence artifact kind is invalid")
	if (
		isinstance(value["bytes"], bool)
		or not isinstance(value["bytes"], int)
		or not 0 <= value["bytes"] <= MAX_EVIDENCE_PER_ARTIFACT
	):
		raise ValueError("evidence artifact byte count or limit is invalid")
	if value["mime"] not in EVIDENCE_MIME[value["kind"]]:
		raise ValueError("evidence artifact mime is not allowed for its kind")
	if not isinstance(value["sha256"], str) or not SHA256.fullmatch(value["sha256"]):
		raise ValueError("evidence artifact sha256 is invalid")
	relative = safe_relative_path(value["path"])
	expected = ("evidence", scenario_id, EVIDENCE_MIME[value["kind"]][value["mime"]])
	if relative.parts != expected:
		raise ValueError("evidence artifact must be inside its scenario evidence directory")
	return value


def _validate_evidence(value: object, run_root: Path, scenario_id: str) -> dict:
	value = _validate_evidence_structure(value, scenario_id)
	relative = safe_relative_path(value["path"])
	try:
		with ExitStack() as descriptors:
			directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
			directory_descriptor = os.open(run_root, directory_flags)
			descriptors.callback(os.close, directory_descriptor)
			for component in relative.parts[:-1]:
				directory_descriptor = os.open(component, directory_flags, dir_fd=directory_descriptor)
				descriptors.callback(os.close, directory_descriptor)
			descriptor = os.open(
				relative.parts[-1],
				os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
				dir_fd=directory_descriptor,
			)
			descriptors.callback(os.close, descriptor)
			before = os.fstat(descriptor)
			if not stat.S_ISREG(before.st_mode) or before.st_size != value["bytes"]:
				raise ValueError(f"evidence artifact size or identity mismatch: {value['path']}")
			content = bytearray()
			while len(content) <= MAX_EVIDENCE_PER_ARTIFACT:
				chunk = os.read(descriptor, min(64 * 1024, MAX_EVIDENCE_PER_ARTIFACT + 1 - len(content)))
				if not chunk:
					break
				content.extend(chunk)
			after = os.fstat(descriptor)
			identity = (before.st_dev, before.st_ino, before.st_size)
			if identity != (after.st_dev, after.st_ino, after.st_size) or len(content) != value["bytes"]:
				raise ValueError(f"evidence artifact changed while being read: {value['path']}")
	except OSError as error:
		raise ValueError(f"could not hash evidence artifact {value['path']}: {error}") from error
	content = bytes(content)
	if hashlib.sha256(content).hexdigest() != value["sha256"]:
		raise ValueError(f"evidence artifact size or digest mismatch: {value['path']}")
	try:
		text = content.decode("utf-8")
	except UnicodeDecodeError as error:
		raise ValueError(f"evidence artifact is not UTF-8 text: {value['path']}") from error
	if value["mime"] == "application/json":
		try:
			parsed = json.loads(content, object_pairs_hook=_json_object)
		except (UnicodeDecodeError, json.JSONDecodeError) as error:
			raise ValueError(f"evidence JSON is invalid: {value['path']}") from error
		if canonical_json(parsed) != content:
			raise ValueError(f"evidence JSON is not canonical: {value['path']}")
	unsafe = (
		re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text)
		or re.search(r"(?i)\b(?:authorization|(?:set-)?cookie)\s*:\s*(?!\[REDACTED\])\S", text)
		or re.search(
			r"""(?ix)(?:["']?(?:password|pwd|token|csrf_token|api_key|api_secret|access_token|reset_token|sid)["']?\s*[:=]\s*)["']?(?!\[REDACTED\])[^\s,;}<>&"']+""",
			text,
		)
		or re.search(
			r"""(?ix)(?:data-)?(?:csrf[-_]token|api[-_]key|api[-_]secret|access[-_]token|reset[-_]token)=["'](?!\[REDACTED\])[^"']+""",
			text,
		)
		or re.search(
			r"(?i)(?:[?&](?:key|token|csrf_token|api_key|api_secret|access_token|reset_token)=|(?:%3f|%26)(?:key|token|csrf_token|api_key|api_secret|access_token|reset_token)%3d)(?!\[REDACTED\])[^&%\s<>\"']+",
			text,
		)
		or re.search(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text)
		or re.search(r"(?i)\b[A-Z0-9._+-]+%40[A-Z0-9.-]+(?:\.|%2e)[A-Z]{2,}\b", text)
		or re.search(r"(?i)frappe-lt-runtime-[0-9a-f]{32}[A-Za-z0-9@._:-]*", text)
	)
	if unsafe:
		raise ValueError(f"evidence artifact contains disallowed sensitive material: {value['path']}")
	return value


def _contains_inactive(item: object) -> bool:
	if isinstance(item, dict):
		return item.get("active") is False or any(_contains_inactive(child) for child in item.values())
	if isinstance(item, list):
		return any(_contains_inactive(child) for child in item)
	return False


def _remove_evidence_tree(path: Path) -> None:
	try:
		if path.is_symlink() or path.is_file():
			path.unlink()
		elif path.is_dir():
			shutil.rmtree(path)
	except OSError as error:
		raise ValueError(f"could not remove runtime evidence: {error}") from error


def _validate_browser_results(
	value: object,
	scenarios: dict,
	run_root: Path,
	*,
	diagnostic_sampling: bool = False,
	diagnostic_key: str | None = None,
) -> list[dict]:
	if not isinstance(diagnostic_sampling, bool):
		raise ValueError("diagnostic_sampling must be boolean")
	value = _exact(value, {"harness_contract", "scenarios", "schema_version", "toolchain"}, "browser result")
	if value["schema_version"] != BROWSER_SCHEMA_VERSION or not isinstance(value["scenarios"], list):
		raise ValueError("unsupported browser result schema")
	_validate_harness_contract(value["harness_contract"])
	_validate_toolchain(value["toolchain"])
	if diagnostic_key is not None and (
		not isinstance(diagnostic_key, str) or not re.fullmatch(r"[0-9a-f]{64}", diagnostic_key)
	):
		raise ValueError("runtime evidence key is invalid")
	diagnostic_key_bytes = bytes.fromhex(diagnostic_key) if diagnostic_key is not None else None
	active_keys = _active_translation_keys()
	manifest = {scenario["id"]: scenario for scenario in scenarios["scenarios"]}
	results = []
	diagnostic_ids = set()
	total_evidence = 0
	seen = set()
	for result in value["scenarios"]:
		result = _exact(
			result,
			{
				"attempts",
				"blocked_reason",
				"duration_ms",
				"error",
				"evidence",
				"fallbacks",
				"id",
				"layouts",
				"ready",
				"status",
			},
			"browser scenario result",
		)
		scenario_id = result["id"]
		if not isinstance(scenario_id, str) or scenario_id not in manifest or scenario_id in seen:
			raise ValueError(f"unknown or duplicate browser scenario result {scenario_id!r}")
		seen.add(scenario_id)
		if result["status"] not in {"blocked", "fail", "pass"}:
			raise ValueError("browser scenario status is invalid")
		if not isinstance(result["ready"], bool):
			raise ValueError("browser scenario readiness must be boolean")
		if (result["status"] == "blocked") != (result["blocked_reason"] is not None):
			raise ValueError("Blocked Runtime Scenario must have exactly one blocked reason")
		if result["blocked_reason"] is not None and not isinstance(result["blocked_reason"], str):
			raise ValueError("blocked reason must be text or null")
		if result["error"] is not None and (
			result["status"] != "fail" or not isinstance(result["error"], str) or not result["error"]
		):
			raise ValueError("scenario error must be nonempty text only for a failed scenario")
		if result["error"] is not None:
			result["error"] = _redact(result["error"])
		if result["blocked_reason"] is not None:
			result["blocked_reason"] = _redact(result["blocked_reason"])
		if not isinstance(result["duration_ms"], int) or not 0 <= result["duration_ms"] <= 600_000:
			raise ValueError("browser scenario duration is invalid")
		_validate_attempts(result["attempts"], result["status"])
		for attempt in result["attempts"]:
			if attempt["error"] is not None:
				attempt["error"] = _redact(attempt["error"])
		if (
			result["status"] == "pass"
			and result["duration_ms"] > manifest[scenario_id]["scenario_timeout_ms"]
		):
			result["status"] = "blocked"
			result["blocked_reason"] = "scenario exceeded its manifest timeout"
			result["attempts"][-1]["outcome"] = "blocked"
			result["attempts"][-1]["error"] = result["blocked_reason"]
		if not all(isinstance(result[field], list) for field in ("evidence", "fallbacks", "layouts")):
			raise ValueError("browser findings and evidence must be lists")
		result["fallbacks"] = sorted(
			(
				_validate_fallback(item, manifest[scenario_id], active_keys, diagnostic_key_bytes)
				for item in result["fallbacks"]
			),
			key=lambda item: (
				item["key"]["source"],
				item["key"]["context"] or "",
				item["render_status"],
				item["target"]["value"],
			),
		)
		for finding in result["fallbacks"]:
			if not finding["active"]:
				diagnostic_id = finding["key"]["source"]
				if diagnostic_id in diagnostic_ids:
					raise ValueError("inactive lookup diagnostic identifier was replayed")
				diagnostic_ids.add(diagnostic_id)
		result["layouts"] = sorted(
			(_validate_layout(item, scenario_id) for item in result["layouts"]),
			key=lambda item: (item["severity"], item["kind"], item["target"], item["detail"]),
		)
		result["evidence"] = sorted(
			(_validate_evidence(item, run_root, scenario_id) for item in result["evidence"]),
			key=lambda item: item["path"],
		)
		if any(not item["active"] for item in result["fallbacks"]) and result["evidence"]:
			for item in result["evidence"]:
				try:
					(run_root / item["path"]).unlink()
				except OSError as error:
					raise ValueError(f"could not remove inactive lookup evidence: {error}") from error
			raise ValueError("inactive lookups cannot publish evidence artifacts")
		if len({item["path"] for item in result["evidence"]}) != len(result["evidence"]):
			raise ValueError(f"scenario {scenario_id!r} contains duplicate evidence paths")
		scenario_evidence = sum(item["bytes"] for item in result["evidence"])
		if scenario_evidence > MAX_EVIDENCE_PER_SCENARIO:
			raise ValueError(f"scenario {scenario_id!r} exceeds its evidence byte limit")
		total_evidence += scenario_evidence
		has_active_lookup = any(item["active"] for item in result["fallbacks"])
		blocking_fallback = any(_is_blocking_fallback(item) for item in result["fallbacks"])
		blocking_inventory_lookup = any(_is_inactive_inventory_lookup(item) for item in result["fallbacks"])
		blocking_layout = any(_is_functional_layout(item) for item in result["layouts"])
		if result["status"] == "pass" and not result["ready"]:
			result["status"] = "blocked"
			result["blocked_reason"] = "scenario did not prove readiness"
			result["attempts"][-1]["outcome"] = "blocked"
			result["attempts"][-1]["error"] = result["blocked_reason"]
		if result["status"] == "pass" and (blocking_fallback or blocking_inventory_lookup or blocking_layout):
			result["status"] = "fail"
			result["error"] = "scenario produced blocking runtime findings"
			result["attempts"][-1]["outcome"] = "assertion_failure"
			result["attempts"][-1]["error"] = result["error"]
		if result["status"] == "pass" and not has_active_lookup:
			result["status"] = "blocked"
			result["blocked_reason"] = "scenario produced no active effective translation lookup evidence"
			result["attempts"][-1]["outcome"] = "blocked"
			result["attempts"][-1]["error"] = result["blocked_reason"]
		if result["status"] == "pass" and result["evidence"] and not diagnostic_sampling:
			raise ValueError("pass evidence requires explicit diagnostic sampling")
		results.append(result)
	if total_evidence > MAX_EVIDENCE_PER_RUN:
		raise ValueError("runtime run exceeds its evidence byte limit")
	if sum(len(result["fallbacks"]) + len(result["layouts"]) for result in results) > MAX_FINDINGS:
		raise ValueError("runtime run exceeds its finding limit")
	for scenario_id in sorted(set(manifest) - seen):
		results.append(
			blocked_result(scenario_id, "browser did not return a result for the manifest scenario")
		)
	return sorted(results, key=lambda result: result["id"])


def validate_browser_results(
	value: object,
	scenarios: dict,
	run_root: Path,
	*,
	diagnostic_sampling: bool = False,
	diagnostic_key: str | None = None,
) -> list[dict]:
	evidence_root = run_root / "evidence"
	try:
		declares_inactive = _contains_inactive(value)
		results = _validate_browser_results(
			value,
			scenarios,
			run_root,
			diagnostic_sampling=diagnostic_sampling,
			diagnostic_key=diagnostic_key,
		)
	except Exception:
		_remove_evidence_tree(evidence_root)
		raise
	if declares_inactive:
		try:
			if evidence_root.is_symlink() or evidence_root.is_file():
				_remove_evidence_tree(evidence_root)
			else:
				for result in results:
					if any(not finding["active"] for finding in result["fallbacks"]):
						_remove_evidence_tree(evidence_root / result["id"])
		except Exception:
			_remove_evidence_tree(evidence_root)
			raise
	return results


def blocked_result(scenario_id: str, reason: str) -> dict:
	reason = _redact(reason)
	return {
		"attempts": [
			{
				"duration_ms": 0,
				"error": reason,
				"kind": "initial",
				"number": 1,
				"outcome": "blocked",
			}
		],
		"blocked_reason": reason,
		"duration_ms": 0,
		"evidence": [],
		"error": None,
		"fallbacks": [],
		"id": scenario_id,
		"layouts": [],
		"ready": False,
		"status": "blocked",
	}


def _default_browser_runner(site: str, run_root: Path, plan_path: Path) -> tuple[dict, int, str | None]:
	run_root = run_root.resolve()
	plan_path = plan_path.resolve()
	result_path = run_root / "browser-results.json"
	environment = os.environ.copy()
	environment.update(
		{
			"FRAPPE_LT_RUNTIME_PLAN": str(plan_path),
			"FRAPPE_LT_RUNTIME_RESULT": str(result_path),
			"FRAPPE_LT_RUNTIME_ROOT": str(run_root),
		}
	)
	bench = shutil.which("bench") or "bench"
	plan = _read_json(plan_path)
	deadline_seconds = (
		120 + sum(scenario["scenario_timeout_ms"] for scenario in plan.get("scenarios", [])) / 1000
	)
	process = subprocess.Popen(
		[bench, "--site", site, "run-ui-tests", "frappe_lt", "--headless"],
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		env=environment,
		start_new_session=True,
	)
	diagnostic_bytes = bytearray()
	diagnostic_truncated = False

	def drain_output():
		nonlocal diagnostic_truncated
		while chunk := process.stdout.read(8192):
			diagnostic_bytes.extend(chunk)
			if len(diagnostic_bytes) > 64 * 1024:
				del diagnostic_bytes[32 * 1024 : -32 * 1024]
				diagnostic_truncated = True

	def diagnostic_output():
		if not diagnostic_truncated:
			return bytes(diagnostic_bytes)
		return (
			bytes(diagnostic_bytes[: 32 * 1024])
			+ b"\n...[diagnostic output truncated]...\n"
			+ bytes(diagnostic_bytes[-32 * 1024 :])
		)

	drain = threading.Thread(target=drain_output, name="frappe-lt-cypress-output", daemon=True)
	drain.start()
	try:
		process.wait(timeout=deadline_seconds)
	except subprocess.TimeoutExpired as error:
		os.killpg(process.pid, signal.SIGTERM)
		try:
			process.wait(timeout=10)
		except subprocess.TimeoutExpired:
			os.killpg(process.pid, signal.SIGKILL)
			process.wait()
		drain.join(timeout=10)
		diagnostic = _redact_browser_diagnostic(diagnostic_output(), plan)
		result_path.unlink(missing_ok=True)
		raise BrowserRunnerError(
			f"Cypress exceeded its {int(deadline_seconds)} second run deadline",
			diagnostic,
		) from error
	drain.join(timeout=10)
	if drain.is_alive():
		process.stdout.close()
		raise BrowserRunnerError("Cypress diagnostic pipe did not close", "")
	diagnostic = _redact_browser_diagnostic(diagnostic_output(), plan)
	if not result_path.is_file():
		raise BrowserRunnerError(
			f"Cypress exited {process.returncode} without writing a result",
			diagnostic,
		)
	try:
		value = _read_json(result_path)
	finally:
		result_path.unlink(missing_ok=True)
	return value, process.returncode, diagnostic


def _validate_cleanup_failure(value: object, label: str = "report cleanup failure") -> dict:
	value = _exact(value, {"error", "mutation_id", "target"}, label)
	target = _exact(value["target"], {"doctype", "name"}, f"{label} target")
	if (
		not isinstance(value["error"], str)
		or not value["error"]
		or isinstance(value["mutation_id"], bool)
		or not isinstance(value["mutation_id"], int)
		or value["mutation_id"] < 0
		or not all(isinstance(target[field], str) and target[field] for field in target)
	):
		raise ValueError(f"{label} is malformed")
	return value


def _report_summary(results: list[dict], coverage_result: dict, cleanup_failures: list[dict]) -> dict:
	return {
		"blocked": sum(result["status"] == "blocked" for result in results),
		"cleanup_failures": len(cleanup_failures),
		"coverage_gaps": len(coverage_result["gaps"]),
		"english_fallbacks": sum(
			_is_blocking_fallback(finding) for result in results for finding in result["fallbacks"]
		),
		"fail": sum(result["status"] == "fail" for result in results),
		"functional_layout_defects": sum(
			_is_functional_layout(finding) for result in results for finding in result["layouts"]
		),
		"pass": sum(result["status"] == "pass" for result in results),
		"runtime_inventory_gaps": sum(
			_is_inactive_inventory_lookup(finding) for result in results for finding in result["fallbacks"]
		),
		"total": len(results),
	}


def _fact_blocking_causes(
	coverage_result: dict, results: list[dict], cleanup_failures: list[dict]
) -> list[dict]:
	causes = [
		*({"detail": gap, "type": "runtime_coverage_gap"} for gap in coverage_result["gaps"]),
		*(
			{
				"detail": f"{result['id']}: {result['error'] or result['blocked_reason'] or result['id']}"[
					:2048
				],
				"type": f"scenario_{result['status']}",
			}
			for result in results
			if result["status"] in {"blocked", "fail"}
		),
		*(
			{
				"detail": f"{failure['target']['doctype']}:{failure['target']['name']}",
				"type": "cleanup_failure",
			}
			for failure in cleanup_failures
		),
	]
	return sorted(causes, key=lambda cause: (cause["type"], cause["detail"]))


def validate_machine_report(value: object, scenario_contract: dict) -> dict:
	"""Validate the complete public runtime report before it is published."""
	scenario_contract = _exact(
		scenario_contract, {"scenarios", "schema_version"}, "Runtime Scenario Manifest"
	)
	manifest_scenario_ids = (
		[
			scenario["id"] if isinstance(scenario, dict) and isinstance(scenario.get("id"), str) else None
			for scenario in scenario_contract["scenarios"]
		]
		if isinstance(scenario_contract["scenarios"], list)
		else []
	)
	if (
		scenario_contract["schema_version"] != RUNTIME_CONTRACT_SCHEMA_VERSION
		or not manifest_scenario_ids
		or any(scenario_id is None or not scenario_id for scenario_id in manifest_scenario_ids)
		or manifest_scenario_ids != sorted(set(manifest_scenario_ids))
	):
		raise ValueError("Runtime Scenario Manifest denominator is invalid")
	value = _exact(
		value,
		{
			"blocking_causes",
			"cleanup_failures",
			"coverage",
			"cypress_diagnostic_log",
			"diagnostic_sampling",
			"discovery",
			"durations_ms",
			"environment",
			"run_id",
			"scenario_results",
			"schema_version",
			"site",
			"stale_recoveries",
			"status",
			"summary",
			"toolchain",
		},
		"machine report",
	)
	if value["schema_version"] != REPORT_SCHEMA_VERSION:
		raise ValueError("unsupported machine report schema")
	if not isinstance(value["run_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["run_id"]):
		raise ValueError("machine report run_id is invalid")
	if value["site"] != "development.localhost" or value["status"] not in {"fail", "pass"}:
		raise ValueError("machine report site or status is invalid")
	if not isinstance(value["diagnostic_sampling"], bool):
		raise ValueError("machine report diagnostic_sampling must be boolean")
	if value["cypress_diagnostic_log"] is not None and not isinstance(value["cypress_diagnostic_log"], str):
		raise ValueError("machine report Cypress diagnostic must be text or null")
	if value["toolchain"] is not None:
		_validate_toolchain(value["toolchain"])
	environment = value["environment"]
	if environment is not None:
		environment = _exact(
			environment,
			{"babel", "installed_apps", "inventory_digest", "mo_sha256", "python", "upstream"},
			"machine report environment",
		)
		if environment["installed_apps"] != ["frappe", "erpnext", "frappe_lt"]:
			raise ValueError("machine report environment installed apps are invalid")
		if not isinstance(environment["inventory_digest"], str) or not SHA256.fullmatch(
			environment["inventory_digest"]
		):
			raise ValueError("machine report environment inventory digest is invalid")
		if not isinstance(environment["mo_sha256"], str) or not SHA256.fullmatch(environment["mo_sha256"]):
			raise ValueError("machine report environment MO digest is invalid")
		if not all(
			isinstance(environment[field], str) and environment[field] for field in ("babel", "python")
		):
			raise ValueError("machine report environment tool versions are invalid")
		if not isinstance(environment["upstream"], dict) or list(environment["upstream"]) != [
			"erpnext",
			"frappe",
		]:
			raise ValueError("machine report environment upstream apps are invalid")
		for app, pin in environment["upstream"].items():
			pin = _exact(pin, {"commit", "version"}, f"machine report environment {app}")
			if (
				not isinstance(pin["commit"], str)
				or not re.fullmatch(r"[0-9a-f]{40}", pin["commit"])
				or not isinstance(pin["version"], str)
				or not pin["version"]
			):
				raise ValueError("machine report environment upstream pin is invalid")
	if value["status"] == "pass" and (environment is None or value["toolchain"] is None):
		raise ValueError("machine report pass requires verified environment and toolchain facts")

	discovery = _exact(value["discovery"], {"candidates", "collector_counts"}, "report discovery")
	if not isinstance(discovery["candidates"], list) or not isinstance(discovery["collector_counts"], dict):
		raise ValueError("machine report discovery is malformed")
	collector_names = set(discovery["collector_counts"])
	if collector_names not in (set(), {"email", "metadata", "portal", "print"}):
		raise ValueError("machine report collector count fields are malformed")
	if value["status"] == "pass" and collector_names != {"email", "metadata", "portal", "print"}:
		raise ValueError("machine report pass requires every discovery collector")
	candidate_ids = []
	for candidate in discovery["candidates"]:
		candidate = _exact(candidate, {"app", "id", "identity", "type"}, "report candidate")
		if (
			candidate["app"] not in {"erpnext", "frappe"}
			or not all(isinstance(candidate[field], str) and candidate[field] for field in candidate)
			or candidate["id"] != f"{candidate['type']}:{quote(candidate['identity'], safe='._-')}"
		):
			raise ValueError("machine report candidate is malformed")
		candidate_ids.append(candidate["id"])
	if candidate_ids != sorted(set(candidate_ids)):
		raise ValueError("machine report candidates must have unique canonical ids")
	if list(discovery["collector_counts"]) != sorted(discovery["collector_counts"]) or any(
		isinstance(count, bool) or not isinstance(count, int) or count < 1
		for count in discovery["collector_counts"].values()
	):
		raise ValueError("machine report collector counts are malformed")

	coverage_result = _exact(value["coverage"], {"covered", "gaps", "reviewed_exclusions"}, "report coverage")
	for field in ("covered", "gaps", "reviewed_exclusions"):
		items = coverage_result[field]
		if (
			not isinstance(items, list)
			or items != sorted(set(items))
			or any(not isinstance(item, str) or not item for item in items)
		):
			raise ValueError(f"machine report coverage {field} is malformed")
	coverage_groups = [set(coverage_result[field]) for field in coverage_result]
	coverage_partition_invalid = any(
		left & right for index, left in enumerate(coverage_groups) for right in coverage_groups[index + 1 :]
	) or set().union(*coverage_groups) != set(candidate_ids)

	results = value["scenario_results"]
	if not isinstance(results, list):
		raise ValueError("machine report scenario results must be a list")
	result_ids = []
	total_evidence = 0
	for result in results:
		result = _exact(
			result,
			{
				"attempts",
				"blocked_reason",
				"duration_ms",
				"error",
				"evidence",
				"fallbacks",
				"id",
				"layouts",
				"ready",
				"status",
			},
			"machine report scenario result",
		)
		if not isinstance(result["id"], str) or not result["id"]:
			raise ValueError("machine report scenario result id is invalid")
		if result["status"] not in {"blocked", "fail", "pass"} or not isinstance(result["ready"], bool):
			raise ValueError("machine report scenario result status is invalid")
		if (
			not isinstance(result["duration_ms"], int)
			or isinstance(result["duration_ms"], bool)
			or result["duration_ms"] < 0
			or (result["error"] is not None and not isinstance(result["error"], str))
			or (result["blocked_reason"] is not None and not isinstance(result["blocked_reason"], str))
		):
			raise ValueError("machine report scenario result duration is invalid")
		if not all(
			isinstance(result[field], list) for field in ("attempts", "evidence", "fallbacks", "layouts")
		):
			raise ValueError("machine report scenario result lists are malformed")
		_validate_attempts(result["attempts"], result["status"])
		for fallback in result["fallbacks"]:
			fallback = _exact(
				fallback,
				{
					"active",
					"effective",
					"excluded",
					"exclusion_id",
					"key",
					"raw_source",
					"render_status",
					"schema_version",
					"scenario_id",
					"source",
					"target",
					"visible",
				},
				"machine report lookup evidence",
			)
			if fallback["schema_version"] != 1:
				raise ValueError("machine report lookup evidence schema is invalid")
			key = _exact(fallback["key"], {"context", "source"}, "machine report Translation Key")
			target = _exact(fallback["target"], {"type", "value"}, "machine report render target")
			if (
				fallback["scenario_id"] != result["id"]
				or not all(isinstance(fallback[field], bool) for field in ("active", "excluded", "visible"))
				or not all(
					isinstance(fallback[field], str) for field in ("effective", "raw_source", "scenario_id")
				)
				or fallback["render_status"] not in {"ambiguous", "unique", "unrendered"}
				or fallback["source"]
				not in {"database", "erpnext", "frappe", "frappe_lt", "merged", "missing"}
				or not isinstance(key["source"], str)
				or not key["source"]
				or key["source"] != key["source"].strip()
				or fallback["raw_source"].strip() != key["source"]
				or (key["context"] is not None and not isinstance(key["context"], str))
				or fallback["visible"] != (fallback["render_status"] != "unrendered")
				or fallback["excluded"] != (fallback["exclusion_id"] is not None)
				or (fallback["exclusion_id"] is not None and not isinstance(fallback["exclusion_id"], str))
				or target["type"] not in {"locator", "output_interval"}
				or not isinstance(target["value"], str)
				or not target["value"]
			):
				raise ValueError("machine report lookup evidence is malformed")
		for layout in result["layouts"]:
			_validate_layout(layout, result["id"])
		for evidence in result["evidence"]:
			_validate_evidence_structure(evidence, result["id"])
		if len({evidence["path"] for evidence in result["evidence"]}) != len(result["evidence"]):
			raise ValueError(f"scenario {result['id']!r} contains duplicate evidence paths")
		scenario_evidence = sum(evidence["bytes"] for evidence in result["evidence"])
		if scenario_evidence > MAX_EVIDENCE_PER_SCENARIO:
			raise ValueError(f"scenario {result['id']!r} exceeds its evidence byte limit")
		total_evidence += scenario_evidence
		if result["status"] == "pass" and (
			not result["ready"]
			or result["error"] is not None
			or result["blocked_reason"] is not None
			or not any(finding["active"] for finding in result["fallbacks"])
			or any(_is_blocking_fallback(finding) for finding in result["fallbacks"])
			or any(_is_inactive_inventory_lookup(finding) for finding in result["fallbacks"])
			or any(_is_functional_layout(finding) for finding in result["layouts"])
		):
			raise ValueError("machine report pass result contains blocking facts")
		result_ids.append(result["id"])
	if result_ids != manifest_scenario_ids:
		raise ValueError("machine report scenario results must exactly match the Runtime Scenario Manifest")
	if total_evidence > MAX_EVIDENCE_PER_RUN:
		raise ValueError("runtime run exceeds its evidence byte limit")

	failures = value["cleanup_failures"]
	if not isinstance(failures, list):
		raise ValueError("machine report cleanup failures must be a list")
	for failure in failures:
		_validate_cleanup_failure(failure)

	if not isinstance(value["stale_recoveries"], list):
		raise ValueError("machine report stale recoveries must be a list")
	for recovery in value["stale_recoveries"]:
		recovery = _exact(
			recovery,
			{"cleanup_failures", "mutation_count", "original_error", "run_id"},
			"report stale recovery",
		)
		if not isinstance(recovery["cleanup_failures"], list):
			raise ValueError("machine report stale recovery is malformed")
		if (
			not isinstance(recovery["run_id"], str)
			or not re.fullmatch(r"[0-9a-f]{32}", recovery["run_id"])
			or isinstance(recovery["mutation_count"], bool)
			or not isinstance(recovery["mutation_count"], int)
			or recovery["mutation_count"] < 0
			or (recovery["original_error"] is not None and not isinstance(recovery["original_error"], str))
		):
			raise ValueError("machine report stale recovery is malformed")
		for failure in recovery["cleanup_failures"]:
			_validate_cleanup_failure(failure, "report stale cleanup failure")

	durations = _exact(
		value["durations_ms"], {"cleanup", "discovery", "preflight", "scenarios", "total"}, "report durations"
	)
	if any(
		isinstance(duration, bool) or not isinstance(duration, int) or duration < 0
		for duration in durations.values()
	):
		raise ValueError("machine report durations are malformed")

	summary = _exact(
		value["summary"],
		{
			"blocked",
			"cleanup_failures",
			"coverage_gaps",
			"english_fallbacks",
			"fail",
			"functional_layout_defects",
			"pass",
			"runtime_inventory_gaps",
			"total",
		},
		"machine report summary",
	)
	if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in summary.values()):
		raise ValueError("machine report summary counts are malformed")
	expected = _report_summary(results, coverage_result, failures)
	if any(summary[field] != count for field, count in expected.items()):
		raise ValueError("machine report summary does not match report facts")

	causes = value["blocking_causes"]
	if not isinstance(causes, list):
		raise ValueError("machine report blocking causes must be a list")
	for cause in causes:
		cause = _exact(cause, {"detail", "type"}, "report blocking cause")
		if cause["type"] not in {
			"cleanup_failure",
			"runtime_coverage_gap",
			"scenario_blocked",
			"scenario_fail",
			"tool_error",
		} or not all(isinstance(cause[field], str) and cause[field] for field in cause):
			raise ValueError("machine report blocking cause is malformed")
	if causes != sorted(causes, key=lambda cause: (cause["type"], cause["detail"])):
		raise ValueError("machine report blocking causes are not canonical")
	non_tool_causes = [cause for cause in causes if cause["type"] != "tool_error"]
	if non_tool_causes != _fact_blocking_causes(coverage_result, results, failures):
		raise ValueError("machine report blocking causes do not match report facts")
	if coverage_partition_invalid and not any(cause["type"] == "tool_error" for cause in causes):
		raise ValueError("machine report coverage must partition every discovered candidate")
	if (value["status"] == "fail") != bool(causes):
		raise ValueError("machine report status does not match blocking causes")
	return value


def _human_report(report: dict) -> str:
	lines = [
		"# Runtime validation report",
		"",
		f"- Run: `{report['run_id']}`",
		f"- Site: `{report['site']}`",
		f"- Result: **{report['status']}**",
		f"- Scenarios: {report['summary']['total']} total, {report['summary']['pass']} pass, "
		f"{report['summary']['fail']} fail, {report['summary']['blocked']} blocked",
		f"- Runtime Coverage Gaps: {report['summary']['coverage_gaps']}",
		f"- English Fallback findings: {report['summary']['english_fallbacks']}",
		f"- Runtime Inventory Gaps: {report['summary']['runtime_inventory_gaps']}",
		f"- Functional Layout Defects: {report['summary']['functional_layout_defects']}",
		f"- Runtime Cleanup Failures: {report['summary']['cleanup_failures']}",
		"",
		"## Scenarios",
		"",
		"| Scenario | Result | Duration (ms) |",
		"| --- | --- | ---: |",
	]
	for result in report["scenario_results"]:
		lines.append(f"| `{result['id']}` | {result['status']} | {result['duration_ms']} |")
	if report["blocking_causes"]:
		lines.extend(("", "## Blocking causes", ""))
		for cause in report["blocking_causes"]:
			lines.append(f"- `{cause['type']}`: {cause['detail']}")
	return "\n".join(lines) + "\n"


def _write_reports(run_root: Path, machine: bytes, human: bytes, replace=os.replace) -> None:
	"""Publish human output first and the machine report last as the run commit marker."""
	artifacts = {"runtime-report.json": machine, "runtime-report.md": human}
	temporary = {}
	published = []
	try:
		for name, content in sorted(artifacts.items()):
			fd, raw_path = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=run_root)
			path = Path(raw_path)
			with os.fdopen(fd, "wb") as stream:
				stream.write(content)
				stream.flush()
				os.fsync(stream.fileno())
			temporary[name] = path
		for name in ("runtime-report.md", "runtime-report.json"):
			target = run_root / name
			if target.is_symlink():
				raise ValueError(f"runtime report path uses a symlink: {name}")
			replace(temporary[name], target)
			published.append(target)
		directory_fd = os.open(run_root, os.O_RDONLY)
		try:
			os.fsync(directory_fd)
		finally:
			os.close(directory_fd)
	except Exception:
		for path in published:
			path.unlink(missing_ok=True)
		raise
	finally:
		for path in temporary.values():
			path.unlink(missing_ok=True)


def _build_report(
	*,
	run_id: str,
	site: str,
	environment: dict | None,
	discovery: dict,
	coverage_result: dict,
	results: list[dict],
	cleanup_failures: list[dict],
	stale_recoveries: list[dict],
	durations: dict,
	tool_errors: list[str],
	toolchain: dict | None = None,
	diagnostic_sampling: bool = False,
	cypress_diagnostic_log: str | None = None,
) -> dict:
	results = [
		{
			**result,
			"attempts": [
				{
					**attempt,
					"error": _redact(attempt["error"]) if attempt["error"] is not None else None,
				}
				for attempt in result["attempts"]
			],
			"blocked_reason": _redact(result["blocked_reason"])
			if result["blocked_reason"] is not None
			else None,
			"error": _redact(result["error"]) if result["error"] is not None else None,
			"fallbacks": [
				{
					**finding,
					"effective": redact_sensitive(finding["effective"], max_chars=256 * 1024),
					"key": {
						"context": redact_sensitive(finding["key"]["context"], max_chars=4096)
						if finding["key"]["context"] is not None
						else None,
						"source": redact_sensitive(finding["key"]["source"], max_chars=256 * 1024),
					},
					"raw_source": redact_sensitive(finding["raw_source"], max_chars=256 * 1024),
					"target": {
						**finding["target"],
						"value": redact_sensitive(finding["target"]["value"], max_chars=4096),
					},
				}
				for finding in result["fallbacks"]
			],
			"layouts": [
				{
					**finding,
					"detail": redact_sensitive(finding["detail"], max_chars=4096),
					"target": redact_sensitive(finding["target"], max_chars=4096),
				}
				for finding in result["layouts"]
			],
		}
		for result in results
	]
	cleanup_failures = [
		{
			**failure,
			"error": _redact(failure["error"]),
			"target": {**failure["target"], "name": _redact(failure["target"]["name"])},
		}
		for failure in cleanup_failures
	]
	stale_recoveries = [
		{
			**recovery,
			"cleanup_failures": [
				{
					**failure,
					"error": _redact(failure["error"]),
					"target": {
						**failure["target"],
						"name": _redact(failure["target"]["name"]),
					},
				}
				for failure in recovery["cleanup_failures"]
			],
			"original_error": _redact(recovery["original_error"])
			if recovery["original_error"] is not None
			else None,
		}
		for recovery in stale_recoveries
	]
	blocking_causes = [
		*({"detail": _redact(error), "type": "tool_error"} for error in tool_errors),
		*_fact_blocking_causes(coverage_result, results, cleanup_failures),
	]
	summary = _report_summary(results, coverage_result, cleanup_failures)
	report_environment = None
	if environment:
		report_environment = {
			"babel": environment["babel"],
			"installed_apps": environment["installed_apps"],
			"inventory_digest": environment["inventory_digest"],
			"mo_sha256": environment["mo_sha256"],
			"python": environment["python"],
			"upstream": {
				app: {"commit": value["commit"], "version": value["version"]}
				for app, value in sorted(environment["upstream"].items())
			},
		}
	return {
		"blocking_causes": sorted(blocking_causes, key=lambda cause: (cause["type"], cause["detail"])),
		"cleanup_failures": cleanup_failures,
		"coverage": coverage_result,
		"cypress_diagnostic_log": redact_sensitive(cypress_diagnostic_log, max_chars=65 * 1024)
		if cypress_diagnostic_log
		else None,
		"diagnostic_sampling": diagnostic_sampling,
		"discovery": discovery,
		"durations_ms": durations,
		"environment": report_environment,
		"run_id": run_id,
		"scenario_results": results,
		"schema_version": REPORT_SCHEMA_VERSION,
		"site": site,
		"stale_recoveries": stale_recoveries,
		"status": "fail" if blocking_causes else "pass",
		"summary": summary,
		"toolchain": toolchain,
	}


def run(
	site: str,
	output_dir: str | None = None,
	*,
	browser_runner=None,
	frappe_module=None,
	clock=time.monotonic,
	run_id: str | None = None,
	diagnostic_sampling: bool = False,
) -> dict:
	"""Run the reviewed runtime denominator and always attempt durable cleanup."""
	if site != "development.localhost":
		raise ValueError("runtime validation target must be development.localhost")
	if not isinstance(diagnostic_sampling, bool):
		raise ValueError("diagnostic_sampling must be boolean")
	frappe = frappe_module
	if frappe is None:
		import frappe as frappe_module

		frappe = frappe_module
	run_id = run_id or uuid.uuid4().hex
	if not re.fullmatch(r"[0-9a-f]{32}", run_id):
		raise ValueError("run_id must be 32 lowercase hexadecimal characters")
	if output_dir:
		run_root = Path(output_dir).resolve()
	else:
		run_root = Path(frappe.get_site_path("private", "frappe_lt_runtime", "reports", run_id)).resolve()
	run_root.mkdir(mode=0o700, parents=True, exist_ok=False)
	started = clock()
	durations = {"cleanup": 0, "discovery": 0, "preflight": 0, "scenarios": 0, "total": 0}
	environment = None
	discovery_result = {"candidates": [], "collector_counts": {}}
	coverage_result = {"covered": [], "gaps": [], "reviewed_exclusions": []}
	results = []
	cleanup_failures = []
	stale_recoveries = []
	tool_errors = []
	toolchain = None
	cypress_diagnostic_log = None
	contracts = None
	control = None
	try:
		preflight_started = clock()
		contracts = load_contracts()
		environment = verify_environment(
			frappe,
			site=site,
			require_clean_upstream=True,
			required_apps=("frappe", "erpnext", "frappe_lt"),
			require_exact_apps=True,
			require_active_catalog=True,
			require_runtime_metadata=True,
		)
		durations["preflight"] = int((clock() - preflight_started) * 1000)
		discovery_started = clock()
		discovery_result = discover(frappe)
		coverage_result = coverage(discovery_result, contracts["scenarios"], contracts["classifications"])
		durations["discovery"] = int((clock() - discovery_started) * 1000)
		control = SiteControl(frappe, site, run_id)
		with control.lease():
			cleanup_failures.extend(control.recover_stale())
			stale_recoveries.extend(control.recoveries)
			if cleanup_failures:
				raise RuntimeError("stale runtime run could not be fully recovered")
			control.start()
			try:
				prepared = control.prepare(
					contracts["profiles"],
					contracts["scenarios"],
					diagnostic_sampling=diagnostic_sampling,
				)
				scenarios_started = clock()
				browser_output = (browser_runner or _default_browser_runner)(
					site, run_root, Path(prepared["browser_plan"])
				)
				if isinstance(browser_output, tuple):
					browser, browser_exit, *browser_metadata = browser_output
					cypress_diagnostic_log = browser_metadata[0] if browser_metadata else None
				else:
					browser, browser_exit = browser_output, 0
				results = validate_browser_results(
					browser,
					contracts["scenarios"],
					run_root,
					diagnostic_sampling=diagnostic_sampling,
					diagnostic_key=prepared["evidence_key"],
				)
				toolchain = _validate_toolchain(browser["toolchain"])
				blocking_result = next((result for result in results if result["status"] != "pass"), None)
				original_error = None
				if browser_exit:
					original_error = f"Cypress exited {browser_exit}"
					tool_errors.append(original_error)
				else:
					cypress_diagnostic_log = None
				if not browser_exit and blocking_result:
					original_error = (
						blocking_result["error"]
						or blocking_result["blocked_reason"]
						or f"scenario {blocking_result['id']} failed with runtime findings"
					)
				if original_error:
					control.set_original_error(original_error)
				durations["scenarios"] = int((clock() - scenarios_started) * 1000)
			except Exception as error:
				if isinstance(error, BrowserRunnerError):
					cypress_diagnostic_log = error.diagnostic
				control.set_original_error(str(error))
				tool_errors.append(str(error))
				results = [
					blocked_result(scenario["id"], str(error))
					for scenario in contracts["scenarios"]["scenarios"]
				]
			finally:
				cleanup_started = clock()
				try:
					cleanup_failures.extend(control.cleanup())
				except Exception as error:
					cleanup_failures.append(
						{
							"error": str(error)[:2048],
							"mutation_id": 0,
							"target": {"doctype": "Runtime Control", "name": site},
						}
					)
				durations["cleanup"] = int((clock() - cleanup_started) * 1000)
	except Exception as error:
		tool_errors.append(str(error))
		if contracts and not results:
			results = [
				blocked_result(scenario["id"], str(error)) for scenario in contracts["scenarios"]["scenarios"]
			]
	durations["total"] = int((clock() - started) * 1000)
	report = _build_report(
		run_id=run_id,
		site=site,
		environment=environment,
		discovery=discovery_result,
		coverage_result=coverage_result,
		results=sorted(results, key=lambda result: result["id"]),
		cleanup_failures=sorted(
			cleanup_failures,
			key=lambda failure: (
				failure["target"]["doctype"],
				failure["target"]["name"],
				failure["mutation_id"],
			),
		),
		stale_recoveries=stale_recoveries,
		durations=durations,
		tool_errors=sorted(set(tool_errors)),
		toolchain=toolchain,
		diagnostic_sampling=diagnostic_sampling,
		cypress_diagnostic_log=cypress_diagnostic_log,
	)
	if contracts is None:
		raise ValueError("Runtime Scenario Manifest was not loaded")
	validate_machine_report(report, contracts["scenarios"])
	machine = canonical_json(report)
	if len(machine) > MAX_REPORT_BYTES:
		raise ValueError(f"runtime machine report exceeds {MAX_REPORT_BYTES} bytes")
	_write_reports(run_root, machine, _human_report(report).encode())
	return {
		"exit_code": 0 if report["status"] == "pass" else 1,
		"report": str(run_root / "runtime-report.json"),
		"run_id": run_id,
		"status": report["status"],
		"summary": report["summary"],
	}
