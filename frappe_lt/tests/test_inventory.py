import hashlib
import importlib
import json
import random
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from frappe_lt.inventory import (
	ExtractionEvent,
	_load_provenance,
	_quality_artifacts_for_inventory,
	build_inventory,
	build_report,
	canonical_json,
	generate_human_report,
	load_compatibility,
	validate_provenance,
	validate_tool_versions,
	verify_owned_artifacts,
	write_artifacts,
)
from frappe_lt.runtime_extraction import (
	METADATA_SIGNATURE_CATEGORIES,
	RUNTIME_METADATA_CATEGORIES,
	_erpnext_installer_navbar_labels,
	_metadata_digest,
	_navbar_owners,
	_validate_metadata_signatures,
	extract_runtime,
	verify_standard_metadata,
)
from frappe_lt.source_extraction import (
	_source_locator,
	_source_stable_locator,
	configured_method_map,
	extract_babel_python,
	extract_python,
)


class CompatibilityManifestTest(TestCase):
	def test_manifest_is_the_authority_for_pinned_release(self):
		manifest = load_compatibility()

		self.assertEqual(manifest["schema_version"], 2)
		self.assertEqual(
			manifest["upstream"],
			{
				"erpnext": {
					"commit": "12cd563fb9a79731f75ae2a45b1446a0a2dd9e74",
					"version": "16.35.0",
				},
				"frappe": {
					"commit": "c1f1e8ec3708750d7254f7f99d869ffb9886f19f",
					"version": "16.34.0",
				},
			},
		)
		self.assertEqual(manifest["schema_versions"], {"inventory": 1, "provenance": 1, "report": 1})
		self.assertEqual(manifest["source_date_epoch"], 1704067200)
		self.assertEqual(manifest["tools"], {"babel": "2.16.0", "python": "3.14"})
		self.assertEqual(set(manifest["runtime_metadata_sha256"]), set(METADATA_SIGNATURE_CATEGORIES))
		self.assertEqual(
			set(manifest["artifact_sha256"]),
			{
				"inventory_report.json",
				"inventory_report.md",
				"provenance.json",
				"release_inventory.json",
			},
		)
		self.assertEqual(manifest["quality_gate"]["schema_version"], 1)
		self.assertEqual(
			set(manifest["quality_gate"]["artifact_sha256"]),
			{
				"catalog_segments.json",
				"collision_resolutions.json",
				"glossary_selectors.json",
				"translation_exceptions.json",
			},
		)

	def test_manifest_rejects_noncanonical_pins(self):
		manifest = load_compatibility()
		manifest["upstream"]["frappe"]["commit"] = "short"
		with TemporaryDirectory() as directory:
			path = Path(directory) / "compatibility.json"
			path.write_bytes(canonical_json(manifest))
			with self.assertRaisesRegex(ValueError, "full commit"):
				load_compatibility(path)

	def test_only_authenticated_legacy_baselines_may_use_schema_one_without_quality_fields(self):
		manifest = load_compatibility()
		legacy = {**manifest, "schema_version": 1}
		legacy.pop("quality_gate")
		with TemporaryDirectory() as directory:
			path = Path(directory) / "compatibility.json"
			path.write_bytes(canonical_json(legacy))
			with self.assertRaisesRegex(ValueError, "schema_version must be 2"):
				load_compatibility(path)
			self.assertEqual(
				load_compatibility(path, allow_legacy_baseline=True)["schema_version"],
				1,
			)


class ReleaseInventoryTest(TestCase):
	def test_source_and_runtime_events_form_one_active_key(self):
		manifest = load_compatibility()
		events = [
			ExtractionEvent(
				source="  Account  ",
				context=None,
				app="erpnext",
				origin="source",
				raw_source="  Account  ",
				source_location="erpnext/accounts/doctype/account/account.py",
				extractor="python",
				line=12,
			),
			ExtractionEvent(
				source="Account",
				context=None,
				app="erpnext",
				origin="runtime",
				raw_source="Account",
				source_location="metadata/doctype/Account",
				extractor="doctype",
				stable_locator="doctype:Account:name",
			),
			ExtractionEvent(
				source="Account",
				context=None,
				app="frappe",
				origin="runtime",
				raw_source="Account",
				source_location="metadata/doctype/Email Account/field/account_section",
				extractor="doctype",
				stable_locator="doctype:Email Account:field:account_section:label",
			),
		]

		inventory = build_inventory(events, manifest)

		self.assertEqual(inventory["schema_version"], 1)
		self.assertEqual(len(inventory["entries"]), 1)
		entry = inventory["entries"][0]
		self.assertEqual(entry["key"], {"context": None, "source": "Account"})
		self.assertEqual(entry["extraction_origin"], "both")
		self.assertEqual(entry["raw_sources"], ["  Account  ", "Account"])
		self.assertEqual(entry["apps"], ["erpnext", "frappe"])
		self.assertEqual(entry["upstream_versions"], {"erpnext": "16.35.0", "frappe": "16.34.0"})
		self.assertNotIn("upstream", entry)
		self.assertEqual(len(entry["source_locations"]), 3)
		self.assertEqual(
			entry["stable_locators"],
			[
				"erpnext:doctype:Account:name",
				"frappe:doctype:Email Account:field:account_section:label",
			],
		)
		self.assertEqual(len(entry["source_digest"]), 64)
		self.assertEqual(entry["context_decision"], "CONTEXT.md#saskaita-account-contextless-collision")

	def test_normalization_preserves_internal_newlines_unicode_and_raw_variants_deterministically(self):
		values = [" line\r\nbreak ", "line\nbreak", r"line\nbreak", "é", "e\u0301"]
		events = [
			ExtractionEvent(value, None, "frappe", "source", value, f"frappe/{index}.py", "python")
			for index, value in enumerate(values)
		]
		manifest = load_compatibility()
		first = build_inventory(events, manifest)
		random.Random(7).shuffle(events)
		second = build_inventory([*events, events[0]], manifest)

		self.assertEqual(canonical_json(first), canonical_json(second))
		self.assertEqual(len(first["entries"]), 5)
		self.assertIn(
			" line\r\nbreak ",
			next(entry for entry in first["entries"] if entry["key"]["source"] == "line\r\nbreak")[
				"raw_sources"
			],
		)
		self.assertNotEqual(
			next(entry for entry in first["entries"] if entry["key"]["source"] == "é")["source_digest"],
			next(entry for entry in first["entries"] if entry["key"]["source"] == "e\u0301")["source_digest"],
		)

	def test_invalid_context_and_nonrelative_location_are_rejected(self):
		manifest = load_compatibility()
		for event, diagnostic in (
			(ExtractionEvent("Key", "", "frappe", "source", "Key", "frappe/key.py", "python"), "context"),
			(ExtractionEvent("Key", None, "frappe", "source", "Key", "/tmp/key.py", "python"), "POSIX"),
		):
			with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(ValueError, diagnostic):
				build_inventory([event], manifest)

	def test_account_decision_requires_both_pinned_collision_locations(self):
		with self.assertRaisesRegex(ValueError, "Account invariant"):
			build_inventory(
				[
					ExtractionEvent(
						"Account",
						None,
						"erpnext",
						"runtime",
						"Account",
						"metadata/doctype/Account",
						"doctype",
						stable_locator="doctype:Account:name",
					)
				],
				load_compatibility(),
			)

	def test_provenance_assigns_exactly_one_valid_coverage_status(self):
		inventory = build_inventory(
			[
				ExtractionEvent("Item", None, "erpnext", "source", "Item", "erpnext/item.py", "python"),
				ExtractionEvent(
					"Missing", None, "erpnext", "runtime", "Missing", "metadata/doctype/Missing", "doctype"
				),
			],
			load_compatibility(),
		)
		provenance = {
			"schema_version": 1,
			"entries": [
				{
					"key": {"context": None, "source": "Missing"},
					"status": "missing",
				},
				{
					"key": {"context": None, "source": "Item"},
					"origin": "new_ai",
					"status": "translated",
					"translation": "Prekė",
				},
			],
		}

		coverage = validate_provenance(inventory, provenance)

		self.assertEqual(coverage[("Missing", None)]["status"], "missing")
		self.assertEqual(coverage[("Item", None)]["status"], "translated")

	def test_no_baseline_marks_every_active_key_new_and_report_totals_match(self):
		manifest = load_compatibility()
		inventory = build_inventory(
			[ExtractionEvent("Item", None, "erpnext", "source", "Item", "erpnext/item.py", "python")],
			manifest,
		)
		provenance = {
			"schema_version": 1,
			"entries": [
				{
					"exception": None,
					"key": {"context": None, "source": "Item"},
					"origin": "new_ai",
					"status": "translated",
					"translation": "Prekė",
				}
			],
		}

		report = build_report(inventory, provenance, manifest)
		human = generate_human_report(report)

		self.assertEqual(report["inventory_digest"], hashlib.sha256(canonical_json(inventory)).hexdigest())
		self.assertEqual(report["baseline"], {"inventory_digest": None, "provided": False})
		self.assertEqual(
			report["summary"]["lifecycle"], {"changed": 0, "new": 1, "removed": 0, "unchanged": 0}
		)
		self.assertEqual(report["summary"]["coverage"], {"excepted": 0, "missing": 0, "translated": 1})
		self.assertEqual(report["entries"][0]["lifecycle"], "new")
		self.assertNotIn("previous_key", report["entries"][0])
		self.assertNotIn("source_digest", report["entries"][0])
		self.assertIn("New: 1", human)
		self.assertIn("Translated: 1", human)
		self.assertIn("| Source | Context | Lifecycle | Coverage |", human)
		self.assertNotIn("Previous key", human)
		self.assertIn("| <code>Item</code> |  | new | translated |", human)

	def test_human_report_escapes_table_and_html_in_all_key_fields(self):
		report = {
			"baseline": {"provided": False, "inventory_digest": None},
			"entries": [
				{
					"coverage": "missing",
					"key": {"context": "x|y\rnext", "source": "</code>|\nrow"},
					"lifecycle": "new",
				}
			],
			"inventory_digest": "0" * 64,
			"removed": [{"key": {"context": None, "source": "</code>|removed"}}],
			"summary": {
				"coverage": {"excepted": 0, "missing": 1, "translated": 0},
				"lifecycle": {"changed": 0, "new": 1, "removed": 1, "unchanged": 0},
			},
		}

		human = generate_human_report(report)

		self.assertNotIn("</code>|", human)
		self.assertIn("&lt;/code&gt;&#124;<br>row", human)
		self.assertIn("x&#124;y<br>next", human)

	def test_valid_baseline_reports_all_lifecycle_transitions_without_unsafe_matching(self):
		manifest = load_compatibility()
		old = build_inventory(
			[
				ExtractionEvent("Keep", None, "frappe", "source", "Keep", "frappe/keep.py", "python"),
				ExtractionEvent("Same key", None, "frappe", "source", "Same key", "frappe/old.py", "python"),
				ExtractionEvent(
					"Old title",
					None,
					"erpnext",
					"runtime",
					"Old title",
					"metadata/report/Sales",
					"report",
					stable_locator="report:Sales:title",
				),
				ExtractionEvent(
					"Unsafe old", None, "erpnext", "runtime", "Unsafe old", "metadata/report/Unsafe", "report"
				),
			],
			manifest,
		)
		current = build_inventory(
			[
				ExtractionEvent("Keep", None, "frappe", "source", "Keep", "frappe/keep.py", "python"),
				ExtractionEvent("Same key", None, "frappe", "source", "Same key", "frappe/new.py", "python"),
				ExtractionEvent(
					"New title",
					None,
					"erpnext",
					"runtime",
					"New title",
					"metadata/report/Sales",
					"report",
					stable_locator="report:Sales:title",
				),
				ExtractionEvent(
					"Unsafe new", None, "erpnext", "runtime", "Unsafe new", "metadata/report/Unsafe", "report"
				),
			],
			manifest,
		)
		provenance = {
			"schema_version": 1,
			"entries": [
				{
					"key": entry["key"],
					"status": "missing",
					"translation": None,
					"exception": None,
					"origin": None,
				}
				for entry in current["entries"]
			],
		}
		old_digest = hashlib.sha256(canonical_json(old)).hexdigest()

		baseline_manifest = {
			**manifest,
			"artifact_sha256": {**manifest["artifact_sha256"], "release_inventory.json": old_digest},
			"inventory_digest": old_digest,
		}
		report = build_report(current, provenance, manifest, old, old_digest, baseline_manifest)
		human = generate_human_report(report)

		self.assertEqual(
			report["summary"]["lifecycle"], {"changed": 2, "new": 1, "removed": 1, "unchanged": 1}
		)
		by_source = {entry["key"]["source"]: entry for entry in report["entries"]}
		self.assertEqual(by_source["Keep"]["lifecycle"], "unchanged")
		self.assertEqual(by_source["Same key"]["lifecycle"], "changed")
		self.assertEqual(by_source["New title"]["previous_key"]["source"], "Old title")
		self.assertEqual(by_source["Unsafe new"]["lifecycle"], "new")
		self.assertNotIn("previous_key", by_source["Unsafe new"])
		self.assertTrue(all("source_digest" not in entry for entry in report["entries"]))
		self.assertEqual(report["removed"][0]["key"]["source"], "Unsafe old")
		self.assertEqual(len(report["removed"][0]["source_digest"]), 64)
		self.assertIn("Previous key", human)
		with self.assertRaisesRegex(ValueError, "baseline digest"):
			build_report(current, provenance, manifest, old, "0" * 64, baseline_manifest)

	def test_same_source_in_a_different_app_is_removed_and_new(self):
		manifest = load_compatibility()
		old = build_inventory(
			[ExtractionEvent("Moved", None, "frappe", "source", "Moved", "frappe/moved.py", "python")],
			manifest,
		)
		current = build_inventory(
			[ExtractionEvent("Moved", None, "erpnext", "source", "Moved", "erpnext/moved.py", "python")],
			manifest,
		)
		provenance = {
			"schema_version": 1,
			"entries": [
				{
					"exception": None,
					"key": {"context": None, "source": "Moved"},
					"origin": None,
					"status": "missing",
					"translation": None,
				}
			],
		}

		report = build_report(
			current,
			provenance,
			manifest,
			old,
			(old_digest := hashlib.sha256(canonical_json(old)).hexdigest()),
			{
				**manifest,
				"artifact_sha256": {
					**manifest["artifact_sha256"],
					"release_inventory.json": old_digest,
				},
				"inventory_digest": old_digest,
			},
		)

		self.assertEqual(report["entries"][0]["lifecycle"], "new")
		self.assertEqual(report["removed"][0]["lifecycle"], "removed")

	def test_baseline_is_validated_against_its_own_release_manifest_and_exact_bytes(self):
		manifest = load_compatibility()
		prior_manifest = json.loads(json.dumps(manifest))
		prior_manifest["upstream"]["frappe"] = {"commit": "1" * 40, "version": "16.33.0"}
		old = build_inventory(
			[ExtractionEvent("Item", None, "frappe", "source", "Item", "frappe/item.py", "python")],
			prior_manifest,
		)
		old_bytes = canonical_json(old)
		old_digest = hashlib.sha256(old_bytes).hexdigest()
		prior_manifest["inventory_digest"] = old_digest
		prior_manifest["artifact_sha256"]["release_inventory.json"] = old_digest
		prior_manifest["schema_version"] = 1
		prior_manifest.pop("quality_gate")
		current = build_inventory(
			[ExtractionEvent("Item", None, "frappe", "source", "Item", "frappe/item.py", "python")],
			manifest,
		)
		provenance = {
			"schema_version": 1,
			"entries": [{"key": {"context": None, "source": "Item"}, "status": "missing"}],
		}

		report = build_report(current, provenance, manifest, old, old_digest, prior_manifest, old_bytes)

		self.assertTrue(report["baseline"]["provided"])
		noncanonical = b" " + old_bytes
		noncanonical_digest = hashlib.sha256(noncanonical).hexdigest()
		prior_manifest["inventory_digest"] = noncanonical_digest
		prior_manifest["artifact_sha256"]["release_inventory.json"] = noncanonical_digest
		with self.assertRaisesRegex(ValueError, "not canonical JSON"):
			build_report(
				current,
				provenance,
				manifest,
				old,
				noncanonical_digest,
				prior_manifest,
				noncanonical,
			)

	def test_provenance_rejects_duplicates_unknown_keys_and_contradictory_states(self):
		inventory = build_inventory(
			[ExtractionEvent("Item", None, "erpnext", "source", "Item", "erpnext/item.py", "python")],
			load_compatibility(),
		)
		valid = {
			"exception": None,
			"key": {"context": None, "source": "Item"},
			"origin": "new_ai",
			"status": "translated",
			"translation": "Prekė",
		}
		invalid_sets = (
			([valid, valid], "duplicate"),
			([{**valid, "key": {"context": None, "source": "Unknown"}}], "unknown"),
			([{**valid, "origin": None}], "invalid origin"),
			([{**valid, "status": "excepted", "origin": "approved_exception"}], "explicit exception"),
			([{**valid, "status": "missing"}], "cannot have"),
		)
		for entries, diagnostic in invalid_sets:
			with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(ValueError, diagnostic):
				validate_provenance(inventory, {"schema_version": 1, "entries": entries})

	def test_loading_provenance_omits_null_optional_fields_without_validating(self):
		with TemporaryDirectory() as directory:
			path = Path(directory) / "provenance.json"
			path.write_text(
				'{"schema_version":1,"entries":[{"key":{"context":null,"source":"Unknown"},'
				'"status":"missing","translation":null,"origin":null,"exception":null}]}',
				encoding="utf-8",
			)

			provenance = _load_provenance(path)

		self.assertEqual(
			provenance["entries"][0],
			{"key": {"context": None, "source": "Unknown"}, "status": "missing"},
		)

	def test_missing_provenance_blocks_report_generation(self):
		inventory = build_inventory(
			[
				ExtractionEvent("Existing", None, "frappe", "source", "Existing", "frappe/a.py", "python"),
				ExtractionEvent("New", None, "frappe", "source", "New", "frappe/b.py", "python"),
			],
			load_compatibility(),
		)
		provenance = {
			"schema_version": 1,
			"entries": [
				{
					"key": {"context": None, "source": "Existing"},
					"origin": "new_ai",
					"status": "translated",
					"translation": "Esamas",
				}
			],
		}

		with self.assertRaisesRegex(ValueError, "provenance is missing 1 active Translation Key"):
			build_report(inventory, provenance, load_compatibility())


class SourceExtractionTest(TestCase):
	def test_every_configured_babel_mapping_invokes_its_extractor(self):
		import frappe
		from frappe.gettext.translate import PYTHON_KEYWORDS

		fixtures = {
			"**/hooks.py": None,
			"**/doctype/*/*.json": b'{"name":"Fixture","fields":[]}',
			"**/desktop_icon/*.json": b'{"label":"Fixture"}',
			"**/workspace_sidebar/*.json": b'{"title":"Fixture","items":[]}',
			"**/workspace/*/*.json": b'{"doctype":"Workspace","label":"Fixture","content":"[]"}',
			"**/web_form/*/*.json": b'{"doctype":"Web Form","name":"fixture","title":"Fixture"}',
			"**/onboarding_step/*/*.json": b'{"doctype":"Onboarding Step","title":"Fixture"}',
			"**/module_onboarding/*/*.json": b'{"doctype":"Module Onboarding","title":"Fixture"}',
			"**/report/*/*.json": b'{"doctype":"Report","report_name":"Fixture"}',
			"**.py": b'_("Fixture")',
			"**/templates/**.js": b'_("Fixture")',
			"**.js": b'__("Fixture")',
			"**.html": b'_("Fixture")',
			"**.vue": b'_("Fixture")',
			"**/custom/*.json": (
				b'{"doctype":"Fixture","custom_fields":'
				b'[{"fieldname":"fixture","fieldtype":"Data","label":"Fixture"}]}'
			),
			"**/fixtures/custom_field.json": (
				b'[{"dt":"Fixture","fieldname":"fixture","fieldtype":"Data","label":"Fixture"}]'
			),
			"**/setup/setup_wizard/data/uom_data.json": b'[{"uom_name":"Fixture"}]',
			"**/setup/doctype/incoterm/incoterms.csv": b"code,title\nFIX,Fixture\n",
			"**/setup/setup_wizard/data/*.txt": b"Fixture\n",
			"**.tsx": b'_("Fixture")',
			"**.ts": b'_("Fixture")',
		}
		expected_patterns = {
			"frappe": set(list(fixtures)[:16]),
			"erpnext": set(fixtures),
		}
		for app in ("frappe", "erpnext"):
			method_map = configured_method_map(app)
			self.assertEqual({pattern for pattern, _method in method_map}, expected_patterns[app])
			for pattern, method in method_map:
				with self.subTest(app=app, pattern=pattern):
					if pattern == "**/hooks.py":
						fileobj = Path(frappe.get_app_source_path(app, app, "hooks.py")).open("rb")
					else:
						fileobj = BytesIO(fixtures[pattern])
						fileobj.name = f"fixture/{pattern.replace('*', 'x').replace('/', '_')}"
					if isinstance(method, str):
						module_name, function_name = method.rsplit(".", 1)
						extractor = getattr(importlib.import_module(module_name), function_name)
					else:
						extractor = method
					try:
						rows = list(extractor(fileobj, PYTHON_KEYWORDS, (), {}))
					finally:
						fileobj.close()
					self.assertTrue(rows)

	def test_python_lang_is_not_context_but_explicit_context_is(self):
		code = """\
_("positional lang", "lt")
_("positional context", "lt", "Ledger")
_("keyword lang", lang="lt")
_("keyword context", context="Button")
_("both keywords", lang="lt", context="Notice")
frappe._("qualified", "lt", "Qualified")
N_("marker context", "Marker")
"""
		with TemporaryDirectory() as directory:
			path = Path(directory) / "messages.py"
			path.write_text(code, encoding="utf-8")

			events = extract_python(path, "frappe", "frappe/messages.py")

		self.assertEqual(
			[(event.source, event.context) for event in events],
			[
				("positional lang", None),
				("positional context", "Ledger"),
				("keyword lang", None),
				("keyword context", "Button"),
				("both keywords", "Notice"),
				("qualified", "Qualified"),
				("marker context", "Marker"),
			],
		)
		self.assertTrue(all(event.origin == "source" for event in events))
		self.assertEqual(events[0].stable_locator, "source:frappe/messages.py:python:1")
		self.assertNotIn(events[0].source, events[0].stable_locator)

		fileobj = BytesIO(code.encode())
		fileobj.name = "frappe/messages.py"
		babel_messages = list(extract_babel_python(fileobj, {}, (), {}))
		self.assertEqual(
			[(message, context) for _line, _function, message, context in babel_messages],
			[
				("positional lang", []),
				(("Ledger", "positional context"), []),
				("keyword lang", []),
				(("Button", "keyword context"), []),
				(("Notice", "both keywords"), []),
				(("Qualified", "qualified"), []),
				(("Marker", "marker context"), []),
			],
		)

	def test_python_parse_error_identifies_extractor_and_file(self):
		with TemporaryDirectory() as directory:
			path = Path(directory) / "broken.py"
			path.write_text('_("unterminated"', encoding="utf-8")
			with self.assertRaisesRegex(ValueError, "python extractor could not parse frappe/broken.py"):
				extract_python(path, "frappe", "frappe/broken.py")
			with self.assertRaisesRegex(ValueError, "python extractor could not read frappe/missing.py"):
				extract_python(Path(directory) / "missing.py", "frappe", "frappe/missing.py")

		fileobj = BytesIO(b'_("unterminated"')
		fileobj.name = "frappe/babel-broken.py"
		with self.assertRaisesRegex(ValueError, "python extractor could not parse frappe/babel-broken.py"):
			list(extract_babel_python(fileobj, {}, (), {}))

	def test_source_locator_is_owning_app_relative_and_rejects_escape(self):
		with TemporaryDirectory() as directory:
			root = Path(directory) / "app"
			root.mkdir()
			self.assertEqual(_source_locator(root, "module/file.py"), "module/file.py")
			with self.assertRaisesRegex(ValueError, "outside owning app"):
				_source_locator(root, "../other/file.py")

	def test_source_stable_locator_requires_path_extractor_and_positive_line(self):
		self.assertEqual(
			_source_stable_locator("frappe/module/file.py", "extract_babel_python", 7),
			"source:frappe/module/file.py:extract_babel_python:7",
		)
		for location, extractor, line in (
			("", "python", 1),
			("/tmp/file.py", "python", 1),
			("../file.py", "python", 1),
			("frappe/file.py", "", 1),
			("frappe/file.py", "python", None),
			("frappe/file.py", "python", 0),
		):
			with self.subTest(location=location, extractor=extractor, line=line):
				self.assertIsNone(_source_stable_locator(location, extractor, line))

	def test_unique_source_locator_links_changed_text_but_ambiguity_does_not(self):
		manifest = load_compatibility()
		with TemporaryDirectory() as directory:
			path = Path(directory) / "messages.py"
			path.write_text('_("Old text")\n', encoding="utf-8")
			old = build_inventory(extract_python(path, "frappe", "frappe/messages.py"), manifest)
			old_digest = hashlib.sha256(canonical_json(old)).hexdigest()
			baseline_manifest = {
				**manifest,
				"artifact_sha256": {
					**manifest["artifact_sha256"],
					"release_inventory.json": old_digest,
				},
				"inventory_digest": old_digest,
			}

			path.write_text('_("New text")\n', encoding="utf-8")
			current = build_inventory(extract_python(path, "frappe", "frappe/messages.py"), manifest)
			provenance = {
				"schema_version": 1,
				"entries": [{"key": current["entries"][0]["key"], "status": "missing"}],
			}
			report = build_report(current, provenance, manifest, old, old_digest, baseline_manifest)
			self.assertEqual(report["entries"][0]["lifecycle"], "changed")
			self.assertEqual(report["entries"][0]["previous_key"]["source"], "Old text")

			path.write_text('_("New one"); _("New two")\n', encoding="utf-8")
			ambiguous = build_inventory(extract_python(path, "frappe", "frappe/messages.py"), manifest)
			ambiguous_provenance = {
				"schema_version": 1,
				"entries": [{"key": entry["key"], "status": "missing"} for entry in ambiguous["entries"]],
			}
			ambiguous_report = build_report(
				ambiguous,
				ambiguous_provenance,
				manifest,
				old,
				old_digest,
				baseline_manifest,
			)

		self.assertEqual({entry["lifecycle"] for entry in ambiguous_report["entries"]}, {"new"})
		self.assertTrue(all("previous_key" not in entry for entry in ambiguous_report["entries"]))
		self.assertEqual([entry["key"]["source"] for entry in ambiguous_report["removed"]], ["Old text"])


class RuntimeExtractionTest(TestCase):
	def test_navbar_ownership_uses_frappe_hooks_and_erpnext_installer_source(self):
		class FakeFrappe:
			@staticmethod
			def get_hooks(hook, app_name=None):
				self.assertEqual(app_name, "frappe")
				return [{"item_label": "About"}] if hook == "standard_navbar_items" else []

		self.assertEqual(
			set(_erpnext_installer_navbar_labels()),
			{"Documentation", "Frappe School", "Report an Issue", "User Forum"},
		)
		self.assertEqual(
			_navbar_owners(FakeFrappe()),
			{
				"About": {"frappe"},
				"Documentation": {"erpnext"},
				"Frappe School": {"erpnext"},
				"Report an Issue": {"erpnext"},
				"User Forum": {"erpnext"},
			},
		)
		self.assertNotIn("Delete Demo Data", _navbar_owners(FakeFrappe()))

	def test_runtime_requires_a_non_development_site_with_exact_pinned_apps(self):
		class FakeFrappe:
			class local:
				site = "development.localhost"

			@staticmethod
			def get_installed_apps():
				return ["frappe", "erpnext", "payments"]

		with self.assertRaisesRegex(ValueError, "development.localhost"):
			extract_runtime(FakeFrappe(), {})

		FakeFrappe.local.site = "test_site"
		with self.assertRaisesRegex(ValueError, "exactly.*frappe.*erpnext"):
			extract_runtime(FakeFrappe(), {})

	def test_runtime_reads_standard_metadata_without_get_meta_or_row_names(self):
		class FakeFrappe:
			class local:
				site = "test_site"

			@staticmethod
			def get_installed_apps():
				return ["frappe", "erpnext"]

			@staticmethod
			def get_module_app(module):
				return "erpnext" if module == "Accounts" else "frappe"

			@staticmethod
			def get_hooks(hook, app_name=None):
				if hook == "standard_navbar_items" and app_name == "frappe":
					return [{"item_label": "About"}]
				return []

			@staticmethod
			def get_all(doctype, filters=None, **kwargs):
				if kwargs.get("limit") == 1:
					return []
				if doctype == "DocType":
					return [{"name": "Account", "module": "Accounts", "description": " Ledger account "}]
				if doctype == "DocField":
					return [
						{
							"parent": "Account",
							"fieldname": "account_type",
							"fieldtype": "Select",
							"label": "Account Type",
							"description": None,
							"options": "Asset\n1\nLiability",
						}
					]
				if doctype in {"DocPerm", "DocType Link", "Report Filter"}:
					return []
				if doctype == "Page":
					return [{"name": "ledger", "title": "Ledger", "module": "Accounts"}]
				if doctype == "Report":
					return [
						{
							"name": "Account Balance",
							"report_name": "Account Balance",
							"module": "Accounts",
							"ref_doctype": "Account",
							"query": 'select account as "Account Label:Data"',
						}
					]
				if doctype == "Report Column":
					return [
						{
							"parent": "Account Balance",
							"fieldname": "balance",
							"label": "Balance",
						}
					]
				if doctype == "Navbar Item":
					return [{"item_label": "About"}]
				if doctype == "Workspace":
					return [
						{
							"name": "Accounts",
							"label": "Accounts",
							"app": "erpnext",
							"for_user": "",
							"public": 1,
							"content": '[{"type":"header","data":{"text":"Overview"}}]',
						}
					]
				if doctype == "Workspace Link":
					return [
						{
							"parent": "Accounts",
							"idx": 1,
							"label": "Reports",
							"description": "Financial reports",
						}
					]
				if doctype in {
					"Workspace Chart",
					"Workspace Number Card",
					"Workspace Shortcut",
					"Workspace Quick List",
				}:
					return []
				if doctype == "Workspace Sidebar":
					return [
						{
							"name": "Accounts Setup",
							"title": "Accounts Setup",
							"app": "erpnext",
							"for_user": "",
						}
					]
				if doctype == "Workspace Sidebar Item":
					return [{"parent": "Accounts Setup", "idx": 1, "label": "Setup"}]
				if doctype == "Custom Field":
					return [
						{
							"name": "Account-custom_account_label",
							"dt": "Account",
							"fieldname": "custom_account_label",
							"fieldtype": "Data",
							"label": "Custom Account Label",
							"is_system_generated": 1,
						}
					]
				if doctype == "Property Setter":
					return [
						{
							"name": "Account-account_type-label",
							"doc_type": "Account",
							"doctype_or_field": "DocField",
							"field_name": "account_type",
							"property": "label",
							"value": "Effective Account Type",
							"is_system_generated": 1,
						}
					]
				raise AssertionError(doctype)

		with patch("frappe_lt.runtime_extraction._validate_metadata_signatures"):
			extraction = extract_runtime(FakeFrappe(), {})
		events = extraction.events

		by_source = {event.source: event for event in events}
		self.assertEqual(
			set(by_source),
			{
				"About",
				"Account",
				"Account Balance",
				"Account Label",
				"Accounts",
				"Accounts Setup",
				"Asset",
				"Balance",
				"Custom Account Label",
				"Effective Account Type",
				"Financial reports",
				"Ledger",
				"Liability",
				"Overview",
				"Reports",
				"Setup",
				" Ledger account ",
			},
		)
		self.assertEqual(extraction.categories, RUNTIME_METADATA_CATEGORIES)
		self.assertEqual(
			by_source["Custom Account Label"].stable_locator,
			"custom_field:Account:custom_account_label:label",
		)
		self.assertEqual(by_source["Effective Account Type"].extractor, "property_setter")
		self.assertEqual(
			by_source["Effective Account Type"].source_location,
			"metadata/property_setter/Account-account_type-label",
		)
		self.assertEqual(by_source["Balance"].context, "Column of report 'Account Balance'")
		self.assertIsNone(by_source["Asset"].stable_locator)
		self.assertFalse(any(event.extractor == "workflow" for event in events))
		self.assertTrue(all(event.origin == "runtime" for event in events))
		self.assertTrue(all("test_site" not in event.source_location for event in events))

	def test_runtime_metadata_signature_rejects_a_spoofed_standard_record(self):
		metadata = {category: {category: []} for category in METADATA_SIGNATURE_CATEGORIES}
		expected = {
			category: _metadata_digest(metadata[category]) for category in METADATA_SIGNATURE_CATEGORIES
		}
		with patch("frappe_lt.runtime_extraction.collect_standard_metadata", return_value=metadata):
			self.assertIs(verify_standard_metadata(object(), expected), metadata)

		spoofed = deepcopy(metadata)
		spoofed["page"]["page"].append({"name": "spoofed", "title": "Spoofed", "module": "Core"})

		with (
			patch("frappe_lt.runtime_extraction.collect_standard_metadata", return_value=spoofed),
			self.assertRaisesRegex(ValueError, "page.*computed"),
		):
			verify_standard_metadata(object(), expected)

	def test_fresh_post_install_navbar_signature_is_required(self):
		fresh_labels = [
			"About",
			"Documentation",
			"Frappe School",
			"Frappe Support",
			"Keyboard Shortcuts",
			"Report an Issue",
			"System Health",
			"User Forum",
		]
		stale_labels = [
			"About",
			"Delete Demo Data",
			"Frappe Support",
			"Keyboard Shortcuts",
			"System Health",
		]
		metadata = {category: {category: []} for category in METADATA_SIGNATURE_CATEGORIES}
		metadata["navbar"] = {"Navbar Item": [{"item_label": label} for label in fresh_labels]}
		expected = {
			category: _metadata_digest(metadata[category]) for category in METADATA_SIGNATURE_CATEGORIES
		}
		self.assertEqual(
			expected["navbar"],
			"cc77f1af258ebbb42d2c9cc0fe13541bae0e11426611089bc95317f6c01f2946",
		)
		expected["navbar"] = load_compatibility()["runtime_metadata_sha256"]["navbar"]
		_validate_metadata_signatures(metadata, expected)

		metadata["navbar"] = {"Navbar Item": [{"item_label": label} for label in stale_labels]}
		self.assertEqual(
			_metadata_digest(metadata["navbar"]),
			"22953e8a5717671cf6758317fa97cdeb7f330c7764923d60e9ac0c318d9c1d12",
		)
		with self.assertRaisesRegex(ValueError, "navbar.*computed"):
			_validate_metadata_signatures(metadata, expected)

	def test_runtime_rejects_custom_metadata_before_extracting(self):
		class FakeFrappe:
			class local:
				site = "test_site"

			@staticmethod
			def get_installed_apps():
				return ["frappe", "erpnext"]

			@staticmethod
			def get_all(doctype, **kwargs):
				return [{"name": "CUSTOM"}] if doctype == "Custom Field" else []

		with self.assertRaisesRegex(ValueError, "Custom Field"):
			extract_runtime(FakeFrappe(), {})

	def test_runtime_rejects_workflow_without_a_trustworthy_upstream_identity(self):
		class FakeFrappe:
			class local:
				site = "test_site"

			@staticmethod
			def get_installed_apps():
				return ["frappe", "erpnext"]

			@staticmethod
			def get_all(doctype, **kwargs):
				return [{"name": "User Approval"}] if doctype == "Workflow" else []

		with self.assertRaisesRegex(ValueError, "local/custom metadata in Workflow"):
			extract_runtime(FakeFrappe(), {})


class ArtifactWriteTest(TestCase):
	def test_quality_bundle_copies_authenticated_registered_candidates_and_manifests(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			inventory_digest = "a" * 64
			candidate = canonical_json({"schema_version": 1, "entries": []})
			segment = canonical_json({"schema_version": 1, "keys": []})
			candidate_name = "catalog_candidates/nested/item-smoke.json"
			segment_name = "catalog_segments/nested/item-smoke.json"
			for name, content in ((candidate_name, candidate), (segment_name, segment)):
				path = root / name
				path.parent.mkdir(parents=True, exist_ok=True)
				path.write_bytes(content)
			registry = canonical_json(
				{
					"schema_version": 1,
					"inventory_digest": inventory_digest,
					"candidates": [
						{
							"name": "item-smoke",
							"candidate": candidate_name,
							"candidate_sha256": hashlib.sha256(candidate).hexdigest(),
							"manifest": segment_name,
							"manifest_sha256": hashlib.sha256(segment).hexdigest(),
						}
					],
				}
			)
			(root / "catalog_segments.json").write_bytes(registry)
			manifest = {
				"quality_gate": {
					"artifact_sha256": {"catalog_segments.json": hashlib.sha256(registry).hexdigest()}
				}
			}

			artifacts = _quality_artifacts_for_inventory(manifest, inventory_digest, root)
			self.assertEqual(
				artifacts,
				{
					"catalog_segments.json": registry,
					candidate_name: candidate,
					segment_name: segment,
				},
			)
			for output_name in ("first", "second"):
				write_artifacts(
					root / output_name,
					{**artifacts, "compatibility.json": b"manifest\n"},
				)
			for name, content in artifacts.items():
				self.assertEqual((root / "first" / name).read_bytes(), content)
				self.assertEqual((root / "second" / name).read_bytes(), content)

			(root / candidate_name).write_bytes(b"tampered\n")
			with self.assertRaisesRegex(ValueError, "candidate digest mismatch"):
				_quality_artifacts_for_inventory(manifest, inventory_digest, root)

	def test_stale_quality_bundle_is_rejected_before_inventory_publication(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			content = canonical_json({"schema_version": 1, "inventory_digest": "a" * 64})
			(root / "catalog_segments.json").write_bytes(content)
			manifest = {
				"quality_gate": {
					"artifact_sha256": {"catalog_segments.json": hashlib.sha256(content).hexdigest()}
				}
			}
			with self.assertRaisesRegex(ValueError, "newly generated Release Inventory"):
				_quality_artifacts_for_inventory(manifest, "b" * 64, root)

	def test_manifest_authenticates_every_owned_nonmanifest_artifact(self):
		manifest = verify_owned_artifacts()
		self.assertEqual(manifest["inventory_digest"], manifest["artifact_sha256"]["release_inventory.json"])
		with TemporaryDirectory() as directory:
			root = Path(directory)
			for name in manifest["artifact_sha256"]:
				(root / name).write_bytes((Path(__file__).parents[1] / name).read_bytes())
			(root / "compatibility.json").write_bytes(canonical_json(manifest))
			(root / "inventory_report.md").write_text("tampered\n", encoding="utf-8")
			with self.assertRaisesRegex(ValueError, "artifact digest mismatch"):
				verify_owned_artifacts(root / "compatibility.json")

	def test_tool_versions_are_checked_by_the_inventory_authority(self):
		manifest = load_compatibility()
		self.assertEqual(
			validate_tool_versions(manifest, (3, 14), "2.16.0"),
			{"babel": "2.16.0", "python": "3.14"},
		)
		with self.assertRaisesRegex(ValueError, "Python must be"):
			validate_tool_versions(manifest, (3, 13), "2.16.0")
		with self.assertRaisesRegex(ValueError, "Babel must be"):
			validate_tool_versions(manifest, (3, 14), "2.17.0")

	def test_manifest_is_replaced_last_and_injected_failure_never_commits_it(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			(root / "compatibility.json").write_text("old manifest\n", encoding="utf-8")
			(root / "release_inventory.json").write_text("old inventory\n", encoding="utf-8")
			replaced = []

			def fail_after_inventory(source, target):
				replaced.append(Path(target).name)
				Path(source).replace(target)
				if Path(target).name == "release_inventory.json":
					raise OSError("injected replace failure")

			with self.assertRaisesRegex(OSError, "injected"):
				write_artifacts(
					root,
					{
						"compatibility.json": b"new manifest\n",
						"inventory_report.json": b"new report\n",
						"release_inventory.json": b"new inventory\n",
					},
					replace=fail_after_inventory,
				)

			self.assertNotIn("compatibility.json", replaced)
			self.assertEqual((root / "compatibility.json").read_text(encoding="utf-8"), "old manifest\n")
			self.assertEqual((root / "release_inventory.json").read_text(encoding="utf-8"), "new inventory\n")
			self.assertFalse(list(root.glob(".*.tmp")))

	def test_artifact_writer_does_not_follow_predictable_temp_symlink(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			victim = root / "victim"
			victim.write_text("safe", encoding="utf-8")
			(root / "release_inventory.json.tmp").symlink_to(victim)

			write_artifacts(
				root,
				{
					"compatibility.json": b"manifest\n",
					"release_inventory.json": b"inventory\n",
				},
			)

			self.assertEqual(victim.read_text(encoding="utf-8"), "safe")
			self.assertEqual((root / "release_inventory.json").read_bytes(), b"inventory\n")
