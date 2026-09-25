import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe_lt
from frappe_lt.verify import (
	_assert_catalog_precedence,
	check_database_override,
	run,
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
		with (
			patch("frappe_lt.verify.verify_release") as release,
			patch("frappe_lt.verify.parse_po", return_value=parsed) as parser,
		):
			self.assertEqual(validate_po(catalog), {("Item", None): "Prekė"})

		parser.assert_called_once_with(catalog)
		release.assert_called_once_with(po_path=catalog)

	def test_native_catalog_contains_full_contextual_release(self):
		catalog = Path(__file__).parents[1] / "locale" / "lt.po"

		messages = validate_po(catalog)
		self.assertEqual(messages[("Item", None)], "Prekė")
		self.assertGreater(len(messages), 1)

	def test_run_checks_active_mo_without_compiling_or_replacing_it(self):
		with TemporaryDirectory() as directory:
			mo_path = Path(directory) / "frappe_lt.mo"
			mo_path.write_bytes(b"active MO")

			frappe_module = ModuleType("frappe")
			gettext_module = ModuleType("frappe.gettext")
			translate_module = ModuleType("frappe.gettext.translate")
			translate_module.get_mo_path = lambda _app, _locale: mo_path
			runtime_translate = ModuleType("frappe.translate")
			runtime_translate.clear_cache = lambda: None
			frappe_module.gettext = gettext_module
			frappe_module.translate = runtime_translate
			frappe_module.local = SimpleNamespace(site="test.site")
			frappe_module.get_app_path = lambda *_parts: Path(directory) / "lt.po"
			frappe_module.get_installed_apps = lambda: ["frappe", "erpnext", "frappe_lt"]
			frappe_module.db = SimpleNamespace(sql=lambda *_args, **_kwargs: [])
			frappe_module._ = lambda *_args, **_kwargs: "Prekė"
			gettext_module.translate = translate_module
			with (
				patch.dict(
					sys.modules,
					{
						"frappe": frappe_module,
						"frappe.gettext": gettext_module,
						"frappe.gettext.translate": translate_module,
						"frappe.translate": runtime_translate,
					},
				),
				patch(
					"frappe_lt.verify.verify_release",
					return_value={
						"mo_sha256": "a" * 64,
						"inventory_digest": "b" * 64,
						"release_digest": "c" * 64,
					},
				) as release,
				patch("frappe_lt.verify.verify_mo", return_value=("a" * 64, len(b"active MO"))) as mo,
				patch(
					"frappe_lt.install._checked",
					return_value={
						"release_digest": "c" * 64,
						"inventory_digest": "b" * 64,
						"mo_sha256": "a" * 64,
					},
				) as checked,
				patch("frappe_lt.install._migration_state", return_value="committed"),
				patch.object(
					frappe_lt,
					"profile",
					SimpleNamespace(status=lambda: {"state_after": "APPLIED"}),
					create=True,
				),
				patch(
					"frappe_lt.verify.parse_po",
					return_value=SimpleNamespace(messages={("Item", None): "Prekė"}),
				),
				patch("frappe_lt.verify._assert_catalog_precedence") as precedence,
			):
				result = run("test.site", mode="ci")

			self.assertEqual(result["release_digest"], "c" * 64)
			self.assertEqual(result["mo_bytes"], len(b"active MO"))
			checked.assert_called_once_with(frappe_module)
			self.assertEqual(mo_path.read_bytes(), b"active MO")
			release.assert_called_once()
			mo.assert_called_once_with(mo_path, with_size=True)
			precedence.assert_called_once_with(
				frappe_module, ["frappe", "erpnext", "frappe_lt"], {("Item", None): "Prekė"}
			)

	def test_final_verify_blocks_incomplete_migration_before_mo_or_profile(self):
		frappe = ModuleType("frappe")
		frappe.local = SimpleNamespace(site="test.site")
		frappe.get_installed_apps = lambda: ["frappe", "erpnext", "frappe_lt"]
		frappe.get_app_path = lambda *_args: "lt.po"
		with (
			patch.dict(sys.modules, {"frappe": frappe, "frappe.translate": ModuleType("frappe.translate")}),
			patch(
				"frappe_lt.verify.verify_release",
				return_value={
					"release_digest": "c" * 64,
					"inventory_digest": "b" * 64,
					"mo_sha256": "a" * 64,
				},
			),
			patch(
				"frappe_lt.install._checked",
				return_value={
					"release_digest": "c" * 64,
					"inventory_digest": "b" * 64,
					"mo_sha256": "a" * 64,
				},
			),
			patch("frappe_lt.install._migration_state", return_value="pending"),
			patch.object(
				frappe_lt, "profile", SimpleNamespace(status=lambda: {"state_after": "APPLIED"}), create=True
			),
			patch("frappe_lt.verify.verify_mo", side_effect=AssertionError("MO must not be checked")),
			self.assertRaisesRegex(ValueError, "legacy migration report or postcommit cache stage"),
		):
			run("test.site")

	def test_wrong_mo_diagnostic_is_authenticated_stderr_and_stdout_stays_machine_safe(self):
		frappe = ModuleType("frappe")
		frappe.local = SimpleNamespace(site="test.site")
		frappe.get_installed_apps = lambda: ["frappe", "erpnext", "frappe_lt"]
		frappe.get_app_path = lambda *_args: "lt.po"
		runtime_translate = ModuleType("frappe.translate")
		environment = {
			"babel": "2.16.0",
			"python": "3.14.4",
			"upstream": {
				"erpnext": {"commit": "b" * 40, "version": "16.35.0"},
				"frappe": {"commit": "a" * 40, "version": "16.34.0"},
			},
		}
		stdout, stderr = io.StringIO(), io.StringIO()
		with (
			patch.dict(sys.modules, {"frappe": frappe, "frappe.translate": runtime_translate}),
			patch(
				"frappe_lt.verify.verify_release",
				return_value={
					"inventory_digest": "b" * 64,
					"mo_sha256": "a" * 64,
					"release_digest": "c" * 64,
				},
			),
			patch(
				"frappe_lt.install._checked",
				return_value={
					"inventory_digest": "b" * 64,
					"mo_sha256": "a" * 64,
					"release_digest": "c" * 64,
				},
			),
			patch("frappe_lt.install._migration_state", return_value="committed"),
			patch.object(
				frappe_lt, "profile", SimpleNamespace(status=lambda: {"state_after": "APPLIED"}), create=True
			),
			patch("frappe_lt.inventory.verify_environment", return_value=environment),
			redirect_stdout(stdout),
			redirect_stderr(stderr),
			self.assertRaisesRegex(ValueError, "requested MO digest differs"),
		):
			run("test.site", mode="ci", expected_digest="0" * 64)
		self.assertEqual(stdout.getvalue(), "")
		for expected in ("Verified pinned environment:", "16.34.0", "16.35.0", "3.14.4", "2.16.0"):
			self.assertIn(expected, stderr.getvalue())

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

	def test_app_order_requires_frappe_lt_after_erpnext(self):
		self.assertEqual(
			validate_app_order(["frappe", "payments", "erpnext", "frappe_lt"], mode="local"),
			["frappe", "payments", "erpnext", "frappe_lt"],
		)
		with self.assertRaisesRegex(ValueError, "frappe, erpnext, frappe_lt"):
			validate_app_order(["frappe", "frappe_lt", "erpnext"], mode="local")
		with self.assertRaisesRegex(ValueError, "CI site"):
			validate_app_order(["frappe", "payments", "erpnext", "frappe_lt"], mode="ci")

	def test_catalog_precedence_includes_every_installed_app(self):
		translate = ModuleType("frappe.gettext.translate")
		runtime_translate = ModuleType("frappe.translate")
		translate.get_catalog = lambda *_args: []
		apps = ["frappe", "erpnext", "frappe_lt", "later_app"]

		def translations(_locale, *, apps):
			return (
				{"Item": "Wrong"}
				if "later_app" in apps
				else ({"Item": "Prekė"} if "frappe_lt" in apps else {})
			)

		runtime_translate.get_translations_from_apps = translations
		with (
			patch.dict(
				sys.modules,
				{
					"frappe.gettext": ModuleType("frappe.gettext"),
					"frappe.gettext.translate": translate,
					"frappe.translate": runtime_translate,
				},
			),
			self.assertRaisesRegex(ValueError, "app precedence failed"),
		):
			_assert_catalog_precedence(None, apps, {("Item", None): "Prekė"})

	def test_contextual_key_precedence_is_checked(self):
		translate = ModuleType("frappe.gettext.translate")
		translate.get_catalog = lambda *_args: []
		runtime_translate = ModuleType("frappe.translate")
		runtime_translate.get_translations_from_apps = (
			lambda _locale, *, apps: {
				"Item": "Prekė",
				"Save:Button": "Wrong",
			}
			if "frappe_lt" in apps
			else {}
		)
		with (
			patch.dict(
				sys.modules,
				{
					"frappe.gettext": ModuleType("frappe.gettext"),
					"frappe.gettext.translate": translate,
					"frappe.translate": runtime_translate,
				},
			),
			self.assertRaisesRegex(ValueError, "app precedence failed"),
		):
			_assert_catalog_precedence(
				None,
				["frappe", "erpnext", "frappe_lt"],
				{
					("Item", None): "Prekė",
					("Save", "Button"): "Išsaugoti",
				},
			)

	def test_database_override_blocks_ci_and_warns_locally(self):
		rows = [{"name": "LT-ITEM", "translated_text": "Vietinė prekė"}]

		with self.assertRaisesRegex(ValueError, "LT-ITEM"):
			check_database_override(rows, mode="ci")
		warning = check_database_override(rows, mode="local")
		self.assertIn("cannot prove effective runtime origin", warning)
		self.assertIsNone(check_database_override([], mode="local"))
