import datetime
import os
import subprocess
import sys
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from babel.messages.catalog import Catalog
from babel.messages.pofile import read_po, write_po

FIXED_PO_DATE = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)


@dataclass(frozen=True)
class ParsedPO:
	locale: str
	creation_date: str
	revision_date: str
	messages: dict[tuple[str, str | None], str]


def build_po(entries: list[dict]) -> bytes:
	"""Build the deterministic Lithuanian candidate catalog."""
	catalog = Catalog(
		locale="lt",
		creation_date=FIXED_PO_DATE,
		revision_date=FIXED_PO_DATE,
		project="frappe_lt",
		version="0",
	)
	for entry in entries:
		key = entry["key"]
		catalog.add(key["source"], entry["translation"], context=key.get("context"))
	output = BytesIO()
	write_po(output, catalog, sort_output=True, width=None, omit_header=False)
	return output.getvalue().rstrip(b"\n") + b"\n"


def parse_po(path: Path) -> ParsedPO:
	"""Parse a PO through the shared strict boundary used by gates and smoke tests."""
	with path.open("rb") as stream:
		catalog = read_po(stream, locale="lt", abort_invalid=True)
	if str(catalog.locale) != "lt":
		raise ValueError("PO catalog language must be lt")
	result = {}
	for message in catalog:
		if not message.id:
			continue
		if "fuzzy" in message.flags:
			raise ValueError("PO message must not be fuzzy")
		if isinstance(message.id, tuple) or isinstance(message.string, tuple):
			raise ValueError("PO message must not be plural")
		key = (message.id, message.context)
		if key in result:
			raise ValueError(f"PO catalog contains duplicate message {key!r}")
		result[key] = message.string
	return ParsedPO(
		locale=str(catalog.locale),
		creation_date=catalog.creation_date.strftime("%Y-%m-%d %H:%M%z"),
		revision_date=catalog.revision_date.strftime("%Y-%m-%d %H:%M%z"),
		messages=result,
	)


def compile_po(
	po_path: Path,
	workspace: Path,
	*,
	mo_path: Path | None = None,
	compiler=None,
) -> Path:
	"""Compile one PO with Frappe and require a newly created MO output."""
	mo_path = mo_path or (workspace / "sites" / "assets" / "locale" / "lt" / "LC_MESSAGES" / "frappe_lt.mo")
	mo_path.unlink(missing_ok=True)
	if compiler is not None:
		compiler(po_path, workspace)
	else:
		script = """
import os
from pathlib import Path
from frappe.gettext import translate

po_path = Path(os.environ["FRAPPE_LT_CANDIDATE_PO"])
mo_path = Path(os.environ["FRAPPE_LT_CANDIDATE_MO"])
translate.get_po_path = lambda app, locale=None: po_path
translate.get_mo_path = lambda app, locale=None: mo_path
translate._compile_translation("frappe_lt", "lt", True)
"""
		home = workspace / "home"
		xdg_cache = workspace / "xdg-cache"
		xdg_config = workspace / "xdg-config"
		xdg_data = workspace / "xdg-data"
		for directory in (home, xdg_cache, xdg_config, xdg_data):
			directory.mkdir(parents=True, exist_ok=True)
		environment = {
			"FRAPPE_LT_CANDIDATE_PO": str(po_path),
			"FRAPPE_LT_CANDIDATE_MO": str(mo_path),
			"HOME": str(home),
			"LANG": os.environ.get("LANG", "C.UTF-8"),
			"LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
			"SOURCE_DATE_EPOCH": "1704067200",
			"XDG_CACHE_HOME": str(xdg_cache),
			"XDG_CONFIG_HOME": str(xdg_config),
			"XDG_DATA_HOME": str(xdg_data),
		}
		subprocess.run(
			[sys.executable, "-I", "-c", script],
			cwd=workspace,
			env=environment,
			check=True,
			capture_output=True,
			text=True,
		)
	if not mo_path.is_file():
		raise RuntimeError(f"Frappe gettext compiler did not create {mo_path}")
	return mo_path
