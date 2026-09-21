import json
from unittest.mock import patch

import frappe
import frappe.translate
from frappe.tests import IntegrationTestCase

from frappe_lt.runtime_contracts import load_contracts
from frappe_lt.runtime_control import (
	SiteControl,
	_load_journal,
	_runtime_default_value_name,
	_write_durable,
	capture_portal,
	capture_print,
	capture_welcome_email,
	resolve_translation,
)


class DatabaseMaskingTest(IntegrationTestCase):
	def test_contextless_database_translation_masks_app_catalog(self):
		existing = frappe.get_all(
			"Translation",
			filters={"language": "lt", "source_text": "Item", "context": ("in", (None, ""))},
			pluck="name",
		)
		for name in existing:
			frappe.delete_doc("Translation", name, ignore_permissions=True)
		frappe.translate.clear_cache()
		self.assertEqual(frappe._("Item", lang="lt"), "Prekė")

		override = frappe.get_doc(
			{
				"doctype": "Translation",
				"language": "lt",
				"source_text": "Item",
				"translated_text": "Duomenų bazės prekė",
			}
		).insert(ignore_permissions=True)
		try:
			frappe.translate.clear_cache()
			self.assertEqual(frappe._("Item", lang="lt"), "Duomenų bazės prekė")
		finally:
			override.delete(ignore_permissions=True)
			frappe.translate.clear_cache()

		self.assertEqual(frappe._("Item", lang="lt"), "Prekė")


class RuntimeSiteControlTest(IntegrationTestCase):
	def _assert_clean_control(self, control):
		frappe.set_user("Administrator")
		cleanup = control.cleanup()
		residue = control.residue_scan()
		self.assertEqual({"cleanup": cleanup, "residue": residue}, {"cleanup": [], "residue": []})

	def test_prepare_owns_exact_portal_and_draft_print_fixtures_without_ledger_entries(self):
		run_id = "b" * 32
		control = SiteControl(frappe, frappe.local.site, run_id)
		self.addCleanup(self._assert_clean_control, control)
		contracts = load_contracts()
		ledger_count = frappe.db.count("GL Entry")
		tree_before = {
			doctype: frappe.get_all(
				doctype,
				fields=["name", parent_field, "old_parent", "lft", "rgt"],
				order_by="name asc",
			)
			for doctype, parent_field in (
				("Customer Group", "parent_customer_group"),
				("Item Group", "parent_item_group"),
				("Territory", "parent_territory"),
			)
		}
		uom_before = frappe.db.get_value("UOM", "Nos", ["enabled", "must_be_whole_number"], as_dict=True)
		with control.lease():
			self.assertEqual(control.recover_stale(), [])
			control.start()
			try:
				prepared = control.prepare(
					contracts["profiles"], contracts["scenarios"], diagnostic_sampling=True
				)
				plan = json.loads(control.secret_path.read_bytes())
				self.assertEqual(plan["schema_version"], 2)
				self.assertTrue(plan["diagnostic_sampling"])
				portal = plan["fixtures"]["portal-contact"]
				printable = plan["fixtures"]["todo-draft"]
				for value in (
					portal["contact"],
					portal["customer"],
					portal["customer_group"],
					portal["territory"],
					printable["name"],
				):
					self.assertTrue(value.startswith(control.marker))
				self.assertEqual(portal["user"], plan["credentials"]["portal-customer"]["user"])
				self.assertEqual(
					frappe.db.get_value("Customer Group", portal["customer_group"], "parent_customer_group"),
					"All Customer Groups",
				)
				self.assertEqual(
					frappe.db.get_value("Territory", portal["territory"], "parent_territory"),
					"All Territories",
				)
				contact = frappe.get_doc("Contact", portal["contact"])
				self.assertEqual(contact.user, portal["user"])
				self.assertEqual(
					[(row.email_id, row.is_primary) for row in contact.email_ids],
					[(portal["user"], 1)],
				)
				self.assertEqual(
					[(row.link_doctype, row.link_name) for row in contact.links],
					[("Customer", portal["customer"])],
				)
				printable_doc = frappe.get_doc("ToDo", printable["name"])
				self.assertEqual(printable_doc.docstatus, 0)
				self.assertEqual(printable_doc.description, control.marker)
				self.assertEqual(frappe.db.count("GL Entry"), ledger_count)
				self.assertEqual(
					prepared["fixtures"],
					["item-draft", "portal-contact", "runtime-user", "todo-draft"],
				)
				expected_defaults = {}
				for profile in contracts["profiles"]["profiles"]:
					if profile["administrator"]:
						continue
					user = f"{control.marker}-{profile['id']}@invalid.example"
					profile_defaults = {**profile["defaults"], "time_zone": profile["time_zone"]}
					for key, value in sorted(profile_defaults.items()):
						name = _runtime_default_value_name(control.marker, user, key)
						expected_defaults[name] = {
							"defkey": key,
							"defvalue": value,
							"parent": user,
							"parentfield": "defaults",
							"parenttype": "User",
						}
				actual_defaults = {
					row.pop("name"): row
					for row in frappe.get_all(
						"DefaultValue",
						filters={"parent": ("like", f"{control.marker}%")},
						fields=["name", "parent", "parentfield", "parenttype", "defkey", "defvalue"],
					)
				}
				self.assertEqual(actual_defaults, expected_defaults)
				journal = _load_journal(control.journal_path)
				targets = [mutation["target"] for mutation in journal["mutations"]]
				for doctype, name in (
					("Customer Group", portal["customer_group"]),
					("Territory", portal["territory"]),
				):
					self.assertIn({"doctype": doctype, "name": name}, targets)
					self.assertLess(
						targets.index({"doctype": doctype, "name": name}),
						targets.index({"doctype": "Customer", "name": portal["customer"]}),
					)
				for doctype, root_name in (
					("Customer Group", "All Customer Groups"),
					("Item Group", "All Item Groups"),
					("Territory", "All Territories"),
				):
					if not any(row["name"] == root_name for row in tree_before[doctype]):
						self.assertIn({"doctype": doctype, "name": root_name}, targets)
				self.assertLess(
					targets.index({"doctype": "Customer", "name": portal["customer"]}),
					targets.index({"doctype": "Contact", "name": portal["contact"]}),
				)
				for name, default in expected_defaults.items():
					self.assertIn({"doctype": "DefaultValue", "name": name}, targets)
					self.assertLess(
						targets.index({"doctype": "User", "name": default["parent"]}),
						targets.index({"doctype": "DefaultValue", "name": name}),
					)
			finally:
				cleanup = control.cleanup()
			self.assertEqual(cleanup, [])

		self.assertEqual(frappe.db.count("GL Entry"), ledger_count)
		self.assertFalse(frappe.db.exists("DefaultValue", {"parent": ("like", f"{control.marker}%")}))
		self.assertEqual(
			{
				doctype: frappe.get_all(
					doctype,
					fields=["name", parent_field, "old_parent", "lft", "rgt"],
					order_by="name asc",
				)
				for doctype, parent_field in (
					("Customer Group", "parent_customer_group"),
					("Item Group", "parent_item_group"),
					("Territory", "parent_territory"),
				)
			},
			tree_before,
		)
		self.assertEqual(
			frappe.db.get_value("UOM", "Nos", ["enabled", "must_be_whole_number"], as_dict=True),
			uom_before,
		)
		self.assertEqual(control.residue_scan(), [])

	def test_stale_recovery_removes_exact_journaled_role_profile_defaults(self):
		run_id = "9" * 32
		control = SiteControl(frappe, frappe.local.site, run_id)
		self.addCleanup(self._assert_clean_control, control)
		contracts = load_contracts()
		profile = next(
			profile for profile in contracts["profiles"]["profiles"] if profile["id"] == "accounts-user"
		)
		with control.lease():
			self.assertEqual(control.recover_stale(), [])
			control.start()
			control.prepare({"profiles": [profile]}, {"scenarios": []})
			user = f"{control.marker}-{profile['id']}@invalid.example"
			expected_names = [
				_runtime_default_value_name(control.marker, user, key)
				for key in sorted({*profile["defaults"], "time_zone"})
			]
			self.assertEqual(
				frappe.get_all(
					"DefaultValue",
					filters={"parent": user},
					pluck="name",
					order_by="name asc",
				),
				sorted(expected_names),
			)
			journal_targets = [
				mutation["target"] for mutation in _load_journal(control.journal_path)["mutations"]
			]
			self.assertTrue(
				all({"doctype": "DefaultValue", "name": name} in journal_targets for name in expected_names)
			)

		recovery = SiteControl(frappe, frappe.local.site, "8" * 32)
		self.addCleanup(self._assert_clean_control, recovery)
		with recovery.lease():
			self.assertEqual(recovery.recover_stale(), [])
		self.assertFalse(frappe.db.exists("User", user))
		self.assertFalse(frappe.db.exists("DefaultValue", {"name": ("in", expected_names)}))
		self.assertFalse(control.journal_path.exists())
		self.assertEqual(recovery.residue_scan(marker=control.marker), [])

	def test_standard_outputs_capture_exact_response_without_queue_or_access_log(self):
		run_id = "a" * 32
		control = SiteControl(frappe, frappe.local.site, run_id)
		contracts = load_contracts()
		with control.lease():
			self.assertEqual(control.recover_stale(), [])
			control.start()
			try:
				control.prepare(contracts["profiles"], contracts["scenarios"])
				plan = json.loads(control.secret_path.read_bytes())
				token = plan["token"]
				portal_fixture = plan["fixtures"]["portal-contact"]
				frappe.set_user(portal_fixture["user"])
				with patch("frappe.utils.get_assets_json", return_value={}):
					portal = capture_portal(run_id, token, "portal-account-desktop", "/me")
				self.assertEqual(portal["status"], 200)
				self.assertIn(control.marker, portal["html"])

				frappe.set_user("Administrator")
				print_fixture = plan["fixtures"]["todo-draft"]
				printed = capture_print(
					run_id,
					token,
					"todo-standard-print",
					print_fixture["doctype"],
					print_fixture["name"],
				)
				self.assertEqual(printed["status"], 200)
				self.assertEqual(printed["suppressed_access_logs"], 1)
				self.assertIn(control.marker, printed["html"])
				self.assertFalse(
					frappe.db.exists("Access Log", {"reference_document": print_fixture["name"]})
				)

				email = capture_welcome_email(
					run_id,
					token,
					"standard-welcome-email",
					plan["fixtures"]["runtime-user"]["user"],
				)
				self.assertEqual(email["suppressed"], {"email_queue": 0, "enqueue": 0, "outbound": 1})
				self.assertIn("MIME-Version", email["output"])
				self.assertIn("key=[REDACTED]", email["visible_output"])
				self.assertNotIn("/update-password?key=" + control.marker, email["output"])
				self.assertFalse(
					frappe.get_all(
						"Email Queue Recipient",
						filters={"recipient": plan["fixtures"]["runtime-user"]["user"]},
						limit=1,
					)
				)
			finally:
				frappe.set_user("Administrator")
				cleanup = control.cleanup()
			self.assertEqual(cleanup, [])

		self.assertEqual(control.residue_scan(), [])

	def test_effective_lookup_preserves_context_interpolation_and_database_origin(self):
		run_id = "c" * 32
		token = "test-capability-token"
		control = SiteControl(frappe, frappe.local.site, run_id)
		self.assertEqual(control.recover_stale(), [])
		control.start()
		_write_durable(
			control.secret_path,
			{
				"credentials": {"administrator": {"user": "Administrator"}},
				"diagnostic_sampling": False,
				"fixtures": {},
				"run_id": run_id,
				"schema_version": 2,
				"scenarios": [
					{
						"id": "integration-lookup",
						"kind": "desk",
						"role_profile_id": "administrator",
					}
				],
				"token": token,
			},
		)
		documents = []
		try:
			for context, translated in (("Greeting", "Sveiki, {0}"), ("Heading", "Pasisveikinimas {0}")):
				documents.append(
					frappe.get_doc(
						{
							"context": context,
							"doctype": "Translation",
							"language": "lt",
							"source_text": "Runtime Hello {0}",
							"translated_text": translated,
						}
					).insert(ignore_permissions=True)
				)
			frappe.translate.clear_cache()
			greeting = resolve_translation(
				run_id, token, "integration-lookup", " Runtime Hello {0} ", "Greeting"
			)
			heading = resolve_translation(run_id, token, "integration-lookup", "Runtime Hello {0}", "Heading")
			self.assertEqual(greeting["source"], "database")
			self.assertEqual(greeting["raw_source"], " Runtime Hello {0} ")
			self.assertEqual(greeting["key"]["source"], "Runtime Hello {0}")
			self.assertEqual(greeting["effective"].format("Jonai"), "Sveiki, Jonai")
			self.assertEqual(heading["effective"].format("Jonai"), "Pasisveikinimas Jonai")
			self.assertNotEqual(greeting["effective"], heading["effective"])
		finally:
			for document in reversed(documents):
				document.delete(ignore_permissions=True)
			frappe.translate.clear_cache()
			self.assertEqual(control.cleanup(), [])

	def test_journaled_identity_is_removed_by_unconditional_cleanup(self):
		run_id = "d" * 32
		user = f"frappe-lt-runtime-{run_id}@invalid.example"
		control = SiteControl(frappe, frappe.local.site, run_id)
		self.addCleanup(self._assert_clean_control, control)
		with control.lease():
			self.assertEqual(control.recover_stale(), [])
			control.start()
			try:
				control.before_document_mutation("User", user)
				with control.journal_default_writes(user) as default_writes:
					frappe.get_doc(
						{
							"doctype": "User",
							"email": user,
							"first_name": "Runtime Cleanup",
							"send_welcome_email": 0,
						}
					).insert(ignore_permissions=True)
				frappe.db.commit()
				self.assertTrue(frappe.db.exists("User", user))
				self.assertEqual(len(default_writes["add"]), 5)
				self.assertEqual(default_writes["clear"], [])
				self.assertEqual(
					len(frappe.get_all("DefaultValue", filters={"parent": user}, pluck="name")), 5
				)
			finally:
				cleanup = control.cleanup()
			self.assertEqual(cleanup, [])

		self.assertFalse(frappe.db.exists("User", user))
		self.assertFalse(
			frappe.db.exists("Deleted Document", {"deleted_doctype": "User", "deleted_name": user})
		)
		self.assertFalse(frappe.db.sql("select 1 from tabSessions where user=%s limit 1", (user,)))
		self.assertFalse(control.journal_path.exists())

	def test_mutable_site_lease_rejects_a_second_run(self):
		first = SiteControl(frappe, frappe.local.site, "e" * 32)
		second = SiteControl(frappe, frappe.local.site, "f" * 32)
		with first.lease(), self.assertRaisesRegex(RuntimeError, "holds the lease"):
			with second.lease():
				self.fail("second mutable runtime run acquired the lease")
