import json
import os
import threading
import time
from unittest.mock import patch

import frappe
from filelock import FileLock
from frappe.core.doctype.system_settings.system_settings import SystemSettings
from frappe.tests import IntegrationTestCase

from frappe_lt import commands, profile


class LithuanianProfileTest(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.test_users = []
		self._restore_and_forget_profile()

	def tearDown(self):
		frappe.db.rollback()
		self._restore_and_forget_profile()
		for user in self.test_users:
			frappe.db.delete("DefaultValue", {"parent": user})
			frappe.db.delete("User", {"name": user})
		frappe.db.commit()
		super().tearDown()
		frappe.db.rollback()
		profile.apply()

	def _restore_and_forget_profile(self):
		state = profile.status()["state_after"]
		if state not in {"ABSENT", "RESTORED", "ABANDONED"}:
			profile.restore()
		frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
		frappe.db.commit()

	def _make_user(self, name, *, enabled):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": name,
				"first_name": name,
				"enabled": enabled,
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		self.test_users.append(name)
		return name

	def _capture_profile_environment(self):
		targets = frappe.get_all(
			"User", filters={"enabled": 1, "name": ("!=", "Guest")}, pluck="name", order_by="name asc"
		)
		global_keys = tuple(sorted(set(profile.SYSTEM_VALUES) | {"currency", "lang"}))
		return {
			"language": frappe.db.get_value("Language", "lt", list(profile.LANGUAGE_VALUES), as_dict=True),
			"system_rows": frappe.db.sql(
				"select field, value from tabSingles where doctype=%s order by field",
				("System Settings",),
			),
			"global_rows": frappe.db.sql(
				"select defkey, defvalue from tabDefaultValue "
				"where parent=%s and defkey in %s order by creation, name",
				("__default", global_keys),
			),
			"local_lang": getattr(frappe.local, "lang", None),
			"users": {
				user: {
					"values": frappe.db.get_value("User", user, list(profile.USER_VALUES), as_dict=True),
					"defaults": profile._default_states(user),
				}
				for user in targets
			},
		}

	def _restore_profile_environment(self, state):
		frappe.db.rollback()
		frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
		frappe.db.set_value("Language", "lt", state["language"], update_modified=False)
		frappe.db.sql("delete from tabSingles where doctype=%s", ("System Settings",))
		for field, value in state["system_rows"]:
			frappe.db.sql(
				"insert into tabSingles (doctype, field, value) values (%s, %s, %s)",
				("System Settings", field, value),
			)
		for user, saved in state["users"].items():
			if not frappe.db.exists("User", user):
				continue
			frappe.db.set_value("User", user, saved["values"], update_modified=False)
			for field, default_state in saved["defaults"].items():
				profile._restore_default(user, field, default_state)
		global_keys = tuple(sorted(set(profile.SYSTEM_VALUES) | {"currency", "lang"}))
		frappe.db.delete("DefaultValue", {"parent": "__default", "defkey": ("in", global_keys)})
		for field, value in state["global_rows"]:
			frappe.defaults.add_default(field, value, "__default")
		frappe.db.commit()
		profile._clear_profile_caches(list(state["users"]))
		frappe.local.lang = state["local_lang"]

	def _assert_profile_environment_restored(self, state):
		current = self._capture_profile_environment()
		for key in ("language", "system_rows", "global_rows", "local_lang"):
			self.assertEqual(current[key], state[key])
		for user, user_state in state["users"].items():
			self.assertEqual(current["users"][user], user_state)

	def _all_global_default_rows(self):
		return frappe.db.sql(
			"select * from tabDefaultValue where parent=%s order by creation, name",
			("__default",),
			as_dict=True,
		)

	def test_apply_profiles_only_frozen_active_users_and_is_idempotent(self):
		active = self._make_user("profile-active@example.com", enabled=1)
		disabled = self._make_user("profile-disabled@example.com", enabled=0)
		frappe.db.set_value("User", active, {"language": "en", "time_zone": "UTC"})
		frappe.db.set_value("User", disabled, {"language": "en", "time_zone": "UTC"})
		conflicting_defaults = {
			"date_format": "dd/mm/yyyy",
			"time_format": "HH:mm:ss",
			"number_format": "#,###.##",
			"first_day_of_the_week": "Sunday",
			"time_zone": "UTC",
		}
		for field, value in conflicting_defaults.items():
			frappe.db.set_default(field, value, active)
			frappe.db.set_default(field, value, disabled)
		guest_before = frappe.db.get_value("User", "Guest", ["language", "time_zone"])
		guest_defaults_before = frappe.db.sql(
			"select defkey, defvalue from tabDefaultValue where parent='Guest' order by creation, name"
		)
		disabled_defaults_before = frappe.db.sql(
			"select defkey, defvalue from tabDefaultValue where parent=%s order by creation, name", disabled
		)
		companies_before = frappe.get_all(
			"Company", fields=["name", "country", "default_currency"], order_by="name"
		)
		gl_entry_count_before = frappe.db.count("GL Entry")
		frappe.db.commit()

		first = profile.apply()

		self.assertEqual(first["state_before"], "ABSENT")
		self.assertEqual(first["state_after"], "APPLIED")
		active_targets = frappe.get_all("User", filters={"enabled": 1, "name": ("!=", "Guest")}, pluck="name")
		profile_rows = frappe.get_all(
			"DefaultValue", filters={"parent": profile.NAMESPACE}, fields=["defkey", "defvalue"]
		)
		self.assertEqual(len(profile_rows), len(active_targets) + 3)
		manifest = json.loads(next(row.defvalue for row in profile_rows if row.defkey == "manifest"))
		self.assertNotIn("targets", manifest)
		self.assertEqual(manifest["target_count"], len(active_targets))
		self.assertEqual(
			frappe.db.get_value("User", active, ["language", "time_zone"]), ("lt", "Europe/Vilnius")
		)
		self.assertEqual(
			{field: frappe.db.get_default(field, active) for field in conflicting_defaults},
			profile.USER_DEFAULT_VALUES,
		)
		self.assertEqual(frappe.db.get_value("User", disabled, ["language", "time_zone"]), ("en", "UTC"))
		self.assertEqual(
			frappe.db.sql(
				"select defkey, defvalue from tabDefaultValue where parent=%s order by creation, name",
				disabled,
			),
			disabled_defaults_before,
		)
		self.assertEqual(frappe.db.get_value("User", "Guest", ["language", "time_zone"]), guest_before)
		self.assertEqual(
			frappe.db.sql(
				"select defkey, defvalue from tabDefaultValue where parent='Guest' order by creation, name"
			),
			guest_defaults_before,
		)
		self.assertEqual(
			frappe.get_all("Company", fields=["name", "country", "default_currency"], order_by="name"),
			companies_before,
		)
		self.assertEqual(frappe.db.count("GL Entry"), gl_entry_count_before)
		self.assertEqual(
			frappe.db.get_value(
				"Language",
				"lt",
				["enabled", "date_format", "time_format", "number_format", "first_day_of_the_week"],
			),
			(1, "yyyy-mm-dd", "HH:mm", "# ###,##", "Monday"),
		)
		self.assertEqual(
			frappe.db.get_value(
				"System Settings",
				None,
				[
					"country",
					"language",
					"time_zone",
					"date_format",
					"time_format",
					"number_format",
					"first_day_of_the_week",
				],
			),
			["Lithuania", "lt", "Europe/Vilnius", "yyyy-mm-dd", "HH:mm", "# ###,##", "Monday"],
		)

		second = profile.apply()

		self.assertEqual(second["state_before"], "APPLIED")
		self.assertEqual(second["state_after"], "APPLIED")
		self.assertFalse(second["changed"])

	def test_snapshot_uses_python_order_instead_of_database_collation(self):
		self._make_user("profile-Z@example.com", enabled=1)
		self._make_user("profile-a@example.com", enabled=1)
		frappe.db.commit()

		profile.apply()

		stored_targets = [
			json.loads(value)["name"]
			for value in frappe.get_all(
				"DefaultValue",
				filters={"parent": profile.NAMESPACE, "defkey": "user"},
				pluck="defvalue",
				order_by="creation asc, name asc",
			)
		]
		expected = sorted(
			frappe.get_all("User", filters={"enabled": 1, "name": ("!=", "Guest")}, pluck="name")
		)
		self.assertEqual(stored_targets, expected)

	def test_setup_synchronizes_stale_global_defaults_when_system_singles_already_match(self):
		state = self._capture_profile_environment()
		companies_before = frappe.get_all(
			"Company", fields=["name", "country", "default_currency"], order_by="name"
		)
		gl_entries_before = frappe.db.count("GL Entry")
		system_currency_before = frappe.db.get_single_value("System Settings", "currency", cache=False)
		global_currency_before = frappe.defaults.get_global_default("currency")
		stale_defaults = {
			"country": "Germany",
			"language": "de",
			"time_zone": "UTC",
			"date_format": "dd-mm-yyyy",
			"time_format": "HH:mm:ss",
			"number_format": "#,###.##",
			"first_day_of_the_week": "Sunday",
		}
		try:
			frappe.db.set_single_value("System Settings", profile.SYSTEM_VALUES, update_modified=False)
			for field, value in stale_defaults.items():
				frappe.defaults.set_default(field, value, "__default")
			frappe.defaults.set_default("lang", "de", "__default")
			frappe.local.lang = "de"
			frappe.db.commit()

			result = profile.apply()

			self.assertEqual(result["state_after"], "APPLIED")
			self.assertEqual(
				{field: frappe.db.get_default(field) for field in profile.SYSTEM_VALUES},
				profile.SYSTEM_VALUES,
			)
			self.assertEqual(frappe.db.get_default("lang"), "lt")
			self.assertEqual(frappe.local.lang, "lt")
			unchanged = {(item["entity"], item["name"], item["field"]) for item in result["unchanged"]}
			self.assertLessEqual(
				{("System Settings", "System Settings", field) for field in profile.SYSTEM_VALUES},
				unchanged,
			)
			self.assertFalse([item for item in result["changed"] if item["entity"] == "System Settings"])
			self.assertEqual(
				frappe.get_all("Company", fields=["name", "country", "default_currency"], order_by="name"),
				companies_before,
			)
			self.assertEqual(frappe.db.count("GL Entry"), gl_entries_before)
			self.assertEqual(
				frappe.db.get_single_value("System Settings", "currency", cache=False),
				system_currency_before,
			)
			self.assertEqual(frappe.defaults.get_global_default("currency"), global_currency_before)
		finally:
			self._restore_profile_environment(state)

	def test_restore_recovers_exact_global_default_states_from_the_system_snapshot(self):
		state = self._capture_profile_environment()
		fields = tuple(sorted(set(profile.SYSTEM_VALUES) | {"lang"}))
		try:
			frappe.db.delete("DefaultValue", {"parent": "__default", "defkey": ("in", fields)})
			for field, value in (
				("language", None),
				("time_zone", ""),
				("date_format", "0"),
				("time_format", "HH:mm:ss"),
				("time_format", ""),
				("number_format", "#,###.##"),
				("first_day_of_the_week", "Sunday"),
				("lang", "de"),
			):
				frappe.defaults.add_default(field, value, "__default")
			frappe.db.set_single_value("System Settings", "country", "India")
			frappe.db.commit()
			before = frappe.db.sql(
				"select defkey, defvalue from tabDefaultValue "
				"where parent=%s and defkey in %s order by defkey, creation, name",
				("__default", fields),
			)

			profile.apply()
			profile.restore()

			self.assertEqual(
				frappe.db.sql(
					"select defkey, defvalue from tabDefaultValue "
					"where parent=%s and defkey in %s order by defkey, creation, name",
					("__default", fields),
				),
				before,
			)
		finally:
			self._restore_profile_environment(state)

	def test_system_controller_restore_preserves_a_later_global_default_change(self):
		state = self._capture_profile_environment()
		try:
			frappe.db.set_single_value("System Settings", "country", "India")
			profile._set_default_exact("__default", "country", "Germany")
			frappe.db.commit()
			profile.apply()
			profile._set_default_exact("__default", "country", "France")
			frappe.db.commit()

			result = profile.restore()

			self.assertEqual(frappe.db.get_single_value("System Settings", "country", cache=False), "India")
			self.assertEqual(
				profile._default_states("__default", profile.GLOBAL_DEFAULT_VALUES)["country"],
				{
					"exists": True,
					"value": "France",
				},
			)
			self.assertIn(
				{
					"entity": "System Default",
					"name": "__default",
					"field": "country",
					"reason": "current_differs",
				},
				result["skipped_manual_change"],
			)
		finally:
			self._restore_profile_environment(state)

	def test_system_restore_recovers_invalid_required_rows_without_normalizing_unmanaged_rows(self):
		state = self._capture_profile_environment()
		try:
			frappe.db.delete("Singles", {"doctype": "System Settings", "field": "country"})
			frappe.db.sql(
				"update tabSingles set value=null where doctype=%s and field=%s",
				("System Settings", "language"),
			)
			frappe.db.set_single_value("System Settings", "time_zone", "", update_modified=False)
			frappe.db.set_single_value("System Settings", "float_precision", "007", update_modified=False)
			frappe.db.set_single_value("System Settings", "session_expiry", "001:00", update_modified=False)
			frappe.db.set_single_value(
				"System Settings", "allowed_file_extensions", " jpg \n pdf ", update_modified=False
			)
			frappe.db.commit()
			managed_before = profile._single_states()
			unmanaged_before = frappe.db.sql(
				"select field, value from tabSingles where doctype=%s and field not in %s order by field",
				("System Settings", tuple(profile.SYSTEM_VALUES)),
			)
			unmanaged_defaults_before = [
				row
				for row in self._all_global_default_rows()
				if row.defkey not in profile.GLOBAL_DEFAULT_VALUES
			]
			all_role_before = frappe.db.sql("select * from tabRole where name=%s", ("All",), as_dict=True)
			password_dates_before = frappe.db.sql(
				"select name, last_password_reset_date from tabUser order by name"
			)
			scheduler_events_before = frappe.db.sql(
				"select * from `tabScheduler Event` order by name", as_dict=True
			)
			scheduler_before = frappe.db.sql(
				"select * from `tabScheduled Job Type` order by name", as_dict=True
			)

			profile.apply()
			self.assertEqual(
				frappe.db.sql(
					"select field, value from tabSingles where doctype=%s and field not in %s order by field",
					("System Settings", tuple(profile.SYSTEM_VALUES)),
				),
				unmanaged_before,
			)
			self.assertEqual(
				[
					row
					for row in self._all_global_default_rows()
					if row.defkey not in profile.GLOBAL_DEFAULT_VALUES
				],
				unmanaged_defaults_before,
			)
			self.assertEqual(
				frappe.db.sql("select * from tabRole where name=%s", ("All",), as_dict=True),
				all_role_before,
			)
			self.assertEqual(
				frappe.db.sql("select name, last_password_reset_date from tabUser order by name"),
				password_dates_before,
			)
			self.assertEqual(
				frappe.db.sql("select * from `tabScheduler Event` order by name", as_dict=True),
				scheduler_events_before,
			)
			self.assertEqual(
				frappe.db.sql("select * from `tabScheduled Job Type` order by name", as_dict=True),
				scheduler_before,
			)
			profile.restore()

			self.assertEqual(profile._single_states(), managed_before)
			self.assertEqual(
				frappe.db.sql(
					"select field, value from tabSingles where doctype=%s and field not in %s order by field",
					("System Settings", tuple(profile.SYSTEM_VALUES)),
				),
				unmanaged_before,
			)
		finally:
			self._restore_profile_environment(state)

	def test_system_phase_rejects_unmanaged_controller_normalization_before_save(self):
		state = self._capture_profile_environment()
		try:
			frappe.db.set_single_value(
				"System Settings", "allowed_file_extensions", " jpg \n pdf ", update_modified=False
			)
			frappe.db.set_single_value(
				"System Settings", "frequency", "legacy-frequency", update_modified=False
			)
			frappe.db.commit()
			singles_before = frappe.db.sql(
				"select field, value from tabSingles where doctype=%s order by field, value",
				("System Settings",),
			)
			defaults_before = self._all_global_default_rows()
			all_role_before = frappe.db.sql("select * from tabRole where name=%s", ("All",), as_dict=True)
			password_dates_before = frappe.db.sql(
				"select name, last_password_reset_date from tabUser order by name"
			)
			scheduler_events_before = frappe.db.sql(
				"select * from `tabScheduler Event` order by name", as_dict=True
			)
			scheduler_before = frappe.db.sql(
				"select * from `tabScheduled Job Type` order by name", as_dict=True
			)

			with patch.object(SystemSettings, "save", side_effect=AssertionError("unexpected save")):
				with self.assertRaisesRegex(profile.ProfileError, "unmanaged System Settings"):
					profile.apply()

			self.assertEqual(profile.status()["state_after"], "LANGUAGE_APPLIED")
			self.assertEqual(
				frappe.db.sql(
					"select field, value from tabSingles where doctype=%s order by field, value",
					("System Settings",),
				),
				singles_before,
			)
			self.assertEqual(self._all_global_default_rows(), defaults_before)
			self.assertEqual(
				frappe.db.sql("select * from tabRole where name=%s", ("All",), as_dict=True),
				all_role_before,
			)
			self.assertEqual(
				frappe.db.sql("select name, last_password_reset_date from tabUser order by name"),
				password_dates_before,
			)
			self.assertEqual(
				frappe.db.sql("select * from `tabScheduler Event` order by name", as_dict=True),
				scheduler_events_before,
			)
			self.assertEqual(
				frappe.db.sql("select * from `tabScheduled Job Type` order by name", as_dict=True),
				scheduler_before,
			)
		finally:
			self._restore_profile_environment(state)

	def test_preflight_rejects_duplicate_managed_and_unmanaged_system_single_rows(self):
		state = self._capture_profile_environment()
		try:
			for field in ("country", "float_precision"):
				with self.subTest(field=field):
					self._restore_profile_environment(state)
					frappe.db.sql(
						"insert into tabSingles (doctype, field, value) values (%s, %s, %s)",
						("System Settings", field, "duplicate-profile-value"),
					)
					frappe.db.commit()
					rows_before = frappe.db.sql(
						"select field, value from tabSingles where doctype=%s order by field, value",
						("System Settings",),
					)

					with patch.object(SystemSettings, "save", side_effect=AssertionError("unexpected save")):
						with self.assertRaisesRegex(profile.ProfileError, "duplicate field row"):
							profile.apply()

					self.assertEqual(
						frappe.db.sql(
							"select field, value from tabSingles where doctype=%s order by field, value",
							("System Settings",),
						),
						rows_before,
					)
					self.assertFalse(frappe.db.exists("DefaultValue", {"parent": profile.NAMESPACE}))
		finally:
			self._restore_profile_environment(state)

	def test_restore_preserves_manual_changes_and_removes_previously_absent_default(self):
		user = self._make_user("profile-restore@example.com", enabled=1)
		frappe.db.set_value("User", user, {"language": "", "time_zone": None})
		frappe.db.set_default("date_format", "", user)
		frappe.db.set_default("time_format", "0", user)
		frappe.defaults.clear_default("number_format", parent=user)
		frappe.db.commit()

		profile.apply()
		frappe.db.set_value("User", user, "language", "de", update_modified=False)
		frappe.db.commit()

		result = profile.restore()

		self.assertEqual(result["state_before"], "APPLIED")
		self.assertEqual(result["state_after"], "RESTORED")
		self.assertEqual(frappe.db.get_value("User", user, "language"), "de")
		self.assertIsNone(frappe.db.get_value("User", user, "time_zone"))
		self.assertEqual(frappe.db.get_default("date_format", user), "")
		self.assertEqual(frappe.db.get_default("time_format", user), "0")
		self.assertFalse(frappe.db.exists("DefaultValue", {"parent": user, "defkey": "number_format"}))
		self.assertIn(
			{
				"entity": "User",
				"name": user,
				"field": "language",
				"reason": "current_differs",
			},
			result["skipped_manual_change"],
		)
		self.assertEqual(
			frappe.get_all("DefaultValue", filters={"parent": profile.NAMESPACE}, pluck="defkey"),
			["manifest"],
		)

	def test_restore_distinguishes_absent_null_empty_and_zero_default_values(self):
		user = self._make_user("profile-exact-states@example.com", enabled=1)
		for field in profile.USER_DEFAULT_VALUES:
			frappe.defaults.clear_default(field, parent=user)
		frappe.get_doc(
			{
				"doctype": "DefaultValue",
				"parent": user,
				"parenttype": "__default",
				"parentfield": "system_defaults",
				"defkey": "time_format",
				"defvalue": None,
			}
		).insert(ignore_permissions=True)
		frappe.defaults.add_default("number_format", "", user)
		frappe.defaults.add_default("first_day_of_the_week", "0", user)
		frappe.db.commit()
		before = profile._default_states(user)
		self.assertEqual(before["date_format"], {"exists": False, "value": None})
		self.assertEqual(before["time_format"], {"exists": True, "value": None})
		self.assertEqual(before["number_format"], {"exists": True, "value": ""})
		self.assertEqual(before["first_day_of_the_week"], {"exists": True, "value": "0"})

		profile.apply()
		self.assertEqual(
			frappe.db.sql(
				"select defvalue from tabDefaultValue where parent=%s and defkey='time_format' "
				"order by creation, name",
				user,
				pluck=True,
			),
			[profile.USER_DEFAULT_VALUES["time_format"]],
		)
		second = profile.apply()
		self.assertFalse(second["changed"])
		profile.restore()

		self.assertEqual(profile._default_states(user), before)
		self.assertEqual(
			frappe.db.sql(
				"select defvalue from tabDefaultValue where parent=%s and defkey='time_format' "
				"order by creation, name",
				user,
				pluck=True,
			),
			[None],
		)

	def test_duplicate_user_defaults_are_canonicalized_and_restored_in_order(self):
		user = self._make_user("profile-duplicates@example.com", enabled=1)
		frappe.defaults.clear_default("date_format", parent=user)
		generated = frappe.get_doc(
			{
				"doctype": "DefaultValue",
				"parent": user,
				"parenttype": "__default",
				"parentfield": "system_defaults",
				"defkey": "date_format",
				"defvalue": "dd/mm/yyyy",
			}
		).insert(ignore_permissions=True)
		custom_name = "zzzzzzzzzzzzzzzzzzzz-profile-default"
		custom = frappe.get_doc(
			{
				"doctype": "DefaultValue",
				"name": custom_name,
				"parent": user,
				"parenttype": "__default",
				"parentfield": "system_defaults",
				"defkey": "date_format",
				"defvalue": "yyyy-mm-dd",
			}
		).insert(ignore_permissions=True)
		shared_creation = "2026-01-02 03:04:05.000000"
		frappe.db.sql(
			"update tabDefaultValue set name=case when name=%s then %s else name end, creation=%s "
			"where name in %s",
			(custom.name, custom_name, shared_creation, (generated.name, custom.name)),
		)
		frappe.db.commit()
		before = frappe.db.sql(
			"select name, creation, defvalue from tabDefaultValue where parent=%s and defkey='date_format' "
			"order by creation, name",
			user,
			as_dict=True,
		)
		self.assertEqual([row.defvalue for row in before], ["dd/mm/yyyy", "yyyy-mm-dd"])
		set_default = frappe.defaults.set_default
		add_default = frappe.defaults.add_default
		retained_name = "zzzzzzzzzzzzzzzzzzzz-current-default"

		def pinned_early_return(key, value, parent, parenttype="__default"):
			if parent == user and key == "date_format":
				return
			return set_default(key, value, parent, parenttype)

		def ci_ordering_add_default(key, value, parent, parenttype=None):
			if parent != user or key != "date_format":
				return add_default(key, value, parent, parenttype)
			row = frappe.get_doc(
				{
					"doctype": "DefaultValue",
					"name": "000-ci-restored-default",
					"parent": parent,
					"parenttype": parenttype or "__default",
					"parentfield": "system_defaults",
					"defkey": key,
					"defvalue": value,
				}
			).insert(ignore_permissions=True)
			frappe.db.sql(
				"update tabDefaultValue set name=%s, creation=%s where name=%s",
				("000-ci-restored-default", shared_creation, row.name),
			)

		with patch.object(frappe.defaults, "set_default", side_effect=pinned_early_return):
			profile.apply()

		applied = frappe.db.get_value(
			"DefaultValue",
			{"parent": user, "defkey": "date_format"},
			["name", "creation", "defvalue"],
			as_dict=True,
		)
		self.assertEqual(applied.defvalue, "yyyy-mm-dd")
		frappe.db.sql(
			"update tabDefaultValue set name=%s where name=%s",
			(retained_name, applied.name),
		)
		frappe.db.commit()

		with patch.object(frappe.defaults, "add_default", side_effect=ci_ordering_add_default):
			profile.restore()

		restored = frappe.db.sql(
			"select name, creation, defvalue from tabDefaultValue where parent=%s and defkey='date_format' "
			"order by creation, name",
			user,
			as_dict=True,
		)
		self.assertEqual(
			[(row.name, row.defvalue) for row in restored],
			[(retained_name, "dd/mm/yyyy"), ("000-ci-restored-default", "yyyy-mm-dd")],
		)
		self.assertEqual(restored[0].creation, applied.creation)
		self.assertGreater(restored[1].creation, restored[0].creation)

	def test_restore_reports_unchanged_and_does_not_rewrite_values_already_original(self):
		state = self._capture_profile_environment()
		try:
			self._assert_restore_does_not_rewrite_original_values()
		finally:
			self._restore_profile_environment(state)
		self._assert_profile_environment_restored(state)

	def _assert_restore_does_not_rewrite_original_values(self):
		user = self._make_user("profile-unchanged@example.com", enabled=1)
		frappe.db.set_value("Language", "lt", profile.LANGUAGE_VALUES, update_modified=False)
		frappe.db.set_single_value("System Settings", profile.SYSTEM_VALUES, update_modified=False)
		frappe.db.set_value("User", user, profile.USER_VALUES, update_modified=False)
		for field, value in profile.USER_DEFAULT_VALUES.items():
			frappe.defaults.set_default(field, value, user)
		frappe.db.commit()
		profile.apply()
		default_rows_before = frappe.get_all(
			"DefaultValue",
			filters={"parent": user, "defkey": ("in", tuple(profile.USER_DEFAULT_VALUES))},
			fields=["name", "defkey", "defvalue"],
			order_by="creation asc, name asc",
		)
		set_value = frappe.db.set_value
		clear_default = frappe.defaults.clear_default

		def reject_direct_rewrite(doctype, *args, **kwargs):
			if doctype == "Language" or (doctype == "User" and args[0] == user):
				raise AssertionError(f"unexpected {doctype} rewrite")
			return set_value(doctype, *args, **kwargs)

		def reject_default_rewrite(*args, **kwargs):
			if kwargs.get("parent") == user:
				raise AssertionError("unexpected User Default rewrite")
			return clear_default(*args, **kwargs)

		with (
			patch.object(frappe.db, "set_value", side_effect=reject_direct_rewrite),
			patch.object(frappe.defaults, "clear_default", side_effect=reject_default_rewrite),
			patch.object(SystemSettings, "save", side_effect=AssertionError("unexpected System rewrite")),
		):
			result = profile.restore()

		self.assertFalse(
			[item for item in result["changed"] if item["name"] in {user, "lt", "System Settings"}]
		)
		self.assertEqual(
			frappe.get_all(
				"DefaultValue",
				filters={"parent": user, "defkey": ("in", tuple(profile.USER_DEFAULT_VALUES))},
				fields=["name", "defkey", "defvalue"],
				order_by="creation asc, name asc",
			),
			default_rows_before,
		)
		unchanged = {(item["entity"], item["name"], item["field"]) for item in result["unchanged"]}
		self.assertIn(("User", user, "language"), unchanged)
		self.assertIn(("User Default", user, "date_format"), unchanged)
		self.assertIn(("Language", "lt", "date_format"), unchanged)
		self.assertIn(("System Settings", "System Settings", "country"), unchanged)

	def test_failed_phases_roll_back_only_the_current_phase_and_can_resume(self):
		def fail_language(_storage):
			frappe.db.set_value("Language", "lt", "date_format", "dd.mm.yyyy", update_modified=False)
			raise RuntimeError("language fault")

		def fail_system(_storage):
			frappe.db.set_single_value("System Settings", "country", "Germany")
			raise RuntimeError("system fault")

		def fail_users(_storage):
			frappe.db.set_value("User", "Administrator", "language", "de", update_modified=False)
			raise RuntimeError("users fault")

		cases = (
			("_apply_language", fail_language, "SNAPSHOTTED", "Language", "lt", "date_format"),
			(
				"_apply_system",
				fail_system,
				"LANGUAGE_APPLIED",
				"System Settings",
				None,
				"country",
			),
			("_apply_users", fail_users, "SYSTEM_APPLIED", "User", "Administrator", "language"),
		)
		for method, failure, expected_state, doctype, name, field in cases:
			with self.subTest(phase=method):
				self._restore_and_forget_profile()
				before = frappe.db.get_value(doctype, name, field)
				with patch.object(profile, method, side_effect=failure):
					with self.assertRaisesRegex(profile.ProfileError, "Safe command: bench --site"):
						profile.apply()

				self.assertEqual(profile.status()["state_after"], expected_state)
				self.assertEqual(frappe.db.get_value(doctype, name, field), before)

				resumed = profile.apply()
				self.assertEqual(resumed["state_before"], expected_state)
				self.assertEqual(resumed["state_after"], "APPLIED")

	def test_restore_is_available_from_every_partial_state(self):
		for method, failure, expected_state in (
			("_apply_language", RuntimeError("language fault"), "SNAPSHOTTED"),
			("_apply_system", RuntimeError("system fault"), "LANGUAGE_APPLIED"),
			("_apply_users", RuntimeError("users fault"), "SYSTEM_APPLIED"),
		):
			with self.subTest(state=expected_state):
				self._restore_and_forget_profile()
				language_before = frappe.db.get_value(
					"Language", "lt", ["enabled", "date_format", "time_format", "number_format"]
				)
				system_before = frappe.db.get_value(
					"System Settings", None, ["country", "language", "time_zone", "date_format"]
				)
				with patch.object(profile, method, side_effect=failure):
					with self.assertRaises(profile.ProfileError):
						profile.apply()

				restored = profile.restore()

				self.assertEqual(restored["state_before"], expected_state)
				self.assertEqual(restored["state_after"], "RESTORED")
				self.assertEqual(
					frappe.db.get_value(
						"Language", "lt", ["enabled", "date_format", "time_format", "number_format"]
					),
					language_before,
				)
				self.assertEqual(
					frappe.db.get_value(
						"System Settings", None, ["country", "language", "time_zone", "date_format"]
					),
					system_before,
				)

	def test_mid_restore_failure_rolls_back_all_values_and_keeps_snapshot(self):
		user = self._make_user("profile-restore-rollback@example.com", enabled=1)
		frappe.db.set_value("User", user, {"language": "en", "time_zone": "UTC"})
		for field in profile.USER_DEFAULT_VALUES:
			frappe.defaults.set_default(field, f"before-{field}", user)
		frappe.db.commit()
		profile.apply()
		rows_before = frappe.get_all(
			"DefaultValue",
			filters={"parent": profile.NAMESPACE},
			fields=["name", "defkey", "defvalue"],
			order_by="creation asc, name asc",
		)
		restore_default = profile._restore_default
		calls = 0

		def fail_after_one_default(*args, **kwargs):
			nonlocal calls
			calls += 1
			restore_default(*args, **kwargs)
			if calls == 1:
				raise RuntimeError("restore fault")

		with patch.object(profile, "_restore_default", side_effect=fail_after_one_default):
			with self.assertRaisesRegex(profile.ProfileError, "Phase: RESTORE"):
				profile.restore()

		self.assertEqual(profile.status()["state_after"], "APPLIED")
		self.assertEqual(
			frappe.db.get_value("User", user, ["language", "time_zone"]),
			("lt", "Europe/Vilnius"),
		)
		self.assertEqual(
			{field: frappe.db.get_default(field, user) for field in profile.USER_DEFAULT_VALUES},
			profile.USER_DEFAULT_VALUES,
		)
		self.assertEqual(
			frappe.get_all(
				"DefaultValue",
				filters={"parent": profile.NAMESPACE},
				fields=["name", "defkey", "defvalue"],
				order_by="creation asc, name asc",
			),
			rows_before,
		)

	def test_target_set_is_not_rescanned_when_setup_resumes(self):
		with patch.object(profile, "_apply_language", side_effect=RuntimeError("stop after snapshot")):
			with self.assertRaises(profile.ProfileError):
				profile.apply()
		late_user = self._make_user("profile-late@example.com", enabled=1)
		frappe.db.set_value("User", late_user, {"language": "en", "time_zone": "UTC"})
		frappe.db.commit()

		profile.apply()

		self.assertEqual(frappe.db.get_value("User", late_user, ["language", "time_zone"]), ("en", "UTC"))
		user_snapshots = [
			json.loads(value)
			for value in frappe.get_all(
				"DefaultValue",
				filters={"parent": profile.NAMESPACE, "defkey": "user"},
				pluck="defvalue",
			)
		]
		self.assertNotIn(late_user, {row["name"] for row in user_snapshots})

	def test_preflight_rejects_missing_language_and_unknown_schema_without_mutation(self):
		language = frappe.get_doc("Language", "lt").as_dict()
		frappe.db.delete("Language", {"name": "lt"})
		frappe.db.commit()
		try:
			with self.assertRaisesRegex(profile.ProfileError, "Language/lt is missing"):
				profile.apply()
			self.assertFalse(frappe.db.exists("DefaultValue", {"parent": profile.NAMESPACE}))
		finally:
			frappe.get_doc(language).insert(ignore_permissions=True)
			frappe.db.commit()

		frappe.get_doc(
			{
				"doctype": "DefaultValue",
				"parent": profile.NAMESPACE,
				"parenttype": "__default",
				"parentfield": "system_defaults",
				"defkey": "manifest",
				"defvalue": json.dumps(
					{
						"schema_version": 999,
						"state": "SNAPSHOTTED",
						"last_completed_phase": "SNAPSHOT",
					}
				),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		language_before = frappe.db.get_value("Language", "lt", "date_format")
		try:
			with self.assertRaisesRegex(
				profile.ProfileError,
				r"Unsupported profile schema version.*compatible frappe_lt version.*Safe command: bench update --apps frappe_lt && bench version --format json",
			):
				profile.apply()

			self.assertEqual(frappe.db.get_value("Language", "lt", "date_format"), language_before)
			self.assertEqual(
				frappe.get_all("DefaultValue", filters={"parent": profile.NAMESPACE}, pluck="defkey"),
				["manifest"],
			)
		finally:
			frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
			frappe.db.commit()

	def test_language_deleted_after_preflight_rolls_back_phase_without_advancing_manifest(self):
		language = frappe.get_doc("Language", "lt").as_dict()
		site = frappe.local.site
		deleted = threading.Event()
		errors = []
		lock_direct_row = profile._lock_direct_row

		def delete_language():
			try:
				frappe.init(site=site)
				frappe.connect()
				frappe.db.delete("Language", {"name": "lt"})
				frappe.db.commit()
			except Exception as exc:
				errors.append(exc)
			finally:
				deleted.set()
				frappe.destroy()

		def lock_after_delete(doctype, name, fields):
			if doctype == "Language" and name == "lt":
				worker = threading.Thread(target=delete_language)
				worker.start()
				self.assertTrue(deleted.wait(timeout=5))
				worker.join(timeout=5)
				self.assertFalse(worker.is_alive())
				self.assertFalse(errors)
			return lock_direct_row(doctype, name, fields)

		try:
			with patch.object(profile, "_lock_direct_row", side_effect=lock_after_delete):
				with self.assertRaisesRegex(profile.ProfileError, "Language/lt disappeared"):
					profile.apply()

			self.assertEqual(profile.status()["state_after"], "SNAPSHOTTED")
		finally:
			frappe.db.rollback()
			if not frappe.db.exists("Language", "lt"):
				frappe.get_doc(language).insert(ignore_permissions=True)
			frappe.db.commit()

	def test_preflight_rejects_unknown_versioned_namespace_before_snapshot(self):
		unknown_namespace = "frappe_lt.profile.v999"
		frappe.get_doc(
			{
				"doctype": "DefaultValue",
				"parent": unknown_namespace,
				"parenttype": "__default",
				"parentfield": "system_defaults",
				"defkey": "manifest",
				"defvalue": "{}",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		language_before = frappe.db.get_value("Language", "lt", list(profile.LANGUAGE_VALUES))
		try:
			with self.assertRaisesRegex(
				profile.ProfileError,
				r"Unsupported profile namespace.*compatible frappe_lt version.*Safe command: bench update --apps frappe_lt && bench version --format json",
			):
				profile.apply()

			self.assertFalse(frappe.db.exists("DefaultValue", {"parent": profile.NAMESPACE}))
			self.assertEqual(
				frappe.db.get_value("Language", "lt", list(profile.LANGUAGE_VALUES)), language_before
			)
		finally:
			frappe.db.delete("DefaultValue", {"parent": unknown_namespace})
			frappe.db.commit()

	def test_preflight_rejects_unsupported_namespace_without_a_manifest(self):
		state = self._capture_profile_environment()
		unknown_namespace = "frappe_lt.profile.v998"
		frappe.get_doc(
			{
				"doctype": "DefaultValue",
				"parent": unknown_namespace,
				"parenttype": "__default",
				"parentfield": "system_defaults",
				"defkey": "language",
				"defvalue": "{}",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			with self.assertRaisesRegex(profile.ProfileError, "Unsupported profile namespace"):
				profile.apply()
			self.assertFalse(frappe.db.exists("DefaultValue", {"parent": profile.NAMESPACE}))
		finally:
			self._restore_profile_environment(state)
			frappe.db.delete("DefaultValue", {"parent": unknown_namespace})
			frappe.db.commit()

	def test_profile_prefix_in_an_unrelated_default_parent_does_not_block_setup(self):
		unrelated_parent = "frappe_lt.profile.victim@example.com"
		frappe.get_doc(
			{
				"doctype": "DefaultValue",
				"parent": unrelated_parent,
				"parenttype": "__default",
				"parentfield": "system_defaults",
				"defkey": "time_zone",
				"defvalue": "UTC",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			self.assertEqual(profile.apply()["state_after"], "APPLIED")
		finally:
			frappe.db.delete("DefaultValue", {"parent": unrelated_parent})
			frappe.db.commit()

	def test_preflight_fully_validates_every_snapshot_row_before_language_mutation(self):
		language_before = frappe.db.get_value("Language", "lt", list(profile.LANGUAGE_VALUES), as_dict=True)
		frappe.db.set_value("Language", "lt", "date_format", "dd.mm.yyyy", update_modified=False)
		frappe.db.commit()

		def injected_system_field(payload, _manifest):
			payload["values"]["currency"] = {
				"previous": {"exists": True, "value": "USD"},
				"applied": {"exists": True, "value": "EUR"},
			}

		def missing_late_user_default(payload, _manifest):
			payload["defaults"].pop("time_zone")

		def malformed_state(payload, _manifest):
			payload["values"]["language"]["previous"]["exists"] = "true"

		def altered_applied_value(payload, _manifest):
			payload["values"]["language"]["applied"]["value"] = "de"

		def guest_target(payload, manifest):
			original_name = payload["name"]
			payload["name"] = "Guest"
			manifest["target_digest"] = profile._target_digest(
				sorted(
					"Guest" if target == original_name else target for target in self._snapshot_user_names()
				)
			)

		def invalid_language_enabled(payload, _manifest):
			payload["values"]["enabled"]["previous"]["value"] = 2

		def numeric_system_text(payload, _manifest):
			payload["values"]["country"]["previous"]["value"] = 42

		def numeric_user_text(payload, _manifest):
			payload["values"]["language"]["previous"]["value"] = 42

		def numeric_user_default(payload, _manifest):
			payload["defaults"]["date_format"]["previous"]["value"] = 0.5

		def numeric_duplicate_user_default(payload, _manifest):
			payload["defaults"]["date_format"]["previous"]["value"] = ["", 0]

		cases = (
			("system_settings", injected_system_field),
			("user", missing_late_user_default),
			("user", malformed_state),
			("user", altered_applied_value),
			("user", guest_target),
			("language", invalid_language_enabled),
			("system_settings", numeric_system_text),
			("user", numeric_user_text),
			("user", numeric_user_default),
			("user", numeric_duplicate_user_default),
		)
		try:
			for (
				defkey,
				corrupt,
			) in cases:
				with self.subTest(corruption=corrupt.__name__):
					frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
					frappe.db.commit()
					with patch.object(profile, "_apply_language", side_effect=RuntimeError("snapshot only")):
						with self.assertRaises(profile.ProfileError):
							profile.apply()
					row = frappe.get_all(
						"DefaultValue",
						filters={"parent": profile.NAMESPACE, "defkey": defkey},
						fields=["name", "defvalue"],
						order_by="creation asc, name asc",
					)[-1]
					manifest_row = frappe.get_value(
						"DefaultValue",
						{"parent": profile.NAMESPACE, "defkey": "manifest"},
						["name", "defvalue"],
						as_dict=True,
					)
					payload = json.loads(row.defvalue)
					manifest = json.loads(manifest_row.defvalue)
					corrupt(payload, manifest)
					frappe.db.set_value("DefaultValue", row.name, "defvalue", json.dumps(payload))
					frappe.db.set_value("DefaultValue", manifest_row.name, "defvalue", json.dumps(manifest))
					frappe.db.commit()

					with self.assertRaisesRegex(
						profile.ProfileError,
						r"Invalid profile storage.*trusted backup.*Safe command: bench --site .* restore '/path/to/trusted-backup.sql' && bench --site .* show-lithuanian-profile-status",
					):
						profile.apply()

					self.assertEqual(frappe.db.get_value("Language", "lt", "date_format"), "dd.mm.yyyy")
		finally:
			frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
			frappe.db.set_value("Language", "lt", language_before, update_modified=False)
			frappe.db.commit()

	def test_preflight_rejects_duplicate_snapshot_entities_before_mutation(self):
		language_before = frappe.db.get_value("Language", "lt", "date_format")
		frappe.db.set_value("Language", "lt", "date_format", "dd.mm.yyyy", update_modified=False)
		frappe.db.commit()
		try:
			for defkey in ("language", "system_settings", "user"):
				with self.subTest(defkey=defkey):
					frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
					frappe.db.commit()
					with patch.object(profile, "_apply_language", side_effect=RuntimeError("snapshot only")):
						with self.assertRaises(profile.ProfileError):
							profile.apply()
					row = frappe.get_all(
						"DefaultValue",
						filters={"parent": profile.NAMESPACE, "defkey": defkey},
						fields=["parent", "parenttype", "parentfield", "defkey", "defvalue"],
						order_by="creation asc, name asc",
					)[0]
					frappe.get_doc({"doctype": "DefaultValue", **row}).insert(ignore_permissions=True)
					frappe.db.commit()

					with self.assertRaisesRegex(profile.ProfileError, "Profile (snapshot|target set)"):
						profile.apply()
					self.assertEqual(frappe.db.get_value("Language", "lt", "date_format"), "dd.mm.yyyy")
		finally:
			frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
			frappe.db.set_value("Language", "lt", "date_format", language_before, update_modified=False)
			frappe.db.commit()

	def test_preflight_rejects_profile_rows_with_wrong_defaultvalue_ownership(self):
		language_before = frappe.db.get_value("Language", "lt", "date_format")
		frappe.db.set_value("Language", "lt", "date_format", "dd.mm.yyyy", update_modified=False)
		frappe.db.commit()
		with patch.object(profile, "_apply_language", side_effect=RuntimeError("snapshot only")):
			with self.assertRaises(profile.ProfileError):
				profile.apply()
		row = frappe.get_value(
			"DefaultValue",
			{"parent": profile.NAMESPACE, "defkey": "language"},
			"name",
		)
		frappe.db.set_value("DefaultValue", row, "parenttype", "User")
		frappe.db.commit()

		try:
			with self.assertRaisesRegex(profile.ProfileError, "DefaultValue ownership"):
				profile.apply()
			self.assertEqual(frappe.db.get_value("Language", "lt", "date_format"), "dd.mm.yyyy")
		finally:
			frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
			frappe.db.set_value("Language", "lt", "date_format", language_before, update_modified=False)
			frappe.db.commit()

	def test_preflight_rejects_non_string_manifest_state_without_raw_type_error(self):
		with patch.object(profile, "_apply_language", side_effect=RuntimeError("snapshot only")):
			with self.assertRaises(profile.ProfileError):
				profile.apply()
		manifest_row = frappe.get_value(
			"DefaultValue",
			{"parent": profile.NAMESPACE, "defkey": "manifest"},
			["name", "defvalue"],
			as_dict=True,
		)
		manifest = json.loads(manifest_row.defvalue)
		manifest["state"] = []
		frappe.db.set_value("DefaultValue", manifest_row.name, "defvalue", json.dumps(manifest))
		frappe.db.commit()

		try:
			with self.assertRaisesRegex(profile.ProfileError, "Invalid profile storage: manifest state"):
				profile.status()
		finally:
			frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
			frappe.db.commit()

	def test_loader_accepts_a_valid_snapshot_with_zero_user_rows(self):
		with patch.object(profile, "_apply_language", side_effect=RuntimeError("snapshot only")):
			with self.assertRaises(profile.ProfileError):
				profile.apply()
		manifest_row = frappe.get_value(
			"DefaultValue",
			{"parent": profile.NAMESPACE, "defkey": "manifest"},
			["name", "defvalue"],
			as_dict=True,
		)
		manifest = json.loads(manifest_row.defvalue)
		manifest["target_count"] = 0
		manifest["target_digest"] = profile._target_digest([])
		frappe.db.set_value("DefaultValue", manifest_row.name, "defvalue", json.dumps(manifest))
		frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE, "defkey": "user"})
		frappe.db.commit()

		self.assertEqual(profile.status()["state_after"], "SNAPSHOTTED")

	def _snapshot_user_names(self):
		return [
			json.loads(value)["name"]
			for value in frappe.get_all(
				"DefaultValue",
				filters={"parent": profile.NAMESPACE, "defkey": "user"},
				pluck="defvalue",
			)
		]

	def test_preflight_rejects_impossible_lifecycle_state(self):
		frappe.get_doc(
			{
				"doctype": "DefaultValue",
				"parent": profile.NAMESPACE,
				"parenttype": "__default",
				"parentfield": "system_defaults",
				"defkey": "manifest",
				"defvalue": json.dumps(
					{
						"schema_version": profile.SCHEMA_VERSION,
						"state": "SYSTEM_APPLIED",
						"last_completed_phase": "LANGUAGE",
					}
				),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			with self.assertRaisesRegex(profile.ProfileError, "Impossible profile lifecycle state"):
				profile.status()
		finally:
			frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
			frappe.db.commit()

	def test_site_lock_contention_does_not_change_profile_data(self):
		lock_path = frappe.get_site_path("locks", f"{profile.LOCK_NAME}.lock")
		before = frappe.get_all(
			"DefaultValue", filters={"parent": profile.NAMESPACE}, fields=["defkey", "defvalue"]
		)
		with FileLock(os.path.abspath(lock_path)):
			with self.assertRaisesRegex(profile.ProfileError, "already holds the site lock"):
				profile.apply()
		after = frappe.get_all(
			"DefaultValue", filters={"parent": profile.NAMESPACE}, fields=["defkey", "defvalue"]
		)
		self.assertEqual(after, before)

	def test_snapshot_failure_leaves_no_profile_rows(self):
		insert_row = profile._insert_row
		calls = 0

		def fail_during_snapshot(key, value):
			nonlocal calls
			calls += 1
			insert_row(key, value)
			if calls == 2:
				raise RuntimeError("snapshot fault")

		with patch.object(profile, "_insert_row", side_effect=fail_during_snapshot):
			with self.assertRaisesRegex(profile.ProfileError, "Phase: SNAPSHOT"):
				profile.apply()

		self.assertFalse(frappe.db.exists("DefaultValue", {"parent": profile.NAMESPACE}))

	def test_every_later_manual_change_is_preserved_by_restore(self):
		user = self._make_user("profile-manual@example.com", enabled=1)
		frappe.db.commit()
		state = self._capture_profile_environment()
		language_fields = list(profile.LANGUAGE_VALUES)
		system_fields = list(profile.SYSTEM_VALUES)
		manual_language = {
			"enabled": 0,
			"date_format": "dd-mm-yyyy",
			"time_format": "HH:mm:ss",
			"number_format": "#,###.##",
			"first_day_of_the_week": "Sunday",
		}
		manual_system = {
			"country": "Germany",
			"language": "de",
			"time_zone": "UTC",
			"date_format": "dd-mm-yyyy",
			"time_format": "HH:mm:ss",
			"number_format": "#,###.##",
			"first_day_of_the_week": "Sunday",
		}
		manual_user = {"language": "de", "time_zone": "UTC"}
		manual_defaults = {
			"date_format": "dd-mm-yyyy",
			"time_format": "HH:mm:ss",
			"number_format": "#,###.##",
			"first_day_of_the_week": "Sunday",
			"time_zone": "UTC",
		}

		try:
			profile.apply()
			frappe.db.set_value("Language", "lt", manual_language, update_modified=False)
			frappe.db.set_value("User", user, manual_user, update_modified=False)
			for field, value in manual_defaults.items():
				frappe.defaults.set_default(field, value, user)
			system = frappe.get_single("System Settings")
			system.update(manual_system)
			system.save(ignore_permissions=True)
			frappe.db.commit()

			result = profile.restore()

			self.assertEqual(
				dict(
					zip(
						language_fields,
						frappe.db.get_value("Language", "lt", language_fields),
						strict=True,
					)
				),
				manual_language,
			)
			self.assertEqual(
				dict(
					zip(
						system_fields,
						frappe.db.get_value("System Settings", None, system_fields),
						strict=True,
					)
				),
				manual_system,
			)
			self.assertEqual(
				dict(
					zip(
						manual_user,
						frappe.db.get_value("User", user, list(manual_user)),
						strict=True,
					)
				),
				manual_user,
			)
			self.assertEqual(
				{field: frappe.db.get_default(field, user) for field in manual_defaults},
				manual_defaults,
			)
			skipped = {
				(item["entity"], item["name"], item["field"]) for item in result["skipped_manual_change"]
			}
			expected = {
				*(("Language", "lt", field) for field in manual_language),
				*(("System Settings", "System Settings", field) for field in manual_system),
				*(("User", user, field) for field in manual_user),
				*(("User Default", user, field) for field in manual_defaults),
			}
			self.assertLessEqual(expected, skipped)
		finally:
			self._restore_profile_environment(state)

	def test_deleted_target_is_reported_and_not_recreated(self):
		user = self._make_user("profile-deleted@example.com", enabled=1)
		frappe.db.commit()
		profile.apply()
		frappe.db.delete("User", {"name": user})
		frappe.db.commit()

		result = profile.restore()

		self.assertFalse(frappe.db.exists("User", user))
		self.assertEqual(
			len([item for item in result["missing_target"] if item["name"] == user]),
			len(profile.USER_VALUES) + len(profile.USER_DEFAULT_VALUES),
		)

	def test_leave_and_uninstall_lifecycle_is_irreversible(self):
		state = self._capture_profile_environment()
		try:
			with self.assertRaisesRegex(
				profile.ProfileError,
				r"setup-lithuanian-profile && bench --site .* restore-lithuanian-profile && bench --site .* uninstall-app frappe_lt",
			):
				profile.before_uninstall()
			with self.assertRaisesRegex(
				profile.ProfileError, r"Safe command: bench --site .* setup-lithuanian-profile"
			):
				profile.restore()
			profile.apply()
			with self.assertRaisesRegex(profile.ProfileError, "Uninstall is blocked"):
				profile.before_uninstall()
			with self.assertRaisesRegex(profile.ProfileError, "--confirm-leave-profile"):
				profile.abandon()
			self.assertEqual(profile.status()["state_after"], "APPLIED")

			left = profile.abandon(confirmed=True)

			self.assertEqual(left["state_after"], "ABANDONED")
			with self.assertRaisesRegex(
				profile.ProfileError, r"Safe command: bench --site .* uninstall-app frappe_lt"
			):
				profile.apply()
			with self.assertRaisesRegex(
				profile.ProfileError, r"Safe command: bench --site .* uninstall-app frappe_lt"
			):
				profile.restore()
			with self.assertRaisesRegex(
				profile.ProfileError, r"Safe command: bench --site .* uninstall-app frappe_lt"
			):
				profile.abandon(confirmed=True)
			profile.before_uninstall()
			profile.after_uninstall()
			self.assertEqual(profile.status()["state_after"], "ABSENT")
		finally:
			self._restore_profile_environment(state)

	def test_genuine_reinstall_recovers_a_terminal_marker_with_before_install_authorization(self):
		profile.apply()
		profile.abandon(confirmed=True)
		with self.assertRaisesRegex(profile.ProfileError, "cannot be applied again"):
			profile.apply()

		with patch.object(frappe, "get_installed_apps", return_value=["frappe", "erpnext"]):
			profile.before_install()
		authorized = self._profile_manifest()
		self.assertIsNotNone(authorized["reinstall_authorization"])
		profile._set_install_recovery_authorization(None)
		result = profile.after_install()

		self.assertEqual(result["state_before"], "SNAPSHOTTED")
		self.assertEqual(result["state_after"], "APPLIED")
		self.assertNotIn("reinstall_authorization", self._profile_manifest())

	def test_force_install_while_current_does_not_authorize_terminal_marker_recovery(self):
		profile.apply()
		profile.abandon(confirmed=True)

		with patch.object(frappe, "get_installed_apps", return_value=["frappe", "erpnext", "frappe_lt"]):
			profile.before_install()
		self.assertIsNone(self._profile_manifest()["reinstall_authorization"])
		with self.assertRaisesRegex(profile.ProfileError, "cannot be recovered by a forced current install"):
			profile.after_install()

		self.assertEqual(profile.status()["state_after"], "ABANDONED")
		self.assertIsNone(self._profile_manifest()["reinstall_authorization"])

	def test_install_recovery_lock_contention_retains_persisted_authorization_for_new_process(self):
		profile.apply()
		profile.abandon(confirmed=True)
		lock_path = frappe.get_site_path("locks", f"{profile.LOCK_NAME}.lock")
		with patch.object(frappe, "get_installed_apps", return_value=["frappe", "erpnext"]):
			with FileLock(os.path.abspath(lock_path)):
				with self.assertRaisesRegex(profile.ProfileError, "already holds the site lock"):
					profile.before_install()
		self.assertEqual(profile.status()["state_after"], "ABANDONED")

		with patch.object(frappe, "get_installed_apps", return_value=["frappe", "erpnext"]):
			profile.before_install()
		authorized = self._profile_manifest()
		profile._set_install_recovery_authorization(None)
		with FileLock(os.path.abspath(lock_path)):
			with self.assertRaisesRegex(profile.ProfileError, "already holds the site lock"):
				profile.after_install()
		self.assertEqual(profile.status()["state_after"], "ABANDONED")
		self.assertEqual(self._profile_manifest(), authorized)

		profile._set_install_recovery_authorization(None)
		self.assertEqual(profile.after_install()["state_after"], "APPLIED")

	def test_failed_reinstall_preflight_retains_terminal_marker(self):
		profile.apply()
		profile.abandon(confirmed=True)
		with patch.object(frappe, "get_installed_apps", return_value=["frappe", "erpnext"]):
			profile.before_install()
		authorized = self._profile_manifest()
		profile._set_install_recovery_authorization(None)
		language = frappe.get_doc("Language", "lt").as_dict()
		frappe.db.delete("Language", {"name": "lt"})
		frappe.db.commit()
		try:
			with self.assertRaisesRegex(profile.ProfileError, "Language/lt is missing"):
				profile.after_install()

			self.assertEqual(profile.status()["state_after"], "ABANDONED")
			self.assertEqual(self._profile_manifest(), authorized)
		finally:
			frappe.get_doc(language).insert(ignore_permissions=True)
			frappe.db.commit()
		profile._set_install_recovery_authorization(None)
		self.assertEqual(profile.after_install()["state_after"], "APPLIED")

	def test_failed_reinstall_snapshot_retains_authorization_for_new_process_retry(self):
		profile.apply()
		profile.abandon(confirmed=True)
		with patch.object(frappe, "get_installed_apps", return_value=["frappe", "erpnext"]):
			profile.before_install()
		authorized = self._profile_manifest()
		profile._set_install_recovery_authorization(None)
		create_snapshot = profile._create_snapshot

		def fail_after_snapshot_creation():
			create_snapshot()
			raise RuntimeError("snapshot fault")

		with patch.object(profile, "_create_snapshot", side_effect=fail_after_snapshot_creation):
			with self.assertRaisesRegex(profile.ProfileError, "Phase: SNAPSHOT"):
				profile.after_install()

		self.assertEqual(profile.status()["state_after"], "ABANDONED")
		self.assertEqual(self._profile_manifest(), authorized)
		profile._set_install_recovery_authorization(None)
		self.assertEqual(profile.after_install()["state_after"], "APPLIED")

	def test_terminal_manifest_rejects_invalid_reinstall_authorization(self):
		profile.apply()
		profile.abandon(confirmed=True)
		row = frappe.db.get_value("DefaultValue", {"parent": profile.NAMESPACE, "defkey": "manifest"}, "name")
		manifest = self._profile_manifest()
		manifest["reinstall_authorization"] = {
			"app": "frappe_lt",
			"site": "another-site",
			"marker_name": row,
			"terminal_state": "ABANDONED",
			"authorized_at": manifest["updated_at"],
			"nonce": "0" * 64,
		}
		frappe.db.set_value("DefaultValue", row, "defvalue", json.dumps(manifest))
		frappe.db.commit()
		try:
			with self.assertRaisesRegex(profile.ProfileError, "reinstall authorization"):
				profile.status()
		finally:
			frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
			frappe.db.commit()

	def _profile_manifest(self):
		return json.loads(
			frappe.db.get_value(
				"DefaultValue", {"parent": profile.NAMESPACE, "defkey": "manifest"}, "defvalue"
			)
		)

	def test_commit_and_rollback_clear_locale_caches(self):
		country_before = frappe.get_system_settings("country")
		admin_time_zone_before = frappe.defaults.get_defaults("Administrator").get("time_zone")
		expected_lang_after_rollback = (
			frappe.db.get_value("User", frappe.session.user, "language")
			or frappe.db.get_single_value("System Settings", "language", cache=False)
			or "en"
		)
		frappe.get_cached_doc("Language", "lt")
		frappe.local.lang = "stale-profile-language"
		site = frappe.local.site

		def read_fresh_connection():
			fresh = {}
			errors = []

			def read():
				try:
					frappe.init(site=site)
					frappe.connect()
					fresh.update(
						country=frappe.get_system_settings("country"),
						date_format=frappe.get_cached_doc("Language", "lt").date_format,
						time_zone=frappe.defaults.get_defaults("Administrator").get("time_zone"),
					)
				except Exception as exc:
					errors.append(exc)
				finally:
					frappe.destroy()

			worker = threading.Thread(target=read)
			worker.start()
			worker.join(timeout=5)
			self.assertFalse(worker.is_alive())
			self.assertFalse(errors)
			return fresh

		def fail_system(_storage):
			system = frappe.get_single("System Settings")
			system.country = "Germany"
			system.save(ignore_permissions=True)
			raise RuntimeError("system cache fault")

		with patch.object(profile, "_apply_system", side_effect=fail_system):
			with self.assertRaises(profile.ProfileError):
				profile.apply()

		self.assertEqual(frappe.get_system_settings("country"), country_before)
		self.assertEqual(frappe.local.lang, expected_lang_after_rollback)
		self.assertEqual(frappe.get_cached_doc("Language", "lt").date_format, "yyyy-mm-dd")
		self.assertEqual(
			read_fresh_connection(),
			{
				"country": country_before,
				"date_format": "yyyy-mm-dd",
				"time_zone": admin_time_zone_before,
			},
		)

		profile.apply()

		self.assertEqual(frappe.get_system_settings("country"), "Lithuania")
		self.assertEqual(frappe.local.lang, "lt")
		self.assertEqual(frappe.defaults.get_defaults("Administrator")["time_zone"], "Europe/Vilnius")
		self.assertEqual(
			read_fresh_connection(),
			{"country": "Lithuania", "date_format": "yyyy-mm-dd", "time_zone": "Europe/Vilnius"},
		)

	def test_all_service_and_command_json_results_are_complete_sorted_and_stable(self):
		state = self._capture_profile_environment()
		try:
			self._assert_all_service_and_command_json_results()
		finally:
			self._restore_profile_environment(state)
		self._assert_profile_environment_restored(state)

	def _assert_all_service_and_command_json_results(self):
		def run_command(method, **kwargs):
			outputs = []
			with (
				patch.object(commands, "get_site", return_value=frappe.local.site),
				patch.object(frappe, "init"),
				patch.object(frappe, "connect"),
				patch.object(frappe, "destroy"),
				patch.object(commands.click, "echo", side_effect=outputs.append),
			):
				commands._run_profile_command(None, method, **kwargs)
			self.assertEqual(len(outputs), 1)
			result = json.loads(outputs[0])
			self.assertEqual(outputs[0], json.dumps(result, ensure_ascii=False, sort_keys=True))
			self.assertEqual(
				set(result),
				{
					"operation",
					"state_before",
					"state_after",
					"phase",
					"changed",
					"unchanged",
					"skipped_manual_change",
					"missing_target",
				},
			)
			for key in ("changed", "unchanged", "skipped_manual_change", "missing_target"):
				self.assertEqual(
					result[key],
					sorted(
						result[key],
						key=lambda row: (row["entity"], row["name"], row["field"], row["reason"]),
					),
				)
				self.assertTrue(all(set(row) == {"entity", "name", "field", "reason"} for row in result[key]))
			return outputs[0], result

		setup_json, setup = run_command("apply")
		first_status_json, status = run_command("status")
		second_status_json, _ = run_command("status")
		restore_json, restored = run_command("restore")
		self.assertEqual(first_status_json, second_status_json)
		self.assertEqual(
			(setup["operation"], status["operation"], restored["operation"]), ("setup", "status", "restore")
		)

		frappe.db.delete("DefaultValue", {"parent": profile.NAMESPACE})
		frappe.db.commit()
		second_setup_json, _ = run_command("apply")
		leave_json, left = run_command("abandon", confirmed=True)
		self.assertEqual(setup_json, second_setup_json)
		self.assertEqual(left["operation"], "leave")
		self.assertTrue(restore_json)
		self.assertTrue(leave_json)

	def test_admin_commit_before_restore_locks_is_seen_by_all_guarded_comparisons(self):
		user = self._make_user("profile-race-before@example.com", enabled=1)
		language_before = frappe.db.get_value("Language", "lt", "date_format")
		system_before = frappe.db.get_single_value("System Settings", "country", cache=False)
		precision_before = frappe.db.get_single_value("System Settings", "float_precision", cache=False)
		precision_after = "7" if str(precision_before) != "7" else "8"
		currency_before = frappe.db.get_single_value("System Settings", "currency", cache=False)
		global_currency_before = frappe.defaults.get_global_default("currency")
		frappe.db.set_value("User", user, "language", "en", update_modified=False)
		frappe.defaults.set_default("date_format", "dd/mm/yyyy", user)
		frappe.defaults.clear_default("number_format", parent=user)
		frappe.db.set_value("Language", "lt", "date_format", "dd.mm.yyyy", update_modified=False)
		frappe.db.set_single_value("System Settings", "country", "India")
		frappe.db.commit()
		profile.apply()
		update_started = threading.Event()
		worker_errors = []
		site = frappe.local.site

		def concurrent_admin_change():
			try:
				frappe.init(site=site)
				frappe.connect()
				frappe.db.set_value("User", user, "language", "de", update_modified=False)
				frappe.defaults.set_default("date_format", "dd-mm-yyyy", user)
				frappe.defaults.set_default("number_format", "#.###,##", user)
				frappe.db.set_value("Language", "lt", "date_format", "dd-mm-yyyy", update_modified=False)
				frappe.db.set_single_value("System Settings", "country", "Germany")
				frappe.db.set_single_value(
					"System Settings", "float_precision", precision_after, update_modified=False
				)
				update_started.set()
				time.sleep(0.5)
				frappe.db.commit()
			except Exception as exc:
				worker_errors.append(exc)
				update_started.set()
			finally:
				frappe.destroy()

		worker = threading.Thread(target=concurrent_admin_change)
		worker.start()
		self.assertTrue(update_started.wait(timeout=5))
		try:
			result = profile.restore()
			worker.join(timeout=5)

			self.assertFalse(worker.is_alive())
			self.assertFalse(worker_errors)
			self.assertEqual(frappe.db.get_value("User", user, "language"), "de")
			self.assertEqual(frappe.db.get_default("date_format", user), "dd-mm-yyyy")
			self.assertEqual(frappe.db.get_default("number_format", user), "#.###,##")
			self.assertEqual(frappe.db.get_value("Language", "lt", "date_format"), "dd-mm-yyyy")
			self.assertEqual(frappe.db.get_single_value("System Settings", "country", cache=False), "Germany")
			self.assertEqual(
				str(frappe.db.get_single_value("System Settings", "float_precision", cache=False)),
				precision_after,
			)
			self.assertEqual(
				frappe.db.get_single_value("System Settings", "currency", cache=False), currency_before
			)
			self.assertEqual(frappe.defaults.get_global_default("currency"), global_currency_before)
			skipped = {
				(item["entity"], item["name"], item["field"]) for item in result["skipped_manual_change"]
			}
			self.assertLessEqual(
				{
					("User", user, "language"),
					("User Default", user, "date_format"),
					("User Default", user, "number_format"),
					("Language", "lt", "date_format"),
					("System Settings", "System Settings", "country"),
				},
				skipped,
			)
		finally:
			worker.join(timeout=5)
			frappe.db.set_value("Language", "lt", "date_format", language_before, update_modified=False)
			frappe.db.set_single_value("System Settings", "country", system_before)
			frappe.db.set_single_value(
				"System Settings", "float_precision", precision_before, update_modified=False
			)
			frappe.db.set_single_value("System Settings", "currency", currency_before, update_modified=False)
			frappe.defaults.set_default("currency", global_currency_before, "__default")
			frappe.db.commit()

	def test_admin_commits_before_setup_phase_locks_become_refreshed_restore_values(self):
		state = self._capture_profile_environment()
		user = self._make_user("profile-setup-before-lock@example.com", enabled=1)
		frappe.db.commit()
		site = frappe.local.site
		errors = []

		def committed(change):
			def run():
				try:
					frappe.init(site=site)
					frappe.connect()
					change()
					frappe.db.commit()
				except Exception as exc:
					errors.append(exc)
				finally:
					frappe.destroy()

			worker = threading.Thread(target=run)
			worker.start()
			worker.join(timeout=5)
			self.assertFalse(worker.is_alive())
			self.assertFalse(errors)

		def language_change():
			frappe.db.set_value("Language", "lt", "date_format", "dd-mm-yyyy", update_modified=False)

		def system_change():
			frappe.db.set_single_value("System Settings", "country", "Germany", update_modified=False)
			frappe.db.set_single_value("System Settings", "float_precision", "8", update_modified=False)
			frappe.defaults.clear_default("date_format", parent="__default")
			frappe.defaults.add_default("date_format", "manual-global-1", "__default")
			frappe.defaults.add_default("date_format", "manual-global-2", "__default")

		def user_change():
			frappe.db.set_value("User", user, "language", "de", update_modified=False)
			frappe.defaults.clear_default("time_format", parent=user)
			frappe.defaults.add_default("time_format", "manual-user-1", user)
			frappe.defaults.add_default("time_format", "manual-user-2", user)

		apply_language = profile._apply_language
		apply_system = profile._apply_system
		apply_users = profile._apply_users
		try:
			with (
				patch.object(
					profile,
					"_apply_language",
					side_effect=lambda storage: (committed(language_change), apply_language(storage))[1],
				),
				patch.object(
					profile,
					"_apply_system",
					side_effect=lambda storage: (committed(system_change), apply_system(storage))[1],
				),
				patch.object(
					profile,
					"_apply_users",
					side_effect=lambda storage: (committed(user_change), apply_users(storage))[1],
				),
			):
				profile.apply()

			profile.restore()

			self.assertEqual(frappe.db.get_value("Language", "lt", "date_format"), "dd-mm-yyyy")
			self.assertEqual(frappe.db.get_single_value("System Settings", "country", cache=False), "Germany")
			self.assertEqual(
				str(frappe.db.get_single_value("System Settings", "float_precision", cache=False)), "8"
			)
			self.assertEqual(
				profile._default_states("__default", profile.GLOBAL_DEFAULT_VALUES)["date_format"],
				{"exists": True, "value": ["manual-global-1", "manual-global-2"]},
			)
			self.assertEqual(frappe.db.get_value("User", user, "language"), "de")
			self.assertEqual(
				profile._default_states(user)["time_format"],
				{"exists": True, "value": ["manual-user-1", "manual-user-2"]},
			)
		finally:
			self._restore_profile_environment(state)

	def test_admin_writes_after_setup_phase_locks_wait_and_win_after_each_phase_commit(self):
		state = self._capture_profile_environment()
		user = self._make_user("profile-setup-after-lock@example.com", enabled=1)
		frappe.defaults.clear_default("number_format", parent=user)
		frappe.defaults.clear_default("date_format", parent=user)
		frappe.defaults.add_default("date_format", "duplicate-before-1", user)
		frappe.defaults.add_default("date_format", "duplicate-before-2", user)
		frappe.db.commit()
		site = frappe.local.site
		workers = []
		errors = []
		phase_events = {}

		def start_worker(name, change):
			attempted = threading.Event()
			done = threading.Event()
			phase_events[name] = (attempted, done)

			def run():
				for retry in range(10):
					try:
						frappe.init(site=site)
						frappe.connect()
						attempted.set()
						change()
						frappe.db.commit()
						break
					except frappe.QueryDeadlockError as exc:
						frappe.db.rollback()
						if retry == 9:
							errors.append((name, exc))
						else:
							time.sleep(0.1)
					except Exception as exc:
						errors.append((name, exc))
						break
					finally:
						frappe.destroy()
				done.set()

			worker = threading.Thread(target=run, name=name)
			workers.append(worker)
			worker.start()

		def assert_phase_blocked(names):
			for name in names:
				self.assertTrue(phase_events[name][0].wait(timeout=5))
			time.sleep(0.5)
			self.assertTrue(all(not phase_events[name][1].is_set() for name in names))

		def language_change():
			frappe.db.set_value("Language", "lt", "date_format", "dd-mm-yyyy", update_modified=False)

		def system_change():
			frappe.db.set_single_value("System Settings", "country", "Germany", update_modified=False)
			frappe.db.set_single_value("System Settings", "float_precision", "8", update_modified=False)

		def global_default_change():
			frappe.defaults.set_default("date_format", "after-system-lock", "__default")

		def user_change():
			frappe.db.set_value("User", user, "language", "de", update_modified=False)

		def duplicate_default_change():
			frappe.defaults.set_default("date_format", "after-user-lock", user)

		def missing_default_change():
			frappe.defaults.set_default("number_format", "after-missing-lock", user)

		save_snapshot_row = profile._save_snapshot_row
		started = set()

		def save_with_competing_writes(row_name, payload):
			if set(payload) == {"values"} and "language" not in started:
				started.add("language")
				start_worker("language", language_change)
				assert_phase_blocked(["language"])
			elif "defaults" in payload and "name" not in payload and "system" not in started:
				started.add("system")
				start_worker("system", system_change)
				start_worker("global-default", global_default_change)
				assert_phase_blocked(["system", "global-default"])
			elif payload.get("name") == user and "user" not in started:
				started.add("user")
				start_worker("user", user_change)
				start_worker("duplicate-default", duplicate_default_change)
				start_worker("missing-default", missing_default_change)
				assert_phase_blocked(["user", "duplicate-default", "missing-default"])
			return save_snapshot_row(row_name, payload)

		try:
			with patch.object(profile, "_save_snapshot_row", side_effect=save_with_competing_writes):
				profile.apply()
			for worker in workers:
				worker.join(timeout=5)
			self.assertTrue(all(not worker.is_alive() for worker in workers))
			self.assertFalse(errors)
			frappe.db.rollback()
			self.assertEqual(frappe.db.get_value("Language", "lt", "date_format"), "dd-mm-yyyy")
			self.assertEqual(frappe.db.get_single_value("System Settings", "country", cache=False), "Germany")
			self.assertEqual(
				str(frappe.db.get_single_value("System Settings", "float_precision", cache=False)), "8"
			)
			self.assertEqual(frappe.db.get_default("date_format"), "after-system-lock")
			self.assertEqual(frappe.db.get_value("User", user, "language"), "de")
			self.assertEqual(
				profile._default_states(user)["date_format"],
				{
					"exists": True,
					"value": "after-user-lock",
				},
			)
			self.assertEqual(
				profile._default_states(user)["number_format"],
				{
					"exists": True,
					"value": "after-missing-lock",
				},
			)

			profile.restore()

			self.assertEqual(frappe.db.get_value("Language", "lt", "date_format"), "dd-mm-yyyy")
			self.assertEqual(frappe.db.get_single_value("System Settings", "country", cache=False), "Germany")
			self.assertEqual(frappe.db.get_default("date_format"), "after-system-lock")
			self.assertEqual(frappe.db.get_value("User", user, "language"), "de")
			self.assertEqual(frappe.db.get_default("date_format", user), "after-user-lock")
			self.assertEqual(frappe.db.get_default("number_format", user), "after-missing-lock")
		finally:
			for worker in workers:
				worker.join(timeout=5)
			self._restore_profile_environment(state)

	def test_admin_writes_after_restore_comparison_wait_and_win_after_restore_commit(self):
		user = self._make_user("profile-race-after@example.com", enabled=1)
		language_before = frappe.db.get_value("Language", "lt", "date_format")
		system_before = frappe.db.get_single_value("System Settings", "country", cache=False)
		precision_before = frappe.db.get_single_value("System Settings", "float_precision", cache=False)
		precision_after = "7" if str(precision_before) != "7" else "8"
		currency_before = frappe.db.get_single_value("System Settings", "currency", cache=False)
		global_currency_before = frappe.defaults.get_global_default("currency")
		global_country_before = profile._default_states("__default", profile.GLOBAL_DEFAULT_VALUES)["country"]
		frappe.db.set_value("User", user, "language", "en", update_modified=False)
		frappe.defaults.set_default("date_format", "dd/mm/yyyy", user)
		frappe.defaults.clear_default("number_format", parent=user)
		frappe.db.set_value("Language", "lt", "date_format", "dd.mm.yyyy", update_modified=False)
		frappe.db.set_single_value("System Settings", "country", "India")
		frappe.db.commit()
		profile.apply()
		unmanaged_probe = "frappe_lt_unmanaged_probe"
		frappe.db.sql(
			"insert into tabSingles (doctype, field, value) values (%s, %s, %s)",
			("System Settings", unmanaged_probe, "preserve-me"),
		)
		frappe.db.commit()
		site = frappe.local.site
		snapshot_isolation = frappe.db.sql("select @@innodb_snapshot_isolation", pluck=True)[0]
		original_execute = profile._execute_restore_plan
		attempted = []
		completed = []
		errors = []
		workers = []
		completed_before_restore = []

		def start_worker(name, change):
			attempt = threading.Event()
			done = threading.Event()
			attempted.append(attempt)
			completed.append(done)

			def run():
				for retry in range(10):
					try:
						frappe.init(site=site)
						frappe.connect()
						frappe.db.sql(f"set session innodb_snapshot_isolation={int(snapshot_isolation)}")
						attempt.set()
						change()
						frappe.db.commit()
						break
					except frappe.QueryDeadlockError as exc:
						frappe.db.rollback()
						if retry == 9:
							errors.append((name, exc))
						else:
							time.sleep(0.1)
					except Exception as exc:
						errors.append((name, exc))
						break
					finally:
						frappe.destroy()
				done.set()

			worker = threading.Thread(target=run, name=name)
			workers.append(worker)
			worker.start()

		def execute_with_competing_writes(actions, locked):
			if workers:
				return original_execute(actions, locked)
			start_worker(
				"user", lambda: frappe.db.set_value("User", user, "language", "de", update_modified=False)
			)
			start_worker(
				"existing-default", lambda: frappe.defaults.set_default("date_format", "dd-mm-yyyy", user)
			)
			start_worker(
				"absent-default", lambda: frappe.defaults.set_default("number_format", "#.###,##", user)
			)
			start_worker(
				"language",
				lambda: frappe.db.set_value(
					"Language", "lt", "date_format", "dd-mm-yyyy", update_modified=False
				),
			)
			start_worker(
				"system", lambda: frappe.db.set_single_value("System Settings", "country", "Germany")
			)
			start_worker(
				"system-default",
				lambda: frappe.defaults.set_default("country", "Germany", "__default"),
			)
			start_worker(
				"unrelated-system",
				lambda: frappe.db.set_single_value(
					"System Settings", "float_precision", precision_after, update_modified=False
				),
			)
			for event in attempted:
				self.assertTrue(event.wait(timeout=5))
			time.sleep(0.5)
			completed_before_restore.extend(event.is_set() for event in completed)
			return original_execute(actions, locked)

		try:
			with patch.object(profile, "_execute_restore_plan", side_effect=execute_with_competing_writes):
				profile.restore()
			for worker in workers:
				worker.join(timeout=5)
			frappe.db.rollback()

			self.assertEqual(completed_before_restore, [False] * 7)
			self.assertFalse(errors)
			self.assertTrue(all(not worker.is_alive() for worker in workers))
			self.assertEqual(frappe.db.get_value("User", user, "language"), "de")
			self.assertEqual(frappe.db.get_default("date_format", user), "dd-mm-yyyy")
			self.assertEqual(frappe.db.get_default("number_format", user), "#.###,##")
			self.assertEqual(frappe.db.get_value("Language", "lt", "date_format"), "dd-mm-yyyy")
			self.assertEqual(frappe.db.get_single_value("System Settings", "country", cache=False), "Germany")
			self.assertEqual(
				profile._default_states("__default", profile.GLOBAL_DEFAULT_VALUES)["country"],
				{"exists": True, "value": "Germany"},
			)
			self.assertEqual(
				str(frappe.db.get_single_value("System Settings", "float_precision", cache=False)),
				precision_after,
			)
			self.assertEqual(
				frappe.db.get_single_value("System Settings", "currency", cache=False), currency_before
			)
			self.assertEqual(frappe.defaults.get_global_default("currency"), global_currency_before)
			self.assertEqual(
				frappe.db.sql(
					"select value from tabSingles where doctype=%s and field=%s",
					("System Settings", unmanaged_probe),
					pluck=True,
				),
				["preserve-me"],
			)
		finally:
			for worker in workers:
				worker.join(timeout=5)
			frappe.db.rollback()
			frappe.db.set_value("Language", "lt", "date_format", language_before, update_modified=False)
			frappe.db.set_single_value("System Settings", "country", system_before)
			frappe.db.set_single_value(
				"System Settings", "float_precision", precision_before, update_modified=False
			)
			frappe.db.set_single_value("System Settings", "currency", currency_before, update_modified=False)
			frappe.defaults.set_default("currency", global_currency_before, "__default")
			profile._restore_default("__default", "country", global_country_before)
			frappe.db.delete("Singles", {"doctype": "System Settings", "field": unmanaged_probe})
			frappe.db.commit()

	def test_system_restore_race_is_safe_with_snapshot_isolation_disabled(self):
		snapshot_isolation = frappe.db.sql("select @@innodb_snapshot_isolation", pluck=True)[0]
		frappe.db.sql("set session innodb_snapshot_isolation=OFF")
		try:
			self.test_admin_writes_after_restore_comparison_wait_and_win_after_restore_commit()
		finally:
			frappe.db.rollback()
			frappe.db.sql(f"set session innodb_snapshot_isolation={int(snapshot_isolation)}")

	def test_restore_upgrades_read_committed_before_locking_missing_defaults(self):
		isolation = frappe.db.sql("select @@tx_isolation", pluck=True)[0]
		frappe.db.commit()
		frappe.db.sql("set session transaction isolation level read committed")
		try:
			self.test_admin_writes_after_restore_comparison_wait_and_win_after_restore_commit()
			self.assertEqual(frappe.db.sql("select @@tx_isolation", pluck=True)[0], "REPEATABLE-READ")
		finally:
			frappe.db.rollback()
			frappe.db.sql(f"set session transaction isolation level {isolation.replace('-', ' ')}")

	def test_restore_fails_before_mutation_when_repeatable_read_cannot_be_established(self):
		before = frappe.get_all(
			"DefaultValue", filters={"parent": profile.NAMESPACE}, fields=["name", "defkey", "defvalue"]
		)
		db_sql = frappe.db.sql

		def reject_isolation(query, *args, **kwargs):
			if query == "set session transaction isolation level repeatable read":
				raise RuntimeError("unsupported isolation")
			return db_sql(query, *args, **kwargs)

		with patch.object(frappe.db, "sql", side_effect=reject_isolation):
			with self.assertRaisesRegex(
				profile.ProfileError,
				r"REPEATABLE READ.*Safe command: bench --site .* restore-lithuanian-profile",
			):
				profile.restore()

		self.assertEqual(
			frappe.get_all(
				"DefaultValue",
				filters={"parent": profile.NAMESPACE},
				fields=["name", "defkey", "defvalue"],
			),
			before,
		)
