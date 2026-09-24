import platform
from pathlib import Path

from frappe_lt.inventory import _git_value, load_compatibility
from frappe_lt.po import parse_po
from frappe_lt.release_catalog import verify_mo, verify_release

EXPECTED_APP_ORDER = ["frappe", "erpnext", "frappe_lt"]
COMPATIBILITY = load_compatibility()
SOURCE_DATE_EPOCH = str(COMPATIBILITY["source_date_epoch"])


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


def validate_po(path: Path) -> dict[str, str]:
	verify_release(po_path=path)
	catalog = parse_po(path)
	return catalog.messages


def _environment(frappe) -> dict[str, str]:
	from frappe_lt import install
	from frappe_lt.inventory import validate_tool_versions

	# Share the same real target-inventory and v16 compatibility check as the
	# bench and install-hook preflights, including unlisted patch versions.
	plan = install._saved(frappe)
	ready = install._preflight(plan["package"], plan["exceptions"], classify_site=False)
	if ready["site"] != frappe.local.site:
		raise ValueError("verification site differs from authenticated install inputs")
	compatibility = load_compatibility()
	tools = validate_tool_versions(compatibility)
	return {
		"frappe_commit": _git_value(Path(frappe.get_app_source_path("frappe")), "rev-parse", "HEAD"),
		"erpnext_commit": _git_value(Path(frappe.get_app_source_path("erpnext")), "rev-parse", "HEAD"),
		"frappe_version": ready["versions"]["frappe"],
		"erpnext_version": ready["versions"]["erpnext"],
		"python": platform.python_version(),
		"babel": tools["babel"],
		"warnings": ready["warnings"],
	}


def _assert_catalog_precedence(frappe, installed_apps: list[str], messages: dict) -> None:
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
	with_override = get_translations_from_apps("lt", apps=installed_apps)
	for (source, context), translation in messages.items():
		key = f"{source}:{context}" if context else source
		if with_override.get(key) != translation:
			raise ValueError("app precedence failed for a Release Inventory Translation Key")
	if with_override.get("Item") != "Prekė":
		raise ValueError("app precedence failed: the installed app catalog must resolve Item to Prekė")


def run(site: str, mode: str = "local", expected_digest: str | None = None) -> dict:
	"""Read-only verification of the pinned whole catalog on a Frappe site."""
	if mode not in {"ci", "local"}:
		raise ValueError("mode must be 'ci' or 'local'")
	if not site:
		raise ValueError("site is required")

	import frappe
	import frappe.translate

	if frappe.local.site != site:
		raise ValueError(f"command site {site!r} does not match initialized site {frappe.local.site!r}")
	environment = _environment(frappe)
	print("Verified pinned environment:", environment)
	installed_apps = validate_app_order(frappe.get_installed_apps(), mode)
	po_path = Path(frappe.get_app_path("frappe_lt", "locale", "lt.po"))
	release = verify_release(po_path=po_path)
	from frappe_lt import install, profile

	plan = install._checked(frappe)
	if (plan["release_digest"], plan["inventory_digest"], plan["mo_sha256"]) != (
		release["release_digest"],
		release["inventory_digest"],
		release["mo_sha256"],
	):
		raise ValueError("saved installation release differs from the authenticated catalog")
	if install._migration_state(frappe, plan) not in {"committed", "no_op"}:
		raise ValueError("legacy migration report or postcommit cache stage is incomplete")
	if profile.status()["state_after"] != "APPLIED":
		raise ValueError("Lithuanian profile is not applied")
	if expected_digest is not None and expected_digest != release["mo_sha256"]:
		raise ValueError("requested MO digest differs from authenticated release")
	expected_digest = release["mo_sha256"]
	from frappe.gettext.translate import get_mo_path

	mo_path = Path(get_mo_path("frappe_lt", "lt"))
	actual_digest = verify_mo(mo_path)
	_assert_catalog_precedence(frappe, installed_apps, parse_po(po_path).messages)

	rows = frappe.db.sql(
		"""select name
		from `tabTranslation`
		where language = %s and source_text = %s and coalesce(context, '') = ''""",
		("lt", "Item"),
		as_dict=True,
	)
	warning = check_database_override(rows, mode)
	if warning:
		print(warning)

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
		"release_digest": release["release_digest"],
		"inventory_digest": release["inventory_digest"],
		"mo_sha256": actual_digest,
		"mo_path": str(mo_path),
		"database_override": rows,
		"database_override_warning": warning,
		"effective_translation": effective,
	}
