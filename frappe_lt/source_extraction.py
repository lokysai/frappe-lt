import ast
import os
from pathlib import Path, PurePosixPath

from frappe_lt.inventory import ExtractionEvent

PYTHON_TRANSLATION_FUNCTIONS = {"_", "_lt", "N_"}


def _source_stable_locator(location: str, extractor: str, line: int | None) -> str | None:
	path = PurePosixPath(location)
	if (
		path.is_absolute()
		or "\\" in location
		or ".." in path.parts
		or not location
		or not extractor
		or not isinstance(line, int)
		or isinstance(line, bool)
		or line <= 0
	):
		return None
	return f"source:{location}:{extractor}:{line}"


def _literal_string(node: ast.expr | None) -> str | None:
	if node is None:
		return None
	try:
		value = ast.literal_eval(node)
	except ValueError, TypeError:
		return None
	return value if isinstance(value, str) else None


def _translation_call(node: ast.Call) -> tuple[str, str | None] | None:
	if isinstance(node.func, ast.Name):
		function = node.func.id
	elif isinstance(node.func, ast.Attribute):
		function = node.func.attr
	else:
		return None
	if function not in PYTHON_TRANSLATION_FUNCTIONS:
		return None

	source_node = node.args[0] if node.args else None
	for keyword in node.keywords:
		if keyword.arg == "msg" and source_node is None:
			source_node = keyword.value
	source = _literal_string(source_node)
	if source is None:
		return None

	context_position = 1 if function == "N_" else 2
	context = _literal_string(node.args[context_position]) if len(node.args) > context_position else None
	for keyword in node.keywords:
		if keyword.arg == "context":
			context = _literal_string(keyword.value)
	return source, context


def _python_messages(read, filename: str):
	try:
		code = read()
		if isinstance(code, bytes):
			code = code.decode("utf-8")
	except (OSError, UnicodeError) as error:
		raise ValueError(f"python extractor could not read {filename}: {error}") from error
	try:
		tree = ast.parse(code, filename=filename)
	except SyntaxError as error:
		raise ValueError(f"python extractor could not parse {filename}: {error}") from error

	for node in ast.walk(tree):
		if isinstance(node, ast.Call) and (message := _translation_call(node)) is not None:
			yield node.lineno, *message


def extract_python(path: Path, app: str, relative_path: str) -> list[ExtractionEvent]:
	"""Extract Frappe Python calls without treating the positional lang argument as context."""
	events = []
	for line, source, context in _python_messages(path.read_bytes, relative_path):
		events.append(
			ExtractionEvent(
				app=app,
				context=context,
				extractor="python",
				line=line,
				origin="source",
				raw_source=source,
				source=source,
				source_location=relative_path,
				stable_locator=_source_stable_locator(relative_path, "python", line),
			)
		)
	return sorted(events, key=lambda event: (event.line or 0, event.source, event.context or ""))


def extract_babel_python(fileobj, keywords, comment_tags, options):
	"""Pinned Frappe Python extractor adapter with correct lang/context positions."""
	filename = Path(str(getattr(fileobj, "name", "<file>"))).as_posix()
	for line, source, context in _python_messages(fileobj.read, filename):
		if context is None:
			yield line, "gettext", source, []
		else:
			yield line, "pgettext", (context, source), []


def _source_locator(app_path: Path, filename: str) -> str:
	root = app_path.resolve()
	candidate = Path(filename)
	resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
	try:
		return resolved.relative_to(root).as_posix()
	except ValueError as error:
		raise ValueError(f"source file is outside owning app: {filename}") from error


def configured_method_map(app: str):
	"""Return the exact pinned Babel map, including the corrected Python adapter."""
	from frappe.gettext.translate import get_method_map

	default_method_map = get_method_map("frappe")
	method_map = [] if app == "frappe" else get_method_map(app)
	method_map.extend(default_method_map)
	return [
		(pattern, extract_babel_python if method.endswith(".python.extract") else method)
		for pattern, method in method_map
	]


def extract_sources(frappe) -> list[ExtractionEvent]:
	"""Run the pinned Babel mappings for Frappe and ERPNext as source-only extraction."""
	from babel.messages.extract import extract_from_dir
	from frappe.gettext.translate import PYTHON_KEYWORDS, get_is_gitignored_function_for_app

	events = []
	for app in ("frappe", "erpnext"):
		app_path = Path(frappe.get_pymodule_path(app, ".."))
		method_map = configured_method_map(app)
		is_gitignored = get_is_gitignored_function_for_app(app)

		def directory_filter(directory, is_ignored=is_gitignored):
			name = os.path.basename(directory)
			return not (name.startswith((".", "_")) or is_ignored(str(directory)))

		current_file = None
		extractors = {}

		def callback(filename, method, options, extractors=extractors, app_root=app_path):
			nonlocal current_file
			current_file = _source_locator(app_root, filename)
			extractors[filename] = getattr(method, "__name__", str(method)).removesuffix(".extract")

		try:
			rows = extract_from_dir(
				app_path,
				method_map,
				callback=callback,
				directory_filter=directory_filter,
				keywords=PYTHON_KEYWORDS,
			)
			for filename, line, message, _comments, context in rows:
				if not message or not frappe.as_unicode(message).strip():
					continue
				if isinstance(message, tuple):
					raise ValueError(f"source extractor does not support plural message in {filename}")
				location = _source_locator(app_path, filename)
				if filename not in extractors:
					raise ValueError(f"Babel did not report an extractor for {filename}")
				extractor = extractors[filename]
				events.append(
					ExtractionEvent(
						app=app,
						context=context,
						extractor=extractor,
						line=line,
						origin="source",
						raw_source=message,
						source=message,
						source_location=location,
						stable_locator=_source_stable_locator(location, extractor, line),
					)
				)
		except Exception as error:
			raise ValueError(
				f"source extractor failed for {app}/{current_file or '<directory>'}: {error}"
			) from error
	return sorted(
		events,
		key=lambda event: (
			event.app,
			event.source_location,
			event.line is None,
			event.line or 0,
			event.source,
			event.context or "",
		),
	)
