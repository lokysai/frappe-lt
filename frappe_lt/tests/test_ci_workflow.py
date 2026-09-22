import json
import re
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

	def test_workflow_parses_and_static_gate_runs_every_runtime_helper_test(self):
		verify = self.workflow["jobs"]["verify"]
		commands = "\n".join(step.get("run", "") for step in verify["steps"])
		for required in (
			"frappe_lt.tests.test_runtime_validation",
			"frappe_lt.tests.test_ci_workflow",
			"node --test",
			"node --check",
		):
			self.assertIn(required, commands)

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
			"assert runtime_exit == 1",
			'assert report["status"] == "fail"',
			'assert report["schema_version"] == 5',
			'assert summary["total"] == len(expected_ids)',
			'assert summary["blocked"] == 0',
			'assert summary["cleanup_failures"] == 0',
			'assert summary["coverage_gaps"] > 0',
			'assert summary["english_fallbacks"] > 0',
			'assert summary["functional_layout_defects"] == 0',
			"validate_machine_report(report, scenario_contract)",
			'blocker_types = {cause["type"] for cause in report["blocking_causes"]}',
			'"runtime_coverage_gap"',
			'"scenario_fail"',
		):
			self.assertIn(required, commands)
		gate = next(
			step
			for step in steps
			if step["name"] == "Assert harness detects expected catalog and coverage blockers"
		)
		gate_commands = [line.strip() for line in gate["run"].splitlines() if line.strip()]
		self.assertEqual(gate_commands[0], "runtime_exit=0")
		self.assertIn("|| runtime_exit=$?", gate_commands[1])
		self.assertNotIn('summary["total"] == 10', commands)
		self.assertIn('assert blocker_types == {"runtime_coverage_gap", "scenario_fail"}', commands)
		self.assertIn("*.evidence.json", commands)
		self.assertIn(".*.evidence.json.*.tmp", commands)
		self.assertTrue(
			any(step.get("if") == "always()" and "Stop runtime web process" in step["name"] for step in steps)
		)

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
