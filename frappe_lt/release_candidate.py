"""Thin release-candidate evidence capture and final publication boundary."""

import hashlib
import json
import math
import os
import re
import secrets
import stat
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from frappe_lt.catalog_quality import release_quality_summary
from frappe_lt.inventory import (
	canonical_json,
	clean_candidate_identity,
	verify_environment,
)
from frappe_lt.measurements import BASELINE_SITE, ENABLED_SITE
from frappe_lt.measurements import validate_report as validate_measurements
from frappe_lt.release_catalog import verify_release
from frappe_lt.review_evidence import CORRECTION_REASONS, REASONS
from frappe_lt.runtime_contracts import (
	CLASSIFICATIONS_PATH,
	ROLE_PROFILES_PATH,
	SCENARIOS_PATH,
	load_contracts,
)
from frappe_lt.runtime_control import assert_no_runtime_residue
from frappe_lt.runtime_discovery import coverage as runtime_coverage
from frappe_lt.runtime_discovery import discover as runtime_discover
from frappe_lt.runtime_discovery import runtime_site_state_digest, validate_candidate_snapshot
from frappe_lt.runtime_validation import MAX_EVIDENCE_PER_RUN, validate_machine_report

ROOT = Path(__file__).parent
CAPTURE_NAME = "candidate.json"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
CAPTURE_ARTIFACTS = {
	"frappe_lt/compatibility.json": ROOT / "compatibility.json",
	"frappe_lt/locale/lt.po": ROOT / "locale" / "lt.po",
	"frappe_lt/release_catalog.json": ROOT / "release_catalog.json",
	"frappe_lt/release_inventory.json": ROOT / "release_inventory.json",
	"frappe_lt/runtime_candidate_classifications.json": CLASSIFICATIONS_PATH,
	"frappe_lt/runtime_role_profiles.json": ROLE_PROFILES_PATH,
	"frappe_lt/runtime_scenarios.json": SCENARIOS_PATH,
}
MAX_CAPTURE_BYTES = 64 * 1024
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 64 * 1024 * 1024
MAX_FINAL_BYTES = 256 * 1024
INPUTS = tuple(
	sorted(
		(
			"candidate.json",
			"install-status.json",
			"install.json",
			"migration.json",
			"performance.json",
			"preflight.json",
			"prepare.json",
			"runtime-candidates.json",
			"runtime-residue.json",
			"runtime/runtime-report.json",
			"tests.json",
			"verify.json",
		)
	)
)
KNOWN_WARNING_CODES = frozenset({"UNKNOWN_V16_PATCH_EXACT_INVENTORY_MATCH"})
SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:%+-]{0,255}\Z")
MOBILE_MAX_WIDTH = 768
GITHUB_REPOSITORY = "lokysai/frappe-lt"
GITHUB_WORKFLOW = ".github/workflows/ci.yml"
MAX_GITHUB_RESPONSE_BYTES = 64 * 1024


def _exact(value, fields, label):
	if not isinstance(value, dict) or set(value) != set(fields):
		raise ValueError(f"{label} fields must be exactly {sorted(fields)}")
	return value


def _artifact(path: Path, relative: str) -> dict:
	path = Path(path).absolute()
	if any(part.is_symlink() for part in (path, *path.parents)):
		raise ValueError(f"release-candidate artifact is missing or unsafe: {relative}")
	try:
		descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
	except OSError as error:
		raise ValueError(f"release-candidate artifact is missing or unsafe: {relative}") from error
	try:
		before = os.fstat(descriptor)
		if not stat.S_ISREG(before.st_mode):
			raise ValueError(f"release-candidate artifact is missing or unsafe: {relative}")
		content = bytearray()
		while True:
			chunk = os.read(descriptor, 64 * 1024)
			if not chunk:
				break
			content.extend(chunk)
		after = os.fstat(descriptor)
		if (before.st_dev, before.st_ino, before.st_size) != (
			after.st_dev,
			after.st_ino,
			after.st_size,
		) or len(content) != before.st_size:
			raise ValueError(f"release-candidate artifact changed while being read: {relative}")
	finally:
		os.close(descriptor)
	content = bytes(content)
	return {
		"bytes": len(content),
		"path": relative,
		"sha256": hashlib.sha256(content).hexdigest(),
	}


def _candidate_root(frappe, root: Path, commit: str) -> Path:
	if not COMMIT.fullmatch(commit) or root.name != commit:
		raise ValueError("release-candidate directory must use the candidate SHA as its basename")
	expected = Path(frappe.get_site_path("private", "frappe_lt_release_candidate", commit)).absolute()
	actual = root.absolute()
	if actual != expected:
		raise ValueError("release-candidate directory is outside the active site's private candidate root")
	for path in (expected.parent.parent, expected.parent, expected):
		if path.is_symlink():
			raise ValueError("release-candidate directory contains a symlink")
	return expected


def _validate_candidate(value, label="candidate"):
	value = _exact(value, {"clean", "commit"}, label)
	if (
		value["clean"] is not True
		or not isinstance(value["commit"], str)
		or not COMMIT.fullmatch(value["commit"])
	):
		raise ValueError(f"{label} must identify one clean full commit")
	return value


def _validate_digest(value, label):
	if not isinstance(value, str) or not SHA256.fullmatch(value):
		raise ValueError(f"{label} must be a SHA-256 digest")
	return value


def validate_capture(value: object) -> dict:
	value = _exact(
		value,
		{
			"artifacts",
			"candidate",
			"environment",
			"quality",
			"release",
			"runtime_contracts",
			"schema_version",
			"site",
		},
		"release-candidate capture",
	)
	if value["schema_version"] != 1 or value["site"] != "development.localhost":
		raise ValueError("release-candidate capture identity is invalid")
	_validate_candidate(value["candidate"])
	environment = _exact(
		value["environment"], {"babel", "installed_apps", "python", "upstream"}, "capture environment"
	)
	if environment["installed_apps"] != ["frappe", "erpnext"]:
		raise ValueError("capture must precede frappe_lt site installation")
	if not all(isinstance(environment[field], str) and environment[field] for field in ("babel", "python")):
		raise ValueError("capture tool versions are invalid")
	upstream = _exact(environment["upstream"], {"erpnext", "frappe"}, "capture upstream")
	for app, pin in upstream.items():
		pin = _exact(pin, {"commit", "version"}, f"capture {app} pin")
		if not isinstance(pin["commit"], str) or not COMMIT.fullmatch(pin["commit"]):
			raise ValueError(f"capture {app} commit is invalid")
		if not isinstance(pin["version"], str) or not pin["version"]:
			raise ValueError(f"capture {app} version is invalid")
	release = _exact(value["release"], {"inventory_digest", "mo_sha256", "release_digest"}, "capture release")
	if any(not isinstance(digest, str) or not SHA256.fullmatch(digest) for digest in release.values()):
		raise ValueError("capture release digests are invalid")
	quality = _exact(
		value["quality"],
		{"corrected_inherited", "reason_counts", "translation_coverage", "translation_exceptions"},
		"capture quality",
	)
	counts = _exact(quality["reason_counts"], set(REASONS), "capture quality reason counts")
	coverage = _exact(quality["translation_coverage"], {"covered", "total"}, "capture coverage")
	if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts.values()):
		raise ValueError("capture quality reason counts are invalid")
	if any(
		isinstance(quality[field], bool) or not isinstance(quality[field], int) or quality[field] < 0
		for field in ("corrected_inherited", "translation_exceptions")
	) or any(
		isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in coverage.values()
	):
		raise ValueError("capture quality totals are invalid")
	if coverage["covered"] != coverage["total"] or sum(counts.values()) != coverage["total"]:
		raise ValueError("release candidate must have complete authenticated Translation Coverage")
	if quality["translation_exceptions"] != counts["approved_translation_exception"]:
		raise ValueError("capture Translation Exception total is inconsistent")
	if quality["corrected_inherited"] != sum(counts[reason] for reason in CORRECTION_REASONS):
		raise ValueError("capture corrected inherited total is inconsistent")
	artifacts = value["artifacts"]
	if not isinstance(artifacts, list) or len(artifacts) != len(CAPTURE_ARTIFACTS):
		raise ValueError("capture artifact index is incomplete")
	paths = []
	for item in artifacts:
		item = _exact(item, {"bytes", "path", "sha256"}, "capture artifact")
		path = item["path"]
		if (
			not isinstance(path, str)
			or path not in CAPTURE_ARTIFACTS
			or Path(path).is_absolute()
			or ".." in Path(path).parts
			or isinstance(item["bytes"], bool)
			or not isinstance(item["bytes"], int)
			or item["bytes"] < 1
			or not isinstance(item["sha256"], str)
			or not SHA256.fullmatch(item["sha256"])
		):
			raise ValueError("capture artifact reference is invalid")
		paths.append(path)
	if paths != sorted(CAPTURE_ARTIFACTS):
		raise ValueError("capture artifact index must use exact canonical paths")
	contracts = _exact(
		value["runtime_contracts"], {"classifications", "role_profiles", "scenarios"}, "runtime contracts"
	)
	expected_contracts = {
		"classifications": "frappe_lt/runtime_candidate_classifications.json",
		"role_profiles": "frappe_lt/runtime_role_profiles.json",
		"scenarios": "frappe_lt/runtime_scenarios.json",
	}
	by_path = {item["path"]: item["sha256"] for item in artifacts}
	if (
		by_path["frappe_lt/release_catalog.json"] != release["release_digest"]
		or by_path["frappe_lt/release_inventory.json"] != release["inventory_digest"]
	):
		raise ValueError("captured release artifacts do not match authenticated release digests")
	if contracts != {name: by_path[path] for name, path in expected_contracts.items()}:
		raise ValueError("runtime contract digests do not match the artifact index")
	return value


def _publish(path: Path, content: bytes, link=os.link) -> None:
	flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
	try:
		directory_fd = os.open(path.parent, flags)
	except OSError as error:
		raise ValueError("release-candidate publication directory is unsafe") from error
	temporary = f".{path.name}.{secrets.token_hex(16)}.tmp"
	try:
		try:
			os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
		except FileNotFoundError:
			pass
		else:
			raise ValueError(f"release-candidate evidence already exists: {path.name}")
		fd = os.open(
			temporary,
			os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
			0o600,
			dir_fd=directory_fd,
		)
		with os.fdopen(fd, "wb") as stream:
			stream.write(content)
			stream.flush()
			os.fsync(stream.fileno())
		link(
			temporary,
			path.name,
			src_dir_fd=directory_fd,
			dst_dir_fd=directory_fd,
			follow_symlinks=False,
		)
		os.fsync(directory_fd)
	finally:
		try:
			os.unlink(temporary, dir_fd=directory_fd)
		except FileNotFoundError:
			pass
		os.close(directory_fd)


def capture(site: str, output_dir: str | Path, *, frappe_module=None) -> dict:
	"""Select and publish one clean release candidate before site mutation."""
	if site != "development.localhost":
		raise ValueError("release-candidate target must be development.localhost")
	output_dir = Path(output_dir)
	if output_dir.is_symlink() or not output_dir.is_dir():
		raise ValueError("release-candidate output directory must be an existing non-symlink directory")
	frappe = frappe_module
	if frappe is None:
		import frappe as frappe_module

		frappe = frappe_module
	environment = verify_environment(
		frappe,
		site=site,
		require_clean_upstream=True,
		required_apps=("frappe", "erpnext"),
		require_exact_apps=True,
		require_runtime_metadata=True,
	)
	release = verify_release()
	if environment["inventory_digest"] != release["inventory_digest"]:
		raise ValueError("capture environment differs from the authenticated release")
	load_contracts()
	repository = ROOT.parent
	candidate = clean_candidate_identity(repository)
	commit = candidate["commit"]
	_candidate_root(frappe, output_dir, commit)
	artifacts = [_artifact(path, name) for name, path in sorted(CAPTURE_ARTIFACTS.items())]
	by_path = {item["path"]: item["sha256"] for item in artifacts}
	value = validate_capture(
		{
			"artifacts": artifacts,
			"candidate": candidate,
			"environment": {
				"babel": environment["babel"],
				"installed_apps": environment["installed_apps"],
				"python": environment["python"],
				"upstream": {
					app: {"commit": pin["commit"], "version": pin["version"]}
					for app, pin in sorted(environment["upstream"].items())
				},
			},
			"quality": release_quality_summary(),
			"release": release,
			"runtime_contracts": {
				"classifications": by_path["frappe_lt/runtime_candidate_classifications.json"],
				"role_profiles": by_path["frappe_lt/runtime_role_profiles.json"],
				"scenarios": by_path["frappe_lt/runtime_scenarios.json"],
			},
			"schema_version": 1,
			"site": site,
		}
	)
	content = canonical_json(value)
	if len(content) > MAX_CAPTURE_BYTES:
		raise ValueError(f"release-candidate capture exceeds {MAX_CAPTURE_BYTES} bytes")
	path = output_dir / CAPTURE_NAME
	_publish(path, content)
	return {"output": str(path), "sha256": hashlib.sha256(content).hexdigest()}


def _read_bytes(root_descriptor: int, relative: str, *, maximum=MAX_INPUT_BYTES, allow_empty=False):
	path = Path(relative)
	if path.is_absolute() or not path.parts or ".." in path.parts:
		raise ValueError(f"release-candidate input is missing or unsafe: {relative}")
	try:
		with ExitStack() as descriptors:
			directory = os.dup(root_descriptor)
			descriptors.callback(os.close, directory)
			directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
			for component in path.parts[:-1]:
				directory = os.open(component, directory_flags, dir_fd=directory)
				descriptors.callback(os.close, directory)
			descriptor = os.open(
				path.parts[-1],
				os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
				dir_fd=directory,
			)
			descriptors.callback(os.close, descriptor)
			before = os.fstat(descriptor)
			if (
				not stat.S_ISREG(before.st_mode)
				or before.st_size > maximum
				or (before.st_size == 0 and not allow_empty)
			):
				raise ValueError(f"release-candidate input has an invalid byte size: {relative}")
			content = bytearray()
			while len(content) <= maximum:
				chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(content)))
				if not chunk:
					break
				content.extend(chunk)
			after = os.fstat(descriptor)
			if (before.st_dev, before.st_ino, before.st_size) != (
				after.st_dev,
				after.st_ino,
				after.st_size,
			) or len(content) != before.st_size:
				raise ValueError(f"release-candidate input changed while being read: {relative}")
	except OSError as error:
		raise ValueError(f"release-candidate input is missing or unsafe: {relative}") from error
	return bytes(content)


def _read_input(root_descriptor: int, relative: str):
	content = _read_bytes(root_descriptor, relative)
	try:
		value = json.loads(
			content,
			object_pairs_hook=lambda pairs: _unique_object(pairs, relative),
			parse_constant=lambda constant: (_ for _ in ()).throw(
				ValueError(f"non-finite value in {relative}: {constant}")
			),
		)
	except (UnicodeDecodeError, json.JSONDecodeError) as error:
		raise ValueError(f"release-candidate input is not valid JSON: {relative}") from error
	return value, content


def _unique_object(pairs, label):
	value = {}
	for key, item in pairs:
		if key in value:
			raise ValueError(f"duplicate JSON key in {label}: {key}")
		value[key] = item
	return value


def _validate_preflight(value, capture):
	value = _exact(
		value,
		{
			"inventory_digest",
			"migration",
			"mo_sha256",
			"release_digest",
			"site",
			"state",
			"versions",
			"warnings",
		},
		"install preflight evidence",
	)
	if value["site"] != capture["site"] or value["state"] != "ready":
		raise ValueError("install preflight is not green for the selected site")
	if {field: value[field] for field in ("inventory_digest", "mo_sha256", "release_digest")} != capture[
		"release"
	]:
		raise ValueError("install preflight release differs from candidate capture")
	if value["versions"] != {app: pin["version"] for app, pin in capture["environment"]["upstream"].items()}:
		raise ValueError("install preflight versions differ from candidate capture")
	if (
		not isinstance(value["warnings"], list)
		or value["warnings"] != sorted(set(value["warnings"]))
		or any(item not in KNOWN_WARNING_CODES for item in value["warnings"])
	):
		raise ValueError("install preflight warnings are invalid")
	migration = _exact(value["migration"], {"delete", "extras", "overrides"}, "migration summary")
	if any(
		isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in migration.values()
	):
		raise ValueError("migration summary counts are invalid")
	return value


def _validate_install(install, status, site):
	install = _exact(install, {"maintenance_mode", "run_id", "site", "state"}, "install evidence")
	status = _exact(
		status,
		{"installed", "maintenance_mode", "migration", "mo", "profile", "run_id", "site"},
		"install status evidence",
	)
	if (
		install["site"] != site
		or install["state"] != "verified"
		or install["maintenance_mode"] != 1
		or status["site"] != site
		or status["installed"] is not True
		or status["maintenance_mode"] is not True
		or status["migration"] not in {"committed", "no_op"}
		or status["mo"] != "matched"
		or status["profile"] != "APPLIED"
		or status["run_id"] != install["run_id"]
		or not isinstance(install["run_id"], str)
		or re.fullmatch(r"[0-9a-f]{32}", install["run_id"]) is None
	):
		raise ValueError("installation is not verified inside the maintenance boundary")
	return install, status


def _validate_prepare(value, preflight):
	value = _exact(value, set(preflight) | {"run_id"}, "install prepare evidence")
	if (
		{field: value[field] for field in preflight} != {**preflight, "state": "prepared"}
		or not isinstance(value["run_id"], str)
		or re.fullmatch(r"[0-9a-f]{32}", value["run_id"]) is None
	):
		raise ValueError("install prepare evidence differs from its green preflight")
	return value


def _validate_verify(value, capture):
	value = _exact(
		value,
		{
			"effective_translation",
			"inventory_digest",
			"mo_bytes",
			"mo_sha256",
			"release_digest",
			"site",
			"state",
		},
		"verification evidence",
	)
	if (
		value["site"] != capture["site"]
		or value["state"] != "verified"
		or value["effective_translation"] != "Prekė"
		or isinstance(value["mo_bytes"], bool)
		or not isinstance(value["mo_bytes"], int)
		or value["mo_bytes"] < 1
		or {field: value[field] for field in ("inventory_digest", "mo_sha256", "release_digest")}
		!= capture["release"]
	):
		raise ValueError("full-catalog verification does not match the selected release")
	return value


def _fetch_github_json(url, *, timeout=5):
	if not isinstance(url, str) or not url.startswith("https://api.github.com/"):
		raise ValueError("GitHub Actions API URL is invalid")
	request = Request(
		url,
		headers={
			"Accept": "application/vnd.github+json",
			"User-Agent": "frappe-lt-release-candidate",
			"X-GitHub-Api-Version": "2022-11-28",
		},
	)
	try:
		with urlopen(request, timeout=timeout) as response:
			if response.geturl() != url:
				raise ValueError("GitHub Actions API redirected unexpectedly")
			content = response.read(MAX_GITHUB_RESPONSE_BYTES + 1)
	except (OSError, TimeoutError) as error:
		raise ValueError("GitHub Actions API is unavailable") from error
	if len(content) > MAX_GITHUB_RESPONSE_BYTES:
		raise ValueError("GitHub Actions API response exceeds its byte limit")
	try:
		return json.loads(content, object_pairs_hook=lambda pairs: _unique_object(pairs, "GitHub response"))
	except (UnicodeDecodeError, json.JSONDecodeError) as error:
		raise ValueError("GitHub Actions API response is invalid JSON") from error


def _validate_tests(value, candidate, *, fetch_json=None, verified=None):
	value = _exact(
		value,
		{"candidate", "categories", "provenance", "schema_version"},
		"existing test evidence",
	)
	if (
		value["schema_version"] != 2
		or _validate_candidate(value["candidate"], "existing test candidate") != candidate
	):
		raise ValueError("existing test evidence does not match the selected candidate")
	categories = _exact(value["categories"], {"fault", "integration", "subprocess"}, "test categories")
	if any(state != "pass" for state in categories.values()):
		raise ValueError("existing test evidence is not green")
	provenance = _exact(
		value["provenance"],
		{"repository", "run_attempt", "run_id", "workflow"},
		"existing test provenance",
	)
	if (
		provenance["repository"] != GITHUB_REPOSITORY
		or provenance["workflow"] != GITHUB_WORKFLOW
		or isinstance(provenance["run_id"], bool)
		or not isinstance(provenance["run_id"], int)
		or provenance["run_id"] < 1
		or isinstance(provenance["run_attempt"], bool)
		or not isinstance(provenance["run_attempt"], int)
		or provenance["run_attempt"] < 1
	):
		raise ValueError("existing test GitHub provenance is invalid")
	api_url = (
		f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/runs/"
		f"{provenance['run_id']}/attempts/{provenance['run_attempt']}"
	)
	artifact_name = f"candidate-tests-{candidate['commit']}-{provenance['run_attempt']}"
	artifact_url = (
		f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/runs/"
		f"{provenance['run_id']}/artifacts?name={quote(artifact_name)}"
	)
	if verified is None:
		fetch = fetch_json or _fetch_github_json
		response = fetch(api_url)
		if not isinstance(response, dict):
			raise ValueError("GitHub Actions API response is invalid")
		repository = response.get("repository")
		run_url = f"https://github.com/{GITHUB_REPOSITORY}/actions/runs/{provenance['run_id']}"
		if (
			response.get("id") != provenance["run_id"]
			or response.get("run_attempt") != provenance["run_attempt"]
			or response.get("path") != GITHUB_WORKFLOW
			or response.get("head_sha") != candidate["commit"]
			or response.get("conclusion") != "success"
			or not isinstance(repository, dict)
			or repository.get("full_name") != GITHUB_REPOSITORY
			or response.get("html_url") != run_url
		):
			raise ValueError("GitHub Actions run does not authenticate the selected candidate")
		artifact_response = fetch(artifact_url)
		artifacts = artifact_response.get("artifacts") if isinstance(artifact_response, dict) else None
		if (
			not isinstance(artifact_response, dict)
			or artifact_response.get("total_count") != 1
			or not isinstance(artifacts, list)
			or len(artifacts) != 1
			or not isinstance(artifacts[0], dict)
			or artifacts[0].get("name") != artifact_name
			or artifacts[0].get("expired") is not False
			or isinstance(artifacts[0].get("id"), bool)
			or not isinstance(artifacts[0].get("id"), int)
			or artifacts[0]["id"] < 1
			or not isinstance(artifacts[0].get("workflow_run"), dict)
			or artifacts[0]["workflow_run"].get("id") != provenance["run_id"]
			or artifacts[0]["workflow_run"].get("head_sha") != candidate["commit"]
		):
			raise ValueError("GitHub Actions test artifact does not authenticate the selected candidate")
		verified = {
			"artifact": artifact_name,
			"run_attempt": provenance["run_attempt"],
			"run_id": provenance["run_id"],
			"url": run_url,
		}
	elif verified != {
		"artifact": artifact_name,
		"run_attempt": provenance["run_attempt"],
		"run_id": provenance["run_id"],
		"url": f"https://github.com/{GITHUB_REPOSITORY}/actions/runs/{provenance['run_id']}",
	}:
		raise ValueError("cached GitHub Actions verification differs from test evidence")
	return value, verified


def _migration_summary(value, install_status, preflight):
	expected = {
		"deleted": preflight["migration"]["delete"],
		"package_sha256": __import__(
			"frappe_lt.legacy_migration", fromlist=["PACKAGE_SHA256"]
		).PACKAGE_SHA256,
		"postcommit_drift": False,
		"run_id": install_status["run_id"],
		"site": install_status["site"],
		"state": "committed",
	}
	if value != expected:
		raise ValueError("legacy migration final report is stale or inconsistent")
	content = canonical_json(value)
	return {
		"deleted": value["deleted"],
		"package_sha256": value["package_sha256"],
		"postcommit_drift": False,
		"run_id": value["run_id"],
		"sha256": hashlib.sha256(content).hexdigest(),
		"state": value["state"],
	}


def _validate_migration_final(frappe, install_status, preflight, supplied):
	from frappe_lt import legacy_migration

	run_id = install_status["run_id"]
	path = Path(frappe.get_site_path("private")) / "frappe_lt_legacy_migration" / f"{run_id}.final.json"
	value = legacy_migration._read_private(path)
	if value != supplied:
		raise ValueError("rooted migration report differs from the authoritative private report")
	return _migration_summary(value, install_status, preflight)


def _human_report(report):
	lines = [
		"# Release candidate validation",
		"",
		f"- Candidate: `{report['candidate']['commit']}`",
		f"- Result: **{report['status']}**",
		f"- Translation Coverage: {report['quality']['translation_coverage']['covered']}/"
		f"{report['quality']['translation_coverage']['total']}",
		f"- Desktop scenarios: {report['summary']['desktop']}",
		f"- Mobile scenarios: {report['summary']['mobile']}",
		f"- English Fallback: {report['summary']['english_fallbacks']}",
		f"- Preserved Token failures: {report['summary']['preserved_token_failures']}",
		f"- Functional Layout Defects: {report['summary']['functional_layout_defects']}",
		f"- Runtime residue: {report['summary']['runtime_residue']}",
		f"- Paired warm p95 overhead: {report['performance']['paired_warm_p95_overhead_ms']} ms",
		"",
	]
	return "\n".join(lines)


def _validate_final_index(
	value,
	*,
	root: Path,
	root_descriptor: int | None = None,
	site_exception_path: str | None = None,
	github_fetch=None,
	verified_tests=None,
):
	value = _exact(
		value,
		{
			"artifacts",
			"candidate",
			"install",
			"migration",
			"performance",
			"preflight",
			"quality",
			"release",
			"runtime",
			"runtime_contracts",
			"schema_version",
			"site",
			"status",
			"summary",
			"tests",
			"upstream",
			"verification",
		},
		"release-candidate machine index",
	)
	if value["schema_version"] != 1 or value["site"] != "development.localhost" or value["status"] != "pass":
		raise ValueError("release-candidate machine index is not a successful supported schema")
	_validate_candidate(value["candidate"], "release-candidate selected commit")
	install = _exact(
		value["install"],
		{"maintenance_mode", "migration", "run_id", "state"},
		"release-candidate install",
	)
	if (
		install["maintenance_mode"] is not True
		or install["migration"] not in {"committed", "no_op"}
		or install["state"] != "verified"
		or not isinstance(install["run_id"], str)
		or re.fullmatch(r"[0-9a-f]{32}", install["run_id"]) is None
	):
		raise ValueError("release-candidate install is not green")
	migration = _exact(
		value["migration"],
		{"deleted", "package_sha256", "postcommit_drift", "run_id", "sha256", "state"},
		"release-candidate migration",
	)
	if (
		isinstance(migration["deleted"], bool)
		or not isinstance(migration["deleted"], int)
		or migration["deleted"] < 0
		or migration["postcommit_drift"] is not False
		or migration["run_id"] != install["run_id"]
		or migration["state"] != "committed"
	):
		raise ValueError("release-candidate migration is not committed without drift")
	_validate_digest(migration["package_sha256"], "release-candidate migration package")
	_validate_digest(migration["sha256"], "release-candidate migration report")
	from frappe_lt.legacy_migration import PACKAGE_SHA256

	if migration["package_sha256"] != PACKAGE_SHA256:
		raise ValueError("release-candidate migration package is not authenticated")
	performance = _exact(
		value["performance"],
		{"compiled_mo_bytes", "limit_bytes", "paired_warm_p95_overhead_ms", "shipped_bytes"},
		"release-candidate performance",
	)
	if (
		isinstance(performance["paired_warm_p95_overhead_ms"], bool)
		or not isinstance(performance["paired_warm_p95_overhead_ms"], (int, float))
		or not math.isfinite(performance["paired_warm_p95_overhead_ms"])
		or performance["paired_warm_p95_overhead_ms"] >= 50
		or isinstance(performance["compiled_mo_bytes"], bool)
		or not isinstance(performance["compiled_mo_bytes"], int)
		or performance["compiled_mo_bytes"] < 1
		or isinstance(performance["shipped_bytes"], bool)
		or not isinstance(performance["shipped_bytes"], int)
		or performance["shipped_bytes"] < 1
		or performance["limit_bytes"] != 35 * 1024 * 1024
		or performance["shipped_bytes"] > performance["limit_bytes"]
	):
		raise ValueError("release-candidate performance is not finite and green")
	preflight = _exact(
		value["preflight"],
		{"migration", "warning_codes", "warning_count"},
		"release-candidate preflight",
	)
	warnings = preflight["warning_codes"]
	if (
		not isinstance(warnings, list)
		or warnings != sorted(set(warnings))
		or any(code not in KNOWN_WARNING_CODES for code in warnings)
		or preflight["warning_count"] != len(warnings)
	):
		raise ValueError("release-candidate preflight warning codes are invalid")
	migration_counts = _exact(preflight["migration"], {"delete", "extras", "overrides"}, "migration counts")
	if any(
		isinstance(count, bool) or not isinstance(count, int) or count < 0
		for count in migration_counts.values()
	):
		raise ValueError("release-candidate migration counts are invalid")
	if migration["deleted"] != migration_counts["delete"]:
		raise ValueError("release-candidate migration deletion count is inconsistent")
	quality = _exact(
		value["quality"],
		{"corrected_inherited", "reason_counts", "translation_coverage", "translation_exceptions"},
		"release-candidate quality",
	)
	counts = _exact(quality["reason_counts"], set(REASONS), "release-candidate reason counts")
	coverage = _exact(quality["translation_coverage"], {"covered", "total"}, "release-candidate coverage")
	if any(
		isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts.values()
	) or any(
		isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in coverage.values()
	):
		raise ValueError("release-candidate quality counts are invalid")
	if (
		coverage["total"] < 1
		or coverage["covered"] != coverage["total"]
		or sum(counts.values()) != coverage["total"]
		or quality["translation_exceptions"] != counts["approved_translation_exception"]
		or quality["corrected_inherited"] != sum(counts[reason] for reason in CORRECTION_REASONS)
		or isinstance(quality["corrected_inherited"], bool)
		or not isinstance(quality["corrected_inherited"], int)
		or not 0 <= quality["corrected_inherited"] <= coverage["total"]
	):
		raise ValueError("release-candidate quality invariant is invalid")
	release = _exact(
		value["release"], {"inventory_digest", "mo_sha256", "release_digest"}, "release-candidate release"
	)
	for name, digest in release.items():
		_validate_digest(digest, f"release-candidate {name}")
	contracts = _exact(
		value["runtime_contracts"],
		{"classifications", "role_profiles", "scenarios"},
		"release-candidate runtime contracts",
	)
	for name, digest in contracts.items():
		_validate_digest(digest, f"release-candidate runtime {name}")
	contract_bundle = load_contracts()
	manifest = contract_bundle["scenarios"]["scenarios"]
	manifest_ids = [scenario["id"] for scenario in manifest]
	if not manifest_ids:
		raise ValueError("release-candidate Runtime Scenario denominator is empty")
	runtime = _exact(
		value["runtime"], {"candidate_count", "run_id", "scenarios"}, "release-candidate runtime"
	)
	if (
		isinstance(runtime["candidate_count"], bool)
		or not isinstance(runtime["candidate_count"], int)
		or runtime["candidate_count"] < 1
		or not isinstance(runtime["run_id"], str)
		or re.fullmatch(r"[0-9a-f]{32}", runtime["run_id"]) is None
		or not isinstance(runtime["scenarios"], list)
	):
		raise ValueError("release-candidate runtime identity is invalid")
	for scenario in runtime["scenarios"]:
		scenario = _exact(scenario, {"id", "status"}, "release-candidate runtime scenario")
		if (
			not isinstance(scenario["id"], str)
			or SAFE_ID.fullmatch(scenario["id"]) is None
			or scenario["status"] != "pass"
		):
			raise ValueError("release-candidate runtime scenario is not green")
	if [scenario["id"] for scenario in runtime["scenarios"]] != manifest_ids:
		raise ValueError("release-candidate runtime scenarios do not equal the complete manifest denominator")
	upstream = _exact(value["upstream"], {"erpnext", "frappe"}, "release-candidate upstream")
	for app, pin in upstream.items():
		pin = _exact(pin, {"commit", "version"}, f"release-candidate {app} pin")
		if (
			not isinstance(pin["commit"], str)
			or not COMMIT.fullmatch(pin["commit"])
			or not isinstance(pin["version"], str)
			or SAFE_VERSION.fullmatch(pin["version"]) is None
		):
			raise ValueError(f"release-candidate {app} pin is invalid")
	verification = _exact(
		value["verification"],
		{"effective_translation", "mo_bytes", "state"},
		"release-candidate verification",
	)
	if verification != {
		"effective_translation": "Prekė",
		"mo_bytes": performance["compiled_mo_bytes"],
		"state": "verified",
	}:
		raise ValueError("release-candidate verification is not green")
	tests = _exact(
		value["tests"],
		{"artifact", "categories", "run_attempt", "run_id", "source", "url"},
		"release-candidate tests",
	)
	if (
		tests["artifact"] != "tests.json"
		or tests["source"] != (f"candidate-tests-{value['candidate']['commit']}-{tests['run_attempt']}")
		or _exact(
			tests["categories"], {"fault", "integration", "subprocess"}, "release-candidate test summary"
		)
		!= {"fault": "pass", "integration": "pass", "subprocess": "pass"}
	):
		raise ValueError("release-candidate existing tests are not green")
	if (
		isinstance(tests["run_id"], bool)
		or not isinstance(tests["run_id"], int)
		or tests["run_id"] < 1
		or isinstance(tests["run_attempt"], bool)
		or not isinstance(tests["run_attempt"], int)
		or tests["run_attempt"] < 1
		or tests["url"] != f"https://github.com/{GITHUB_REPOSITORY}/actions/runs/{tests['run_id']}"
	):
		raise ValueError("release-candidate test run reference is invalid")
	artifacts = value["artifacts"]
	if not isinstance(artifacts, list):
		raise ValueError("release-candidate machine artifact index is invalid")
	paths = [item.get("path") for item in artifacts if isinstance(item, dict)]
	if len(paths) != len(artifacts) or paths != sorted(set(paths)) or not set(INPUTS) <= set(paths):
		raise ValueError("release-candidate machine artifact index is incomplete or noncanonical")
	allowed_evidence_paths = {
		f"runtime/evidence/{scenario_id}/{name}"
		for scenario_id in manifest_ids
		for name in ("browser.json", "email.txt", "portal.html", "print.html")
	}
	if any(path not in INPUTS and path not in allowed_evidence_paths for path in paths):
		raise ValueError("release-candidate machine artifact path is not structurally allowlisted")
	for item in artifacts:
		item = _exact(item, {"bytes", "path", "sha256"}, "release-candidate artifact reference")
		if (
			Path(item["path"]).is_absolute()
			or ".." in Path(item["path"]).parts
			or isinstance(item["bytes"], bool)
			or not isinstance(item["bytes"], int)
			or item["bytes"] < 1
			or not isinstance(item["sha256"], str)
			or not SHA256.fullmatch(item["sha256"])
		):
			raise ValueError("release-candidate artifact reference is invalid")
	summary = _exact(
		value["summary"],
		{
			"cleanup_failures",
			"desktop",
			"english_fallbacks",
			"functional_layout_defects",
			"mobile",
			"preserved_token_failures",
			"routes",
			"runtime_residue",
		},
		"release-candidate summary",
	)
	if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in summary.values()):
		raise ValueError("release-candidate summary counts are invalid")
	expected_summary = {
		"cleanup_failures": 0,
		"desktop": sum(scenario["viewport"]["width"] > MOBILE_MAX_WIDTH for scenario in manifest),
		"english_fallbacks": 0,
		"functional_layout_defects": 0,
		"mobile": sum(scenario["viewport"]["width"] <= MOBILE_MAX_WIDTH for scenario in manifest),
		"preserved_token_failures": 0,
		"routes": runtime["candidate_count"],
		"runtime_residue": 0,
	}
	if summary != expected_summary:
		raise ValueError("release-candidate summary does not exactly match its authenticated facts")
	if root is not None:
		root = Path(root)
		flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
		close_root = root_descriptor is None
		if close_root:
			try:
				root_descriptor = os.open(root, flags)
			except OSError as error:
				raise ValueError("release-candidate artifact root is unsafe") from error
		try:
			rooted = {relative: _read_input(root_descriptor, relative)[0] for relative in INPUTS}
			capture = validate_capture(rooted["candidate.json"])
			source_preflight = _validate_preflight(rooted["preflight.json"], capture)
			source_prepare = _validate_prepare(rooted["prepare.json"], source_preflight)
			source_install, source_install_status = _validate_install(
				rooted["install.json"], rooted["install-status.json"], capture["site"]
			)
			if source_prepare["run_id"] != source_install["run_id"]:
				raise ValueError("rooted install and prepare runs differ")
			source_migration = _migration_summary(
				rooted["migration.json"], source_install_status, source_preflight
			)
			source_verify = _validate_verify(rooted["verify.json"], capture)
			source_tests, verified_tests = _validate_tests(
				rooted["tests.json"],
				capture["candidate"],
				fetch_json=github_fetch,
				verified=verified_tests,
			)
			source_candidates = validate_candidate_snapshot(rooted["runtime-candidates.json"])
			runtime_report = validate_machine_report(
				rooted["runtime/runtime-report.json"],
				contract_bundle["scenarios"],
				run_root=Path(f"/proc/self/fd/{root_descriptor}") / "runtime",
				site_exception_path=site_exception_path,
			)
			captured_artifacts = {item["path"]: item for item in capture["artifacts"]}
			source_performance = validate_measurements(
				rooted["performance.json"],
				candidate=capture["candidate"],
				po_sha256=captured_artifacts["frappe_lt/locale/lt.po"]["sha256"],
				release=capture["release"],
				expected_mo_bytes=source_verify["mo_bytes"],
				expected_sites={"baseline": BASELINE_SITE, "enabled": ENABLED_SITE},
				root=ROOT,
			)
			if rooted["runtime-residue.json"] != []:
				raise ValueError("rooted runtime residue is not empty")
			if source_performance["upstream_pins"] != capture["environment"]["upstream"]:
				raise ValueError("rooted performance upstream pins differ from capture")
			if (
				source_candidates["discovery"] != runtime_report["discovery"]
				or source_candidates["coverage"] != runtime_report["coverage"]
				or source_candidates["site_state_sha256"] != runtime_report["site_state_sha256"]
			):
				raise ValueError("rooted runtime discovery or coverage artifacts disagree")
			if (
				runtime_coverage(
					source_candidates["discovery"],
					contract_bundle["scenarios"],
					contract_bundle["classifications"],
				)
				!= source_candidates["coverage"]
			):
				raise ValueError("rooted runtime coverage differs from current contracts")
			if source_candidates["coverage"]["gaps"] or runtime_report["status"] != "pass":
				raise ValueError("rooted runtime evidence is not green")
			if (
				source_candidates["candidate"] != capture["candidate"]
				or runtime_report["candidate"] != capture["candidate"]
			):
				raise ValueError("rooted runtime candidate differs from capture")
			if source_candidates["site"] != capture["site"] or runtime_report["site"] != capture["site"]:
				raise ValueError("rooted runtime site differs from capture")
			for source in (source_candidates["environment"], runtime_report["environment"]):
				if (
					source["inventory_digest"] != capture["release"]["inventory_digest"]
					or source["upstream"] != capture["environment"]["upstream"]
				):
					raise ValueError("rooted runtime provenance differs from capture")
			if runtime_report["environment"]["mo_sha256"] != capture["release"]["mo_sha256"]:
				raise ValueError("rooted runtime MO differs from capture")
			for name, path in CAPTURE_ARTIFACTS.items():
				if _artifact(path, name) != captured_artifacts[name]:
					raise ValueError(f"rooted captured artifact changed: {name}")
			if release_quality_summary() != capture["quality"]:
				raise ValueError("rooted authenticated catalog quality changed since capture")
			expected_from_sources = {
				"candidate": capture["candidate"],
				"install": {
					"maintenance_mode": source_install_status["maintenance_mode"],
					"migration": source_install_status["migration"],
					"run_id": source_install["run_id"],
					"state": source_install["state"],
				},
				"migration": source_migration,
				"performance": {
					"compiled_mo_bytes": source_verify["mo_bytes"],
					"limit_bytes": source_performance["artifacts"]["limit_bytes"],
					"paired_warm_p95_overhead_ms": source_performance["paired_warm_p95_overhead_ms"],
					"shipped_bytes": source_performance["artifacts"]["shipped_bytes"],
				},
				"preflight": {
					"migration": source_preflight["migration"],
					"warning_codes": source_preflight["warnings"],
					"warning_count": len(source_preflight["warnings"]),
				},
				"quality": capture["quality"],
				"release": capture["release"],
				"runtime": {
					"candidate_count": len(source_candidates["discovery"]["candidates"]),
					"run_id": runtime_report["run_id"],
					"scenarios": [
						{"id": result["id"], "status": result["status"]}
						for result in runtime_report["scenario_results"]
					],
				},
				"runtime_contracts": capture["runtime_contracts"],
				"tests": {
					"artifact": "tests.json",
					"categories": source_tests["categories"],
					"run_attempt": verified_tests["run_attempt"],
					"run_id": verified_tests["run_id"],
					"source": verified_tests["artifact"],
					"url": verified_tests["url"],
				},
				"upstream": capture["environment"]["upstream"],
				"verification": {
					"effective_translation": source_verify["effective_translation"],
					"mo_bytes": source_verify["mo_bytes"],
					"state": source_verify["state"],
				},
			}
			for field, expected in expected_from_sources.items():
				if value[field] != expected:
					raise ValueError(f"release-candidate {field} differs from rooted evidence")
			nested = sorted(
				f"runtime/{evidence['path']}"
				for result in runtime_report["scenario_results"]
				for evidence in result["evidence"]
			)
			expected_paths = sorted((*INPUTS, *nested))
			if paths != expected_paths:
				raise ValueError("release-candidate artifact index omits or invents runtime evidence")
			total = 0
			for item in artifacts:
				content = _read_bytes(root_descriptor, item["path"])
				total += len(content)
				if len(content) != item["bytes"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
					raise ValueError(f"release-candidate artifact bytes or digest changed: {item['path']}")
			if total > MAX_TOTAL_INPUT_BYTES + MAX_EVIDENCE_PER_RUN:
				raise ValueError("release-candidate artifact index exceeds its aggregate byte limit")
		finally:
			if close_root:
				os.close(root_descriptor)
	encoded = canonical_json(value).decode().lower()
	if any(marker in encoded for marker in ("/private/", "authorization:", "cookie", "credential", "secret")):
		raise ValueError("release-candidate machine index contains private or secret material")
	return value


def validate_final_index(
	value,
	*,
	root: Path | None = None,
	site_exception_path: str | None = None,
	github_fetch=None,
):
	"""Authenticate a final index against every rooted source artifact."""
	if root is None:
		raise ValueError("rooted release-candidate artifact verification is required")
	return _validate_final_index(
		value,
		root=Path(root),
		site_exception_path=site_exception_path,
		github_fetch=github_fetch,
	)


def _publish_final(directory_fd: int, machine: bytes, human: bytes, link=os.link):
	targets = {
		"release-candidate.json": machine,
		"release-candidate.md": human,
	}
	temporary = {}
	published = {}
	try:
		for name in targets:
			try:
				os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
			except FileNotFoundError:
				continue
			raise ValueError("final release-candidate evidence already exists")
		for name, content in targets.items():
			temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
			fd = os.open(
				temporary_name,
				os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
				0o600,
				dir_fd=directory_fd,
			)
			with os.fdopen(fd, "wb") as stream:
				stream.write(content)
				stream.flush()
				os.fsync(stream.fileno())
			temporary[name] = temporary_name
		for name in ("release-candidate.md", "release-candidate.json"):
			link(
				temporary[name],
				name,
				src_dir_fd=directory_fd,
				dst_dir_fd=directory_fd,
				follow_symlinks=False,
			)
			identity = os.stat(temporary[name], dir_fd=directory_fd, follow_symlinks=False)
			published[name] = identity.st_dev, identity.st_ino
		os.fsync(directory_fd)
	except Exception:
		for name, identity in published.items():
			try:
				current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
				if (current.st_dev, current.st_ino) == identity:
					os.unlink(name, dir_fd=directory_fd)
			except FileNotFoundError:
				pass
		raise
	finally:
		for name in temporary.values():
			try:
				os.unlink(name, dir_fd=directory_fd)
			except FileNotFoundError:
				pass


def finalize(
	root: str | Path,
	*,
	site: str = "development.localhost",
	frappe_module=None,
	site_exception_path: str | None = None,
	link=os.link,
	github_fetch=None,
) -> dict:
	"""Validate existing authoritative evidence and publish its bounded public index."""
	root = Path(root)
	if site != "development.localhost":
		raise ValueError("release-candidate target must be development.localhost")
	frappe = frappe_module
	if frappe is None:
		import frappe as frappe_module

		frappe = frappe_module
	if root.is_symlink() or not root.is_dir() or not COMMIT.fullmatch(root.name):
		raise ValueError("release-candidate root must be an existing candidate-SHA directory")
	_candidate_root(frappe, root, root.name)
	flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
	try:
		root_descriptor = os.open(root, flags)
	except OSError as error:
		raise ValueError("release-candidate root is unsafe") from error
	values = {}
	contents = {}
	try:
		root_stat = os.fstat(root_descriptor)
		root_identity = root_stat.st_dev, root_stat.st_ino
		for relative in INPUTS:
			values[relative], contents[relative] = _read_input(root_descriptor, relative)
	finally:
		os.close(root_descriptor)
	if sum(map(len, contents.values())) > MAX_TOTAL_INPUT_BYTES:
		raise ValueError("release-candidate inputs exceed their aggregate byte limit")
	capture_value = validate_capture(values["candidate.json"])
	if capture_value["site"] != site:
		raise ValueError("release-candidate evidence site differs from the command site")
	_candidate_root(frappe, root, capture_value["candidate"]["commit"])
	preflight = _validate_preflight(values["preflight.json"], capture_value)
	prepare = _validate_prepare(values["prepare.json"], preflight)
	install, install_status = _validate_install(
		values["install.json"], values["install-status.json"], capture_value["site"]
	)
	if install["run_id"] != prepare["run_id"]:
		raise ValueError("install run differs from the prepared migration run")
	verify = _validate_verify(values["verify.json"], capture_value)
	tests, verified_tests = _validate_tests(
		values["tests.json"], capture_value["candidate"], fetch_json=github_fetch
	)
	candidates = validate_candidate_snapshot(values["runtime-candidates.json"])
	contracts = load_contracts()
	runtime = validate_machine_report(
		values["runtime/runtime-report.json"],
		contracts["scenarios"],
		site_exception_path=site_exception_path,
		run_root=root / "runtime",
	)
	captured_artifacts = {item["path"]: item for item in capture_value["artifacts"]}
	if values["runtime-residue.json"] != []:
		raise ValueError("independent runtime residue evidence is not empty")
	if assert_no_runtime_residue() != []:
		raise ValueError("current independent runtime residue check is not empty")
	from frappe_lt import install as install_module
	from frappe_lt import verify as verify_module

	current_install_status = install_module.status()
	if current_install_status != install_status:
		raise ValueError("current install status differs from the supplied safe output")
	current_verify = verify_module.run(capture_value["site"], mode="local")
	if current_verify != verify:
		raise ValueError("current verification differs from the supplied safe output")
	install_plan = install_module._checked(frappe)
	expected_install_binding = {
		"candidate": capture_value["candidate"],
		"capture_sha256": hashlib.sha256(contents["candidate.json"]).hexdigest(),
		"schema_version": 1,
	}
	if (
		install_plan.get("schema_version") != 2
		or install_plan.get("release_candidate") != expected_install_binding
	):
		raise ValueError("saved installation is not bound to the selected candidate capture")
	from frappe_lt import legacy_migration

	_policy_entries, current_policy_sha256 = legacy_migration.trusted_site_policy(site_exception_path)
	if install_plan["policy_sha256"] != current_policy_sha256:
		raise ValueError("saved installation site-exception policy differs from finalization policy")
	performance = validate_measurements(
		values["performance.json"],
		candidate=capture_value["candidate"],
		po_sha256=captured_artifacts["frappe_lt/locale/lt.po"]["sha256"],
		release=capture_value["release"],
		expected_mo_bytes=current_verify["mo_bytes"],
		expected_sites={"baseline": BASELINE_SITE, "enabled": ENABLED_SITE},
		root=ROOT,
	)
	if performance["upstream_pins"] != capture_value["environment"]["upstream"]:
		raise ValueError("performance upstream pins differ from candidate capture")
	current_environment = verify_environment(
		frappe,
		site=capture_value["site"],
		require_clean_upstream=True,
		required_apps=("frappe", "erpnext", "frappe_lt"),
		require_exact_apps=True,
		require_active_catalog=True,
		require_runtime_metadata=True,
	)
	current_release = verify_release()
	if current_release != capture_value["release"]:
		raise ValueError("authenticated release changed since candidate capture")
	if release_quality_summary() != capture_value["quality"]:
		raise ValueError("authenticated catalog quality changed since candidate capture")
	for name, path in CAPTURE_ARTIFACTS.items():
		if _artifact(path, name) != captured_artifacts[name]:
			raise ValueError(f"release artifact changed since candidate capture: {name}")
	if (
		current_environment["inventory_digest"] != capture_value["release"]["inventory_digest"]
		or current_environment["mo_sha256"] != capture_value["release"]["mo_sha256"]
		or {
			app: {"commit": pin["commit"], "version": pin["version"]}
			for app, pin in current_environment["upstream"].items()
		}
		!= capture_value["environment"]["upstream"]
	):
		raise ValueError("current environment differs from candidate capture")
	if clean_candidate_identity(ROOT.parent) != capture_value["candidate"]:
		raise ValueError("frappe_lt candidate changed or became dirty after capture")
	if candidates["site"] != capture_value["site"] or runtime["site"] != capture_value["site"]:
		raise ValueError("runtime evidence site differs from candidate capture")
	if (
		candidates["candidate"] != capture_value["candidate"]
		or runtime["candidate"] != capture_value["candidate"]
	):
		raise ValueError("runtime evidence candidate differs from candidate capture")
	if candidates["discovery"] != runtime["discovery"] or candidates["coverage"] != runtime["coverage"]:
		raise ValueError("runtime discovery or coverage artifacts disagree")
	current_coverage = runtime_coverage(
		candidates["discovery"], contracts["scenarios"], contracts["classifications"]
	)
	if current_coverage != candidates["coverage"]:
		raise ValueError("runtime coverage differs from the current authenticated contracts")
	if current_coverage["gaps"] or runtime["status"] != "pass":
		raise ValueError("runtime evidence is not green")
	for source in (candidates["environment"], runtime["environment"]):
		if (
			source["inventory_digest"] != capture_value["release"]["inventory_digest"]
			or source["upstream"] != capture_value["environment"]["upstream"]
		):
			raise ValueError("runtime provenance differs from candidate capture")
	if runtime["environment"]["mo_sha256"] != capture_value["release"]["mo_sha256"]:
		raise ValueError("runtime MO differs from candidate capture")
	current_site_state = runtime_site_state_digest()
	if (
		candidates["site_state_sha256"] != runtime["site_state_sha256"]
		or runtime["site_state_sha256"] != current_site_state
	):
		raise ValueError("runtime evidence is stale for the current mutable site state")
	if runtime_discover(frappe) != candidates["discovery"]:
		raise ValueError("runtime discovery evidence is stale for the current site")
	migration = _validate_migration_final(frappe, install_status, preflight, values["migration.json"])
	refs = [
		{
			"bytes": len(contents[relative]),
			"path": relative,
			"sha256": hashlib.sha256(contents[relative]).hexdigest(),
		}
		for relative in INPUTS
	]
	refs.extend(
		{
			"bytes": evidence["bytes"],
			"path": f"runtime/{evidence['path']}",
			"sha256": evidence["sha256"],
		}
		for result in runtime["scenario_results"]
		for evidence in result["evidence"]
	)
	refs.sort(key=lambda item: item["path"])
	manifest = {scenario["id"]: scenario for scenario in contracts["scenarios"]["scenarios"]}
	summary = {
		"cleanup_failures": runtime["summary"]["cleanup_failures"],
		"desktop": sum(
			manifest[result["id"]]["viewport"]["width"] > MOBILE_MAX_WIDTH
			for result in runtime["scenario_results"]
		),
		"english_fallbacks": runtime["summary"]["english_fallbacks"],
		"functional_layout_defects": runtime["summary"]["functional_layout_defects"],
		"mobile": sum(
			manifest[result["id"]]["viewport"]["width"] <= MOBILE_MAX_WIDTH
			for result in runtime["scenario_results"]
		),
		"preserved_token_failures": runtime["summary"]["preserved_token_failures"],
		"routes": len(candidates["discovery"]["candidates"]),
		"runtime_residue": 0,
	}
	machine = {
		"artifacts": refs,
		"candidate": capture_value["candidate"],
		"install": {
			"maintenance_mode": install_status["maintenance_mode"],
			"migration": install_status["migration"],
			"run_id": install["run_id"],
			"state": install["state"],
		},
		"migration": migration,
		"performance": {
			"compiled_mo_bytes": verify["mo_bytes"],
			"limit_bytes": performance["artifacts"]["limit_bytes"],
			"paired_warm_p95_overhead_ms": performance["paired_warm_p95_overhead_ms"],
			"shipped_bytes": performance["artifacts"]["shipped_bytes"],
		},
		"preflight": {
			"migration": preflight["migration"],
			"warning_codes": preflight["warnings"],
			"warning_count": len(preflight["warnings"]),
		},
		"quality": capture_value["quality"],
		"release": capture_value["release"],
		"runtime": {
			"candidate_count": len(candidates["discovery"]["candidates"]),
			"run_id": runtime["run_id"],
			"scenarios": [
				{"id": result["id"], "status": result["status"]} for result in runtime["scenario_results"]
			],
		},
		"runtime_contracts": capture_value["runtime_contracts"],
		"schema_version": 1,
		"site": capture_value["site"],
		"status": "pass",
		"summary": summary,
		"tests": {
			"artifact": "tests.json",
			"categories": tests["categories"],
			"run_attempt": verified_tests["run_attempt"],
			"run_id": verified_tests["run_id"],
			"source": verified_tests["artifact"],
			"url": verified_tests["url"],
		},
		"upstream": capture_value["environment"]["upstream"],
		"verification": {
			"effective_translation": verify["effective_translation"],
			"mo_bytes": verify["mo_bytes"],
			"state": verify["state"],
		},
	}
	flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
	try:
		publication_descriptor = os.open(root, flags)
	except OSError as error:
		raise ValueError("final release-candidate publication directory is unsafe") from error
	try:
		current_root = os.fstat(publication_descriptor)
		if (current_root.st_dev, current_root.st_ino) != root_identity:
			raise ValueError("release-candidate root identity changed before publication")
		machine = _validate_final_index(
			machine,
			root=root,
			root_descriptor=publication_descriptor,
			site_exception_path=site_exception_path,
			verified_tests=verified_tests,
		)
		encoded = canonical_json(machine)
		if len(encoded) > MAX_FINAL_BYTES:
			raise ValueError(f"release-candidate machine index exceeds {MAX_FINAL_BYTES} bytes")
		_publish_final(publication_descriptor, encoded, _human_report(machine).encode(), link=link)
	finally:
		os.close(publication_descriptor)
	return {
		"output": str(root / "release-candidate.json"),
		"sha256": hashlib.sha256(encoded).hexdigest(),
		"status": "pass",
	}
