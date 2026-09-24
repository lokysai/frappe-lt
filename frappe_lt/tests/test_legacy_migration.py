import json
import os
import stat
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from frappe_lt import install, legacy_migration
from frappe_lt.legacy_migration import PACKAGE_SHA256, _publish, _trusted_plan, authenticate_package, classify


class LegacyPackageTests(unittest.TestCase):
	def test_opted_in_preflight_and_apply_block_when_target_extraction_fails(self):
		frappe = SimpleNamespace(db=SimpleNamespace(rollback=lambda: None))
		run_id = "f" * 32
		with (
			patch.dict("sys.modules", {"frappe": frappe}),
			patch.object(
				legacy_migration, "_inputs", side_effect=install.InstallError("TARGET_EXTRACTION_FAILED")
			) as inputs,
			patch.object(legacy_migration, "_private_root", return_value=Path("/unused")),
			patch.object(legacy_migration, "_trusted_plan", return_value={"inventory_digest": "a" * 64}),
			patch.object(legacy_migration, "_check_lock"),
			patch.object(legacy_migration, "_site_lock", return_value=nullcontext()),
			patch.object(legacy_migration, "_marker", return_value=[]),
			patch.object(legacy_migration, "_snapshot", side_effect=AssertionError("do not classify")),
		):
			self.assertEqual(
				legacy_migration.preflight("test.local", "original.csv", allow_exact_inventory_patch=True),
				{"exit_code": 1, "state": "blocked"},
			)
			self.assertEqual(
				legacy_migration.apply(
					"test.local", "original.csv", run_id, allow_exact_inventory_patch=True
				),
				{"exit_code": 1, "state": "blocked", "run_id": run_id},
			)
			self.assertEqual(inputs.call_count, 2)
			inputs.assert_called_with("test.local", "original.csv", None, allow_exact_inventory_patch=True)

	def test_opt_in_reauthenticates_install_target_without_bypassing_legacy_inputs(self):
		frappe = SimpleNamespace(utils=SimpleNamespace(sanitize_html=str))
		trusted = ({("Save", None): {"source_digest": "digest"}}, None, None, None)
		with (
			patch.dict("sys.modules", {"frappe": frappe}),
			patch("frappe_lt.inventory.verify_environment") as strict,
			patch.object(
				install, "_preflight", return_value={"site": "test.local", "inventory_digest": "a" * 64}
			) as gate,
			patch.object(
				legacy_migration, "authenticate_package", return_value={("Save", ""): "Saugoti"}
			) as package,
			patch("frappe_lt.catalog_quality._load_trusted", return_value=trusted),
			patch("frappe_lt.catalog_quality._validate_inventory", return_value=trusted[0]),
			patch("frappe_lt.inventory.load_compatibility", return_value={"inventory_digest": "a" * 64}),
			patch.object(legacy_migration, "_site_policy", return_value=(set(), "b" * 64)) as policy,
		):
			legacy_migration._inputs("test.local", "original.csv", None)
			strict.assert_called_once_with(frappe, site="test.local", require_clean_upstream=True)
			gate.assert_not_called()
			strict.reset_mock()
			result = legacy_migration._inputs(
				"test.local", "original.csv", None, allow_exact_inventory_patch=True
			)
			self.assertEqual(result[0], {("Save", ""): "Saugoti"})
			self.assertEqual(result[1], {("Save", ""): {"source_digest": "digest"}})
			gate.assert_called_once_with("original.csv", None, classify_site=False)
			strict.assert_not_called()
			self.assertEqual(package.call_count, 2)
			self.assertEqual(policy.call_count, 2)
			for ready in (
				{"site": "other.local", "inventory_digest": "a" * 64},
				{"site": "test.local", "inventory_digest": "c" * 64},
			):
				with patch.object(install, "_preflight", return_value=ready):
					with self.assertRaises(ValueError):
						legacy_migration._inputs(
							"test.local", "original.csv", None, allow_exact_inventory_patch=True
						)
			with patch.object(
				install, "_preflight", side_effect=install.InstallError("TARGET_EXTRACTION_FAILED")
			):
				with self.assertRaisesRegex(ValueError, "TARGET_EXTRACTION_FAILED"):
					legacy_migration._inputs(
						"test.local", "original.csv", None, allow_exact_inventory_patch=True
					)

	def test_original_package_is_required_before_fingerprints_are_generated(self):
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "legacy.csv"
			path.write_text("Language,Source Text,Context,Translated Text\nlt,Item,,Prekė\n")
			with self.assertRaisesRegex(ValueError, "digest"):
				authenticate_package(path, lambda value: value)

	def test_identical_legacy_duplicates_are_all_planned_but_changed_override_survives(self):
		rows = [
			{
				"name": "a",
				"language": "lt",
				"source_text": "Item",
				"context": None,
				"translated_text": "Senas",
			},
			{"name": "b", "language": "lt", "source_text": "Item", "context": "", "translated_text": "Senas"},
			{
				"name": "c",
				"language": "lt",
				"source_text": "Other",
				"context": "",
				"translated_text": "Naujas",
			},
		]
		result = classify(rows, {("Item", ""): "Senas", ("Other", ""): "Senas"}, {}, set())
		self.assertEqual(result["delete"], ["a", "b"])
		self.assertEqual(result["overrides"], ["c"])
		self.assertEqual(result["extras"], [])

	def test_site_exception_waives_english_only_not_tokens_or_markup(self):
		inventory = {("<b>Pay {0}</b>", ""): {"source_digest": "digest"}}
		approved = {("<b>Pay {0}</b>", "", "digest")}
		row = {
			"name": "extra",
			"language": "lt",
			"source_text": "<b>Pay {0}</b>",
			"context": None,
			"translated_text": "Pay {1}",
		}
		codes = {item["code"] for item in classify([row], {}, inventory, approved)["blocked"]}
		self.assertIn("PRESERVED_TOKEN_MISMATCH", codes)
		self.assertIn("HTML_EQUIVALENCE_MISMATCH", codes)

	def test_active_contextless_english_override_is_blocked(self):
		row = {
			"name": "english",
			"language": "lt",
			"source_text": "Save",
			"context": None,
			"translated_text": "Save",
		}
		active = {("Save", None): {"source_digest": "digest"}}
		self.assertEqual(
			[finding["code"] for finding in classify([row], {}, active, set())["blocked"]],
			["ENGLISH_FALLBACK"],
		)
		row["translated_text"] = " Save "
		self.assertIn(
			"ENGLISH_FALLBACK",
			[finding["code"] for finding in classify([row], {}, active, set())["blocked"]],
		)

	def test_unused_english_extra_is_reported_without_blocking(self):
		row = {
			"name": "extra",
			"language": "lt",
			"source_text": "Unused",
			"context": None,
			"translated_text": "Unused",
		}
		result = classify([row], {}, {}, set())
		self.assertEqual(result["english_extras"], ["extra"])
		self.assertEqual(result["extras"], ["extra"])
		self.assertEqual(result["blocked"], [])

	def test_empty_active_override_blocks_fallback_even_with_exception(self):
		row = {
			"name": "empty",
			"language": "lt",
			"source_text": "Save",
			"context": None,
			"translated_text": "",
		}
		active = {("Save", None): {"source_digest": "digest"}}
		result = classify([row], {}, active, {("Save", "", "digest")})
		self.assertIn("ENGLISH_FALLBACK", [finding["code"] for finding in result["blocked"]])

	def test_absent_key_with_null_value_is_never_original_package(self):
		row = {
			"name": "null",
			"language": "lt",
			"source_text": "Unrelated",
			"context": None,
			"translated_text": None,
		}
		result = classify([row], {("Item", ""): None}, {}, set())
		self.assertEqual(result["delete"], [])
		self.assertEqual(result["extras"], ["null"])

	def test_case_insensitive_sql_result_is_never_skipped_or_deleted(self):
		row = {
			"name": "upper",
			"language": "LT",
			"source_text": "Item",
			"context": None,
			"translated_text": "Prekė",
		}
		result = classify([row], {("Item", ""): "Prekė"}, {}, set())
		self.assertEqual(result["delete"], [])
		self.assertEqual(result["blocked"], [{"name": "upper", "code": "LANGUAGE_MISMATCH"}])

	def test_flattened_runtime_key_collision_blocks_database_rows(self):
		for source, context, inventory in (
			("Foo:Bar", None, {("Foo", "Bar"): {"source_digest": "digest"}}),
			("Foo", "Bar", {("Foo:Bar", None): {"source_digest": "digest"}}),
			(
				"Foo",
				"Bar",
				{("Foo", "Bar"): {"source_digest": "digest"}, ("Foo:Bar", None): {"source_digest": "other"}},
			),
		):
			with self.subTest(source=source, context=context):
				row = {
					"name": "collision",
					"language": "lt",
					"source_text": source,
					"context": context,
					"translated_text": source,
				}
				result = classify([row], {}, inventory, set())
				self.assertIn("RUNTIME_KEY_COLLISION", [item["code"] for item in result["blocked"]])
				matched = classify([row], {(source, context or ""): source}, inventory, set())
				self.assertEqual(matched["delete"], ["collision"])
				self.assertIn("RUNTIME_KEY_COLLISION", [item["code"] for item in matched["blocked"]])

	def test_private_recovery_plan_rejects_malformed_or_nonexistent_deletes(self):
		run_id = "a" * 32
		plan = {
			"schema_version": 1,
			"run_id": run_id,
			"site": "development.localhost",
			"state": "planned",
			"package_sha256": PACKAGE_SHA256,
			"inventory_digest": "b" * 64,
			"policy_sha256": "c" * 64,
			"rows": [],
			"classification": {
				"delete": ["missing"],
				"overrides": [],
				"extras": [],
				"english_extras": [],
				"duplicates": [],
				"blocked": [],
			},
		}
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			_publish(root / (run_id + ".planned.json"), plan)
			with self.assertRaisesRegex(ValueError, "invalid saved migration rows"):
				_trusted_plan(root, "development.localhost", run_id)
			plan["classification"]["delete"] = []
			_publish(root / (run_id + ".planned.json"), plan, replace=True)
			self.assertEqual(_trusted_plan(root, "development.localhost", run_id), plan)
			plan["classification"].pop("blocked")
			_publish(root / (run_id + ".planned.json"), plan, replace=True)
			with self.assertRaisesRegex(ValueError, "invalid saved migration plan"):
				_trusted_plan(root, "development.localhost", run_id)


@unittest.skipUnless(
	os.environ.get("FRAPPE_LT_LEGACY_TEST_SITE"), "requires live Frappe site and original CSV"
)
class LegacyMigrationDBTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		import frappe

		cls.frappe = frappe
		cls.site = os.environ["FRAPPE_LT_LEGACY_TEST_SITE"]
		cls.package = os.environ["FRAPPE_LT_ORIGINAL_PACKAGE"]
		frappe.init(site=cls.site, sites_path=".")
		frappe.connect()

	@classmethod
	def tearDownClass(cls):
		cls.frappe.destroy()

	def setUp(self):
		self.names = []

	def tearDown(self):
		for name in self.names:
			self.frappe.db.sql("DELETE FROM `tabTranslation` WHERE name = %s", name)
		self.frappe.db.commit()

	def insert(self, source, translation, *, context=None, language="lt", db=None):
		db = db or self.frappe.db
		name = "lt_migration_" + uuid.uuid4().hex
		db.sql(
			"INSERT INTO `tabTranslation` (name, language, source_text, context, translated_text) "
			"VALUES (%s, %s, %s, %s, %s)",
			(name, language, source, context, translation),
		)
		self.names.append(name)
		return name

	def test_exact_and_other_language_preserved_with_idempotent_rerun(self):
		from frappe_lt.legacy_migration import apply, preflight

		exact = self.insert("Item", "Prekė")
		other = self.insert("Item", "Prekė", language="de")
		contextual = self.insert("Item", "Mano prekė", context="Site context")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		self.assertEqual(planned["exit_code"], 0)
		self.assertIn(planned["delete"], (1, 2))  # this test site may have a legacy duplicate
		private = Path(self.frappe.get_site_path("private")) / "frappe_lt_legacy_migration"
		report = private / (planned["run_id"] + ".planned.json")
		self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
		self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
		plan = json.loads(report.read_bytes())
		self.assertEqual(plan["run_id"], planned["run_id"])
		self.assertIn(exact, plan["classification"]["delete"])
		self.assertIn(
			{
				"name": exact,
				"language": "lt",
				"source_text": "Item",
				"context": None,
				"translated_text": "Prekė",
			},
			plan["rows"],
		)
		result = apply(self.site, self.package, planned["run_id"])
		self.assertEqual(result["state"], "committed")
		self.assertFalse(self.frappe.db.exists("Translation", exact))
		self.assertTrue(self.frappe.db.exists("Translation", other))
		self.assertEqual(self.frappe.db.get_value("Translation", contextual, "translated_text"), "Mano prekė")
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "no_op")
		fresh = preflight(self.site, self.package)
		self.assertEqual(fresh["delete"], 0)
		self.assertEqual(apply(self.site, self.package, fresh["run_id"])["state"], "no_op")

	def test_second_connection_insert_invalidates_planned_key(self):
		from frappe.database import get_db

		from frappe_lt.legacy_migration import apply, preflight

		first = self.insert("Item", "Prekė")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		conf = self.frappe.local.conf
		second = get_db(
			host=conf.db_host,
			port=conf.db_port,
			user=conf.db_name,
			password=conf.db_password,
			cur_db_name=conf.db_name,
		)
		try:
			inserted = self.insert("Item", "Prekė", db=second)
			second.commit()
		finally:
			second.close()
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "stale")
		final = (
			Path(self.frappe.get_site_path("private"))
			/ "frappe_lt_legacy_migration"
			/ (planned["run_id"] + ".final.json")
		)
		self.assertEqual(json.loads(final.read_bytes())["state"], "rolled_back")
		self.assertTrue(self.frappe.db.exists("Translation", first))
		self.assertTrue(self.frappe.db.exists("Translation", inserted))

	def test_second_connection_edit_invalidates_plan_without_deletion(self):
		from frappe.database import get_db

		from frappe_lt.legacy_migration import apply, preflight

		name = self.insert("Item", "Prekė")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		conf = self.frappe.local.conf
		second = get_db(
			host=conf.db_host,
			port=conf.db_port,
			user=conf.db_name,
			password=conf.db_password,
			cur_db_name=conf.db_name,
		)
		try:
			second.sql(
				"UPDATE `tabTranslation` SET translated_text = %s WHERE name = %s", ("Prekė ranka", name)
			)
			second.commit()
		finally:
			second.close()
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "stale")
		self.assertEqual(self.frappe.db.get_value("Translation", name, "translated_text"), "Prekė ranka")

	def test_uppercase_language_cannot_escape_preflight(self):
		from frappe_lt.legacy_migration import apply, preflight

		name = self.insert("Item", "Prekė", language="LT")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		self.assertGreaterEqual(planned["blocked"], 1)
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "blocked")
		self.assertTrue(self.frappe.db.exists("Translation", name))

	def test_locked_paging_prevents_phantom_or_unsafe_delete(self):
		"""A second MariaDB connection races before and after the first page boundary."""
		from frappe.database import get_db

		from frappe_lt.legacy_migration import PAGE_SIZE, apply, preflight

		prefix = "lt_migration_page_" + uuid.uuid4().hex[:10] + "_"
		names = [f"{prefix}{index:06d}" for index in range(PAGE_SIZE + 2)]
		self.names.extend(names)
		self.frappe.db.bulk_insert(
			"Translation",
			["name", "language", "source_text", "context", "translated_text"],
			[(name, "lt", "Item", None, "Prekė") for name in names],
			chunk_size=PAGE_SIZE,
		)
		self.frappe.db.commit()
		self.frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 3")
		conf = self.frappe.local.conf
		for action, target in (
			("insert", f"{prefix}000000a"),
			("insert", f"{prefix}000500a"),
			("edit", names[0]),
			("edit", names[PAGE_SIZE]),
		):
			with self.subTest(action=action, target=target):
				planned = preflight(self.site, self.package)
				self.assertEqual(planned["exit_code"], 0)
				attempted = threading.Event()
				finished = threading.Event()
				outcome = []

				def concurrent_write(
					action=action, target=target, attempted=attempted, finished=finished, outcome=outcome
				):
					second = None
					try:
						self.frappe.init(site=self.site, sites_path=".")
						second = get_db(
							host=conf.db_host,
							port=conf.db_port,
							user=conf.db_name,
							password=conf.db_password,
							cur_db_name=conf.db_name,
						)
						second.sql("SET SESSION innodb_lock_wait_timeout = 3")
						attempted.set()
						if action == "insert":
							second.sql(
								"INSERT INTO `tabTranslation` (name, language, source_text, context, translated_text) VALUES (%s, 'lt', 'Item', NULL, 'Prekė')",
								target,
							)
						else:
							second.sql(
								"UPDATE `tabTranslation` SET translated_text = 'Pakeista' WHERE name = %s",
								target,
							)
						updated = second._cursor.rowcount
						second.commit()
						outcome.append("after_delete" if action == "edit" and not updated else "committed")
					except Exception as error:
						if second is not None:
							second.rollback()
						outcome.append(error)
					finally:
						if second is not None:
							second.close()
						finished.set()

				original = self.frappe.db.sql
				launched = []
				observed = []

				def after_first_page(
					query,
					*args,
					original=original,
					launched=launched,
					attempted=attempted,
					observed=observed,
					finished=finished,
					**kwargs,
				):
					result = original(query, *args, **kwargs)
					if "FROM `tabTranslation`" in query and "FOR UPDATE" in query and not launched:
						thread = threading.Thread(target=concurrent_write, daemon=True)
						launched.append(thread)
						thread.start()
						if not attempted.wait(2):
							raise AssertionError("second connection did not start")
						time.sleep(0.15)
						observed.append(finished.is_set())
					return result

				try:
					with patch.object(self.frappe.db, "sql", side_effect=after_first_page):
						result = apply(self.site, self.package, planned["run_id"])
					self.assertTrue(launched, "locked scan did not reach first page")
					self.assertTrue(finished.wait(4), "concurrent write hung past lock timeout")
					launched[0].join(timeout=1)
					self.assertIn(outcome, (["committed"], ["after_delete"]))
					self.assertIn(result["state"], {"stale", "committed"})
					final = (
						Path(self.frappe.get_site_path("private"))
						/ "frappe_lt_legacy_migration"
						/ (planned["run_id"] + ".final.json")
					)
					final_state = json.loads(final.read_bytes())["state"]
					if observed[0]:
						self.assertEqual(
							final_state, "rolled_back", "phantom committed without rechecking plan"
						)
					if final_state == "rolled_back":
						self.assertTrue(self.frappe.db.exists("Translation", names[0]))
					else:
						self.assertEqual(final_state, "committed")
						self.assertFalse(self.frappe.db.exists("Translation", names[0]))
					if action == "insert":
						self.assertTrue(self.frappe.db.exists("Translation", target))
					elif outcome == ["committed"]:
						self.assertEqual(
							self.frappe.db.get_value("Translation", target, "translated_text"), "Pakeista"
						)
				finally:
					if launched:
						launched[0].join(timeout=4)
					if action == "insert":
						self.names.append(target)
						self.frappe.db.sql("DELETE FROM `tabTranslation` WHERE name = %s", target)
					else:
						self.frappe.db.sql(
							"UPDATE `tabTranslation` SET translated_text = 'Prekė' WHERE name = %s", target
						)
					missing = [name for name in names if not self.frappe.db.exists("Translation", name)]
					if missing:
						self.frappe.db.bulk_insert(
							"Translation",
							["name", "language", "source_text", "context", "translated_text"],
							[(name, "lt", "Item", None, "Prekė") for name in missing],
							chunk_size=PAGE_SIZE,
						)
					self.frappe.db.commit()

	def test_conflicting_duplicate_blocks_but_identical_duplicate_is_removed(self):
		from frappe_lt.legacy_migration import apply, preflight

		first = self.insert("Item", "Prekė")
		second = self.insert("Item", "Prekė", context="")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		self.assertGreaterEqual(planned["duplicates"], 1)
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "committed")
		self.assertFalse(self.frappe.db.exists("Translation", first))
		self.assertFalse(self.frappe.db.exists("Translation", second))
		first = self.insert("Item", "Prekė")
		changed = self.insert("Item", "Prekė ranka")
		self.frappe.db.commit()
		blocked = preflight(self.site, self.package)
		self.assertGreaterEqual(blocked["blocked"], 1)
		self.assertEqual(apply(self.site, self.package, blocked["run_id"])["state"], "blocked")
		self.assertTrue(self.frappe.db.exists("Translation", first))
		self.assertTrue(self.frappe.db.exists("Translation", changed))

	def test_cache_failure_after_commit_recovers_same_run(self):
		from frappe_lt.legacy_migration import apply, preflight

		name = self.insert("Item", "Prekė")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		with patch("frappe_lt.legacy_migration._clear_cache", side_effect=RuntimeError("cache down")):
			failed = apply(self.site, self.package, planned["run_id"])
		self.assertEqual(failed["state"], "cache_failure")
		self.assertFalse(self.frappe.db.exists("Translation", name))
		other_plan = preflight(self.site, self.package)
		self.assertEqual(apply(self.site, self.package, other_plan["run_id"])["state"], "pending_recovery")
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "committed")
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "no_op")

	def test_report_failure_after_commit_recovers_without_rollback_label(self):
		from frappe_lt.legacy_migration import apply, preflight

		name = self.insert("Item", "Prekė")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		import os as os_module

		replace = os_module.replace

		def fail_final(src, dst):
			if str(dst).endswith(".final.json"):
				raise OSError("report destination unavailable")
			return replace(src, dst)

		with patch("frappe_lt.legacy_migration.os.replace", side_effect=fail_final):
			failed = apply(self.site, self.package, planned["run_id"])
		self.assertEqual(failed["state"], "report_failure")
		self.assertFalse(self.frappe.db.exists("Translation", name))
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "committed")

	def test_recovery_reports_postcommit_db_drift_and_requires_fresh_plan(self):
		from frappe_lt.legacy_migration import apply, preflight

		self.insert("Item", "Prekė")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		with patch("frappe_lt.legacy_migration._clear_cache", side_effect=RuntimeError("cache down")):
			self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "cache_failure")
		new = self.insert("Item", "Prekė")
		self.frappe.db.commit()
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "stale")
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "stale")
		fresh = preflight(self.site, self.package)
		self.assertEqual(apply(self.site, self.package, fresh["run_id"])["state"], "committed")
		self.assertFalse(self.frappe.db.exists("Translation", new))

	def test_done_rerun_rechecks_new_exact_override_and_conflict(self):
		from frappe_lt.legacy_migration import apply, preflight

		self.insert("Item", "Prekė")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "committed")
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "no_op")
		for value in ("Prekė", "Pakeista", "Item"):
			name = self.insert("Item", value)
			self.frappe.db.commit()
			self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "stale")
			self.frappe.db.sql("DELETE FROM `tabTranslation` WHERE name = %s", name)
			self.frappe.db.commit()

	def test_done_and_final_must_agree_with_committed_marker(self):
		from frappe_lt.legacy_migration import _publish, apply, preflight

		self.insert("Item", "Prekė")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		run_id = planned["run_id"]
		self.assertEqual(apply(self.site, self.package, run_id)["state"], "committed")
		root = Path(self.frappe.get_site_path("private")) / "frappe_lt_legacy_migration"
		done = root / (run_id + ".done")
		original = json.loads(done.read_bytes())
		try:
			_publish(done, {**original, "postcommit_drift": not original["postcommit_drift"]}, replace=True)
			self.assertEqual(apply(self.site, self.package, run_id)["state"], "report_failure")
		finally:
			_publish(done, original, replace=True)
		self.assertEqual(apply(self.site, self.package, run_id)["state"], "no_op")

	def test_recovery_with_missing_package_and_revoked_policy(self):
		from frappe_lt.legacy_migration import apply, preflight

		self.insert("Item", "Prekė")
		self.frappe.db.commit()
		with tempfile.TemporaryDirectory() as directory:
			policy = Path(directory) / "policy.json"
			policy.write_text('{"schema_version":1,"entries":[]}')
			planned = preflight(self.site, self.package, policy)
			with patch("frappe_lt.legacy_migration._clear_cache", side_effect=RuntimeError("cache down")):
				self.assertEqual(
					apply(self.site, self.package, planned["run_id"], policy)["state"], "cache_failure"
				)
			policy.unlink()
			self.assertEqual(
				apply(self.site, directory + "/missing.csv", planned["run_id"], policy)["state"], "stale"
			)
			private = Path(self.frappe.get_site_path("private")) / "frappe_lt_legacy_migration"
			self.assertTrue((private / (planned["run_id"] + ".done")).exists())
			self.assertEqual(
				apply(self.site, directory + "/missing.csv", planned["run_id"])["state"], "stale"
			)
			fresh = preflight(self.site, self.package)
			self.assertEqual(
				apply(self.site, directory + "/missing.csv", fresh["run_id"])["state"], "blocked"
			)

	def test_recovery_completes_after_site_policy_change_but_requires_fresh_plan(self):
		from frappe_lt.legacy_migration import apply, preflight

		self.insert("Item", "Prekė")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		with patch("frappe_lt.legacy_migration._clear_cache", side_effect=RuntimeError("cache down")):
			self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "cache_failure")
		with tempfile.TemporaryDirectory() as directory:
			policy_path = Path(directory) / "site.json"
			policy_path.write_text('{"schema_version":1,"entries":[]}')
			self.assertEqual(apply(self.site, self.package, planned["run_id"], policy_path)["state"], "stale")
			fresh = preflight(self.site, self.package, policy_path)
			self.assertEqual(apply(self.site, self.package, fresh["run_id"], policy_path)["state"], "no_op")

	def test_failed_delete_rolls_back_every_match(self):
		from frappe_lt.legacy_migration import apply, preflight

		first = self.insert("Item", "Prekė")
		second = self.insert("Item", "Prekė", context="")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		sql = self.frappe.db.sql

		def fail_delete(query, *args, **kwargs):
			if query.startswith("DELETE FROM `tabTranslation`"):
				sql(query, *args, **kwargs)
				raise RuntimeError("database delete failed")
			return sql(query, *args, **kwargs)

		with patch.object(self.frappe.db, "sql", side_effect=fail_delete):
			self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "db_failure")
		self.assertTrue(self.frappe.db.exists("Translation", first))
		self.assertTrue(self.frappe.db.exists("Translation", second))

	def test_original_package_and_private_plan_are_required(self):
		from frappe_lt.legacy_migration import preflight

		name = self.insert("Item", "Prekė")
		self.frappe.db.commit()
		with tempfile.TemporaryDirectory() as directory:
			altered = Path(directory) / "altered.csv"
			altered.write_bytes(Path(self.package).read_bytes() + b"\n")
			self.assertEqual(preflight(self.site, altered)["exit_code"], 1)
		with patch("frappe_lt.legacy_migration.os.link", side_effect=OSError("no private report")):
			self.assertEqual(preflight(self.site, self.package)["exit_code"], 3)
		with patch("frappe_lt.legacy_migration.MAX_REPORT_BYTES", 1):
			self.assertEqual(preflight(self.site, self.package)["exit_code"], 3)
		self.assertTrue(self.frappe.db.exists("Translation", name))

	def test_scan_limit_blocks_apply_without_partial_delete(self):
		from frappe_lt.legacy_migration import apply, preflight

		name = self.insert("Item", "Prekė")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		with patch("frappe_lt.legacy_migration.MAX_ROWS", 0):
			self.assertEqual(apply(self.site, self.package, planned["run_id"])["exit_code"], 1)
		self.assertTrue(self.frappe.db.exists("Translation", name))

	def test_site_exception_waives_only_active_english_gate(self):
		from frappe_lt.legacy_migration import preflight

		inventory = json.loads((Path(__file__).parent.parent / "release_inventory.json").read_bytes())
		digest = next(
			entry["source_digest"]
			for entry in inventory["entries"]
			if entry["key"] == {"source": "Item", "context": None}
		)
		name = self.insert("Item", "Item")
		self.frappe.db.commit()
		with tempfile.TemporaryDirectory() as directory:
			policy_path = Path(directory) / "site.json"
			policy = {
				"schema_version": 1,
				"entries": [
					{
						"key": {"source": "Item", "context": None},
						"source_digest": digest,
						"approver": "Site reviewer",
						"reason": "Explicit English preference",
						"revoked": False,
					}
				],
			}
			self.assertGreaterEqual(preflight(self.site, self.package)["blocked"], 1)
			policy_path.write_text(json.dumps(policy))
			self.assertEqual(preflight(self.site, self.package, policy_path)["blocked"], 0)
			self.frappe.db.sql(
				"UPDATE `tabTranslation` SET translated_text = %s WHERE name = %s", ("Item {0}", name)
			)
			self.frappe.db.commit()
			self.assertGreaterEqual(preflight(self.site, self.package, policy_path)["blocked"], 1)
			self.frappe.db.sql(
				"UPDATE `tabTranslation` SET translated_text = %s WHERE name = %s", ("Item", name)
			)
			self.frappe.db.commit()
			policy["entries"][0]["revoked"] = True
			policy_path.write_text(json.dumps(policy))
			self.assertGreaterEqual(preflight(self.site, self.package, policy_path)["blocked"], 1)
			policy["entries"][0]["source_digest"] = "0" * 64
			policy_path.write_text(json.dumps(policy))
			self.assertEqual(preflight(self.site, self.package, policy_path)["exit_code"], 1)
			policy_path.write_text(
				'{"schema_version":1,"entries":[{"key":{"source":"Item","context":null},'
				f'"source_digest":"{digest}","approver":"Reviewer","reason":"Review",'
				'"revoked":true,"revoked":false}]}'
			)
			self.assertEqual(preflight(self.site, self.package, policy_path)["exit_code"], 1)

	def test_cache_is_cleared_only_after_committed_delete(self):
		from frappe.translate import MERGED_TRANSLATION_KEY, USER_TRANSLATION_KEY

		from frappe_lt.legacy_migration import apply, preflight

		self.insert("Item", "Prekė")
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		for key in (MERGED_TRANSLATION_KEY, USER_TRANSLATION_KEY, "bootinfo"):
			self.frappe.cache.hset(key, "lt", {"sentinel": True})
		self.assertEqual(self.frappe.cache.hget(USER_TRANSLATION_KEY, "lt"), {"sentinel": True})
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["state"], "committed")
		for key in (MERGED_TRANSLATION_KEY, USER_TRANSLATION_KEY, "bootinfo"):
			self.assertIsNone(self.frappe.cache.hget(key, "lt"))

	def test_identical_duplicates_span_multiple_conditional_delete_batches(self):
		from frappe_lt.legacy_migration import PAGE_SIZE, apply, preflight

		names = ["lt_migration_" + uuid.uuid4().hex for _ in range(PAGE_SIZE + 1)]
		self.names.extend(names)
		self.frappe.db.bulk_insert(
			"Translation",
			["name", "language", "source_text", "context", "translated_text"],
			[(name, "lt", "Item", None, "Prekė") for name in names],
			chunk_size=PAGE_SIZE,
		)
		self.frappe.db.commit()
		planned = preflight(self.site, self.package)
		self.assertEqual(planned["delete"], len(names))
		self.assertEqual(apply(self.site, self.package, planned["run_id"])["deleted"], len(names))
		self.assertFalse(self.frappe.db.exists("Translation", names[0]))
		self.assertFalse(self.frappe.db.exists("Translation", names[-1]))
