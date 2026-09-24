import hashlib
import json
import stat
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe_lt
from frappe_lt import install, legacy_migration, release_catalog


class InstallTests(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.root = Path(self.directory.name)
		(self.root / "locks").mkdir()
		(self.root / "private").mkdir()
		config = self.root / "site_config.json"
		config.write_text(json.dumps({"db_name": "private", "maintenance_mode": 0}))
		config.chmod(0o600)
		self.frappe = SimpleNamespace(
			local=SimpleNamespace(site="test.local"),
			conf=SimpleNamespace(maintenance_mode=0),
			__version__="16.1.0",
			get_installed_apps=lambda: ["frappe", "erpnext"],
			get_site_path=lambda *parts: str(self.root.joinpath(*parts)),
			as_unicode=str,
			utils=SimpleNamespace(sanitize_html=str),
			db=SimpleNamespace(commit=lambda: None),
		)
		self.erpnext = SimpleNamespace(__version__="16.1.0")
		self.release = {"inventory_digest": "a" * 64, "release_digest": "b" * 64, "mo_sha256": "c" * 64}
		self.manifest = {
			"upstream": {
				"frappe": {"version": "16.1.0", "commit": "a" * 40},
				"erpnext": {"version": "16.1.0", "commit": "b" * 40},
			}
		}
		self.modules = patch.dict("sys.modules", {"frappe": self.frappe, "erpnext": self.erpnext})
		self.modules.start()
		self.addCleanup(self.modules.stop)

	def test_read_only_preflight_and_unknown_patch_exact_keys_only(self):
		from frappe_lt import catalog_quality

		with (
			patch.object(install, "_release", return_value=(self.release, self.manifest)),
			patch.object(
				install,
				"_upstream_environment",
				return_value=({"frappe": "16.1.0", "erpnext": "16.2.3"}, True),
			),
			patch.object(install, "_target_keys", return_value={("Save", None), ("Save", "Button")}),
			patch.object(install, "_inventory_keys", return_value={("Save", None), ("Save", "Button")}),
			patch.object(legacy_migration, "authenticate_package", return_value={}),
			patch.object(legacy_migration, "trusted_site_policy", return_value=(set(), "digest")),
			patch.object(legacy_migration, "_snapshot", return_value=[]),
			patch.object(catalog_quality, "_load_trusted", return_value=({}, None, None, None)),
			patch.object(catalog_quality, "_validate_inventory", return_value={}),
			patch.object(
				legacy_migration,
				"classify",
				return_value={"delete": [], "overrides": [], "extras": [], "blocked": []},
			),
		):
			self.erpnext.__version__ = "16.2.3"
			result = install.preflight("original.csv")
			self.assertEqual(result["warnings"], ["UNKNOWN_V16_PATCH_EXACT_INVENTORY_MATCH"])
			self.assertEqual(
				sorted(path.name for path in self.root.iterdir()), ["locks", "private", "site_config.json"]
			)
			with patch.object(install, "_target_keys", return_value={("Save", None)}):
				with self.assertRaisesRegex(install.InstallError, "TARGET_INVENTORY_MISMATCH"):
					install.preflight("original.csv")
			with patch.object(
				legacy_migration,
				"classify",
				return_value={"blocked": [{"name": "private value", "code": "ENGLISH_FALLBACK"}]},
			):
				with self.assertRaisesRegex(install.InstallError, "SITE_OVERRIDES_BLOCKED") as blocked:
					install.preflight("original.csv")
				self.assertNotIn("private value", str(blocked.exception))

	def test_missing_erpnext_or_unsupported_major_blocks_before_extraction(self):
		self.frappe.get_installed_apps = lambda: ["frappe"]
		with self.assertRaisesRegex(install.InstallError, "ERPNEXT_NOT_INSTALLED"):
			install.preflight("original.csv")
		self.frappe.get_installed_apps = lambda: ["frappe", "erpnext"]
		self.frappe.__version__ = "17.0.0"
		with patch.object(install, "_release", return_value=(self.release, self.manifest)):
			with self.assertRaisesRegex(install.InstallError, "UNSUPPORTED_MAJOR"):
				install.preflight("original.csv")

	def test_release_uses_pinned_catalog_not_legacy_mo_digest(self):
		with (
			patch.object(
				install,
				"verify_owned_artifacts",
				return_value={"inventory_digest": "a" * 64, "mo_sha256": "d" * 64},
			),
			patch("frappe_lt.inventory.load_compatibility", return_value=self.manifest),
			patch.object(release_catalog, "verify_release", return_value=self.release) as verify,
		):
			self.assertEqual(install._release(), (self.release, self.manifest))
			verify.assert_called_once_with()

	def test_real_release_gate_is_read_only(self):
		po = Path(release_catalog.__file__).with_name("locale") / "lt.po"
		before = hashlib.sha256(po.read_bytes()).hexdigest()
		with patch("frappe_lt.po.compile_po", side_effect=AssertionError("read-only verification")):
			result, compatibility = install._release()
		self.assertEqual(result["inventory_digest"], compatibility["inventory_digest"])
		self.assertNotEqual(result["mo_sha256"], compatibility["mo_sha256"])
		self.assertEqual(hashlib.sha256(po.read_bytes()).hexdigest(), before)

	def test_legacy_commit_does_not_reclassify_deleted_rows_on_resume(self):
		package = self.root / "package.csv"
		package.write_text("original")
		ready = {**self.release, "site": "test.local", "versions": {"frappe": "16.1.0", "erpnext": "16.1.0"}}
		with (
			patch.object(
				install,
				"_saved",
				return_value={
					**ready,
					"package": str(package),
					"package_sha256": install._digest(package),
					"exceptions": None,
					"policy_sha256": hashlib.sha256(b"").hexdigest(),
					"run_id": "f" * 32,
				},
			),
			patch.object(install, "_release", return_value=(self.release, self.manifest)),
			patch.object(install, "_upstream_environment", return_value=(ready["versions"], False)),
			patch.object(install, "_target_keys", return_value={}),
			patch.object(install, "_inventory_keys", return_value={}),
			patch.object(
				legacy_migration,
				"classify",
				side_effect=AssertionError("deleted rows must not be classified"),
			),
		):
			install._maintenance(self.frappe)
			self.assertEqual(install._checked(self.frappe)["run_id"], "f" * 32)

	def test_in_app_check_reextracts_and_blocks_before_migration_on_failure(self):
		package = self.root / "package.csv"
		package.write_text("original")
		data = {
			**self.release,
			"site": "test.local",
			"versions": {"frappe": "16.1.0", "erpnext": "16.2.3"},
			"package": str(package),
			"package_sha256": install._digest(package),
			"exceptions": None,
			"policy_sha256": hashlib.sha256(b"").hexdigest(),
			"run_id": "f" * 32,
		}
		install._maintenance(self.frappe)
		with (
			patch.object(install, "_saved", return_value=data),
			patch.object(install, "_release", return_value=(self.release, self.manifest)),
			patch.object(install, "_upstream_environment", return_value=(data["versions"], True)),
			patch.object(install, "_inventory_keys", return_value={("Save", "Button")}),
			patch.object(legacy_migration, "apply", side_effect=AssertionError("must not apply")),
		):
			with patch.object(install, "_target_keys", side_effect=RuntimeError("extraction failed")):
				with self.assertRaisesRegex(install.InstallError, "TARGET_EXTRACTION_FAILED"):
					install._checked(self.frappe)
			with patch.object(install, "_target_keys", return_value={("Save", None)}):
				with self.assertRaisesRegex(install.InstallError, "TARGET_INVENTORY_MISMATCH"):
					install._checked(self.frappe)
			with patch.object(install, "_target_keys", return_value={("Save", "Button")}):
				self.assertEqual(install._checked(self.frappe), data)

	def test_no_op_requires_trusted_plan_and_final_report(self):
		data = {
			**self.release,
			"site": "test.local",
			"package": "original.csv",
			"exceptions": None,
			"run_id": "f" * 32,
		}
		with (
			patch.object(frappe_lt, "profile", SimpleNamespace(), create=True),
			patch.object(install, "_checked", return_value=data),
			patch.object(legacy_migration, "apply", return_value={"exit_code": 0, "state": "no_op"}),
			patch.object(install, "_ensure_mo", side_effect=AssertionError("unverified no-op")),
			patch.object(legacy_migration, "_private_root", return_value=self.root / "private"),
			patch.object(legacy_migration, "_marker", return_value=[]),
			patch.object(legacy_migration, "_trusted_plan", return_value={"classification": {"delete": []}}),
		):
			with self.assertRaisesRegex(install.InstallError, "MIGRATION_INCOMPLETE"):
				install._finish()

	def test_no_op_requires_exact_final_and_unchanged_rows(self):
		data = {**self.release, "site": "test.local", "policy_sha256": "d" * 64, "run_id": "f" * 32}
		root = self.root / "private"
		final = root / (data["run_id"] + ".final.json")
		plan = {
			"inventory_digest": data["inventory_digest"],
			"policy_sha256": data["policy_sha256"],
			"package_sha256": "e" * 64,
			"classification": {"delete": [], "blocked": []},
			"rows": [{"name": "r"}],
		}
		with (
			patch.object(legacy_migration, "_private_root", return_value=root),
			patch.object(legacy_migration, "_trusted_plan", return_value=plan),
			patch.object(legacy_migration, "_marker", return_value=[]),
			patch.object(legacy_migration, "_snapshot", return_value=plan["rows"]) as snapshot,
		):
			final.write_text(
				json.dumps(
					{
						"deleted": 0,
						"package_sha256": "e" * 64,
						"postcommit_drift": False,
						"run_id": data["run_id"],
						"site": "test.local",
						"state": "committed",
					}
				)
			)
			final.chmod(0o600)
			self.assertEqual(install._migration_state(self.frappe, data), "no_op")
			final.write_text(
				json.dumps(
					{
						"deleted": 0,
						"package_sha256": "e" * 64,
						"postcommit_drift": False,
						"run_id": data["run_id"],
						"site": "other.local",
						"state": "committed",
					}
				)
			)
			self.assertEqual(install._migration_state(self.frappe, data), "pending")
			self.assertTrue(snapshot.called)

	def test_committed_marker_requires_matching_reports_and_rows(self):
		run_id = "f" * 32
		data = {**self.release, "site": "test.local", "policy_sha256": "d" * 64, "run_id": run_id}
		root = self.root / "private"
		plan = {
			"inventory_digest": data["inventory_digest"],
			"policy_sha256": data["policy_sha256"],
			"package_sha256": "e" * 64,
			"classification": {"delete": ["old"], "blocked": []},
			"rows": [{"name": "old"}],
		}
		(root / (run_id + ".done")).write_text(
			json.dumps({"run_id": run_id, "state": "committed", "postcommit_drift": False})
		)
		final = root / (run_id + ".final.json")
		report = {
			"run_id": run_id,
			"site": "test.local",
			"state": "committed",
			"package_sha256": "e" * 64,
			"deleted": 1,
			"postcommit_drift": False,
		}
		final.write_text(json.dumps(report))
		for path in (final, root / (run_id + ".done")):
			path.chmod(0o600)
		with (
			patch.object(legacy_migration, "_private_root", return_value=root),
			patch.object(legacy_migration, "_trusted_plan", return_value=plan),
			patch.object(legacy_migration, "_marker", return_value=[run_id]),
			patch.object(legacy_migration, "_snapshot", return_value=[]),
		):
			self.assertEqual(install._migration_state(self.frappe, data), "committed")
			final.write_text(json.dumps({**report, "package_sha256": "a" * 64}))
			self.assertEqual(install._migration_state(self.frappe, data), "pending")

	def test_typical_site_config_permissions_and_preflight_does_not_touch_assets(self):
		config = self.root / "site_config.json"
		config.chmod(0o644)
		mo = self.root / "shared.mo"
		mo.write_bytes(b"shared")
		with (
			patch.object(install, "_release", return_value=(self.release, self.manifest)),
			patch.object(
				install,
				"_upstream_environment",
				return_value=({"frappe": "16.1.0", "erpnext": "16.1.0"}, False),
			),
			patch.object(install, "_target_keys", return_value=set()),
			patch.object(install, "_inventory_keys", return_value=set()),
			patch.object(legacy_migration, "authenticate_package", return_value={}),
			patch.object(legacy_migration, "trusted_site_policy", return_value=(set(), "digest")),
			patch.object(legacy_migration, "_snapshot", return_value=[]),
			patch("frappe_lt.catalog_quality._load_trusted", return_value=({}, None, None, None)),
			patch("frappe_lt.catalog_quality._validate_inventory", return_value={}),
			patch.object(
				legacy_migration,
				"classify",
				return_value={"delete": [], "overrides": [], "extras": [], "blocked": []},
			),
		):
			install.preflight("original.csv")
		self.assertEqual(mo.read_bytes(), b"shared")
		install._maintenance(self.frappe)
		self.assertEqual(json.loads(config.read_bytes())["maintenance_mode"], 1)

	def test_active_mo_is_never_compiled_in_place(self):
		mo = self.root / "frappe_lt.mo"
		self.frappe.get_app_path = lambda *args: str(self.root / "lt.po")
		with (
			patch.object(install, "_mo_lock", return_value=nullcontext()),
			patch.object(install, "_all_sites", return_value=["test.local"]),
			patch.object(install, "_shared_ready"),
			patch.object(install, "_mo_path", return_value=mo),
			patch("frappe_lt.po.compile_po") as compile_po,
			patch.object(install, "_verify_mo", side_effect=AssertionError("wrong compiled digest")),
		):
			compile_po.side_effect = (
				lambda po, workspace, **kwargs: kwargs["mo_path"].write_bytes(b"wrong") or kwargs["mo_path"]
			)
			with self.assertRaises(install.InstallError):
				install._ensure_mo(self.frappe, self.release["mo_sha256"])
			self.assertFalse(mo.exists())

	def test_single_site_mo_is_compiled_isolated_then_published(self):
		mo = self.root / "assets" / "locale" / "lt" / "LC_MESSAGES" / "frappe_lt.mo"
		po = self.root / "lt.po"
		po.write_text("source catalog")
		self.frappe.get_app_path = lambda *args: str(po)
		expected = hashlib.sha256(b"compiled catalog").hexdigest()

		def compile_isolated(source, workspace, *, mo_path):
			self.assertEqual(source, po)
			self.assertNotEqual(mo_path, mo)
			self.assertFalse(mo.exists())
			mo_path.write_bytes(b"compiled catalog")

		with (
			patch.object(install, "_mo_lock", return_value=nullcontext()),
			patch.object(install, "_all_sites", return_value=["test.local"]),
			patch.object(install, "_shared_ready"),
			patch.object(install, "_mo_path", return_value=mo),
			patch("frappe_lt.po.compile_po", side_effect=compile_isolated),
			patch.object(install, "_verify_mo") as verified,
		):
			install._ensure_mo(self.frappe, expected)
		self.assertEqual(mo.read_bytes(), b"compiled catalog")
		verified.assert_called_once_with(expected)

	def test_new_site_during_isolated_compile_blocks_publication(self):
		mo = self.root / "frappe_lt.mo"
		self.frappe.get_app_path = lambda *args: str(self.root / "lt.po")
		expected = hashlib.sha256(b"compiled catalog").hexdigest()
		with (
			patch.object(install, "_mo_lock", return_value=nullcontext()),
			patch.object(
				install, "_shared_ready", side_effect=[None, install.InstallError("SHARED_MO_INCOMPATIBLE")]
			),
			patch.object(install, "_mo_path", return_value=mo),
			patch(
				"frappe_lt.po.compile_po",
				side_effect=lambda source, workspace, *, mo_path: mo_path.write_bytes(b"compiled catalog"),
			),
		):
			with self.assertRaisesRegex(install.InstallError, "SHARED_MO_INCOMPATIBLE"):
				install._ensure_mo(self.frappe, expected)
		self.assertFalse(mo.exists())

	def test_two_prepared_sites_can_publish_only_matching_shared_mo(self):
		sites = self.root / "bench" / "sites"
		current = sites / "test.local"
		other = sites / "other.local"
		for site in (current, other):
			(site / "private" / "frappe_lt_install").mkdir(parents=True)
			(site / "private" / "frappe_lt_install").chmod(0o700)
			(site / "site_config.json").write_text('{"maintenance_mode": 1}')
			(site / "site_config.json").chmod(0o600)
		self.frappe.get_site_path = lambda *parts: str(current.joinpath(*parts))
		data = {
			"schema_version": 1,
			"site": "test.local",
			"release_digest": self.release["release_digest"],
			"inventory_digest": self.release["inventory_digest"],
			"mo_sha256": hashlib.sha256(b"compiled catalog").hexdigest(),
			"versions": {"frappe": "16.1.0", "erpnext": "16.1.0"},
			"package": "/private/lt.csv",
			"package_sha256": "a" * 64,
			"exceptions": None,
			"policy_sha256": "b" * 64,
			"run_id": "c" * 32,
		}
		for site, name in ((current, "test.local"), (other, "other.local")):
			path = site / "private" / "frappe_lt_install" / install.STATE_FILE
			path.write_text(json.dumps({**data, "site": name}))
			path.chmod(0o600)
		mo = sites / "assets" / "locale" / "lt" / "LC_MESSAGES" / "frappe_lt.mo"
		self.frappe.get_app_path = lambda *args: str(self.root / "lt.po")
		with (
			patch.object(install, "_saved", return_value=data),
			patch.object(install, "_mo_path", return_value=mo),
			patch.object(install, "_verify_mo"),
			patch(
				"frappe_lt.po.compile_po",
				side_effect=lambda source, workspace, *, mo_path: mo_path.write_bytes(b"compiled catalog"),
			) as compile_po,
		):
			install._ensure_mo(self.frappe, data["mo_sha256"])
			self.assertEqual(mo.read_bytes(), b"compiled catalog")
			self.assertEqual(compile_po.call_count, 1)
			mo.unlink()
			(other / "site_config.json").write_text('{"maintenance_mode": 0}')
			with self.assertRaisesRegex(install.InstallError, "SHARED_MO_INCOMPATIBLE"):
				install._ensure_mo(self.frappe, data["mo_sha256"])
			self.assertFalse(mo.exists())
			(other / "site_config.json").write_text('{"maintenance_mode": 1}')
			path = other / "private" / "frappe_lt_install" / install.STATE_FILE
			path.write_text(json.dumps({**data, "site": "other.local", "mo_sha256": "d" * 64}))
			with self.assertRaisesRegex(install.InstallError, "SHARED_MO_INCOMPATIBLE"):
				install._ensure_mo(self.frappe, data["mo_sha256"])
			self.assertFalse(mo.exists())

	def test_bench_mo_lock_rejects_parallel_publication(self):
		site = self.root / "bench" / "sites" / "test.local"
		site.mkdir(parents=True)
		self.frappe.get_site_path = lambda *parts: str(site.joinpath(*parts))
		result = []

		def contend():
			try:
				with install._mo_lock(self.frappe):
					pass
			except install.InstallError as error:
				result.append(str(error))

		with install._mo_lock(self.frappe):
			worker = threading.Thread(target=contend)
			worker.start()
			worker.join()
		self.assertEqual(result, ["SHARED_MO_BUSY"])
		self.assertEqual(stat.S_IMODE((site.parent / ".frappe_lt_mo.lock").stat().st_mode), 0o600)

	def test_mo_verifier_receives_active_path_and_release_digest(self):
		mo = self.root / "frappe_lt.mo"
		with (
			patch.object(install, "_mo_path", return_value=mo),
			patch.object(release_catalog, "verify_mo", return_value=self.release["mo_sha256"]) as verify,
		):
			install._verify_mo(self.release["mo_sha256"])
			verify.assert_called_once_with(mo)
			with self.assertRaisesRegex(install.InstallError, "MO_MISMATCH"):
				install._verify_mo("d" * 64)

	def test_status_reports_only_safe_state_even_with_missing_evidence(self):
		root = self.root / "private" / "frappe_lt_install"
		root.mkdir(mode=0o700)
		data = {"run_id": "f" * 32, "mo_sha256": self.release["mo_sha256"]}
		with (
			patch.object(install, "_saved", return_value=data),
			patch.object(install, "_migration_state", side_effect=ValueError("private Translation content")),
			patch.object(install, "_mo_path", return_value=self.root / "missing.mo"),
			patch.object(
				frappe_lt, "profile", SimpleNamespace(status=lambda: {"state_after": "ABSENT"}), create=True
			),
		):
			result = install.status()
		self.assertEqual(result["migration"], "pending")
		self.assertEqual(result["mo"], "pending")
		self.assertNotIn("private Translation content", json.dumps(result))

	def test_extraction_uses_real_source_and_standard_metadata_on_installed_site(self):
		self.frappe.get_installed_apps = lambda: ["frappe", "erpnext", "frappe_lt"]
		self.frappe.get_all = lambda *args, **kwargs: ["legacy override"]

		def runtime(proxy, signatures, *, allow_deployment_site):
			self.assertTrue(allow_deployment_site)
			self.assertEqual(proxy.get_installed_apps(), ["frappe", "erpnext"])
			self.assertEqual(proxy.get_all("Translation", filters=None, fields=["name"], limit=1), [])
			self.assertEqual(proxy.get_all("DocType", fields=["name"]), ["legacy override"])
			return SimpleNamespace(events=[SimpleNamespace(source=" Save ", context="Button")])

		with (
			patch("frappe_lt.runtime_extraction.extract_runtime", side_effect=runtime),
			patch(
				"frappe_lt.source_extraction.extract_sources",
				return_value=[SimpleNamespace(source="Item", context=None)],
			),
		):
			self.assertEqual(
				install._target_keys(self.frappe, {"runtime_metadata_sha256": {}}),
				{("Save", "Button"), ("Item", None)},
			)

	def test_deployment_site_opt_in_preserves_clean_inventory_extraction_guard(self):
		from frappe_lt.runtime_extraction import extract_runtime

		deployment = SimpleNamespace(
			local=SimpleNamespace(site="development.localhost"),
			get_installed_apps=lambda: ["frappe"],
		)
		with self.assertRaisesRegex(ValueError, "must not use development.localhost"):
			extract_runtime(deployment, {})
		with self.assertRaisesRegex(ValueError, "exactly frappe and erpnext"):
			extract_runtime(deployment, {}, allow_deployment_site=True)

	def test_unknown_patch_checks_tools_worktrees_and_real_target_keys(self):
		from frappe_lt import inventory

		bench = self.root / "bench"
		(bench / "sites").mkdir(parents=True)
		self.frappe.get_app_source_path = lambda app: str(bench / "apps" / app)
		self.erpnext.__version__ = "16.2.3"

		def git(path, *args):
			if args == ("rev-parse", "--show-toplevel"):
				return str(bench / "apps" / path.name)
			if args == ("status", "--porcelain"):
				return ""
			return "c" * 40

		with (
			patch.object(inventory, "validate_tool_versions") as tools,
			patch.object(inventory, "_git_value", side_effect=git) as git_value,
			patch.object(install.Path, "cwd", return_value=bench / "sites"),
			patch.object(install, "_release", return_value=(self.release, self.manifest)),
			patch.object(install, "_inventory_keys", return_value={("Save", "Button")}),
			patch.object(install, "_target_keys", return_value={("Save", "Button")}) as extraction,
		):
			result = install._preflight("original.csv", None, classify_site=False)
			self.assertEqual(result["warnings"], ["UNKNOWN_V16_PATCH_EXACT_INVENTORY_MATCH"])
			tools.assert_called_once_with(self.manifest)
			extraction.assert_called_once_with(self.frappe, self.manifest)
			git_value.side_effect = lambda path, *args: (
				"dirty" if args == ("status", "--porcelain") else git(path, *args)
			)
			with self.assertRaisesRegex(install.InstallError, "UPSTREAM_WORKTREE_DIRTY"):
				install._preflight("original.csv", None, classify_site=False)
			git_value.side_effect = git
			with patch.object(install, "_target_keys", side_effect=RuntimeError("private extraction")):
				with self.assertRaisesRegex(install.InstallError, "TARGET_EXTRACTION_FAILED") as error:
					install._preflight("original.csv", None, classify_site=False)
				self.assertNotIn("private extraction", str(error.exception))

	def test_prepare_and_finish_pass_install_only_opt_in(self):
		package = self.root / "package.csv"
		package.write_text("original")
		ready = {**self.release, "site": "test.local", "versions": {"frappe": "16.1.0", "erpnext": "16.2.3"}}
		with (
			patch.object(install, "preflight", return_value=ready),
			patch.object(
				legacy_migration,
				"preflight",
				return_value={"exit_code": 0, "state": "planned", "run_id": "f" * 32},
			) as plan,
		):
			install.prepare(str(package))
			plan.assert_called_once_with("test.local", str(package), None, allow_exact_inventory_patch=True)
		with (
			patch.object(frappe_lt, "profile", SimpleNamespace(), create=True),
			patch.object(
				install,
				"_checked",
				return_value={
					"site": "test.local",
					"package": str(package),
					"exceptions": None,
					"run_id": "f" * 32,
				},
			),
			patch.object(
				legacy_migration, "apply", return_value={"exit_code": 1, "state": "blocked"}
			) as applied,
		):
			with self.assertRaisesRegex(install.InstallError, "MIGRATION_INCOMPLETE"):
				install._finish()
			applied.assert_called_once_with(
				"test.local", str(package), "f" * 32, None, allow_exact_inventory_patch=True
			)

	def test_schema_v1_state_remains_compatible_off_release_site_only(self):
		data = {
			"exceptions": None,
			"inventory_digest": "a" * 64,
			"mo_sha256": "b" * 64,
			"package": "/private/package.csv",
			"package_sha256": "c" * 64,
			"policy_sha256": "d" * 64,
			"release_digest": "e" * 64,
			"run_id": "f" * 32,
			"schema_version": 1,
			"site": "test.local",
			"versions": {},
		}
		self.assertTrue(install._valid_saved(data, "test.local"))
		self.assertFalse(
			install._valid_saved({**data, "site": "development.localhost"}, "development.localhost")
		)

	def test_development_prepare_persists_candidate_capture_binding_before_maintenance(self):
		self.frappe.local.site = "development.localhost"
		package = self.root / "package.csv"
		package.write_text("original")
		binding = {
			"candidate": {"clean": True, "commit": "a" * 40},
			"capture_sha256": "b" * 64,
			"schema_version": 1,
		}
		ready = {
			**self.release,
			"site": "development.localhost",
			"versions": {"frappe": "16.1.0", "erpnext": "16.1.0"},
		}
		events = []
		with (
			patch.object(
				install,
				"_release_candidate_binding",
				side_effect=lambda _frappe: events.append("binding") or binding,
			),
			patch.object(
				install, "preflight", side_effect=lambda *_args: events.append("preflight") or ready
			),
			patch.object(
				legacy_migration,
				"preflight",
				side_effect=lambda *_args, **_kwargs: events.append("legacy")
				or {"exit_code": 0, "state": "planned", "run_id": "f" * 32},
			),
			patch.object(
				install,
				"_maintenance",
				side_effect=lambda _frappe: events.append("maintenance"),
			),
		):
			install.prepare(package)
		saved = install._saved(self.frappe)
		self.assertEqual(saved["schema_version"], 2)
		self.assertEqual(saved["release_candidate"], binding)
		self.assertEqual(events, ["binding", "preflight", "legacy", "maintenance"])

	def test_bound_install_rechecks_candidate_capture_before_resume(self):
		self.frappe.local.site = "development.localhost"
		binding = {
			"candidate": {"clean": True, "commit": "a" * 40},
			"capture_sha256": "b" * 64,
			"schema_version": 1,
		}
		data = {"release_candidate": binding, "schema_version": 2}
		install._maintenance(self.frappe)
		with (
			patch.object(install, "_saved", return_value=data),
			patch.object(
				install,
				"_release_candidate_binding",
				return_value={**binding, "capture_sha256": "c" * 64},
			),
			patch.object(install, "_preflight", side_effect=AssertionError("must not continue")),
			self.assertRaisesRegex(install.InstallError, "RELEASE_CANDIDATE_CHANGED"),
		):
			install._checked(self.frappe)

	def test_prepare_does_not_save_or_enter_maintenance_when_legacy_blocks(self):
		with (
			patch.object(
				install,
				"preflight",
				return_value={**self.release, "site": "test.local", "versions": {}, "warnings": []},
			),
			patch.object(legacy_migration, "preflight", return_value={"exit_code": 1, "state": "planned"}),
		):
			with self.assertRaisesRegex(install.InstallError, "LEGACY_PREFLIGHT_BLOCKED"):
				install.prepare("missing.csv")
		self.assertFalse((self.root / "private" / "frappe_lt_install").exists())
		self.assertEqual(json.loads((self.root / "site_config.json").read_bytes())["maintenance_mode"], 0)

	def test_prepare_saves_only_identifiers_and_keeps_maintenance_on_retry(self):
		package = self.root / "package.csv"
		package.write_text("secret Translation content")
		ready = {
			**self.release,
			"site": "test.local",
			"versions": {"frappe": "16.1.0", "erpnext": "16.1.0"},
			"warnings": [],
		}
		with (
			patch.object(install, "preflight", return_value=ready),
			patch.object(install, "_preflight", return_value=ready),
			patch.object(
				legacy_migration,
				"preflight",
				return_value={"exit_code": 0, "state": "planned", "run_id": "f" * 32},
			),
		):
			self.assertEqual(install.prepare(str(package))["state"], "prepared")
			self.assertEqual(install.prepare(str(package))["state"], "prepared")
		saved = install._saved(self.frappe)
		self.assertNotIn("secret Translation", json.dumps(saved))
		self.assertEqual(
			stat.S_IMODE((self.root / "private" / "frappe_lt_install" / "install.json").stat().st_mode), 0o600
		)
		self.assertEqual(json.loads((self.root / "site_config.json").read_bytes())["maintenance_mode"], 1)

	def test_prepare_can_retry_after_private_state_write_failure_under_maintenance(self):
		package = self.root / "package.csv"
		package.write_text("original")
		ready = {
			**self.release,
			"site": "test.local",
			"versions": {"frappe": "16.1.0", "erpnext": "16.1.0"},
		}
		original_publish = legacy_migration._publish
		failed = False

		def interrupt_once(path, content, **kwargs):
			nonlocal failed
			if path.name == install.STATE_FILE and not failed:
				failed = True
				raise OSError("private report failure")
			return original_publish(path, content, **kwargs)

		with (
			patch.object(install, "preflight", return_value=ready),
			patch.object(legacy_migration, "_publish", side_effect=interrupt_once),
			patch.object(
				legacy_migration,
				"preflight",
				return_value={
					"exit_code": 0,
					"state": "planned",
					"run_id": "f" * 32,
				},
			),
		):
			with self.assertRaises(OSError):
				install.prepare(str(package))
			self.assertTrue(install._in_maintenance(self.frappe))
			self.assertFalse((self.root / "private" / "frappe_lt_install" / install.STATE_FILE).exists())
			self.assertEqual(install.prepare(str(package))["state"], "prepared")

	def test_failed_before_install_keeps_prepared_maintenance_when_app_was_not_registered(self):
		package = self.root / "package.csv"
		package.write_text("original")
		ready = {
			**self.release,
			"site": "test.local",
			"versions": {"frappe": "16.1.0", "erpnext": "16.1.0"},
		}
		with (
			patch.object(frappe_lt, "profile", SimpleNamespace(before_install=lambda: None), create=True),
			patch.object(install, "preflight", return_value=ready),
			patch.object(install, "_preflight", return_value=ready),
			patch.object(
				legacy_migration,
				"preflight",
				return_value={"exit_code": 0, "state": "planned", "run_id": "f" * 32},
			),
		):
			install.prepare(str(package))
			with patch.object(install, "_preflight", side_effect=install.InstallError("PLAN_STALE")):
				with self.assertRaisesRegex(install.InstallError, "PLAN_STALE"):
					install.before_install()
			self.assertEqual(install.prepare(str(package))["state"], "prepared")
		self.assertEqual(self.frappe.get_installed_apps(), ["frappe", "erpnext"])
		self.assertTrue(install._in_maintenance(self.frappe))

	def test_site_lock_rejects_parallel_attempt(self):
		result = []

		def contend():
			try:
				with install._lock(self.frappe):
					pass
			except install.InstallError as error:
				result.append(str(error))

		with install._lock(self.frappe):
			worker = threading.Thread(target=contend)
			worker.start()
			worker.join()
		self.assertEqual(result, ["INSTALL_ALREADY_RUNNING"])
		self.assertTrue((self.root / "locks" / "frappe_lt_install.lock").exists())

	def test_shared_mo_cannot_be_replaced_or_built_for_another_site(self):
		mo = self.root / "frappe_lt.mo"
		mo.write_bytes(b"another release")
		with (
			patch.object(install, "_mo_lock", return_value=nullcontext()),
			patch.object(install, "_all_sites", return_value=["test.local", "other.local"]),
			patch.object(install, "_mo_path", return_value=mo),
			patch.object(
				install, "_verify_mo", side_effect=AssertionError("must not verify incompatible MO")
			),
		):
			with self.assertRaisesRegex(install.InstallError, "SHARED_MO_INCOMPATIBLE"):
				install._ensure_mo(self.frappe, self.release["mo_sha256"])
			self.assertEqual(mo.read_bytes(), b"another release")
			mo.unlink()
			with self.assertRaisesRegex(install.InstallError, "SHARED_MO_INCOMPATIBLE"):
				install._ensure_mo(self.frappe, self.release["mo_sha256"])

	def test_bench_site_scan_counts_symlinked_sites(self):
		sites = self.root / "sites"
		sites.mkdir()
		current = sites / "test.local"
		current.mkdir()
		(current / "site_config.json").write_text("{}")
		self.frappe.get_site_path = lambda *parts: str(current.joinpath(*parts))
		other = sites / "other.local"
		with tempfile.TemporaryDirectory(dir=self.root) as directory:
			Path(directory, "site_config.json").write_text("{}")
			other.symlink_to(directory, target_is_directory=True)
			try:
				self.assertIn("other.local", install._all_sites(self.frappe))
			finally:
				other.unlink()

	def test_changed_inputs_block_before_migration_apply(self):
		package = self.root / "package.csv"
		package.write_text("original")
		with (
			patch.object(
				install,
				"preflight",
				return_value={**self.release, "site": "test.local", "versions": {}, "warnings": []},
			),
			patch.object(
				legacy_migration,
				"preflight",
				return_value={"exit_code": 0, "state": "planned", "run_id": "f" * 32},
			),
		):
			install.prepare(str(package))
		package.write_text("changed")
		with self.assertRaisesRegex(install.InstallError, "INPUT_CHANGED"):
			install._checked(self.frappe)

	def test_failed_migration_blocks_mo_profile_and_final_verify(self):
		profile = SimpleNamespace(status=lambda: self.fail("profile must not start"))
		data = {
			**self.release,
			"site": "test.local",
			"package": "/private.csv",
			"exceptions": None,
			"run_id": "f" * 32,
		}
		with (
			patch.object(frappe_lt, "profile", profile, create=True),
			patch.object(install, "_checked", return_value=data),
			patch.object(legacy_migration, "apply", return_value={"exit_code": 3, "state": "cache_failure"}),
			patch.object(install, "_ensure_mo", side_effect=AssertionError("MO must not start")),
		):
			with self.assertRaisesRegex(install.InstallError, "MIGRATION_INCOMPLETE"):
				install._finish()

	def test_committed_migration_is_resumed_before_profile_and_full_verify(self):
		events = []
		data = {
			**self.release,
			"site": "test.local",
			"package": "/private.csv",
			"exceptions": None,
			"run_id": "f" * 32,
		}
		profile = SimpleNamespace(
			status=lambda: {"state_after": "ABSENT" if "profile" not in events else "APPLIED"},
			after_install=lambda: events.append("profile"),
		)
		translations = SimpleNamespace(
			clear_cache=lambda: events.append("cache"),
			get_translations_from_apps=lambda *args, **kwargs: {"Item": "Prekė", "Save:Button": "Išsaugoti"},
		)
		self.frappe.get_app_path = lambda *args: str(self.root / "lt.po")
		self.frappe.clear_cache = lambda: events.append("site_cache")
		self.frappe._ = lambda *args, **kwargs: "Prekė"
		catalog = SimpleNamespace(messages={("Item", None): "Prekė", ("Save", "Button"): "Išsaugoti"})
		with (
			patch.dict(
				"sys.modules",
				{
					"frappe.translate": translations,
					"frappe_lt.po": SimpleNamespace(parse_po=lambda path: catalog),
				},
			),
			patch.object(frappe_lt, "profile", profile, create=True),
			patch.object(install, "_checked", return_value=data),
			patch.object(install, "_migration_state", return_value="no_op"),
			patch.object(
				legacy_migration,
				"apply",
				side_effect=lambda *args, **kwargs: events.append("migration")
				or {"exit_code": 0, "state": "no_op"},
			),
			patch.object(install, "_ensure_mo", side_effect=lambda *args: events.append("mo")),
			patch.object(install, "_release", side_effect=lambda: events.append("release")),
			patch.object(install, "_verify_mo", side_effect=lambda *args: events.append("verify_mo")),
			patch.object(install, "_inventory_keys", return_value=set(catalog.messages)),
		):
			self.assertEqual(install._finish()["state"], "verified")
		self.assertEqual(
			events, ["migration", "mo", "profile", "release", "verify_mo", "cache", "site_cache"]
		)

	def test_phase_failures_keep_maintenance_and_resume_in_order(self):
		data = {
			**self.release,
			"site": "test.local",
			"package": "/private.csv",
			"exceptions": None,
			"run_id": "f" * 32,
		}
		self.frappe.get_installed_apps = lambda: ["frappe", "erpnext", "frappe_lt"]
		self.frappe.get_app_path = lambda *args: str(self.root / "lt.po")
		self.frappe.clear_cache = lambda: None
		self.frappe._ = lambda *args, **kwargs: "Prekė"
		catalog = SimpleNamespace(messages={("Item", None): "Prekė"})
		profile_state = {"state_after": "ABSENT"}
		profile = SimpleNamespace(
			status=lambda: profile_state.copy(),
			after_install=lambda: profile_state.update(state_after="APPLIED"),
		)
		translations = SimpleNamespace(
			clear_cache=lambda: None,
			get_translations_from_apps=lambda *args, **kwargs: {"Item": "Prekė"},
		)
		install._maintenance(self.frappe)
		for stage in ("migration", "report", "cache", "mo", "profile", "verify", "final_cache"):
			with self.subTest(stage=stage):
				profile_state["state_after"] = "ABSENT"
				attempt = {"failed": False}
				migration_stage = stage if stage in ("migration", "report", "cache") else "migration"

				def fail_once(name, result=None, *, _stage=stage, _attempt=attempt):
					if name == _stage and not _attempt["failed"]:
						_attempt["failed"] = True
						if name in ("migration", "report", "cache"):
							return {"exit_code": 3, "state": name + "_failure"}
						raise OSError("private Translation content")
					return result

				with (
					patch.dict(
						"sys.modules",
						{
							"frappe.translate": SimpleNamespace(
								clear_cache=lambda: fail_once("final_cache"),
								get_translations_from_apps=translations.get_translations_from_apps,
							),
							"frappe_lt.po": SimpleNamespace(parse_po=lambda path: catalog),
						},
					),
					patch.object(frappe_lt, "profile", profile, create=True),
					patch.object(install, "_checked", return_value=data),
					patch.object(
						legacy_migration,
						"apply",
						side_effect=lambda *args, _migration_stage=migration_stage, **kwargs: fail_once(
							_migration_stage, {"exit_code": 0, "state": "committed"}
						),
					),
					patch.object(install, "_migration_state", return_value="committed"),
					patch.object(install, "_ensure_mo", side_effect=lambda *args: fail_once("mo")),
					patch.object(install, "_release", side_effect=lambda: fail_once("verify")),
					patch.object(install, "_verify_mo"),
					patch.object(install, "_inventory_keys", return_value=set(catalog.messages)),
				):
					if stage == "profile":
						profile.after_install = lambda: fail_once("profile") or profile_state.update(
							state_after="APPLIED"
						)
					with self.assertRaises(install.InstallError) as error:
						install.after_install()
					self.assertNotIn("private Translation content", str(error.exception))
					self.assertTrue(install._in_maintenance(self.frappe))
					if stage == "profile":
						profile.after_install = lambda: profile_state.update(state_after="APPLIED")
					self.assertEqual(install.resume()["state"], "verified")
					self.assertTrue(install._in_maintenance(self.frappe))


if __name__ == "__main__":
	unittest.main()
