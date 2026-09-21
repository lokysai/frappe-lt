import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from frappe_lt.inventory import _json_object, canonical_json

JOURNAL_SCHEMA_VERSION = 2
RUN_MARKER_PREFIX = "frappe-lt-runtime-"
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_LOOKUPS = 5000
LOGIN_USER_FIELDS = ("last_active", "last_ip", "last_login")
_EMAIL_CAPTURE_LOCK = threading.Lock()
_LOGIN_LOCK = threading.Lock()
_PRINT_CAPTURE_LOCK = threading.Lock()


def _runtime_default_value_name(marker: str, user: str, key: str) -> str:
	digest = hashlib.sha256(f"{user}\0{key}".encode()).hexdigest()
	return f"{marker}-default-{digest}"


def redact_sensitive(value: object, *, max_chars: int = 2048) -> str:
	text = str(value)
	text = re.sub(
		r"(?i)\b(authorization\s*:\s*)(?:bearer\s+)?[^\s,;}]+",
		r"\1[REDACTED]",
		text,
	)
	text = re.sub(r"(?i)\b((?:set-)?cookie\s*:\s*)[^\r\n]+", r"\1[REDACTED]", text)
	text = re.sub(
		r"""(?ix)(["']?(?:password|pwd|token|csrf_token|api_key|api_secret|access_token|reset_token)["']?\s*[:=]\s*)(["'])(.*?)\2""",
		r"\1\2[REDACTED]\2",
		text,
	)
	text = re.sub(
		r"(?i)\b(password|pwd|token|csrf_token|api_key|api_secret|access_token|reset_token|key|sid)\b\s*[:=]\s*[^\s,;}<>&\"']+",
		r"\1=[REDACTED]",
		text,
	)
	text = re.sub(
		r"(?i)([?&](?:key|token|csrf_token|api_key|api_secret|access_token|reset_token)=)[^&\s<>\"']+",
		r"\1[REDACTED]",
		text,
	)
	text = re.sub(
		r"(?i)((?:%3f|%26)(?:key|token|csrf_token|api_key|api_secret|access_token|reset_token)%3d)[^%\s<>\"']+",
		r"\1[REDACTED]",
		text,
	)
	text = re.sub(
		r"""(?ix)((?:data-)?(?:csrf[-_]token|api[-_]key|api[-_]secret|access[-_]token|reset[-_]token)=["'])[^"']*""",
		r"\1[REDACTED]",
		text,
	)
	text = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED]", text)
	text = re.sub(r"(?i)\b[A-Z0-9._+-]+%40[A-Z0-9.-]+(?:\.|%2e)[A-Z]{2,}\b", "[REDACTED]", text)
	text = re.sub(r"(?i)frappe-lt-runtime-[0-9a-f]{32}[A-Za-z0-9@._:-]*", "[REDACTED]", text)
	return text[:max_chars]


def _write_durable(path: Path, value: dict) -> None:
	path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
	temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	try:
		with temporary.open("wb") as stream:
			stream.write(canonical_json(value))
			stream.flush()
			os.fsync(stream.fileno())
		os.chmod(temporary, 0o600)
		os.replace(temporary, path)
		directory_fd = os.open(path.parent, os.O_RDONLY)
		try:
			os.fsync(directory_fd)
		finally:
			os.close(directory_fd)
	finally:
		temporary.unlink(missing_ok=True)


def _load_journal(path: Path) -> dict:
	try:
		journal = json.loads(path.read_bytes(), object_pairs_hook=_json_object)
	except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
		raise ValueError(f"could not read durable runtime journal: {error}") from error
	if not isinstance(journal, dict) or set(journal) != {
		"created_at_epoch",
		"login_baseline",
		"mutations",
		"original_error",
		"run_id",
		"schema_version",
		"site",
		"state",
	}:
		raise ValueError("durable runtime journal has malformed fields")
	if journal["schema_version"] != JOURNAL_SCHEMA_VERSION:
		raise ValueError("unsupported durable runtime journal schema")
	if not isinstance(journal["run_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", journal["run_id"]):
		raise ValueError("durable runtime journal run_id is invalid")
	if not isinstance(journal["site"], str) or not journal["site"] or "/" in journal["site"]:
		raise ValueError("durable runtime journal site is invalid")
	if journal["state"] not in {"active", "cleaning", "failed"}:
		raise ValueError("durable runtime journal has invalid state")
	if not isinstance(journal["created_at_epoch"], int) or journal["created_at_epoch"] < 0:
		raise ValueError("durable runtime journal creation time is invalid")
	if journal["original_error"] is not None and not isinstance(journal["original_error"], str):
		raise ValueError("durable runtime journal original error is invalid")
	baseline = journal["login_baseline"]
	if not isinstance(baseline, dict) or set(baseline) != {
		"activity_logs",
		"sessions",
		"user_values",
		"users",
	}:
		raise ValueError("durable runtime journal login baseline is invalid")
	for field in ("activity_logs", "sessions", "users"):
		if not isinstance(baseline[field], list) or not all(
			isinstance(value, str) and value for value in baseline[field]
		):
			raise ValueError("durable runtime journal login baseline values are invalid")
	if not isinstance(baseline["user_values"], dict) or set(baseline["user_values"]) != set(
		baseline["users"]
	):
		raise ValueError("durable runtime journal login user values are invalid")
	for values in baseline["user_values"].values():
		if not isinstance(values, dict) or set(values) != {"after", "before"}:
			raise ValueError("durable runtime journal login user before-images are invalid")
		if not isinstance(values["before"], dict) or set(values["before"]) != set(LOGIN_USER_FIELDS):
			raise ValueError("durable runtime journal login user before-image fields are invalid")
		if values["after"] is not None and (
			not isinstance(values["after"], dict) or set(values["after"]) != set(LOGIN_USER_FIELDS)
		):
			raise ValueError("durable runtime journal login user after-image fields are invalid")
	if not isinstance(journal["mutations"], list):
		raise ValueError("durable runtime journal mutations must be a list")
	ids = []
	for mutation in journal["mutations"]:
		if not isinstance(mutation, dict) or set(mutation) != {"before", "id", "kind", "target"}:
			raise ValueError("durable runtime journal mutation has malformed fields")
		if mutation["kind"] != "document":
			raise ValueError("durable runtime journal mutation kind is invalid")
		if not isinstance(mutation["id"], int) or mutation["id"] < 1:
			raise ValueError("durable runtime journal mutation id is invalid")
		if not isinstance(mutation["target"], dict) or set(mutation["target"]) != {"doctype", "name"}:
			raise ValueError("durable runtime journal target is invalid")
		if (
			not isinstance(mutation["target"]["doctype"], str)
			or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]{0,139}", mutation["target"]["doctype"])
			or not isinstance(mutation["target"]["name"], str)
			or not mutation["target"]["name"]
			or len(mutation["target"]["name"]) > 255
		):
			raise ValueError("durable runtime journal target identity is invalid")
		if mutation["before"] is not None and not isinstance(mutation["before"], dict):
			raise ValueError("durable runtime journal before-image is invalid")
		if mutation["before"] is not None and (
			mutation["before"].get("doctype") != mutation["target"]["doctype"]
			or mutation["before"].get("name") != mutation["target"]["name"]
		):
			raise ValueError("durable runtime journal before-image identity does not match target")
		ids.append(mutation["id"])
	if ids != list(range(1, len(ids) + 1)):
		raise ValueError("durable runtime journal mutation ids are not contiguous")
	return journal


class SiteControl:
	def __init__(self, frappe, site: str, run_id: str, *, site_path: Path | None = None):
		self.frappe = frappe
		self.site = site
		self.run_id = run_id
		self.marker = f"{RUN_MARKER_PREFIX}{run_id}"
		root = (site_path or Path(frappe.get_site_path())).resolve()
		self.root = root / "private" / "frappe_lt_runtime"
		self.journal_path = self.root / "journal.json"
		self.journal_lock_path = self.root / "journal.lock"
		self.lock_path = self.root / "mutable.lock"
		self.operation_lock_path = self.root / "operation.lock"
		self.secret_path = self.root / f"{run_id}.browser.json"
		self.journal = None
		self.recoveries = []

	@contextmanager
	def lease(self):
		self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
		with self.lock_path.open("a+b") as lock:
			try:
				fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
			except BlockingIOError as error:
				raise RuntimeError(f"another mutable {self.site} runtime run holds the lease") from error
			try:
				yield
			finally:
				fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

	@contextmanager
	def journal_lock(self):
		self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
		with self.journal_lock_path.open("a+b") as lock:
			fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
			try:
				yield
			finally:
				fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

	@contextmanager
	def operation(self, *, exclusive: bool = False):
		self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
		with self.operation_lock_path.open("a+b") as lock:
			fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
			try:
				yield
			finally:
				fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

	@contextmanager
	def suppress_process_effects(self):
		effects = {"enqueue": [], "mail": [], "share": []}
		original_enqueue = self.frappe.enqueue
		original_sendmail = self.frappe.sendmail
		share = getattr(self.frappe, "share", None)
		original_add_docshare = getattr(share, "add_docshare", None)
		self.frappe.enqueue = lambda *args, **kwargs: effects["enqueue"].append((args, kwargs))
		self.frappe.sendmail = lambda *args, **kwargs: effects["mail"].append((args, kwargs))
		if original_add_docshare is not None:
			share.add_docshare = lambda *args, **kwargs: effects["share"].append((args, kwargs))
		try:
			yield effects
		finally:
			self.frappe.enqueue = original_enqueue
			self.frappe.sendmail = original_sendmail
			if original_add_docshare is not None:
				share.add_docshare = original_add_docshare

	@contextmanager
	def journal_default_writes(self, user: str):
		writes = {"add": [], "clear": []}
		original_add_default = self.frappe.defaults.add_default
		original_clear_default = self.frappe.defaults.clear_default

		def add_default(key, value, parent, parenttype=None):
			if parent != user:
				raise RuntimeError("test identity creation attempted a default write for another owner")
			writes["add"].append((key, value, parent, parenttype))
			self._create_role_profile_defaults(parent, {key: value})

		self.frappe.defaults.add_default = add_default
		self.frappe.defaults.clear_default = lambda *args, **kwargs: writes["clear"].append((args, kwargs))
		try:
			yield writes
		finally:
			self.frappe.defaults.add_default = original_add_default
			self.frappe.defaults.clear_default = original_clear_default

	def recover_stale(self) -> list[dict]:
		with self.operation(exclusive=True):
			return self._recover_stale_locked()

	def _recover_stale_locked(self) -> list[dict]:
		with self.journal_lock():
			if not self.journal_path.exists():
				return []
			journal = _load_journal(self.journal_path)
			if journal["site"] != self.site:
				return [
					{
						"error": f"stale runtime journal belongs to unexpected site {journal['site']!r}",
						"mutation_id": 0,
						"target": {"doctype": "Runtime Journal", "name": journal["run_id"]},
					}
				]
			journal["state"] = "cleaning"
			_write_durable(self.journal_path, journal)
		stale_secret = self.root / f"{journal['run_id']}.browser.json"
		failures = []
		try:
			stale_secret.unlink(missing_ok=True)
		except OSError as error:
			failures.append(
				{
					"error": f"could not revoke stale browser capability: {error}"[:2048],
					"mutation_id": 0,
					"target": {"doctype": "Runtime Browser Plan", "name": stale_secret.name},
				}
			)
		failures.extend(self._cleanup_login_effects(journal))
		failures.extend(self._restore(journal))
		failures.extend(
			self._residue_failures(journal["run_id"], marker=f"{RUN_MARKER_PREFIX}{journal['run_id']}")
		)
		self.recoveries.append(
			{
				"cleanup_failures": failures,
				"mutation_count": len(journal["mutations"]),
				"original_error": journal["original_error"],
				"run_id": journal["run_id"],
			}
		)
		if failures:
			with self.journal_lock():
				journal = _load_journal(self.journal_path)
				journal["state"] = "failed"
				_write_durable(self.journal_path, journal)
			return failures
		try:
			with self.journal_lock():
				self.journal_path.unlink()
		except OSError as error:
			return [
				{
					"error": f"could not remove recovered runtime journal: {error}"[:2048],
					"mutation_id": 0,
					"target": {"doctype": "Runtime Journal", "name": journal["run_id"]},
				}
			]
		return []

	def start(self) -> None:
		with self.journal_lock():
			if self.journal_path.exists():
				raise RuntimeError("stale runtime journal must be recovered before a new run")
			self.journal = {
				"created_at_epoch": int(time.time()),
				"login_baseline": {
					"activity_logs": [],
					"sessions": [],
					"user_values": {},
					"users": [],
				},
				"mutations": [],
				"original_error": None,
				"run_id": self.run_id,
				"schema_version": JOURNAL_SCHEMA_VERSION,
				"site": self.site,
				"state": "active",
			}
			_write_durable(self.journal_path, self.journal)

	def set_original_error(self, error: str) -> None:
		with self.journal_lock():
			if not self.journal_path.exists():
				return
			self.journal = _load_journal(self.journal_path)
			if self.journal is None or self.journal["original_error"] is not None:
				return
			self.journal["original_error"] = redact_sensitive(error)
			_write_durable(self.journal_path, self.journal)

	def before_document_mutation(self, doctype: str, name: str) -> None:
		with self.journal_lock():
			if not self.journal_path.exists():
				raise RuntimeError("runtime journal has not started")
			self.journal = _load_journal(self.journal_path)
			if self.journal["run_id"] != self.run_id or self.journal["state"] != "active":
				raise RuntimeError("runtime journal is not active for this run")
			before = None
			if self.frappe.db.exists(doctype, name):
				before = self.frappe.get_doc(doctype, name).as_dict(convert_dates_to_str=True, no_nulls=False)
			mutation = {
				"before": before,
				"id": len(self.journal["mutations"]) + 1,
				"kind": "document",
				"target": {"doctype": doctype, "name": name},
			}
			self.journal["mutations"].append(mutation)
			_write_durable(self.journal_path, self.journal)

	def before_runtime_login(self, user: str, sid: str) -> None:
		with self.journal_lock():
			self.journal = _load_journal(self.journal_path)
			if self.journal["run_id"] != self.run_id or self.journal["state"] != "active":
				raise RuntimeError("runtime journal is not active for this run")
			baseline = self.journal["login_baseline"]
			if user not in baseline["users"] or sid in baseline["sessions"]:
				raise RuntimeError("runtime login is not authorized by the journal")
			baseline["sessions"].append(sid)
			baseline["sessions"].sort()
			_write_durable(self.journal_path, self.journal)

	def after_runtime_login(self, user: str) -> None:
		with self.journal_lock():
			self.journal = _load_journal(self.journal_path)
			if self.journal["run_id"] != self.run_id or self.journal["state"] != "active":
				raise RuntimeError("runtime journal is not active for this run")
			values = self.journal["login_baseline"]["user_values"].get(user)
			if values is None:
				raise RuntimeError("runtime login user is not authorized by the journal")
			values["after"] = dict(self.frappe.db.get_value("User", user, LOGIN_USER_FIELDS, as_dict=True))
			_write_durable(self.journal_path, self.journal)

	def _create_role_profile_defaults(self, user: str, defaults: dict) -> None:
		for key, value in sorted(defaults.items()):
			name = _runtime_default_value_name(self.marker, user, key)
			self.before_document_mutation("DefaultValue", name)
			document = self.frappe.get_doc(
				{
					"defkey": key,
					"defvalue": value,
					"doctype": "DefaultValue",
					"parent": user,
					"parentfield": "defaults",
					"parenttype": "User",
				}
			)
			document.name = name
			document.flags.name_set = True
			document.insert(ignore_permissions=True)
		self.frappe.clear_cache(user=user)

	def prepare(self, profiles: dict, scenarios: dict, *, diagnostic_sampling: bool = False) -> dict:
		if not isinstance(diagnostic_sampling, bool):
			raise ValueError("diagnostic_sampling must be boolean")
		credentials = {}
		fixtures = {}
		fixture_ids = {
			scenario["fixture_id"] for scenario in scenarios["scenarios"] if scenario["fixture_id"]
		}
		for profile in profiles["profiles"]:
			for role in profile["roles"]:
				if not self.frappe.db.exists("Role", role):
					raise RuntimeError(
						f"Runtime Role Profile {profile['id']!r} requires missing role {role!r}"
					)
		customer = None
		customer_group = "Commercial"
		territory = "Rest Of The World"
		if "portal-contact" in fixture_ids:
			for doctype, name in (
				("Customer Group", customer_group),
				("Territory", territory),
			):
				if not self.frappe.db.exists(doctype, name):
					raise RuntimeError(f"fixture portal-contact requires {doctype} {name!r}")
				if self.frappe.db.get_value(doctype, name, "is_group"):
					raise RuntimeError(f"fixture portal-contact requires leaf {doctype} {name!r}")
			customer = f"{self.marker}-customer"
			self.before_document_mutation("Customer", customer)
			document = self.frappe.get_doc(
				{
					"customer_group": customer_group,
					"customer_name": customer,
					"customer_type": "Company",
					"doctype": "Customer",
					"territory": territory,
				}
			)
			document.name = customer
			document.flags.name_set = True
			with self.suppress_process_effects() as effects:
				document.insert(ignore_permissions=True)
			if any(effects.values()):
				raise RuntimeError("fixture Customer attempted an external side effect")
			actual_customer = self.frappe.get_doc("Customer", customer)
			if (
				actual_customer.customer_name != customer
				or actual_customer.customer_group != customer_group
				or actual_customer.territory != territory
			):
				raise RuntimeError("fixture portal Customer does not match its exact postconditions")
		if "item-draft" in fixture_ids:
			for doctype, name in (("Item Group", "All Item Groups"), ("UOM", "Nos")):
				if not self.frappe.db.exists(doctype, name):
					raise RuntimeError(f"fixture item-draft requires {doctype} {name!r}")

		for profile in profiles["profiles"]:
			if profile["administrator"]:
				administrator = self.frappe.get_doc("User", "Administrator")
				effective = self.frappe.defaults.get_defaults_for("Administrator")
				expected_defaults = {**profile["defaults"], "time_zone": profile["time_zone"]}
				if (
					administrator.default_app != profile["default_app"]
					or administrator.time_zone != profile["time_zone"]
					or administrator.language != profile["language"]
					or administrator.module_profile != profile["module_profile"]
					or administrator.user_type != profile["user_type"]
					or {key: effective.get(key) for key in expected_defaults} != expected_defaults
				):
					raise RuntimeError("Administrator does not match its Runtime Role Profile")
				credentials[profile["id"]] = {"user": "Administrator"}
				continue
			user = f"{self.marker}-{profile['id']}@invalid.example"
			password = secrets.token_urlsafe(24)
			self.before_document_mutation("Notification Settings", user)
			self.before_document_mutation("User", user)
			with (
				self.suppress_process_effects() as effects,
				self.journal_default_writes(user) as default_writes,
			):
				self.frappe.get_doc(
					{
						"default_app": profile["default_app"],
						"doctype": "User",
						"email": user,
						"first_name": self.marker,
						"language": profile["language"],
						"module_profile": profile["module_profile"],
						"new_password": password,
						"roles": [{"role": role} for role in profile["roles"]],
						"send_welcome_email": 0,
						"time_zone": profile["time_zone"],
						"user_type": profile["user_type"],
					}
				).insert(ignore_permissions=True)
			queued_methods = [
				args[0] if args else kwargs.get("method") for args, kwargs in effects["enqueue"]
			]
			if any(method != "frappe.core.doctype.user.user.create_contact" for method in queued_methods):
				raise RuntimeError("test identity creation attempted an unexpected queued effect")
			if effects["mail"]:
				raise RuntimeError("test identity creation attempted outbound mail")
			if len(effects["share"]) != 1:
				raise RuntimeError("test identity creation did not emit exactly one suppressible self-share")
			expected_defaults = {**profile["defaults"], "time_zone": profile["time_zone"]}
			expected_default_writes = {(key, value, user, None) for key, value in expected_defaults.items()}
			if (
				default_writes["clear"]
				or len(set(default_writes["add"])) != len(default_writes["add"])
				or set(default_writes["add"]) != expected_default_writes
			):
				raise RuntimeError("test identity creation attempted unexpected implicit default writes")
			actual = self.frappe.get_doc("User", user)
			effective = self.frappe.defaults.get_defaults_for(user)
			if (
				actual.default_app != profile["default_app"]
				or actual.time_zone != profile["time_zone"]
				or actual.language != profile["language"]
				or actual.module_profile != profile["module_profile"]
				or actual.user_type != profile["user_type"]
				or sorted(role.role for role in actual.roles) != profile["roles"]
				or {key: effective.get(key) for key in expected_defaults} != expected_defaults
			):
				raise RuntimeError(f"test identity does not match Runtime Role Profile {profile['id']!r}")
			if profile["portal_link"] is not None:
				if customer is None:
					raise RuntimeError("portal Runtime Role Profile requires portal-contact fixture")
				contact_name = f"{self.marker}-contact"
				contact = self.frappe.get_doc(
					{
						"doctype": profile["portal_link"]["doctype"],
						"email_ids": [{"email_id": user, "is_primary": 1}],
						"first_name": self.marker,
						"links": [
							{
								"link_doctype": profile["portal_link"]["dynamic_link_doctype"],
								"link_name": customer,
							}
						],
						"user": user,
					}
				)
				contact.name = contact_name
				contact.flags.name_set = True
				self.before_document_mutation(contact.doctype, contact.name)
				with self.suppress_process_effects() as effects:
					contact.insert(ignore_permissions=True)
				if any(effects.values()):
					raise RuntimeError("portal identity creation attempted an external side effect")
				actual_contact = self.frappe.get_doc("Contact", contact_name)
				if (
					actual_contact.user != user
					or [(row.email_id, row.is_primary) for row in actual_contact.email_ids] != [(user, 1)]
					or [(row.link_doctype, row.link_name) for row in actual_contact.links]
					!= [(profile["portal_link"]["dynamic_link_doctype"], customer)]
				):
					raise RuntimeError("portal identity does not have its exact Contact and dynamic link")
				fixtures["portal-contact"] = {
					"contact": contact_name,
					"customer": customer,
					"customer_group": customer_group,
					"territory": territory,
					"user": user,
				}
			credentials[profile["id"]] = {"user": user}

		if "item-draft" in fixture_ids:
			name = self.marker
			self.before_document_mutation("Item", name)
			document = self.frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": name,
					"item_name": name,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
				}
			)
			document.name = name
			document.flags.name_set = True
			with self.suppress_process_effects() as effects:
				document.insert(ignore_permissions=True)
			if any(effects.values()):
				raise RuntimeError("fixture item-draft attempted an external side effect")
			actual = self.frappe.get_doc("Item", name)
			if (
				actual.docstatus != 0
				or actual.item_code != name
				or actual.item_name != name
				or actual.item_group != "All Item Groups"
				or actual.stock_uom != "Nos"
			):
				raise RuntimeError("fixture item-draft does not match its exact postconditions")
			fixtures["item-draft"] = {"item_name": name}
		if "todo-draft" in fixture_ids:
			name = f"{self.marker}-todo"
			self.before_document_mutation("ToDo", name)
			document = self.frappe.get_doc(
				{
					"description": self.marker,
					"doctype": "ToDo",
					"priority": "Medium",
					"status": "Open",
				}
			)
			document.name = name
			document.flags.name_set = True
			with self.suppress_process_effects() as effects:
				document.insert(ignore_permissions=True)
			if any(effects.values()):
				raise RuntimeError("fixture todo-draft attempted an external side effect")
			actual = self.frappe.get_doc("ToDo", name)
			if actual.docstatus != 0 or actual.description != self.marker or actual.status != "Open":
				raise RuntimeError("fixture todo-draft does not match its exact postconditions")
			fixtures["todo-draft"] = {"doctype": "ToDo", "name": name}
		fixtures["runtime-user"] = {"marker": self.marker}
		if "portal-customer" in credentials:
			fixtures["runtime-user"]["user"] = credentials["portal-customer"]["user"]
		login_users = sorted(credential["user"] for credential in credentials.values())
		with self.journal_lock():
			self.journal = _load_journal(self.journal_path)
			self.journal["login_baseline"] = {
				"activity_logs": [],
				"sessions": [],
				"user_values": {
					user: {
						"after": None,
						"before": dict(
							self.frappe.db.get_value("User", user, LOGIN_USER_FIELDS, as_dict=True)
						),
					}
					for user in login_users
				},
				"users": login_users,
			}
			_write_durable(self.journal_path, self.journal)
		self.frappe.db.commit()
		browser = {
			"credentials": credentials,
			"diagnostic_sampling": diagnostic_sampling,
			"fixtures": fixtures,
			"run_id": self.run_id,
			"schema_version": 2,
			"scenarios": scenarios["scenarios"],
			"token": secrets.token_urlsafe(32),
		}
		_write_durable(self.secret_path, browser)
		return {"browser_plan": str(self.secret_path), "fixtures": sorted(fixtures)}

	def cleanup(self) -> list[dict]:
		with self.operation(exclusive=True):
			return self._cleanup_locked()

	def _cleanup_locked(self) -> list[dict]:
		with self.journal_lock():
			if not self.journal_path.exists():
				return []
			self.journal = _load_journal(self.journal_path)
			if self.journal["site"] != self.site or self.journal["run_id"] != self.run_id:
				return [
					{
						"error": "runtime journal does not belong to this site and run",
						"mutation_id": 0,
						"target": {"doctype": "Runtime Journal", "name": self.journal["run_id"]},
					}
				]
			self.journal["state"] = "cleaning"
			_write_durable(self.journal_path, self.journal)
		failures = self._cleanup_login_effects(self.journal)
		failures.extend(self._restore(self.journal))
		try:
			self.secret_path.unlink(missing_ok=True)
		except OSError as error:
			failures.append(
				{
					"error": f"could not revoke browser capability: {error}"[:2048],
					"mutation_id": 0,
					"target": {"doctype": "Runtime Browser Plan", "name": self.secret_path.name},
				}
			)
		failures.extend(self._residue_failures(self.run_id))
		if failures:
			with self.journal_lock():
				if self.journal_path.exists():
					self.journal = _load_journal(self.journal_path)
					self.journal["state"] = "failed"
					_write_durable(self.journal_path, self.journal)
			return sorted(
				failures,
				key=lambda failure: (
					failure["target"]["doctype"],
					failure["target"]["name"],
					failure["mutation_id"],
				),
			)
		with self.journal_lock():
			self.journal_path.unlink(missing_ok=True)
		return []

	def _cleanup_login_effects(self, journal: dict) -> list[dict]:
		baseline = journal["login_baseline"]
		if not baseline["users"]:
			return []
		failures = []
		try:
			if baseline["sessions"]:
				self.frappe.db.delete("Sessions", {"sid": ("in", baseline["sessions"])})
				for sid in baseline["sessions"]:
					self.frappe.cache.hdel("session", sid)
			if baseline["activity_logs"]:
				self.frappe.db.delete("Activity Log", {"name": ("in", baseline["activity_logs"])})
			for user, values in sorted(baseline["user_values"].items()):
				if not self.frappe.db.exists("User", user):
					continue
				if values["after"] is None:
					self.frappe.db.set_value("User", user, values["before"], update_modified=False)
					continue
				current = dict(self.frappe.db.get_value("User", user, LOGIN_USER_FIELDS, as_dict=True))
				if current == values["before"]:
					continue
				if current != values["after"]:
					raise RuntimeError(f"login metadata for {user!r} changed concurrently")
				self.frappe.db.set_value("User", user, values["before"], update_modified=False)
			self.frappe.db.commit()
			remaining_sessions = (
				[
					row[0]
					for row in self.frappe.db.sql(
						"select sid from tabSessions where sid in %(sids)s order by sid",
						{"sids": tuple(baseline["sessions"])},
					)
				]
				if baseline["sessions"]
				else []
			)
			remaining_logs = (
				self.frappe.get_all(
					"Activity Log",
					filters={"name": ("in", baseline["activity_logs"])},
					pluck="name",
					order_by="name asc",
				)
				if baseline["activity_logs"]
				else []
			)
			if remaining_sessions or remaining_logs:
				raise RuntimeError("run-created login effects remain after cleanup")
		except Exception as error:
			self.frappe.db.rollback()
			failures.append(
				{
					"error": str(error)[:2048],
					"mutation_id": 0,
					"target": {"doctype": "Runtime Login Effects", "name": journal["run_id"]},
				}
			)
		return failures

	def _restore(self, journal: dict) -> list[dict]:
		failures = []
		for mutation in reversed(journal["mutations"]):
			target = mutation["target"]
			queued = []
			mailed = []
			shared = []
			original_enqueue = getattr(self.frappe, "enqueue", None)
			original_sendmail = getattr(self.frappe, "sendmail", None)
			share = getattr(self.frappe, "share", None)
			original_add_docshare = getattr(share, "add_docshare", None)

			def suppress_enqueue(*args, _queued=queued, **kwargs):
				_queued.append((args, kwargs))

			def suppress_sendmail(*args, _mailed=mailed, **kwargs):
				_mailed.append((args, kwargs))

			self.frappe.enqueue = suppress_enqueue
			self.frappe.sendmail = suppress_sendmail
			if original_add_docshare is not None:
				share.add_docshare = lambda *args, _shared=shared, **kwargs: _shared.append((args, kwargs))
			try:
				default_parent = mutation["before"].get("parent") if mutation["before"] else None
				if mutation["before"] is None:
					if self.frappe.db.exists(target["doctype"], target["name"]):
						if target["doctype"] == "DefaultValue":
							default_parent = self.frappe.get_doc(target["doctype"], target["name"]).parent
						self.frappe.delete_doc(
							target["doctype"],
							target["name"],
							delete_permanently=True,
							force=True,
							ignore_permissions=True,
						)
					self.frappe.db.delete(
						"Deleted Document",
						{"deleted_name": target["name"]},
					)
				else:
					before = mutation["before"]
					if self.frappe.db.exists(target["doctype"], target["name"]):
						current = self.frappe.get_doc(target["doctype"], target["name"])
						current.update(before)
						current.save(ignore_permissions=True)
					else:
						self.frappe.get_doc(before).insert(ignore_permissions=True)
				queued_methods = [args[0] if args else kwargs.get("method") for args, kwargs in queued]
				allowed_queued = {"frappe.model.delete_doc.delete_dynamic_links"}
				if target["doctype"] == "User" and mutation["before"] is not None:
					allowed_queued.add("frappe.core.doctype.user.user.create_contact")
				if any(method not in allowed_queued for method in queued_methods):
					raise RuntimeError("cleanup attempted an unexpected queued effect")
				if "frappe.model.delete_doc.delete_dynamic_links" in queued_methods:
					from frappe.model.delete_doc import delete_dynamic_links

					for args, kwargs in queued:
						method = args[0] if args else kwargs.get("method")
						if method == "frappe.model.delete_doc.delete_dynamic_links":
							delete_dynamic_links(kwargs["doctype"], kwargs["name"])
				if mailed:
					raise RuntimeError("cleanup attempted outbound mail")
				if default_parent is not None:
					self.frappe.clear_cache(user=default_parent)
				self.frappe.db.commit()
			except Exception as error:
				self.frappe.db.rollback()
				failures.append(
					{
						"error": str(error)[:2048],
						"mutation_id": mutation["id"],
						"target": target,
					}
				)
			finally:
				if original_enqueue is None:
					del self.frappe.enqueue
				else:
					self.frappe.enqueue = original_enqueue
				if original_sendmail is None:
					del self.frappe.sendmail
				else:
					self.frappe.sendmail = original_sendmail
				if original_add_docshare is not None:
					share.add_docshare = original_add_docshare
		return sorted(failures, key=lambda failure: failure["mutation_id"])

	def _residue_failures(self, run_id: str, *, marker: str | None = None) -> list[dict]:
		try:
			return self.residue_scan(marker=marker)
		except Exception as error:
			return [
				{
					"error": str(error)[:2048],
					"mutation_id": 0,
					"target": {"doctype": "Runtime Residue Scan", "name": run_id},
				}
			]

	def residue_scan(self, *, marker: str | None = None) -> list[dict]:
		residue = []
		marker = marker or self.marker
		for doctype in (
			"Contact",
			"Customer",
			"Customer Group",
			"Item",
			"Notification Settings",
			"Territory",
			"ToDo",
			"User",
		):
			field = "name"
			for name in self.frappe.get_all(
				doctype, filters={field: ("like", f"{marker}%")}, pluck="name", order_by="name asc"
			):
				residue.append(
					{
						"error": "run-marked record remains after cleanup",
						"mutation_id": 0,
						"target": {"doctype": doctype, "name": name},
					}
				)
		for doctype, field in (
			("Access Log", "reference_document"),
			("Activity Log", "user"),
			("Allowed To Transact With", "parent"),
			("Block Module", "parent"),
			("Communication", "reference_name"),
			("Contact Email", "parent"),
			("Contact Phone", "parent"),
			("Customer Credit Limit", "parent"),
			("DefaultValue", "parent"),
			("DocShare", "user"),
			("Dynamic Link", "parent"),
			("Dynamic Link", "link_name"),
			("Email Queue", "reference_name"),
			("Email Queue Recipient", "recipient"),
			("Has Role", "parent"),
			("Item Barcode", "parent"),
			("Item Customer Detail", "parent"),
			("Item Default", "parent"),
			("Item Reorder", "parent"),
			("Item Supplier", "parent"),
			("Item Tax", "parent"),
			("Item Variant Attribute", "parent"),
			("Notification Subscribed Document", "parent"),
			("Notification Type Preference", "parent"),
			("Party Account", "parent"),
			("Portal User", "parent"),
			("Sales Team", "parent"),
			("Supplier Number At Customer", "parent"),
			("UOM Conversion Detail", "parent"),
			("User Email", "parent"),
			("User Role Profile", "parent"),
			("User Social Login", "parent"),
		):
			for row in self.frappe.get_all(
				doctype,
				filters={field: ("like", f"{marker}%")},
				fields=["name", field],
				order_by="name asc",
			):
				residue.append(
					{
						"error": "run-marked side effect remains after cleanup",
						"mutation_id": 0,
						"target": {
							"doctype": doctype,
							"name": row["name"] if isinstance(row, dict) else row,
						},
					}
				)
		for doctype, field in (("Deleted Document", "deleted_name"),):
			for name in self.frappe.get_all(
				doctype, filters={field: ("like", f"{marker}%")}, pluck=field, order_by=f"{field} asc"
			):
				residue.append(
					{
						"error": "run-marked cleanup artifact remains",
						"mutation_id": 0,
						"target": {"doctype": doctype, "name": name},
					}
				)
		for (user,) in self.frappe.db.sql(
			"select user from tabSessions where user like %s order by user", (f"{marker}%",)
		):
			residue.append(
				{
					"error": "run-marked cleanup artifact remains",
					"mutation_id": 0,
					"target": {"doctype": "Sessions", "name": user},
				}
			)
		for doctype, name in self.frappe.db.sql(
			"select doctype, name from `__Auth` where name like %s order by doctype, name",
			(f"{marker}%",),
		):
			residue.append(
				{
					"error": "run-marked authentication secret remains after cleanup",
					"mutation_id": 0,
					"target": {"doctype": f"__Auth/{doctype}", "name": name},
				}
			)
		unique = {
			(finding["error"], finding["target"]["doctype"], finding["target"]["name"]): finding
			for finding in residue
		}
		return [unique[key] for key in sorted(unique)]


def assert_no_runtime_residue() -> list:
	"""Fail CI when any run-marked record, setting, session, or secret remains."""
	import frappe

	control = SiteControl(frappe, frappe.local.site, "0" * 32)
	findings = control.residue_scan(marker=RUN_MARKER_PREFIX)
	if findings:
		raise RuntimeError(f"runtime cleanup residue remains: {findings}")
	return []


def _authorized_browser_plan(frappe, run_id: str, token: str) -> dict:
	if (
		not isinstance(run_id, str)
		or not re.fullmatch(r"[0-9a-f]{32}", run_id)
		or not isinstance(token, str)
		or len(token) > 128
	):
		raise frappe.PermissionError
	root = Path(frappe.get_site_path("private", "frappe_lt_runtime")).resolve()
	path = root / f"{run_id}.browser.json"
	if path.parent != root or path.is_symlink():
		raise frappe.PermissionError
	try:
		plan = json.loads(path.read_bytes(), object_pairs_hook=_json_object)
	except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
		raise frappe.PermissionError from error
	if not isinstance(plan, dict) or set(plan) != {
		"credentials",
		"diagnostic_sampling",
		"fixtures",
		"run_id",
		"schema_version",
		"scenarios",
		"token",
	}:
		raise frappe.PermissionError
	journal_path = root / "journal.json"
	try:
		journal = _load_journal(journal_path)
	except ValueError as error:
		raise frappe.PermissionError from error
	if (
		plan.get("schema_version") != 2
		or not isinstance(plan.get("diagnostic_sampling"), bool)
		or plan.get("run_id") != run_id
		or journal["run_id"] != run_id
		or journal["state"] != "active"
		or not secrets.compare_digest(plan.get("token", ""), token)
	):
		raise frappe.PermissionError
	return plan


def _authorized_scenario(
	frappe, plan: dict, scenario_id: str, kind: str, *, require_user: bool = True
) -> dict:
	if not isinstance(scenario_id, str):
		raise frappe.PermissionError
	scenario = next(
		(
			candidate
			for candidate in plan["scenarios"]
			if candidate["id"] == scenario_id and candidate["kind"] == kind
		),
		None,
	)
	credential = plan["credentials"].get(scenario["role_profile_id"]) if scenario else None
	if scenario is None or credential is None or (require_user and frappe.session.user != credential["user"]):
		raise frappe.PermissionError
	return scenario


def _validate_capture(lookups: list[dict], **outputs: str) -> None:
	if len(lookups) > MAX_CAPTURE_LOOKUPS:
		raise ValueError("server output performed too many translation lookups")
	for lookup in lookups:
		key = lookup["key"]
		if (
			not isinstance(key["source"], str)
			or not key["source"]
			or len(key["source"].encode()) > 256 * 1024
			or (key["context"] is not None and not isinstance(key["context"], str))
			or (key["context"] is not None and len(key["context"].encode()) > 4096)
			or not isinstance(lookup["effective"], str)
			or len(lookup["effective"].encode()) > 256 * 1024
		):
			raise ValueError("captured translation lookup exceeds its field limits")
	for label, output in outputs.items():
		if not isinstance(output, str) or len(output.encode()) > MAX_CAPTURE_BYTES:
			raise ValueError(f"captured {label} exceeds its output limit")
	if len(canonical_json({"lookups": lookups, **outputs})) > MAX_CAPTURE_BYTES:
		raise ValueError("captured runtime output exceeds its aggregate limit")


def _resolve_effective(frappe, raw_source: str, context: str | None, *, lookup_path: str = "server") -> dict:
	from frappe.translate import get_all_translations, get_translations_from_apps, get_user_translations

	raw_source = frappe.as_unicode(raw_source)
	normalized_source = raw_source.strip()
	if not normalized_source or context == "" or lookup_path not in {"client", "server"}:
		raise ValueError("source must be nonempty and context must be nonempty or null")
	lookup_source = raw_source if lookup_path == "client" else normalized_source
	contextual_key = f"{lookup_source}:{context}" if context else None
	merged = get_all_translations("lt")
	selected_key = contextual_key if contextual_key and merged.get(contextual_key) else None
	if selected_key is None and merged.get(lookup_source):
		selected_key = lookup_source
	effective = merged[selected_key] if selected_key is not None else lookup_source
	origin = "merged" if selected_key is not None else "missing"
	for app in ("frappe", "erpnext", "frappe_lt"):
		dictionary = get_translations_from_apps("lt", apps=[app])
		if selected_key is not None and dictionary.get(selected_key) == effective:
			origin = app
	database = get_user_translations("lt")
	if selected_key is not None and database.get(selected_key) == effective:
		origin = "database"
	return {
		"effective": effective,
		"key": {"context": context, "source": normalized_source},
		"raw_source": raw_source,
		"source": origin,
	}


def resolve_translation(
	run_id: str,
	token: str,
	scenario_id: str,
	source: str,
	context: str | None = None,
	lookup_path: str = "server",
) -> dict:
	"""Resolve one effective Lithuanian lookup and identify its highest-precedence source."""
	import frappe

	control = SiteControl(frappe, frappe.local.site, run_id)
	with control.operation():
		plan = _authorized_browser_plan(frappe, run_id, token)
		if not isinstance(source, str) or not source.strip() or len(source.encode()) > 256 * 1024:
			raise ValueError("translation source exceeds its input limit")
		if context is not None and (
			not isinstance(context, str) or not context or len(context.encode()) > 4096
		):
			raise ValueError("translation context exceeds its input limit")
		if lookup_path not in {"client", "server"}:
			raise ValueError("translation lookup_path is invalid")
		if scenario_id not in {scenario["id"] for scenario in plan["scenarios"]}:
			raise frappe.PermissionError
		scenario = next(scenario for scenario in plan["scenarios"] if scenario["id"] == scenario_id)
		_authorized_scenario(frappe, plan, scenario_id, scenario["kind"])
		result = _resolve_effective(frappe, source, context, lookup_path=lookup_path)
		_validate_capture([result])
		return result


def runtime_login(run_id: str, token: str, scenario_id: str) -> dict:
	"""Create the scenario's Frappe session while suppressing login-hook delivery effects."""
	import frappe

	control = SiteControl(frappe, frappe.local.site, run_id)
	with control.operation():
		return _runtime_login_locked(frappe, control, run_id, token, scenario_id)


def _runtime_login_locked(frappe, control: SiteControl, run_id: str, token: str, scenario_id: str) -> dict:
	plan = _authorized_browser_plan(frappe, run_id, token)
	if not isinstance(scenario_id, str):
		raise frappe.PermissionError
	scenario = next((item for item in plan["scenarios"] if item["id"] == scenario_id), None)
	if scenario is None:
		raise frappe.PermissionError
	scenario = _authorized_scenario(frappe, plan, scenario_id, scenario["kind"], require_user=False)
	user = plan["credentials"][scenario["role_profile_id"]]["user"]
	sid = hashlib.sha256(f"{run_id}:{scenario_id}:{token}".encode()).hexdigest()[:56]
	control.before_runtime_login(user, sid)
	effects = {"enqueue": [], "mail": []}
	with _LOGIN_LOCK:
		original_enqueue = frappe.enqueue
		original_generate_hash = frappe.generate_hash
		original_sendmail = frappe.sendmail
		previous_mute = frappe.flags.mute_emails
		hash_state = {"used": False}

		def scoped_enqueue(*args, **kwargs):
			if getattr(frappe.local, "frappe_lt_runtime_login", None) == run_id:
				effects["enqueue"].append((args, kwargs))
				return None
			return original_enqueue(*args, **kwargs)

		def scoped_sendmail(*args, **kwargs):
			if getattr(frappe.local, "frappe_lt_runtime_login", None) == run_id:
				effects["mail"].append((args, kwargs))
				return None
			return original_sendmail(*args, **kwargs)

		def scoped_generate_hash(*args, **kwargs):
			if not hash_state["used"] and getattr(frappe.local, "frappe_lt_runtime_login", None) == run_id:
				hash_state["used"] = True
				return sid
			return original_generate_hash(*args, **kwargs)

		frappe.local.frappe_lt_runtime_login = run_id
		frappe.flags.mute_emails = True
		frappe.enqueue = scoped_enqueue
		frappe.generate_hash = scoped_generate_hash
		frappe.sendmail = scoped_sendmail
		try:
			from frappe.sessions import Session, generate_csrf_token

			manager = frappe.local.login_manager
			manager.user = user
			manager.get_user_info()
			manager.full_name = " ".join(
				filter(None, [manager.info.first_name, getattr(manager.info, "last_name", None)])
			)
			frappe.local.session_obj = Session(
				user=user,
				full_name=manager.full_name,
				user_type=manager.user_type,
			)
			frappe.generate_hash = original_generate_hash
			frappe.local.session = frappe.local.session_obj.data
			manager.set_user_info()
			generate_csrf_token()
		finally:
			frappe.enqueue = original_enqueue
			frappe.generate_hash = original_generate_hash
			frappe.sendmail = original_sendmail
			frappe.flags.mute_emails = previous_mute
			frappe.local.frappe_lt_runtime_login = None
	control.after_runtime_login(user)
	return {
		"csrf_token": frappe.local.session.data.csrf_token,
		"suppressed_enqueue": len(effects["enqueue"]),
		"suppressed_mail": len(effects["mail"]),
		"user": user,
	}


@contextmanager
def _capture_server_lookups(frappe):
	"""Observe calls to Frappe's translator, including references imported before this request."""
	import sys

	translator_code = frappe._.__code__
	previous = sys.getprofile()
	lookups = []
	budget = {"bytes": 0, "overflow": False}

	def profile(frame, event, value):
		if previous is not None:
			previous(frame, event, value)
		if event != "return" or frame.f_code is not translator_code or not isinstance(value, str):
			return
		source = frappe.as_unicode(
			frame.f_locals.get("non_translated_string", frame.f_locals.get("msg", ""))
		).strip()
		context = frame.f_locals.get("context")
		lookup_bytes = len(source.encode()) + len(value.encode())
		if context is not None:
			lookup_bytes += len(frappe.as_unicode(context).encode())
		budget["bytes"] += lookup_bytes
		if len(lookups) >= MAX_CAPTURE_LOOKUPS or budget["bytes"] > MAX_CAPTURE_BYTES:
			budget["overflow"] = True
			return
		if source:
			lookups.append(
				{
					"effective": value,
					"key": {"context": context, "source": source},
				}
			)

	sys.setprofile(profile)
	try:
		yield lookups
	finally:
		sys.setprofile(previous)
	if budget["overflow"]:
		raise ValueError("server output translation lookups exceed their capture limit")


def _capture_http_response(
	frappe,
	route: str,
	*,
	form_dict=None,
	get_response=None,
	access_log_module=None,
) -> dict:
	if get_response is None:
		from frappe.website.serve import get_response

	request_state = getattr(frappe, "local", frappe)
	previous_form_dict = request_state.form_dict
	suppressed_access_logs = []
	if form_dict is not None:
		request_state.form_dict = form_dict
	try:
		if access_log_module is None:
			with _capture_server_lookups(frappe) as lookups:
				response = get_response(route)
		else:
			with _PRINT_CAPTURE_LOCK:
				original_access_log = access_log_module.make_access_log
				capture_marker = object()
				previous_capture_marker = getattr(request_state, "frappe_lt_runtime_print_capture", None)

				def scoped_access_log(*args, **kwargs):
					if getattr(request_state, "frappe_lt_runtime_print_capture", None) is capture_marker:
						suppressed_access_logs.append((args, kwargs))
						return None
					return original_access_log(*args, **kwargs)

				request_state.frappe_lt_runtime_print_capture = capture_marker
				access_log_module.make_access_log = scoped_access_log
				try:
					with _capture_server_lookups(frappe) as lookups:
						response = get_response(route)
				finally:
					access_log_module.make_access_log = original_access_log
					request_state.frappe_lt_runtime_print_capture = previous_capture_marker
	finally:
		request_state.form_dict = previous_form_dict
	body = response.get_data(as_text=True)
	_validate_capture(lookups, body=body)
	return {
		"body": body,
		"content_type": response.content_type or "",
		"lookups": lookups,
		"status": response.status_code,
		"suppressed_access_logs": len(suppressed_access_logs),
	}


def capture_portal(run_id: str, token: str, scenario_id: str, route: str) -> dict:
	"""Render one authorized portal route while recording its effective translation calls."""
	import frappe

	control = SiteControl(frappe, frappe.local.site, run_id)
	with control.operation():
		plan = _authorized_browser_plan(frappe, run_id, token)
		scenario = _authorized_scenario(frappe, plan, scenario_id, "portal")
		if not isinstance(route, str) or route != scenario["target"]["route"]:
			raise frappe.PermissionError
		captured = _capture_http_response(frappe, route, form_dict=frappe._dict())
		captured["html"] = captured.pop("body")
		return captured


def capture_welcome_email(run_id: str, token: str, scenario_id: str, user: str) -> dict:
	"""Invoke the standard welcome action while intercepting its same-process outbound effect."""
	import frappe

	control = SiteControl(frappe, frappe.local.site, run_id)
	with control.operation():
		return _capture_welcome_email_locked(frappe, control, run_id, token, scenario_id, user)


def _redact_email_output(output: str) -> str:
	from email import policy
	from email.parser import Parser

	def redact(value: str) -> str:
		return re.sub(
			r"(?i)(/update-password\?key=)[^&\s<>\"']+",
			r"\1[REDACTED]",
			value,
		)

	parsed = Parser(policy=policy.default).parsestr(output)
	if parsed["Subject"] is not None:
		parsed.replace_header("Subject", redact(str(parsed["Subject"])))
	parts = list(parsed.walk()) if parsed.is_multipart() else [parsed]
	for part in parts:
		if part.get_content_disposition() == "attachment" or part.get_content_maintype() != "text":
			continue
		try:
			content = part.get_content()
		except (LookupError, UnicodeError) as error:
			raise ValueError("captured email contains undecodable text") from error
		part.set_content(redact(content), subtype=part.get_content_subtype(), charset="utf-8")
	return parsed.as_string(policy=policy.SMTP)


def _email_visible_output(output: str) -> tuple[str, str]:
	from email import policy
	from email.parser import Parser

	parsed = Parser(policy=policy.default).parsestr(output)
	subject = str(parsed["Subject"] or "")
	parts = list(parsed.walk()) if parsed.is_multipart() else [parsed]
	visible_parts = []
	for part in parts:
		if part.get_content_disposition() == "attachment" or part.get_content_type() not in {
			"text/html",
			"text/plain",
		}:
			continue
		content = part.get_content()
		if part.get_content_type() == "text/html":
			from frappe.utils import strip_html

			content = strip_html(content)
		visible_parts.append(content)
	return subject, "\n".join([subject, *visible_parts])


def _finalize_recipient_message(frappe, builder, recipient: str, send_mail_context) -> dict:
	message = builder.as_dict()
	if message is None:
		raise RuntimeError("welcome action did not build an outbound message")
	recipients = message.get("recipients") or []
	if recipients != [recipient]:
		raise RuntimeError("welcome action did not target exactly its run-owned recipient")
	if json.loads(message.get("attachments") or "[]"):
		raise RuntimeError("welcome action unexpectedly included attachments")
	queue = frappe.new_doc("Email Queue")
	queue.update({key: value for key, value in message.items() if key != "recipients"})
	queue.set_recipients(recipients)
	final = send_mail_context(queue).build_message(recipient)
	message["message"] = frappe.safe_decode(final)
	return message


def _capture_welcome_email_locked(
	frappe, control: SiteControl, run_id: str, token: str, scenario_id: str, user: str
) -> dict:
	plan = _authorized_browser_plan(frappe, run_id, token)
	scenario = _authorized_scenario(frappe, plan, scenario_id, "email")
	expected_user = plan["fixtures"].get(scenario["fixture_id"], {}).get("user")
	if user != expected_user or user == "Administrator":
		raise frappe.PermissionError
	queue_before = set(
		frappe.get_all(
			"Email Queue Recipient",
			filters={"recipient": user},
			pluck="parent",
			order_by="parent asc",
		)
	)
	captured = []
	lookups = []
	enqueued = []
	from frappe.email.doctype.email_queue.email_queue import QueueBuilder, SendMailContext

	def intercept_process(builder, send_now=False):
		captured.append(_finalize_recipient_message(frappe, builder, user, SendMailContext))
		return None

	control.journal = _load_journal(control.journal_path)
	control.before_document_mutation("User", user)
	with _EMAIL_CAPTURE_LOCK:
		original_process = QueueBuilder.process
		original_enqueue = frappe.enqueue
		previous_mute = frappe.flags.mute_emails
		previous_capture_marker = getattr(frappe.local, "frappe_lt_runtime_email_capture", None)

		def scoped_process(builder, send_now=False):
			if getattr(frappe.local, "frappe_lt_runtime_email_capture", None) == run_id:
				return intercept_process(builder, send_now)
			return original_process(builder, send_now)

		def scoped_enqueue(*args, **kwargs):
			if getattr(frappe.local, "frappe_lt_runtime_email_capture", None) == run_id:
				enqueued.append((args, kwargs))
				return None
			return original_enqueue(*args, **kwargs)

		frappe.local.frappe_lt_runtime_email_capture = run_id
		frappe.flags.mute_emails = True
		frappe.enqueue = scoped_enqueue
		QueueBuilder.process = scoped_process
		try:
			with _capture_server_lookups(frappe) as lookups:
				frappe.get_doc("User", user).send_welcome_mail_to_user()
		finally:
			QueueBuilder.process = original_process
			frappe.enqueue = original_enqueue
			frappe.flags.mute_emails = previous_mute
			frappe.local.frappe_lt_runtime_email_capture = previous_capture_marker
	if len(captured) != 1:
		raise RuntimeError(f"welcome action emitted {len(captured)} outbound messages instead of one")
	if enqueued:
		raise RuntimeError(f"welcome action attempted {len(enqueued)} queued effects")
	queue_after = set(
		frappe.get_all(
			"Email Queue Recipient",
			filters={"recipient": user},
			pluck="parent",
			order_by="parent asc",
		)
	)
	if queue_after != queue_before:
		raise RuntimeError("welcome action left a durable Email Queue effect")
	message = captured[0]
	output = _redact_email_output(message.get("message") or "")
	subject, visible_output = _email_visible_output(output)
	result = {
		"lookups": lookups,
		"output": output,
		"subject": subject,
		"suppressed": {"email_queue": 0, "enqueue": 0, "outbound": 1},
		"visible_output": visible_output,
	}
	_validate_capture(result["lookups"], output=output, subject=subject)
	if len(visible_output.encode()) > MAX_CAPTURE_BYTES:
		raise ValueError("captured email visible output exceeds its output limit")
	frappe.db.commit()
	return result


def capture_print(run_id: str, token: str, scenario_id: str, doctype: str, name: str) -> dict:
	"""Run Frappe's final print action while recording request-process lookups."""
	import frappe
	import frappe.www.printview as printview

	control = SiteControl(frappe, frappe.local.site, run_id)
	with control.operation():
		plan = _authorized_browser_plan(frappe, run_id, token)
		_authorized_scenario(frappe, plan, scenario_id, "print")
		allowed = {
			(fixture["doctype"], fixture["name"])
			for fixture_id, fixture in plan["fixtures"].items()
			if fixture_id == "todo-draft"
		}
		if (doctype, name) not in allowed:
			raise frappe.PermissionError
		captured = _capture_http_response(
			frappe,
			"/printview",
			form_dict=frappe._dict(
				{
					"doctype": doctype,
					"format": "Standard",
					"name": name,
					"no_letterhead": 1,
					"settings": None,
				}
			),
			access_log_module=printview,
		)
		if captured["suppressed_access_logs"] != 1:
			raise RuntimeError(
				"standard print action did not emit exactly one suppressible Access Log side effect"
			)
		captured["html"] = captured.pop("body")
		return captured


try:
	import frappe as _frappe
except ImportError:
	_frappe = None

if _frappe is not None:
	resolve_translation = _frappe.whitelist(methods=["POST"])(resolve_translation)
	runtime_login = _frappe.whitelist(allow_guest=True, methods=["POST"])(runtime_login)
	capture_portal = _frappe.whitelist(methods=["POST"])(capture_portal)
	capture_welcome_email = _frappe.whitelist(methods=["POST"])(capture_welcome_email)
	capture_print = _frappe.whitelist(methods=["POST"])(capture_print)
