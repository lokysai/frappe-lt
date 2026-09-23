"""Production Frappe segment regression checks against authenticated release data."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from frappe_lt.catalog_quality import _html_errors, _tokens, registered_candidates, run
from frappe_lt.inventory import canonical_json
from frappe_lt.review_evidence import evidence_summary, review_run_id, validate_review_evidence

ROOT = Path(__file__).parents[1]


def _read(name):
	return json.loads((ROOT / name).read_bytes())


def _key(record):
	return record["key"]["source"], record["key"]["context"]


class FrappeCatalogTest(TestCase):
	@classmethod
	def setUpClass(cls):
		cls.manifest = _read("catalog_segments/frappe.json")
		cls.candidate = _read("catalog_candidates/frappe.json")
		cls.entries = {_key(item): item for item in cls.candidate["entries"]}
		cls.inventory = {_key(item): item for item in _read("release_inventory.json")["entries"]}
		cls.provenance = {_key(item): item for item in _read("provenance.json")["entries"]}
		cls.exceptions = {_key(item): item for item in _read("translation_exceptions.json")["entries"]}

	def translation(self, source, context=None):
		return self.entries[source, context]["translation"]

	def test_frozen_frappe_first_keys_and_review_origins(self):
		self.assertIn("frappe", registered_candidates())
		manifest_bytes = (ROOT / "catalog_segments/frappe.json").read_bytes()
		self.assertEqual(self.candidate["manifest_sha256"], hashlib.sha256(manifest_bytes).hexdigest())
		self.assertEqual(self.candidate["inventory_digest"], self.manifest["inventory_digest"])
		self.assertEqual(len(self.entries), len(self.manifest["keys"]))
		self.assertEqual(len(self.entries), 6344)
		for item in self.manifest["keys"]:
			key = _key(item)
			with self.subTest(key=key):
				self.assertIn("frappe", self.inventory[key]["apps"])
				self.assertEqual(self.entries[key]["source_digest"], item["source_digest"])
				entry = self.entries[key]
				reason = validate_review_evidence(entry)
				self.assertEqual(entry["provenance"]["review"]["run_id"], review_run_id(entry))
				origin = self.provenance[key]
				if reason == "accepted_as_is":
					self.assertEqual(entry["translation"], origin["translation"])
					self.assertEqual(origin["origin"], "inherited_v15")
				elif reason == "new_translation":
					self.assertEqual(origin["status"], "missing")
				elif reason == "approved_translation_exception":
					self.assertEqual(origin["status"], "excepted")
					self.assertEqual(origin["origin"], "approved_exception")
					self.assertEqual(entry["translation"], key[0])
					self.assertEqual(self.exceptions[key]["source_digest"], item["source_digest"])
				else:
					self.assertEqual(origin["origin"], "inherited_v15")
					self.assertNotEqual(entry["translation"], origin["translation"])
		self.assertEqual(set(self.entries), {_key(item) for item in self.manifest["keys"]})
		self.assertEqual(
			evidence_summary(self.candidate["entries"], self.entries)["translation_coverage"],
			{"covered": 6344, "total": 6344},
		)

	def test_critical_meanings_and_actions(self):
		expected = {
			"Account": "Sąskaita",
			"User": "Naudotojas",
			"Draft": "Juodraštis",
			"Submit": "Patvirtinti",
			"Submitted": "Patvirtintas",
			"Cancelled": "Atšauktas",
			"Go to {0} List": "Atidaryti {0} sąrašą",
			"Visit Web Page": "Atidaryti tinklalapį",
			"OTP Secret has been reset. Re-registration will be required on next login.": (
				"OTP slaptasis raktas nustatytas iš naujo. "
				"Kitą kartą prisijungus reikės iš naujo užregistruoti OTP priemonę."
			),
			"Use % for any non empty value.": "Jei turi tikti bet kuri netuščia reikšmė, įrašykite %.",
			"The email button is enabled for the user in the document.": (
				"Naudotojas dokumente gali naudoti el. laiško siuntimo mygtuką."
			),
			"The print button is enabled for the user in the document.": (
				"Naudotojas dokumente gali naudoti spausdinimo mygtuką."
			),
			"If the user has access to Employee and Report is enabled, they can view Employee-based reports.": (
				"Jei naudotojas turi prieigą prie darbuotojų duomenų ir yra įjungta ataskaitų teisė, "
				"jis gali peržiūrėti su darbuotojais susijusias ataskaitas."
			),
		}
		for source, translation in expected.items():
			with self.subTest(source=source):
				self.assertEqual(self.translation(source), translation)
		self.assertEqual(self.translation("Delete Tab", "Title of confirmation dialog"), "Ištrinti kortelę")
		self.assertEqual(self.translation("Submit", "Button in list view actions menu"), "Patvirtinti")
		self.assertIn(
			"Ar tikrai norite",
			self.translation(
				"Are you sure you want to delete the column? All the fields in the column will be moved to the previous column.",
				"Confirmation dialog message",
			),
		)

	def test_portal_print_and_email_preserve_rendered_content(self):
		portal = "Visit Web Page"
		print_source = (
			"Error connecting to QZ Tray Application...<br><br> You need to have QZ Tray application installed and running, "
			'to use the Raw Print feature.<br><br><a target="_blank" href="https://qz.io/download/">'
			'Click here to Download and install QZ Tray</a>.<br> <a target="_blank" '
			'href="https://erpnext.com/docs/user/manual/en/setting-up/print/raw-printing">'
			"Click here to learn more about Raw Printing</a>."
		)
		email = "We have received a request from you to download your {0} data associated with: {1}"
		linked_email = (
			"The report you requested has been generated.<br><br>Click here to download:<br>"
			"<a href='{0}'>{0}</a><br><br>This link will expire in {1} hours."
		)
		for source, location in (
			(portal, "frappe/website/"),
			(print_source, "frappe/public/js/frappe/form/print_utils.js"),
			(email, "frappe/templates/emails/download_data.html"),
			(linked_email, "frappe/desk/utils.py"),
		):
			with self.subTest(source=source):
				entry = self.entries[source, None]
				self.assertTrue(
					any(location in loc["path"] for loc in self.inventory[source, None]["source_locations"])
				)
				self.assertEqual(_tokens(source), _tokens(entry["translation"]))
				self.assertEqual(_html_errors(source, entry["translation"]), [])
		self.assertIn("https://qz.io/download/", self.translation(print_source))
		self.assertIn("href='{0}'", self.translation(linked_email))

	def test_gate_is_byte_deterministic_and_never_publishes_active_locale(self):
		active_po = ROOT / "locale/lt.po"
		before = hashlib.sha256(active_po.read_bytes()).hexdigest()

		def compile_stub(_po_path, workspace):
			path = workspace / "sites/assets/locale/lt/LC_MESSAGES/frappe_lt.mo"
			path.parent.mkdir(parents=True)
			path.write_bytes(b"compiled")

		with TemporaryDirectory() as directory:
			root = Path(directory)
			results = []
			for number in range(2):
				po, report = root / f"frappe-{number}.po", root / f"frappe-{number}.json"
				result = run("frappe", po, report, compile_candidate=compile_stub)
				self.assertEqual(result["exit_code"], 0, result["errors"][:5])
				self.assertEqual(result["summary"]["translation_coverage"], {"covered": 6344, "total": 6344})
				results.append((po.read_bytes(), report.read_bytes()))
			self.assertEqual(*results)
			self.assertEqual(results[0][1], canonical_json(result))
		self.assertEqual(hashlib.sha256(active_po.read_bytes()).hexdigest(), before)
