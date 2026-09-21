import hashlib
import io
import json
import os
import subprocess
import sys
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from frappe_lt.inventory import canonical_json, verify_environment
from frappe_lt.runtime_contracts import (
	load_contracts,
	safe_relative_path,
	validate_classifications,
	validate_role_profiles,
	validate_scenarios,
)
from frappe_lt.runtime_control import (
	SiteControl,
	_active_translation_key,
	_authorized_browser_plan,
	_capture_http_response,
	_capture_server_lookups,
	_email_visible_output,
	_finalize_recipient_message,
	_redact_email_output,
	_resolve_effective,
	_runtime_default_value_name,
	_write_durable,
	redact_sensitive,
)
from frappe_lt.runtime_discovery import (
	RuntimeCandidate,
	coverage,
	discover,
	export_candidate_snapshot,
	validate_candidate_snapshot,
)
from frappe_lt.runtime_validation import (
	BrowserRunnerError,
	_build_report,
	_default_browser_runner,
	_write_reports,
	blocked_result,
	run,
	validate_browser_results,
)


class RuntimeContractTest(TestCase):
	def test_versioned_contracts_are_strict_and_deterministic(self):
		contracts = load_contracts()
		self.assertEqual(contracts["scenarios"]["schema_version"], 2)
		self.assertEqual(contracts["profiles"]["schema_version"], 2)
		self.assertEqual(contracts["classifications"]["schema_version"], 2)
		self.assertEqual(
			[scenario["id"] for scenario in contracts["scenarios"]["scenarios"]],
			sorted(scenario["id"] for scenario in contracts["scenarios"]["scenarios"]),
		)
		self.assertEqual(
			canonical_json(contracts),
			canonical_json(load_contracts()),
		)

	def test_contracts_reject_unknown_missing_duplicate_and_malformed_fields(self):
		contracts = load_contracts()
		profile = dict(contracts["profiles"]["profiles"][0])
		profile["unexpected"] = True
		with self.assertRaisesRegex(ValueError, "fields must be exactly"):
			validate_role_profiles({"schema_version": 2, "profiles": [profile]})

		profiles = contracts["profiles"]["profiles"]
		with self.assertRaisesRegex(ValueError, "unique ids"):
			validate_role_profiles({"schema_version": 2, "profiles": [profiles[0], profiles[0]]})

		scenario = dict(contracts["scenarios"]["scenarios"][0])
		scenario.pop("readiness")
		with self.assertRaisesRegex(ValueError, "fields must be exactly"):
			validate_scenarios(
				{"schema_version": 2, "scenarios": [scenario]},
				{profile["id"] for profile in profiles},
			)

	def test_contract_source_order_must_already_be_canonical(self):
		contracts = load_contracts()
		for validator, contract, ids in (
			(
				validate_role_profiles,
				contracts["profiles"],
				None,
			),
			(
				validate_scenarios,
				contracts["scenarios"],
				{profile["id"] for profile in contracts["profiles"]["profiles"]},
			),
			(
				validate_classifications,
				{
					"classifications": [
						{
							"candidate_id": "page:a",
							"disposition": "unsafe",
							"reason": "unsafe",
							"reviewed_by": "reviewer",
						},
						{
							"candidate_id": "page:b",
							"disposition": "unsafe",
							"reason": "unsafe",
							"reviewed_by": "reviewer",
						},
					],
					"schema_version": 2,
				},
				None,
			),
		):
			with self.subTest(validator=validator.__name__):
				value = deepcopy(contract)
				field = (
					"profiles"
					if "profiles" in value
					else "scenarios"
					if "scenarios" in value
					else "classifications"
				)
				value[field].reverse()
				with self.assertRaisesRegex(ValueError, "canonical order"):
					if ids is None:
						validator(value)
					else:
						validator(value, ids)

	def test_role_profiles_version_routing_identity_and_only_approved_defaults(self):
		profiles = load_contracts()["profiles"]["profiles"]
		for profile in profiles:
			self.assertEqual(profile["time_zone"], "Europe/Vilnius")
			self.assertIsNone(profile["default_app"])
			self.assertEqual(
				set(profile["defaults"]),
				{"date_format", "first_day_of_the_week", "number_format", "time_format"},
			)
			self.assertNotIn("Accounts Viewer", profile["roles"])

		invalid = deepcopy(profiles[0])
		invalid["defaults"]["unreviewed_default"] = "value"
		with self.assertRaisesRegex(ValueError, "approved keys"):
			validate_role_profiles({"profiles": [invalid], "schema_version": 2})

	def test_pinned_v16_scenarios_use_canonical_desk_contracts(self):
		contracts = load_contracts()
		scenarios = contracts["scenarios"]["scenarios"]
		by_id = {scenario["id"]: scenario for scenario in scenarios}
		self.assertTrue(
			all(
				scenario["target"]["route"].startswith("/desk")
				for scenario in scenarios
				if scenario["kind"] == "desk"
			)
		)
		self.assertEqual(by_id["desk-permission-manager-page"]["candidate_id"], "page:permission-manager")
		self.assertEqual(
			by_id["desk-system-settings-administrator"]["target"], {"route": "/desk/system-settings"}
		)
		self.assertNotIn("page:Workspaces", {scenario["candidate_id"] for scenario in scenarios})

		profile_ids = {profile["id"] for profile in contracts["profiles"]["profiles"]}
		base = deepcopy(by_id["desk-permission-manager-page"])
		invalid_contracts = []
		wrong_candidate = deepcopy(base)
		wrong_candidate["candidate_id"] = "portal:route:me"
		invalid_contracts.append(wrong_candidate)
		wrong_readiness = deepcopy(base)
		wrong_readiness["readiness"] = {"type": "output", "value": "ready"}
		invalid_contracts.append(wrong_readiness)
		wrong_readiness_value = deepcopy(base)
		wrong_readiness_value["readiness"]["value"] = "item"
		invalid_contracts.append(wrong_readiness_value)
		wrong_target = deepcopy(base)
		wrong_target["target"] = {"route": "/desk/item"}
		invalid_contracts.append(wrong_target)
		wrong_fixture = deepcopy(base)
		wrong_fixture["fixture_id"] = "runtime-user"
		invalid_contracts.append(wrong_fixture)
		broad_exclusion = deepcopy(base)
		broad_exclusion["expected_exclusions"] = [{"id": "all-ui", "reason": "hide it", "target": "body"}]
		invalid_contracts.append(broad_exclusion)
		for scenario in invalid_contracts:
			with (
				self.subTest(scenario=scenario),
				self.assertRaisesRegex(ValueError, "candidate|readiness|fixture|exclusion"),
			):
				validate_scenarios({"scenarios": [scenario], "schema_version": 2}, profile_ids)

	def test_output_manifest_uses_only_run_owned_draft_fixtures(self):
		contracts = load_contracts()
		by_id = {scenario["id"]: scenario for scenario in contracts["scenarios"]["scenarios"]}
		print_scenario = by_id["todo-standard-print"]
		self.assertEqual(print_scenario["candidate_id"], "output:print:ToDo")
		self.assertEqual(print_scenario["fixture_id"], "todo-draft")
		self.assertEqual(print_scenario["role_profile_id"], "administrator")
		self.assertEqual(print_scenario["target"], {"output": "standard-print:ToDo"})
		self.assertNotIn(
			"sales-invoice-existing",
			{
				scenario["fixture_id"]
				for scenario in contracts["scenarios"]["scenarios"]
				if scenario["fixture_id"]
			},
		)

		with self.assertRaisesRegex(ValueError, "reviewer"):
			validate_classifications(
				{
					"schema_version": 2,
					"classifications": [
						{
							"candidate_id": "page:Desk",
							"disposition": "unsafe",
							"reason": "out of scope",
							"reviewed_by": "",
						}
					],
				}
			)

	def test_candidate_classification_requires_an_individual_executable_disposition(self):
		for disposition in ("out_of_scope", "standard", ""):
			with (
				self.subTest(disposition=disposition),
				self.assertRaisesRegex(ValueError, "unsafe or non_executable"),
			):
				validate_classifications(
					{
						"classifications": [
							{
								"candidate_id": "page:permission-manager",
								"disposition": disposition,
								"reason": "reviewed individually",
								"reviewed_by": "reviewer",
							}
						],
						"schema_version": 2,
					}
				)

	def test_safe_evidence_paths_reject_escape_and_platform_aliases(self):
		self.assertEqual(
			str(safe_relative_path("evidence/scenario/screenshot.png")), "evidence/scenario/screenshot.png"
		)
		for path in ("/tmp/file", "../file", "evidence\\file", "evidence/../file", ""):
			with self.subTest(path=path), self.assertRaises(ValueError):
				safe_relative_path(path)

	def test_public_environment_verifier_is_read_only_and_checks_site_cwd_pins_and_order(self):
		manifest = {
			"inventory_digest": "0" * 64,
			"upstream": {
				"erpnext": {"commit": "2" * 40, "version": "16.35.0"},
				"frappe": {"commit": "1" * 40, "version": "16.34.0"},
			},
		}
		frappe = SimpleNamespace(
			__version__="16.34.0",
			get_app_source_path=lambda app: f"/bench/apps/{app}/{app}",
			get_installed_apps=lambda: ["frappe", "erpnext", "frappe_lt"],
			local=SimpleNamespace(site="development.localhost"),
		)
		erpnext = ModuleType("erpnext")
		erpnext.__version__ = "16.35.0"

		def git_value(path, *arguments):
			if arguments == ("rev-parse", "--show-toplevel"):
				return str(Path(path).parents[0])
			if arguments == ("rev-parse", "HEAD"):
				return "1" * 40 if "frappe" in Path(path).name else "2" * 40
			if arguments == ("status", "--porcelain"):
				return ""
			raise AssertionError(arguments)

		with (
			patch.dict(sys.modules, {"erpnext": erpnext}),
			patch("frappe_lt.inventory.verify_owned_artifacts", return_value=manifest),
			patch(
				"frappe_lt.inventory.validate_tool_versions",
				return_value={"babel": "2.16.0", "python": "3.14"},
			),
			patch("frappe_lt.inventory._git_value", side_effect=git_value),
			patch("frappe_lt.inventory.Path.cwd", return_value=Path("/bench/sites")),
		):
			result = verify_environment(
				frappe,
				site="development.localhost",
				require_clean_upstream=True,
				required_apps=("frappe", "erpnext", "frappe_lt"),
			)
		self.assertEqual(result["active_directory"], "/bench/sites")
		self.assertEqual(result["upstream"]["frappe"]["commit"], "1" * 40)
		self.assertEqual(result["upstream"]["erpnext"]["version"], "16.35.0")

	def test_browser_capability_rejects_path_traversal_before_filesystem_access(self):
		class PermissionError(Exception):
			pass

		frappe = SimpleNamespace(
			PermissionError=PermissionError,
			get_site_path=lambda *_parts: self.fail("invalid run id reached the filesystem"),
		)
		with self.assertRaises(PermissionError):
			_authorized_browser_plan(frappe, "../../public/files/forged", "token")

	def test_report_and_journal_redaction_handles_json_headers_and_cookies(self):
		redacted = redact_sensitive(
			'{"token":"secret-token","password":"hunter2"} '
			"Authorization: Bearer bearer-secret\nCookie: sid=session-secret\n"
			"To: runtime-recipient@invalid.example\n"
			"https://example.invalid/update-password%3Fkey%3Dencoded-reset-secret"
		)
		for secret in (
			"secret-token",
			"hunter2",
			"bearer-secret",
			"session-secret",
			"runtime-recipient@invalid.example",
			"encoded-reset-secret",
		):
			self.assertNotIn(secret, redacted)
		self.assertGreaterEqual(redacted.count("[REDACTED]"), 6)


class RuntimeDiscoveryTest(TestCase):
	def test_candidate_snapshot_schema_rejects_unknown_fields_and_noncanonical_order(self):
		snapshot = {
			"coverage": {
				"covered": ["page:a"],
				"gaps": ["page:b"],
				"reviewed_exclusions": [],
			},
			"discovery": {
				"candidates": [
					{"app": "frappe", "id": "page:a", "identity": "a", "type": "page"},
					{"app": "erpnext", "id": "page:b", "identity": "b", "type": "page"},
				],
				"collector_counts": {"email": 1, "metadata": 1, "portal": 1, "print": 1},
			},
			"environment": {
				"babel": "2.16.0",
				"installed_apps": ["frappe", "erpnext", "frappe_lt"],
				"inventory_digest": "3" * 64,
				"python": "3.14.0",
				"upstream": {
					"erpnext": {"commit": "2" * 40, "version": "16.35.0"},
					"frappe": {"commit": "1" * 40, "version": "16.34.0"},
				},
			},
			"schema_version": 1,
			"site": "development.localhost",
		}
		self.assertEqual(validate_candidate_snapshot(deepcopy(snapshot)), snapshot)
		unknown = deepcopy(snapshot)
		unknown["unexpected"] = True
		with self.assertRaisesRegex(ValueError, "fields must be exactly"):
			validate_candidate_snapshot(unknown)
		missing = deepcopy(snapshot)
		missing.pop("coverage")
		with self.assertRaisesRegex(ValueError, "fields must be exactly"):
			validate_candidate_snapshot(missing)
		noncanonical = deepcopy(snapshot)
		noncanonical["discovery"]["candidates"].reverse()
		with self.assertRaisesRegex(ValueError, "canonical order"):
			validate_candidate_snapshot(noncanonical)
		malformed = deepcopy(snapshot)
		malformed["discovery"]["candidates"][0]["app"] = "custom_app"
		with self.assertRaisesRegex(ValueError, "out-of-scope app"):
			validate_candidate_snapshot(malformed)

	def test_candidate_snapshot_export_is_canonical_and_pinned(self):
		environment = {
			"active_directory": "/bench/sites",
			"babel": "2.16.0",
			"installed_apps": ["frappe", "erpnext", "frappe_lt"],
			"inventory_digest": "3" * 64,
			"python": "3.14.0",
			"site": "development.localhost",
			"upstream": {
				"erpnext": {"commit": "2" * 40, "path": "/bench/apps/erpnext", "version": "16.35.0"},
				"frappe": {"commit": "1" * 40, "path": "/bench/apps/frappe", "version": "16.34.0"},
			},
		}
		discovery = {
			"candidates": [
				{"app": "frappe", "id": "page:a", "identity": "a", "type": "page"},
				{"app": "erpnext", "id": "page:b", "identity": "b", "type": "page"},
			],
			"collector_counts": {"email": 1, "metadata": 1, "portal": 1, "print": 1},
		}
		contracts = {
			"classifications": {
				"classifications": [
					{
						"candidate_id": "page:b",
						"disposition": "non_executable",
						"reason": "reviewed",
						"reviewed_by": "reviewer",
					}
				]
			},
			"scenarios": {"scenarios": [{"candidate_id": "page:a"}]},
		}
		frappe = SimpleNamespace(local=SimpleNamespace(site="development.localhost"))
		with TemporaryDirectory() as directory:
			first = Path(directory) / "first.json"
			second = Path(directory) / "second.json"
			with (
				patch("frappe_lt.runtime_discovery.load_contracts", return_value=contracts),
				patch("frappe_lt.runtime_discovery.verify_environment", return_value=environment) as verifier,
				patch("frappe_lt.runtime_discovery.discover", return_value=discovery),
			):
				first_result = export_candidate_snapshot(
					"development.localhost", str(first), frappe_module=frappe
				)
				second_result = export_candidate_snapshot(
					"development.localhost", str(second), frappe_module=frappe
				)
			self.assertEqual(verifier.call_count, 2)
			verifier.assert_called_with(
				frappe,
				site="development.localhost",
				require_clean_upstream=True,
				required_apps=("frappe", "erpnext", "frappe_lt"),
			)
			self.assertEqual(first.read_bytes(), second.read_bytes())
			snapshot = json.loads(first.read_bytes())
			self.assertEqual(first.read_bytes(), canonical_json(snapshot))
			self.assertEqual(snapshot["schema_version"], 1)
			self.assertEqual(snapshot["environment"]["upstream"]["frappe"]["commit"], "1" * 40)
			self.assertEqual(snapshot["environment"]["upstream"]["erpnext"]["version"], "16.35.0")
			self.assertNotIn("path", snapshot["environment"]["upstream"]["frappe"])
			self.assertEqual(first_result["sha256"], hashlib.sha256(first.read_bytes()).hexdigest())
			self.assertEqual(first_result["sha256"], second_result["sha256"])

	def test_candidate_snapshot_write_failure_preserves_previous_export(self):
		environment = {
			"babel": "2.16.0",
			"installed_apps": ["frappe", "erpnext", "frappe_lt"],
			"inventory_digest": "3" * 64,
			"python": "3.14.0",
			"upstream": {
				"erpnext": {"commit": "2" * 40, "version": "16.35.0"},
				"frappe": {"commit": "1" * 40, "version": "16.34.0"},
			},
		}
		discovery = {
			"candidates": [{"app": "frappe", "id": "page:a", "identity": "a", "type": "page"}],
			"collector_counts": {"email": 1, "metadata": 1, "portal": 1, "print": 1},
		}
		contracts = {
			"classifications": {"classifications": []},
			"scenarios": {"scenarios": [{"candidate_id": "page:a"}]},
		}
		frappe = SimpleNamespace(local=SimpleNamespace(site="development.localhost"))
		with TemporaryDirectory() as directory:
			root = Path(directory)
			output = root / "candidate.json"
			output.write_bytes(b"previous export\n")
			with (
				patch("frappe_lt.runtime_discovery.load_contracts", return_value=contracts),
				patch("frappe_lt.runtime_discovery.verify_environment", return_value=environment),
				patch("frappe_lt.runtime_discovery.discover", return_value=discovery),
				self.assertRaisesRegex(OSError, "disk full"),
			):
				export_candidate_snapshot(
					"development.localhost",
					str(output),
					frappe_module=frappe,
					replace=lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
				)
			self.assertEqual(output.read_bytes(), b"previous export\n")
			self.assertEqual(list(root.glob(".candidate.json.*.tmp")), [])

	def test_discovery_is_sorted_typed_and_fails_closed(self):
		collectors = {
			"z": lambda _frappe: [RuntimeCandidate("page:Z", "page", "frappe", "Z")],
			"a": lambda _frappe: [RuntimeCandidate("page:A", "page", "frappe", "A")],
		}
		result = discover(object(), collectors)
		self.assertEqual([candidate["id"] for candidate in result["candidates"]], ["page:A", "page:Z"])
		self.assertEqual(result["collector_counts"], {"a": 1, "z": 1})

		with self.assertRaisesRegex(ValueError, "unexpectedly returned no candidates"):
			discover(object(), {"empty": lambda _frappe: []})
		with self.assertRaisesRegex(ValueError, "collector 'broken' failed"):
			discover(object(), {"broken": lambda _frappe: 1 / 0})

	def test_coverage_never_executes_or_silently_adds_discovered_candidates(self):
		discovery = {
			"candidates": [
				{"app": "frappe", "id": "page:A", "identity": "A", "type": "page"},
				{"app": "frappe", "id": "page:B", "identity": "B", "type": "page"},
				{"app": "frappe", "id": "page:C", "identity": "C", "type": "page"},
			]
		}
		scenarios = {"scenarios": [{"candidate_id": "page:A"}]}
		classifications = {
			"classifications": [
				{
					"candidate_id": "page:B",
					"disposition": "non_executable",
					"reason": "not product UI",
					"reviewed_by": "reviewer",
				}
			]
		}
		self.assertEqual(
			coverage(discovery, scenarios, classifications),
			{"covered": ["page:A"], "gaps": ["page:C"], "reviewed_exclusions": ["page:B"]},
		)
		with self.assertRaisesRegex(ValueError, "Manifest references candidates not found"):
			coverage(discovery, {"scenarios": [{"candidate_id": "page:Missing"}]}, classifications)
		with self.assertRaisesRegex(ValueError, "both manifest and classifier"):
			coverage(
				discovery,
				{"scenarios": [{"candidate_id": "page:A"}]},
				{
					"classifications": [
						{
							"candidate_id": "page:A",
							"disposition": "non_executable",
							"reason": "conflict",
							"reviewed_by": "reviewer",
						}
					]
				},
			)


def _browser_result(scenario_id, **changes):
	result = {
		"attempts": [{"duration_ms": 12, "error": None, "kind": "initial", "number": 1, "outcome": "pass"}],
		"blocked_reason": None,
		"duration_ms": 12,
		"evidence": [],
		"error": None,
		"fallbacks": [
			{
				"effective": "Išsaugoti",
				"excluded": False,
				"exclusion_id": None,
				"key": {"context": None, "source": "Save"},
				"raw_source": "Save",
				"render_status": "unique",
				"scenario_id": scenario_id,
				"source": "frappe_lt",
				"target": {"type": "locator", "value": "body"},
				"visible": True,
			}
		],
		"id": scenario_id,
		"layouts": [],
		"ready": True,
		"status": "pass",
	}
	result.update(changes)
	return result


def _toolchain():
	return {
		"browser": {"name": "Electron", "version": "138.0.7204.185"},
		"cypress": {"version": "15.2.0"},
		"node": {"version": "v24.8.0"},
		"plugins": {},
		"schema_version": 1,
	}


class RuntimeReportTest(TestCase):
	def setUp(self):
		self.contracts = load_contracts()
		self.scenario = self.contracts["scenarios"]["scenarios"][0]

	def test_browser_result_requires_a_strict_versioned_toolchain(self):
		scenario_id = self.scenario["id"]
		browser = {
			"scenarios": [_browser_result(scenario_id)],
			"schema_version": 4,
			"toolchain": _toolchain(),
		}
		with TemporaryDirectory() as directory:
			result = validate_browser_results(browser, self.contracts["scenarios"], Path(directory))[0]
		self.assertEqual(result["status"], "pass")
		invalid = deepcopy(browser)
		invalid["toolchain"]["unexpected"] = "value"
		with TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "toolchain fields"):
			validate_browser_results(invalid, self.contracts["scenarios"], Path(directory))

	def test_visible_effective_fallback_and_functional_layout_force_failure(self):
		scenario_id = self.scenario["id"]
		browser = {
			"schema_version": 4,
			"scenarios": [
				_browser_result(
					scenario_id,
					fallbacks=[
						{
							"effective": "Save",
							"excluded": False,
							"exclusion_id": None,
							"key": {"context": "Button", "source": "Save"},
							"raw_source": "Save",
							"render_status": "unique",
							"scenario_id": scenario_id,
							"source": "missing",
							"target": {"type": "locator", "value": "button[role=submit]"},
							"visible": True,
						}
					],
					layouts=[
						{
							"detail": "button is clipped",
							"kind": "clipped",
							"scenario_id": scenario_id,
							"severity": "functional",
							"target": "button[role=submit]",
						}
					],
				)
			],
			"toolchain": _toolchain(),
		}
		with TemporaryDirectory() as directory:
			results = validate_browser_results(browser, self.contracts["scenarios"], Path(directory))
		self.assertEqual(next(result for result in results if result["id"] == scenario_id)["status"], "fail")
		self.assertTrue(
			all(result["status"] == "blocked" for result in results if result["id"] != scenario_id)
		)

	def test_every_rendered_lookup_correlation_can_prove_a_fallback(self):
		scenario_id = self.scenario["id"]
		for render_status, visible, expected in (
			("ambiguous", True, "fail"),
			("unrendered", False, "pass"),
			("unique", True, "fail"),
		):
			fallback = deepcopy(_browser_result(scenario_id)["fallbacks"][0])
			fallback.update(
				{
					"effective": "Save",
					"render_status": render_status,
					"source": "missing",
					"visible": visible,
				}
			)
			with self.subTest(render_status=render_status), TemporaryDirectory() as directory:
				results = validate_browser_results(
					{
						"schema_version": 4,
						"scenarios": [_browser_result(scenario_id, fallbacks=[fallback])],
						"toolchain": _toolchain(),
					},
					self.contracts["scenarios"],
					Path(directory),
				)
			self.assertEqual(results[0]["status"], expected)

	def test_trusted_boundary_blocks_missing_readiness_lookups_and_timeout(self):
		scenario_id = self.scenario["id"]
		for changes, reason in (
			({"ready": False}, "readiness"),
			({"fallbacks": []}, "lookup evidence"),
			({"duration_ms": self.scenario["scenario_timeout_ms"] + 1}, "timeout"),
		):
			with self.subTest(reason=reason), TemporaryDirectory() as directory:
				results = validate_browser_results(
					{
						"schema_version": 4,
						"scenarios": [_browser_result(scenario_id, **changes)],
						"toolchain": _toolchain(),
					},
					self.contracts["scenarios"],
					Path(directory),
				)
				result = next(item for item in results if item["id"] == scenario_id)
				self.assertEqual(result["status"], "blocked")
				self.assertIn(reason, result["blocked_reason"])

	def test_browser_runner_uses_absolute_paths_retains_exit_status_and_removes_transport_file(self):
		with TemporaryDirectory() as directory:
			root = Path(directory) / "run"
			root.mkdir()
			plan = Path(directory) / "plan.json"
			plan.write_text('{"scenarios": []}')
			result_path = root / "browser-results.json"
			result_path.write_text(
				json.dumps({"scenarios": [], "schema_version": 4, "toolchain": _toolchain()})
			)
			process = SimpleNamespace(
				returncode=7,
				pid=123,
				stdout=io.BytesIO(b"Authorization: Bearer cypress-secret\ntransport failed\n"),
				wait=lambda timeout=None: 7,
			)

			with patch("frappe_lt.runtime_validation.subprocess.Popen", return_value=process) as popen:
				value, returncode, diagnostic = _default_browser_runner("development.localhost", root, plan)
			self.assertEqual(
				value,
				{"scenarios": [], "schema_version": 4, "toolchain": _toolchain()},
			)
			self.assertEqual(returncode, 7)
			self.assertNotIn("cypress-secret", diagnostic)
			self.assertIn("transport failed", diagnostic)
			environment = popen.call_args.kwargs["env"]
			self.assertEqual(environment["FRAPPE_LT_RUNTIME_PLAN"], str(plan.resolve()))
			self.assertEqual(environment["FRAPPE_LT_RUNTIME_ROOT"], str(root.resolve()))
			self.assertFalse(result_path.exists())

	def test_browser_runner_keeps_bounded_diagnostic_head_and_tail(self):
		with TemporaryDirectory() as directory:
			root = Path(directory) / "run"
			root.mkdir()
			plan = Path(directory) / "plan.json"
			plan.write_text('{"scenarios": []}')
			result_path = root / "browser-results.json"
			result_path.write_text(
				json.dumps({"scenarios": [], "schema_version": 4, "toolchain": _toolchain()})
			)
			process = SimpleNamespace(
				returncode=1,
				pid=123,
				stdout=io.BytesIO(
					b"diagnostic-start\n" + b"discarded-install-output\n" * 4096 + b"terminal-cypress-error\n"
				),
				wait=lambda timeout=None: 1,
			)

			with patch("frappe_lt.runtime_validation.subprocess.Popen", return_value=process):
				_value, _returncode, diagnostic = _default_browser_runner("development.localhost", root, plan)

			self.assertIn("diagnostic-start", diagnostic)
			self.assertIn("terminal-cypress-error", diagnostic)
			self.assertIn("diagnostic output truncated", diagnostic)
			self.assertLessEqual(len(diagnostic.encode()), 65 * 1024)

	def test_evidence_is_allowlisted_bounded_and_digest_verified(self):
		scenario_id = self.scenario["id"]
		with TemporaryDirectory() as directory:
			root = Path(directory)
			path = root / "evidence" / scenario_id / "browser.json"
			path.parent.mkdir(parents=True)
			path.write_bytes(b'{"safe":true}\n')
			evidence = {
				"bytes": len(b'{"safe":true}\n'),
				"kind": "browser",
				"mime": "application/json",
				"path": f"evidence/{scenario_id}/browser.json",
				"sha256": hashlib.sha256(b'{"safe":true}\n').hexdigest(),
			}
			browser = {
				"schema_version": 4,
				"scenarios": [_browser_result(scenario_id, evidence=[evidence])],
				"toolchain": _toolchain(),
			}
			validated = validate_browser_results(
				browser, self.contracts["scenarios"], root, diagnostic_sampling=True
			)
			self.assertEqual(validated[0]["evidence"], [evidence])
			evidence["sha256"] = "0" * 64
			with self.assertRaisesRegex(ValueError, "digest mismatch"):
				validate_browser_results(browser, self.contracts["scenarios"], root, diagnostic_sampling=True)

	def test_pass_evidence_requires_explicit_diagnostic_sampling(self):
		scenario_id = self.scenario["id"]
		with TemporaryDirectory() as directory:
			root = Path(directory)
			path = root / "evidence" / scenario_id / "browser.json"
			path.parent.mkdir(parents=True)
			content = b'{"status":"pass"}\n'
			path.write_bytes(content)
			evidence = {
				"bytes": len(content),
				"kind": "browser",
				"mime": "application/json",
				"path": f"evidence/{scenario_id}/browser.json",
				"sha256": hashlib.sha256(content).hexdigest(),
			}
			browser = {
				"scenarios": [_browser_result(scenario_id, evidence=[evidence])],
				"schema_version": 4,
				"toolchain": _toolchain(),
			}
			with self.assertRaisesRegex(ValueError, "diagnostic sampling"):
				validate_browser_results(browser, self.contracts["scenarios"], root)
			validated = validate_browser_results(
				browser,
				self.contracts["scenarios"],
				root,
				diagnostic_sampling=True,
			)
			self.assertEqual(validated[0]["evidence"], [evidence])

	def test_unredacted_encoded_reset_link_is_removed_and_blocks_the_scenario(self):
		scenario_id = self.scenario["id"]
		with TemporaryDirectory() as directory:
			root = Path(directory)
			path = root / "evidence" / scenario_id / "email.txt"
			path.parent.mkdir(parents=True)
			content = b"https://example.invalid/update-password%3Fkey%3Dsecret-value"
			path.write_bytes(content)
			browser = {
				"scenarios": [
					_browser_result(
						scenario_id,
						evidence=[
							{
								"bytes": len(content),
								"kind": "email",
								"mime": "text/plain",
								"path": f"evidence/{scenario_id}/email.txt",
								"sha256": hashlib.sha256(content).hexdigest(),
							}
						],
					)
				],
				"schema_version": 4,
				"toolchain": _toolchain(),
			}
			with self.assertRaisesRegex(ValueError, "disallowed sensitive material"):
				validate_browser_results(browser, self.contracts["scenarios"], root, diagnostic_sampling=True)
			self.assertFalse(path.exists())

	def test_unreviewed_runtime_exclusion_cannot_hide_visible_fallback(self):
		scenario_id = self.scenario["id"]
		fallback = {
			"effective": "Save",
			"excluded": True,
			"exclusion_id": "not-reviewed",
			"key": {"context": None, "source": "Save"},
			"raw_source": "Save",
			"render_status": "unique",
			"scenario_id": scenario_id,
			"source": "missing",
			"target": {"type": "locator", "value": "button"},
			"visible": True,
		}
		browser = {
			"schema_version": 4,
			"scenarios": [_browser_result(scenario_id, fallbacks=[fallback])],
			"toolchain": _toolchain(),
		}
		with TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "exact reviewed"):
			validate_browser_results(browser, self.contracts["scenarios"], Path(directory))

	def test_rendered_lookup_must_belong_to_authenticated_inventory(self):
		scenario_id = self.scenario["id"]
		fallback = deepcopy(_browser_result(scenario_id)["fallbacks"][0])
		fallback.update(
			{
				"effective": "frappe-lt-dynamic-value",
				"key": {"context": None, "source": "frappe-lt-dynamic-value"},
				"raw_source": "frappe-lt-dynamic-value",
				"source": "missing",
			}
		)
		with (
			TemporaryDirectory() as directory,
			self.assertRaisesRegex(ValueError, "authenticated Release Inventory"),
		):
			validate_browser_results(
				{
					"schema_version": 4,
					"scenarios": [_browser_result(scenario_id, fallbacks=[fallback])],
					"toolchain": _toolchain(),
				},
				self.contracts["scenarios"],
				Path(directory),
			)

		fallback.update(
			{
				"render_status": "unrendered",
				"target": {"type": "locator", "value": "body[data-route]"},
				"visible": False,
			}
		)
		with TemporaryDirectory() as directory:
			results = validate_browser_results(
				{
					"schema_version": 4,
					"scenarios": [_browser_result(scenario_id, fallbacks=[fallback])],
					"toolchain": _toolchain(),
				},
				self.contracts["scenarios"],
				Path(directory),
			)
		self.assertEqual(results[0]["status"], "blocked")
		self.assertIn("no active effective translation lookup", results[0]["blocked_reason"])

	def test_reviewed_output_exclusion_keeps_its_exact_captured_interval(self):
		scenario = next(
			item for item in self.contracts["scenarios"]["scenarios"] if item["id"] == "todo-standard-print"
		)
		fallback = {
			"effective": "frappe-lt-runtime-" + "a" * 32 + "-todo",
			"excluded": True,
			"exclusion_id": "fixture-values",
			"key": {"context": None, "source": "Runtime fixture"},
			"raw_source": "Runtime fixture",
			"render_status": "unique",
			"scenario_id": scenario["id"],
			"source": "missing",
			"target": {"type": "output_interval", "value": "print:http-body:41-96"},
			"visible": True,
		}
		with TemporaryDirectory() as directory:
			result = validate_browser_results(
				{
					"scenarios": [_browser_result(scenario["id"], fallbacks=[fallback])],
					"schema_version": 4,
					"toolchain": _toolchain(),
				},
				{"scenarios": [scenario]},
				Path(directory),
			)[0]
		self.assertTrue(result["fallbacks"][0]["excluded"])
		self.assertEqual(result["fallbacks"][0]["target"]["value"], "print:http-body:41-96")

	def test_result_precedence_preserves_scenario_gap_finding_and_cleanup_facts(self):
		scenario_id = self.scenario["id"]
		result = _browser_result(
			scenario_id,
			error="original scenario assertion",
			status="fail",
			fallbacks=[
				{
					"effective": "Save",
					"excluded": False,
					"exclusion_id": None,
					"key": {"context": None, "source": "Save"},
					"raw_source": "Save",
					"render_status": "ambiguous",
					"scenario_id": scenario_id,
					"source": "missing",
					"target": {"type": "locator", "value": "button"},
					"visible": True,
				}
			],
		)
		report = _build_report(
			run_id="a" * 32,
			site="development.localhost",
			environment=None,
			discovery={"candidates": [], "collector_counts": {}},
			coverage_result={"covered": [], "gaps": ["page:Gap"], "reviewed_exclusions": []},
			results=[result],
			cleanup_failures=[
				{
					"error": "still present",
					"mutation_id": 1,
					"target": {"doctype": "Item", "name": "marked"},
				},
				{
					"error": "restore failed",
					"mutation_id": 2,
					"target": {"doctype": "User", "name": "marked-user"},
				},
			],
			stale_recoveries=[],
			durations={"cleanup": 1, "discovery": 1, "preflight": 1, "scenarios": 1, "total": 4},
			tool_errors=[],
			toolchain=_toolchain(),
			diagnostic_sampling=True,
			cypress_diagnostic_log="Authorization: Bearer report-secret",
		)
		self.assertEqual(report["status"], "fail")
		self.assertEqual(report["schema_version"], 3)
		self.assertEqual(report["toolchain"], _toolchain())
		self.assertTrue(report["diagnostic_sampling"])
		self.assertNotIn("report-secret", report["cypress_diagnostic_log"])
		self.assertEqual(
			set(report),
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
		)
		self.assertEqual(
			{cause["type"] for cause in report["blocking_causes"]},
			{"cleanup_failure", "runtime_coverage_gap", "scenario_fail"},
		)
		self.assertEqual(report["summary"]["english_fallbacks"], 1)
		self.assertEqual(report["scenario_results"][0]["status"], "fail")
		self.assertEqual(report["scenario_results"][0]["error"], "original scenario assertion")
		self.assertEqual(
			[(failure["target"]["doctype"], failure["error"]) for failure in report["cleanup_failures"]],
			[("Item", "still present"), ("User", "restore failed")],
		)

	def test_browser_retry_is_limited_to_named_setup_or_transport_failures(self):
		scenario_id = self.scenario["id"]
		browser = {
			"schema_version": 4,
			"scenarios": [
				_browser_result(
					scenario_id,
					attempts=[
						{
							"duration_ms": 2,
							"error": "assertion failed",
							"kind": "initial",
							"number": 1,
							"outcome": "assertion_failure",
						},
						{
							"duration_ms": 2,
							"error": None,
							"kind": "setup",
							"number": 2,
							"outcome": "pass",
						},
					],
				)
			],
			"toolchain": _toolchain(),
		}
		with TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "assertion failures"):
			validate_browser_results(browser, self.contracts["scenarios"], Path(directory))

	def test_browser_attempt_records_outcome_duration_and_redacted_error(self):
		scenario_id = self.scenario["id"]
		browser = {
			"schema_version": 4,
			"scenarios": [
				_browser_result(
					scenario_id,
					attempts=[
						{
							"duration_ms": 7,
							"error": "token=attempt-secret",
							"kind": "initial",
							"number": 1,
							"outcome": "assertion_failure",
						}
					],
					error="token=scenario-secret",
					status="fail",
				)
			],
			"toolchain": _toolchain(),
		}
		with TemporaryDirectory() as directory:
			result = validate_browser_results(browser, self.contracts["scenarios"], Path(directory))[0]
		self.assertEqual(result["attempts"][0]["duration_ms"], 7)
		self.assertEqual(result["attempts"][0]["outcome"], "assertion_failure")
		self.assertNotIn("attempt-secret", result["attempts"][0]["error"])

	def test_command_exit_codes_and_report_write_failure(self):
		contracts = self.contracts
		browser = {
			"schema_version": 4,
			"scenarios": [
				_browser_result(scenario["id"]) for scenario in contracts["scenarios"]["scenarios"]
			],
			"toolchain": _toolchain(),
		}
		environment = {
			"babel": "2.16.0",
			"installed_apps": ["frappe", "erpnext", "frappe_lt"],
			"inventory_digest": "0" * 64,
			"python": "3.14.4",
			"site": "development.localhost",
			"upstream": {
				"frappe": {"commit": "1" * 40, "path": "/bench/apps/frappe", "version": "16.34.0"},
				"erpnext": {"commit": "2" * 40, "path": "/bench/apps/erpnext", "version": "16.35.0"},
			},
		}

		class Control:
			prepared_diagnostic_sampling = None

			def __init__(self, *_args):
				self.recoveries = []

			def lease(self):
				return nullcontext()

			def recover_stale(self):
				return []

			def start(self):
				pass

			def prepare(self, *_args, **kwargs):
				type(self).prepared_diagnostic_sampling = kwargs["diagnostic_sampling"]
				return {"browser_plan": "/private/plan.json"}

			def cleanup(self):
				return []

			def set_original_error(self, _error):
				pass

		fake_frappe = SimpleNamespace(local=SimpleNamespace(site="development.localhost"))
		discovery = {"candidates": [], "collector_counts": {"metadata": 1}}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			with (
				patch("frappe_lt.runtime_validation.load_contracts", return_value=contracts),
				patch("frappe_lt.runtime_validation.verify_environment", return_value=environment),
				patch("frappe_lt.runtime_validation.discover", return_value=discovery),
				patch(
					"frappe_lt.runtime_validation.coverage",
					return_value={"covered": [], "gaps": [], "reviewed_exclusions": []},
				),
				patch("frappe_lt.runtime_validation.SiteControl", Control),
			):
				result = run(
					"development.localhost",
					str(root / "pass"),
					browser_runner=lambda *_args: browser,
					diagnostic_sampling=True,
					frappe_module=fake_frappe,
					run_id="a" * 32,
				)
				self.assertEqual(result["exit_code"], 0)
				self.assertTrue(Control.prepared_diagnostic_sampling)
				report = json.loads(Path(result["report"]).read_bytes())
				self.assertTrue(report["diagnostic_sampling"])
				self.assertEqual(report["toolchain"], _toolchain())

			with (
				patch("frappe_lt.runtime_validation.load_contracts", return_value=contracts),
				patch("frappe_lt.runtime_validation.verify_environment", return_value=environment),
				patch("frappe_lt.runtime_validation.discover", return_value=discovery),
				patch(
					"frappe_lt.runtime_validation.coverage",
					return_value={"covered": [], "gaps": ["page:Gap"], "reviewed_exclusions": []},
				),
				patch("frappe_lt.runtime_validation.SiteControl", Control),
			):
				result = run(
					"development.localhost",
					str(root / "fail"),
					browser_runner=lambda *_args: browser,
					frappe_module=fake_frappe,
					run_id="b" * 32,
				)
				self.assertEqual(result["exit_code"], 1)

			with (
				patch("frappe_lt.runtime_validation.load_contracts", return_value=contracts),
				patch("frappe_lt.runtime_validation.verify_environment", return_value=environment),
				patch("frappe_lt.runtime_validation.discover", return_value=discovery),
				patch(
					"frappe_lt.runtime_validation.coverage",
					return_value={"covered": [], "gaps": [], "reviewed_exclusions": []},
				),
				patch("frappe_lt.runtime_validation.SiteControl", Control),
			):

				def transport_failure(*_args):
					raise BrowserRunnerError(
						"Cypress transport failed",
						"Authorization: Bearer transport-secret\nconnection refused",
					)

				result = run(
					"development.localhost",
					str(root / "transport-failure"),
					browser_runner=transport_failure,
					frappe_module=fake_frappe,
					run_id="d" * 32,
				)
				self.assertEqual(result["exit_code"], 1)
				report = json.loads(Path(result["report"]).read_bytes())
				self.assertNotIn("transport-secret", report["cypress_diagnostic_log"])
				self.assertIn("connection refused", report["cypress_diagnostic_log"])

			escaped = deepcopy(browser)
			escaped["scenarios"][0]["evidence"] = [
				{
					"bytes": 0,
					"kind": "browser",
					"mime": "application/json",
					"path": "../escaped.json",
					"sha256": hashlib.sha256(b"").hexdigest(),
				}
			]
			with (
				patch("frappe_lt.runtime_validation.load_contracts", return_value=contracts),
				patch("frappe_lt.runtime_validation.verify_environment", return_value=environment),
				patch("frappe_lt.runtime_validation.discover", return_value=discovery),
				patch(
					"frappe_lt.runtime_validation.coverage",
					return_value={"covered": [], "gaps": [], "reviewed_exclusions": []},
				),
				patch("frappe_lt.runtime_validation.SiteControl", Control),
			):
				result = run(
					"development.localhost",
					str(root / "evidence-failure"),
					browser_runner=lambda *_args: escaped,
					frappe_module=fake_frappe,
					run_id="e" * 32,
				)
				self.assertEqual(result["exit_code"], 1)
				report = json.loads(Path(result["report"]).read_bytes())
				self.assertTrue(all(item["status"] == "blocked" for item in report["scenario_results"]))
				self.assertTrue(any(cause["type"] == "tool_error" for cause in report["blocking_causes"]))

			with (
				patch("frappe_lt.runtime_validation.load_contracts", return_value=contracts),
				patch("frappe_lt.runtime_validation.verify_environment", return_value=environment),
				patch("frappe_lt.runtime_validation.discover", return_value=discovery),
				patch(
					"frappe_lt.runtime_validation.coverage",
					return_value={"covered": [], "gaps": [], "reviewed_exclusions": []},
				),
				patch("frappe_lt.runtime_validation.SiteControl", Control),
				patch("frappe_lt.runtime_validation._write_reports", side_effect=OSError("disk full")),
				self.assertRaisesRegex(OSError, "disk full"),
			):
				run(
					"development.localhost",
					str(root / "write-failure"),
					browser_runner=lambda *_args: browser,
					frappe_module=fake_frappe,
					run_id="c" * 32,
				)

	def test_machine_report_is_the_last_commit_marker_on_partial_write_failure(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			calls = []

			def replace(source, target):
				calls.append(Path(target).name)
				if Path(target).name == "runtime-report.json":
					raise OSError("machine report replace failed")
				os.replace(source, target)

			with self.assertRaisesRegex(OSError, "machine report replace failed"):
				_write_reports(root, b"{}\n", b"report\n", replace=replace)
			self.assertEqual(calls, ["runtime-report.md", "runtime-report.json"])
			self.assertFalse((root / "runtime-report.md").exists())
			self.assertFalse((root / "runtime-report.json").exists())


class RuntimeCrashRecoveryTest(TestCase):
	def test_login_metadata_is_serialized_before_the_durable_journal_write(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			user = "runtime-user@example.invalid"

			class DB:
				def get_value(self, _doctype, _name, _fields, *, as_dict):
					if not as_dict:
						raise AssertionError("login metadata must be read as a mapping")
					return {
						"last_active": datetime(2026, 9, 21, 16, 20, 12, 123456),
						"last_ip": "127.0.0.1",
						"last_login": None,
					}

			class Frappe:
				db = DB()

				def get_site_path(self, *parts):
					return str(root.joinpath(*parts))

			control = SiteControl(Frappe(), "development.localhost", "a" * 32, site_path=root)
			control.start()
			journal = json.loads(control.journal_path.read_bytes())
			journal["login_baseline"] = {
				"activity_logs": [],
				"sessions": [],
				"user_values": {
					user: {
						"after": None,
						"before": {field: None for field in ("last_active", "last_ip", "last_login")},
					}
				},
				"users": [user],
			}
			_write_durable(control.journal_path, journal)

			control.after_runtime_login(user)

			after = json.loads(control.journal_path.read_bytes())["login_baseline"]["user_values"][user][
				"after"
			]
			self.assertEqual(after["last_active"], "2026-09-21 16:20:12.123456")
			self.assertEqual(after["last_ip"], "127.0.0.1")

	def test_role_profile_defaults_are_deterministic_journaled_and_recovered(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			documents = {}
			cleared_users = []

			class DB:
				def exists(self, doctype, name):
					return (doctype, name) in documents

				def delete(self, _doctype, _filters):
					pass

				def commit(self):
					pass

				def rollback(self):
					pass

				def sql(self, _query, _values=None):
					return []

			class Document:
				def __init__(self, values):
					self.values = values
					self.name = None
					self.flags = SimpleNamespace(name_set=False)

				def insert(self, **_kwargs):
					self.values["name"] = self.name
					documents[(self.values["doctype"], self.name)] = dict(self.values)

			class Frappe:
				db = DB()
				enqueue = None
				sendmail = None

				def get_site_path(self, *parts):
					return str(root.joinpath(*parts))

				def get_doc(self, doctype, name=None):
					if isinstance(doctype, dict):
						return Document(dict(doctype))
					return SimpleNamespace(**documents[(doctype, name)])

				def delete_doc(self, doctype, name, **_kwargs):
					documents.pop((doctype, name), None)

				def clear_cache(self, *, user):
					cleared_users.append(user)

				def get_all(self, *_args, **_kwargs):
					return []

			run_id = "a" * 32
			user = f"frappe-lt-runtime-{run_id}-accounts-user@invalid.example"
			defaults = {
				"date_format": "yyyy-mm-dd",
				"first_day_of_the_week": "Monday",
				"number_format": "# ###,##",
				"time_format": "HH:mm",
				"time_zone": "Europe/Vilnius",
			}
			control = SiteControl(Frappe(), "development.localhost", run_id, site_path=root)
			control.start()
			control._create_role_profile_defaults(user, defaults)

			expected_names = [
				_runtime_default_value_name(control.marker, user, key) for key in sorted(defaults)
			]
			journal = json.loads(control.journal_path.read_bytes())
			self.assertEqual(
				[mutation["target"] for mutation in journal["mutations"]],
				[{"doctype": "DefaultValue", "name": name} for name in expected_names],
			)
			self.assertEqual(
				set(documents),
				{("DefaultValue", name) for name in expected_names},
			)
			for key, name in zip(sorted(defaults), expected_names, strict=True):
				self.assertTrue(name.startswith(f"{control.marker}-default-"))
				self.assertEqual(
					documents[("DefaultValue", name)],
					{
						"defkey": key,
						"defvalue": defaults[key],
						"doctype": "DefaultValue",
						"name": name,
						"parent": user,
						"parentfield": "defaults",
						"parenttype": "User",
					},
				)

			recovery = SiteControl(Frappe(), "development.localhost", "b" * 32, site_path=root)
			self.assertEqual(recovery.recover_stale(), [])
			self.assertEqual(documents, {})
			self.assertFalse(control.journal_path.exists())
			self.assertEqual(cleared_users, [user] * 6)

	def test_standard_http_capture_correlates_the_same_body_and_suppresses_access_log(self):
		def translator(msg, lang=None, context=None):
			non_translated_string = msg
			return "Spausdinti" if msg == "Print" else non_translated_string

		original_form = {"api": "request"}
		module_form_proxy = object()
		access_calls = []
		access_log = SimpleNamespace(
			make_access_log=lambda **kwargs: access_calls.append(("original", kwargs))
		)
		request_state = SimpleNamespace(form_dict=original_form)
		frappe = SimpleNamespace(
			_=translator,
			as_unicode=str,
			form_dict=module_form_proxy,
			local=request_state,
		)

		def get_response(route):
			self.assertEqual(route, "/printview")
			self.assertEqual(request_state.form_dict, {"doctype": "ToDo", "name": "runtime-todo"})
			translated = translator("Print")
			access_log.make_access_log(doctype="ToDo", document="runtime-todo")
			return SimpleNamespace(
				content_type="text/html; charset=utf-8",
				get_data=lambda as_text=False: f"<body>{translated}</body>" if as_text else b"",
				status_code=200,
			)

		captured = _capture_http_response(
			frappe,
			"/printview",
			form_dict={"doctype": "ToDo", "name": "runtime-todo"},
			get_response=get_response,
			access_log_module=access_log,
		)
		self.assertIs(frappe.form_dict, module_form_proxy)
		self.assertIs(request_state.form_dict, original_form)
		self.assertIsNone(request_state.frappe_lt_runtime_print_capture)
		self.assertEqual(access_calls, [])
		self.assertEqual(captured["body"], "<body>Spausdinti</body>")
		self.assertEqual(captured["status"], 200)
		self.assertEqual(captured["suppressed_access_logs"], 1)
		self.assertEqual(
			captured["lookups"],
			[
				{
					"effective": "Spausdinti",
					"key": {"context": None, "source": "Print"},
					"raw_source": "Print",
				}
			],
		)

	def test_welcome_email_subject_is_read_from_the_final_mime_message(self):
		raw = (
			"MIME-Version: 1.0\n"
			"Subject: =?utf-8?b?U3ZlaWtpIGF0dnlrxJk=?=\n"
			"Content-Type: text/plain; charset=utf-8\n\n"
			"Final body https://development.localhost/update-password?key=secret-reset-key"
		)
		redacted = _redact_email_output(raw)
		subject, visible = _email_visible_output(redacted)
		self.assertEqual(subject, "Sveiki atvykę")
		self.assertIn("Final body", visible)
		self.assertIn("key=[REDACTED]", visible)
		self.assertNotIn("secret-reset-key", redacted)

	def test_welcome_email_finalization_uses_an_unsaved_queue_with_recipient_rows(self):
		recipient = "runtime-user@invalid.example"

		class Queue:
			def __init__(self):
				self.values = {}
				self.recipients = []

			def update(self, values):
				self.values.update(values)

			def set_recipients(self, recipients):
				self.recipients = [SimpleNamespace(recipient=value) for value in recipients]

		class Context:
			def __init__(self, queue):
				self.queue = queue

			def build_message(self, target):
				self_test.assertEqual(target, recipient)
				self_test.assertEqual([row.recipient for row in self.queue.recipients], [recipient])
				return b"MIME-Version: 1.0\r\nSubject: Final\r\n\r\nBody"

		self_test = self
		queue = Queue()
		frappe = SimpleNamespace(new_doc=lambda doctype: queue, safe_decode=lambda value: value.decode())
		builder = SimpleNamespace(
			as_dict=lambda: {
				"attachments": "[]",
				"message": "intermediate",
				"recipients": [recipient],
			}
		)
		message = _finalize_recipient_message(frappe, builder, recipient, Context)
		self.assertEqual(message["message"], "MIME-Version: 1.0\r\nSubject: Final\r\n\r\nBody")
		self.assertEqual(queue.values["message"], "intermediate")

	def test_client_lookup_uses_raw_dictionary_key_while_server_lookup_normalizes(self):
		translate = ModuleType("frappe.translate")
		by_app = {
			"frappe": {"Save": "Saugoti"},
			"erpnext": {},
			"frappe_lt": {"Save": "Išsaugoti"},
		}
		translate.get_all_translations = lambda _lang: {"Save": "Išsaugoti"}
		translate.get_translations_from_apps = lambda _lang, apps: by_app[apps[0]]
		translate.get_user_translations = lambda _lang: {}
		frappe = SimpleNamespace(as_unicode=str)
		with patch.dict(sys.modules, {"frappe.translate": translate}):
			active_keys = frozenset({("Save", None)})
			client = _resolve_effective(frappe, " Save ", None, lookup_path="client", active_keys=active_keys)
			server = _resolve_effective(frappe, " Save ", None, lookup_path="server", active_keys=active_keys)
		self.assertEqual(
			client,
			{
				"active": True,
				"effective": " Save ",
				"key": {"context": None, "source": "Save"},
				"raw_source": " Save ",
				"source": "missing",
			},
		)
		self.assertEqual(server["effective"], "Išsaugoti")
		self.assertEqual(server["source"], "frappe_lt")
		self.assertEqual(server["raw_source"], " Save ")

	def test_runtime_lookups_are_bound_to_active_inventory_keys(self):
		active_keys = frozenset({("Accounting", None), ("Save", "Button")})
		self.assertEqual(
			_active_translation_key("Accounting", "Item", active_keys),
			("Accounting", None),
		)
		self.assertEqual(_active_translation_key("Save", "Button", active_keys), ("Save", "Button"))
		self.assertIsNone(_active_translation_key("2026-09-21", None, active_keys))
		self.assertIsNone(
			_active_translation_key("<div>frappe-lt-runtime-description</div>", None, active_keys)
		)

		translate = ModuleType("frappe.translate")
		translate.get_all_translations = lambda _lang: {}
		translate.get_translations_from_apps = lambda _lang, apps: {}
		translate.get_user_translations = lambda _lang: {}
		frappe = SimpleNamespace(as_unicode=str)
		with patch.dict(sys.modules, {"frappe.translate": translate}):
			contextual = _resolve_effective(
				frappe,
				"Accounting",
				"Item",
				active_keys=active_keys,
			)
			dynamic = _resolve_effective(
				frappe,
				"2026-09-21",
				None,
				active_keys=active_keys,
			)
		self.assertTrue(contextual["active"])
		self.assertEqual(contextual["key"], {"context": "Item", "source": "Accounting"})
		self.assertFalse(dynamic["active"])

	def test_cleanup_removes_only_exact_journaled_login_effects(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			sessions = {"baseline-session", "runtime-session"}
			activity_logs = {"baseline-log", "runtime-log"}
			cache_deletes = []

			class DB:
				def exists(self, _doctype, _name):
					return False

				def delete(self, doctype, filters):
					values = set(filters[next(iter(filters))][1])
					(sessions if doctype == "Sessions" else activity_logs).difference_update(values)

				def commit(self):
					pass

				def rollback(self):
					pass

				def sql(self, query, values=None):
					if "where sid in" in query:
						return [(sid,) for sid in sorted(sessions & set(values["sids"]))]
					if "where user like" in query:
						prefix = values[0][:-1]
						return [(user,) for user in sorted(sessions) if user.startswith(prefix)]
					if "from `__Auth`" in query:
						return []
					raise AssertionError(query)

			class Frappe:
				db = DB()
				cache = SimpleNamespace(hdel=lambda *args: cache_deletes.append(args))

				def get_site_path(self, *parts):
					return str(root.joinpath(*parts))

				def get_all(self, doctype, **kwargs):
					if doctype == "Sessions":
						raise AssertionError("Sessions is not a DocType")
					if doctype == "Activity Log":
						if "name" in kwargs.get("filters", {}):
							return sorted(activity_logs & set(kwargs["filters"]["name"][1]))
						if "user" in kwargs.get("filters", {}):
							return []
						return sorted(activity_logs)
					return []

			control = SiteControl(Frappe(), "development.localhost", "a" * 32, site_path=root)
			self.assertTrue(control.root.is_absolute())
			control.start()
			journal = json.loads(control.journal_path.read_bytes())
			journal["login_baseline"] = {
				"activity_logs": ["runtime-log"],
				"sessions": ["runtime-session"],
				"user_values": {
					"Administrator": {
						"after": None,
						"before": {field: None for field in ("last_active", "last_ip", "last_login")},
					}
				},
				"users": ["Administrator"],
			}
			_write_durable(control.journal_path, journal)
			self.assertEqual(control.cleanup(), [])
			self.assertEqual(sessions, {"baseline-session"})
			self.assertEqual(activity_logs, {"baseline-log"})
			self.assertEqual(cache_deletes, [("session", "runtime-session")])

	def test_residue_scan_covers_generated_children_and_deduplicates_overlapping_links(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)

			class DB:
				def sql(self, _query, _values=None):
					return []

			class Frappe:
				db = DB()

				def get_site_path(self, *parts):
					return str(root.joinpath(*parts))

				def get_all(self, doctype, **kwargs):
					if doctype == "Dynamic Link":
						return [{"name": "linked-row"}]
					if doctype == "Notification Type Preference":
						return [{"name": "preference-row"}]
					if doctype == "UOM Conversion Detail":
						return [{"name": "uom-row"}]
					return []

			control = SiteControl(Frappe(), "development.localhost", "a" * 32, site_path=root)
			residue = control.residue_scan()
			self.assertEqual(
				[(finding["target"]["doctype"], finding["target"]["name"]) for finding in residue],
				[
					("Dynamic Link", "linked-row"),
					("Notification Type Preference", "preference-row"),
					("UOM Conversion Detail", "uom-row"),
				],
			)

	def test_server_lookup_capture_observes_preimported_translator_references(self):
		def translator(msg, lang=None, context=None):
			non_translated_string = msg
			key = f"{str(msg).strip()}:{context}" if context else str(msg).strip()
			return {"Hello {0}:Greeting": "Sveiki, {0}"}.get(key, non_translated_string)

		frappe = SimpleNamespace(_=translator, as_unicode=str)
		preimported = translator
		previous = sys.getprofile()
		with _capture_server_lookups(frappe) as lookups:
			self.assertEqual(preimported(" Hello {0} ", context="Greeting"), "Sveiki, {0}")
			self.assertEqual(preimported(" Open\n\t", context="ToDo"), " Open\n\t")
		self.assertIs(sys.getprofile(), previous)
		self.assertEqual(
			lookups,
			[
				{
					"effective": "Sveiki, {0}",
					"key": {"context": "Greeting", "source": "Hello {0}"},
					"raw_source": " Hello {0} ",
				},
				{
					"effective": " Open\n\t",
					"key": {"context": "ToDo", "source": "Open"},
					"raw_source": " Open\n\t",
				},
			],
		)

	def test_concurrent_processes_append_every_journal_mutation(self):
		script = r"""
import sys
from pathlib import Path
from frappe_lt.runtime_control import SiteControl

class DB:
    def exists(self, _doctype, _name): return False

class Frappe:
    db = DB()
    def get_site_path(self, *parts): return str(Path(sys.argv[1]).joinpath(*parts))

SiteControl(
    Frappe(), "development.localhost", "a" * 32, site_path=Path(sys.argv[1])
).before_document_mutation("Item", sys.argv[2])
"""
		with TemporaryDirectory() as directory:
			root = Path(directory)

			class DB:
				def exists(self, _doctype, _name):
					return False

				def delete(self, _doctype, _filters):
					pass

				def commit(self):
					pass

				def rollback(self):
					pass

				def sql(self, _query, _values=None):
					return []

			class Frappe:
				db = DB()
				enqueue = None
				sendmail = None

				def get_site_path(self, *parts):
					return str(root.joinpath(*parts))

				def get_all(self, *_args, **_kwargs):
					return []

			control = SiteControl(Frappe(), "development.localhost", "a" * 32, site_path=root)
			control.start()
			processes = [
				subprocess.Popen([sys.executable, "-c", script, str(root), f"item-{index}"])
				for index in range(8)
			]
			self.assertEqual([process.wait() for process in processes], [0] * len(processes))
			journal = json.loads(control.journal_path.read_bytes())
			self.assertEqual([mutation["id"] for mutation in journal["mutations"]], list(range(1, 9)))
			self.assertEqual(
				sorted(mutation["target"]["name"] for mutation in journal["mutations"]),
				[f"item-{index}" for index in range(8)],
			)
			self.assertEqual(control.cleanup(), [])

	def test_error_recording_cannot_recreate_a_cleaned_journal(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)

			class DB:
				def commit(self):
					pass

				def rollback(self):
					pass

				def sql(self, _query, _values=None):
					return []

			class Frappe:
				db = DB()

				def get_site_path(self, *parts):
					return str(root.joinpath(*parts))

				def get_all(self, *_args, **_kwargs):
					return []

			control = SiteControl(Frappe(), "development.localhost", "a" * 32, site_path=root)
			control.start()
			self.assertEqual(control.cleanup(), [])
			control.set_original_error("late failure")
			self.assertFalse(control.journal_path.exists())

	def test_cleanup_uses_permanent_deletion(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			deleted = []
			deleted_audits = []

			class DB:
				present = False

				def exists(self, _doctype, _name):
					return self.present

				def commit(self):
					pass

				def delete(self, doctype, filters):
					deleted_audits.append((doctype, filters))

				def rollback(self):
					pass

				def sql(self, _query, _values=None):
					return []

			class Frappe:
				db = DB()
				enqueue = None
				sendmail = None

				def get_site_path(self, *parts):
					return str(root.joinpath(*parts))

				def delete_doc(self, doctype, name, **kwargs):
					deleted.append((doctype, name, kwargs))
					self.db.present = False

				def get_all(self, *_args, **_kwargs):
					return []

			control = SiteControl(Frappe(), "development.localhost", "a" * 32, site_path=root)
			control.start()
			control.before_document_mutation("User", "runtime-user")
			control.frappe.db.present = True
			self.assertEqual(control.cleanup(), [])
			self.assertEqual(len(deleted), 1)
			self.assertTrue(deleted[0][2]["delete_permanently"])
			self.assertEqual(
				deleted_audits,
				[
					(
						"Deleted Document",
						{"deleted_name": "runtime-user"},
					)
				],
			)

	def test_cleanup_restores_with_the_current_document_timestamp(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			state = {
				"doctype": "User",
				"modified": "before-runtime-action",
				"name": "runtime-user",
				"value": "before",
			}
			saved_timestamps = []

			class Document:
				def __init__(self):
					self.values = dict(state)

				@property
				def modified(self):
					return self.values["modified"]

				@modified.setter
				def modified(self, value):
					self.values["modified"] = value

				def as_dict(self, **_kwargs):
					return dict(self.values)

				def update(self, values):
					self.values.update(values)

				def save(self, **_kwargs):
					saved_timestamps.append(self.modified)
					if self.modified != state["modified"]:
						raise RuntimeError("document timestamp is stale")
					state.update(self.values)

			class DB:
				def exists(self, _doctype, _name):
					return True

				def commit(self):
					pass

				def delete(self, _doctype, _filters):
					pass

				def rollback(self):
					pass

				def sql(self, _query, _values=None):
					return []

			class Frappe:
				db = DB()
				enqueue = None
				sendmail = None

				def get_site_path(self, *parts):
					return str(root.joinpath(*parts))

				def get_doc(self, _doctype, _name):
					return Document()

				def get_all(self, *_args, **_kwargs):
					return []

			control = SiteControl(Frappe(), "development.localhost", "a" * 32, site_path=root)
			control.start()
			control.before_document_mutation("User", "runtime-user")
			state.update(modified="after-runtime-action", value="after")

			self.assertEqual(control.cleanup(), [])
			self.assertEqual(saved_timestamps, ["after-runtime-action"])
			self.assertEqual(state["value"], "before")

	def test_cleanup_preserves_prior_failures_when_residue_scan_itself_fails(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)

			class Frappe:
				def get_site_path(self, *parts):
					return str(root.joinpath(*parts))

			class Control(SiteControl):
				def _cleanup_login_effects(self, journal):
					return [
						{
							"error": "login cleanup failed",
							"mutation_id": 0,
							"target": {"doctype": "Runtime Login Effects", "name": journal["run_id"]},
						}
					]

				def _restore(self, journal):
					return [
						{
							"error": "document restore failed",
							"mutation_id": 1,
							"target": {"doctype": "Item", "name": "runtime-item"},
						}
					]

				def residue_scan(self, **kwargs):
					raise RuntimeError("residue query failed")

			control = Control(Frappe(), "development.localhost", "a" * 32, site_path=root)
			control.start()
			failures = control.cleanup()
			self.assertEqual(
				{failure["error"] for failure in failures},
				{"login cleanup failed", "document restore failed", "residue query failed"},
			)
			self.assertTrue(control.journal_path.exists())

	def test_process_effect_suppression_records_and_restores_calls(self):
		with TemporaryDirectory() as directory:
			enqueued = []
			mailed = []
			frappe = SimpleNamespace(
				enqueue=lambda *args, **kwargs: enqueued.append((args, kwargs)),
				sendmail=lambda *args, **kwargs: mailed.append((args, kwargs)),
			)
			control = SiteControl(frappe, "development.localhost", "a" * 32, site_path=Path(directory))
			with control.suppress_process_effects() as effects:
				frappe.enqueue("job", queue="short")
				frappe.sendmail(recipients=["nobody@invalid.example"])
			self.assertEqual(len(effects["enqueue"]), 1)
			self.assertEqual(len(effects["mail"]), 1)
			frappe.enqueue("restored")
			frappe.sendmail(subject="restored")
			self.assertEqual(len(enqueued), 1)
			self.assertEqual(len(mailed), 1)

	def test_next_process_recovers_a_killed_run_after_journaled_mutation(self):
		script = r"""
import json
import os
import sys
from pathlib import Path
from frappe_lt.runtime_control import SiteControl

root = Path(sys.argv[1])
state_path = root / "state.json"

class DB:
    def exists(self, doctype, name):
        return name in json.loads(state_path.read_text())
    def commit(self): pass
    def rollback(self): pass

class Frappe:
    local = type("Local", (), {"site": "development.localhost"})()
    db = DB()
    def get_site_path(self, *parts): return str(root.joinpath(*parts))
    def delete_doc(self, doctype, name, **kwargs):
        state = json.loads(state_path.read_text())
        state.pop(name, None)
        state_path.write_text(json.dumps(state))
    def get_all(self, *args, **kwargs): return []

control = SiteControl(Frappe(), "development.localhost", "a" * 32, site_path=root)
control.start()
control.secret_path.write_text("credential material")
control.before_document_mutation("Item", "frappe-lt-runtime-killed")
state_path.write_text(json.dumps({"frappe-lt-runtime-killed": {"doctype": "Item", "name": "frappe-lt-runtime-killed"}}))
control.set_original_error("original assertion failed")
os._exit(23)
"""
		with TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "state.json").write_text("{}")
			completed = subprocess.run([sys.executable, "-c", script, str(root)], check=False)
			self.assertEqual(completed.returncode, 23)
			self.assertTrue((root / "private" / "frappe_lt_runtime" / "journal.json").is_file())

			state_path = root / "state.json"

			class DB:
				def exists(self, _doctype, name):
					return name in json.loads(state_path.read_text())

				def delete(self, _doctype, _filters):
					pass

				def commit(self):
					pass

				def rollback(self):
					pass

				def sql(self, _query, _values=None):
					return []

			class Frappe:
				local = SimpleNamespace(site="development.localhost")
				db = DB()

				def get_site_path(self, *parts):
					return str(root.joinpath(*parts))

				def delete_doc(self, _doctype, name, **_kwargs):
					state = json.loads(state_path.read_text())
					state.pop(name, None)
					state_path.write_text(json.dumps(state))

				def get_all(self, *_args, **_kwargs):
					return []

			from frappe_lt.runtime_control import SiteControl

			control = SiteControl(Frappe(), "development.localhost", "b" * 32, site_path=root)
			self.assertEqual(control.recover_stale(), [])
			self.assertEqual(json.loads(state_path.read_text()), {})
			self.assertEqual(control.recoveries[0]["original_error"], "original assertion failed")
			self.assertFalse((root / "private" / "frappe_lt_runtime" / f"{'a' * 32}.browser.json").exists())
			self.assertFalse(control.journal_path.exists())

	def test_blocked_result_is_canonical_and_does_not_hide_reason(self):
		first = blocked_result("scenario", "fixture unavailable")
		second = blocked_result("scenario", "fixture unavailable")
		self.assertEqual(canonical_json(first), canonical_json(second))
		self.assertEqual(first["status"], "blocked")
		self.assertEqual(first["blocked_reason"], "fixture unavailable")
