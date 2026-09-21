import frappe
import frappe.translate
from frappe.tests import IntegrationTestCase

from frappe_lt.runtime_control import SiteControl, _write_durable, resolve_translation


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
				"fixtures": {},
				"run_id": run_id,
				"schema_version": 1,
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
		with control.lease():
			self.assertEqual(control.recover_stale(), [])
			control.start()
			try:
				control.before_document_mutation("User", user)
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
			finally:
				cleanup = control.cleanup()
			self.assertEqual(cleanup, [])

		self.assertFalse(frappe.db.exists("User", user))
		self.assertFalse(
			frappe.db.exists("Deleted Document", {"deleted_doctype": "User", "deleted_name": user})
		)
		self.assertFalse(frappe.db.exists("Sessions", {"user": user}))
		self.assertFalse(control.journal_path.exists())

	def test_mutable_site_lease_rejects_a_second_run(self):
		first = SiteControl(frappe, frappe.local.site, "e" * 32)
		second = SiteControl(frappe, frappe.local.site, "f" * 32)
		with first.lease(), self.assertRaisesRegex(RuntimeError, "holds the lease"):
			with second.lease():
				self.fail("second mutable runtime run acquired the lease")
