import hashlib
import json
import os
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from frappe_lt import catalog_quality
from frappe_lt.catalog_partition import build_partition_artifacts
from frappe_lt.catalog_quality import _LiteralIndex, _tokens, run
from frappe_lt.inventory import canonical_json
from frappe_lt.po import compile_po
from frappe_lt.review_evidence import review_run_id


def _write_json(path, value):
	content = canonical_json(value)
	path.write_bytes(content)
	return hashlib.sha256(content).hexdigest()


class CatalogQualityGateTest(TestCase):
	@staticmethod
	def _compile_candidate(_po_path, workspace):
		mo_path = workspace / "sites" / "assets" / "locale" / "lt" / "LC_MESSAGES" / "frappe_lt.mo"
		mo_path.parent.mkdir(parents=True, exist_ok=True)
		mo_path.write_bytes(b"compiled")

	def _replace_quality_artifact(self, compatibility_path, name, value):
		compatibility_path = Path(compatibility_path)
		compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
		value.setdefault("inventory_digest", compatibility["inventory_digest"])
		digest = _write_json(compatibility_path.parent / name, value)
		compatibility["quality_gate"]["artifact_sha256"][name] = digest
		_write_json(compatibility_path, compatibility)

	def _replace_owned_artifact(self, compatibility_path, name, value):
		compatibility_path = Path(compatibility_path)
		compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
		compatibility["artifact_sha256"][name] = _write_json(compatibility_path.parent / name, value)
		_write_json(compatibility_path, compatibility)

	def _fixture(self, root, entries, candidate_entries, *, segment_keys=None):
		root = Path(root)
		selected = (
			{(item["key"]["source"], item["key"]["context"]) for item in segment_keys}
			if segment_keys is not None
			else {(entry["key"]["source"], entry["key"]["context"]) for entry in entries}
		)
		inventory_entries = []
		for entry in entries:
			entry = json.loads(json.dumps(entry))
			key = (entry["key"]["source"], entry["key"]["context"])
			app = "erpnext" if key in selected else "frappe"
			entry["apps"] = [app]
			entry["source_locations"] = [
				{
					"app": app,
					"extractor": "extract_babel_python",
					"line": 1,
					"origin": "source",
					"path": f"{app}/{'stock' if app == 'erpnext' else 'core'}/fixture.py",
				}
			]
			inventory_entries.append(entry)
		inventory = {"schema_version": 1, "entries": inventory_entries}
		owned_artifacts = {
			"inventory_report.json": canonical_json({"schema_version": 1}),
			"inventory_report.md": b"# Test report\n",
			"provenance.json": canonical_json(
				{
					"schema_version": 1,
					"entries": [{"key": entry["key"], "status": "missing"} for entry in inventory_entries],
				}
			),
			"release_inventory.json": canonical_json(inventory),
		}
		for name, content in owned_artifacts.items():
			(root / name).write_bytes(content)
		inventory_digest = hashlib.sha256(owned_artifacts["release_inventory.json"]).hexdigest()
		prepared_entries = []
		for entry in candidate_entries:
			entry = json.loads(json.dumps(entry))
			entry["provenance"] = {
				"origin": "new_ai",
				"review": {
					"agent": "test-agent",
					"explanation": None,
					"model": "test/model",
					"reason": "new_translation",
					"run_id": "",
					"status": "reviewed",
				},
			}
			entry["provenance"]["review"]["run_id"] = review_run_id(entry)
			prepared_entries.append(entry)
		ownership_overrides = {
			"classifier_schema_version": 1,
			"entries": [],
			"inventory_digest": inventory_digest,
			"schema_version": 1,
		}
		partition_artifacts = build_partition_artifacts(inventory, inventory_digest, ownership_overrides)
		for name, content in partition_artifacts.items():
			path = root / name
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes(content)
		manifest_name = "catalog_segments/erpnext-operations.json"
		manifest_digest = hashlib.sha256(partition_artifacts[manifest_name]).hexdigest()
		candidate = {
			"schema_version": 1,
			"review_evidence_schema_version": 1,
			"inventory_digest": inventory_digest,
			"manifest_sha256": manifest_digest,
			"segment_id": "erpnext-operations",
			"entries": prepared_entries,
		}
		candidate_path = root / "catalog_candidates" / "test.json"
		candidate_path.parent.mkdir(parents=True, exist_ok=True)
		candidate_digest = _write_json(candidate_path, candidate)
		registry = {
			"schema_version": 1,
			"inventory_digest": inventory_digest,
			"candidate_directory": "catalog_candidates",
			"candidates": [
				{
					"name": "test",
					"candidate": "catalog_candidates/test.json",
					"candidate_sha256": candidate_digest,
					"manifest": manifest_name,
					"manifest_sha256": manifest_digest,
					"segment_id": "erpnext-operations",
				}
			],
		}
		quality_artifacts = {
			"catalog_partition.json": hashlib.sha256(
				partition_artifacts["catalog_partition.json"]
			).hexdigest(),
			"catalog_segment_ownership_overrides.json": hashlib.sha256(
				partition_artifacts["catalog_segment_ownership_overrides.json"]
			).hexdigest(),
			"catalog_segments.json": _write_json(root / "catalog_segments.json", registry),
			"translation_exceptions.json": _write_json(
				root / "translation_exceptions.json",
				{"schema_version": 1, "inventory_digest": inventory_digest, "entries": []},
			),
			"collision_resolutions.json": _write_json(
				root / "collision_resolutions.json",
				{"schema_version": 1, "inventory_digest": inventory_digest, "entries": []},
			),
			"glossary_selectors.json": _write_json(
				root / "glossary_selectors.json",
				{"schema_version": 1, "inventory_digest": inventory_digest, "entries": []},
			),
		}
		compatibility = json.loads(
			Path(__file__).parents[1].joinpath("compatibility.json").read_text(encoding="utf-8")
		)
		compatibility["inventory_digest"] = inventory_digest
		compatibility["artifact_sha256"] = {
			name: hashlib.sha256(content).hexdigest() for name, content in owned_artifacts.items()
		}
		compatibility["quality_gate"] = {
			"schema_version": 1,
			"artifact_sha256": quality_artifacts,
		}
		_write_json(root / "compatibility.json", compatibility)
		return root / "compatibility.json"

	def test_schema_two_manifest_missing_owned_artifacts_fails_before_semantic_checks(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility_path = self._fixture(root, [], [])
			compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
			for name in ("provenance.json", "inventory_report.json", "inventory_report.md"):
				compatibility["artifact_sha256"].pop(name)
			_write_json(compatibility_path, compatibility)

			with patch(
				"frappe_lt.catalog_quality._validate_inventory",
				side_effect=AssertionError("semantic validation must not run"),
			):
				result = run(
					"test",
					root / "candidate.po",
					root / "report.json",
					compatibility_path=compatibility_path,
				)

			self.assertEqual(result["exit_code"], 2)
			self.assertIn("artifact_sha256 must authenticate exactly", result["errors"][0]["detail"])

	def test_trusted_whole_inventory_candidate_is_published_after_compile(self):
		entry = {
			"key": {"source": "Item {0}", "context": None},
			"source_digest": "a" * 64,
			"source_locations": [{"app": "erpnext", "path": "item.py", "line": 1}],
			"stable_locators": ["erpnext:source:item.py:python:1"],
		}
		translation = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Prekė {0}",
			"flags": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [translation])
			compiled = []
			output = root / "approved" / "candidate.po"
			report = root / "reports" / "quality.json"

			def compile_candidate(po_path, workspace):
				compiled.append((po_path.read_bytes(), workspace))
				self._compile_candidate(po_path, workspace)

			with patch("frappe_lt.catalog_quality.compile_po", wraps=compile_po) as adapter:
				result = run(
					"test",
					output,
					report,
					compatibility_path=compatibility,
					compile_candidate=compile_candidate,
				)

			self.assertEqual(result["exit_code"], 0)
			self.assertEqual(result["schema_version"], 2)
			self.assertNotIn("duration_seconds", result)
			self.assertEqual(
				result["summary"]["translation_coverage"],
				{"covered": 1, "total": 1},
			)
			self.assertEqual(result["summary"]["reason_counts"]["new_translation"], 1)
			adapter.assert_called_once()
			self.assertEqual(len(compiled), 1)
			self.assertNotEqual(compiled[0][0], b"")
			self.assertTrue(output.is_file())
			self.assertEqual(report.read_bytes(), canonical_json(result))
			self.assertIn('msgid "Item {0}"', output.read_text(encoding="utf-8"))
			self.assertIn('msgstr "Prekė {0}"', output.read_text(encoding="utf-8"))

	def test_candidate_review_evidence_must_match_authenticated_release_provenance(self):
		entry = {
			"key": {"context": None, "source": "Item"},
			"source_digest": "a" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		cases = (
			(
				"accepted_as_is",
				"inherited_v15",
				"Prekė",
				{"key": entry["key"], "origin": "new_ai", "status": "translated", "translation": "Prekė"},
				None,
			),
			(
				"new_translation",
				"new_ai",
				"Prekė",
				{
					"key": entry["key"],
					"origin": "inherited_v15",
					"status": "translated",
					"translation": "Prekė",
				},
				None,
			),
			(
				"grammar_correction",
				"corrected_inherited",
				"Prekė",
				{
					"key": entry["key"],
					"origin": "inherited_v15",
					"status": "translated",
					"translation": "Prekė",
				},
				"Corrected grammar.",
			),
			(
				"approved_translation_exception",
				"approved_exception",
				"Item",
				{"key": entry["key"], "status": "missing"},
				"Reviewed technical identifier.",
			),
		)
		for reason, origin, translation, release_record, explanation in cases:
			with self.subTest(reason=reason), TemporaryDirectory() as directory:
				root = Path(directory)
				compatibility = self._fixture(
					root,
					[entry],
					[
						{
							"flags": [],
							"key": entry["key"],
							"source_digest": entry["source_digest"],
							"translation": translation,
						}
					],
				)
				candidate_path = root / "catalog_candidates" / "test.json"
				candidate = json.loads(candidate_path.read_bytes())
				candidate_entry = candidate["entries"][0]
				candidate_entry["provenance"] = {
					"origin": origin,
					"review": {
						"agent": "test-agent",
						"explanation": explanation,
						"model": "test/model",
						"reason": reason,
						"run_id": "",
						"status": "reviewed",
					},
				}
				candidate_entry["provenance"]["review"]["run_id"] = review_run_id(candidate_entry)
				registry = json.loads(root.joinpath("catalog_segments.json").read_bytes())
				registry["candidates"][0]["candidate_sha256"] = _write_json(candidate_path, candidate)
				self._replace_quality_artifact(compatibility, "catalog_segments.json", registry)
				self._replace_owned_artifact(
					compatibility,
					"provenance.json",
					{"entries": [release_record], "schema_version": 1},
				)
				if reason == "approved_translation_exception":
					self._replace_quality_artifact(
						compatibility,
						"translation_exceptions.json",
						{
							"entries": [
								{
									"key": entry["key"],
									"review": explanation,
									"reviewed": True,
									"source_digest": entry["source_digest"],
								}
							],
							"schema_version": 1,
						},
					)

				result = run(
					"test",
					root / "candidate.po",
					root / "report.json",
					compatibility_path=compatibility,
					compile_candidate=lambda *_args: self.fail("must fail at trust boundary"),
				)

				self.assertEqual(result["exit_code"], 2)
				self.assertIn("authenticated release provenance", result["errors"][0]["detail"])

	def test_registered_segment_checks_exact_selected_coverage(self):
		entries = [
			{
				"key": {"source": source, "context": None},
				"source_digest": digest * 64,
				"source_locations": [],
				"stable_locators": [],
			}
			for source, digest in (("Item", "a"), ("Customer", "b"))
		]
		candidate_entry = {
			"key": entries[1]["key"],
			"source_digest": entries[1]["source_digest"],
			"translation": "Klientas",
			"flags": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(
				root,
				entries,
				[candidate_entry],
				segment_keys=[{"key": entries[1]["key"], "source_digest": entries[1]["source_digest"]}],
			)

			result = run(
				"test",
				root / "candidate.po",
				root / "report.json",
				compatibility_path=compatibility,
				compile_candidate=self._compile_candidate,
			)

			self.assertEqual(result["exit_code"], 0)
			self.assertEqual(result["summary"]["keys"], 1)

	def test_validated_review_evidence_drives_coverage_reason_correction_and_exception_totals(self):
		entries = [
			{
				"key": {"context": None, "source": source},
				"source_digest": digest * 64,
				"source_locations": [],
				"stable_locators": [],
			}
			for source, digest in (("Accepted", "a"), ("Grammar", "b"), ("API", "c"))
		]
		candidates = [
			{
				"flags": [],
				"key": entry["key"],
				"source_digest": entry["source_digest"],
				"translation": translation,
			}
			for entry, translation in zip(entries, ("Priimta", "Gramatika", "API"), strict=True)
		]
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, entries, candidates)
			candidate_path = root / "catalog_candidates" / "test.json"
			candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
			for record, origin, reason, explanation in zip(
				candidate["entries"],
				("inherited_v15", "corrected_inherited", "approved_exception"),
				("accepted_as_is", "grammar_correction", "approved_translation_exception"),
				(None, "Corrected grammar.", "Reviewed technical identifier."),
				strict=True,
			):
				record["provenance"] = {
					"origin": origin,
					"review": {
						"agent": "test-agent",
						"explanation": explanation,
						"model": "test/model",
						"reason": reason,
						"run_id": "",
						"status": "reviewed",
					},
				}
				record["provenance"]["review"]["run_id"] = review_run_id(record)
			registry = json.loads((root / "catalog_segments.json").read_text(encoding="utf-8"))
			registry["candidates"][0]["candidate_sha256"] = _write_json(candidate_path, candidate)
			self._replace_quality_artifact(compatibility, "catalog_segments.json", registry)
			self._replace_quality_artifact(
				compatibility,
				"translation_exceptions.json",
				{
					"schema_version": 1,
					"entries": [
						{
							"key": entries[2]["key"],
							"review": "Reviewed technical identifier.",
							"reviewed": True,
							"source_digest": entries[2]["source_digest"],
						}
					],
				},
			)
			self._replace_owned_artifact(
				compatibility,
				"provenance.json",
				{
					"entries": [
						{
							"key": entries[0]["key"],
							"origin": "inherited_v15",
							"status": "translated",
							"translation": candidates[0]["translation"],
						},
						{
							"key": entries[1]["key"],
							"origin": "inherited_v15",
							"status": "translated",
							"translation": "Sena gramatika",
						},
						{
							"exception": "Reviewed technical identifier.",
							"key": entries[2]["key"],
							"origin": "approved_exception",
							"status": "excepted",
						},
					],
					"schema_version": 1,
				},
			)

			result = run(
				"test",
				root / "candidate.po",
				root / "report.json",
				compatibility_path=compatibility,
				compile_candidate=self._compile_candidate,
			)

			self.assertEqual(result["exit_code"], 0)
			self.assertEqual(result["summary"]["translation_coverage"], {"covered": 3, "total": 3})
			self.assertEqual(sum(result["summary"]["reason_counts"].values()), 3)
			self.assertEqual(result["summary"]["corrected_inherited"], 1)
			self.assertEqual(result["summary"]["translation_exceptions"], 1)
			self.assertEqual(root.joinpath("report.json").read_bytes(), canonical_json(result))

	def test_quality_errors_are_complete_sorted_and_do_not_replace_candidate(self):
		entries = [
			{
				"key": {"source": source, "context": context},
				"source_digest": digest * 64,
				"source_locations": [{"app": "frappe", "path": f"{digest}.py", "line": 1}],
				"stable_locators": [],
			}
			for source, context, digest in (("Alpha", None, "a"), ("Beta", "button", "b"))
		]
		candidate_entries = [
			{
				"key": entries[0]["key"],
				"source_digest": entries[0]["source_digest"],
				"translation": "",
				"flags": ["fuzzy"],
			},
			{
				"key": entries[0]["key"],
				"source_digest": entries[0]["source_digest"],
				"translation": "Kita",
				"flags": [],
			},
			{
				"key": {"source": "Extra", "context": None},
				"source_digest": "c" * 64,
				"translation": "Papildomas",
				"flags": [],
			},
		]
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, entries, candidate_entries)
			output = root / "candidate.po"
			output.write_bytes(b"last-approved")
			results = []
			for index in range(2):
				result = run(
					"test",
					output,
					root / f"report-{index}.json",
					compatibility_path=compatibility,
					compile_candidate=lambda _po, _workspace: self.fail("must not compile"),
				)
				results.append(result)

			self.assertEqual(results[0]["exit_code"], 1)
			self.assertEqual(results[0], results[1])
			self.assertEqual(
				[error["code"] for error in results[0]["errors"]],
				[
					"CONFLICTING_TRANSLATION",
					"EMPTY_TRANSLATION",
					"FUZZY_TRANSLATION",
					"MISSING_TRANSLATION_KEY",
					"EXTRA_TRANSLATION_KEY",
				],
			)
			self.assertEqual(output.read_bytes(), b"last-approved")
			self.assertEqual(
				results[0]["summary"]["translation_coverage"],
				{"covered": 1, "total": 2},
			)
			self.assertEqual(sum(results[0]["summary"]["reason_counts"].values()), 3)

	def test_repeated_identical_key_has_duplicate_status_distinct_from_conflict(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "7" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Prekė",
			"flags": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate, candidate])

			result = run(
				"test",
				root / "candidate.po",
				root / "report.json",
				compatibility_path=compatibility,
				compile_candidate=self._compile_candidate,
			)

			self.assertEqual([error["code"] for error in result["errors"]], ["DUPLICATE_TRANSLATION_KEY"])
			self.assertEqual(result["summary"]["translation_coverage"], {"covered": 1, "total": 1})
			self.assertEqual(sum(result["summary"]["reason_counts"].values()), 2)

	def test_preserved_token_families_precedence_escaping_and_unknown_syntax(self):
		source = (
			"Open https://example.test/a?q=1 for %(user)s %s {0} {name} ${value} "
			"{{ user }} {% if ok %} $code %% and 50% or $ 5 { words } "
			r"\%(escaped)s \%d \{escaped} \${escaped} \{{ escaped }} \$escaped \https://escaped.test $$"
		)
		valid = (
			"Atverkite https://example.test/a?q=1 : %(user)s %s {0} {name} ${value} "
			"{{ user }} {% if ok %} $code %% ir 50% arba $ 5 { words } "
			r"\%(escaped)s \%d \{escaped} \${escaped} \{{ escaped }} \$escaped \https://escaped.test $$"
		)
		mutations = {
			"url": valid.replace("q=1", "q=2"),
			"python": valid.replace("%(user)s", "%(naudotojas)s"),
			"printf": valid.replace(" %s ", " %d "),
			"brace": valid.replace("{name}", "{vardas}"),
			"javascript": valid.replace("${value}", "${reiksme}"),
			"jinja": valid.replace("{{ user }}", "{{ naudotojas }}"),
			"dollar": valid.replace("$code", "$kodas"),
			"escaped": valid.replace("%%", "%"),
			"unknown": valid + " ${broken",
			"malformed-python": valid + " %(broken",
			"malformed-printf": valid + " %q",
			"malformed-brace": valid + " {broken",
			"malformed-jinja": valid + " {{ broken",
			"malformed-dollar": valid + " $1",
			"lone-closing-brace": valid + " }",
			"residual-closing-brace": valid + " name}",
		}
		entry = {
			"key": {"source": source, "context": None},
			"source_digest": "d" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			for name, translation in {"valid": valid, **mutations}.items():
				case = root / name
				case.mkdir()
				compatibility = self._fixture(
					case,
					[entry],
					[
						{
							"key": entry["key"],
							"source_digest": entry["source_digest"],
							"translation": translation,
							"flags": [],
						}
					],
				)
				result = run(
					"test",
					case / "candidate.po",
					case / "report.json",
					compatibility_path=compatibility,
					compile_candidate=self._compile_candidate,
				)
				if name == "valid":
					self.assertEqual(result["exit_code"], 0)
				else:
					self.assertEqual(result["exit_code"], 1, name)
					self.assertEqual(
						result["errors"][0]["code"],
						"UNKNOWN_TOKEN_SYNTAX"
						if name in {"unknown", "lone-closing-brace", "residual-closing-brace"}
						or name.startswith("malformed-")
						else "PRESERVED_TOKEN_MISMATCH",
						name,
					)

	def test_unmatched_token_delimiters_and_backslash_escaped_tokens(self):
		quoted_literal, quoted_literal_unknown = _tokens("Use the '{{' separator")
		self.assertFalse(quoted_literal_unknown)
		self.assertEqual(quoted_literal, Counter({("literal", "{{"): 1}))
		self.assertEqual(_tokens("Naudokite „{{“ skirtuką"), (quoted_literal, False))

		for prose in ("Use { words } with 50% or $ 5 in prose", "{ words }"):
			ordinary_tokens, ordinary_unknown = _tokens(prose)
			self.assertFalse(ordinary_unknown)
			self.assertEqual(ordinary_tokens, Counter())
		for malformed in ("{", "{ words", "}", "name}", "%}", "#}", "${broken", "%(broken"):
			with self.subTest(malformed=malformed):
				self.assertTrue(_tokens(malformed)[1])

		escaped = r"\{name} \${value} \%(user)s \%s \$code \https://example.test \}"
		escaped_tokens, escaped_unknown = _tokens(escaped)
		self.assertFalse(escaped_unknown)
		self.assertNotEqual(escaped_tokens, _tokens(escaped.replace(r"\{name}", r"\{vardas}"))[0])

	def test_bare_url_prefixes_are_preserved_tokens(self):
		source = "URL must start with http:// or https://"
		tokens, unknown = _tokens(source)
		self.assertFalse(unknown)
		self.assertEqual(tokens, Counter({("url", "http://"): 1, ("url", "https://"): 1}))
		self.assertNotEqual(_tokens("URL turi prasidėti http:// arba http://")[0], tokens)
		self.assertEqual(
			_tokens("Nenaudokite „http://“")[0],
			_tokens('Do not use "http://"')[0],
		)
		for malformed in ("ftp://", "mailto:"):
			with self.subTest(malformed=malformed):
				self.assertTrue(_tokens(malformed)[1])

	def test_quoted_naming_series_delimiters_are_exact_literals(self):
		source = (
			"Special Characters except '-', '#', '.', '/', '{{' and '}}' not allowed in naming series {0}"
		)
		tokens, unknown = _tokens(source)
		self.assertFalse(unknown)
		self.assertEqual(tokens[("literal", "{{")], 1)
		self.assertEqual(tokens[("literal", "}}")], 1)
		self.assertEqual(tokens[("brace", "{0}")], 1)
		self.assertNotEqual(_tokens(source.replace("'}}'", "'{{'"))[0], tokens)
		self.assertTrue(_tokens(source.replace("'}}'", "}}"))[1])

	def test_percent_before_sentence_period_is_prose(self):
		for source in ("Discount cannot be greater than 100%.", "Widths can be set in px or %."):
			with self.subTest(source=source):
				self.assertEqual(_tokens(source), (Counter(), False))
		for malformed in ("%.*", "%.2q", "%.z"):
			with self.subTest(malformed=malformed):
				self.assertTrue(_tokens(malformed)[1])

	def test_nested_fieldname_example_is_one_exact_preserved_literal(self):
		source = "{{{0}}} is not a valid fieldname pattern. It should be {{field_name}}."
		tokens, unknown = _tokens(source)
		self.assertFalse(unknown)
		self.assertEqual(tokens, Counter({("literal", "{{{0}}}"): 1, ("jinja", "{{field_name}}"): 1}))
		self.assertNotEqual(_tokens(source.replace("{{{0}}}", "{{{1}}}"))[0], tokens)
		for malformed in ("{{{0}}", "{{{0}}}}", "{{{name}}}"):
			with self.subTest(malformed=malformed):
				self.assertTrue(_tokens(malformed)[1])

	def test_javascript_conditional_expression_preserves_exact_text(self):
		source = '{0} ${skip_list ? "" : type}'
		tokens, unknown = _tokens(source)
		self.assertFalse(unknown)
		self.assertEqual(
			tokens, Counter({("brace", "{0}"): 1, ("javascript", '${skip_list ? "" : type}'): 1})
		)
		self.assertNotEqual(tokens, _tokens('{0} ${skip_list ? "" : other}')[0])
		self.assertTrue(_tokens('{0} ${skip_list ? "" : type')[1])

	def test_frozen_source_examples_pass_gate_and_changed_literals_do_not(self):
		pairs = (
			("URL must start with http:// or https://", "URL turi prasidėti http:// arba https://"),
			(
				"Naming series '{0}' for DocType '{1}' does not contain standard '.' or '{{' separator. Using fallback extraction.",
				"Dokumento tipo '{1}' numeravimo serijoje '{0}' nėra įprasto '.' arba '{{' skirtuko. Naudojamas atsarginis būdas.",
			),
			(
				"Special Characters except '-', '#', '.', '/', '{{' and '}}' not allowed in naming series {0}",
				"Numeravimo serijoje {0} leidžiami tik '-', '#', '.', '/', '{{' ir '}}' specialieji ženklai",
			),
			("Discount cannot be greater than 100%.", "Nuolaida negali viršyti 100%."),
			("Widths can be set in px or %.", "Plotį galima nurodyti px arba %."),
			(
				"{{{0}}} is not a valid fieldname pattern. It should be {{field_name}}.",
				"{{{0}}} nėra tinkamas lauko pavadinimo šablonas. Turi būti {{field_name}}.",
			),
		)
		entries = [
			{
				"key": {"source": source, "context": None},
				"source_digest": hashlib.sha256(source.encode()).hexdigest(),
				"source_locations": [],
				"stable_locators": [],
			}
			for source, _ in pairs
		]
		candidates = [
			{
				"key": entry["key"],
				"source_digest": entry["source_digest"],
				"translation": translation,
				"flags": [],
			}
			for entry, (_, translation) in zip(entries, pairs, strict=True)
		]
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, entries, candidates)
			result = run(
				"test",
				root / "candidate.po",
				root / "report.json",
				compatibility_path=compatibility,
				compile_candidate=self._compile_candidate,
			)
			self.assertEqual(result["errors"], [])
			self.assertEqual(result["exit_code"], 0)
			self.assertEqual(result["summary"]["keys"], len(pairs))

		for source, translation, expected_code in (
			(
				"URL must start with http:// or https://",
				"URL turi prasidėti http:// arba http://",
				"PRESERVED_TOKEN_MISMATCH",
			),
			(
				"Special Characters except '-', '#', '.', '/', '{{' and '}}' not allowed in naming series {0}",
				"Numeravimo serijoje {0} leidžiami tik '{{' ir '{{' specialieji ženklai",
				"PRESERVED_TOKEN_MISMATCH",
			),
			(
				"{{{0}}} is not a valid fieldname pattern. It should be {{field_name}}.",
				"{{{1}}} nėra tinkamas šablonas. Turi būti {{field_name}}.",
				"UNKNOWN_TOKEN_SYNTAX",
			),
		):
			with self.subTest(source=source), TemporaryDirectory() as directory:
				root = Path(directory)
				entry = next(entry for entry in entries if entry["key"]["source"] == source)
				candidate = {
					**next(c for c in candidates if c["key"] == entry["key"]),
					"translation": translation,
				}
				compatibility = self._fixture(root, [entry], [candidate])
				result = run(
					"test",
					root / "candidate.po",
					root / "report.json",
					compatibility_path=compatibility,
					compile_candidate=self._compile_candidate,
				)
				self.assertEqual(result["exit_code"], 1)
				self.assertIn(
					expected_code,
					[error["code"] for error in result["errors"]],
				)

	def test_python_and_printf_tokens_preserve_space_and_dynamic_arguments(self):
		value = "% d %*s %.*f %2$s %2$*3$s %2$.*3$f %2$*3$.*4$f %(count) d %*s"
		tokens, unknown = _tokens(value)

		self.assertFalse(unknown)
		self.assertEqual(tokens[("printf", "% d")], 1)
		self.assertEqual(tokens[("printf", "%*s")], 2)
		self.assertEqual(tokens[("printf", "%.*f")], 1)
		self.assertEqual(tokens[("printf", "%2$s")], 1)
		self.assertEqual(tokens[("printf", "%2$*3$s")], 1)
		self.assertEqual(tokens[("printf", "%2$.*3$f")], 1)
		self.assertEqual(tokens[("printf", "%2$*3$.*4$f")], 1)
		self.assertEqual(tokens[("python", "%(count) d")], 1)

		for mutation in (
			value.replace("% d", "%d", 1),
			value.replace("%*s", "%s", 1),
			value.replace("%.*f", "%.2f", 1),
			value.replace("%2$*3$.*4$f", "%2$*4$.*3$f", 1),
			value.removesuffix(" %*s"),
		):
			with self.subTest(mutation=mutation):
				mutated, malformed = _tokens(mutation)
				self.assertFalse(malformed)
				self.assertNotEqual(mutated, tokens)

	def test_malformed_dynamic_printf_tokens_are_unknown(self):
		for malformed in ("%*q", "%.*q", "%.*", "%2$*", "%2$*3s", "%2$.*f", "%(name)*s"):
			with self.subTest(malformed=malformed):
				self.assertTrue(_tokens(malformed)[1])

		for prose in ("Use 50% or more", "Keep 20% yearly", "A % sign"):
			with self.subTest(prose=prose):
				self.assertEqual(_tokens(prose), (Counter(), False))

	def test_html_equivalence_and_significant_whitespace(self):
		source = (
			'<p class="lead" title="Hello">One <strong>bold</strong> tail</p>'
			'<br><a href="https://example.test">Link</a>'
		)
		valid = (
			'<a href="https://example.test">Nuoroda</a>'
			'<p class="lead" title="Sveiki">Vienas <strong>ryškus</strong> galas</p><br>'
		)
		cases = {
			"valid": (source, valid, None),
			"equivalent-eof-fragment": (
				"<p>Following purchase orders:</p><ul>",
				"<p>Šie pirkimo užsakymai:</p><ul>",
				None,
			),
			"equivalent-eof-anchor": (
				'Read <a href="https://example.test">documentation<a>.',
				'Skaitykite <a href="https://example.test">dokumentaciją<a>.',
				None,
			),
			"matching-unclosed-div": ("<div>Source", "<div>Vertimas", "HTML_SOURCE_INVALID"),
			"matching-unclosed-em": ("<em>Source", "<em>Vertimas", "HTML_SOURCE_INVALID"),
			"wrong-container-before-unclosed-ul": (
				"<div>Source<ul>",
				"<div>Vertimas<ul>",
				"HTML_SOURCE_INVALID",
			),
			"structure": (source, valid.replace("strong", "em"), "HTML_EQUIVALENCE_MISMATCH"),
			"attribute": (source, valid.replace('class="lead"', 'class="intro"'), "HTML_ATTRIBUTE_MISMATCH"),
			"url": (source, valid.replace("example.test", "example.invalid"), "PRESERVED_TOKEN_MISMATCH"),
			"source-malformed": ("<p>Broken", "<p>Sugadinta</p>", "HTML_SOURCE_INVALID"),
			"translation-malformed": ("<p>Good</p>", "<p>Blogas", "HTML_TRANSLATION_INVALID"),
			"plain-space": ("Two  spaces", "Du tarpai", "SIGNIFICANT_WHITESPACE_MISMATCH"),
			"plain-newline": ("A\r\nB\nC", "A\nB\nC", "SIGNIFICANT_WHITESPACE_MISMATCH"),
			"html-tail": ("<b>A</b>  tail", "<b>Ą</b> galas", "SIGNIFICANT_WHITESPACE_MISMATCH"),
			"all-translatable-attributes": (
				'<img alt="Image" title="Title"><input aria-label="Name" placeholder="Value">',
				'<img alt="Vaizdas" title="Pavadinimas"><input aria-label="Vardas" placeholder="Reikšmė">',
				None,
			),
			"embedded-attribute-references": (
				'<img alt="View https://example.test/a /root"><input aria-label="Email '
				'mailto:a@example.test" placeholder="Load //cdn.test/x"><span title="Visit '
				'./dot ../up docs/page?tab=1#part #part ?page=1">X</span>',
				'<img alt="Rodyti https://example.test/a /root"><input aria-label="Rašyti '
				'mailto:a@example.test" placeholder="Įkelti //cdn.test/x"><span title="Aplankyti '
				'./dot ../up docs/page?tab=1#part #part ?page=1">Y</span>',
				None,
			),
			"embedded-rootless-reference-changed": (
				'<span title="Visit docs/page?tab=1#part">X</span>',
				'<span title="Aplankyk docs/other?tab=1#part">Y</span>',
				"HTML_ATTRIBUTE_MISMATCH",
			),
			"slash-free-attribute-prose": (
				'<span title="Read documentation today">X</span>',
				'<span title="Skaitykite dokumentaciją šiandien">Y</span>',
				None,
			),
			"embedded-attribute-reference-changed": (
				'<span title="Visit /old">X</span>',
				'<span title="Aplankyk /new">Y</span>',
				"HTML_ATTRIBUTE_MISMATCH",
			),
			"embedded-attribute-reference-multiset": (
				'<span title="Visit /same and /same">X</span>',
				'<span title="Aplankyk /same">Y</span>',
				"HTML_ATTRIBUTE_MISMATCH",
			),
			"html-exact-newlines": (
				"<b>A\r\nB\rC\nD</b>",
				"<b>Ą\nB\nC\nD</b>",
				"SIGNIFICANT_WHITESPACE_MISMATCH",
			),
			"html-branch-newlines-reordered": (
				"<p>A<b>B</b>\rC</p><section>D<i>E</i>\nF</section><aside>G<u>H</u>\r\nI</aside>",
				"<aside>Ž<u>H</u>\r\nI</aside><p>Ą<b>B</b>\rC</p><section>Č<i>E</i>\nF</section>",
				None,
			),
			"html-paired-branch-newline-changed": (
				"<p>A<b>B</b>\rC</p><section>D<i>E</i>\nF</section><aside>G<u>H</u>\r\nI</aside>",
				"<aside>Ž<u>H</u>\r\nI</aside><p>Ą<b>B</b>\nC</p><section>Č<i>E</i>\nF</section>",
				"SIGNIFICANT_WHITESPACE_MISMATCH",
			),
			"reserved-newline-sentinel": (
				"<b>A&#57344;B</b>",
				"<b>ĄB</b>",
				"HTML_SOURCE_INVALID",
			),
			"invalid-nesting": ("<p><div>Text</div></p>", "<p><div>Tekstas</div></p>", "HTML_SOURCE_INVALID"),
			"duplicate-attribute": ('<b class="a" class="b">X</b>', "<b>X</b>", "HTML_SOURCE_INVALID"),
			"duplicate-attribute-eof-different-url": (
				'<a href="https://same.test" href="https://source.test">Source',
				'<a href="https://same.test" href="https://translation.test">Vertimas',
				"HTML_SOURCE_INVALID",
			),
			"void-misuse": ("<br></br>", "<br>", "HTML_SOURCE_INVALID"),
			"unknown-entity": ("<b>&bogus;</b>", "<b>Tekstas</b>", "HTML_SOURCE_INVALID"),
			"urls": (
				'<a href="https://example.test">A</a><a href="/relative">B</a>'
				'<a href="//cdn.example.test/x">C</a><a href="mailto:a@example.test">D</a>',
				'<a href="https://example.test">Ą</a><a href="/changed">B</a>'
				'<a href="//cdn.example.test/x">C</a><a href="mailto:a@example.test">D</a>',
				"HTML_ATTRIBUTE_MISMATCH",
			),
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			for name, (case_source, translation, error_code) in cases.items():
				case = root / name
				case.mkdir()
				entry = {
					"key": {"source": case_source, "context": None},
					"source_digest": hashlib.sha256(case_source.encode()).hexdigest(),
					"source_locations": [],
					"stable_locators": [],
				}
				compatibility = self._fixture(
					case,
					[entry],
					[
						{
							"key": entry["key"],
							"source_digest": entry["source_digest"],
							"translation": translation,
							"flags": [],
						}
					],
				)
				result = run(
					"test",
					case / "candidate.po",
					case / "report.json",
					compatibility_path=compatibility,
					compile_candidate=self._compile_candidate,
				)
				if error_code is None:
					self.assertEqual(result["exit_code"], 0, name)
				else:
					self.assertIn(error_code, [error["code"] for error in result["errors"]], name)

	def test_html_newlines_preserve_syntax_attributes_and_sibling_nodes(self):
		self.assertEqual(
			catalog_quality._html_errors('<span\nclass="x">A</span>', '<span class="x">Ą</span>'),
			[],
		)
		self.assertEqual(
			catalog_quality._html_errors(
				'<span title="First\r\nSecond\rThird\nFourth">A</span>',
				'<span title="Pirmas\r\nAntras\rTrečias\nKetvirtas">Ą</span>',
			),
			[],
		)
		self.assertEqual(
			catalog_quality._html_errors(
				'<p title="A\rB">One\nTwo</p><p title="C\nD">Three\r\nFour</p>',
				'<p title="Č\nD">Trys\r\nKeturi</p><p title="Ą\rB">Vienas\nDu</p>',
			),
			[],
		)
		self.assertEqual(
			catalog_quality._html_errors(
				"<p>One\rTwo</p><p>Three\nFour</p>",
				"<p>Trys\nKeturi</p><p>Vienas\nDu</p>",
			),
			["SIGNIFICANT_WHITESPACE_MISMATCH"],
		)
		self.assertEqual(
			catalog_quality._html_errors(
				'<span title="First\rSecond">A</span>', '<span title="Pirmas\nAntras">Ą</span>'
			),
			["HTML_ATTRIBUTE_MISMATCH"],
		)

	def test_identical_translation_requires_exact_reviewed_exception(self):
		technical_identifier = '<div id=\\"technical-fragment\\"></div>'
		entry = {
			"key": {"source": technical_identifier, "context": None},
			"source_digest": "e" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": technical_identifier,
			"flags": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			candidate_path = root / "catalog_candidates" / "test.json"
			candidate_data = json.loads(candidate_path.read_text(encoding="utf-8"))
			candidate_record = candidate_data["entries"][0]
			candidate_record["provenance"] = {
				"origin": "approved_exception",
				"review": {
					"agent": "test-agent",
					"explanation": "Reviewed technical identifier.",
					"model": "test/model",
					"reason": "approved_translation_exception",
					"run_id": "",
					"status": "reviewed",
				},
			}
			candidate_record["provenance"]["review"]["run_id"] = review_run_id(candidate_record)
			registry = json.loads((root / "catalog_segments.json").read_text(encoding="utf-8"))
			registry["candidates"][0]["candidate_sha256"] = _write_json(candidate_path, candidate_data)
			self._replace_quality_artifact(compatibility, "catalog_segments.json", registry)
			self._replace_owned_artifact(
				compatibility,
				"provenance.json",
				{
					"entries": [
						{
							"exception": "Reviewed technical identifier.",
							"key": entry["key"],
							"origin": "approved_exception",
							"status": "excepted",
						}
					],
					"schema_version": 1,
				},
			)
			for name, exception, expected_exit, expected in (
				("missing", [], 1, "IDENTICAL_TRANSLATION_WITHOUT_EXCEPTION"),
				(
					"stale",
					[
						{
							"key": entry["key"],
							"source_digest": "f" * 64,
							"reviewed": True,
							"review": "review-1",
						}
					],
					2,
					"UNTRUSTED_INPUT_OR_TOOL_FAILURE",
				),
				(
					"valid",
					[
						{
							"key": entry["key"],
							"source_digest": entry["source_digest"],
							"reviewed": True,
							"review": "review-1",
						}
					],
					0,
					None,
				),
			):
				self._replace_quality_artifact(
					compatibility,
					"translation_exceptions.json",
					{"schema_version": 1, "entries": exception},
				)
				result = run(
					"test",
					root / f"{name}.po",
					root / f"{name}.json",
					compatibility_path=compatibility,
					compile_candidate=self._compile_candidate,
				)
				self.assertEqual(result["exit_code"], expected_exit)
				if expected is not None:
					self.assertIn(expected, [error["code"] for error in result["errors"]])
				if name == "missing":
					self.assertIn(
						"TRANSLATION_EXCEPTION_EVIDENCE_MISMATCH",
						[error["code"] for error in result["errors"]],
					)

	def test_reviewed_identical_exception_checks_tokens_and_waives_only_source_html(self):
		cases = (
			("unknown-token", "Broken {{ token", "Broken {{ token", "UNKNOWN_TOKEN_SYNTAX"),
			(
				"changed-malformed-html",
				'<div id=\\"source\\"></div>',
				'<div id=\\"translation\\"></div>',
				"HTML_SOURCE_INVALID",
			),
		)
		for name, source, translation, expected_error in cases:
			with self.subTest(name=name), TemporaryDirectory() as directory:
				root = Path(directory)
				entry = {
					"key": {"source": source, "context": None},
					"source_digest": hashlib.sha256(source.encode()).hexdigest(),
					"source_locations": [],
					"stable_locators": [],
				}
				candidate = {
					"key": entry["key"],
					"source_digest": entry["source_digest"],
					"translation": translation,
					"flags": [],
				}
				compatibility = self._fixture(root, [entry], [candidate])
				candidate_path = root / "catalog_candidates" / "test.json"
				candidate_data = json.loads(candidate_path.read_text(encoding="utf-8"))
				candidate_record = candidate_data["entries"][0]
				candidate_record["provenance"] = {
					"origin": "approved_exception",
					"review": {
						"agent": "test-agent",
						"explanation": "Reviewed technical identifier.",
						"model": "test/model",
						"reason": "approved_translation_exception",
						"run_id": "",
						"status": "reviewed",
					},
				}
				candidate_record["provenance"]["review"]["run_id"] = review_run_id(candidate_record)
				registry = json.loads((root / "catalog_segments.json").read_text(encoding="utf-8"))
				registry["candidates"][0]["candidate_sha256"] = _write_json(candidate_path, candidate_data)
				self._replace_quality_artifact(compatibility, "catalog_segments.json", registry)
				self._replace_quality_artifact(
					compatibility,
					"translation_exceptions.json",
					{
						"schema_version": 1,
						"entries": [
							{
								"key": entry["key"],
								"source_digest": entry["source_digest"],
								"reviewed": True,
								"review": "review-1",
							}
						],
					},
				)
				self._replace_owned_artifact(
					compatibility,
					"provenance.json",
					{
						"entries": [
							{
								"exception": "Reviewed technical identifier.",
								"key": entry["key"],
								"origin": "approved_exception",
								"status": "excepted",
							}
						],
						"schema_version": 1,
					},
				)

				result = run(
					"test",
					root / "candidate.po",
					root / "report.json",
					compatibility_path=compatibility,
					compile_candidate=self._compile_candidate,
				)

				self.assertIn(expected_error, [error["code"] for error in result["errors"]])

	def test_contextless_collision_requires_exact_reviewed_resolution(self):
		entry = {
			"context_decision": "CONTEXT.md#saskaita-account-contextless-collision",
			"key": {"source": "Account", "context": None},
			"source_digest": "1" * 64,
			"source_locations": [],
			"stable_locators": [
				"erpnext:doctype:Account:name",
				"frappe:doctype:Email Account:field:account_section:label",
			],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Sąskaita",
			"flags": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			resolution = {
				"key": entry["key"],
				"source_digest": entry["source_digest"],
				"translation": candidate["translation"],
				"stable_locators": entry["stable_locators"],
				"reviewed": True,
				"review": "CONTEXT.md#saskaita-account-contextless-collision",
			}
			for name, resolutions, expected_exit in (
				("missing", [], 1),
				("wrong-translation", [{**resolution, "translation": "Paskyra"}], 1),
				("wrong-locators", [{**resolution, "stable_locators": []}], 2),
				("valid", [resolution], 0),
			):
				self._replace_quality_artifact(
					compatibility,
					"collision_resolutions.json",
					{"schema_version": 1, "entries": resolutions},
				)
				result = run(
					"test",
					root / f"{name}.po",
					root / f"{name}.json",
					compatibility_path=compatibility,
					compile_candidate=self._compile_candidate,
				)
				self.assertEqual(result["exit_code"], expected_exit, name)
				if expected_exit == 1:
					self.assertIn(
						"UNRESOLVED_CONTEXTLESS_COLLISION",
						[error["code"] for error in result["errors"]],
					)

	def test_glossary_selector_blocks_only_exact_selection_and_reports_unselected_suspicion(self):
		item = {
			"key": {"source": "Item", "context": None},
			"source_digest": "2" * 64,
			"source_locations": [{"app": "erpnext", "path": "item.py", "line": 1}],
			"stable_locators": ["erpnext:doctype:Item:name"],
		}
		other = {
			"key": {"source": "Component", "context": None},
			"source_digest": "3" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		selector = {
			"id": "glossary.preke",
			"glossary_id": "CONTEXT.md#glossary-preke",
			"key": item["key"],
			"source_digest": item["source_digest"],
			"source": "Item",
			"context": None,
			"stable_locators": item["stable_locators"],
			"accepted_forms": ["Prekė"],
			"forbidden_forms": ["Elementas"],
			"case_sensitive": True,
			"unicode_normalization": "NFC",
			"reviewed": True,
			"review": "review-2",
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			for name, item_translation, expected_exit in (
				("valid", "Prekė", 0),
				("forbidden", "Elementas", 1),
				("wrong-case", "prekė", 1),
			):
				case = root / name
				case.mkdir()
				candidates = [
					{
						"key": item["key"],
						"source_digest": item["source_digest"],
						"translation": item_translation,
						"flags": [],
					},
					{
						"key": other["key"],
						"source_digest": other["source_digest"],
						"translation": "Elementas",
						"flags": [],
					},
				]
				compatibility = self._fixture(case, [item, other], candidates)
				self._replace_quality_artifact(
					compatibility,
					"glossary_selectors.json",
					{"schema_version": 1, "entries": [selector]},
				)
				result = run(
					"test",
					case / "candidate.po",
					case / "report.json",
					compatibility_path=compatibility,
					compile_candidate=self._compile_candidate,
				)
				self.assertEqual(result["exit_code"], expected_exit, name)
				if expected_exit:
					self.assertIn("GLOSSARY_SELECTOR_VIOLATION", [e["code"] for e in result["errors"]])
					self.assertEqual(result["errors"][0]["selector_id"], selector["id"])
				else:
					self.assertEqual(result["notices"][0]["code"], "GLOSSARY_REVIEW_SUGGESTION")
					self.assertEqual(result["notices"][0]["selector_id"], selector["id"])

	def test_suffix_heavy_glossary_index_has_linear_outputs_and_a_cumulative_match_budget(self):
		forms = {"a" * length: {("accepted", f"selector-{length}")} for length in range(1, 65)}
		index = _LiteralIndex(forms)
		self.assertEqual(sum(len(outputs) for outputs in index.outputs), len(forms))
		budget = [0]
		self.assertEqual(len(index.matches("a" * 256, budget)), len(forms))
		self.assertEqual(budget, [len(forms)])
		with patch("frappe_lt.catalog_quality.MAX_GLOSSARY_MATCHES", 1):
			cumulative_budget = [0]
			single = _LiteralIndex({"a": {("accepted", "selector")}})
			single.matches("a", cumulative_budget)
			with self.assertRaisesRegex(ValueError, "glossary matches exceed 1"):
				single.matches("a", cumulative_budget)

		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "4" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "aaa",
			"flags": [],
		}
		selectors = [
			{
				"id": f"selector-{length}",
				"glossary_id": "CONTEXT.md#glossary-preke",
				"key": entry["key"],
				"source_digest": entry["source_digest"],
				"source": "Item",
				"context": None,
				"accepted_forms": ["a" * length],
				"forbidden_forms": ["z" * length],
				"case_sensitive": True,
				"unicode_normalization": "none",
				"reviewed": True,
				"review": "budget-test",
			}
			for length in range(1, 4)
		]
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			self._replace_quality_artifact(
				compatibility,
				"glossary_selectors.json",
				{"schema_version": 1, "entries": selectors},
			)
			with patch("frappe_lt.catalog_quality.MAX_GLOSSARY_MATCHES", 2):
				result = run(
					"test",
					root / "candidate.po",
					root / "report.json",
					compatibility_path=compatibility,
					compile_candidate=self._compile_candidate,
				)
			self.assertEqual(result["exit_code"], 2)
			self.assertIn("glossary matches exceed 2", result["errors"][0]["detail"])
			self.assertEqual((root / "report.json").read_bytes(), canonical_json(result))

	def test_trust_and_tool_failures_report_exit_two_without_replacing_candidate(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "4" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Prekė",
			"flags": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			for failure in ("parse", "compile", "write", "fsync", "directory-fsync", "replace"):
				case = root / failure
				case.mkdir()
				compatibility = self._fixture(case, [entry], [candidate])
				output = case / "approved.po"
				output.write_bytes(b"last-approved")
				kwargs = {
					"po_parser": lambda _path: None,
					"compile_candidate": self._compile_candidate,
				}
				if failure == "parse":
					kwargs["po_parser"] = lambda _path: (_ for _ in ()).throw(ValueError("parse"))
				elif failure == "compile":
					kwargs["compile_candidate"] = lambda _po, _workspace: (_ for _ in ()).throw(
						RuntimeError("compile")
					)
				elif failure == "write":
					kwargs["write"] = lambda _stream, _value: (_ for _ in ()).throw(OSError("write"))
				elif failure == "fsync":
					kwargs["fsync"] = lambda _fd: (_ for _ in ()).throw(OSError("fsync"))
				elif failure == "directory-fsync":
					fsync_calls = []

					def fail_directory_fsync(_fd, calls=fsync_calls):
						calls.append(_fd)
						if len(calls) == 2:
							raise OSError("directory fsync")

					kwargs["fsync"] = fail_directory_fsync
				else:
					replace_calls = []

					def fail_replace(source, target, calls=replace_calls):
						calls.append((source, target))
						raise OSError("replace")

					kwargs["replace"] = fail_replace

				rollback_guard = (
					patch("frappe_lt.catalog_quality.os.replace") if failure == "replace" else nullcontext()
				)
				with rollback_guard as rollback_replace:
					result = run(
						"test",
						output,
						case / "report.json",
						compatibility_path=compatibility,
						**kwargs,
					)

				self.assertEqual(result["exit_code"], 2, failure)
				self.assertEqual(len(result["errors"]), 1, failure)
				self.assertEqual(output.read_bytes(), b"last-approved", failure)
				self.assertEqual((case / "report.json").read_bytes(), canonical_json(result), failure)
				if failure == "replace":
					self.assertEqual(len(replace_calls), 1)
					rollback_replace.assert_not_called()

	def test_authenticated_digest_failure_stops_before_semantic_checks(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "5" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [])
			(root / "glossary_selectors.json").write_text("{}", encoding="utf-8")

			result = run(
				"test",
				root / "candidate.po",
				root / "report.json",
				compatibility_path=compatibility,
				compile_candidate=lambda _po, _workspace: self.fail("semantic phase must not run"),
			)

			self.assertEqual(result["exit_code"], 2)
			self.assertEqual(result["errors"][0]["code"], "UNTRUSTED_INPUT_OR_TOOL_FAILURE")
			self.assertFalse((root / "candidate.po").exists())

	def test_authenticated_inventory_and_provenance_bytes_are_each_read_once_and_strictly_parsed(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "5" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"flags": [],
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Prekė",
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			read_bytes = catalog_quality._read_bytes
			reads = []

			def counted_read(path, budget=None):
				reads.append(Path(path).name)
				return read_bytes(path, budget)

			with patch("frappe_lt.catalog_quality._read_bytes", side_effect=counted_read):
				result = run(
					"test",
					root / "candidate.po",
					root / "report.json",
					compatibility_path=compatibility,
					compile_candidate=self._compile_candidate,
				)

			self.assertEqual(result["exit_code"], 0)
			self.assertEqual(reads.count("release_inventory.json"), 1)
			self.assertEqual(reads.count("provenance.json"), 1)

		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			content = b'{"entries":[],"entries":[],"schema_version":1}\n'
			root.joinpath("provenance.json").write_bytes(content)
			compatibility_data = json.loads(compatibility.read_bytes())
			compatibility_data["artifact_sha256"]["provenance.json"] = hashlib.sha256(content).hexdigest()
			_write_json(compatibility, compatibility_data)

			result = run(
				"test",
				root / "candidate.po",
				root / "report.json",
				compatibility_path=compatibility,
			)

			self.assertEqual(result["exit_code"], 2)
			self.assertIn("duplicate JSON object member", result["errors"][0]["detail"])

		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			self._replace_owned_artifact(
				compatibility,
				"provenance.json",
				{"entries": [], "schema_version": 1},
			)

			result = run(
				"test",
				root / "candidate.po",
				root / "report.json",
				compatibility_path=compatibility,
			)

			self.assertEqual(result["exit_code"], 2)
			self.assertIn("provenance is missing 1 active", result["errors"][0]["detail"])

	def test_documented_input_limits_are_enforced_before_semantic_checks(self):
		entry = {
			"key": {"source": "Long source", "context": None},
			"source_digest": "6" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Ilgas vertimas",
			"flags": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			with patch("frappe_lt.catalog_quality.MAX_STRING_BYTES", 8):
				result = run(
					"test",
					root / "candidate.po",
					root / "report.json",
					compatibility_path=compatibility,
					compile_candidate=lambda _po, _workspace: self.fail("must fail at trust boundary"),
				)

			self.assertEqual(result["exit_code"], 2)
			self.assertIn("exceeding 8 bytes", result["errors"][0]["detail"])

	def test_fail_fast_writes_one_canonical_error(self):
		entries = [
			{
				"key": {"source": source, "context": None},
				"source_digest": digest * 64,
				"source_locations": [],
				"stable_locators": [],
			}
			for source, digest in (("Alpha", "a"), ("Beta", "b"))
		]
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, entries, [])
			result = run(
				"test",
				root / "candidate.po",
				root / "report.json",
				compatibility_path=compatibility,
				compile_candidate=lambda _po, _workspace: self.fail("must not compile"),
				fail_fast=True,
			)

			self.assertEqual(result["exit_code"], 1)
			self.assertEqual(len(result["errors"]), 1)
			self.assertEqual((root / "report.json").read_bytes(), canonical_json(result))

	def test_unknown_quality_artifact_schema_is_exit_two(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "c" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [])
			self._replace_quality_artifact(
				compatibility,
				"glossary_selectors.json",
				{"schema_version": 999, "entries": []},
			)

			result = run(
				"test",
				root / "candidate.po",
				root / "report.json",
				compatibility_path=compatibility,
			)

			self.assertEqual(result["exit_code"], 2)
			self.assertIn("unsupported schema", result["errors"][0]["detail"])

	def test_unknown_inventory_candidate_review_evidence_and_segment_schemas_are_exit_two(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "c" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Prekė",
			"flags": [],
		}
		for schema, expected in (
			("inventory", "Release Inventory schema"),
			("candidate", "candidate schema"),
			("review-evidence", "candidate review evidence schema"),
			("segment", "manifest schema"),
		):
			with self.subTest(schema=schema), TemporaryDirectory() as directory:
				root = Path(directory)
				compatibility = self._fixture(
					root,
					[entry],
					[candidate],
					segment_keys=[{"key": entry["key"], "source_digest": entry["source_digest"]}],
				)
				if schema == "inventory":
					inventory = json.loads((root / "release_inventory.json").read_text(encoding="utf-8"))
					inventory["schema_version"] = 999
					inventory_digest = _write_json(root / "release_inventory.json", inventory)
					compatibility_data = json.loads(compatibility.read_text(encoding="utf-8"))
					compatibility_data["inventory_digest"] = inventory_digest
					compatibility_data["artifact_sha256"]["release_inventory.json"] = inventory_digest
					_write_json(compatibility, compatibility_data)
				elif schema in {"candidate", "review-evidence"}:
					candidate_path = root / "catalog_candidates" / "test.json"
					candidate_data = json.loads(candidate_path.read_text(encoding="utf-8"))
					field = "schema_version" if schema == "candidate" else "review_evidence_schema_version"
					candidate_data[field] = 999
					registry = json.loads((root / "catalog_segments.json").read_text(encoding="utf-8"))
					registry["candidates"][0]["candidate_sha256"] = _write_json(
						candidate_path, candidate_data
					)
					self._replace_quality_artifact(compatibility, "catalog_segments.json", registry)
				else:
					manifest_path = root / "catalog_segments" / "erpnext-operations.json"
					manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
					manifest["schema_version"] = 999
					manifest_digest = _write_json(manifest_path, manifest)
					partition = json.loads((root / "catalog_partition.json").read_text(encoding="utf-8"))
					next(record for record in partition["segments"] if record["id"] == "erpnext-operations")[
						"manifest_sha256"
					] = manifest_digest
					self._replace_quality_artifact(
						compatibility,
						"catalog_partition.json",
						partition,
					)

				result = run(
					"test",
					root / "candidate.po",
					root / "report.json",
					compatibility_path=compatibility,
				)
				self.assertEqual(result["exit_code"], 2)
				self.assertIn(expected, result["errors"][0]["detail"])

	def test_segment_registry_rejects_stale_duplicate_overlapping_missing_and_unregistered_data(self):
		entries = [
			{
				"key": {"source": source, "context": None},
				"source_digest": digest * 64,
				"source_locations": [],
				"stable_locators": [],
			}
			for source, digest in (("Item", "8"), ("Customer", "9"))
		]
		candidate = {
			"key": entries[0]["key"],
			"source_digest": entries[0]["source_digest"],
			"translation": "Prekė",
			"flags": [],
		}
		for failure in ("stale", "duplicate", "overlap", "missing", "unregistered"):
			with self.subTest(failure=failure), TemporaryDirectory() as directory:
				root = Path(directory)
				compatibility = self._fixture(
					root,
					entries,
					[candidate],
					segment_keys=[{"key": entries[0]["key"], "source_digest": entries[0]["source_digest"]}],
				)
				registry = json.loads((root / "catalog_segments.json").read_text(encoding="utf-8"))
				if failure in {"stale", "duplicate"}:
					manifest_path = root / "catalog_segments" / "erpnext-operations.json"
					manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
					if failure == "stale":
						manifest["inventory_digest"] = "0" * 64
					else:
						manifest["keys"].append(manifest["keys"][0])
					manifest_digest = _write_json(manifest_path, manifest)
					partition = json.loads((root / "catalog_partition.json").read_text(encoding="utf-8"))
					next(record for record in partition["segments"] if record["id"] == "erpnext-operations")[
						"manifest_sha256"
					] = manifest_digest
					self._replace_quality_artifact(
						compatibility,
						"catalog_partition.json",
						partition,
					)
				elif failure == "overlap":
					first_candidate = root / registry["candidates"][0]["candidate"]
					second_candidate = first_candidate.with_name("second.json")
					second_candidate.write_bytes(first_candidate.read_bytes())
					registry["candidates"].append(
						{
							**registry["candidates"][0],
							"name": "second",
							"candidate": "catalog_candidates/second.json",
						}
					)
				elif failure == "missing":
					(root / "catalog_candidates" / "test.json").unlink()
				else:
					candidate_directory = root / "catalog_candidates"
					(candidate_directory / "nested").mkdir()
					(candidate_directory / "nested" / "stray.json").write_text("{}", encoding="utf-8")
				if failure not in {"missing", "stale", "duplicate"}:
					self._replace_quality_artifact(
						compatibility,
						"catalog_segments.json",
						registry,
					)

				result = run(
					"test",
					root / "candidate.po",
					root / "report.json",
					compatibility_path=compatibility,
					compile_candidate=lambda _po, _workspace: self.fail("must fail at trust boundary"),
				)

				self.assertEqual(result["exit_code"], 2)
				if failure == "unregistered":
					self.assertIn("nested/stray.json", result["errors"][0]["detail"])

	def test_report_destination_is_validated_independently_from_candidate_destination(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "a" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Prekė",
			"flags": [],
		}
		active_po = Path(__file__).parents[1] / "locale" / "lt.po"
		active_before = active_po.read_bytes()
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			alias_target = root / "alias-target.po"
			alias_target.write_bytes(b"sentinel")
			alias = root / "alias.po"
			alias.symlink_to(alias_target)
			cases = (
				(root / "same.po", root / "same.po", False),
				(active_po, root / "active-po.json", True),
				(root / "active-report.po", active_po.parent / "quality.json", False),
				(root / "candidate.mo", root / "mo.json", True),
				(alias, root / "alias.json", True),
			)
			for output, report, writes_report in cases:
				with self.subTest(output=output, report=report):
					result = run(
						"test",
						output,
						report,
						compatibility_path=compatibility,
						compile_candidate=lambda *_args: self.fail("must reject before validation"),
					)
					self.assertEqual(result["exit_code"], 2)
					self.assertEqual(report.exists(), writes_report)
					if writes_report:
						self.assertEqual(report.read_bytes(), canonical_json(result))
			self.assertEqual(alias_target.read_bytes(), b"sentinel")
		self.assertEqual(active_po.read_bytes(), active_before)

	def test_duplicate_records_still_collect_safe_semantic_errors(self):
		entry = {
			"key": {"source": "Item {0}", "context": None},
			"source_digest": "b" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": "f" * 64,
			"translation": "",
			"flags": ["fuzzy"],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(
				root,
				[entry],
				[candidate, candidate, candidate],
			)
			result = run(
				"test",
				root / "candidate.po",
				root / "report.json",
				compatibility_path=compatibility,
				compile_candidate=self._compile_candidate,
			)
			self.assertEqual(
				[error["code"] for error in result["errors"]],
				[
					"DUPLICATE_TRANSLATION_KEY",
					"EMPTY_TRANSLATION",
					"FUZZY_TRANSLATION",
					"PRESERVED_TOKEN_MISMATCH",
					"SOURCE_DIGEST_MISMATCH",
				],
			)
			sort_keys = [tuple(error["sort_key"]) for error in result["errors"]]
			self.assertEqual(len(sort_keys), len(set(sort_keys)))

	def test_source_digest_mismatch_is_reported_for_a_single_record(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "a" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": "b" * 64,
			"translation": "Prekė",
			"flags": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			result = run(
				"test",
				root / "candidate.po",
				root / "report.json",
				compatibility_path=compatibility,
				compile_candidate=self._compile_candidate,
			)
			self.assertIn("SOURCE_DIGEST_MISMATCH", [error["code"] for error in result["errors"]])

	def test_token_and_html_parsing_happen_once_per_source_and_translation(self):
		source = "<b>Item {0}</b>"
		translation = "<b>Prekė {0}</b>"
		entries = [
			{
				"key": {"source": source, "context": context},
				"source_digest": "c" * 64,
				"source_locations": [],
				"stable_locators": [],
			}
			for context in (None, "button")
		]
		candidates = [
			{
				"key": entry["key"],
				"source_digest": entry["source_digest"],
				"translation": translation,
				"flags": [],
			}
			for entry in entries
		]
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, entries, candidates)
			parse_calls = []
			parse_fragment = catalog_quality._RecordingHTMLParser.parseFragment

			def counted_parse_fragment(parser, value, *args, **kwargs):
				parse_calls.append(value)
				return parse_fragment(parser, value, *args, **kwargs)

			with (
				patch(
					"frappe_lt.catalog_quality._tokens",
					wraps=__import__("frappe_lt.catalog_quality", fromlist=["_tokens"])._tokens,
				) as tokens,
				patch.object(
					catalog_quality._RecordingHTMLParser,
					"parseFragment",
					new=counted_parse_fragment,
				),
			):
				result = run(
					"test",
					root / "candidate.po",
					root / "report.json",
					compatibility_path=compatibility,
					compile_candidate=self._compile_candidate,
				)
			self.assertEqual(result["exit_code"], 0)
			self.assertEqual(tokens.call_count, 2)
			self.assertCountEqual(parse_calls, [source, translation])

	def test_duplicate_json_members_and_input_count_limits_are_trust_failures(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "d" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [])
			content = compatibility.read_text(encoding="utf-8")
			compatibility.write_text(content[:-2] + ',"schema_version":2}\n', encoding="utf-8")
			result = run(
				"test", root / "candidate.po", root / "report.json", compatibility_path=compatibility
			)
			self.assertEqual(result["exit_code"], 2)
			self.assertIn("duplicate JSON object member", result["errors"][0]["detail"])

		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [])
			with patch("frappe_lt.catalog_quality.MAX_TOTAL_ARTIFACT_BYTES", 1):
				result = run(
					"test", root / "candidate.po", root / "report.json", compatibility_path=compatibility
				)
			self.assertEqual(result["exit_code"], 2)
			self.assertIn("aggregate bytes", result["errors"][0]["detail"])

	def test_aggregate_normalized_glossary_size_is_bounded_before_trie_construction(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "d" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Prekė",
			"flags": [],
		}
		selector = {
			"id": "selector.item",
			"glossary_id": "CONTEXT.md#glossary-preke",
			"key": entry["key"],
			"source": "Item",
			"context": None,
			"source_digest": entry["source_digest"],
			"accepted_forms": ["Prekė"],
			"forbidden_forms": ["Elementas"],
			"case_sensitive": True,
			"unicode_normalization": "NFC",
			"reviewed": True,
			"review": "CONTEXT.md#glossary-preke",
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			self._replace_quality_artifact(
				compatibility,
				"glossary_selectors.json",
				{"schema_version": 1, "entries": [selector]},
			)
			with (
				patch("frappe_lt.catalog_quality.MAX_NORMALIZED_GLOSSARY_CHARS", 4),
				patch.object(
					catalog_quality,
					"_LiteralIndex",
					side_effect=AssertionError("trie construction must not start"),
				),
			):
				result = run(
					"test", root / "candidate.po", root / "report.json", compatibility_path=compatibility
				)

			self.assertEqual(result["exit_code"], 2)
			self.assertIn("normalized forms exceed 4", result["errors"][0]["detail"])

	def test_registry_slug_and_selector_references_are_strictly_validated(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "e" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Prekė",
			"flags": [],
		}
		for failure in ("unsafe-name", "stale-selector", "blank-review"):
			with self.subTest(failure=failure), TemporaryDirectory() as directory:
				root = Path(directory)
				compatibility = self._fixture(root, [entry], [candidate])
				if failure == "unsafe-name":
					registry = json.loads((root / "catalog_segments.json").read_text(encoding="utf-8"))
					registry["candidates"][0]["name"] = "../unsafe"
					self._replace_quality_artifact(compatibility, "catalog_segments.json", registry)
				else:
					selector = {
						"id": "selector.item",
						"glossary_id": "CONTEXT.md#glossary-preke",
						"key": entry["key"],
						"source": "Item",
						"context": None,
						"source_digest": "f" * 64 if failure == "stale-selector" else entry["source_digest"],
						"accepted_forms": ["Prekė"],
						"forbidden_forms": ["Elementas"],
						"case_sensitive": True,
						"unicode_normalization": "NFC",
						"reviewed": True,
						"review": "" if failure == "blank-review" else "CONTEXT.md#glossary-preke",
					}
					self._replace_quality_artifact(
						compatibility,
						"glossary_selectors.json",
						{"schema_version": 1, "entries": [selector]},
					)
				result = run(
					"test",
					root / "candidate.po",
					root / "report.json",
					compatibility_path=compatibility,
				)
				self.assertEqual(result["exit_code"], 2)

	def test_rollback_failure_is_reported_as_indeterminate_tool_failure(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "f" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Prekė",
			"flags": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			output = root / "candidate.po"
			output.write_bytes(b"previous")
			fsync_calls = []
			replace_calls = []

			def fsync(_fd):
				fsync_calls.append(_fd)
				if len(fsync_calls) == 2:
					raise OSError("publication directory fsync")

			def replace(source, target):
				replace_calls.append((source, target))
				if len(replace_calls) == 2:
					raise OSError("rollback replace")
				os.replace(source, target)

			result = run(
				"test",
				output,
				root / "report.json",
				compatibility_path=compatibility,
				compile_candidate=self._compile_candidate,
				fsync=fsync,
				replace=replace,
			)
			self.assertEqual(result["exit_code"], 2)
			self.assertIn("publication rollback failed", result["errors"][0]["detail"])

	def test_post_replace_failure_durably_deletes_new_output_through_injected_seam(self):
		entry = {
			"key": {"source": "Item", "context": None},
			"source_digest": "1" * 64,
			"source_locations": [],
			"stable_locators": [],
		}
		candidate = {
			"key": entry["key"],
			"source_digest": entry["source_digest"],
			"translation": "Prekė",
			"flags": [],
		}
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility = self._fixture(root, [entry], [candidate])
			output = root / "candidate.po"
			fsync_calls = []
			removed = []

			def fsync(_fd):
				fsync_calls.append(_fd)
				if len(fsync_calls) == 2:
					raise OSError("publication directory fsync")

			def remove(path):
				removed.append(path)
				path.unlink(missing_ok=True)

			result = run(
				"test",
				output,
				root / "report.json",
				compatibility_path=compatibility,
				compile_candidate=self._compile_candidate,
				fsync=fsync,
				remove=remove,
			)
			self.assertEqual(result["exit_code"], 2)
			self.assertEqual(removed, [output])
			self.assertGreaterEqual(len(fsync_calls), 3)
			self.assertFalse(output.exists())

	def test_frappe_compile_uses_isolated_python_and_minimal_disposable_environment(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			po_path = root / "candidate.po"
			po_path.write_bytes(b"po")

			def subprocess_run(command, **kwargs):
				Path(kwargs["env"]["FRAPPE_LT_CANDIDATE_MO"]).parent.mkdir(parents=True)
				Path(kwargs["env"]["FRAPPE_LT_CANDIDATE_MO"]).write_bytes(b"mo")
				self.assertEqual(command[1:3], ["-I", "-c"])
				self.assertNotIn("PYTHONPATH", kwargs["env"])
				self.assertNotIn("PYTHONHOME", kwargs["env"])
				self.assertEqual(Path(kwargs["env"]["HOME"]).parent, root)

			with (
				patch.dict(os.environ, {"PYTHONPATH": "untrusted", "PYTHONHOME": "untrusted"}),
				patch("frappe_lt.po.subprocess.run", side_effect=subprocess_run),
			):
				mo_path = compile_po(po_path, root)
			self.assertTrue(mo_path.is_file())
