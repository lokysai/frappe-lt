import sys
from pathlib import Path

from frappe_lt.po import parse_po
from frappe_lt.release_catalog import verify_mo, verify_release

EXPECTED_APP_ORDER = ["frappe", "erpnext", "frappe_lt"]


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
		try:
			from frappe_lt.inventory import verify_environment

			environment = verify_environment(
				frappe,
				site=site,
				require_active_catalog=True,
				require_clean_upstream=True,
				require_exact_apps=True,
				required_apps=("frappe", "erpnext", "frappe_lt"),
				require_active_directory=False,
			)
			pins = environment["upstream"]
			print(
				"Verified pinned environment: "
				f"frappe {pins['frappe']['version']} {pins['frappe']['commit']}; "
				f"erpnext {pins['erpnext']['version']} {pins['erpnext']['commit']}; "
				f"Python {environment['python']}; Babel {environment['babel']}",
				file=sys.stderr,
			)
		except Exception:
			# Diagnostics are secondary; never replace the authenticated mismatch.
			pass
		raise ValueError("requested MO digest differs from authenticated release")
	expected_digest = release["mo_sha256"]
	from frappe.gettext.translate import get_mo_path

	mo_path = Path(get_mo_path("frappe_lt", "lt"))
	actual_digest, mo_bytes = verify_mo(mo_path, with_size=True)
	_assert_catalog_precedence(frappe, installed_apps, parse_po(po_path).messages)

	rows = frappe.db.sql(
		"""select name
		from `tabTranslation`
		where language = %s and source_text = %s and coalesce(context, '') = ''""",
		("lt", "Item"),
		as_dict=True,
	)
	if check_database_override(rows, mode):
		print("Warning: a contextless Item database override is present", file=sys.stderr)

	frappe.translate.clear_cache()
	effective = frappe._("Item", lang="lt")
	if effective != "Prekė":
		raise ValueError(f"effective runtime translation must be 'Prekė'; found {effective!r}")

	return {
		"effective_translation": effective,
		"inventory_digest": release["inventory_digest"],
		"mo_bytes": mo_bytes,
		"mo_sha256": actual_digest,
		"release_digest": release["release_digest"],
		"site": site,
		"state": "verified",
	}
