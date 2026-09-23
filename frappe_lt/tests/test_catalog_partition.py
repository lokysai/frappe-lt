import hashlib
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from frappe_lt.catalog_partition import (
	build_partition,
	build_partition_artifacts,
	validate_partition_artifacts,
)
from frappe_lt.catalog_quality import run
from frappe_lt.inventory import canonical_json
from frappe_lt.review_evidence import review_run_id


def _entry(source, apps, locations, digest):
	return {
		"apps": apps,
		"key": {"context": None, "source": source},
		"source_digest": digest * 64,
		"source_locations": locations,
		"stable_locators": [],
	}


def _source(app, path):
	return {
		"app": app,
		"extractor": "extract_babel_python",
		"line": 1,
		"origin": "source",
		"path": path,
	}


class CatalogPartitionTest(TestCase):
	def test_registered_operations_candidate_covers_frozen_manifest_and_preserves_active_po(self):
		source_root = Path(__file__).parents[1]
		manifest = json.loads(source_root.joinpath("catalog_segments/erpnext-operations.json").read_bytes())
		candidate = json.loads(
			source_root.joinpath("catalog_candidates/erpnext-operations.json").read_bytes()
		)
		entries_by_source = {entry["key"]["source"]: entry for entry in candidate["entries"]}
		for source in (
			"<p>Posting Date {0} cannot be before Purchase Order date for the following:</p><ul>",
			"The stock for the item {0} in the {1} warehouse was negative on the {2}. "
			"You should create a positive entry {3} before the date {4} and time {5} to post the "
			"correct valuation rate. For more details, please read the "
			"<a href='https://docs.erpnext.com/docs/user/manual/en/stock-adjustment-cogs-with-negative-stock'>"
			"documentation<a>.",
		):
			entry = entries_by_source[source]
			self.assertNotEqual(entry["translation"], entry["key"]["source"])
			self.assertEqual(entry["provenance"]["review"]["reason"], "new_translation")
		escaped_markup = entries_by_source['<div id=\\"item-prices-container\\"></div>']
		self.assertEqual(escaped_markup["translation"], escaped_markup["key"]["source"])
		self.assertEqual(
			escaped_markup["provenance"]["review"]["reason"],
			"approved_translation_exception",
		)
		active_po = source_root / "locale" / "lt.po"
		active_before = active_po.read_bytes()

		with TemporaryDirectory() as directory:
			root = Path(directory)

			def compile_candidate(po_path, workspace):
				self.assertTrue(po_path.is_file())
				mo_path = workspace / "sites/assets/locale/lt/LC_MESSAGES/frappe_lt.mo"
				mo_path.parent.mkdir(parents=True)
				mo_path.write_bytes(b"compiled")

			result = run(
				"erpnext-operations",
				root / "erpnext-operations.po",
				root / "report.json",
				compile_candidate=compile_candidate,
			)

		self.assertEqual(result["exit_code"], 0)
		self.assertEqual(result["summary"]["keys"], len(manifest["keys"]))
		self.assertEqual(
			result["summary"]["translation_coverage"],
			{"covered": len(manifest["keys"]), "total": len(manifest["keys"])},
		)
		self.assertEqual(sum(result["summary"]["reason_counts"].values()), len(manifest["keys"]))
		self.assertEqual(active_po.read_bytes(), active_before)

	def test_frappe_first_finance_location_precedence_and_operations_remainder(self):
		entries = [
			_entry(
				"Shared",
				["erpnext", "frappe"],
				[
					_source("frappe", "frappe/shared.py"),
					_source("erpnext", "erpnext/accounts/doctype/account/account.py"),
				],
				"a",
			),
			_entry(
				"Mixed ERPNext",
				["erpnext"],
				[
					_source("erpnext", "erpnext/stock/doctype/item/item.py"),
					_source("erpnext", "erpnext/selling/doctype/customer/customer.py"),
				],
				"b",
			),
			_entry(
				"Operations",
				["erpnext"],
				[_source("erpnext", "erpnext/manufacturing/doctype/work_order/work_order.py")],
				"c",
			),
		]
		inventory = {"entries": entries, "schema_version": 1}
		digest = hashlib.sha256(canonical_json(inventory)).hexdigest()

		partition = build_partition(inventory, digest, [])

		self.assertEqual(
			{
				segment["id"]: [item["key"]["source"] for item in segment["keys"]]
				for segment in partition["segments"]
			},
			{
				"erpnext-finance-commerce": ["Mixed ERPNext"],
				"erpnext-operations": ["Operations"],
				"frappe": ["Shared"],
			},
		)

	def test_unclassifiable_key_requires_a_reviewed_source_bound_exact_override(self):
		entry = _entry(
			"Runtime only",
			["erpnext"],
			[
				{
					"app": "erpnext",
					"extractor": "page",
					"line": None,
					"origin": "runtime",
					"path": "metadata/page/runtime-only",
				}
			],
			"d",
		)
		inventory = {"entries": [entry], "schema_version": 1}
		digest = hashlib.sha256(canonical_json(inventory)).hexdigest()
		with self.assertRaisesRegex(ValueError, "requires an ownership override"):
			build_partition(inventory, digest, [])

		override = {
			"key": entry["key"],
			"reason": "Reviewed runtime Page owner is an ERPNext operations module.",
			"review": "issue-25",
			"reviewed": True,
			"segment_id": "erpnext-operations",
			"source_digest": entry["source_digest"],
		}
		partition = build_partition(inventory, digest, [override])

		operations = next(
			segment for segment in partition["segments"] if segment["id"] == "erpnext-operations"
		)
		self.assertEqual(operations["keys"], [{"key": entry["key"], "source_digest": "d" * 64}])

	def test_duplicate_stale_unnecessary_unknown_or_unreviewed_overrides_fail_closed(self):
		unknown = _entry(
			"Runtime only",
			["erpnext"],
			[
				{
					"app": "erpnext",
					"extractor": "page",
					"line": None,
					"origin": "runtime",
					"path": "metadata/page/runtime-only",
				}
			],
			"e",
		)
		classified = _entry(
			"Classified",
			["erpnext"],
			[_source("erpnext", "erpnext/stock/doctype/item/item.py")],
			"f",
		)
		inventory = {"entries": [unknown, classified], "schema_version": 1}
		digest = hashlib.sha256(canonical_json(inventory)).hexdigest()
		valid = {
			"key": unknown["key"],
			"reason": "Reviewed exact runtime owner.",
			"review": "issue-25",
			"reviewed": True,
			"segment_id": "erpnext-operations",
			"source_digest": unknown["source_digest"],
		}
		cases = {
			"duplicate": [valid, valid],
			"stale": [{**valid, "source_digest": "0" * 64}],
			"unnecessary": [{**valid, "key": classified["key"], "source_digest": "f" * 64}],
			"unknown key": [{**valid, "key": {"context": None, "source": "Absent"}}],
			"unknown segment": [{**valid, "segment_id": "other"}],
			"reviewed": [{**valid, "reviewed": False}],
		}
		for label, overrides in cases.items():
			with self.subTest(label=label), self.assertRaisesRegex(ValueError, label):
				build_partition(inventory, digest, overrides)

	def test_declared_apps_must_exactly_match_nonempty_source_location_apps(self):
		cases = (
			_entry("Empty", ["erpnext"], [], "a"),
			_entry("Mismatch", ["erpnext"], [_source("frappe", "frappe/example.py")], "b"),
			_entry(
				"Missing shared owner",
				["erpnext", "frappe"],
				[_source("erpnext", "erpnext/stock/item.py")],
				"c",
			),
		)
		for entry in cases:
			with self.subTest(source=entry["key"]["source"]):
				inventory = {"entries": [entry], "schema_version": 1}
				with self.assertRaisesRegex(ValueError, "apps.*Source Locations"):
					build_partition(
						inventory,
						hashlib.sha256(canonical_json(inventory)).hexdigest(),
						[],
					)

	def test_classifier_accepts_only_documented_exact_source_and_runtime_location_forms(self):
		valid_locations = [
			_source("frappe", "frappe/example.py"),
			{
				"app": "frappe",
				"extractor": "frappe.gettext.extractors.javascript",
				"line": 1,
				"origin": "source",
				"path": "cypress/integration/web_form.js",
			},
			_source("erpnext", "erpnext/accounts/doctype/account/account.py"),
			{
				"app": "erpnext",
				"extractor": "frappe.gettext.extractors.html_template",
				"line": None,
				"origin": "source",
				"path": "banking/src/components/example.tsx",
			},
			{
				"app": "erpnext",
				"extractor": "doctype",
				"line": None,
				"origin": "runtime",
				"path": "metadata/doctype/Account/name",
			},
		]
		entries = [
			_entry(f"Valid {index}", [location["app"]], [location], str(index + 1))
			for index, location in enumerate(valid_locations)
		]
		inventory = {"entries": entries, "schema_version": 1}
		digest = hashlib.sha256(canonical_json(inventory)).hexdigest()
		overrides = [
			{
				"key": entries[-1]["key"],
				"reason": "Runtime-only fixture has a reviewed exact owner.",
				"review": "issue-25",
				"reviewed": True,
				"segment_id": "erpnext-finance-commerce",
				"source_digest": entries[-1]["source_digest"],
			}
		]
		build_partition(inventory, digest, overrides)

		for path in ("vendor/erpnext/accounts/file.py", "erpnextish/accounts/file.py"):
			with self.subTest(path=path):
				bad = _entry("Bad", ["erpnext"], [_source("erpnext", path)], "a")
				bad_inventory = {"entries": [bad], "schema_version": 1}
				with self.assertRaisesRegex(ValueError, "documented Source Location form"):
					build_partition(
						bad_inventory,
						hashlib.sha256(canonical_json(bad_inventory)).hexdigest(),
						[],
					)

	def test_partition_and_three_manifests_are_canonical_and_repeatable(self):
		entries = [
			_entry("Framework", ["frappe"], [_source("frappe", "frappe/framework.py")], "a"),
			_entry(
				"Invoice",
				["erpnext"],
				[_source("erpnext", "erpnext/accounts/invoice.py")],
				"b",
			),
			_entry(
				"Stock",
				["erpnext"],
				[_source("erpnext", "erpnext/stock/item.py")],
				"c",
			),
		]
		inventory = {"entries": entries, "schema_version": 1}
		digest = hashlib.sha256(canonical_json(inventory)).hexdigest()
		overrides = {
			"classifier_schema_version": 1,
			"entries": [],
			"inventory_digest": digest,
			"schema_version": 1,
		}

		first = build_partition_artifacts(inventory, digest, overrides)
		second = build_partition_artifacts(inventory, digest, overrides)

		self.assertEqual(first, second)
		self.assertEqual(
			set(first),
			{
				"catalog_partition.json",
				"catalog_segment_ownership_overrides.json",
				"catalog_segments/erpnext-finance-commerce.json",
				"catalog_segments/erpnext-operations.json",
				"catalog_segments/frappe.json",
			},
		)
		for content in first.values():
			self.assertTrue(content.endswith(b"\n"))
			self.assertEqual(content, canonical_json(json.loads(content)))

	def test_authenticated_partition_rejects_gap_overlap_extra_wrong_digest_and_stale_inventory(self):
		entries = [
			_entry("Framework", ["frappe"], [_source("frappe", "frappe/framework.py")], "a"),
			_entry(
				"Invoice",
				["erpnext"],
				[_source("erpnext", "erpnext/accounts/invoice.py")],
				"b",
			),
			_entry("Stock", ["erpnext"], [_source("erpnext", "erpnext/stock/item.py")], "c"),
		]
		inventory = {"entries": entries, "schema_version": 1}
		digest = hashlib.sha256(canonical_json(inventory)).hexdigest()
		overrides = {
			"classifier_schema_version": 1,
			"entries": [],
			"inventory_digest": digest,
			"schema_version": 1,
		}
		valid = build_partition_artifacts(inventory, digest, overrides)
		validate_partition_artifacts(inventory, digest, valid)

		def changed_manifest(path, change):
			artifacts = dict(valid)
			manifest = json.loads(artifacts[path])
			change(manifest)
			artifacts[path] = canonical_json(manifest)
			partition = json.loads(artifacts["catalog_partition.json"])
			record = next(item for item in partition["segments"] if item["manifest"] == path)
			record["manifest_sha256"] = hashlib.sha256(artifacts[path]).hexdigest()
			artifacts["catalog_partition.json"] = canonical_json(partition)
			return artifacts

		finance = "catalog_segments/erpnext-finance-commerce.json"
		operations = "catalog_segments/erpnext-operations.json"
		cases = {
			"gap": changed_manifest(finance, lambda value: value["keys"].clear()),
			"overlap": changed_manifest(
				operations,
				lambda value: value["keys"].append(
					{"key": entries[1]["key"], "source_digest": entries[1]["source_digest"]}
				),
			),
			"extra": changed_manifest(
				operations,
				lambda value: value["keys"].append(
					{
						"key": {"context": None, "source": "Extra"},
						"source_digest": "d" * 64,
					}
				),
			),
			"source digest": changed_manifest(
				operations, lambda value: value["keys"][0].update(source_digest="0" * 64)
			),
		}
		stale = dict(valid)
		partition = json.loads(stale["catalog_partition.json"])
		partition["inventory_digest"] = "0" * 64
		stale["catalog_partition.json"] = canonical_json(partition)
		cases["stale"] = stale
		for label, artifacts in cases.items():
			with self.subTest(label=label), self.assertRaisesRegex(ValueError, label):
				validate_partition_artifacts(inventory, digest, artifacts)

	def test_attached_inventory_exact_union_and_later_candidate_use_the_same_gate_without_active_po(self):
		source_root = Path(__file__).parents[1]
		self.assertFalse(source_root.joinpath("catalog_segments/item-smoke.json").exists())
		self.assertFalse(source_root.joinpath("catalog_candidates/item-smoke.json").exists())
		compatibility = json.loads(source_root.joinpath("compatibility.json").read_bytes())
		registry = json.loads(source_root.joinpath("catalog_segments.json").read_bytes())
		self.assertNotIn("item-smoke", {record["name"] for record in registry["candidates"]})
		inventory = json.loads(source_root.joinpath("release_inventory.json").read_bytes())
		partition = json.loads(source_root.joinpath("catalog_partition.json").read_bytes())
		inventory_keys = {(entry["key"]["source"], entry["key"]["context"]) for entry in inventory["entries"]}
		claimed = set()
		manifests = {}
		for record in partition["segments"]:
			manifest = json.loads(source_root.joinpath(record["manifest"]).read_bytes())
			keys = {(item["key"]["source"], item["key"]["context"]) for item in manifest["keys"]}
			self.assertFalse(claimed & keys)
			claimed |= keys
			manifests[record["id"]] = (record, manifest)
		self.assertEqual(claimed, inventory_keys)
		self.assertEqual(len(claimed), 16_535)

		item = next(
			entry for entry in inventory["entries"] if entry["key"] == {"context": None, "source": "Item"}
		)
		segment_id = next(
			segment_id
			for segment_id, (_record, manifest) in manifests.items()
			if any(selected["key"] == item["key"] for selected in manifest["keys"])
		)
		manifest_record = manifests[segment_id][0]
		candidate_entry = {
			"flags": [],
			"key": item["key"],
			"provenance": {
				"origin": "new_ai",
				"review": {
					"agent": "ci-test-agent",
					"explanation": None,
					"model": "test/model",
					"reason": "new_translation",
					"run_id": "",
					"status": "reviewed",
				},
			},
			"source_digest": item["source_digest"],
			"translation": "Prekė",
		}
		candidate_entry["provenance"]["review"]["run_id"] = review_run_id(candidate_entry)
		candidate = {
			"entries": [candidate_entry],
			"inventory_digest": compatibility["inventory_digest"],
			"manifest_sha256": manifest_record["manifest_sha256"],
			"review_evidence_schema_version": 1,
			"schema_version": 1,
			"segment_id": segment_id,
		}
		active_po = source_root / "locale" / "lt.po"
		active_before = active_po.read_bytes()
		with TemporaryDirectory() as directory:
			root = Path(directory)
			for name in {
				*compatibility["artifact_sha256"],
				*compatibility["quality_gate"]["artifact_sha256"],
				*(record["manifest"] for record in partition["segments"]),
			}:
				target = root / name
				target.parent.mkdir(parents=True, exist_ok=True)
				shutil.copyfile(source_root / name, target)
			candidate_path = root / "catalog_candidates" / "item-test.json"
			candidate_path.parent.mkdir(parents=True, exist_ok=True)
			candidate_bytes = canonical_json(candidate)
			candidate_path.write_bytes(candidate_bytes)
			registry = {
				"candidate_directory": "catalog_candidates",
				"candidates": [
					{
						"candidate": "catalog_candidates/item-test.json",
						"candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
						"manifest": manifest_record["manifest"],
						"manifest_sha256": manifest_record["manifest_sha256"],
						"name": "item-test",
						"segment_id": segment_id,
					}
				],
				"inventory_digest": compatibility["inventory_digest"],
				"schema_version": 1,
			}
			registry_bytes = canonical_json(registry)
			root.joinpath("catalog_segments.json").write_bytes(registry_bytes)
			compatibility["quality_gate"]["artifact_sha256"]["catalog_segments.json"] = hashlib.sha256(
				registry_bytes
			).hexdigest()
			root.joinpath("compatibility.json").write_bytes(canonical_json(compatibility))

			first = run(
				"item-test",
				root / "candidate.po",
				root / "first-report.json",
				compatibility_path=root / "compatibility.json",
				fail_fast=True,
			)
			second = run(
				"item-test",
				root / "candidate.po",
				root / "second-report.json",
				compatibility_path=root / "compatibility.json",
				fail_fast=True,
			)
			self.assertEqual(first["exit_code"], 1)
			self.assertEqual(first["errors"][0]["code"], "MISSING_TRANSLATION_KEY")
			self.assertEqual(first["schema_version"], 2)
			self.assertNotIn("duration_seconds", first)
			self.assertEqual(first, second)
			self.assertEqual(
				root.joinpath("first-report.json").read_bytes(),
				root.joinpath("second-report.json").read_bytes(),
			)
			self.assertFalse(root.joinpath("candidate.po").exists())
		self.assertEqual(active_po.read_bytes(), active_before)
