import json
import os
import secrets
from contextlib import contextmanager
from hashlib import sha256

import frappe
import frappe.defaults
import frappe.translate
from filelock import FileLock, Timeout
from frappe.core.doctype.system_settings.system_settings import clear_system_settings_cache
from frappe.utils import now_datetime

SCHEMA_VERSION = 1
NAMESPACE = f"frappe_lt.profile.v{SCHEMA_VERSION}"
LOCK_NAME = "frappe_lt_profile"
LOCK_TIMEOUT = 1
APP_NAME = "frappe_lt"
INSTALL_RECOVERY_AUTH = "frappe_lt_profile_install_recovery"
DEFAULT_VALUE_COLUMNS = (
	"name",
	"creation",
	"modified",
	"modified_by",
	"owner",
	"docstatus",
	"idx",
	"defkey",
	"defvalue",
	"parent",
	"parentfield",
	"parenttype",
)

LANGUAGE_VALUES = {
	"enabled": 1,
	"date_format": "yyyy-mm-dd",
	"time_format": "HH:mm",
	"number_format": "# ###,##",
	"first_day_of_the_week": "Monday",
}
SYSTEM_VALUES = {
	"country": "Lithuania",
	"language": "lt",
	"time_zone": "Europe/Vilnius",
	"date_format": "yyyy-mm-dd",
	"time_format": "HH:mm",
	"number_format": "# ###,##",
	"first_day_of_the_week": "Monday",
}
GLOBAL_DEFAULT_VALUES = {**SYSTEM_VALUES, "lang": SYSTEM_VALUES["language"]}
USER_VALUES = {"language": "lt", "time_zone": "Europe/Vilnius"}
USER_DEFAULT_VALUES = {
	"date_format": "yyyy-mm-dd",
	"time_format": "HH:mm",
	"number_format": "# ###,##",
	"first_day_of_the_week": "Monday",
	"time_zone": "Europe/Vilnius",
}

STATE_PHASE = {
	"SNAPSHOTTED": "SNAPSHOT",
	"LANGUAGE_APPLIED": "LANGUAGE",
	"SYSTEM_APPLIED": "SYSTEM",
	"APPLIED": "USERS",
	"RESTORED": "RESTORE",
	"ABANDONED": "LEAVE",
}
SETUP_STATES = {"SNAPSHOTTED", "LANGUAGE_APPLIED", "SYSTEM_APPLIED", "APPLIED"}
TERMINAL_STATES = {"RESTORED", "ABANDONED"}


class ProfileError(frappe.ValidationError):
	pass


def apply():
	"""Apply or resume the Lithuanian site profile."""
	with _profile_lock("setup"):
		_ensure_repeatable_read("setup")
		return _apply()


def _apply():
	storage = _load_storage("setup")
	state_before = storage["state"]
	if state_before in TERMINAL_STATES:
		raise _error(
			f"Profile is terminal ({state_before}) and cannot be applied again while this install exists.",
			"setup",
			storage["phase"],
			recovery="remove",
		)
	_assert_apply_preflight()

	result = _result("setup", state_before, state_before, storage["phase"])
	if state_before == "ABSENT":
		try:
			storage = _create_snapshot()
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback()
			_clear_profile_caches([])
			raise _phase_failure("setup", "SNAPSHOT", exc) from exc
		_clear_profile_caches(storage["targets"])
	else:
		for item in _compare_with_applied(storage):
			_add_observation(result, item)

	for expected, next_state, phase, mutate in (
		("SNAPSHOTTED", "LANGUAGE_APPLIED", "LANGUAGE", _apply_language),
		("LANGUAGE_APPLIED", "SYSTEM_APPLIED", "SYSTEM", _apply_system),
		("SYSTEM_APPLIED", "APPLIED", "USERS", _apply_users),
	):
		if storage["state"] != expected:
			continue
		try:
			frappe.db.commit()
			for item in mutate(storage):
				if item["reason"] == "already_applied":
					result["unchanged"].append(item)
				elif item["reason"] == "target_deleted":
					result["missing_target"].append(item)
				else:
					result["changed"].append(item)
			_update_manifest(storage, next_state, phase)
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback()
			_clear_profile_caches(storage["targets"])
			raise _phase_failure("setup", phase, exc) from exc
		_clear_profile_caches(storage["targets"])

	result.update(state_after=storage["state"], phase=storage["phase"])
	return _sorted_result(result)


def status():
	"""Return deterministic profile lifecycle and per-field status."""
	with _profile_lock("status"):
		storage = _load_storage("status")
		result = _result("status", storage["state"], storage["state"], storage["phase"])
		if storage["state"] in SETUP_STATES:
			for item in _compare_with_applied(storage):
				_add_observation(result, item)
		return _sorted_result(result)


def restore():
	"""Guardedly restore every phase that was committed by apply()."""
	with _profile_lock("restore"):
		_ensure_repeatable_read("restore")
		for attempt in range(3):
			frappe.db.commit()
			storage = _load_storage("restore")
			state_before = storage["state"]
			if state_before == "ABSENT":
				raise _error(
					"No Lithuanian profile snapshot exists.", "restore", "PREFLIGHT", recovery="setup"
				)
			if state_before in TERMINAL_STATES:
				raise _error(
					f"Profile is terminal ({state_before}); restore data no longer exists.",
					"restore",
					storage["phase"],
					recovery="remove",
				)

			result = _result("restore", state_before, state_before, "RESTORE")
			try:
				locked = _lock_restore_rows(storage)
				completed = _completed_entities(state_before)
				actions = _plan_restore(storage, completed, locked, result)
				_execute_restore_plan(actions, locked)
				_replace_with_terminal_marker(storage, "RESTORED", "RESTORE")
				frappe.db.commit()
			except frappe.QueryDeadlockError as exc:
				frappe.db.rollback()
				_clear_profile_caches(storage["targets"])
				if attempt < 2:
					continue
				raise _phase_failure("restore", "RESTORE", exc) from exc
			except Exception as exc:
				frappe.db.rollback()
				_clear_profile_caches(storage["targets"])
				raise _phase_failure("restore", "RESTORE", exc) from exc
			_clear_profile_caches(storage["targets"])
			result.update(state_after="RESTORED", phase="RESTORE")
			return _sorted_result(result)


def _ensure_repeatable_read(operation):
	try:
		frappe.db.sql("set session transaction isolation level repeatable read")
		isolation = frappe.db.sql("select @@tx_isolation", pluck=True)[0]
	except Exception as exc:
		raise _error(
			"The database must support REPEATABLE READ for safe profile row and range locking.",
			operation,
			"ISOLATION",
		) from exc
	if isolation.replace("_", "-").upper() != "REPEATABLE-READ":
		raise _error(
			f"The database reported unsupported transaction isolation {isolation!r}; REPEATABLE READ is required.",
			operation,
			"ISOLATION",
		)


def abandon(*, confirmed=False):
	"""Irreversibly discard restore data after literal CLI confirmation."""
	with _profile_lock("leave"):
		storage = _load_storage("leave")
		if not confirmed:
			raise _error(
				"Leaving the applied profile requires --confirm-leave-profile.",
				"leave",
				storage["phase"],
			)
		if storage["state"] not in SETUP_STATES:
			recovery = "setup" if storage["state"] == "ABSENT" else "remove"
			raise _error(
				f"Profile cannot be left from {storage['state']}.",
				"leave",
				storage["phase"],
				recovery=recovery,
			)
		state_before = storage["state"]
		try:
			_replace_with_terminal_marker(storage, "ABANDONED", "LEAVE")
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback()
			raise _phase_failure("leave", "LEAVE", exc) from exc
		_clear_profile_caches(storage["targets"])
		return _sorted_result(_result("leave", state_before, "ABANDONED", "LEAVE"))


def before_uninstall():
	with _profile_lock("uninstall"):
		storage = _load_storage("uninstall")
		if storage["state"] not in TERMINAL_STATES:
			recovery = "setup_restore_remove" if storage["state"] == "ABSENT" else "uninstall"
			raise _error(
				"Uninstall is blocked until the profile is restored or explicitly left.",
				"uninstall",
				storage["phase"],
				recovery=recovery,
			)


def after_uninstall():
	with _profile_lock("uninstall"):
		storage = _load_storage("uninstall")
		if storage["state"] not in TERMINAL_STATES:
			raise _error("Terminal profile marker is missing after uninstall.", "uninstall", storage["phase"])
		frappe.db.delete("DefaultValue", {"parent": NAMESPACE})
		frappe.db.commit()
		_clear_profile_caches([])


def before_install():
	"""Authorize recovery only for a valid terminal marker left by an absent app."""
	_set_install_recovery_authorization(None)
	return _install_apply("before")


def after_install():
	"""Apply on install, recovering only a persistently authorized terminal marker."""
	return _install_apply("after")


def _install_apply(stage):
	with _profile_lock("setup"):
		_ensure_repeatable_read("setup")
		storage = _load_storage("setup")
		if stage == "before":
			if APP_NAME not in frappe.get_installed_apps() and storage["state"] in TERMINAL_STATES:
				authorization = storage["manifest"]["reinstall_authorization"]
				if authorization is None:
					try:
						authorization = _authorize_terminal_reinstall(storage)
						frappe.db.commit()
					except Exception as exc:
						frappe.db.rollback()
						raise _phase_failure("setup", "INSTALL_AUTHORIZATION", exc) from exc
				_set_install_recovery_authorization(authorization)
			return None
		if storage["state"] in TERMINAL_STATES:
			if storage["manifest"]["reinstall_authorization"] is None:
				raise _error(
					"A terminal profile marker cannot be recovered by a forced current install.",
					"setup",
					storage["phase"],
					recovery="remove",
				)
			_assert_apply_preflight()
			try:
				frappe.db.delete("DefaultValue", {"parent": NAMESPACE})
				storage = _create_snapshot()
				frappe.db.commit()
			except Exception as exc:
				frappe.db.rollback()
				_clear_profile_caches([])
				raise _phase_failure("setup", "SNAPSHOT", exc) from exc
			_set_install_recovery_authorization(None)
			_clear_profile_caches(storage["targets"])
		return _apply()


def _authorize_terminal_reinstall(storage):
	authorized_at = str(now_datetime())
	authorization = {
		"app": APP_NAME,
		"site": frappe.local.site,
		"marker_name": storage["row_names"]["manifest"],
		"terminal_state": storage["state"],
		"authorized_at": authorized_at,
		"nonce": secrets.token_hex(32),
	}
	manifest = storage["manifest"]
	manifest["updated_at"] = authorized_at
	manifest["reinstall_authorization"] = authorization
	_validate_manifest(
		manifest,
		"setup",
		terminal=True,
		marker_name=storage["row_names"]["manifest"],
	)
	frappe.db.set_value(
		"DefaultValue",
		storage["row_names"]["manifest"],
		"defvalue",
		_encode(manifest),
		update_modified=False,
	)
	return authorization


def _set_install_recovery_authorization(value):
	setattr(frappe.local, INSTALL_RECOVERY_AUTH, value)


def _assert_apply_preflight():
	if not frappe.db.exists("Language", "lt"):
		raise _error(
			"Language/lt is missing; run migrate or repair the Language records first.",
			"setup",
			"PREFLIGHT",
		)
	try:
		_single_states()
	except frappe.ValidationError as exc:
		raise _error(str(exc), "setup", "PREFLIGHT") from exc


@contextmanager
def _profile_lock(operation):
	lock_path = os.path.abspath(frappe.get_site_path("locks", f"{LOCK_NAME}.lock"))
	try:
		with FileLock(lock_path, timeout=LOCK_TIMEOUT):
			yield
	except Timeout as exc:
		raise _error(
			f"Another frappe_lt profile operation already holds the site lock ({LOCK_NAME}).",
			operation,
			"LOCK",
		) from exc


def _create_snapshot():
	targets = sorted(frappe.get_all("User", filters={"enabled": 1, "name": ("!=", "Guest")}, pluck="name"))
	created_at = str(now_datetime())
	manifest = {
		"schema_version": SCHEMA_VERSION,
		"state": "SNAPSHOTTED",
		"last_completed_phase": "SNAPSHOT",
		"created_at": created_at,
		"updated_at": created_at,
		"target_count": len(targets),
		"target_digest": _target_digest(targets),
	}
	language = _entity_snapshot("Language", "lt", LANGUAGE_VALUES)
	system = _system_snapshot()
	users = [_user_snapshot(user) for user in targets]
	_insert_row("manifest", manifest)
	_insert_row("language", language)
	_insert_row("system_settings", system)
	for user in users:
		_insert_row("user", user)
	return _load_storage("setup")


def _load_storage(operation):
	parents = frappe.db.sql(
		"select distinct parent from tabDefaultValue where parent regexp %s order by parent",
		(r"^frappe_lt\.profile\.v[0-9]+$",),
		pluck=True,
	)
	unsupported = [parent for parent in parents if parent != NAMESPACE]
	if unsupported:
		raise _incompatible_storage_error(
			f"Unsupported profile namespace(s): {', '.join(unsupported)}.", operation
		)
	rows = frappe.get_all(
		"DefaultValue",
		filters={"parent": NAMESPACE},
		fields=["name", "parent", "parenttype", "parentfield", "defkey", "defvalue"],
		order_by="creation asc, name asc",
	)
	if not rows:
		return {"state": "ABSENT", "phase": "PREFLIGHT", "targets": [], "users": {}}
	if any(
		row.parent != NAMESPACE or row.parenttype != "__default" or row.parentfield != "system_defaults"
		for row in rows
	):
		raise _invalid_storage(operation, "DefaultValue ownership")
	manifest_rows = [row for row in rows if row.defkey == "manifest"]
	if len(manifest_rows) != 1:
		raise _storage_error("Profile storage has an invalid manifest count.", operation)
	manifest = _decode(manifest_rows[0].defvalue, operation, "manifest")
	schema_version = manifest.get("schema_version")
	if type(schema_version) is not int:
		raise _invalid_storage(operation, "manifest schema_version")
	if schema_version != SCHEMA_VERSION:
		raise _incompatible_storage_error(
			f"Unsupported profile schema version {schema_version!r}.", operation
		)
	state = manifest.get("state")
	phase = manifest.get("last_completed_phase")
	if not isinstance(state, str):
		raise _invalid_storage(operation, "manifest state")
	if not isinstance(phase, str):
		raise _invalid_storage(operation, "manifest last_completed_phase")
	if state not in SETUP_STATES | TERMINAL_STATES or STATE_PHASE[state] != phase:
		raise _storage_error(f"Impossible profile lifecycle state {state!r}/{phase!r}.", operation)
	if state in TERMINAL_STATES:
		if len(rows) != 1:
			raise _storage_error("Terminal profile marker still has restore rows.", operation)
		_validate_manifest(
			manifest,
			operation,
			terminal=True,
			marker_name=manifest_rows[0].name,
		)
		return {
			"state": state,
			"phase": phase,
			"manifest": manifest,
			"targets": [],
			"users": {},
			"row_names": {"manifest": manifest_rows[0].name},
		}

	_validate_manifest(manifest, operation, terminal=False)
	by_key = {}
	for row in rows:
		by_key.setdefault(row.defkey, []).append(row)
	expected_row_types = {"manifest", "language", "system_settings"}
	if manifest["target_count"]:
		expected_row_types.add("user")
	if set(by_key) != expected_row_types:
		raise _storage_error("Profile snapshot contains an unknown row type.", operation)
	if len(by_key.get("language", [])) != 1 or len(by_key.get("system_settings", [])) != 1:
		raise _storage_error("Profile snapshot is missing its Language or System Settings row.", operation)
	language = _decode(by_key["language"][0].defvalue, operation, "language")
	system = _decode(by_key["system_settings"][0].defvalue, operation, "system_settings")
	_validate_entity_payload(language, LANGUAGE_VALUES, operation, "Language", direct=True)
	_validate_system_payload(system, operation)
	users = [_decode(row.defvalue, operation, "user") for row in by_key.get("user", [])]
	for user in users:
		_validate_user_payload(user, operation)
	targets = [user.get("name") for user in users]
	if "Guest" in targets or len(targets) != len(set(targets)):
		raise _storage_error("Profile target set is invalid.", operation)
	targets.sort()
	if manifest.get("target_count") != len(targets) or manifest.get("target_digest") != _target_digest(
		targets
	):
		raise _storage_error("Profile target rows do not match the frozen target set.", operation)
	return {
		"state": state,
		"phase": phase,
		"manifest": manifest,
		"language": language,
		"system": system,
		"users": {row["name"]: row for row in users},
		"targets": targets,
		"row_names": {
			"language": by_key["language"][0].name,
			"system_settings": by_key["system_settings"][0].name,
			"users": {
				payload["name"]: row.name for payload, row in zip(users, by_key.get("user", []), strict=True)
			},
		},
	}


def _validate_manifest(manifest, operation, *, terminal, marker_name=None):
	expected = {"schema_version", "state", "last_completed_phase", "created_at", "updated_at"}
	if terminal:
		expected.add("reinstall_authorization")
	else:
		expected |= {"target_count", "target_digest"}
	if set(manifest) != expected:
		raise _invalid_storage(operation, "manifest keys")
	if type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION:
		raise _invalid_storage(operation, "manifest schema_version")
	if not isinstance(manifest["state"], str):
		raise _invalid_storage(operation, "manifest state")
	if not isinstance(manifest["last_completed_phase"], str):
		raise _invalid_storage(operation, "manifest last_completed_phase")
	if not isinstance(manifest["created_at"], str) or not manifest["created_at"]:
		raise _invalid_storage(operation, "manifest created_at")
	if not isinstance(manifest["updated_at"], str) or not manifest["updated_at"]:
		raise _invalid_storage(operation, "manifest updated_at")
	if terminal:
		_validate_reinstall_authorization(manifest, marker_name, operation)
		return
	if type(manifest["target_count"]) is not int or manifest["target_count"] < 0:
		raise _invalid_storage(operation, "manifest target_count")
	digest = manifest["target_digest"]
	if (
		not isinstance(digest, str)
		or len(digest) != 64
		or any(char not in "0123456789abcdef" for char in digest)
	):
		raise _invalid_storage(operation, "manifest target_digest")


def _validate_reinstall_authorization(manifest, marker_name, operation):
	authorization = manifest["reinstall_authorization"]
	if authorization is None:
		return
	expected = {
		"app",
		"site",
		"marker_name",
		"terminal_state",
		"authorized_at",
		"nonce",
	}
	if not isinstance(authorization, dict) or set(authorization) != expected:
		raise _invalid_storage(operation, "terminal reinstall authorization")
	if (
		authorization["app"] != APP_NAME
		or authorization["site"] != frappe.local.site
		or authorization["marker_name"] != marker_name
		or authorization["terminal_state"] != manifest["state"]
		or authorization["authorized_at"] != manifest["updated_at"]
	):
		raise _invalid_storage(operation, "terminal reinstall authorization scope")
	nonce = authorization["nonce"]
	if (
		not isinstance(nonce, str)
		or len(nonce) != 64
		or any(char not in "0123456789abcdef" for char in nonce)
	):
		raise _invalid_storage(operation, "terminal reinstall authorization nonce")


def _validate_entity_payload(payload, managed_values, operation, label, *, direct=False):
	if set(payload) != {"values"} or not isinstance(payload["values"], dict):
		raise _invalid_storage(operation, f"{label} object")
	_validate_values(payload["values"], managed_values, operation, label, direct=direct)


def _validate_system_payload(payload, operation):
	if set(payload) != {"values", "defaults"} or not isinstance(payload["defaults"], dict):
		raise _invalid_storage(operation, "System Settings object")
	_validate_values(payload["values"], SYSTEM_VALUES, operation, "System Settings")
	_validate_values(
		payload["defaults"],
		GLOBAL_DEFAULT_VALUES,
		operation,
		"System Default",
		allow_previous_list=True,
	)


def _validate_user_payload(payload, operation):
	if set(payload) != {"name", "values", "defaults"}:
		raise _invalid_storage(operation, "User object")
	if not isinstance(payload["name"], str) or not payload["name"] or payload["name"] == "Guest":
		raise _invalid_storage(operation, "User name")
	if not isinstance(payload["values"], dict) or not isinstance(payload["defaults"], dict):
		raise _invalid_storage(operation, "User values")
	_validate_values(payload["values"], USER_VALUES, operation, "User", direct=True)
	_validate_values(
		payload["defaults"], USER_DEFAULT_VALUES, operation, "User Default", allow_previous_list=True
	)


def _validate_values(
	values,
	managed_values,
	operation,
	label,
	*,
	direct=False,
	allow_previous_list=False,
):
	if set(values) != set(managed_values):
		raise _invalid_storage(operation, f"{label} managed fields")
	for field, pair in values.items():
		if not isinstance(pair, dict) or set(pair) != {"previous", "applied"}:
			raise _invalid_storage(operation, f"{label}.{field} value pair")
		_validate_state(
			pair["previous"],
			operation,
			f"{label}.{field}.previous",
			require_exists=True if direct else None,
			allow_list=allow_previous_list,
		)
		_validate_previous_value(pair["previous"]["value"], operation, label, field)
		_validate_state(
			pair["applied"],
			operation,
			f"{label}.{field}.applied",
			require_exists=True,
		)
		if not _same_value(pair["applied"]["value"], managed_values[field]):
			raise _invalid_storage(operation, f"{label}.{field} applied value")


def _validate_state(state, operation, label, *, require_exists=None, allow_list=False):
	if not isinstance(state, dict) or set(state) != {"exists", "value"}:
		raise _invalid_storage(operation, f"{label} state")
	if type(state["exists"]) is not bool:
		raise _invalid_storage(operation, f"{label} exists")
	if require_exists is not None and state["exists"] is not require_exists:
		raise _invalid_storage(operation, f"{label} existence")
	if not state["exists"] and state["value"] is not None:
		raise _invalid_storage(operation, f"{label} absent value")
	if isinstance(state["value"], (dict, list)):
		if not (allow_list and state["exists"] and isinstance(state["value"], list) and state["value"]):
			raise _invalid_storage(operation, f"{label} value type")
		if any(isinstance(value, (dict, list)) for value in state["value"]):
			raise _invalid_storage(operation, f"{label} nested value")


def _validate_previous_value(value, operation, label, field):
	values = value if isinstance(value, list) else [value]
	if label == "Language" and field == "enabled":
		if any(type(item) not in {bool, int} or item not in {0, 1} for item in values):
			raise _invalid_storage(operation, f"{label}.{field}.previous value type")
		return
	if any(item is not None and not isinstance(item, str) for item in values):
		raise _invalid_storage(operation, f"{label}.{field}.previous value type")


def _same_value(left, right):
	return type(left) is type(right) and left == right


def _invalid_storage(operation, detail):
	return _storage_error(f"Invalid profile storage: {detail}.", operation)


def _entity_snapshot(entity, name, values):
	current = frappe.db.get_value(entity, name, list(values), as_dict=True)
	return {
		"values": {
			field: {
				"previous": {"exists": True, "value": current[field]},
				"applied": {"exists": True, "value": value},
			}
			for field, value in values.items()
		},
	}


def _system_snapshot():
	states = _single_states()
	default_rows = _default_rows("__default", GLOBAL_DEFAULT_VALUES)
	defaults = _states_from_default_rows(default_rows, GLOBAL_DEFAULT_VALUES)
	return {
		"values": {
			field: {"previous": states[field], "applied": {"exists": True, "value": value}}
			for field, value in SYSTEM_VALUES.items()
		},
		"defaults": {
			field: {"previous": defaults[field], "applied": {"exists": True, "value": value}}
			for field, value in GLOBAL_DEFAULT_VALUES.items()
		},
	}


def _user_snapshot(user):
	snapshot = _entity_snapshot("User", user, USER_VALUES)
	defaults = _default_states(user)
	snapshot["name"] = user
	snapshot["defaults"] = {
		field: {"previous": defaults[field], "applied": {"exists": True, "value": value}}
		for field, value in USER_DEFAULT_VALUES.items()
	}
	return snapshot


def _single_states():
	rows = frappe.db.sql(
		"select field, value from tabSingles where doctype=%s order by field",
		("System Settings",),
		as_dict=True,
	)
	values = _single_image(rows)
	return {field: {"exists": field in values, "value": values.get(field)} for field in SYSTEM_VALUES}


def _single_image(rows):
	values = {}
	duplicates = set()
	for row in rows:
		if row.field in values:
			duplicates.add(row.field)
		values[row.field] = row.value
	if duplicates:
		fields = ", ".join(sorted(duplicates))
		raise frappe.ValidationError(f"System Settings contains duplicate field row(s): {fields}")
	return values


def _default_states(user, managed_values=USER_DEFAULT_VALUES):
	return _states_from_default_rows(_default_rows(user, managed_values), managed_values)


def _default_rows(parent, managed_values):
	return frappe.get_all(
		"DefaultValue",
		filters={"parent": parent, "defkey": ("in", tuple(managed_values))},
		fields=["name", "defkey", "defvalue"],
		order_by="creation asc, name asc",
	)


def _states_from_default_rows(rows, managed_values):
	values = {field: [] for field in managed_values}
	for row in rows:
		values[row.defkey].append(row.defvalue)
	return {
		field: {
			"exists": bool(field_values),
			"value": field_values[0] if len(field_values) == 1 else field_values or None,
		}
		for field, field_values in values.items()
	}


def _apply_language(storage):
	current = _lock_direct_row("Language", "lt", LANGUAGE_VALUES)
	if current is None:
		raise frappe.ValidationError("Language/lt disappeared during the Language phase")
	_refresh_direct_previous(storage["language"]["values"], current)
	_save_snapshot_row(storage["row_names"]["language"], storage["language"])
	entries = _direct_apply("Language", "lt", storage["language"]["values"], current)
	applied = frappe.db.get_value("Language", "lt", list(LANGUAGE_VALUES), as_dict=True)
	if applied is None or any(applied[field] != value for field, value in LANGUAGE_VALUES.items()):
		raise frappe.ValidationError("Language/lt profile postcondition failed")
	return entries


def _apply_system(storage):
	locked_image = _lock_single_states()
	locked_default_rows = _lock_all_default_rows("__default")
	locked_defaults = {
		field: _default_lock_from_rows(locked_default_rows, field) for field in sorted(GLOBAL_DEFAULT_VALUES)
	}
	for field, saved in storage["system"]["values"].items():
		saved["previous"] = {"exists": field in locked_image, "value": locked_image.get(field)}
	for field, saved in storage["system"]["defaults"].items():
		saved["previous"] = locked_defaults[field]["state"]
	_save_snapshot_row(storage["row_names"]["system_settings"], storage["system"])
	doc = _system_doc_from_image(locked_image)
	current = {
		field: {"exists": field in locked_image, "value": locked_image.get(field)} for field in SYSTEM_VALUES
	}
	entries = []
	for field, saved in storage["system"]["values"].items():
		value = saved["applied"]["value"]
		if current[field] == saved["applied"]:
			entries.append(_entry("System Settings", "System Settings", field, "already_applied"))
			continue
		doc.set(field, value)
		entries.append(_entry("System Settings", "System Settings", field, "applied"))
	_prepare_valid_system_doc(doc)
	_assert_unmanaged_system_values_stable(doc)
	doc.save(ignore_permissions=True)
	_restore_unmanaged_single_rows(locked_image)
	_restore_all_default_rows("__default", locked_default_rows)
	for field, saved in storage["system"]["defaults"].items():
		locked_default = locked_defaults[field]
		if locked_default["state"] == saved["applied"]:
			entries.append(_entry("System Default", "__default", field, "already_applied"))
			continue
		_set_default_exact("__default", field, saved["applied"]["value"], locked_default["rows"])
		entries.append(_entry("System Default", "__default", field, "applied"))
	current = _single_states()
	if any(current[field] != saved["applied"] for field, saved in storage["system"]["values"].items()):
		raise frappe.ValidationError("System Settings profile postcondition failed")
	if any(
		_default_states("__default", GLOBAL_DEFAULT_VALUES)[field] != {"exists": True, "value": value}
		for field, value in GLOBAL_DEFAULT_VALUES.items()
	):
		raise frappe.ValidationError("System Settings global defaults postcondition failed")
	if (
		frappe.db.get_default("lang") != SYSTEM_VALUES["language"]
		or frappe.local.lang != SYSTEM_VALUES["language"]
	):
		raise frappe.ValidationError("System Settings default language postcondition failed")
	return entries


def _prepare_valid_system_doc(doc):
	for df in doc.meta.fields:
		if df.fieldname in SYSTEM_VALUES:
			continue
		value = doc.get(df.fieldname)
		if df.fieldtype == "Select" and value is not None:
			options = [option for option in (df.options or "").split("\n") if not option.startswith("[")]
			if str(value) not in options:
				doc.set(
					df.fieldname, df.default if df.default in options else options[0] if options else None
				)
		elif df.reqd and (value is None or value == "") and df.default is not None:
			doc.set(df.fieldname, df.default)


def _assert_unmanaged_system_values_stable(doc):
	doc.load_doc_before_save()
	changed = {
		df.fieldname
		for df in doc.meta.fields
		if df.fieldname not in SYSTEM_VALUES and doc.has_value_changed(df.fieldname)
	}
	unsafe = changed & {
		"enable_two_factor_auth",
		"force_user_to_reset_password",
		"enable_snapshot_reports",
		"frequency",
	}
	if unsafe:
		fields = ", ".join(sorted(unsafe))
		raise frappe.ValidationError(f"Cannot safely normalize unmanaged System Settings field(s): {fields}")


def _apply_users(storage):
	entries = []
	locked_users = {}
	for user in storage["targets"]:
		locked_users[user] = {
			"values": _lock_direct_row("User", user, USER_VALUES),
			"defaults": {field: _lock_default_state(user, field) for field in sorted(USER_DEFAULT_VALUES)},
		}
	for user in storage["targets"]:
		locked = locked_users[user]
		if locked["values"] is None:
			entries.extend(_target_deleted_entries("User", user, USER_VALUES))
			entries.extend(_target_deleted_entries("User Default", user, USER_DEFAULT_VALUES))
			continue
		_refresh_direct_previous(storage["users"][user]["values"], locked["values"])
		for field, saved in storage["users"][user]["defaults"].items():
			saved["previous"] = locked["defaults"][field]["state"]
		_save_snapshot_row(storage["row_names"]["users"][user], storage["users"][user])
		entries.extend(_direct_apply("User", user, storage["users"][user]["values"], locked["values"]))
		for field, saved in storage["users"][user]["defaults"].items():
			locked_default = locked["defaults"][field]
			if locked_default["state"] == saved["applied"]:
				entries.append(_entry("User Default", user, field, "already_applied"))
				continue
			_set_default_exact(user, field, saved["applied"]["value"], locked_default["rows"])
			entries.append(_entry("User Default", user, field, "applied"))
		current_user = frappe.db.get_value("User", user, list(USER_VALUES), as_dict=True)
		current_defaults = _default_states(user)
		if any(current_user[field] != value for field, value in USER_VALUES.items()) or any(
			current_defaults[field] != {"exists": True, "value": value}
			for field, value in USER_DEFAULT_VALUES.items()
		):
			raise frappe.ValidationError(f"User profile postcondition failed for {user}")
	return entries


def _set_default_exact(user, field, value, rows=None):
	if rows is None:
		rows = _lock_default_state(user, field)["rows"]
	_restore_default_in_place(user, field, {"exists": True, "value": value}, rows)
	if _lock_default_state(user, field)["state"] != {"exists": True, "value": value}:
		raise frappe.ValidationError(f"Default postcondition failed for {user}/{field}")


def _direct_apply(entity, name, values, current=None):
	current = current or frappe.db.get_value(entity, name, list(values), as_dict=True)
	updates = {}
	entries = []
	for field, saved in values.items():
		if current[field] == saved["applied"]["value"]:
			entries.append(_entry(entity, name, field, "already_applied"))
			continue
		updates[field] = saved["applied"]["value"]
		entries.append(_entry(entity, name, field, "applied"))
	if updates:
		frappe.db.set_value(entity, name, updates, update_modified=False)
	return entries


def _refresh_direct_previous(values, current):
	for field, saved in values.items():
		saved["previous"] = {"exists": True, "value": current[field]}


def _save_snapshot_row(row_name, payload):
	frappe.db.set_value("DefaultValue", row_name, "defvalue", _encode(payload), update_modified=False)


def _system_doc_from_image(image):
	doc = frappe.get_single("System Settings")
	for field in doc.meta.get_valid_fields():
		if field not in {"doctype", "name"}:
			doc.set(field, image.get(field))
	for field, value in image.items():
		doc.set(field, value)
	doc._fix_numeric_types()
	return doc


def _lock_restore_rows(storage):
	language = _lock_direct_row("Language", "lt", LANGUAGE_VALUES)
	system = _lock_single_states()
	all_system_defaults = _lock_all_default_rows("__default")
	locked = {
		"language": language,
		"system": system,
		"all_system_defaults": all_system_defaults,
		"system_defaults": {
			field: _default_lock_from_rows(all_system_defaults, field)
			for field in sorted(GLOBAL_DEFAULT_VALUES)
		},
		"users": {},
	}
	for user in storage["targets"]:
		locked["users"][user] = {
			"values": _lock_direct_row("User", user, USER_VALUES),
			"defaults": {},
		}
		for field in sorted(USER_DEFAULT_VALUES):
			locked["users"][user]["defaults"][field] = _lock_default_state(user, field)
	return locked


def _lock_direct_row(doctype, name, fields):
	columns = ", ".join(f"`{field}`" for field in fields)
	rows = frappe.db.sql(
		f"select {columns} from `tab{doctype}` where name=%s for update",  # nosec B608
		name,
		as_dict=True,
	)
	return rows[0] if rows else None


def _lock_single_states():
	rows = frappe.db.sql(
		"select field, value from tabSingles where doctype=%s order by field for update",
		("System Settings",),
		as_dict=True,
	)
	return _single_image(rows)


def _lock_all_default_rows(parent):
	columns = ", ".join(f"`{column}`" for column in DEFAULT_VALUE_COLUMNS)
	return frappe.db.sql(
		f"select {columns} from tabDefaultValue where parent=%s "  # nosec B608
		"order by creation, name for update",
		(parent,),
		as_dict=True,
	)


def _default_lock_from_rows(rows, field):
	field_rows = [row for row in rows if row.defkey == field]
	return {
		"rows": field_rows,
		"state": {
			"exists": bool(field_rows),
			"value": (
				field_rows[0].defvalue
				if len(field_rows) == 1
				else [row.defvalue for row in field_rows]
				if field_rows
				else None
			),
		},
	}


def _restore_all_default_rows(parent, saved_rows):
	current_rows = _lock_all_default_rows(parent)
	current_by_name = {row.name: row for row in current_rows}
	saved_by_name = {row.name: row for row in saved_rows}
	for name in sorted(set(current_by_name) - set(saved_by_name)):
		frappe.db.sql("delete from tabDefaultValue where name=%s and parent=%s", (name, parent))

	mutable_columns = DEFAULT_VALUE_COLUMNS[1:]
	assignments = ", ".join(f"`{column}`=%s" for column in mutable_columns)
	columns = ", ".join(f"`{column}`" for column in DEFAULT_VALUE_COLUMNS)
	placeholders = ", ".join(["%s"] * len(DEFAULT_VALUE_COLUMNS))
	for name, saved in saved_by_name.items():
		current = current_by_name.get(name)
		if current is None:
			frappe.db.sql(
				f"insert into tabDefaultValue ({columns}) values ({placeholders})",  # nosec B608
				tuple(saved[column] for column in DEFAULT_VALUE_COLUMNS),
			)
		elif any(current[column] != saved[column] for column in mutable_columns):
			frappe.db.sql(
				f"update tabDefaultValue set {assignments} where name=%s",  # nosec B608
				(*[saved[column] for column in mutable_columns], name),
			)

	if _lock_all_default_rows(parent) != saved_rows:
		raise frappe.ValidationError(f"Global DefaultValue compensation failed for {parent}")


def _lock_default_state(user, field):
	rows = frappe.db.sql(
		"select name, defvalue from tabDefaultValue where parent=%s and defkey=%s "
		"order by creation, name for update",
		(user, field),
		as_dict=True,
	)
	return {
		"rows": rows,
		"state": {
			"exists": bool(rows),
			"value": rows[0].defvalue if len(rows) == 1 else [row.defvalue for row in rows] if rows else None,
		},
	}


def _plan_restore(storage, completed, locked, result):
	actions = []
	if "users" in completed:
		_plan_restore_users(storage, locked["users"], result, actions)
	if "system" in completed:
		_plan_restore_system(storage, locked["system"], result, actions)
		_plan_restore_system_defaults(storage, locked["system_defaults"], result, actions)
	if "language" in completed:
		_plan_restore_language(storage, locked["language"], result, actions)
	return actions


def _plan_restore_users(storage, locked_users, result, actions):
	for user in storage["targets"]:
		current = locked_users[user]
		if current["values"] is None:
			result["missing_target"].extend(_target_deleted_entries("User", user, USER_VALUES))
			result["missing_target"].extend(
				_target_deleted_entries("User Default", user, USER_DEFAULT_VALUES)
			)
			continue
		_plan_restore_direct(
			"User", user, storage["users"][user]["values"], current["values"], result, actions
		)
		for field, saved in storage["users"][user]["defaults"].items():
			current_default = current["defaults"][field]["state"]
			if current_default != saved["applied"]:
				result["skipped_manual_change"].append(_entry("User Default", user, field, "current_differs"))
				continue
			if saved["previous"] == saved["applied"]:
				result["unchanged"].append(_entry("User Default", user, field, "already_original"))
				continue
			actions.append(
				{
					"kind": "default",
					"entity": "User Default",
					"name": user,
					"field": field,
					"state": saved["previous"],
					"rows": current["defaults"][field]["rows"],
				}
			)
			result["changed"].append(_entry("User Default", user, field, "restored"))


def _plan_restore_system(storage, current, result, actions):
	for field, saved in storage["system"]["values"].items():
		current_state = {"exists": field in current, "value": current.get(field)}
		if current_state != saved["applied"]:
			result["skipped_manual_change"].append(
				_entry("System Settings", "System Settings", field, "current_differs")
			)
			continue
		previous = saved["previous"]
		if previous == saved["applied"]:
			result["unchanged"].append(
				_entry("System Settings", "System Settings", field, "already_original")
			)
			continue
		actions.append(
			{
				"kind": "system",
				"entity": "System Settings",
				"name": "System Settings",
				"field": field,
				"state": previous,
			}
		)
		result["changed"].append(_entry("System Settings", "System Settings", field, "restored"))


def _plan_restore_system_defaults(storage, current, result, actions):
	for field, saved in storage["system"]["defaults"].items():
		current_state = current[field]["state"]
		if current_state != saved["applied"]:
			result["skipped_manual_change"].append(
				_entry("System Default", "__default", field, "current_differs")
			)
			continue
		if saved["previous"] == saved["applied"]:
			result["unchanged"].append(_entry("System Default", "__default", field, "already_original"))
			continue
		actions.append(
			{
				"kind": "system_default",
				"entity": "System Default",
				"name": "__default",
				"field": field,
				"state": saved["previous"],
				"rows": current[field]["rows"],
			}
		)
		result["changed"].append(_entry("System Default", "__default", field, "restored"))


def _plan_restore_language(storage, current, result, actions):
	if current is None:
		result["missing_target"].extend(_target_deleted_entries("Language", "lt", LANGUAGE_VALUES))
		return
	_plan_restore_direct("Language", "lt", storage["language"]["values"], current, result, actions)


def _plan_restore_direct(entity, name, values, current_values, result, actions):
	for field, saved in values.items():
		current = {"exists": True, "value": current_values[field]}
		if current != saved["applied"]:
			result["skipped_manual_change"].append(_entry(entity, name, field, "current_differs"))
			continue
		if saved["previous"] == saved["applied"]:
			result["unchanged"].append(_entry(entity, name, field, "already_original"))
			continue
		actions.append(
			{
				"kind": "direct",
				"entity": entity,
				"name": name,
				"field": field,
				"value": saved["previous"]["value"],
			}
		)
		result["changed"].append(_entry(entity, name, field, "restored"))


def _execute_restore_plan(actions, locked):
	updates = {}
	for action in actions:
		if action["kind"] == "direct":
			updates.setdefault((action["entity"], action["name"]), {})[action["field"]] = action["value"]
	for (entity, name), values in updates.items():
		frappe.db.set_value(entity, name, values, update_modified=False)

	for action in actions:
		if action["kind"] == "default":
			_restore_default(action["name"], action["field"], action["state"], action["rows"])

	system_actions = [action for action in actions if action["kind"] == "system"]
	if system_actions:
		doc = _system_doc_from_image(locked["system"])
		doc._fix_numeric_types()
		doc._original_modified = locked["system"].get("modified")
		for action in system_actions:
			if _valid_system_value(doc, action["field"], action["state"]):
				doc.set(action["field"], action["state"]["value"])
		_prepare_valid_system_doc(doc)
		_assert_unmanaged_system_values_stable(doc)
		doc.save(ignore_permissions=True)
		for action in system_actions:
			_restore_single_state(action["field"], action["state"])
		_restore_unmanaged_single_rows(locked["system"])
		_restore_all_default_rows("__default", locked["all_system_defaults"])

	system_default_actions = {
		action["field"]: action for action in actions if action["kind"] == "system_default"
	}
	for field in sorted(system_default_actions):
		action = system_default_actions[field]
		_restore_default_in_place(
			"__default", field, action["state"], locked["system_defaults"][field]["rows"]
		)


def _valid_system_value(doc, field, state):
	if not state["exists"] or state["value"] is None:
		return False
	df = doc.meta.get_field(field)
	if df.reqd and state["value"] == "":
		return False
	if df.fieldtype == "Select":
		options = [option for option in (df.options or "").split("\n") if not option.startswith("[")]
		return str(state["value"]) in options
	return True


def _restore_single_state(field, state):
	frappe.db.delete("Singles", {"doctype": "System Settings", "field": field})
	if state["exists"]:
		frappe.db.sql(
			"insert into tabSingles (doctype, field, value) values (%s, %s, %s)",
			("System Settings", field, state["value"]),
		)


def _restore_default_in_place(parent, field, state, rows):
	if not state["exists"]:
		values = []
	elif isinstance(state["value"], list):
		values = state["value"]
	else:
		values = [state["value"]]
	for row, value in zip(rows, values, strict=False):
		frappe.db.set_value("DefaultValue", row.name, "defvalue", value, update_modified=False)
	for row in rows[len(values) :]:
		frappe.db.delete("DefaultValue", {"name": row.name})
	for value in values[len(rows) :]:
		frappe.defaults.add_default(field, value, parent)


def _restore_unmanaged_single_rows(locked_image):
	current = dict(
		frappe.db.sql(
			"select field, value from tabSingles where doctype=%s order by field",
			("System Settings",),
		)
	)
	for field in sorted((set(current) | set(locked_image)) - set(SYSTEM_VALUES)):
		if field in current and field in locked_image and current[field] == locked_image[field]:
			continue
		frappe.db.sql(
			"delete from tabSingles where doctype=%s and field=%s",
			("System Settings", field),
		)
		if field in locked_image:
			frappe.db.sql(
				"insert into tabSingles (doctype, field, value) values (%s, %s, %s)",
				("System Settings", field, locked_image[field]),
			)


def _restore_default(user, field, previous, rows=None):
	if rows is None:
		rows = _lock_default_state(user, field)["rows"]
	_restore_default_in_place(user, field, previous, rows)


def _compare_with_applied(storage):
	entries = []
	completed = _completed_entities(storage["state"])
	if "language" in completed:
		if frappe.db.exists("Language", "lt"):
			entries.extend(_compare_entity("Language", "lt", storage["language"]["values"]))
		else:
			entries.extend(_target_deleted_entries("Language", "lt", LANGUAGE_VALUES))
	if "system" in completed:
		current = _single_states()
		for field, saved in storage["system"]["values"].items():
			reason = "matches_applied" if current[field] == saved["applied"] else "current_differs"
			entries.append(_entry("System Settings", "System Settings", field, reason))
		defaults = _default_states("__default", GLOBAL_DEFAULT_VALUES)
		for field, saved in storage["system"]["defaults"].items():
			reason = "matches_applied" if defaults[field] == saved["applied"] else "current_differs"
			entries.append(_entry("System Default", "__default", field, reason))
	if "users" in completed:
		for user in storage["targets"]:
			if not frappe.db.exists("User", user):
				entries.extend(_target_deleted_entries("User", user, USER_VALUES))
				entries.extend(_target_deleted_entries("User Default", user, USER_DEFAULT_VALUES))
				continue
			entries.extend(_compare_entity("User", user, storage["users"][user]["values"]))
			defaults = _default_states(user)
			for field, saved in storage["users"][user]["defaults"].items():
				reason = "matches_applied" if defaults[field] == saved["applied"] else "current_differs"
				entries.append(_entry("User Default", user, field, reason))
	return entries


def _compare_entity(entity, name, values):
	current = frappe.db.get_value(entity, name, list(values), as_dict=True)
	return [
		_entry(
			entity,
			name,
			field,
			"matches_applied"
			if {"exists": True, "value": current[field]} == saved["applied"]
			else "current_differs",
		)
		for field, saved in values.items()
	]


def _completed_entities(state):
	return {
		"SNAPSHOTTED": set(),
		"LANGUAGE_APPLIED": {"language"},
		"SYSTEM_APPLIED": {"language", "system"},
		"APPLIED": {"language", "system", "users"},
	}[state]


def _update_manifest(storage, state, phase):
	manifest = storage["manifest"]
	manifest.update(state=state, last_completed_phase=phase, updated_at=str(now_datetime()))
	frappe.db.set_value(
		"DefaultValue",
		{"parent": NAMESPACE, "defkey": "manifest"},
		"defvalue",
		_encode(manifest),
		update_modified=False,
	)
	storage.update(state=state, phase=phase)


def _replace_with_terminal_marker(storage, state, phase):
	created_at = storage["manifest"]["created_at"]
	frappe.db.delete("DefaultValue", {"parent": NAMESPACE})
	manifest = {
		"schema_version": SCHEMA_VERSION,
		"state": state,
		"last_completed_phase": phase,
		"created_at": created_at,
		"updated_at": str(now_datetime()),
		"reinstall_authorization": None,
	}
	_insert_row("manifest", manifest)


def _insert_row(key, value):
	frappe.get_doc(
		{
			"doctype": "DefaultValue",
			"parent": NAMESPACE,
			"parenttype": "__default",
			"parentfield": "system_defaults",
			"defkey": key,
			"defvalue": _encode(value),
		}
	).insert(ignore_permissions=True)


def _clear_profile_caches(users):
	clear_system_settings_cache()
	frappe.client_cache.delete_value("languages")
	frappe.cache.delete_value("languages_with_name")
	frappe.translate.clear_cache()
	frappe.clear_document_cache("Language", "lt")
	frappe.clear_document_cache("System Settings", "System Settings")
	frappe.clear_cache()
	for user in users:
		frappe.clear_cache(user=user)
	if hasattr(frappe.local, "system_settings"):
		frappe.local.system_settings = None
	frappe.local.lang = None
	frappe.set_user_lang(frappe.session.user)


def _result(operation, state_before, state_after, phase):
	return {
		"operation": operation,
		"state_before": state_before,
		"state_after": state_after,
		"phase": phase,
		"changed": [],
		"unchanged": [],
		"skipped_manual_change": [],
		"missing_target": [],
	}


def _entry(entity, name, field, reason):
	return {"entity": entity, "name": name, "field": field, "reason": reason}


def _target_deleted_entries(entity, name, values):
	return [_entry(entity, name, field, "target_deleted") for field in sorted(values)]


def _add_observation(result, item):
	if item["reason"] == "matches_applied":
		result["unchanged"].append(item)
	elif item["reason"] == "target_deleted":
		result["missing_target"].append(item)
	else:
		result["skipped_manual_change"].append(item)


def _sorted_result(result):
	for key in ("changed", "unchanged", "skipped_manual_change", "missing_target"):
		result[key] = sorted(
			result[key], key=lambda row: (row["entity"], row["name"], row["field"], row["reason"])
		)
	return result


def _encode(value):
	return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _target_digest(targets):
	return sha256("\n".join(targets).encode()).hexdigest()


def _decode(value, operation, row_name):
	try:
		decoded = json.loads(value)
	except (TypeError, ValueError) as exc:
		raise _storage_error(f"Profile {row_name} row is not valid JSON.", operation) from exc
	if not isinstance(decoded, dict):
		raise _storage_error(f"Profile {row_name} row is not a JSON object.", operation)
	return decoded


def _phase_failure(operation, phase, exc):
	if isinstance(exc, ProfileError):
		return exc
	return _error(f"Lithuanian profile {operation} failed: {exc}", operation, phase)


def _incompatible_storage_error(message, operation):
	return _error(
		f"{message} Install a compatible frappe_lt version before retrying any profile operation.",
		operation,
		"PREFLIGHT",
		recovery="compatible",
	)


def _storage_error(message, operation):
	return _error(
		f"{message} Restore {NAMESPACE} storage from a trusted backup before retrying.",
		operation,
		"PREFLIGHT",
		recovery="repair",
	)


def _error(message, operation, phase, *, recovery=None):
	site = frappe.local.site
	commands = {
		"setup": f"bench --site {site} setup-lithuanian-profile",
		"status": f"bench --site {site} show-lithuanian-profile-status",
		"restore": f"bench --site {site} restore-lithuanian-profile",
		"leave": f"bench --site {site} leave-lithuanian-profile --confirm-leave-profile",
		"remove": f"bench --site {site} uninstall-app frappe_lt",
		"compatible": "bench update --apps frappe_lt && bench version --format json",
		"repair": (
			f"bench --site {site} restore '/path/to/trusted-backup.sql' && "
			f"bench --site {site} show-lithuanian-profile-status"
		),
		"setup_restore_remove": (
			f"bench --site {site} setup-lithuanian-profile && "
			f"bench --site {site} restore-lithuanian-profile && "
			f"bench --site {site} uninstall-app frappe_lt"
		),
		"uninstall": (
			f"bench --site {site} restore-lithuanian-profile OR "
			f"bench --site {site} leave-lithuanian-profile --confirm-leave-profile"
		),
	}
	return ProfileError(f"{message} Phase: {phase}. Safe command: {commands[recovery or operation]}")
