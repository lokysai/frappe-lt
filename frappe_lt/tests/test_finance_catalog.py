"""Frozen finance/commerce Release Inventory and production candidate regressions."""

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from frappe_lt.catalog_partition import _automatic_segment
from frappe_lt.catalog_quality import _html_errors, _tokens, registered_candidates, run
from frappe_lt.inventory import canonical_json, verify_owned_artifacts
from frappe_lt.review_evidence import (
	CORRECTION_REASONS,
	evidence_summary,
	review_run_id,
	validate_review_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
SEGMENT = "erpnext-finance-commerce"
MANIFEST = f"catalog_segments/{SEGMENT}.json"
CANDIDATE = f"catalog_candidates/{SEGMENT}.json"
MANIFEST_SHA256 = "12aa36ed3df75d884b229f755d44f5fcafbe7db47f1e375f82107830b9e19127"
ORIGINALS_SHA256 = "dbb132ad648c072fd30feacd3a6b7b2c17b32d329d4de9c16f7764d1347ecc21"


def _read(name):
	return json.loads((ROOT / name).read_bytes())


def _key(record):
	return record["key"]["source"], record["key"]["context"]


class FinanceManifestTest(TestCase):
	def test_frozen_manifest_and_originals_are_authenticated(self):
		compatibility = verify_owned_artifacts(ROOT / "compatibility.json")
		manifest_bytes = (ROOT / MANIFEST).read_bytes()
		manifest = json.loads(manifest_bytes)
		inventory = {_key(entry): entry for entry in _read("release_inventory.json")["entries"]}
		keys = [_key(entry) for entry in manifest["keys"]]
		self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(), MANIFEST_SHA256)
		self.assertEqual(manifest["inventory_digest"], compatibility["inventory_digest"])
		self.assertEqual(len(keys), len(set(keys)))
		self.assertEqual(len(keys), 4907)
		self.assertNotIn(("Account", None), keys)  # Frappe-first contextless collision.
		self.assertEqual(
			{
				(entry["key"]["source"], entry["key"]["context"]): entry["source_digest"]
				for entry in manifest["keys"]
			},
			{key: inventory[key]["source_digest"] for key in keys},
		)
		overrides = {
			_key(record): record
			for record in _read("catalog_segment_ownership_overrides.json")["entries"]
			if record["segment_id"] == SEGMENT
		}
		for key in keys:
			entry = inventory[key]
			self.assertEqual(entry["apps"], ["erpnext"])
			if _automatic_segment(entry) != SEGMENT:
				self.assertIsNone(_automatic_segment(entry))
				self.assertTrue(overrides[key]["reviewed"])
				self.assertEqual(overrides[key]["source_digest"], entry["source_digest"])
		provenance = _read("provenance.json")["entries"]
		selected = set(keys)
		originals = [
			{"key": record["key"], "translation": record.get("v15_original", record.get("translation"))}
			for record in provenance
			if _key(record) in selected
			and (record.get("origin") == "inherited_v15" or "v15_original" in record)
		]
		self.assertEqual(len(originals), 2592)
		self.assertEqual(hashlib.sha256(canonical_json(originals)).hexdigest(), ORIGINALS_SHA256)
		self.assertEqual(
			sum(
				_key(record) in selected and record.get("origin") == "inherited_v15" for record in provenance
			),
			2590,
		)


class FinanceCatalogTest(TestCase):
	@classmethod
	def setUpClass(cls):
		cls.compatibility = verify_owned_artifacts(ROOT / "compatibility.json")
		cls.manifest = _read(MANIFEST)
		cls.inventory = {_key(entry): entry for entry in _read("release_inventory.json")["entries"]}
		cls.provenance = {_key(entry): entry for entry in _read("provenance.json")["entries"]}
		cls.exceptions = {_key(entry): entry for entry in _read("translation_exceptions.json")["entries"]}
		cls.registry = _read("catalog_segments.json")
		cls.candidate = _read(CANDIDATE)
		cls.entries = {_key(entry): entry for entry in cls.candidate["entries"]}

	def translation(self, source, context=None):
		return self.entries[source, context]["translation"]

	def test_exact_registered_membership_source_digest_and_review_decisions(self):
		self.assertEqual(set(registered_candidates()), {"frappe", "erpnext-operations", SEGMENT})
		registration = next(record for record in self.registry["candidates"] if record["name"] == SEGMENT)
		self.assertEqual(registration["candidate"], CANDIDATE)
		self.assertEqual(registration["manifest"], MANIFEST)
		self.assertEqual(registration["manifest_sha256"], MANIFEST_SHA256)
		self.assertEqual(
			registration["candidate_sha256"], hashlib.sha256((ROOT / CANDIDATE).read_bytes()).hexdigest()
		)
		self.assertEqual(self.candidate["manifest_sha256"], MANIFEST_SHA256)
		self.assertEqual(self.candidate["inventory_digest"], self.compatibility["inventory_digest"])
		self.assertEqual(self.candidate["segment_id"], SEGMENT)
		self.assertEqual(self.candidate["review_evidence_schema_version"], 1)
		self.assertEqual(len(self.candidate["entries"]), len(self.entries))
		self.assertEqual(set(self.entries), {_key(item) for item in self.manifest["keys"]})
		self.assertEqual(len(self.entries), 4907)
		for selected in self.manifest["keys"]:
			key = _key(selected)
			with self.subTest(key=key):
				entry = self.entries[key]
				self.assertEqual(entry["source_digest"], selected["source_digest"])
				self.assertEqual(entry["source_digest"], self.inventory[key]["source_digest"])
				reason = validate_review_evidence(entry)
				self.assertEqual(entry["provenance"]["review"]["run_id"], review_run_id(entry))
				self.assertEqual(entry["provenance"]["review"]["agent"], "opencode")
				self.assertEqual(entry["provenance"]["review"]["model"], "openai/gpt-6-sol")
				self.assertTrue(entry["translation"])
				origin = self.provenance[key]
				if reason == "accepted_as_is":
					self.assertEqual(origin["status"], "translated")
					self.assertEqual(origin["origin"], "inherited_v15")
					self.assertEqual(entry["translation"], origin["translation"])
				elif reason in CORRECTION_REASONS:
					self.assertEqual(origin["status"], "translated")
					if origin["origin"] == "inherited_v15":
						self.assertNotEqual(entry["translation"], origin["translation"])
					else:
						self.assertEqual(origin["origin"], "corrected_inherited")
						self.assertEqual(entry["translation"], origin["translation"])
					if origin["origin"] == "corrected_inherited" and "v15_original" in origin:
						self.assertIsInstance(origin["v15_original"], str)
						self.assertTrue(origin["v15_original"].strip())
						self.assertNotEqual(origin["v15_original"], entry["translation"])
				elif reason == "new_translation":
					self.assertTrue(
						origin["status"] == "missing"
						or (
							origin["status"] == "translated"
							and origin["origin"] == "new_ai"
							and entry["translation"] == origin["translation"]
						)
					)
				else:
					self.assertEqual(reason, "approved_translation_exception")
					self.assertEqual(origin["status"], "excepted")
					self.assertEqual(origin["origin"], "approved_exception")
					self.assertEqual(entry["translation"], key[0])
					self.assertEqual(self.exceptions[key]["source_digest"], selected["source_digest"])
					self.assertTrue(self.exceptions[key]["reviewed"])
					self.assertTrue(self.exceptions[key]["review"])

		for key, digest, original in (
			(("IBAN", None), "cc0b8c010029d86b7c834d7d31af70dc20c98e5bc8a87f2ff767cc4b52dfb829", "IBAN"),
			(
				("[{0}] {1}", "Financial Report Template"),
				"92d53b9a2f162106f7b8911f2705b3233e092e987bf07616134ddc2111742a6a",
				None,
			),
			(("<li>{}</li>", None), "6c0ed09ffb9a28fd48a5c43252830bd20ea69081910ef1c46ce5d20a2910c78a", None),
			(("BOM", None), "f5c12d2e2a1e4011937930f8a628cbef55db42b525bcf8be51bbb08b43687575", "BOM"),
		):
			with self.subTest(key=key):
				self.assertEqual(self.inventory[key]["source_digest"], digest)
				self.assertEqual(self.entries[key]["source_digest"], digest)
				self.assertEqual(self.entries[key]["translation"], key[0])
				self.assertEqual(
					self.entries[key]["provenance"]["review"]["reason"], "approved_translation_exception"
				)
				self.assertEqual(self.provenance[key]["origin"], "approved_exception")
				self.assertEqual(self.provenance[key]["status"], "excepted")
				self.assertEqual(self.provenance[key].get("v15_original"), original)
				self.assertEqual(self.exceptions[key]["source_digest"], digest)
				self.assertTrue(self.exceptions[key]["reviewed"])

	def test_finance_meanings_are_checked_on_real_keys(self):
		for source, expected in {
			"Asset": "Turtas",
			"Rate": "Vieneto kaina",
			"Exchange Rate": "Valiutos kursas",
			"Valuation Rate": "Apskaitinė vieneto vertė",
			"General Ledger": "Didžioji knyga",
			"Journal Entry": "Bendrojo žurnalo įrašas",
			"Cost Center": "Sąnaudų centras",
			"Invoice": "Sąskaita faktūra",
			"Sales Invoice": "Pardavimo sąskaita faktūra",
			"Purchase Invoice": "Pirkimo sąskaita faktūra",
			"Customer": "Klientas",
			"Supplier": "Tiekėjas",
			"Lead": "Potencialus klientas",
			"Opportunity": "Pardavimo galimybė",
			"Quotation": "Komercinis pasiūlymas",
			"Sales Order": "Pardavimo užsakymas",
			"Purchase Order": "Pirkimo užsakymas",
			"Debit": "Debetas",
			"Credit": "Kreditas",
		}.items():
			with self.subTest(source=source):
				self.assertEqual(self.translation(source), expected)
				self.assertTrue(
					any(loc["app"] == "erpnext" for loc in self.inventory[source, None]["source_locations"])
				)
		self.assertIn("{0}", self.translation("Debit ({0})"))
		self.assertIn("{0}", self.translation("Credit ({0})"))
		self.assertNotEqual(self.translation("Debit ({0})"), self.translation("Credit ({0})"))
		self.assertIn("Debeto", self.translation("Debit Amount"))
		self.assertIn("Kredito", self.translation("Credit Amount"))
		self.assertIn("patvirtin", self.translation("Purchase Invoice can be held after submitting.").lower())

	def test_payment_receipt_signer_and_buying_settings_rfq_help(self):
		for source, path, translation, reason in (
			(
				"For",
				"erpnext/accounts/print_format/payment_receipt_voucher/payment_receipt_voucher.html",
				"Įmonės vardu",
				"meaning_correction",
			),
			(
				"If set, the system does not use the user's Email or the standard outgoing Email account for sending request for quotations.",
				"erpnext/buying/doctype/buying_settings/buying_settings.json",
				"Jei nurodyta, siunčiant prašymus pateikti pasiūlymą sistema nenaudos nei naudotojo el. pašto, nei standartinės siunčiamojo el. pašto paskyros.",
				"new_translation",
			),
		):
			with self.subTest(source=source):
				self.assertTrue(
					any(
						loc["app"] == "erpnext" and loc["path"] == path
						for loc in self.inventory[source, None]["source_locations"]
					)
				)
				entry = self.entries[source, None]
				self.assertEqual(entry["translation"], translation)
				self.assertEqual(entry["provenance"]["review"]["reason"], reason)
				self.assertTrue(entry["provenance"]["review"]["explanation"])
				self.assertEqual(entry["provenance"]["review"]["run_id"], review_run_id(entry))

	def test_real_print_accounts_and_crm_sources_preserve_markup_and_tokens(self):
		for source, location, expected in (
			(
				"Sales Invoice",
				"erpnext/accounts/print_format/sales_auditing_voucher/",
				"Pardavimo sąskaita faktūra",
			),
			(
				"Purchase Invoice",
				"erpnext/accounts/print_format/purchase_auditing_voucher/",
				"Pirkimo sąskaita faktūra",
			),
			(
				"Journal Entry",
				"erpnext/accounts/print_format/journal_auditing_voucher/",
				"Bendrojo žurnalo įrašas",
			),
			(
				'<label class="control-label" style="margin-bottom: 0px;">Account Number Settings</label>',
				"erpnext/accounts/doctype/cheque_print_template/",
				"Banko sąskaitos numerio nustatymai",
			),
			(
				"Please set an email id for the Lead {0}",
				"erpnext/crm/doctype/email_campaign/",
				"potencialaus kliento",
			),
			("Failed to send email for campaign {0} to {1}", "erpnext/crm/doctype/email_campaign/", "{1}"),
			(
				'Learn about <a href="https://docs.frappe.io/erpnext/user/manual/en/common_party_accounting" rel="noopener noreferrer">Common Party</a>',
				"erpnext/accounts/doctype/accounts_settings/",
				"https://docs.frappe.io/erpnext/user/manual/en/common_party_accounting",
			),
			(
				"<strong>Grand Total:</strong> {0}",
				"erpnext/accounts/doctype/payment_request/payment_request.py",
				"{0}",
			),
		):
			with self.subTest(source=source):
				self.assertTrue(
					any(
						loc["app"] == "erpnext" and loc["origin"] == "source" and location in loc["path"]
						for loc in self.inventory[source, None]["source_locations"]
					)
				)
				translation = self.translation(source)
				self.assertIn(expected, translation)
				self.assertEqual(_tokens(source), _tokens(translation))
				self.assertEqual(_html_errors(source, translation), [])
				if source == "<strong>Grand Total:</strong> {0}":
					self.assertTrue(_tokens(source)[0])

	def test_report_matches_evidence_and_repeated_gate_output_is_identical(self):
		active_po = ROOT / "locale/lt.po"
		before = active_po.read_bytes()

		def compiled(_po, workspace):
			mo = workspace / "sites/assets/locale/lt/LC_MESSAGES/frappe_lt.mo"
			mo.parent.mkdir(parents=True)
			mo.write_bytes(b"compiled")

		with TemporaryDirectory() as directory:
			outputs = []
			for index in range(2):
				po = Path(directory) / f"finance-{index}.po"
				report = Path(directory) / f"finance-{index}.json"
				result = run(SEGMENT, po, report, compile_candidate=compiled)
				self.assertEqual(result["exit_code"], 0, result["errors"][:5])
				self.assertEqual(result["summary"]["translation_coverage"], {"covered": 4907, "total": 4907})
				self.assertEqual(result["summary"]["keys"], 4907)
				self.assertEqual(result["summary"]["corrected_inherited"], 1857)
				self.assertEqual(result["summary"]["translation_exceptions"], 4)
				self.assertEqual(
					result["summary"]["reason_counts"],
					{
						"accepted_as_is": 733,
						"approved_translation_exception": 4,
						"foreign_language_correction": 37,
						"grammar_correction": 550,
						"meaning_correction": 759,
						"new_translation": 2313,
						"punctuation_correction": 21,
						"terminology_correction": 490,
					},
				)
				self.assertEqual(
					{
						field: result["summary"][field]
						for field in (
							"corrected_inherited",
							"reason_counts",
							"translation_coverage",
							"translation_exceptions",
						)
					},
					evidence_summary(self.candidate["entries"], self.entries),
				)
				self.assertEqual(sum(result["summary"]["reason_counts"].values()), 4907)
				self.assertEqual(report.read_bytes(), canonical_json(result))
				outputs.append((po.read_bytes(), report.read_bytes()))
			self.assertEqual(*outputs)
		self.assertEqual(active_po.read_bytes(), before)

	def test_missing_extra_duplicate_conflicting_and_stale_candidate_entries_are_rejected(self):
		with TemporaryDirectory() as directory:
			root = Path(directory) / "release"
			shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("__pycache__", "*.py", "tests", "*.mo"))
			original = _read(CANDIDATE)
			registry = _read("catalog_segments.json")
			compatibility = _read("compatibility.json")
			for case, expected_code in (
				("missing", "MISSING_TRANSLATION_KEY"),
				("extra", "EXTRA_TRANSLATION_KEY"),
				("duplicate", "DUPLICATE_TRANSLATION_KEY"),
				("conflicting", "CONFLICTING_TRANSLATION"),
				("stale", "SOURCE_DIGEST_MISMATCH"),
			):
				with self.subTest(case=case):
					candidate = deepcopy(original)
					if case == "missing":
						candidate["entries"].pop(0)
					elif case == "extra":
						candidate["entries"].append(
							deepcopy(_read("catalog_candidates/frappe.json")["entries"][0])
						)
					elif case == "duplicate":
						candidate["entries"].append(deepcopy(candidate["entries"][0]))
					elif case == "conflicting":
						conflict = deepcopy(candidate["entries"][0])
						conflict["translation"] += "!"
						conflict["provenance"]["review"]["run_id"] = review_run_id(conflict)
						candidate["entries"].append(conflict)
					elif case == "stale":
						candidate["entries"][0]["source_digest"] = "0" * 64
						candidate["entries"][0]["provenance"]["review"]["run_id"] = review_run_id(
							candidate["entries"][0]
						)
					candidate_bytes = canonical_json(candidate)
					(root / CANDIDATE).write_bytes(candidate_bytes)
					updated_registry = deepcopy(registry)
					next(item for item in updated_registry["candidates"] if item["name"] == SEGMENT)[
						"candidate_sha256"
					] = hashlib.sha256(candidate_bytes).hexdigest()
					registry_bytes = canonical_json(updated_registry)
					(root / "catalog_segments.json").write_bytes(registry_bytes)
					updated_compatibility = deepcopy(compatibility)
					updated_compatibility["quality_gate"]["artifact_sha256"]["catalog_segments.json"] = (
						hashlib.sha256(registry_bytes).hexdigest()
					)
					(root / "compatibility.json").write_bytes(canonical_json(updated_compatibility))
					po = Path(directory) / f"{case}.po"
					result = run(
						SEGMENT,
						po,
						Path(directory) / f"{case}.json",
						compatibility_path=root / "compatibility.json",
						compile_candidate=lambda *_: self.fail("invalid candidate compiled"),
					)
					self.assertEqual(result["exit_code"], 1, result["errors"][:2])
					self.assertIn(expected_code, {error["code"] for error in result["errors"]})
					self.assertFalse(po.exists())

	def test_stale_candidate_bindings_and_hash_tampering_are_rejected(self):
		with TemporaryDirectory() as directory:
			root = Path(directory) / "release"
			shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("__pycache__", "*.py", "tests", "*.mo"))
			original = _read(CANDIDATE)
			registry = _read("catalog_segments.json")
			compatibility = _read("compatibility.json")
			original_registry_bytes = (root / "catalog_segments.json").read_bytes()
			original_compatibility_bytes = (root / "compatibility.json").read_bytes()
			for case, detail in (
				("stale_inventory", "is stale"),
				("stale_manifest", "changed its frozen manifest binding"),
				("stale_registry_manifest", "does not bind the frozen manifest"),
				("candidate_hash", "candidate digest mismatch"),
			):
				with self.subTest(case=case):
					(root / "catalog_segments.json").write_bytes(original_registry_bytes)
					(root / "compatibility.json").write_bytes(original_compatibility_bytes)
					candidate = deepcopy(original)
					if case == "stale_inventory":
						candidate["inventory_digest"] = "0" * 64
					elif case == "stale_manifest":
						candidate["manifest_sha256"] = "0" * 64
					else:
						candidate["entries"][0]["translation"] += "!"
					candidate_bytes = canonical_json(candidate)
					(root / CANDIDATE).write_bytes(candidate_bytes)
					updated_registry = deepcopy(registry)
					if case != "candidate_hash":
						registration = next(
							item for item in updated_registry["candidates"] if item["name"] == SEGMENT
						)
						registration["candidate_sha256"] = hashlib.sha256(candidate_bytes).hexdigest()
						if case == "stale_registry_manifest":
							registration["manifest_sha256"] = "0" * 64
						registry_bytes = canonical_json(updated_registry)
						(root / "catalog_segments.json").write_bytes(registry_bytes)
						updated_compatibility = deepcopy(compatibility)
						updated_compatibility["quality_gate"]["artifact_sha256"]["catalog_segments.json"] = (
							hashlib.sha256(registry_bytes).hexdigest()
						)
						(root / "compatibility.json").write_bytes(canonical_json(updated_compatibility))
					po = Path(directory) / f"{case}.po"
					result = run(
						SEGMENT,
						po,
						Path(directory) / f"{case}.json",
						compatibility_path=root / "compatibility.json",
						compile_candidate=lambda *_: self.fail("invalid candidate compiled"),
					)
					self.assertEqual(result["exit_code"], 2, result["errors"][:2])
					self.assertEqual(result["errors"][0]["code"], "UNTRUSTED_INPUT_OR_TOOL_FAILURE")
					self.assertIn(detail, result["errors"][0]["detail"])
					self.assertFalse(po.exists())
