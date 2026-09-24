import ast
import hashlib
import inspect
import json
import re
import textwrap
from dataclasses import dataclass

from frappe_lt.inventory import RUNTIME_METADATA_CATEGORIES, ExtractionEvent

EXPECTED_RUNTIME_APPS = ["frappe", "erpnext"]
CUSTOM_METADATA = (
	("DocType", {"custom": 1}),
	("Custom Field", {"is_system_generated": 0}),
	("Property Setter", {"is_system_generated": 0}),
	("Client Script", None),
	("Server Script", None),
	("Translation", None),
	# Pinned Workflow records have no standard or owning-app identity marker.
	("Workflow", None),
	("Page", {"standard": ("!=", "Yes")}),
	("Report", {"is_standard": ("!=", "Yes")}),
	("Navbar Item", {"is_standard": 0}),
	("Workspace", {"for_user": ("is", "set")}),
	("Workspace Sidebar", {"standard": 0}),
)
METADATA_SIGNATURE_CATEGORIES = (*RUNTIME_METADATA_CATEGORIES,)
CUSTOM_FIELD_FIELDS = (
	"name",
	"dt",
	"fieldname",
	"label",
	"description",
	"fieldtype",
	"options",
	"insert_after",
	"depends_on",
	"default",
	"hidden",
	"read_only",
	"reqd",
	"permlevel",
	"fetch_from",
	"is_system_generated",
)
PROPERTY_SETTER_FIELDS = (
	"name",
	"doc_type",
	"doctype_or_field",
	"field_name",
	"row_name",
	"property",
	"value",
	"property_type",
	"is_system_generated",
)
EXCLUDED_SELECT_FIELDS = {
	"currency_precision",
	"float_precision",
	"icon",
	"minimum_password_score",
	"naming_series",
	"number_format",
}
REPORT_TRANSLATE_PATTERN = re.compile('"([^:,^"]*):')


@dataclass(frozen=True)
class RuntimeExtraction:
	events: list[ExtractionEvent]
	categories: tuple[str, ...] = RUNTIME_METADATA_CATEGORIES


def _is_translatable(value: str | None) -> bool:
	return bool(
		value
		and re.search("[a-zA-Z]", value)
		and not value.startswith("fa fa-")
		and not value.endswith("px")
		and not value.startswith("eval:")
	)


def _event(
	source: str | None,
	app: str,
	location: str,
	locator: str | None,
	extractor: str = "doctype",
	context: str | None = None,
) -> ExtractionEvent | None:
	if not _is_translatable(source):
		return None
	return ExtractionEvent(
		app=app,
		context=context,
		extractor=extractor,
		line=None,
		origin="runtime",
		raw_source=source,
		source=source,
		source_location=location,
		stable_locator=locator,
	)


def _app_for_module(frappe, module: str) -> str:
	if get_module_app := getattr(frappe, "get_module_app", None):
		app = get_module_app(module)
	else:
		app = frappe.local.module_app[frappe.scrub(module)]
	if app not in EXPECTED_RUNTIME_APPS:
		raise ValueError(f"standard metadata in module {module!r} belongs to unexpected app {app!r}")
	return app


def _append(
	events: list[ExtractionEvent],
	source: str | None,
	app: str,
	location: str,
	locator: str | None,
	extractor: str,
	context: str | None = None,
) -> None:
	if event := _event(source, app, location, locator, extractor, context):
		events.append(event)


def _metadata_digest(tables: dict[str, list[dict]]) -> str:
	payload = {
		doctype: [{field: row.get(field) for field in sorted(row)} for row in rows]
		for doctype, rows in sorted(tables.items())
	}
	encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
	return hashlib.sha256(encoded).hexdigest()


def _validate_metadata_signatures(metadata: dict[str, dict[str, list[dict]]], expected: dict) -> None:
	if set(expected) != set(METADATA_SIGNATURE_CATEGORIES):
		raise ValueError(
			f"runtime metadata signatures must cover exactly {list(METADATA_SIGNATURE_CATEGORIES)}"
		)
	actual = {category: _metadata_digest(metadata[category]) for category in METADATA_SIGNATURE_CATEGORIES}
	mismatches = [
		category for category in METADATA_SIGNATURE_CATEGORIES if actual[category] != expected[category]
	]
	if mismatches:
		details = "; ".join(
			f"{category}: expected {expected[category]}, computed {actual[category]}"
			for category in mismatches
		)
		raise ValueError(f"runtime metadata differs from the pinned clean-install source: {details}")


def verify_standard_metadata(frappe, expected_sha256: dict) -> dict[str, dict[str, list[dict]]]:
	"""Collect and authenticate the pinned standard metadata boundary without mutation."""
	metadata = collect_standard_metadata(frappe)
	_validate_metadata_signatures(metadata, expected_sha256)
	return metadata


def _system_custom_field_app(field: dict) -> str:
	if field["dt"] in {"Custom DocPerm", "DocPerm", "DocShare"}:
		return "frappe"
	return "erpnext"


class _InstallerTranslationIdentity(ast.NodeTransformer):
	def visit_Call(self, node: ast.Call):
		if (
			isinstance(node.func, ast.Name)
			and node.func.id == "_"
			and len(node.args) == 1
			and not node.keywords
		):
			return self.visit(node.args[0])
		raise ValueError("ERPNext installer Navbar definition contains a non-literal call")


def _erpnext_installer_navbar_labels() -> tuple[str, ...]:
	from erpnext.setup import install

	tree = ast.parse(textwrap.dedent(inspect.getsource(install.add_standard_navbar_items)))
	for node in ast.walk(tree):
		if not isinstance(node, ast.Assign) or not any(
			isinstance(target, ast.Name) and target.id == "erpnext_navbar_items" for target in node.targets
		):
			continue
		items = ast.literal_eval(ast.fix_missing_locations(_InstallerTranslationIdentity().visit(node.value)))
		if not isinstance(items, list) or not items:
			break
		labels = tuple(
			item.get("item_label")
			for item in items
			if isinstance(item, dict) and item.get("is_standard") == 1
		)
		if len(labels) != len(items) or any(not isinstance(label, str) or not label for label in labels):
			break
		return labels
	raise ValueError("could not read standard Navbar items from ERPNext installer source")


def _navbar_owners(frappe) -> dict[str, set[str]]:
	owners: dict[str, set[str]] = {}
	for hook in ("standard_navbar_items", "standard_help_items"):
		for item in frappe.get_hooks(hook, app_name="frappe") or []:
			if label := item.get("item_label"):
				owners.setdefault(label, set()).add("frappe")
	for label in _erpnext_installer_navbar_labels():
		owners.setdefault(label, set()).add("erpnext")
	return owners


def collect_standard_metadata(frappe) -> dict[str, dict[str, list[dict]]]:
	"""Bulk-collect the standard metadata boundary shared by inventory and discovery."""
	doctypes = frappe.get_all(
		"DocType",
		filters={"custom": 0},
		fields=["name", "module", "description"],
		order_by="name asc",
	)
	fields = frappe.get_all(
		"DocField",
		fields=["parent", "fieldname", "fieldtype", "label", "description", "options"],
		order_by="parent asc, idx asc",
	)
	permissions = frappe.get_all("DocPerm", fields=["parent", "role"], order_by="parent asc, idx asc")
	links = frappe.get_all("DocType Link", fields=["parent", "group"], order_by="parent asc, idx asc")
	pages = frappe.get_all(
		"Page",
		filters={"standard": "Yes"},
		fields=["name", "title", "module"],
		order_by="name asc",
	)
	reports = frappe.get_all(
		"Report",
		filters={"is_standard": "Yes"},
		fields=["name", "report_name", "module", "ref_doctype", "query"],
		order_by="name asc",
	)
	report_columns = frappe.get_all(
		"Report Column", fields=["parent", "fieldname", "label"], order_by="parent asc, idx asc"
	)
	report_filters = frappe.get_all(
		"Report Filter", fields=["parent", "fieldname", "label"], order_by="parent asc, idx asc"
	)

	navbar_items = frappe.get_all(
		"Navbar Item",
		filters={"item_label": ("is", "set")},
		fields=["item_label"],
		order_by="item_label asc",
	)

	workspaces = frappe.get_all(
		"Workspace",
		fields=["name", "label", "module", "app", "for_user", "public", "content"],
		order_by="name asc",
	)
	workspace_children = {}
	for child_doctype, child_fields in (
		("Workspace Chart", ("label",)),
		("Workspace Number Card", ("label",)),
		("Workspace Link", ("label", "description")),
		("Workspace Shortcut", ("label", "format")),
		("Workspace Quick List", ("label",)),
	):
		workspace_children[child_doctype] = frappe.get_all(
			child_doctype,
			fields=["parent", "idx", *child_fields],
			order_by="parent asc, idx asc",
		)

	sidebars = frappe.get_all(
		"Workspace Sidebar",
		filters={"standard": 1},
		fields=["name", "title", "app", "for_user"],
		order_by="name asc",
	)
	sidebar_items = frappe.get_all(
		"Workspace Sidebar Item",
		fields=["parent", "idx", "label"],
		order_by="parent asc, idx asc",
	)
	custom_fields = frappe.get_all(
		"Custom Field",
		filters={"is_system_generated": 1},
		fields=list(CUSTOM_FIELD_FIELDS),
		order_by="dt asc, fieldname asc",
	)
	property_setters = frappe.get_all(
		"Property Setter",
		filters={"is_system_generated": 1},
		fields=list(PROPERTY_SETTER_FIELDS),
		order_by="name asc",
	)
	metadata = {
		"custom_field": {"Custom Field": custom_fields},
		"doctype": {
			"DocField": fields,
			"DocPerm": permissions,
			"DocType": doctypes,
			"DocType Link": links,
		},
		"navbar": {"Navbar Item": navbar_items},
		"page": {"Page": pages},
		"property_setter": {"Property Setter": property_setters},
		"report": {
			"Report": reports,
			"Report Column": report_columns,
			"Report Filter": report_filters,
		},
		"workflow": {"Workflow": []},
		"workspace": {"Workspace": workspaces, **workspace_children},
		"workspace_sidebar": {
			"Workspace Sidebar": sidebars,
			"Workspace Sidebar Item": sidebar_items,
		},
	}
	return metadata


def extract_runtime(
	frappe, metadata_sha256: dict, *, allow_deployment_site: bool = False
) -> RuntimeExtraction:
	"""Extract metadata from the clean pinned site without invoking mixed source helpers."""
	site = getattr(frappe.local, "site", None)
	if site == "development.localhost" and not allow_deployment_site:
		raise ValueError("runtime inventory must not use development.localhost")
	installed_apps = frappe.get_installed_apps()
	if installed_apps != EXPECTED_RUNTIME_APPS:
		raise ValueError(f"runtime site must contain exactly frappe and erpnext; found {installed_apps}")
	for doctype, filters in CUSTOM_METADATA:
		if frappe.get_all(doctype, filters=filters, fields=["name"], limit=1):
			raise ValueError(f"runtime site contains local/custom metadata in {doctype}")

	metadata = verify_standard_metadata(frappe, metadata_sha256)
	doctypes = metadata["doctype"]["DocType"]
	fields = metadata["doctype"]["DocField"]
	permissions = metadata["doctype"]["DocPerm"]
	links = metadata["doctype"]["DocType Link"]
	pages = metadata["page"]["Page"]
	reports = metadata["report"]["Report"]
	report_columns = metadata["report"]["Report Column"]
	report_filters = metadata["report"]["Report Filter"]
	navbar_items = metadata["navbar"]["Navbar Item"]
	workspaces = metadata["workspace"]["Workspace"]
	workspace_children = {name: rows for name, rows in metadata["workspace"].items() if name != "Workspace"}
	sidebars = metadata["workspace_sidebar"]["Workspace Sidebar"]
	sidebar_items = metadata["workspace_sidebar"]["Workspace Sidebar Item"]
	custom_fields = metadata["custom_field"]["Custom Field"]
	property_setters = metadata["property_setter"]["Property Setter"]
	navbar_owners = _navbar_owners(frappe)

	doctype_apps = {}
	events = []
	doctype_overrides = {}
	field_overrides = {}
	for setter in property_setters:
		key = (setter["doc_type"], setter.get("field_name"), setter["property"])
		if setter["doctype_or_field"] == "DocType":
			doctype_overrides[(setter["doc_type"], setter["property"])] = setter
		elif setter["doctype_or_field"] == "DocField":
			field_overrides[key] = setter
	for doctype in doctypes:
		module_setter = doctype_overrides.get((doctype["name"], "module"))
		app = _app_for_module(frappe, module_setter["value"] if module_setter else doctype["module"])
		doctype_apps[doctype["name"]] = app
		base_location = f"metadata/doctype/{doctype['name']}"
		for source, property_name in (
			(doctype["name"], "name"),
			(doctype["module"], "module"),
			(doctype.get("description"), "description"),
		):
			setter = doctype_overrides.get((doctype["name"], property_name))
			if setter:
				source = setter["value"]
				location = f"metadata/property_setter/{setter['name']}"
				extractor = "property_setter"
				locator = f"property_setter:{setter['name']}"
			else:
				location = base_location
				extractor = "doctype"
				locator = f"doctype:{doctype['name']}:{property_name}"
			_append(events, source, app, location, locator, extractor)

	all_fields = [*fields, *custom_fields]
	for field in all_fields:
		parent = field.get("parent") or field.get("dt")
		if parent not in doctype_apps:
			continue
		is_custom = "dt" in field
		app = _system_custom_field_app(field) if is_custom else doctype_apps[parent]
		if is_custom:
			location = f"metadata/custom_field/{field['name']}"
			base_locator = f"custom_field:{parent}:{field['fieldname']}"
			extractor = "custom_field"
		else:
			location = f"metadata/doctype/{parent}/field/{field['fieldname']}"
			base_locator = f"doctype:{parent}:field:{field['fieldname']}"
			extractor = "doctype"
		values = {}
		for property_name in ("fieldtype", "label", "description", "options"):
			setter = field_overrides.get((parent, field["fieldname"], property_name))
			values[property_name] = setter["value"] if setter else field.get(property_name)
		for property_name in ("label", "description"):
			setter = field_overrides.get((parent, field["fieldname"], property_name))
			_append(
				events,
				values[property_name],
				app,
				f"metadata/property_setter/{setter['name']}" if setter else location,
				f"property_setter:{setter['name']}" if setter else f"{base_locator}:{property_name}",
				"property_setter" if setter else extractor,
			)
		options = values["options"]
		if values["fieldtype"] == "Select" and field["fieldname"] not in EXCLUDED_SELECT_FIELDS and options:
			setter = field_overrides.get((parent, field["fieldname"], "options")) or field_overrides.get(
				(parent, field["fieldname"], "fieldtype")
			)
			for option in options.split("\n"):
				if option and not option.isdigit():
					_append(
						events,
						option,
						app,
						f"metadata/property_setter/{setter['name']}" if setter else location,
						None,
						"property_setter" if setter else extractor,
					)
		elif values["fieldtype"] == "HTML":
			setter = field_overrides.get((parent, field["fieldname"], "options")) or field_overrides.get(
				(parent, field["fieldname"], "fieldtype")
			)
			_append(
				events,
				options,
				app,
				f"metadata/property_setter/{setter['name']}" if setter else location,
				f"property_setter:{setter['name']}" if setter else f"{base_locator}:options",
				"property_setter" if setter else extractor,
			)

	for permission in permissions:
		if app := doctype_apps.get(permission["parent"]):
			_append(
				events,
				permission.get("role"),
				app,
				f"metadata/doctype/{permission['parent']}/permissions",
				None,
				"doctype",
			)
	for link in links:
		if app := doctype_apps.get(link["parent"]):
			_append(
				events,
				link.get("group"),
				app,
				f"metadata/doctype/{link['parent']}/links",
				None,
				"doctype",
			)

	for page in pages:
		app = _app_for_module(frappe, page["module"])
		_append(
			events,
			page.get("title") or page["name"],
			app,
			f"metadata/page/{page['name']}",
			f"page:{page['name']}:title",
			"page",
		)

	report_apps = {}
	for report in reports:
		app = _app_for_module(frappe, report["module"])
		report_apps[report["name"]] = app
		location = f"metadata/report/{report['name']}"
		_append(events, report["name"], app, location, f"report:{report['name']}:name", "report")
		_append(
			events,
			report.get("report_name"),
			app,
			location,
			f"report:{report['name']}:report_name",
			"report",
		)
		for alias in REPORT_TRANSLATE_PATTERN.findall(report.get("query") or ""):
			_append(events, alias, app, location, None, "report")
	for child_doctype, rows, context_template in (
		("Report Column", report_columns, "Column of report '{}'"),
		("Report Filter", report_filters, None),
	):
		for row in rows:
			if app := report_apps.get(row["parent"]):
				fieldname = row.get("fieldname") or ""
				_append(
					events,
					row.get("label"),
					app,
					f"metadata/report/{row['parent']}/{child_doctype.lower().replace(' ', '_')}/{fieldname}",
					f"report:{row['parent']}:{child_doctype.lower().replace(' ', '_')}:{fieldname}:label"
					if fieldname
					else None,
					"report",
					context_template.format(row["parent"]) if context_template else None,
				)

	for item in navbar_items:
		label = item["item_label"]
		owners = navbar_owners.get(label)
		if not owners:
			raise ValueError(f"standard Navbar Item {label!r} has no owner in pinned app hooks")
		for app in sorted(owners):
			_append(events, label, app, "metadata/navbar", None, "navbar")

	workspace_apps = {}
	for workspace in workspaces:
		app = workspace.get("app")
		if not app and workspace.get("module"):
			app = _app_for_module(frappe, workspace["module"])
		if workspace.get("for_user") or not workspace.get("public") or app not in EXPECTED_RUNTIME_APPS:
			raise ValueError(
				f"runtime site contains local/custom metadata in Workspace {workspace['name']!r}"
			)
		workspace_apps[workspace["name"]] = app
		location = f"metadata/workspace/{workspace['name']}"
		_append(
			events, workspace.get("label"), app, location, f"workspace:{workspace['name']}:label", "workspace"
		)
		try:
			content = json.loads(workspace.get("content") or "[]")
		except (TypeError, json.JSONDecodeError) as error:
			raise ValueError(f"Workspace {workspace['name']!r} has invalid content JSON") from error
		for item in content:
			if item.get("type") in {"header", "paragraph"}:
				_append(events, item.get("data", {}).get("text"), app, location, None, "workspace")
	for child_doctype, child_fields in (
		("Workspace Chart", ("label",)),
		("Workspace Number Card", ("label",)),
		("Workspace Link", ("label", "description")),
		("Workspace Shortcut", ("label", "format")),
		("Workspace Quick List", ("label",)),
	):
		for row in workspace_children[child_doctype]:
			if app := workspace_apps.get(row["parent"]):
				for field in child_fields:
					_append(
						events,
						row.get(field),
						app,
						f"metadata/workspace/{row['parent']}/{child_doctype.lower().replace(' ', '_')}/{row['idx']}",
						None,
						"workspace",
					)

	sidebar_apps = {}
	for sidebar in sidebars:
		app = sidebar.get("app")
		if sidebar.get("for_user") or app not in EXPECTED_RUNTIME_APPS:
			raise ValueError(
				f"runtime site contains local/custom metadata in Workspace Sidebar {sidebar['name']!r}"
			)
		sidebar_apps[sidebar["name"]] = app
		_append(
			events,
			sidebar.get("title"),
			app,
			f"metadata/workspace_sidebar/{sidebar['name']}",
			f"workspace_sidebar:{sidebar['name']}:title",
			"workspace_sidebar",
		)
	for item in sidebar_items:
		if app := sidebar_apps.get(item["parent"]):
			_append(
				events,
				item.get("label"),
				app,
				f"metadata/workspace_sidebar/{item['parent']}/item/{item['idx']}",
				None,
				"workspace_sidebar",
			)

	return RuntimeExtraction(
		sorted(
			events,
			key=lambda event: (
				event.app,
				event.source_location,
				event.stable_locator or "",
				event.source,
			),
		)
	)
