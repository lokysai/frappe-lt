"""Finance v15 originals in a release with two already-registered candidates."""

import csv
import hashlib
import json
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from frappe_lt import v15_origin_import
from frappe_lt.catalog_quality import _html_errors, _tokens, registered_candidates, run
from frappe_lt.inventory import (
	build_report,
	canonical_json,
	generate_human_report,
	verify_owned_artifacts,
	write_artifacts,
)

ROOT = Path(__file__).resolve().parents[1]
ORIGINALS_SHA256 = "dbb132ad648c072fd30feacd3a6b7b2c17b32d329d4de9c16f7764d1347ecc21"


class CommittedFinanceOriginTest(TestCase):
	def test_real_finance_print_and_crm_email_keys_keep_markup_and_tokens(self):
		inventory = json.loads((ROOT / "release_inventory.json").read_bytes())
		selectors = json.loads((ROOT / "glossary_selectors.json").read_bytes())
		for source, translation, location, required in (
			(
				'<label class="control-label" style="margin-bottom: 0px;">Account Number Settings</label>',
				'<label class="control-label" style="margin-bottom: 0px;">Banko sąskaitos numerio nustatymai</label>',
				"erpnext/accounts/doctype/cheque_print_template/cheque_print_template.json",
				"Banko sąskaitos numerio nustatymai",
			),
			(
				"Please set an email id for the Lead {0}",
				"Nurodykite potencialaus kliento {0} el. pašto adresą",
				"erpnext/crm/doctype/email_campaign/email_campaign.py",
				"potencialaus kliento",
			),
		):
			with self.subTest(source=source):
				key = {"source": source, "context": None}
				entry = next(item for item in inventory["entries"] if item["key"] == key)
				self.assertTrue(any(loc["path"] == location for loc in entry["source_locations"]))
				selector = next(item for item in selectors["entries"] if item["key"] == key)
				self.assertEqual(selector["source_digest"], entry["source_digest"])
				self.assertEqual(selector["accepted_forms"], [required])
				self.assertEqual(_tokens(source), _tokens(translation))
				self.assertEqual(_html_errors(source, translation), [])

	def test_finance_and_crm_glossary_selectors_bind_exact_real_keys(self):
		inventory = json.loads((ROOT / "release_inventory.json").read_bytes())
		by_key = {(item["key"]["source"], item["key"]["context"]): item for item in inventory["entries"]}
		selectors = json.loads((ROOT / "glossary_selectors.json").read_bytes())
		selected = {(item["key"]["source"], item["key"]["context"]): item for item in selectors["entries"]}
		for source, expected in {
			"General Ledger": "Didžioji knyga",
			"Journal Entry": "Bendrojo žurnalo įrašas",
			"Cost Center": "Sąnaudų centras",
			"Invoice": "Sąskaita faktūra",
			"Valuation Rate": "Apskaitinė vieneto vertė",
			"Sales Invoice": "Pardavimo sąskaita faktūra",
			"Purchase Invoice": "Pirkimo sąskaita faktūra",
			"Opportunity": "Pardavimo galimybė",
			"Quotation": "Komercinis pasiūlymas",
			"Purchase Order": "Pirkimo užsakymas",
		}.items():
			with self.subTest(source=source):
				selector = selected[source, None]
				self.assertTrue(selector["reviewed"])
				self.assertEqual(selector["source_digest"], by_key[source, None]["source_digest"])
				self.assertEqual(selector["accepted_forms"], [expected])
				self.assertFalse(
					any(form in expected for form in selector["forbidden_forms"]),
					"a correct glossary form must not also trigger a forbidden-form match",
				)
				self.assertIn(
					f'<a id="{selector["glossary_id"].removeprefix("CONTEXT.md#")}"></a>',
					(ROOT.parent / "CONTEXT.md").read_text(),
				)

	def test_lead_glossary_selector_binds_the_real_crm_key_and_all_its_locations(self):
		inventory = json.loads((ROOT / "release_inventory.json").read_bytes())
		entry = next(
			item for item in inventory["entries"] if item["key"] == {"source": "Lead", "context": None}
		)
		self.assertEqual(len(entry["source_locations"]), 23)
		selectors = json.loads((ROOT / "glossary_selectors.json").read_bytes())
		selector = next(item for item in selectors["entries"] if item["key"] == entry["key"])
		self.assertTrue(selector["reviewed"])
		self.assertEqual(selector["glossary_id"], "CONTEXT.md#glossary-lead")
		self.assertEqual(selector["source_digest"], entry["source_digest"])
		self.assertEqual(selector["accepted_forms"], ["Potencialus klientas"])
		self.assertIn("Vadovauti", selector["forbidden_forms"])

	def test_reviewed_finance_exceptions_bind_exact_originals_and_source_digests(self):
		inventory = json.loads((ROOT / "release_inventory.json").read_bytes())
		provenance = json.loads((ROOT / "provenance.json").read_bytes())
		exceptions = json.loads((ROOT / "translation_exceptions.json").read_bytes())
		for source, context, digest, original, explanation in (
			(
				"IBAN",
				None,
				"cc0b8c010029d86b7c834d7d31af70dc20c98e5bc8a87f2ff767cc4b52dfb829",
				"IBAN",
				"Reviewed standard international bank-account identifier; keep the IBAN label unchanged.",
			),
			(
				"[{0}] {1}",
				"Financial Report Template",
				"92d53b9a2f162106f7b8911f2705b3233e092e987bf07616134ddc2111742a6a",
				None,
				"Techninis finansinės ataskaitos šablono formatas: abu parametrai ir jų skliaustai turi likti nepakeisti.",
			),
			(
				"<li>{}</li>",
				None,
				"6c0ed09ffb9a28fd48a5c43252830bd20ea69081910ef1c46ce5d20a2910c78a",
				None,
				"Techninis HTML sąrašo fragmentas su vieninteliu parametru; žymos ir parametras turi likti nepakeisti.",
			),
			(
				"BOM",
				None,
				"f5c12d2e2a1e4011937930f8a628cbef55db42b525bcf8be51bbb08b43687575",
				"BOM",
				"BOM yra patvirtinta techninė santrumpa pagal CONTEXT.md; paliekama nepakeista, išsaugant v15 originalą.",
			),
		):
			with self.subTest(source=source):
				key = {"source": source, "context": context}
				entry = next(item for item in inventory["entries"] if item["key"] == key)
				self.assertEqual(entry["source_digest"], digest)
				if source == "IBAN":
					self.assertEqual(len(entry["source_locations"]), 8)
				origin = next(record for record in provenance["entries"] if record["key"] == key)
				expected_origin = {
					"key": key,
					"origin": "approved_exception",
					"status": "excepted",
					"exception": explanation,
				}
				if original is not None:
					expected_origin["v15_original"] = original
				self.assertEqual(origin, expected_origin)
				review = next(record for record in exceptions["entries"] if record["key"] == key)
				self.assertTrue(review["reviewed"])
				self.assertTrue(review["review"])
				self.assertEqual(review["source_digest"], digest)

	def test_complete_original_set_and_existing_candidates_stay_authenticated(self):
		compatibility = verify_owned_artifacts(ROOT / "compatibility.json")
		manifest = json.loads((ROOT / "catalog_segments/erpnext-finance-commerce.json").read_bytes())
		keys = {(entry["key"]["source"], entry["key"]["context"]) for entry in manifest["keys"]}
		provenance = json.loads((ROOT / "provenance.json").read_bytes())
		originals = [
			{"key": record["key"], "translation": record.get("v15_original", record.get("translation"))}
			for record in provenance["entries"]
			if (record["key"]["source"], record["key"]["context"]) in keys
			and (record.get("origin") == "inherited_v15" or "v15_original" in record)
		]
		self.assertEqual(len(keys), 4907)
		self.assertEqual(len(originals), 2592)
		self.assertEqual(
			sum(
				record.get("origin") == "inherited_v15"
				for record in provenance["entries"]
				if (record["key"]["source"], record["key"]["context"]) in keys
			),
			2590,
		)
		self.assertEqual(hashlib.sha256(canonical_json(originals)).hexdigest(), ORIGINALS_SHA256)
		report = json.loads((ROOT / "inventory_report.json").read_bytes())
		self.assertEqual(
			report["summary"],
			{
				"coverage": {"excepted": 230, "missing": 10441, "translated": 5864},
				"lifecycle": {"changed": 0, "new": 16535, "removed": 0, "unchanged": 0},
			},
		)
		self.assertEqual(manifest["inventory_digest"], compatibility["inventory_digest"])
		self.assertTrue(
			{"erpnext-operations", "frappe"} <= set(registered_candidates(ROOT / "compatibility.json"))
		)

		def compiled(_po, workspace):
			mo = workspace / "sites/assets/locale/lt/LC_MESSAGES/frappe_lt.mo"
			mo.parent.mkdir(parents=True)
			mo.write_bytes(b"compiled")

		with TemporaryDirectory() as directory:
			for candidate in registered_candidates(ROOT / "compatibility.json"):
				with self.subTest(candidate=candidate):
					result = run(
						candidate,
						Path(directory) / f"{candidate}.po",
						Path(directory) / f"{candidate}.json",
						compile_candidate=compiled,
					)
					self.assertEqual(result["exit_code"], 0, result["errors"][:3])


class FinanceOriginImportTest(TestCase):
	def setUp(self):
		self.temp = TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name) / "artifacts"
		shutil.copytree(
			ROOT, self.root, ignore=shutil.ignore_patterns("__pycache__", "*.py", "tests", "*.mo")
		)
		self.compatibility = self.root / "compatibility.json"
		self.manifest = json.loads(
			(self.root / "catalog_segments/erpnext-finance-commerce.json").read_bytes()
		)
		self.keys = {(item["key"]["source"], item["key"]["context"]) for item in self.manifest["keys"]}
		# Rebuild the pre-review state even after the production origin baseline is imported.
		compatibility = verify_owned_artifacts(self.compatibility)
		registry_path = self.root / "catalog_segments.json"
		registry = json.loads(registry_path.read_bytes())
		finance = [
			item for item in registry["candidates"] if item["segment_id"] == "erpnext-finance-commerce"
		]
		for item in finance:
			(self.root / item["candidate"]).unlink()
		if finance:
			registry["candidates"] = [
				item for item in registry["candidates"] if item["segment_id"] != "erpnext-finance-commerce"
			]
			registry_bytes = canonical_json(registry)
			registry_path.write_bytes(registry_bytes)
			compatibility["quality_gate"]["artifact_sha256"]["catalog_segments.json"] = hashlib.sha256(
				registry_bytes
			).hexdigest()
		inventory = json.loads((self.root / "release_inventory.json").read_bytes())
		provenance = json.loads((self.root / "provenance.json").read_bytes())
		for record in provenance["entries"]:
			key = record["key"]["source"], record["key"]["context"]
			if key in self.keys:
				record.clear()
				record.update(key={"source": key[0], "context": key[1]}, status="missing")
				if key == ("Item", None):
					record.update(origin="new_ai", status="translated", translation="Prekė")
		report = build_report(inventory, provenance, compatibility)
		artifacts = {
			"provenance.json": canonical_json(provenance),
			"inventory_report.json": canonical_json(report),
			"inventory_report.md": generate_human_report(report).encode(),
		}
		compatibility["artifact_sha256"].update(
			{name: hashlib.sha256(content).hexdigest() for name, content in artifacts.items()}
		)
		artifacts["compatibility.json"] = canonical_json(compatibility)
		write_artifacts(self.root, artifacts)
		self.csvs = [Path(self.temp.name) / name for name in v15_origin_import.CSV_SHA256]

	def csv(self, frappe=(), erpnext=()):
		for path, rows in zip(self.csvs, (frappe, erpnext), strict=True):
			with path.open("w", newline="", encoding="utf-8") as stream:
				csv.writer(stream).writerows(rows)
		return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.csvs}

	def run_import(self, digests, matches):
		def compiled(_po, workspace):
			mo = workspace / "sites/assets/locale/lt/LC_MESSAGES/frappe_lt.mo"
			mo.parent.mkdir(parents=True)
			mo.write_bytes(b"compiled")

		with (
			patch.object(v15_origin_import, "CSV_SHA256", digests),
			patch.object(v15_origin_import, "FINANCE_EXPECTED_MATCHES", matches),
		):
			return v15_origin_import.run(
				self.compatibility,
				*self.csvs,
				segment_id="erpnext-finance-commerce",
				compile_candidate=compiled,
			)

	def test_finance_exact_context_and_original_preserve_other_segments_and_candidates(self):
		key = next(item["key"] for item in self.manifest["keys"] if item["key"]["context"])
		digests = self.csv(
			erpnext=[
				(key["source"], "Tiksli reikšmė", key["context"]),
				(key["source"], "Neteisingas kontekstas", ""),
				("Item", "Prekė", ""),
			]
		)
		before = {
			name: (self.root / name).read_bytes()
			for name in (
				"release_inventory.json",
				"catalog_partition.json",
				"catalog_segments.json",
				"catalog_candidates/frappe.json",
				"catalog_candidates/erpnext-operations.json",
			)
		}
		original = json.loads((self.root / "provenance.json").read_bytes())
		with patch.object(
			v15_origin_import, "catalog_quality_gate", wraps=v15_origin_import.catalog_quality_gate
		) as gate:
			result = self.run_import(digests, 2)
		self.assertEqual(
			[call.args[0] for call in gate.call_args_list],
			["erpnext-operations", "frappe"] * 2,
		)
		self.assertEqual((result["inherited"], result["missing"]), (2, len(self.keys) - 2))
		provenance = json.loads((self.root / "provenance.json").read_bytes())

		def other(entries):
			return [r for r in entries if (r["key"]["source"], r["key"]["context"]) not in self.keys]

		self.assertEqual(other(provenance["entries"]), other(original["entries"]))
		self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
		self.assertEqual(registered_candidates(self.compatibility), ["erpnext-operations", "frappe"])
		matched = next(r for r in provenance["entries"] if r["key"] == key)
		self.assertEqual((matched["origin"], matched["translation"]), ("inherited_v15", "Tiksli reikšmė"))
		verify_owned_artifacts(self.compatibility)
		published = {name: (self.root / name).read_bytes() for name in result["artifacts"]}
		self.run_import(digests, 2)
		self.assertEqual(published, {name: (self.root / name).read_bytes() for name in published})

	def test_empty_conflicting_and_mismatched_existing_finance_origins_never_publish(self):
		before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
		for rows, count, error in (
			([], 0, "unmatched"),
			([("Item", "", "")], 1, "expected 1 nonempty exact"),
			([("Item", "Prekė", ""), ("Item", "Kitas", "")], 1, "conflicting duplicate"),
			([("Item", "Kita prekė", "")], 1, "conflicting existing"),
		):
			with self.subTest(error=error):
				digests = self.csv(erpnext=rows)
				with self.assertRaisesRegex(ValueError, error):
					self.run_import(digests, count)
				self.assertEqual(
					before,
					{p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()},
				)

	def test_same_source_in_wrong_context_is_not_imported_and_cross_csv_conflict_is_rejected(self):
		key = next(item["key"] for item in self.manifest["keys"] if item["key"]["context"])
		before = {name: (self.root / name).read_bytes() for name in ("provenance.json", "compatibility.json")}
		digests = self.csv(erpnext=[(key["source"], "Wrong context", "")])
		with self.assertRaisesRegex(ValueError, "expected 1 nonempty exact"):
			self.run_import(digests, 1)
		self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
		digests = self.csv(
			frappe=[(key["source"], "First", key["context"])],
			erpnext=[(key["source"], "Second", key["context"])],
		)
		with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
			self.run_import(digests, 1)
		self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})

	def test_interrupted_publish_restores_authenticated_release_and_can_retry(self):
		digests = self.csv(erpnext=[("Item", "Prekė", "")])
		before = {
			name: (self.root / name).read_bytes()
			for name in (
				"compatibility.json",
				"provenance.json",
				"inventory_report.json",
				"inventory_report.md",
			)
		}

		def interrupt(root, artifacts):
			def replace(source, target):
				if Path(target).name == "compatibility.json":
					raise OSError("interrupted commit")
				os.replace(source, target)

			write_artifacts(root, artifacts, replace=replace)

		with patch.object(v15_origin_import, "write_artifacts", side_effect=interrupt):
			with self.assertRaisesRegex(OSError, "interrupted commit"):
				self.run_import(digests, 1)
		self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
		verify_owned_artifacts(self.compatibility)
		self.run_import(digests, 1)
		self.assertEqual(registered_candidates(self.compatibility), ["erpnext-operations", "frappe"])

	def test_repeat_import_keeps_reviewed_exception_and_its_v15_original(self):
		digests = self.csv(erpnext=[("Item", "Prekė", ""), ("IBAN", "IBAN", "")])
		self.run_import(digests, 2)
		compatibility = verify_owned_artifacts(self.compatibility)
		inventory = json.loads((self.root / "release_inventory.json").read_bytes())
		provenance = json.loads((self.root / "provenance.json").read_bytes())
		record = next(
			record for record in provenance["entries"] if record["key"] == {"source": "IBAN", "context": None}
		)
		record.pop("translation")
		record.update(
			origin="approved_exception",
			status="excepted",
			v15_original="IBAN",
			exception="Reviewed international bank-account identifier.",
		)
		report = build_report(inventory, provenance, compatibility)
		artifacts = {
			"provenance.json": canonical_json(provenance),
			"inventory_report.json": canonical_json(report),
			"inventory_report.md": generate_human_report(report).encode(),
		}
		compatibility["artifact_sha256"].update(
			{name: hashlib.sha256(content).hexdigest() for name, content in artifacts.items()}
		)
		artifacts["compatibility.json"] = canonical_json(compatibility)
		write_artifacts(self.root, artifacts)
		before = {name: (self.root / name).read_bytes() for name in artifacts}
		result = self.run_import(digests, 2)
		self.assertEqual((result["inherited"], result["missing"]), (1, len(self.keys) - 2))
		self.assertEqual(before, {name: (self.root / name).read_bytes() for name in artifacts})

	def test_keyboard_interrupt_during_publish_or_post_publication_restores_and_can_retry(self):
		digests = self.csv(erpnext=[("Item", "Prekė", "")])
		before = {
			name: (self.root / name).read_bytes()
			for name in (
				"compatibility.json",
				"provenance.json",
				"inventory_report.json",
				"inventory_report.md",
			)
		}

		def interrupt_publish(root, artifacts):
			def replace(source, target):
				if Path(target).name == "compatibility.json":
					raise KeyboardInterrupt("during publication")
				os.replace(source, target)

			write_artifacts(root, artifacts, replace=replace)

		original_registered = v15_origin_import.registered_candidates
		calls = 0

		def interrupt_check(path):
			nonlocal calls
			calls += 1
			if calls == 2:
				raise KeyboardInterrupt("after publication")
			return original_registered(path)

		for name, side_effect in (
			("write_artifacts", interrupt_publish),
			("registered_candidates", interrupt_check),
		):
			with self.subTest(stage=name), patch.object(v15_origin_import, name, side_effect=side_effect):
				with self.assertRaises(KeyboardInterrupt):
					self.run_import(digests, 1)
			self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
			verify_owned_artifacts(self.compatibility)
			self.assertEqual(registered_candidates(self.compatibility), ["erpnext-operations", "frappe"])
		self.assertEqual(self.run_import(digests, 1)["inherited"], 1)

	def test_gate_failure_before_or_after_publication_never_strands_provenance(self):
		digests = self.csv(erpnext=[("Item", "Prekė", "")])
		before = {
			name: (self.root / name).read_bytes()
			for name in (
				"compatibility.json",
				"provenance.json",
				"inventory_report.json",
				"inventory_report.md",
			)
		}
		original_gate = v15_origin_import.catalog_quality_gate
		for fail_on_call in (1, 3):
			with self.subTest(fail_on_call=fail_on_call):
				calls = 0

				def gate(*args, threshold=fail_on_call, **kwargs):
					nonlocal calls
					calls += 1
					if calls == threshold:
						return {"exit_code": 1, "errors": [{"code": "TEST_FAILURE"}]}
					return original_gate(*args, **kwargs)

				with patch.object(v15_origin_import, "catalog_quality_gate", side_effect=gate):
					with self.assertRaisesRegex(ValueError, "failed Catalog Quality Gate"):
						self.run_import(digests, 1)
				self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
				verify_owned_artifacts(self.compatibility)
