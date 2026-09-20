import frappe
import frappe.translate
from frappe.tests import IntegrationTestCase


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
