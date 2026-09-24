import json
import re
import subprocess
from pathlib import Path
from unittest import TestCase

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
UI_RUNNER_PACKAGE_PATH = ROOT / ".github" / "ui-runner" / "package.json"
UI_RUNNER_LOCK_PATH = ROOT / ".github" / "ui-runner" / "yarn.lock"


class CIWorkflowTest(TestCase):
	@classmethod
	def setUpClass(cls):
		cls.workflow = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

	def test_all_workflow_shell_steps_parse(self):
		for name, job in self.workflow["jobs"].items():
			for step in job["steps"]:
				if "run" in step:
					with self.subTest(job=name, step=step["name"]):
						result = subprocess.run(
							["bash", "-n"], input=step["run"], text=True, capture_output=True, check=False
						)
						self.assertEqual(result.returncode, 0, result.stderr)

	def test_release_gate_is_isolated_and_preserves_active_files_on_failure(self):
		steps = self.workflow["jobs"]["verify"]["steps"]
		gate = next(
			step
			for step in steps
			if step["name"] == "Regenerate release in isolation and compare PO and MO digests"
		)
		run = gate["run"]
		for required in (
			"release_catalog.assemble(po, mo)",
			"TemporaryDirectory(",
			'assert manifest["inventory_digest"] == compatibility["inventory_digest"]',
			'assert result["po_sha256"] == manifest["po_sha256"] == state(po)[1]',
			'assert result["mo_sha256"] == manifest["mo_sha256"] == state(mo)[1]',
			"finally:",
			"assert tuple(state(path) for path in active) == before",
		):
			self.assertIn(required, run)
		self.assertNotIn('manifest["mo_sha256"] == compatibility["mo_sha256"]', run)
		self.assertLess(run.index("before ="), run.index("try:"))
		self.assertLess(run.index("release_catalog.assemble"), run.index("finally:"))
		self.assertLess(
			steps.index(next(step for step in steps if step["name"] == "Run static and unit tests")),
			steps.index(gate),
		)

	def test_install_jobs_check_predeployment_inventory_and_require_original_package(self):
		for job_name, site, inventory_name, install_name in (
			(
				"verify",
				"test_site",
				"Compare predeployment target inventory with authenticated release",
				"Prove mandatory install hook rejects an unprepared site and require original CSV",
			),
			(
				"runtime-browser",
				"development.localhost",
				"Compare runtime target inventory before deployment",
				"Prepare authenticated runtime install and configure site",
			),
		):
			steps = self.workflow["jobs"][job_name]["steps"]
			inventory = next(step for step in steps if step["name"] == inventory_name)
			install = next(step for step in steps if step["name"] == install_name)
			self.assertLess(steps.index(inventory), steps.index(install))
			self.assertIn(
				"_target_keys(frappe, verify_owned_artifacts()) == _inventory_keys()", inventory["run"]
			)
			self.assertIn("frappe.db.rollback()", inventory["run"])
			run = install["run"]
			self.assertEqual(
				install["env"]["FRAPPE_LT_ORIGINAL_CSV_URL"],
				"${{ secrets.FRAPPE_LT_ORIGINAL_CSV_URL }}",
			)
			for required in (
				f"bench --site {site} install-app frappe_lt",
				"PREPARED_INPUTS_MISSING",
				'if test -z "$FRAPPE_LT_ORIGINAL_CSV_URL"',
				"BLOCKED: FRAPPE_LT_ORIGINAL_CSV_URL secret is missing",
				'private_dir="$(mktemp -d)"',
				'package="$private_dir/lt-v16-translations.csv"',
				"curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https'",
				"PACKAGE_SHA256",
				"exit 1",
				f'bench --site {site} preflight-lithuanian-install --package "$package"',
				f'bench --site {site} prepare-lithuanian-install --package "$package"',
				f"bench --site {site} show-lithuanian-install-status",
			):
				self.assertIn(required, run)
			if job_name == "verify":
				self.assertIn('printf \'FRAPPE_LT_PACKAGE_DIR=%s\\n\' "$private_dir" >> "$GITHUB_ENV"', run)
				self.assertIn('rm -rf "$FRAPPE_LT_PACKAGE_DIR"', steps[-1]["run"])
				verify_step = next(step for step in steps if step["name"] == "Run integrated verification")
				self.assertIn("resume-lithuanian-install", verify_step["run"])
				self.assertLess(
					steps.index(verify_step),
					steps.index(
						next(step for step in steps if "set-config maintenance_mode 0" in step.get("run", ""))
					),
				)
			else:
				self.assertIn("trap 'rm -rf \"$private_dir\"' EXIT", run)
			self.assertNotIn("$GITHUB_WORKSPACE/frappe_lt/original_translations.csv", run)
			self.assertLess(run.index("PACKAGE_SHA256"), run.index("preflight-lithuanian-install"))
			self.assertLess(run.index("PREPARED_INPUTS_MISSING"), run.index("prepare-lithuanian-install"))
			self.assertLess(run.index("prepare-lithuanian-install"), run.rindex("install-app frappe_lt"))

	def test_workflow_parses_and_static_gate_runs_every_runtime_helper_test(self):
		verify = self.workflow["jobs"]["verify"]
		commands = "\n".join(step.get("run", "") for step in verify["steps"])
		for required in (
			"frappe_lt.tests.test_install",
			"frappe_lt.tests.test_release_catalog",
			"frappe_lt.tests.test_legacy_migration",
			"frappe_lt.tests.test_catalog_partition",
			"frappe_lt.tests.test_review_evidence",
			"frappe_lt.tests.test_v15_origin_import",
			"frappe_lt.tests.test_runtime_validation",
			"frappe_lt.tests.test_finance_catalog",
			"frappe_lt.tests.test_ci_workflow",
			"node --test",
			"node --check",
		):
			self.assertIn(required, commands)

	def test_wrong_digest_diagnostic_uses_pinned_release_verifier(self):
		steps = self.workflow["jobs"]["verify"]["steps"]
		gate = next(
			step for step in steps if step["name"] == "Prove wrong release MO digest fails with diagnostics"
		)
		self.assertIn("frappe_lt.verify.run", gate["run"])
		self.assertIn("requested MO digest differs from authenticated release", gate["run"])
		self.assertNotIn("first build:", gate["run"])
		self.assertNotIn("second build:", gate["run"])

	def test_finance_origin_is_exercised_by_ci_without_reverting_item_provenance(self):
		steps = self.workflow["jobs"]["verify"]["steps"]
		unit = next(step for step in steps if step["name"] == "Run static and unit tests")
		integration = next(
			step
			for step in steps
			if step["name"] == "Authenticate the production partition and run every registered candidate"
		)
		self.assertIn("frappe_lt.tests.test_finance_origin_import", unit["run"])
		self.assertIn('"origin": "inherited_v15"', integration["run"])
		self.assertIn('"reason": "accepted_as_is"', integration["run"])
		self.assertNotIn('"origin": "new_ai"', integration["run"])

	def test_runtime_browser_job_is_pinned_isolated_and_fail_closed(self):
		job = self.workflow["jobs"]["runtime-browser"]
		self.assertEqual(job["runs-on"], "ubuntu-24.04")
		self.assertEqual(set(job["services"]), {"mariadb", "redis"})
		steps = job["steps"]
		commands = "\n".join(step.get("run", "") for step in steps)
		for required in (
			'python-version: "3.14"',
			'node-version: "24"',
			"bench new-site development.localhost",
			"bench --site development.localhost install-app erpnext",
			"bench --site development.localhost install-app frappe_lt",
			"frappe,erpnext,frappe_lt",
			"bench --site development.localhost set-config allow_tests true",
			"bench build --apps frappe,erpnext,frappe_lt",
			"frappe_lt.tests.test_runtime",
			"export-lithuanian-runtime-candidates",
			"validate-lithuanian-runtime",
		):
			self.assertIn(
				required, WORKFLOW_PATH.read_text(encoding="utf-8") if ":" in required else commands
			)
		self.assertLess(
			commands.index("export-lithuanian-runtime-candidates"),
			commands.index("frappe_lt.tests.test_runtime"),
		)
		self.assertLess(
			commands.index("export-lithuanian-runtime-candidates"),
			commands.index("validate-lithuanian-runtime"),
		)
		self.assertLess(
			commands.index("--frozen-lockfile"),
			commands.index("validate-lithuanian-runtime"),
		)
		self.assertNotIn("uninstall-app", commands)
		self.assertNotIn("frappe.utils.install.complete_setup_wizard", commands)
		for required in (
			"frappe.desk.page.setup_wizard.setup_wizard.setup_complete",
			'"company_name":"Frappe LT Runtime"',
			'"company_abbr":"FLTR"',
			'"chart_of_accounts":"Standard"',
			'"fy_start_date"',
			'"fy_end_date"',
			"frappe.db.count",
			"frappe.db.get_single_value",
			"frappe.defaults.get_user_default",
		):
			self.assertIn(required, commands)
		self.assertIn("curl --fail", commands)
		self.assertIn("google-chrome --version", commands)
		for required in (
			"assert runtime_exit in (0, 1)",
			'assert report["status"] == ("pass" if runtime_exit == 0 else "fail")',
			'assert report["schema_version"] == 5',
			'assert summary["total"] == len(expected_ids)',
			'assert summary["blocked"] == 0',
			'assert summary["cleanup_failures"] == 0',
			'assert summary["functional_layout_defects"] == 0',
			"validate_machine_report(report, scenario_contract)",
			'blocker_types = {cause["type"] for cause in report["blocking_causes"]}',
			"assert bool(blocker_types) == (runtime_exit == 1)",
		):
			self.assertIn(required, commands)
		gate = next(
			step for step in steps if step["name"] == "Validate runtime report against actual findings"
		)
		gate_commands = [line.strip() for line in gate["run"].splitlines() if line.strip()]
		self.assertEqual(gate_commands[0], "runtime_exit=0")
		self.assertIn("|| runtime_exit=$?", gate_commands[1])
		self.assertNotIn('summary["total"] == 10', commands)
		self.assertNotIn('assert summary["english_fallbacks"] > 0', commands)
		self.assertIn("*.evidence.json", commands)
		self.assertIn(".*.evidence.json.*.tmp", commands)
		self.assertTrue(
			any(step.get("if") == "always()" and "Stop runtime web process" in step["name"] for step in steps)
		)

	def test_catalog_candidate_integration_cannot_be_vacuous(self):
		steps = self.workflow["jobs"]["verify"]["steps"]
		gate = next(
			step
			for step in steps
			if step["name"] == "Authenticate the production partition and run every registered candidate"
		)
		commands = gate["run"]
		for required in (
			"from frappe_lt.catalog_quality import registered_candidates, run",
			'expected_candidates = {"frappe", "erpnext-operations", "erpnext-finance-commerce"}',
			"assert set(registered_candidates()) == expected_candidates",
			"candidate_checks = 0",
			"candidate_checks += 1",
			"catalog_candidates/ci-integration.json",
			'assert first["errors"][0]["code"] == "MISSING_TRANSLATION_KEY"',
			'assert first["schema_version"] == 2',
			'assert "duration_seconds" not in first',
			"assert first_report.read_bytes() == second_report.read_bytes()",
			"assert not candidate_po.exists()",
			"assert candidate_checks == len(expected_candidates) + 1",
			'assert summary["translation_coverage"] == {"covered": 4907, "total": 4907}',
			"assert candidate_po.read_bytes() == repeat_po.read_bytes()",
			"assert report_path.read_bytes() == repeat_report.read_bytes()",
		):
			self.assertIn(required, commands)
		self.assertEqual(commands.count('"bench", "catalog-quality-gate", "--candidate", candidate'), 2)
		self.assertNotIn("compile_candidate=", commands)
		self.assertIn("active_mo", commands)
		self.assertIn("assert sentinel ==", commands)

	def test_runtime_browser_dependencies_are_fully_locked(self):
		package = json.loads(UI_RUNNER_PACKAGE_PATH.read_text(encoding="utf-8"))
		self.assertEqual(
			package["dependencies"],
			{
				"@4tw/cypress-drag-drop": "2.3.1",
				"@cypress/code-coverage": "3.14.7",
				"@testing-library/cypress": "10.1.3",
				"@testing-library/dom": "8.17.1",
				"cypress": "13.17.0",
				"cypress-real-events": "1.15.1",
				"cypress-split": "1.25.0",
			},
		)
		self.assertEqual(set(package["resolutions"].values()), {"8.70.0"})
		lock = UI_RUNNER_LOCK_PATH.read_text(encoding="utf-8")
		for name, version in package["dependencies"].items():
			self.assertIn(f'{name}@{version}":' if name.startswith("@") else f"{name}@{version}:", lock)

		commands = "\n".join(
			step.get("run", "") for step in self.workflow["jobs"]["runtime-browser"]["steps"]
		)
		for required in (
			"--frozen-lockfile",
			"--modules-folder",
			"@4tw/cypress-drag-drop",
			"@cypress/code-coverage",
			"@testing-library/cypress",
			"@testing-library/dom",
			"cypress-real-events",
			"cypress-split",
			"node_modules/.bin/cypress",
		):
			self.assertIn(required, commands)

	def test_actions_and_runtime_artifact_allowlist_are_exact(self):
		for job in self.workflow["jobs"].values():
			for step in job["steps"]:
				if action := step.get("uses"):
					self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")
		upload = next(
			step
			for step in self.workflow["jobs"]["runtime-browser"]["steps"]
			if step.get("name") == "Upload runtime evidence"
		)
		self.assertEqual(
			upload["uses"],
			"actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
		)
		self.assertEqual(upload["if"], "always()")
		paths = upload["with"]["path"].splitlines()
		self.assertEqual(
			paths,
			[
				"/tmp/frappe-lt-runtime-artifacts/candidate-snapshot.json",
				"/tmp/frappe-lt-runtime-artifacts/run/runtime-report.json",
				"/tmp/frappe-lt-runtime-artifacts/run/runtime-report.md",
				"/tmp/frappe-lt-runtime-artifacts/run/evidence/**/*.json",
				"/tmp/frappe-lt-runtime-artifacts/run/evidence/**/*.html",
				"/tmp/frappe-lt-runtime-artifacts/run/evidence/**/*.txt",
			],
		)
		self.assertFalse(
			any(re.search(r"browser-plan|journal|cookie|credential|\.log$", path) for path in paths)
		)
