import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from frappe_lt.inventory import _json_object, canonical_json, verify_environment
from frappe_lt.runtime_contracts import load_contracts, safe_relative_path
from frappe_lt.runtime_control import SiteControl, redact_sensitive
from frappe_lt.runtime_discovery import coverage, discover

REPORT_SCHEMA_VERSION = 1
BROWSER_SCHEMA_VERSION = 2
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_PER_SCENARIO = 8 * 1024 * 1024
MAX_EVIDENCE_PER_RUN = 32 * 1024 * 1024
MAX_FINDINGS = 500_000
SHA256 = re.compile(r"[0-9a-f]{64}")
EVIDENCE_MIME = {
	"email": {"message/rfc822"},
	"lookup": {"application/json"},
	"print": {"application/pdf", "text/html"},
	"screenshot": {"image/png"},
}
SENSITIVE_EVIDENCE = (
	b"authorization:",
	b"cookie:",
	b"set-cookie:",
	b'"password"',
	b'"pwd"',
	b'"token"',
	b"?key=",
)


def _redact(value: object) -> str:
	return redact_sensitive(value)


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


def _validate_fallback(value: object, scenario: dict) -> dict:
	scenario_id = scenario["id"]
	value = _exact(
		value,
		{
			"effective",
			"excluded",
			"exclusion_id",
			"key",
			"scenario_id",
			"source",
			"target",
			"visible",
		},
		"lookup evidence",
	)
	if value["scenario_id"] != scenario_id:
		raise ValueError("lookup evidence scenario_id does not match its scenario")
	key = _exact(value["key"], {"context", "source"}, "Translation Key")
	if not isinstance(key["source"], str) or not key["source"].strip():
		raise ValueError("Translation Key source must be nonempty text")
	if key["source"] != key["source"].strip():
		raise ValueError("Translation Key source must use Frappe outer-whitespace normalization")
	if key["context"] is not None and (not isinstance(key["context"], str) or not key["context"]):
		raise ValueError("Frappe Context must be nonempty text or null")
	for field in ("effective", "source"):
		if not isinstance(value[field], str):
			raise ValueError(f"lookup evidence {field} must be text")
	if value["source"] not in {"database", "erpnext", "frappe", "frappe_lt", "missing"}:
		raise ValueError("lookup evidence source is invalid")
	target = _exact(value["target"], {"type", "value"}, "render target")
	if target["type"] not in {"locator", "output_interval"} or not isinstance(target["value"], str):
		raise ValueError("render target is invalid")
	if len(key["source"].encode()) > 256 * 1024 or len(value["effective"].encode()) > 256 * 1024:
		raise ValueError("lookup evidence text exceeds its byte limit")
	if (key["context"] is not None and len(key["context"].encode()) > 4096) or len(
		target["value"].encode()
	) > 4096:
		raise ValueError("lookup context or render target exceeds its byte limit")
	if not isinstance(value["visible"], bool) or not isinstance(value["excluded"], bool):
		raise ValueError("lookup evidence visibility and exclusion must be boolean")
	if value["excluded"] != (value["exclusion_id"] is not None):
		raise ValueError("lookup evidence exclusion decision is inconsistent")
	if value["excluded"]:
		exclusions = {exclusion["id"]: exclusion["target"] for exclusion in scenario["expected_exclusions"]}
		if exclusions.get(value["exclusion_id"]) != target["value"]:
			raise ValueError("lookup evidence exclusion is not an exact reviewed scenario exclusion")
	return value


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


def _validate_evidence(value: object, run_root: Path, scenario_id: str) -> dict:
	value = _exact(value, {"bytes", "kind", "mime", "path", "sha256"}, "evidence artifact")
	if value["kind"] not in EVIDENCE_MIME:
		raise ValueError("evidence artifact kind is invalid")
	if not isinstance(value["bytes"], int) or value["bytes"] < 0:
		raise ValueError("evidence artifact bytes must be a nonnegative integer")
	if value["mime"] not in EVIDENCE_MIME[value["kind"]]:
		raise ValueError("evidence artifact mime is not allowed for its kind")
	if not isinstance(value["sha256"], str) or not SHA256.fullmatch(value["sha256"]):
		raise ValueError("evidence artifact sha256 is invalid")
	relative = safe_relative_path(value["path"])
	if not relative.parts or relative.parts[0] != "evidence" or scenario_id not in relative.parts:
		raise ValueError("evidence artifact must be inside its scenario evidence directory")
	path = run_root.joinpath(*relative.parts)
	for component in (path, *path.parents):
		if component == run_root.parent:
			break
		if component.is_symlink():
			raise ValueError(f"evidence path uses a symlink: {value['path']}")
	try:
		actual_bytes = path.stat().st_size
	except OSError as error:
		raise ValueError(f"could not read evidence artifact {value['path']}: {error}") from error
	if value["bytes"] > MAX_EVIDENCE_PER_SCENARIO or actual_bytes != value["bytes"]:
		raise ValueError(f"evidence artifact size mismatch or limit exceeded: {value['path']}")
	digest = hashlib.sha256()
	sensitive = False
	try:
		with path.open("rb") as stream:
			while chunk := stream.read(1024 * 1024):
				digest.update(chunk)
				lower = chunk.lower()
				sensitive = sensitive or any(pattern in lower for pattern in SENSITIVE_EVIDENCE)
	except OSError as error:
		raise ValueError(f"could not hash evidence artifact {value['path']}: {error}") from error
	if digest.hexdigest() != value["sha256"]:
		raise ValueError(f"evidence artifact size or digest mismatch: {value['path']}")
	if sensitive:
		try:
			path.unlink()
			directory_fd = os.open(path.parent, os.O_RDONLY)
			try:
				os.fsync(directory_fd)
			finally:
				os.close(directory_fd)
		except OSError as error:
			raise ValueError(
				f"could not remove sensitive evidence artifact {value['path']}: {error}"
			) from error
		raise ValueError(f"evidence artifact contains disallowed sensitive material: {value['path']}")
	return value


def validate_browser_results(value: object, scenarios: dict, run_root: Path) -> list[dict]:
	value = _exact(value, {"scenarios", "schema_version"}, "browser result")
	if value["schema_version"] != BROWSER_SCHEMA_VERSION or not isinstance(value["scenarios"], list):
		raise ValueError("unsupported browser result schema")
	manifest = {scenario["id"]: scenario for scenario in scenarios["scenarios"]}
	results = []
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
		if scenario_id not in manifest or scenario_id in seen:
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
		if (
			result["status"] == "pass"
			and result["duration_ms"] > manifest[scenario_id]["scenario_timeout_ms"]
		):
			result["status"] = "blocked"
			result["blocked_reason"] = "scenario exceeded its manifest timeout"
		if not isinstance(result["attempts"], list) or not result["attempts"]:
			raise ValueError("browser scenario must record every attempt")
		for number, attempt in enumerate(result["attempts"], start=1):
			attempt = _exact(attempt, {"kind", "number"}, "browser attempt")
			if attempt["number"] != number or attempt["kind"] not in {"initial", "setup", "transport"}:
				raise ValueError("browser attempts are malformed")
			if number == 1 and attempt["kind"] != "initial":
				raise ValueError("first browser attempt must be initial")
		if len(result["attempts"]) > 1 and any(
			attempt["kind"] not in {"setup", "transport"} for attempt in result["attempts"][1:]
		):
			raise ValueError("only setup or transport failures may be retried")
		if not all(isinstance(result[field], list) for field in ("evidence", "fallbacks", "layouts")):
			raise ValueError("browser findings and evidence must be lists")
		result["fallbacks"] = sorted(
			(_validate_fallback(item, manifest[scenario_id]) for item in result["fallbacks"]),
			key=lambda item: (item["key"]["source"], item["key"]["context"] or "", item["target"]["value"]),
		)
		result["layouts"] = sorted(
			(_validate_layout(item, scenario_id) for item in result["layouts"]),
			key=lambda item: (item["severity"], item["kind"], item["target"], item["detail"]),
		)
		result["evidence"] = sorted(
			(_validate_evidence(item, run_root, scenario_id) for item in result["evidence"]),
			key=lambda item: item["path"],
		)
		scenario_evidence = sum(item["bytes"] for item in result["evidence"])
		if scenario_evidence > MAX_EVIDENCE_PER_SCENARIO:
			raise ValueError(f"scenario {scenario_id!r} exceeds its evidence byte limit")
		total_evidence += scenario_evidence
		blocking_fallback = any(
			item["visible"]
			and not item["excluded"]
			and (item["source"] == "missing" or item["effective"] == item["key"]["source"])
			for item in result["fallbacks"]
		)
		blocking_layout = any(item["severity"] == "functional" for item in result["layouts"])
		if result["status"] == "pass" and (not result["ready"] or not result["fallbacks"]):
			result["status"] = "blocked"
			result["blocked_reason"] = (
				"scenario did not prove readiness"
				if not result["ready"]
				else "scenario produced no effective translation lookup evidence"
			)
		if result["status"] == "pass" and (blocking_fallback or blocking_layout):
			result["status"] = "fail"
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


def blocked_result(scenario_id: str, reason: str) -> dict:
	return {
		"attempts": [{"kind": "initial", "number": 1}],
		"blocked_reason": _redact(reason),
		"duration_ms": 0,
		"evidence": [],
		"error": None,
		"fallbacks": [],
		"id": scenario_id,
		"layouts": [],
		"ready": False,
		"status": "blocked",
	}


def _default_browser_runner(site: str, run_root: Path, plan_path: Path) -> tuple[dict, int]:
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
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		env=environment,
		text=True,
		start_new_session=True,
	)
	try:
		process.communicate(timeout=deadline_seconds)
	except subprocess.TimeoutExpired as error:
		os.killpg(process.pid, signal.SIGTERM)
		try:
			process.communicate(timeout=10)
		except subprocess.TimeoutExpired:
			os.killpg(process.pid, signal.SIGKILL)
			process.communicate()
		result_path.unlink(missing_ok=True)
		raise RuntimeError(f"Cypress exceeded its {int(deadline_seconds)} second run deadline") from error
	if not result_path.is_file():
		raise RuntimeError(f"Cypress exited {process.returncode} without writing a result")
	try:
		value = _read_json(result_path)
	finally:
		result_path.unlink(missing_ok=True)
	return value, process.returncode


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
) -> dict:
	cleanup_failures = [{**failure, "error": _redact(failure["error"])} for failure in cleanup_failures]
	stale_recoveries = [
		{
			**recovery,
			"cleanup_failures": [
				{**failure, "error": _redact(failure["error"])} for failure in recovery["cleanup_failures"]
			],
			"original_error": _redact(recovery["original_error"])
			if recovery["original_error"] is not None
			else None,
		}
		for recovery in stale_recoveries
	]
	blocking_causes = []
	for error in tool_errors:
		blocking_causes.append({"detail": _redact(error), "type": "tool_error"})
	for gap in coverage_result["gaps"]:
		blocking_causes.append({"detail": gap, "type": "runtime_coverage_gap"})
	for result in results:
		if result["status"] in {"blocked", "fail"}:
			detail = result["error"] or result["blocked_reason"] or result["id"]
			blocking_causes.append(
				{"detail": f"{result['id']}: {detail}"[:2048], "type": f"scenario_{result['status']}"}
			)
	for failure in cleanup_failures:
		blocking_causes.append(
			{
				"detail": f"{failure['target']['doctype']}:{failure['target']['name']}",
				"type": "cleanup_failure",
			}
		)
	fallback_count = sum(
		1
		for result in results
		for finding in result["fallbacks"]
		if finding["visible"]
		and not finding["excluded"]
		and (finding["source"] == "missing" or finding["effective"] == finding["key"]["source"])
	)
	layout_count = sum(
		1 for result in results for finding in result["layouts"] if finding["severity"] == "functional"
	)
	summary = {
		"blocked": sum(result["status"] == "blocked" for result in results),
		"cleanup_failures": len(cleanup_failures),
		"coverage_gaps": len(coverage_result["gaps"]),
		"english_fallbacks": fallback_count,
		"fail": sum(result["status"] == "fail" for result in results),
		"functional_layout_defects": layout_count,
		"pass": sum(result["status"] == "pass" for result in results),
		"total": len(results),
	}
	report_environment = None
	if environment:
		report_environment = {
			"babel": environment["babel"],
			"installed_apps": environment["installed_apps"],
			"inventory_digest": environment["inventory_digest"],
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
	}


def run(
	site: str,
	output_dir: str | None = None,
	*,
	browser_runner=None,
	frappe_module=None,
	clock=time.monotonic,
	run_id: str | None = None,
) -> dict:
	"""Run the reviewed runtime denominator and always attempt durable cleanup."""
	if site != "development.localhost":
		raise ValueError("runtime validation target must be development.localhost")
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
	coverage_result = {"covered": [], "gaps": [], "reviewed_out_of_scope": []}
	results = []
	cleanup_failures = []
	stale_recoveries = []
	tool_errors = []
	contracts = None
	control = None
	try:
		preflight_started = clock()
		contracts = load_contracts()
		environment = verify_environment(
			frappe,
			site=site,
			required_apps=("frappe", "erpnext", "frappe_lt"),
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
				prepared = control.prepare(contracts["profiles"], contracts["scenarios"])
				scenarios_started = clock()
				browser_output = (browser_runner or _default_browser_runner)(
					site, run_root, Path(prepared["browser_plan"])
				)
				if isinstance(browser_output, tuple):
					browser, browser_exit = browser_output
				else:
					browser, browser_exit = browser_output, 0
				results = validate_browser_results(browser, contracts["scenarios"], run_root)
				blocking_result = next((result for result in results if result["status"] != "pass"), None)
				original_error = None
				if browser_exit:
					original_error = f"Cypress exited {browser_exit}"
					tool_errors.append(original_error)
				elif blocking_result:
					original_error = (
						blocking_result["error"]
						or blocking_result["blocked_reason"]
						or f"scenario {blocking_result['id']} failed with runtime findings"
					)
				if original_error:
					control.set_original_error(original_error)
				durations["scenarios"] = int((clock() - scenarios_started) * 1000)
			except Exception as error:
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
	)
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
