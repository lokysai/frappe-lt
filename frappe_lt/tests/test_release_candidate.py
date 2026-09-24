import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

from frappe_lt import measurements
from frappe_lt.inventory import canonical_json, load_compatibility
from frappe_lt.release_catalog import verify_release
from frappe_lt.review_evidence import REASONS
from frappe_lt.runtime_contracts import load_contracts
from frappe_lt.runtime_discovery import coverage, validate_candidate_snapshot
from frappe_lt.runtime_validation import _build_report, validate_machine_report

CANDIDATE = {"clean": True, "commit": "a" * 40}


def _site_frappe(site_root):
	return SimpleNamespace(
		get_site_path=lambda *parts: str(site_root.joinpath(*parts)),
		local=SimpleNamespace(site="development.localhost"),
	)


def _quality():
	counts = {reason: 0 for reason in REASONS}
	counts["accepted_as_is"] = 1
	counts["approved_translation_exception"] = 1
	counts["grammar_correction"] = 7
	return {
		"corrected_inherited": 7,
		"reason_counts": counts,
		"translation_coverage": {"covered": 9, "total": 9},
		"translation_exceptions": 1,
	}


def _environment(release, apps):
	pins = load_compatibility()["upstream"]
	return {
		"active_directory": "/private/bench/sites",
		"babel": "2.16.0",
		"installed_apps": apps,
		"inventory_digest": release["inventory_digest"],
		"mo_sha256": release["mo_sha256"] if "frappe_lt" in apps else None,
		"python": "3.14.4",
		"site": "development.localhost",
		"upstream": {
			app: {"commit": pin["commit"], "path": f"/private/{app}", "version": pin["version"]}
			for app, pin in sorted(pins.items())
		},
	}


def _public_upstream(environment):
	return {
		app: {"commit": pin["commit"], "version": pin["version"]}
		for app, pin in environment["upstream"].items()
	}


def _toolchain():
	return {
		"browser": {"name": "Electron", "version": "138.0.0"},
		"cypress": {"version": "13.17.0"},
		"node": {"version": "v24.8.0"},
		"plugins": {},
		"schema_version": 1,
	}


def _discovery(contracts):
	ids = {
		*(scenario["candidate_id"] for scenario in contracts["scenarios"]["scenarios"]),
		*(item["candidate_id"] for item in contracts["classifications"]["classifications"]),
	}
	candidates = []
	for candidate_id in sorted(ids):
		kind, identity = candidate_id.rsplit(":", 1)
		candidates.append({"app": "frappe", "id": candidate_id, "identity": unquote(identity), "type": kind})
	return {
		"candidates": candidates,
		"collector_counts": {"email": 1, "metadata": 1, "portal": 1, "print": 1},
	}


def _scenario_result(scenario_id):
	return {
		"attempts": [{"duration_ms": 12, "error": None, "kind": "initial", "number": 1, "outcome": "pass"}],
		"blocked_reason": None,
		"duration_ms": 12,
		"error": None,
		"evidence": [],
		"fallbacks": [
			{
				"active": True,
				"effective": "Prekė",
				"excluded": False,
				"exclusion_id": None,
				"key": {"context": None, "source": "Item"},
				"preserved_tokens": "valid",
				"raw_source": "Item",
				"render_status": "unique",
				"schema_version": 1,
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


class ReleaseCandidateTest(unittest.TestCase):
	def _capture(self, root, release, environment):
		from frappe_lt import release_candidate

		artifacts = [
			release_candidate._artifact(path, relative)
			for relative, path in sorted(release_candidate.CAPTURE_ARTIFACTS.items())
		]
		by_path = {item["path"]: item["sha256"] for item in artifacts}
		return release_candidate.validate_capture(
			{
				"artifacts": artifacts,
				"candidate": CANDIDATE,
				"environment": {
					"babel": environment["babel"],
					"installed_apps": ["frappe", "erpnext"],
					"python": environment["python"],
					"upstream": _public_upstream(environment),
				},
				"quality": _quality(),
				"release": release,
				"runtime_contracts": {
					"classifications": by_path["frappe_lt/runtime_candidate_classifications.json"],
					"role_profiles": by_path["frappe_lt/runtime_role_profiles.json"],
					"scenarios": by_path["frappe_lt/runtime_scenarios.json"],
				},
				"schema_version": 1,
				"site": "development.localhost",
			}
		)

	def _runtime(self, release, environment):
		contracts = load_contracts()
		discovery = _discovery(contracts)
		coverage_result = coverage(discovery, contracts["scenarios"], contracts["classifications"])
		runtime_environment = {
			"babel": environment["babel"],
			"installed_apps": ["frappe", "erpnext", "frappe_lt"],
			"inventory_digest": release["inventory_digest"],
			"mo_sha256": release["mo_sha256"],
			"python": environment["python"],
			"upstream": _public_upstream(environment),
		}
		snapshot = validate_candidate_snapshot(
			{
				"candidate": CANDIDATE,
				"coverage": coverage_result,
				"discovery": discovery,
				"environment": {
					key: value for key, value in runtime_environment.items() if key != "mo_sha256"
				},
				"schema_version": 3,
				"site": "development.localhost",
				"site_state_sha256": "4" * 64,
			}
		)
		results = [_scenario_result(item["id"]) for item in contracts["scenarios"]["scenarios"]]
		report = _build_report(
			candidate=CANDIDATE,
			cleanup_failures=[],
			coverage_result=coverage_result,
			discovery=discovery,
			durations={"cleanup": 1, "discovery": 1, "preflight": 1, "scenarios": 1, "total": 4},
			environment=runtime_environment,
			results=results,
			run_id="c" * 32,
			site="development.localhost",
			site_state_sha256="4" * 64,
			stale_recoveries=[],
			tool_errors=[],
			toolchain=_toolchain(),
		)
		validate_machine_report(report, contracts["scenarios"])
		return snapshot, report

	def _performance(self, release, environment, root):
		now = datetime.now(UTC).isoformat()
		samples = [
			{
				"at_utc": now,
				"duration_ms": pair + (1 if variant == "enabled" else 0),
				"pair": pair,
				"temperature": temperature,
				"variant": variant,
			}
			for pair in range(1, 21)
			for variant in (("baseline", "enabled") if pair % 2 else ("enabled", "baseline"))
			for temperature in ("cold", "warm")
		]
		mo = root / "measurement.mo"
		mo.write_bytes(b"mo")
		artifact_sizes = measurements.artifact_sizes(Path(measurements.__file__).parent, mo)
		artifact_sizes["files"][0]["sha256"] = release["mo_sha256"]
		with (
			patch("frappe_lt.release_catalog.verify_release", return_value=release),
			patch("frappe_lt.release_catalog.verify_mo", return_value=(release["mo_sha256"], 2)),
			patch("frappe_lt.measurements.artifact_sizes", return_value=artifact_sizes),
		):
			value = measurements.report(
				samples,
				baseline="baseline_site",
				candidate=CANDIDATE,
				enabled="development.localhost",
				mo=mo,
				root=Path(measurements.__file__).parent,
			)
		value["upstream_pins"] = _public_upstream(environment)
		return value

	def _inputs(self, root, site_root):
		release = verify_release()
		environment = _environment(release, ["frappe", "erpnext", "frappe_lt"])
		capture = self._capture(root, release, environment)
		snapshot, runtime = self._runtime(release, environment)
		preflight = {
			"inventory_digest": release["inventory_digest"],
			"migration": {"delete": 1, "extras": 2, "overrides": 3},
			"mo_sha256": release["mo_sha256"],
			"release_digest": release["release_digest"],
			"site": "development.localhost",
			"state": "ready",
			"versions": {app: pin["version"] for app, pin in _public_upstream(environment).items()},
			"warnings": [],
		}
		install_status = {
			"installed": True,
			"maintenance_mode": True,
			"migration": "committed",
			"mo": "matched",
			"profile": "APPLIED",
			"run_id": "b" * 32,
			"site": "development.localhost",
		}
		verify = {
			**release,
			"effective_translation": "Prekė",
			"mo_bytes": 2,
			"site": "development.localhost",
			"state": "verified",
		}
		values = {
			"candidate.json": capture,
			"install-status.json": install_status,
			"install.json": {
				"maintenance_mode": 1,
				"run_id": "b" * 32,
				"site": "development.localhost",
				"state": "verified",
			},
			"migration.json": {
				"deleted": 1,
				"package_sha256": __import__(
					"frappe_lt.legacy_migration", fromlist=["PACKAGE_SHA256"]
				).PACKAGE_SHA256,
				"postcommit_drift": False,
				"run_id": "b" * 32,
				"site": "development.localhost",
				"state": "committed",
			},
			"performance.json": self._performance(release, environment, root),
			"preflight.json": preflight,
			"prepare.json": {**preflight, "run_id": "b" * 32, "state": "prepared"},
			"runtime-candidates.json": snapshot,
			"runtime-residue.json": [],
			"runtime/runtime-report.json": runtime,
			"tests.json": {
				"candidate": CANDIDATE,
				"categories": {"fault": "pass", "integration": "pass", "subprocess": "pass"},
				"provenance": {
					"repository": "lokysai/frappe-lt",
					"run_attempt": 2,
					"run_id": 123,
					"workflow": ".github/workflows/ci.yml",
				},
				"schema_version": 2,
			},
			"verify.json": verify,
		}
		for relative, value in values.items():
			path = root / relative
			path.parent.mkdir(exist_ok=True)
			path.write_bytes(canonical_json(value))
		migration_root = site_root / "private" / "frappe_lt_legacy_migration"
		migration_root.mkdir(parents=True)
		migration = {
			"deleted": 1,
			"package_sha256": __import__(
				"frappe_lt.legacy_migration", fromlist=["PACKAGE_SHA256"]
			).PACKAGE_SHA256,
			"postcommit_drift": False,
			"run_id": "b" * 32,
			"site": "development.localhost",
			"state": "committed",
		}
		migration_path = migration_root / ("b" * 32 + ".final.json")
		migration_path.write_bytes(canonical_json(migration))
		migration_path.chmod(0o600)
		return capture, environment, install_status, verify

	@contextmanager
	def _current_state(self, release_candidate, capture, environment, install_status, verify):
		current = {
			**environment,
			"upstream": {
				app: {**pin, "path": f"/private/{app}"}
				for app, pin in capture["environment"]["upstream"].items()
			},
		}
		with (
			patch.object(release_candidate, "assert_no_runtime_residue", return_value=[]),
			patch.object(release_candidate, "clean_candidate_identity", return_value=CANDIDATE),
			patch.object(release_candidate, "release_quality_summary", return_value=capture["quality"]),
			patch.object(release_candidate, "verify_environment", return_value=current),
			patch.object(
				release_candidate,
				"runtime_discover",
				return_value=self._runtime(capture["release"], environment)[0]["discovery"],
			),
			patch.object(release_candidate, "runtime_site_state_digest", return_value="4" * 64),
			patch("frappe_lt.install.status", return_value=install_status),
			patch(
				"frappe_lt.install._checked",
				return_value={
					"policy_sha256": hashlib.sha256(b"").hexdigest(),
					"release_candidate": {
						"candidate": CANDIDATE,
						"capture_sha256": hashlib.sha256(canonical_json(capture)).hexdigest(),
						"schema_version": 1,
					},
					"schema_version": 2,
				},
			),
			patch(
				"frappe_lt.legacy_migration.trusted_site_policy",
				return_value=(set(), hashlib.sha256(b"").hexdigest()),
			),
			patch("frappe_lt.verify.run", return_value=verify),
		):
			yield

	def _github_fetch(self, _url):
		if "/artifacts?" in _url:
			return {
				"artifacts": [
					{
						"expired": False,
						"id": 456,
						"name": f"candidate-tests-{CANDIDATE['commit']}-2",
						"workflow_run": {"head_sha": CANDIDATE["commit"], "id": 123},
					}
				],
				"total_count": 1,
			}
		return {
			"conclusion": "success",
			"head_sha": CANDIDATE["commit"],
			"html_url": "https://github.com/lokysai/frappe-lt/actions/runs/123",
			"id": 123,
			"path": ".github/workflows/ci.yml",
			"repository": {"full_name": "lokysai/frappe-lt"},
			"run_attempt": 2,
		}

	def test_test_evidence_blocks_when_github_is_unavailable(self):
		from frappe_lt import release_candidate

		evidence = {
			"candidate": CANDIDATE,
			"categories": {"fault": "pass", "integration": "pass", "subprocess": "pass"},
			"provenance": {
				"repository": "lokysai/frappe-lt",
				"run_attempt": 2,
				"run_id": 123,
				"workflow": ".github/workflows/ci.yml",
			},
			"schema_version": 2,
		}
		with self.assertRaisesRegex(ValueError, "unavailable"):
			release_candidate._validate_tests(
				evidence,
				CANDIDATE,
				fetch_json=lambda _url: (_ for _ in ()).throw(ValueError("unavailable")),
			)

	def test_capture_binds_clean_candidate_and_private_candidate_path(self):
		from frappe_lt import release_candidate

		release = verify_release()
		environment = _environment(release, ["frappe", "erpnext"])
		with tempfile.TemporaryDirectory() as directory:
			site_root = Path(directory) / "site"
			output = site_root / "private" / "frappe_lt_release_candidate" / CANDIDATE["commit"]
			output.mkdir(parents=True)
			frappe = _site_frappe(site_root)
			with (
				patch.object(release_candidate, "clean_candidate_identity", return_value=CANDIDATE),
				patch.object(release_candidate, "release_quality_summary", return_value=_quality()),
				patch.object(release_candidate, "verify_environment", return_value=environment),
			):
				result = release_candidate.capture("development.localhost", output, frappe_module=frappe)
			content = (output / "candidate.json").read_bytes()
			self.assertEqual(content, canonical_json(json.loads(content)))
			self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
			wrong = output.parent / ("f" * 40)
			wrong.mkdir()
			with (
				patch.object(release_candidate, "clean_candidate_identity", return_value=CANDIDATE),
				patch.object(release_candidate, "release_quality_summary", return_value=_quality()),
				patch.object(release_candidate, "verify_environment", return_value=environment),
				self.assertRaisesRegex(ValueError, "candidate SHA"),
			):
				release_candidate.capture("development.localhost", wrong, frappe_module=frappe)

	def test_finalize_validates_real_evidence_and_publishes_human_then_machine(self):
		from frappe_lt import release_candidate

		with tempfile.TemporaryDirectory() as directory:
			site_root = Path(directory) / "site"
			root = site_root / "private" / "frappe_lt_release_candidate" / CANDIDATE["commit"]
			root.mkdir(parents=True)
			capture, environment, install_status, verify = self._inputs(root, site_root)
			published = []

			def link(source, target, **kwargs):
				published.append(Path(target).name)
				os.link(source, target, **kwargs)

			with self._current_state(release_candidate, capture, environment, install_status, verify):
				result = release_candidate.finalize(
					root,
					frappe_module=_site_frappe(site_root),
					github_fetch=self._github_fetch,
					link=link,
				)
			self.assertEqual(published[-2:], ["release-candidate.md", "release-candidate.json"])
			machine = (root / "release-candidate.json").read_bytes()
			value = json.loads(machine)
			self.assertEqual(machine, canonical_json(value))
			self.assertEqual(result["sha256"], hashlib.sha256(machine).hexdigest())
			self.assertEqual(
				value["summary"]["desktop"] + value["summary"]["mobile"], len(value["runtime"]["scenarios"])
			)
			self.assertEqual(
				value["tests"]["categories"], {"fault": "pass", "integration": "pass", "subprocess": "pass"}
			)
			self.assertEqual(value["tests"]["run_attempt"], 2)
			self.assertNotIn("/private/", machine.decode())
			with patch.object(release_candidate, "release_quality_summary", return_value=capture["quality"]):
				self.assertIs(
					release_candidate.validate_final_index(value, root=root, github_fetch=self._github_fetch),
					value,
				)
			changed_quality = deepcopy(capture["quality"])
			changed_quality["reason_counts"]["accepted_as_is"] += 1
			with (
				patch.object(release_candidate, "release_quality_summary", return_value=changed_quality),
				self.assertRaisesRegex(ValueError, "catalog quality changed"),
			):
				release_candidate.validate_final_index(value, root=root, github_fetch=self._github_fetch)
			for label, mutate in (
				("clean", lambda item: item["candidate"].update(clean=False)),
				("correction total", lambda item: item["quality"].update(corrected_inherited=6)),
				("install", lambda item: item["install"].update(state="verified-looking")),
				("denominator", lambda item: item["runtime"].update(scenarios=[])),
				("verification", lambda item: item["verification"].update(state="pass")),
			):
				forged = deepcopy(value)
				mutate(forged)
				with (
					self.subTest(label=label),
					patch.object(
						release_candidate, "release_quality_summary", return_value=capture["quality"]
					),
					self.assertRaises(ValueError),
				):
					release_candidate.validate_final_index(forged, root=root, github_fetch=self._github_fetch)
			tests_path = root / "tests.json"
			tests_path.write_bytes(tests_path.read_bytes() + b" ")
			with (
				patch.object(release_candidate, "release_quality_summary", return_value=capture["quality"]),
				self.assertRaisesRegex(ValueError, "bytes or digest changed"),
			):
				release_candidate.validate_final_index(value, root=root, github_fetch=self._github_fetch)

	def test_public_final_validator_requires_a_root(self):
		from frappe_lt import release_candidate

		with self.assertRaisesRegex(ValueError, "rooted"):
			release_candidate.validate_final_index({})

	def test_finalize_rejects_tampered_candidate_runtime_tests_warning_and_artifact(self):
		from frappe_lt import release_candidate

		for relative, mutate, message in (
			("tests.json", lambda value: value["candidate"].update(commit="f" * 40), "test evidence"),
			("preflight.json", lambda value: value["warnings"].append("attacker text"), "warnings"),
			(
				"runtime-candidates.json",
				lambda value: value["discovery"]["candidates"].pop(),
				"coverage|discovery",
			),
			(
				"runtime/runtime-report.json",
				lambda value: value["candidate"].update(commit="f" * 40),
				"candidate differs",
			),
		):
			with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
				site_root = Path(directory) / "site"
				root = site_root / "private" / "frappe_lt_release_candidate" / CANDIDATE["commit"]
				root.mkdir(parents=True)
				capture, environment, install_status, verify = self._inputs(root, site_root)
				path = root / relative
				value = json.loads(path.read_bytes())
				mutate(value)
				path.write_bytes(canonical_json(value))
				with self._current_state(release_candidate, capture, environment, install_status, verify):
					with self.assertRaisesRegex(ValueError, message):
						release_candidate.finalize(
							root,
							frappe_module=_site_frappe(site_root),
							github_fetch=self._github_fetch,
						)

	def test_finalize_requires_install_capture_and_site_policy_continuity(self):
		from frappe_lt import release_candidate

		for label, install_plan, policy_digest, message in (
			(
				"capture",
				{
					"policy_sha256": hashlib.sha256(b"").hexdigest(),
					"release_candidate": {
						"candidate": {"clean": True, "commit": "f" * 40},
						"capture_sha256": "e" * 64,
						"schema_version": 1,
					},
					"schema_version": 2,
				},
				hashlib.sha256(b"").hexdigest(),
				"selected candidate capture",
			),
			(
				"policy",
				None,
				"f" * 64,
				"site-exception policy",
			),
		):
			with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
				site_root = Path(directory) / "site"
				root = site_root / "private" / "frappe_lt_release_candidate" / CANDIDATE["commit"]
				root.mkdir(parents=True)
				capture, environment, install_status, verify = self._inputs(root, site_root)
				if install_plan is None:
					install_plan = {
						"policy_sha256": hashlib.sha256(b"").hexdigest(),
						"release_candidate": {
							"candidate": CANDIDATE,
							"capture_sha256": hashlib.sha256(canonical_json(capture)).hexdigest(),
							"schema_version": 1,
						},
						"schema_version": 2,
					}
				with (
					self._current_state(release_candidate, capture, environment, install_status, verify),
					patch("frappe_lt.install._checked", return_value=install_plan),
					patch(
						"frappe_lt.legacy_migration.trusted_site_policy",
						return_value=(set(), policy_digest),
					),
					self.assertRaisesRegex(ValueError, message),
				):
					release_candidate.finalize(
						root,
						frappe_module=_site_frappe(site_root),
						github_fetch=self._github_fetch,
					)

	def test_machine_report_rejects_token_verdict_tampering(self):
		release = verify_release()
		environment = _environment(release, ["frappe", "erpnext", "frappe_lt"])
		_snapshot, report = self._runtime(release, environment)
		report["scenario_results"][0]["fallbacks"][0]["preserved_tokens"] = "mismatch"
		with self.assertRaisesRegex(ValueError, "preserved token verdict"):
			validate_machine_report(report, load_contracts()["scenarios"])

	def test_publication_failure_removes_only_its_human_inode(self):
		from frappe_lt import release_candidate

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			calls = 0

			def link(source, target, **kwargs):
				nonlocal calls
				calls += 1
				if calls == 2:
					raise FileExistsError("concurrent machine marker")
				os.link(source, target, **kwargs)

			descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
			try:
				with self.assertRaisesRegex(FileExistsError, "concurrent"):
					release_candidate._publish_final(descriptor, b"{}\n", b"human\n", link=link)
			finally:
				os.close(descriptor)
			self.assertFalse((root / "release-candidate.md").exists())
			self.assertFalse((root / "release-candidate.json").exists())

	def test_final_publication_uses_the_verified_directory_descriptor_after_root_replacement(self):
		from frappe_lt import release_candidate

		with tempfile.TemporaryDirectory() as directory:
			parent = Path(directory)
			root = parent / "candidate"
			moved = parent / "validated"
			root.mkdir()
			descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
			try:
				root.rename(moved)
				root.mkdir()
				release_candidate._publish_final(descriptor, b"{}\n", b"human\n")
			finally:
				os.close(descriptor)
			self.assertFalse((root / "release-candidate.json").exists())
			self.assertEqual((moved / "release-candidate.json").read_bytes(), b"{}\n")

	def test_descriptor_reader_rejects_parent_symlink(self):
		from frappe_lt import release_candidate

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			outside = root / "outside"
			outside.mkdir()
			(outside / "runtime-report.json").write_text("{}\n")
			(root / "runtime").symlink_to(outside, target_is_directory=True)
			descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
			try:
				with self.assertRaisesRegex(ValueError, "missing or unsafe"):
					release_candidate._read_input(descriptor, "runtime/runtime-report.json")
			finally:
				os.close(descriptor)

	def test_descriptor_reader_rejects_a_file_changed_during_read(self):
		from frappe_lt import release_candidate

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			path = root / "tests.json"
			path.write_bytes(b'{"safe":true}\n')
			descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
			real_read = os.read
			changed = False

			def changing_read(file_descriptor, count):
				nonlocal changed
				content = real_read(file_descriptor, count)
				if not changed:
					changed = True
					with path.open("ab") as stream:
						stream.write(b"x")
				return content

			try:
				with (
					patch("frappe_lt.release_candidate.os.read", side_effect=changing_read),
					self.assertRaisesRegex(ValueError, "changed while being read"),
				):
					release_candidate._read_input(descriptor, "tests.json")
			finally:
				os.close(descriptor)

	def test_finalize_rejects_a_non_site_candidate_location_before_reading_inputs(self):
		from frappe_lt import release_candidate

		with tempfile.TemporaryDirectory() as directory:
			site_root = Path(directory) / "site"
			wrong = Path(directory) / CANDIDATE["commit"]
			wrong.mkdir()
			with self.assertRaisesRegex(ValueError, "outside the active site's private"):
				release_candidate.finalize(wrong, frappe_module=_site_frappe(site_root))


if __name__ == "__main__":
	unittest.main()
