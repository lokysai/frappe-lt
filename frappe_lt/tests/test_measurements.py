import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frappe_lt import measurements, release_catalog


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
			(root / "compatibility.json").write_text('{"upstream": {}}')
			with (
				patch.object(release_catalog, "verify_release", return_value={"mo_sha256": "a" * 64}),
				patch.object(release_catalog, "verify_mo", return_value="a" * 64),
				patch.object(
					measurements,
					"artifact_sizes",
					return_value={"shipped_bytes": 36 * 1024 * 1024, "limit_bytes": 35 * 1024 * 1024},
				),
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
					for variant, duration in (("baseline", pair * 10), ("enabled", (21 - pair) * 10))
					for temperature in ("cold", "warm")
				]
				result = measurements.report(
					samples, root=root, mo=root / "frappe_lt.mo", baseline="a", enabled="b"
				)
				self.assertEqual(result["paired_warm_p95_overhead_ms"], 170)
				self.assertNotEqual(
					result["paired_warm_p95_overhead_ms"],
					result["p95_ms"]["enabled_warm"] - result["p95_ms"]["baseline_warm"],
				)
				self.assertTrue(result["size_review_required"])
				with self.assertRaisesRegex(ValueError, "missing independent samples"):
					measurements.report(
						samples[:-1], root=root, mo=root / "frappe_lt.mo", baseline="a", enabled="b"
					)


if __name__ == "__main__":
	unittest.main()
