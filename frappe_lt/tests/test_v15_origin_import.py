import csv
import hashlib
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from frappe_lt import v15_origin_import
from frappe_lt.catalog_quality import registered_candidates
from frappe_lt.inventory import (
	build_report,
	canonical_json,
	generate_human_report,
	verify_owned_artifacts,
	write_artifacts,
)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_BASELINE_SHA256 = "7c343b91fef46a7574c1f0e32397b39b06834be600b848b1355fded1e758da1a"


class V15OriginImportTest(TestCase):
	def test_committed_origin_baseline_remains_authenticated_after_review(self):
		compatibility = verify_owned_artifacts(ROOT / "compatibility.json")
		self.assertIn("erpnext-operations", registered_candidates(ROOT / "compatibility.json"))
		manifest = json.loads((ROOT / "catalog_segments/frappe.json").read_bytes())
		self.assertEqual(
			compatibility["inventory_digest"],
			manifest["inventory_digest"],
		)
		keys = {(item["key"]["source"], item["key"]["context"]) for item in manifest["keys"]}
		provenance = json.loads((ROOT / "provenance.json").read_bytes())
		records = {
			(record["key"]["source"], record["key"]["context"]): record
			for record in provenance["entries"]
			if (record["key"]["source"], record["key"]["context"]) in keys
		}
		self.assertEqual(len(keys), len(records))
		self.assertEqual(sum(record["status"] == "missing" for record in records.values()), 2902)
		self.assertEqual(sum(record["status"] == "excepted" for record in records.values()), 168)
		self.assertEqual(
			sum(
				(record["status"] == "translated" and record.get("origin") == "inherited_v15")
				or (record["status"] == "excepted" and "v15_original" in record)
				for record in records.values()
			),
			3346,
		)
		# The original v15 text survives even for reviewed Translation Exceptions.
		self.assertEqual(records[("User", None)]["translation"], "Vartotojas")
		self.assertEqual(records[("Submit", None)]["translation"], "Pateikti")
		self.assertEqual(records[("Account", None)]["translation"], "sąskaita")
		self.assertEqual(
			hashlib.sha256(
				canonical_json(
					[
						{
							"key": record["key"],
							"translation": record.get("v15_original", record.get("translation")),
						}
						for record in provenance["entries"]
						if (record["key"]["source"], record["key"]["context"]) in keys
						and (record.get("origin") == "inherited_v15" or "v15_original" in record)
					]
				)
			).hexdigest(),
			EXPECTED_BASELINE_SHA256,
		)
		self.assertEqual(
			v15_origin_import.CSV_SHA256,
			{
				"frappe-v15-lt.csv": "e3c8546c2f1e0a15bc676b42c7ee5704c79e834fffaaa988995a9d60ddae173c",
				"erpnext-v15-lt.csv": "eb43f49b82834cfdcfe9e938c2d55d355a40de228160a221b3d9b89605b6ef7f",
			},
		)

	def setUp(self):
		self.temp = TemporaryDirectory()
		self.addCleanup(self.temp.cleanup)
		self.root = Path(self.temp.name) / "artifacts"
		shutil.copytree(
			ROOT,
			self.root,
			ignore=shutil.ignore_patterns("__pycache__", "*.py", "tests", "*.mo"),
		)
		self.manifest = json.loads((self.root / "catalog_segments/frappe.json").read_bytes())
		# Always start from the pre-review Frappe state, even when the copied
		# production release has already received its v15 baseline.
		compatibility_path = self.root / "compatibility.json"
		compatibility = verify_owned_artifacts(compatibility_path)
		registry_path = self.root / "catalog_segments.json"
		registry = json.loads(registry_path.read_bytes())
		for candidate in registry["candidates"]:
			if candidate["name"] != "erpnext-operations":
				(self.root / candidate["candidate"]).unlink()
		registry["candidates"] = [
			candidate for candidate in registry["candidates"] if candidate["name"] == "erpnext-operations"
		]
		registry_bytes = canonical_json(registry)
		registry_path.write_bytes(registry_bytes)
		compatibility["quality_gate"]["artifact_sha256"]["catalog_segments.json"] = hashlib.sha256(
			registry_bytes
		).hexdigest()
		frappe_keys = {(item["key"]["source"], item["key"]["context"]) for item in self.manifest["keys"]}
		inventory = json.loads((self.root / "release_inventory.json").read_bytes())
		provenance = json.loads((self.root / "provenance.json").read_bytes())
		provenance["entries"] = [
			{"key": record["key"], "status": "missing"}
			if (record["key"]["source"], record["key"]["context"]) in frappe_keys
			else record
			for record in provenance["entries"]
		]
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
		verify_owned_artifacts(compatibility_path)
		registered_candidates(compatibility_path)
		self.source = next(
			item["key"]["source"] for item in self.manifest["keys"] if item["key"]["context"] is None
		)
		self.context_key = next(
			item["key"]
			for item in self.manifest["keys"]
			if item["key"]["context"]
			and item["key"]["source"] != self.source
			and not any(
				other["key"] == {"source": item["key"]["source"], "context": None}
				for other in self.manifest["keys"]
			)
		)
		self.csvs = [Path(self.temp.name) / "frappe-v15-lt.csv", Path(self.temp.name) / "erpnext-v15-lt.csv"]

	def _csv(self, rows, extra=()):
		for path, values in zip(self.csvs, (rows, extra), strict=True):
			with path.open("w", encoding="utf-8", newline="") as stream:
				csv.writer(stream).writerows(values)
		return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.csvs}

	def _run(self, digests, *, matches=1):
		with (
			patch.object(v15_origin_import, "CSV_SHA256", digests),
			patch.object(v15_origin_import, "EXPECTED_MATCHES", matches),
		):
			return v15_origin_import.run(self.root / "compatibility.json", *self.csvs)

	def test_import_exact_keys_and_reauthenticate_idempotently(self):
		text = "  Žodis\n"
		digests = self._csv(
			[
				(self.source, text, ""),
				(self.context_key["source"], "Klaidingas kontekstas", ""),
				(self.source + " ", "Klaidingas tarpas", ""),
			]
		)
		old_coverage = json.loads((self.root / "inventory_report.json").read_bytes())["summary"]["coverage"]
		old_provenance = json.loads((self.root / "provenance.json").read_bytes())
		before = {
			name: (self.root / name).read_bytes()
			for name in (
				"release_inventory.json",
				"catalog_segments/frappe.json",
				"catalog_partition.json",
				"catalog_segments.json",
				"catalog_candidates/erpnext-operations.json",
			)
			if (self.root / name).exists()
		}
		result = self._run(digests)
		self.assertEqual(result["inherited"], 1)
		self.assertEqual(result["missing"], 6343)
		provenance = json.loads((self.root / "provenance.json").read_bytes())
		frappe_keys = {(item["key"]["source"], item["key"]["context"]) for item in self.manifest["keys"]}
		self.assertEqual(
			[
				e
				for e in old_provenance["entries"]
				if (e["key"]["source"], e["key"]["context"]) not in frappe_keys
			],
			[
				e
				for e in provenance["entries"]
				if (e["key"]["source"], e["key"]["context"]) not in frappe_keys
			],
		)
		records = {(e["key"]["source"], e["key"]["context"]): e for e in provenance["entries"]}
		self.assertEqual(records[(self.source, None)]["translation"], text)
		self.assertEqual(records[(self.source, None)]["origin"], "inherited_v15")
		self.assertNotIn("exception", records[(self.source, None)])
		self.assertEqual(
			records[(self.context_key["source"], self.context_key["context"])]["status"], "missing"
		)
		report = json.loads((self.root / "inventory_report.json").read_bytes())
		self.assertEqual(
			report["summary"]["coverage"],
			{
				**old_coverage,
				"missing": old_coverage["missing"] - 1,
				"translated": old_coverage["translated"] + 1,
			},
		)
		compatibility = verify_owned_artifacts(self.root / "compatibility.json")
		self.assertEqual(compatibility["inventory_digest"], report["inventory_digest"])
		self.assertEqual(registered_candidates(self.root / "compatibility.json"), ["erpnext-operations"])
		self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
		updated = {name: (self.root / name).read_bytes() for name in result["artifacts"]}
		self._run(digests)
		self.assertEqual(updated, {name: (self.root / name).read_bytes() for name in updated})
		self.assertEqual(
			compatibility["artifact_sha256"]["inventory_report.json"],
			hashlib.sha256(canonical_json(report)).hexdigest(),
		)

	def test_conflicting_duplicate_fails_before_any_mutation(self):
		digests = self._csv([(self.source, "Pirma", "")], [(self.source, "Antra", "")])
		before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
		with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
			self._run(digests)
		self.assertEqual(
			before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
		)

	def test_rejects_tampered_trust_boundary_and_csv(self):
		digests = self._csv([(self.source, "Vertimas", "")])
		with self.assertRaisesRegex(ValueError, "CSV digest mismatch"):
			self._run({**digests, self.csvs[0].name: "0" * 64})
		path = self.root / "catalog_segments/frappe.json"
		path.write_bytes(path.read_bytes() + b" ")
		with self.assertRaises(ValueError):
			self._run(digests)

	def test_rejects_wrong_commit_marker_and_registered_frappe_before_writing(self):
		digests = self._csv([(self.source, "Vertimas", "")])
		with self.assertRaisesRegex(ValueError, "commit marker"):
			v15_origin_import.run(self.root / "inventory_report.json", *self.csvs)
		registry_path = self.root / "catalog_segments.json"
		registry = json.loads(registry_path.read_bytes())
		registry["candidates"][0]["name"] = "frappe"
		registry_bytes = canonical_json(registry)
		registry_path.write_bytes(registry_bytes)
		compatibility_path = self.root / "compatibility.json"
		compatibility = json.loads(compatibility_path.read_bytes())
		compatibility["quality_gate"]["artifact_sha256"]["catalog_segments.json"] = hashlib.sha256(
			registry_bytes
		).hexdigest()
		compatibility_path.write_bytes(canonical_json(compatibility))
		before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
		with self.assertRaisesRegex(ValueError, "precede Frappe candidate registration"):
			self._run(digests)
		self.assertEqual(
			before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
		)

	def test_full_frappe_segment_counts_without_external_csvs(self):
		keys = [item["key"] for item in self.manifest["keys"][: v15_origin_import.EXPECTED_MATCHES]]
		digests = self._csv([(key["source"], "Originalas", key["context"] or "") for key in keys])
		with patch.object(v15_origin_import, "CSV_SHA256", digests):
			result = v15_origin_import.run(self.root / "compatibility.json", *self.csvs)
		self.assertEqual((result["inherited"], result["missing"]), (3346, 2998))
		provenance = json.loads((self.root / "provenance.json").read_bytes())
		self.assertEqual(
			sum(
				record["origin"] == "inherited_v15" for record in provenance["entries"] if "origin" in record
			),
			3346,
		)
		verify_owned_artifacts(self.root / "compatibility.json")
		registered_candidates(self.root / "compatibility.json")
