from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from frappe_lt.verify import assert_digest, check_database_override, validate_app_order, validate_po


class CatalogTest(TestCase):
	def test_native_catalog_has_only_item_translation(self):
		catalog = Path(__file__).parents[1] / "locale" / "lt.po"

		self.assertEqual(validate_po(catalog), {"Item": "Prekė"})

	def test_catalog_rejects_non_native_entry_shapes(self):
		catalog = Path(__file__).parents[1] / "locale" / "lt.po"
		valid = catalog.read_text(encoding="utf-8")
		invalid_catalogs = {
			"non-reproducible-date": valid.replace(
				"PO-Revision-Date: 2024-01-01 00:00+0000",
				"PO-Revision-Date: 2026-09-20 12:34+0000",
			),
			"fuzzy": valid.replace('msgid "Item"', '#, fuzzy\nmsgid "Item"'),
			"context": valid.replace('msgid "Item"', 'msgctxt "Stock"\nmsgid "Item"'),
			"plural": valid.replace(
				'msgid "Item"\nmsgstr "Prekė"',
				'msgid "Item"\nmsgid_plural "Items"\nmsgstr[0] "Prekė"',
			),
			"extra": valid + '\nmsgid "Order"\nmsgstr "Užsakymas"\n',
			"obsolete": valid + '\n#~ msgid "Order"\n#~ msgstr "Užsakymas"\n',
			"duplicate": valid + '\nmsgid "Item"\nmsgstr "Prekė"\n',
		}

		with TemporaryDirectory() as directory:
			path = Path(directory) / "lt.po"
			for constraint, content in invalid_catalogs.items():
				with self.subTest(constraint=constraint), self.assertRaises(ValueError):
					path.write_text(content, encoding="utf-8")
					validate_po(path)

	def test_digest_mismatch_reports_both_builds_and_environment(self):
		environment = {
			"frappe_commit": "frappe-sha",
			"erpnext_commit": "erpnext-sha",
			"frappe_version": "16.34.0",
			"erpnext_version": "16.35.0",
			"python": "3.14.0",
			"babel": "2.16.0",
		}

		with self.assertRaisesRegex(ValueError, "expected-digest") as raised:
			assert_digest("expected-digest", "first-digest", "second-digest", environment)

		diagnostic = str(raised.exception)
		for value in (*environment.values(), "first-digest", "second-digest"):
			self.assertIn(value, diagnostic)

	def test_app_order_requires_frappe_lt_after_erpnext(self):
		self.assertEqual(
			validate_app_order(["frappe", "payments", "erpnext", "frappe_lt"], mode="local"),
			["frappe", "payments", "erpnext", "frappe_lt"],
		)
		with self.assertRaisesRegex(ValueError, "frappe, erpnext, frappe_lt"):
			validate_app_order(["frappe", "frappe_lt", "erpnext"], mode="local")
		with self.assertRaisesRegex(ValueError, "CI site"):
			validate_app_order(["frappe", "payments", "erpnext", "frappe_lt"], mode="ci")

	def test_database_override_blocks_ci_and_warns_locally(self):
		rows = [{"name": "LT-ITEM", "translated_text": "Vietinė prekė"}]

		with self.assertRaisesRegex(ValueError, "LT-ITEM"):
			check_database_override(rows, mode="ci")
		warning = check_database_override(rows, mode="local")
		self.assertIn("cannot prove effective runtime origin", warning)
		self.assertIsNone(check_database_override([], mode="local"))
