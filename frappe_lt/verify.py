import hashlib
import os
import platform
import shutil
import subprocess
from pathlib import Path

from babel.messages.pofile import read_po

from frappe_lt.inventory import load_compatibility, validate_tool_versions, verify_owned_artifacts

EXPECTED_APP_ORDER = ["frappe", "erpnext", "frappe_lt"]
COMPATIBILITY = load_compatibility()
EXPECTED_COMMITS = {app: pin["commit"] for app, pin in COMPATIBILITY["upstream"].items()}
EXPECTED_VERSIONS = {app: pin["version"] for app, pin in COMPATIBILITY["upstream"].items()}
EXPECTED_MO_SHA256 = COMPATIBILITY["mo_sha256"]
SOURCE_DATE_EPOCH = str(COMPATIBILITY["source_date_epoch"])
FIXED_PO_DATE = "2024-01-01 00:00+0000"


def check_database_override(rows: list[dict], mode: str) -> str | None:
	if not rows:
		return None

	identifiers = ", ".join(str(row["name"]) for row in rows)
	message = (
		"contextless lt Translation rows for Item are present "
		f"({identifiers}); cannot prove effective runtime origin from the app catalog"
	)
	if mode == "ci":
		raise ValueError(message)
	return f"WARNING: {message}"


def validate_app_order(installed_apps: list[str], mode: str) -> list[str]:
	actual_order = [app for app in installed_apps if app in EXPECTED_APP_ORDER]
	if actual_order != EXPECTED_APP_ORDER:
		raise ValueError(
			f"required app order is frappe, erpnext, frappe_lt; installed order is {installed_apps}"
		)
	if mode == "ci" and installed_apps != EXPECTED_APP_ORDER:
		raise ValueError(f"CI site must contain only {EXPECTED_APP_ORDER}; found {installed_apps}")
	return installed_apps


def assert_digest(expected: str, first: str, second: str, environment: dict[str, str]) -> None:
	if first == second == expected:
		return

	details = "\n".join(f"{name}: {value}" for name, value in environment.items())
	raise ValueError(
		f"MO digest mismatch\nexpected: {expected}\nfirst build: {first}\nsecond build: {second}\n{details}"
	)


def validate_po(path: Path) -> dict[str, str]:
	source_lines = path.read_text(encoding="utf-8").splitlines()
	if any(line.startswith("#~") for line in source_lines):
		raise ValueError("PO catalog must not contain obsolete messages")
	physical_messages = sum(line.startswith("msgid ") for line in source_lines) - 1
	if physical_messages != 1:
		raise ValueError(f"PO catalog must contain exactly one message; found {physical_messages}")

	with path.open("rb") as po_file:
		catalog = read_po(po_file, locale="lt", abort_invalid=True)
	if str(catalog.locale) != "lt":
		raise ValueError("PO catalog language must be lt")
	for field, actual in {
		"POT-Creation-Date": catalog.creation_date,
		"PO-Revision-Date": catalog.revision_date,
	}.items():
		if actual.strftime("%Y-%m-%d %H:%M%z") != FIXED_PO_DATE:
			raise ValueError(f"{field} must be fixed at {FIXED_PO_DATE}")

	messages = [message for message in catalog if message.id]
	if len(messages) != 1:
		raise ValueError(f"PO catalog must resolve to exactly one message; found {len(messages)}")

	message = messages[0]
	if "fuzzy" in message.flags:
		raise ValueError("PO message must not be fuzzy")
	if message.context is not None:
		raise ValueError("PO message must not have context")
	if isinstance(message.id, tuple) or isinstance(message.string, tuple):
		raise ValueError("PO message must not be plural")
	if message.id != "Item" or message.string != "Prekė":
		raise ValueError("PO message must be exactly Item -> Prekė")

	return {message.id: message.string}


def _git_commit(path: Path) -> str:
	return subprocess.run(
		["git", "rev-parse", "HEAD"],
		cwd=path,
		check=True,
		capture_output=True,
		text=True,
	).stdout.strip()


def _environment(frappe) -> dict[str, str]:
	import erpnext

	tools = validate_tool_versions(COMPATIBILITY)
	environment = {
		"frappe_commit": _git_commit(Path(frappe.get_app_source_path("frappe"))),
		"erpnext_commit": _git_commit(Path(frappe.get_app_source_path("erpnext"))),
		"frappe_version": frappe.__version__,
		"erpnext_version": erpnext.__version__,
		"python": platform.python_version(),
		"babel": tools["babel"],
	}
	for app, expected in EXPECTED_COMMITS.items():
		actual = environment[f"{app}_commit"]
		if actual != expected:
			raise ValueError(f"{app} commit must be {expected}; found {actual}")
	for app, expected in EXPECTED_VERSIONS.items():
		actual = environment[f"{app}_version"]
		if actual != expected:
			raise ValueError(f"{app} version must be {expected}; found {actual}")
	return environment


def _run_bench(bench_path: Path, *arguments: str, env: dict[str, str] | None = None) -> None:
	bench = shutil.which("bench") or str(bench_path / "env" / "bin" / "bench")
	subprocess.run([bench, *arguments], cwd=bench_path, check=True, env=env)


def _compile_twice(frappe, environment: dict[str, str], expected_digest: str) -> tuple[str, str, Path]:
	from frappe.gettext.translate import get_mo_path
	from frappe.utils import get_bench_path

	bench_path = Path(get_bench_path())
	mo_path = get_mo_path("frappe_lt", "lt")
	compile_environment = os.environ.copy()
	compile_environment["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
	digests = []
	for _build in range(2):
		mo_path.unlink(missing_ok=True)
		_run_bench(
			bench_path,
			"compile-po-to-mo",
			"--app",
			"frappe_lt",
			"--locale",
			"lt",
			"--force",
			env=compile_environment,
		)
		if not mo_path.is_file():
			raise ValueError(f"compile did not create {mo_path}")
		digests.append(hashlib.sha256(mo_path.read_bytes()).hexdigest())

	assert_digest(expected_digest, digests[0], digests[1], environment)
	return digests[0], digests[1], mo_path


def _assert_catalog_precedence(frappe) -> None:
	from frappe.gettext.translate import get_catalog
	from frappe.translate import get_translations_from_apps

	for app in ("frappe", "erpnext"):
		catalog = get_catalog(app, "lt")
		for message in catalog:
			if message.id == "Item" and not message.context and message.string:
				raise ValueError(
					f"{app} Lithuanian source catalog unexpectedly translates Item as {message.string!r}"
				)

	upstream = get_translations_from_apps("lt", apps=["frappe", "erpnext"])
	if "Item" in upstream:
		raise ValueError(f"upstream Lithuanian catalogs unexpectedly translate Item as {upstream['Item']!r}")
	with_override = get_translations_from_apps("lt", apps=EXPECTED_APP_ORDER)
	if with_override.get("Item") != "Prekė":
		raise ValueError(
			"app precedence failed: frappe + erpnext must omit Item and frappe_lt must add Prekė"
		)


def run(site: str, mode: str = "local", expected_digest: str | None = None) -> dict:
	"""Verify the pinned native PO override on an initialized Frappe site."""
	if mode not in {"ci", "local"}:
		raise ValueError("mode must be 'ci' or 'local'")
	if not site:
		raise ValueError("site is required")

	import frappe
	import frappe.translate
	from frappe.utils import get_bench_path

	if frappe.local.site != site:
		raise ValueError(f"command site {site!r} does not match initialized site {frappe.local.site!r}")
	verify_owned_artifacts()
	environment = _environment(frappe)
	print("Verified pinned environment:", environment)
	installed_apps = validate_app_order(frappe.get_installed_apps(), mode)
	validate_po(Path(frappe.get_app_path("frappe_lt", "locale", "lt.po")))
	expected_digest = expected_digest or EXPECTED_MO_SHA256
	first_digest, second_digest, mo_path = _compile_twice(frappe, environment, expected_digest)
	_assert_catalog_precedence(frappe)

	rows = frappe.db.sql(
		"""select name, translated_text
		from `tabTranslation`
		where language = %s and source_text = %s and coalesce(context, '') = ''""",
		("lt", "Item"),
		as_dict=True,
	)
	warning = check_database_override(rows, mode)
	if warning:
		print(warning)

	_run_bench(Path(get_bench_path()), "--site", site, "clear-cache")
	frappe.translate.clear_cache()
	effective = frappe._("Item", lang="lt")
	if effective != "Prekė":
		raise ValueError(f"effective runtime translation must be 'Prekė'; found {effective!r}")

	return {
		"site": site,
		"mode": mode,
		"environment": environment,
		"app_order": installed_apps,
		"source_date_epoch": SOURCE_DATE_EPOCH,
		"expected_digest": expected_digest,
		"first_digest": first_digest,
		"second_digest": second_digest,
		"mo_path": str(mo_path),
		"database_override": rows,
		"database_override_warning": warning,
		"effective_translation": effective,
	}
