import hashlib
import json
import os
import subprocess
import sys
from contextlib import nullcontext
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
	_authorized_browser_plan,
	_capture_server_lookups,
	_write_durable,
	redact_sensitive,
)
from frappe_lt.runtime_discovery import RuntimeCandidate, coverage, discover
from frappe_lt.runtime_validation import (
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
		self.assertEqual(contracts["scenarios"]["schema_version"], 1)
		self.assertEqual(contracts["profiles"]["schema_version"], 1)
		self.assertEqual(contracts["classifications"]["schema_version"], 1)
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
			validate_role_profiles({"schema_version": 1, "profiles": [profile]})

		profiles = contracts["profiles"]["profiles"]
		with self.assertRaisesRegex(ValueError, "unique ids"):
			validate_role_profiles({"schema_version": 1, "profiles": [profiles[0], profiles[0]]})

		scenario = dict(contracts["scenarios"]["scenarios"][0])
		scenario.pop("readiness")
		with self.assertRaisesRegex(ValueError, "fields must be exactly"):
			validate_scenarios(
				{"schema_version": 1, "scenarios": [scenario]},
				{profile["id"] for profile in profiles},
			)

		with self.assertRaisesRegex(ValueError, "reviewer"):
			validate_classifications(
				{
					"schema_version": 1,
					"classifications": [
						{"candidate_id": "page:Desk", "reason": "out of scope", "reviewed_by": ""}
					],
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
			"Authorization: Bearer bearer-secret\nCookie: sid=session-secret"
		)
		for secret in ("secret-token", "hunter2", "bearer-secret", "session-secret"):
			self.assertNotIn(secret, redacted)
		self.assertGreaterEqual(redacted.count("[REDACTED]"), 4)


class RuntimeDiscoveryTest(TestCase):
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
				{"candidate_id": "page:B", "reason": "not product UI", "reviewed_by": "reviewer"}
			]
		}
		self.assertEqual(
			coverage(discovery, scenarios, classifications),
			{"covered": ["page:A"], "gaps": ["page:C"], "reviewed_out_of_scope": ["page:B"]},
		)
		with self.assertRaisesRegex(ValueError, "Manifest references candidates not found"):
			coverage(discovery, {"scenarios": [{"candidate_id": "page:Missing"}]}, classifications)


def _browser_result(scenario_id, **changes):
	result = {
		"attempts": [{"kind": "initial", "number": 1}],
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


class RuntimeReportTest(TestCase):
	def setUp(self):
		self.contracts = load_contracts()
		self.scenario = self.contracts["scenarios"]["scenarios"][0]

	def test_visible_effective_fallback_and_functional_layout_force_failure(self):
		scenario_id = self.scenario["id"]
		browser = {
			"schema_version": 2,
			"scenarios": [
				_browser_result(
					scenario_id,
					fallbacks=[
						{
							"effective": "Save",
							"excluded": False,
							"exclusion_id": None,
							"key": {"context": "Button", "source": "Save"},
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
		}
		with TemporaryDirectory() as directory:
			results = validate_browser_results(browser, self.contracts["scenarios"], Path(directory))
		self.assertEqual(next(result for result in results if result["id"] == scenario_id)["status"], "fail")
		self.assertTrue(
			all(result["status"] == "blocked" for result in results if result["id"] != scenario_id)
		)

	def test_trusted_boundary_blocks_missing_readiness_lookups_and_timeout(self):
		scenario_id = self.scenario["id"]
		for changes, reason in (
			({"ready": False}, "readiness"),
			({"fallbacks": []}, "lookup evidence"),
			({"duration_ms": self.scenario["scenario_timeout_ms"] + 1}, "timeout"),
		):
			with self.subTest(reason=reason), TemporaryDirectory() as directory:
				results = validate_browser_results(
					{"schema_version": 2, "scenarios": [_browser_result(scenario_id, **changes)]},
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
			result_path.write_text('{"scenarios": [], "schema_version": 2}')
			process = SimpleNamespace(returncode=7, pid=123, communicate=lambda timeout=None: (None, None))
			with patch("frappe_lt.runtime_validation.subprocess.Popen", return_value=process) as popen:
				value, returncode = _default_browser_runner("development.localhost", root, plan)
			self.assertEqual(value, {"scenarios": [], "schema_version": 2})
			self.assertEqual(returncode, 7)
			environment = popen.call_args.kwargs["env"]
			self.assertEqual(environment["FRAPPE_LT_RUNTIME_PLAN"], str(plan.resolve()))
			self.assertEqual(environment["FRAPPE_LT_RUNTIME_ROOT"], str(root.resolve()))
			self.assertFalse(result_path.exists())

	def test_evidence_is_allowlisted_bounded_and_digest_verified(self):
		scenario_id = self.scenario["id"]
		with TemporaryDirectory() as directory:
			root = Path(directory)
			path = root / "evidence" / scenario_id / "failure.png"
			path.parent.mkdir(parents=True)
			path.write_bytes(b"safe evidence")
			evidence = {
				"bytes": len(b"safe evidence"),
				"kind": "screenshot",
				"mime": "image/png",
				"path": f"evidence/{scenario_id}/failure.png",
				"sha256": hashlib.sha256(b"safe evidence").hexdigest(),
			}
			browser = {
				"schema_version": 2,
				"scenarios": [_browser_result(scenario_id, evidence=[evidence])],
			}
			validated = validate_browser_results(browser, self.contracts["scenarios"], root)
			self.assertEqual(validated[0]["evidence"], [evidence])
			evidence["sha256"] = "0" * 64
			with self.assertRaisesRegex(ValueError, "digest mismatch"):
				validate_browser_results(browser, self.contracts["scenarios"], root)

	def test_unreviewed_runtime_exclusion_cannot_hide_visible_fallback(self):
		scenario_id = self.scenario["id"]
		fallback = {
			"effective": "Save",
			"excluded": True,
			"exclusion_id": "not-reviewed",
			"key": {"context": None, "source": "Save"},
			"scenario_id": scenario_id,
			"source": "missing",
			"target": {"type": "locator", "value": "button"},
			"visible": True,
		}
		browser = {
			"schema_version": 2,
			"scenarios": [_browser_result(scenario_id, fallbacks=[fallback])],
		}
		with TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "exact reviewed"):
			validate_browser_results(browser, self.contracts["scenarios"], Path(directory))

	def test_result_precedence_preserves_scenario_gap_finding_and_cleanup_facts(self):
		scenario_id = self.scenario["id"]
		result = _browser_result(
			scenario_id,
			status="fail",
			fallbacks=[
				{
					"effective": "Save",
					"excluded": False,
					"exclusion_id": None,
					"key": {"context": None, "source": "Save"},
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
			coverage_result={"covered": [], "gaps": ["page:Gap"], "reviewed_out_of_scope": []},
			results=[result],
			cleanup_failures=[
				{
					"error": "still present",
					"mutation_id": 1,
					"target": {"doctype": "Item", "name": "marked"},
				}
			],
			stale_recoveries=[],
			durations={"cleanup": 1, "discovery": 1, "preflight": 1, "scenarios": 1, "total": 4},
			tool_errors=[],
		)
		self.assertEqual(report["status"], "fail")
		self.assertEqual(
			{cause["type"] for cause in report["blocking_causes"]},
			{"cleanup_failure", "runtime_coverage_gap", "scenario_fail"},
		)
		self.assertEqual(report["summary"]["english_fallbacks"], 1)
		self.assertEqual(report["scenario_results"][0]["status"], "fail")

	def test_browser_retry_is_limited_to_named_setup_or_transport_failures(self):
		scenario_id = self.scenario["id"]
		browser = {
			"schema_version": 2,
			"scenarios": [
				_browser_result(
					scenario_id,
					attempts=[{"kind": "initial", "number": 1}, {"kind": "initial", "number": 2}],
				)
			],
		}
		with TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, "may be retried"):
			validate_browser_results(browser, self.contracts["scenarios"], Path(directory))

	def test_command_exit_codes_and_report_write_failure(self):
		contracts = self.contracts
		browser = {
			"schema_version": 2,
			"scenarios": [
				_browser_result(scenario["id"]) for scenario in contracts["scenarios"]["scenarios"]
			],
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
			def __init__(self, *_args):
				self.recoveries = []

			def lease(self):
				return nullcontext()

			def recover_stale(self):
				return []

			def start(self):
				pass

			def prepare(self, *_args):
				return {"browser_plan": "/private/plan.json"}

			def cleanup(self):
				return []

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
					return_value={"covered": [], "gaps": [], "reviewed_out_of_scope": []},
				),
				patch("frappe_lt.runtime_validation.SiteControl", Control),
			):
				result = run(
					"development.localhost",
					str(root / "pass"),
					browser_runner=lambda *_args: browser,
					frappe_module=fake_frappe,
					run_id="a" * 32,
				)
				self.assertEqual(result["exit_code"], 0)

			with (
				patch("frappe_lt.runtime_validation.load_contracts", return_value=contracts),
				patch("frappe_lt.runtime_validation.verify_environment", return_value=environment),
				patch("frappe_lt.runtime_validation.discover", return_value=discovery),
				patch(
					"frappe_lt.runtime_validation.coverage",
					return_value={"covered": [], "gaps": ["page:Gap"], "reviewed_out_of_scope": []},
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
					return_value={"covered": [], "gaps": [], "reviewed_out_of_scope": []},
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
		self.assertIs(sys.getprofile(), previous)
		self.assertEqual(
			lookups,
			[
				{
					"effective": "Sveiki, {0}",
					"key": {"context": "Greeting", "source": "Hello {0}"},
				}
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
