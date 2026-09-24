import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

from frappe_lt import release_catalog
from frappe_lt.inventory import canonical_json
from frappe_lt.po import parse_po
from frappe_lt.tests.test_catalog_quality import CatalogQualityGateTest, _write_json


def compile_candidate(po_path, workspace):
	mo = workspace / "sites/assets/locale/lt/LC_MESSAGES/frappe_lt.mo"
	mo.parent.mkdir(parents=True, exist_ok=True)
	with po_path.open("rb") as source, mo.open("wb") as target:
		write_mo(target, read_po(source))


class ReleaseCatalogTest(TestCase):
	def fixture(self, root):
		entries = [
			{
				"key": {"source": source, "context": context},
				"source_digest": digest * 64,
				"source_locations": [],
				"stable_locators": [],
			}
			for source, context, digest in (("Item", None, "a"), ("Item", "button", "b"))
		]
		translations = [
			{
				"key": entries[1]["key"],
				"source_digest": entries[1]["source_digest"],
				"translation": "Prekė (mygtukas)",
				"flags": [],
			}
		]
		compatibility = CatalogQualityGateTest()._fixture(
			root,
			entries,
			translations,
			segment_keys=[{"key": entries[1]["key"], "source_digest": entries[1]["source_digest"]}],
		)
		registry = json.loads((root / "catalog_segments.json").read_bytes())
		from frappe_lt.review_evidence import review_run_id

		for segment_id in ("frappe", "erpnext-finance-commerce"):
			selected = entries[:1] if segment_id == "frappe" else []
			candidate_entries = []
			for entry in selected:
				candidate_entry = {
					"flags": [],
					"key": entry["key"],
					"source_digest": entry["source_digest"],
					"translation": "Prekė",
					"provenance": {
						"origin": "new_ai",
						"review": {
							"agent": "test-agent",
							"explanation": None,
							"model": "test/model",
							"reason": "new_translation",
							"run_id": "",
							"status": "reviewed",
						},
					},
				}
				candidate_entry["provenance"]["review"]["run_id"] = review_run_id(candidate_entry)
				candidate_entries.append(candidate_entry)
			manifest_path = f"catalog_segments/{segment_id}.json"
			manifest_sha = hashlib.sha256((root / manifest_path).read_bytes()).hexdigest()
			value = {
				"schema_version": 1,
				"review_evidence_schema_version": 1,
				"inventory_digest": json.loads(compatibility.read_bytes())["inventory_digest"],
				"manifest_sha256": manifest_sha,
				"segment_id": segment_id,
				"entries": candidate_entries,
			}
			registry["candidates"].append(
				{
					"name": segment_id,
					"segment_id": segment_id,
					"candidate": f"catalog_candidates/{segment_id}.json",
					"candidate_sha256": _write_json(root / f"catalog_candidates/{segment_id}.json", value),
					"manifest": manifest_path,
					"manifest_sha256": manifest_sha,
				}
			)
		data = json.loads(compatibility.read_bytes())
		data["quality_gate"]["artifact_sha256"]["catalog_segments.json"] = _write_json(
			root / "catalog_segments.json", registry
		)
		_write_json(compatibility, data)
		manifest = root / "release_catalog.json"
		pin = release_catalog.prepare(manifest, compatibility_path=compatibility, compiler=compile_candidate)
		return compatibility, manifest, pin

	def test_full_catalog_is_deterministic_and_preserves_context(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility, manifest, pin = self.fixture(root)
			po, mo = root / "lt.po", root / "frappe_lt.mo"
			kwargs = {
				"manifest_path": manifest,
				"expected_manifest_sha256": pin,
				"compatibility_path": compatibility,
				"compiler": compile_candidate,
			}
			first = release_catalog.assemble(po, mo, **kwargs)
			first_bytes = (po.read_bytes(), mo.read_bytes())
			second = release_catalog.assemble(po, mo, **kwargs)
			self.assertEqual(first, second)
			self.assertEqual(first_bytes, (po.read_bytes(), mo.read_bytes()))
			self.assertEqual(first["keys"], 2)
			self.assertEqual(
				parse_po(po).messages, {("Item", None): "Prekė", ("Item", "button"): "Prekė (mygtukas)"}
			)
			self.assertEqual(manifest.read_bytes(), canonical_json(json.loads(manifest.read_bytes())))

	def test_read_only_release_and_mo_verification(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility, manifest, pin = self.fixture(root)
			po, mo = root / "lt.po", root / "frappe_lt.mo"
			release_catalog.assemble(
				po,
				mo,
				manifest_path=manifest,
				expected_manifest_sha256=pin,
				compatibility_path=compatibility,
				compiler=compile_candidate,
			)
			options = {
				"po_path": po,
				"manifest_path": manifest,
				"expected_manifest_sha256": pin,
				"compatibility_path": compatibility,
			}
			original = (po.read_bytes(), mo.read_bytes())
			with patch("frappe_lt.release_catalog._build", side_effect=AssertionError("compiled")):
				result = release_catalog.verify_release(**options)
				self.assertEqual(
					result,
					{
						"inventory_digest": json.loads(manifest.read_bytes())["inventory_digest"],
						"release_digest": pin,
						"mo_sha256": hashlib.sha256(original[1]).hexdigest(),
					},
				)
				self.assertEqual(release_catalog.verify_mo(mo, **options), result["mo_sha256"])
			self.assertEqual((po.read_bytes(), mo.read_bytes()), original)
			self.assertNotEqual(json.loads(compatibility.read_bytes())["mo_sha256"], result["mo_sha256"])

			with self.subTest("wrong MO"):
				mo.write_bytes(b"wrong")
				with self.assertRaisesRegex(ValueError, "release MO digest mismatch"):
					release_catalog.verify_mo(mo, **options)
				mo.write_bytes(original[1])
			with self.subTest("missing contextual key"):
				po.write_bytes(original[0].replace(b'msgctxt "button"', b'msgctxt "other"'))
				with self.assertRaisesRegex(ValueError, "release PO digest mismatch"):
					release_catalog.verify_release(**options)
				po.write_bytes(original[0])
			with self.subTest("bad manifest pin"):
				with self.assertRaisesRegex(ValueError, "pinned canonical"):
					release_catalog.verify_release(**(options | {"expected_manifest_sha256": "0" * 64}))
			with self.subTest("missing candidate key"):
				candidate = root / "catalog_candidates/test.json"
				value = json.loads(candidate.read_bytes())
				value["entries"] = []
				registry = json.loads((root / "catalog_segments.json").read_bytes())
				registry["candidates"][0]["candidate_sha256"] = _write_json(candidate, value)
				data = json.loads(compatibility.read_bytes())
				data["quality_gate"]["artifact_sha256"]["catalog_segments.json"] = _write_json(
					root / "catalog_segments.json", registry
				)
				_write_json(compatibility, data)
				with self.assertRaises(ValueError):
					release_catalog.verify_release(**options)

	def test_rejects_tampered_manifest_candidate_missing_context_and_mo(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility, manifest, pin = self.fixture(root)
			po, mo = root / "lt.po", root / "frappe_lt.mo"
			po.write_bytes(b"previous PO")
			mo.write_bytes(b"previous MO")
			kwargs = {
				"manifest_path": manifest,
				"expected_manifest_sha256": pin,
				"compatibility_path": compatibility,
				"compiler": compile_candidate,
			}
			with self.subTest("manifest"):
				old = manifest.read_bytes()
				manifest.write_bytes(old + b" ")
				with self.assertRaisesRegex(ValueError, "pinned canonical"):
					release_catalog.assemble(po, mo, **kwargs)
				manifest.write_bytes(old)
			with self.subTest("candidate"):
				candidate = root / "catalog_candidates/frappe.json"
				old = candidate.read_bytes()
				candidate.write_bytes(old + b" ")
				with self.assertRaisesRegex(ValueError, "candidate digest mismatch"):
					release_catalog.assemble(po, mo, **kwargs)
				candidate.write_bytes(old)
			with self.subTest("missing context"):
				candidate = root / "catalog_candidates/test.json"
				old = candidate.read_bytes()
				value = json.loads(old)
				value["entries"] = []
				registry = json.loads((root / "catalog_segments.json").read_bytes())
				registry["candidates"][0]["candidate_sha256"] = _write_json(candidate, value)
				data = json.loads(compatibility.read_bytes())
				data["quality_gate"]["artifact_sha256"]["catalog_segments.json"] = _write_json(
					root / "catalog_segments.json", registry
				)
				_write_json(compatibility, data)
				with self.assertRaisesRegex(ValueError, "authenticated segment candidates"):
					release_catalog.assemble(po, mo, **kwargs)
				candidate.write_bytes(old)
			with self.subTest("wrong compiler output"):
				# Use a fresh authenticated fixture to reach the compiler boundary.
				other = root / "other"
				other.mkdir()
				trusted, signed, digest = self.fixture(other)

				def wrong_compiler(_po, workspace):
					compiled = workspace / "sites/assets/locale/lt/LC_MESSAGES/frappe_lt.mo"
					compiled.parent.mkdir(parents=True, exist_ok=True)
					compiled.write_bytes(b"wrong")

				with self.assertRaisesRegex(ValueError, "release MO digest mismatch"):
					release_catalog.assemble(
						po,
						mo,
						manifest_path=signed,
						expected_manifest_sha256=digest,
						compatibility_path=trusted,
						compiler=wrong_compiler,
					)
			self.assertEqual((po.read_bytes(), mo.read_bytes()), (b"previous PO", b"previous MO"))

	def test_second_replace_failure_restores_both_active_files(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility, manifest, pin = self.fixture(root)
			po, mo = root / "lt.po", root / "frappe_lt.mo"
			po.write_bytes(b"old PO")
			mo.write_bytes(b"old MO")
			original = os.replace

			def fail_mo(source, destination):
				if Path(destination) == mo:
					raise OSError("MO replace failed")
				return original(source, destination)

			with patch("frappe_lt.release_catalog.os.replace", side_effect=fail_mo):
				with self.assertRaisesRegex(OSError, "MO replace failed"):
					release_catalog.assemble(
						po,
						mo,
						manifest_path=manifest,
						expected_manifest_sha256=pin,
						compatibility_path=compatibility,
						compiler=compile_candidate,
					)
			self.assertEqual((po.read_bytes(), mo.read_bytes()), (b"old PO", b"old MO"))

	def test_full_quality_gate_rejects_missing_context_before_compilation(self):
		with TemporaryDirectory() as directory:
			root = Path(directory)
			compatibility, _manifest, _pin = self.fixture(root)
			candidate = root / "catalog_candidates/test.json"
			value = json.loads(candidate.read_bytes())
			value["entries"] = []
			registry = json.loads((root / "catalog_segments.json").read_bytes())
			registry["candidates"][0]["candidate_sha256"] = _write_json(candidate, value)
			data = json.loads(compatibility.read_bytes())
			data["quality_gate"]["artifact_sha256"]["catalog_segments.json"] = _write_json(
				root / "catalog_segments.json", registry
			)
			_write_json(compatibility, data)
			with self.assertRaisesRegex(ValueError, "MISSING_TRANSLATION_KEY"):
				release_catalog.prepare(
					root / "rejected.json",
					compatibility_path=compatibility,
					compiler=lambda *_args: self.fail("must not compile an incomplete catalog"),
				)
			self.assertFalse((root / "rejected.json").exists())
