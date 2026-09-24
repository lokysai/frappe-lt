"""Authenticated whole-inventory catalog assembly and two-artifact publication.

prepare() produces a canonical digest manifest after an isolated quality-gated build.
The manifest's SHA-256 must be pinned by the release before assemble() may publish.
"""

import os
import tempfile
from pathlib import Path

from frappe_lt.catalog_partition import SEGMENT_IDS
from frappe_lt.catalog_quality import (
	_digest,
	_key_tuple,
	_load_trusted,
	_read_json,
	_replace_bytes,
	_require_digest,
	_run,
	_validate_inventory,
)
from frappe_lt.inventory import canonical_json
from frappe_lt.po import FIXED_PO_DATE, parse_po

MANIFEST_NAME = "release_catalog.json"
# The committed release manifest is the independent expected digest for the full catalog.
PINNED_RELEASE_SHA256 = "e454fbfa8d52fc7362cfd871b5a6abae3b9a09d4b7c3d0f625b73d005e91259d"
MANIFEST_FIELDS = {"schema_version", "inventory_digest", "candidates", "keys", "po_sha256", "mo_sha256"}


def validate_release_bindings(manifest: dict, inventory: dict, records: list[dict]) -> None:
	"""Check the release's complete, exact candidate bindings against authenticated inputs."""
	if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS or manifest["schema_version"] != 1:
		raise ValueError("invalid release catalog manifest schema")
	if manifest["inventory_digest"] != _digest(canonical_json(inventory)):
		raise ValueError("release catalog inventory digest mismatch")
	if type(manifest["keys"]) is not int or manifest["keys"] != len(_validate_inventory(inventory)):
		raise ValueError("release catalog inventory key count mismatch")
	actual = {
		record["segment_id"]: {
			"name": record["name"],
			"candidate_sha256": record["candidate_sha256"],
			"manifest_sha256": record["manifest_sha256"],
		}
		for record in records
	}
	if (
		set(actual) != set(SEGMENT_IDS)
		or len(records) != len(SEGMENT_IDS)
		or manifest["candidates"] != actual
	):
		raise ValueError("release catalog requires exactly the three authenticated segment candidates")
	for field in ("po_sha256", "mo_sha256"):
		if manifest[field] is not None:
			_require_digest(manifest[field], f"release catalog {field}")


def _bindings(compatibility_path: Path) -> dict:
	inventory, _candidates, _manifests, artifacts = _load_trusted(
		None, compatibility_path, all_candidates=True
	)
	records = artifacts["catalog_segments.json"]["candidates"]
	manifest = {
		"schema_version": 1,
		"inventory_digest": _digest(canonical_json(inventory)),
		"keys": len(_validate_inventory(inventory)),
		"candidates": {
			record["segment_id"]: {
				"name": record["name"],
				"candidate_sha256": record["candidate_sha256"],
				"manifest_sha256": record["manifest_sha256"],
			}
			for record in records
		},
		"po_sha256": None,
		"mo_sha256": None,
	}
	validate_release_bindings(manifest, inventory, records)
	return manifest


def _build(manifest: dict, compatibility_path: Path, compiler=None) -> tuple[bytes, bytes]:
	with tempfile.TemporaryDirectory(prefix="frappe-lt-release-") as directory:
		root = Path(directory)
		po, mo = root / "lt.po", root / "frappe_lt.mo"
		result = _run(
			"release",
			po,
			root / "quality.json",
			compatibility_path=compatibility_path,
			compile_candidate=compiler,
			release_manifest=manifest,
			mo_output_path=mo,
		)
		if result["exit_code"]:
			raise ValueError(f"whole catalog quality gate failed: {result['errors'][:3]}")
		return po.read_bytes(), mo.read_bytes()


def prepare(manifest_path: Path, *, compatibility_path: Path | None = None, compiler=None) -> str:
	"""Quality-gate and compile the entire catalog; write the manifest to be pinned."""
	compatibility_path = Path(compatibility_path or Path(__file__).with_name("compatibility.json"))
	manifest = _bindings(compatibility_path)
	po, mo = _build(manifest, compatibility_path, compiler)
	manifest["po_sha256"], manifest["mo_sha256"] = _digest(po), _digest(mo)
	content = canonical_json(manifest)
	_replace_bytes(Path(manifest_path), content)
	return _digest(content)


def _active(path: Path, suffix: str) -> Path:
	path = Path(path).absolute()
	if path.suffix != suffix or any(part.is_symlink() for part in (path, *path.parents)):
		raise ValueError(f"active {suffix} path must have the expected suffix and no symlinks")
	if not path.parent.is_dir():
		raise ValueError(f"active {suffix} parent directory must exist")
	return path


def _pinned_manifest(manifest_path: Path, expected_manifest_sha256: str) -> tuple[dict, str]:
	expected = _require_digest(expected_manifest_sha256, "pinned release manifest digest")
	manifest, content = _read_json(manifest_path)
	if _digest(content) != expected or content != canonical_json(manifest):
		raise ValueError("release manifest is not the pinned canonical artifact")
	for field in ("po_sha256", "mo_sha256"):
		_require_digest(manifest.get(field), f"release catalog {field}")
	return manifest, expected


def verify_release(
	po_path: Path | None = None,
	*,
	manifest_path: Path | None = None,
	expected_manifest_sha256: str = PINNED_RELEASE_SHA256,
	compatibility_path: Path | None = None,
) -> dict[str, str]:
	"""Read-only proof of the pinned whole-inventory PO and MO expectation."""
	manifest_path = Path(manifest_path or Path(__file__).with_name(MANIFEST_NAME))
	compatibility_path = Path(compatibility_path or Path(__file__).with_name("compatibility.json"))
	po_path = _active(po_path or Path(__file__).with_name("locale") / "lt.po", ".po")
	manifest, release_digest = _pinned_manifest(manifest_path, expected_manifest_sha256)
	inventory, candidates, manifests, artifacts = _load_trusted(None, compatibility_path, all_candidates=True)
	records = artifacts["catalog_segments.json"]["candidates"]
	validate_release_bindings(manifest, inventory, records)
	expected = {}
	inventory_entries = _validate_inventory(inventory)
	for record in records:
		candidate = candidates[record["name"]]
		segment_keys = {_key_tuple(item["key"]) for item in manifests[record["name"]]["keys"]}
		for entry in candidate["entries"]:
			key = _key_tuple(entry["key"])
			if key in expected or key not in inventory_entries:
				raise ValueError(f"release candidate key is duplicated or outside inventory: {key!r}")
			if key not in segment_keys:
				raise ValueError(f"release candidate key is outside its segment: {key!r}")
			if entry["source_digest"] != inventory_entries[key]["source_digest"]:
				raise ValueError(f"release source digest mismatch for {key!r}")
			if not entry["translation"] or "fuzzy" in entry["flags"]:
				raise ValueError(f"release translation is missing or fuzzy for {key!r}")
			expected[key] = entry["translation"]
	if set(expected) != set(inventory_entries):
		raise ValueError("release candidates do not cover the exact Release Inventory")
	if _digest(po_path.read_bytes()) != manifest["po_sha256"]:
		raise ValueError("release PO digest mismatch")
	if any(line.startswith("#~") for line in po_path.read_text(encoding="utf-8").splitlines()):
		raise ValueError("release PO contains obsolete messages")
	parsed = parse_po(po_path)
	date = FIXED_PO_DATE.strftime("%Y-%m-%d %H:%M%z")
	if parsed.locale != "lt" or parsed.creation_date != date or parsed.revision_date != date:
		raise ValueError("release PO header is invalid")
	if parsed.messages != expected:
		raise ValueError("release PO keys, contexts or translations differ from the inventory candidates")
	return {
		"release_digest": release_digest,
		"inventory_digest": manifest["inventory_digest"],
		"mo_sha256": manifest["mo_sha256"],
	}


def verify_mo(mo_path: Path | None = None, **release_options) -> str:
	"""Check the active compiled MO against the authenticated release; never compile it."""
	if mo_path is None:
		from frappe.gettext.translate import get_mo_path

		mo_path = get_mo_path("frappe_lt", "lt")
	expected = verify_release(**release_options)["mo_sha256"]
	actual = _digest(_active(mo_path, ".mo").read_bytes())
	if actual != expected:
		raise ValueError(f"release MO digest mismatch: expected {expected}; found {actual}")
	return actual


def _publish_pair(po_path: Path, mo_path: Path, po: bytes, mo: bytes) -> None:
	"""Stage both files, then roll back both names if a replacement fails."""
	paths = (po_path, mo_path)
	contents = (po, mo)
	previous = tuple(path.read_bytes() if path.exists() else None for path in paths)
	staged = []
	replaced = []
	try:
		for path, content in zip(paths, contents, strict=True):
			fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
			stage = Path(name)
			staged.append(stage)
			with os.fdopen(fd, "wb") as stream:
				stream.write(content)
				stream.flush()
				os.fsync(stream.fileno())
		for path, stage in zip(paths, staged, strict=True):
			os.replace(stage, path)
			replaced.append(path)
		for parent in {path.parent for path in paths}:
			fd = os.open(parent, os.O_RDONLY)
			try:
				os.fsync(fd)
			finally:
				os.close(fd)
	except Exception:
		for path, old in zip(paths, previous, strict=True):
			if path not in replaced:
				continue
			if old is None:
				path.unlink(missing_ok=True)
			else:
				_replace_bytes(path, old)
		raise
	finally:
		for stage in staged:
			stage.unlink(missing_ok=True)


def assemble(
	po_path: Path,
	mo_path: Path,
	*,
	manifest_path: Path | None = None,
	expected_manifest_sha256: str | None = None,
	compatibility_path: Path | None = None,
	compiler=None,
) -> dict:
	"""Authenticate the pinned manifest, gate the full catalog, compile and publish."""
	compatibility_path = Path(compatibility_path or Path(__file__).with_name("compatibility.json"))
	manifest_path = Path(manifest_path or Path(__file__).with_name(MANIFEST_NAME))
	po_path, mo_path = _active(po_path, ".po"), _active(mo_path, ".mo")
	if po_path == mo_path or (po_path.exists() and mo_path.exists() and os.path.samefile(po_path, mo_path)):
		raise ValueError("active PO and MO paths must be distinct")
	expected = _require_digest(
		expected_manifest_sha256 or PINNED_RELEASE_SHA256, "pinned release manifest digest"
	)
	manifest, content = _read_json(manifest_path)
	if _digest(content) != expected or content != canonical_json(manifest):
		raise ValueError("release manifest is not the pinned canonical artifact")
	for field in ("po_sha256", "mo_sha256"):
		_require_digest(manifest.get(field), f"release catalog {field}")
	po, mo = _build(manifest, compatibility_path, compiler)
	_publish_pair(po_path, mo_path, po, mo)
	return {
		"keys": manifest["keys"],
		"po_sha256": _digest(po),
		"mo_sha256": _digest(mo),
		"manifest_sha256": expected,
	}
