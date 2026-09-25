import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frappe_lt import measurements, release_catalog
from frappe_lt.inventory import canonical_json


class MeasurementTests(unittest.TestCase):
	def test_shipped_size_includes_po_and_all_authenticated_metadata(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			for name in measurements.METADATA:
				(root / name).write_bytes(b"m")
			for subdir in ("catalog_segments", "catalog_candidates", "locale"):
				(root / subdir).mkdir()
			(root / "catalog_segments" / "frappe.json").write_bytes(b"s")
			(root / "catalog_candidates" / "frappe.json").write_bytes(b"c")
			(root / "locale" / "lt.po").write_bytes(b"po")
			mo = root / "frappe_lt.mo"
			mo.write_bytes(b"mo")
			result = measurements.artifact_sizes(root, mo)
			self.assertEqual(result["mo_and_metadata_bytes"], len(measurements.METADATA) + 4)
			self.assertEqual(result["shipped_bytes"], len(measurements.METADATA) + 6)
			self.assertEqual(result["limit_bytes"], 35 * 1024 * 1024)

	def test_nearest_rank_uses_paired_differences_not_difference_of_p95s(self):
		self.assertEqual(measurements.nearest_rank(list(range(20))), 18)
		self.assertRaises(ValueError, measurements.nearest_rank, [])
		self.assertRaises(ValueError, measurements.nearest_rank, [float("nan")])
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			for name in measurements.METADATA:
				path = root / name
				path.write_bytes(name.encode())
			for subdirectory in ("catalog_segments", "catalog_candidates", "locale"):
				(root / subdirectory).mkdir()
			(root / "catalog_segments" / "frappe.json").write_bytes(b"segment")
			(root / "catalog_candidates" / "frappe.json").write_bytes(b"candidate")
			(root / "locale" / "lt.po").write_bytes(b"po")
			(root / "frappe_lt.mo").write_bytes(b"mo")
			(root / "compatibility.json").write_text(
				json.dumps(
					{
						"upstream": {
							"erpnext": {"commit": "b" * 40, "version": "16.35.0"},
							"frappe": {"commit": "c" * 40, "version": "16.34.0"},
						}
					}
				)
			)
			release = {
				"inventory_digest": hashlib.sha256(
					(root / "release_inventory.json").read_bytes()
				).hexdigest(),
				"mo_sha256": hashlib.sha256(b"mo").hexdigest(),
				"release_digest": hashlib.sha256((root / "release_catalog.json").read_bytes()).hexdigest(),
			}
			with (
				patch.object(release_catalog, "verify_release", return_value=release),
				patch.object(release_catalog, "verify_mo", return_value=(release["mo_sha256"], 2)),
			):
				samples = [
					{
						"pair": pair,
						"variant": variant,
						"temperature": temperature,
						"duration_ms": duration,
						"at_utc": "2026-09-24T00:00:00+00:00",
					}
					for pair in range(1, 21)
					for variant, duration in (
						(("baseline", pair * 10), ("enabled", (21 - pair) * 10))
						if pair % 2
						else (("enabled", (21 - pair) * 10), ("baseline", pair * 10))
					)
					for temperature in ("cold", "warm")
				]
				result = measurements.report(
					samples,
					root=root,
					mo=root / "frappe_lt.mo",
					baseline="baseline_site",
					enabled="development.localhost",
					candidate={"clean": True, "commit": "f" * 40},
				)
				self.assertEqual(result["paired_warm_p95_overhead_ms"], 170)
				self.assertNotEqual(
					result["paired_warm_p95_overhead_ms"],
					result["p95_ms"]["enabled_warm"] - result["p95_ms"]["baseline_warm"],
				)
				self.assertFalse(result["size_review_required"])
				self.assertEqual(result["candidate"], {"clean": True, "commit": "f" * 40})
				self.assertEqual(
					result["protocol"]["sha256"],
					hashlib.sha256(canonical_json(measurements.PROTOCOL)).hexdigest(),
				)
				self.assertEqual(
					result["samples_sha256"], hashlib.sha256(canonical_json(samples)).hexdigest()
				)
				self.assertTrue(
					all(not Path(item["path"]).is_absolute() for item in result["artifacts"]["files"])
				)
				green_samples = [
					{
						**sample,
						"duration_ms": sample["pair"] * 10 + (2 if sample["variant"] == "enabled" else 0),
					}
					for sample in samples
				]
				green = measurements.report(
					green_samples,
					root=root,
					mo=root / "frappe_lt.mo",
					baseline="baseline_site",
					enabled="development.localhost",
					candidate={"clean": True, "commit": "f" * 40},
				)
				self.assertIs(
					measurements.validate_report(
						green,
						candidate=green["candidate"],
						release=green["release"],
						expected_mo_bytes=2,
						root=root,
						po_sha256=hashlib.sha256(b"po").hexdigest(),
					),
					green,
				)
				tampered = json.loads(json.dumps(green))
				tampered["paired_warm_p95_overhead_ms"] = 3
				with self.assertRaisesRegex(ValueError, "calculations"):
					measurements.validate_report(
						tampered,
						candidate=tampered["candidate"],
						release=tampered["release"],
						expected_mo_bytes=2,
					)
				with self.assertRaisesRegex(ValueError, "candidate"):
					measurements.validate_report(
						green,
						candidate={"clean": True, "commit": "e" * 40},
						release=green["release"],
						expected_mo_bytes=2,
					)
				with self.assertRaisesRegex(ValueError, "provenance"):
					measurements.validate_report(
						green,
						candidate=green["candidate"],
						release=green["release"],
						expected_mo_bytes=3,
					)
				with self.assertRaisesRegex(ValueError, "missing independent samples"):
					measurements.report(
						samples[:-1],
						root=root,
						mo=root / "frappe_lt.mo",
						baseline="baseline_site",
						enabled="development.localhost",
						candidate={"clean": True, "commit": "f" * 40},
					)


if __name__ == "__main__":
	unittest.main()
