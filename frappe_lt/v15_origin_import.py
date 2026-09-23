"""Import the authenticated, exact-key Frappe v15 origin baseline before review.

Run with ``python3 -m frappe_lt.v15_origin_import COMPATIBILITY FRAPPE_CSV ERPNEXT_CSV``.
"""

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from frappe_lt import inventory as inventory_module
from frappe_lt.catalog_quality import registered_candidates
from frappe_lt.catalog_quality import run as catalog_quality_gate
from frappe_lt.inventory import (
	build_report,
	canonical_json,
	generate_human_report,
	validate_provenance,
	verify_owned_artifacts,
	write_artifacts,
)

CSV_SHA256 = {
	"frappe-v15-lt.csv": "e3c8546c2f1e0a15bc676b42c7ee5704c79e834fffaaa988995a9d60ddae173c",
	"erpnext-v15-lt.csv": "eb43f49b82834cfdcfe9e938c2d55d355a40de228160a221b3d9b89605b6ef7f",
}
EXPECTED_MATCHES = 3346
FRAPPE_KEYS = 6344
FINANCE_EXPECTED_MATCHES = 2592
FINANCE_KEYS = 4907
MAX_CSV_BYTES = 8 * 1024 * 1024


def _v15_translations(
	paths: tuple[Path, Path], keys: set[tuple[str, str | None]]
) -> dict[tuple[str, str | None], str]:
	translations = {}
	for path in paths:
		if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CSV_BYTES:
			raise ValueError(f"v15 CSV must be a regular file no larger than {MAX_CSV_BYTES} bytes: {path}")
		try:
			content = path.read_bytes()
		except OSError as error:
			raise ValueError(f"could not read v15 CSV {path}: {error}") from error
		if hashlib.sha256(content).hexdigest() != CSV_SHA256[path.name]:
			raise ValueError(f"CSV digest mismatch for {path.name}")
		# newline="" keeps CSV-quoted line breaks and the translation value verbatim.
		with io.TextIOWrapper(io.BytesIO(content), encoding="utf-8", newline="") as stream:
			for number, row in enumerate(csv.reader(stream, strict=True), 1):
				if len(row) not in (2, 3):
					raise ValueError(f"invalid CSV row {path.name}:{number}: expected 2 or 3 columns")
				source, translation = row[:2]
				context = row[2] if len(row) == 3 else ""
				key = (source, context or None)
				if key not in keys:
					continue
				if key in translations and translations[key] != translation:
					raise ValueError(
						f"conflicting duplicate v15 Translation Key {key!r} at {path.name}:{number}"
					)
				translations[key] = translation
	return translations


def _gate_registered(candidates: list[str], compatibility_path: Path, compile_candidate) -> None:
	with TemporaryDirectory(prefix="frappe-lt-origin-gate-") as directory:
		for name in candidates:
			result = catalog_quality_gate(
				name,
				Path(directory) / f"{name}.po",
				Path(directory) / f"{name}.json",
				compatibility_path=compatibility_path,
				compile_candidate=compile_candidate,
			)
			if result["exit_code"]:
				raise ValueError(
					f"registered candidate {name!r} failed Catalog Quality Gate: {result['errors'][:1]}"
				)


def run(
	compatibility_path: Path,
	frappe_csv: Path,
	erpnext_csv: Path,
	*,
	segment_id: str = "frappe",
	compile_candidate=None,
) -> dict:
	"""Verify the release and import exact v15 originals for a frozen segment."""
	if segment_id not in {"frappe", "erpnext-finance-commerce"}:
		raise ValueError(f"unsupported v15 origin segment {segment_id!r}")
	compatibility_path = Path(compatibility_path)
	if compatibility_path.name != "compatibility.json" or compatibility_path.is_symlink():
		raise ValueError("compatibility path must be the release's compatibility.json commit marker")
	root = compatibility_path.parent
	paths = (Path(frappe_csv), Path(erpnext_csv))
	if tuple(path.name for path in paths) != tuple(CSV_SHA256):
		raise ValueError("expected frappe-v15-lt.csv and erpnext-v15-lt.csv in that order")
	compatibility = verify_owned_artifacts(compatibility_path)
	candidates = registered_candidates(compatibility_path)  # Full quality/partition/candidate trust boundary.
	registry = json.loads((root / "catalog_segments.json").read_bytes())
	if segment_id == "frappe" and (
		"frappe" in candidates
		or any(candidate["segment_id"] == "frappe" for candidate in registry["candidates"])
	):
		raise ValueError("Frappe origin import must precede Frappe candidate registration")
	if segment_id == "erpnext-finance-commerce" and any(
		candidate["segment_id"] == segment_id for candidate in registry["candidates"]
	):
		raise ValueError("finance origin import must precede finance candidate registration")
	inventory = json.loads((root / "release_inventory.json").read_bytes())
	provenance = json.loads((root / "provenance.json").read_bytes())
	manifest = json.loads((root / f"catalog_segments/{segment_id}.json").read_bytes())
	expected_keys = FRAPPE_KEYS if segment_id == "frappe" else FINANCE_KEYS
	if len(manifest["keys"]) != expected_keys:
		raise ValueError(f"frozen {segment_id} manifest has an unexpected key count")
	partition = json.loads((root / "catalog_partition.json").read_bytes())
	frozen_names = ["release_inventory.json", "catalog_partition.json", "catalog_segments.json"]
	frozen_names.extend(item["manifest"] for item in partition["segments"])
	frozen = {name: (root / name).read_bytes() for name in frozen_names}
	old_report = json.loads((root / "inventory_report.json").read_bytes())
	if old_report["baseline"]["provided"]:
		raise ValueError("cannot reconstruct a previous-inventory report without its authenticated baseline")
	if (root / "inventory_report.json").read_bytes() != canonical_json(
		build_report(inventory, provenance, compatibility)
	) or (root / "inventory_report.md").read_bytes() != generate_human_report(old_report).encode():
		raise ValueError("authenticated inventory reports are inconsistent with provenance")
	keys = {(item["key"]["source"], item["key"]["context"]) for item in manifest["keys"]}
	reviewed_exceptions = {
		((item["key"]["source"], item["key"]["context"]), item["source_digest"])
		for item in json.loads((root / "translation_exceptions.json").read_bytes())["entries"]
		if item["reviewed"]
	}
	segment_digests = {
		(item["key"]["source"], item["key"]["context"]): item["source_digest"] for item in manifest["keys"]
	}
	translations = _v15_translations(paths, keys)
	matches = {key: text for key in keys if (text := translations.get(key)) is not None and text.strip()}
	expected_matches = EXPECTED_MATCHES if segment_id == "frappe" else FINANCE_EXPECTED_MATCHES
	if len(matches) != expected_matches:
		raise ValueError(
			f"expected {expected_matches} nonempty exact {segment_id} matches; found {len(matches)}"
		)
	inherited = 0
	for record in provenance["entries"]:
		key = (record["key"]["source"], record["key"]["context"])
		if key not in keys:
			continue
		if key in matches:
			if record["status"] == "missing" or (
				segment_id == "erpnext-finance-commerce"
				and record.get("origin") == "new_ai"
				and record.get("translation") == matches[key]
			):
				record.update(status="translated", origin="inherited_v15", translation=matches[key])
				record.pop("exception", None)
			elif (
				segment_id == "erpnext-finance-commerce"
				and record["status"] == "excepted"
				and record.get("origin") == "approved_exception"
				and record.get("v15_original") == matches[key]
				and (key, segment_digests[key]) in reviewed_exceptions
			):
				continue
			elif not (
				record["status"] == "translated"
				and record.get("origin") == "inherited_v15"
				and record.get("translation") == matches[key]
			):
				raise ValueError(f"conflicting existing {segment_id} provenance for {key!r}")
			inherited += 1
		elif record["status"] != "missing":
			raise ValueError(f"unmatched {segment_id} key already has provenance: {key!r}")
	validate_provenance(inventory, provenance)
	report = build_report(inventory, provenance, compatibility)
	if report["inventory_digest"] != compatibility["inventory_digest"]:
		raise ValueError("frozen Release Inventory digest changed")
	artifacts = {
		"provenance.json": canonical_json(provenance),
		"inventory_report.json": canonical_json(report),
		"inventory_report.md": generate_human_report(report).encode(),
	}
	updated = {**compatibility, "artifact_sha256": {**compatibility["artifact_sha256"]}}
	for name, content in artifacts.items():
		updated["artifact_sha256"][name] = hashlib.sha256(content).hexdigest()
	artifacts["compatibility.json"] = canonical_json(updated)
	# Do not touch frozen inventories, manifests, candidates, or quality artifacts.
	if segment_id == "erpnext-finance-commerce":
		_gate_registered(candidates, compatibility_path, compile_candidate)
	previous = {name: (root / name).read_bytes() for name in artifacts}
	try:
		write_artifacts(root, artifacts)
		verify_owned_artifacts(compatibility_path)
		if registered_candidates(compatibility_path) != candidates:
			raise ValueError("registered candidate set changed during v15 origin import")
		if segment_id == "erpnext-finance-commerce":
			_gate_registered(candidates, compatibility_path, compile_candidate)
		if any((root / name).read_bytes() != content for name, content in frozen.items()):
			raise ValueError("frozen Release Inventory or segment manifests changed")
	except BaseException:
		# Restore on catchable interruptions as well as publication/check failures.
		inventory_module.write_artifacts(root, previous)
		raise
	return {"inherited": inherited, "missing": len(keys) - len(matches), "artifacts": sorted(artifacts)}


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("compatibility", type=Path)
	parser.add_argument("frappe_csv", type=Path)
	parser.add_argument("erpnext_csv", type=Path)
	parser.add_argument("--segment", choices=("frappe", "erpnext-finance-commerce"), default="frappe")
	args = parser.parse_args()
	print(
		json.dumps(
			run(args.compatibility, args.frappe_csv, args.erpnext_csv, segment_id=args.segment),
			sort_keys=True,
		)
	)


if __name__ == "__main__":
	main()
