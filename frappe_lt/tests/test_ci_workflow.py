import re
from pathlib import Path
from unittest import TestCase

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


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
		self.assertTrue(
			any(step.get("if") == "always()" and "Stop runtime web process" in step["name"] for step in steps)
		)

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
