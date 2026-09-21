import hashlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from frappe_lt.po import compile_po
from frappe_lt.verify import (
	_compile_twice,
	assert_digest,
	check_database_override,
	validate_app_order,
	validate_po,
)


class CatalogTest(TestCase):
	def test_runtime_verifier_uses_shared_po_parse_boundary(self):
		catalog = Path(__file__).parents[1] / "locale" / "lt.po"
		parsed = SimpleNamespace(
			locale="lt",
			creation_date="2024-01-01 00:00+0000",
			revision_date="2024-01-01 00:00+0000",
			messages={("Item", None): "Prekė"},
		)
		with patch("frappe_lt.verify.parse_po", return_value=parsed) as parser:
			self.assertEqual(validate_po(catalog), {"Item": "Prekė"})

		parser.assert_called_once_with(catalog)

	def test_native_catalog_has_only_item_translation(self):
		catalog = Path(__file__).parents[1] / "locale" / "lt.po"

		self.assertEqual(validate_po(catalog), {"Item": "Prekė"})

	def test_runtime_compilation_uses_shared_adapter_twice_for_active_catalog(self):
		with TemporaryDirectory() as directory:
			bench_path = Path(directory)
			po_path = bench_path / "apps" / "frappe_lt" / "frappe_lt" / "locale" / "lt.po"
			mo_path = bench_path / "sites" / "assets" / "locale" / "lt" / "frappe_lt.mo"
			po_path.parent.mkdir(parents=True)
			po_path.write_bytes(b"catalog")
			mo_path.parent.mkdir(parents=True)
			mo_path.write_bytes(b"stale")

			frappe_module = ModuleType("frappe")
			gettext_module = ModuleType("frappe.gettext")
			translate_module = ModuleType("frappe.gettext.translate")
			utils_module = ModuleType("frappe.utils")
			translate_module.get_mo_path = lambda _app, _locale: mo_path
			utils_module.get_bench_path = lambda: bench_path
			frappe_module.gettext = gettext_module
			frappe_module.utils = utils_module
			gettext_module.translate = translate_module
			frappe = SimpleNamespace(get_app_path=lambda *_parts: po_path)
			output_existed_before_compile = []

			def compile_active(*_arguments, **_kwargs):
				output_existed_before_compile.append(mo_path.exists())
				mo_path.write_bytes(b"compiled")

			digest = hashlib.sha256(b"compiled").hexdigest()
			with (
				patch.dict(
					sys.modules,
					{
						"frappe": frappe_module,
						"frappe.gettext": gettext_module,
						"frappe.gettext.translate": translate_module,
						"frappe.utils": utils_module,
					},
				),
				patch("frappe_lt.verify._run_bench", side_effect=compile_active),
				patch("frappe_lt.verify.compile_po", wraps=compile_po) as adapter,
			):
				result = _compile_twice(frappe, {"python": "3.14.0"}, digest)

			self.assertEqual(result, (digest, digest, mo_path))
			self.assertEqual(adapter.call_count, 2)
			self.assertEqual(output_existed_before_compile, [False, False])

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
