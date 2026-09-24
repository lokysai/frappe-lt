"""Pinned-Bench install fault exercise; run on a fresh ERPNext site in maintenance.

Unlike the isolated unit tests, this drives Frappe's real install_app and the
durable #13 SQL marker/report/cache recovery against a real site database.
"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


def run(site, package):
	import frappe
	from frappe import installer

	from frappe_lt import install, legacy_migration, profile

	frappe.init(site=site)
	frappe.connect()
	try:
		assert frappe.get_installed_apps() == ["frappe", "erpnext"]
		# A real Legacy Exact Match guarantees a non-no-op SQL commit path.
		fingerprints = legacy_migration.authenticate_package(Path(package), frappe.utils.sanitize_html)
		value = fingerprints[("Item", "")]
		frappe.get_doc(
			{
				"doctype": "Translation",
				"language": "lt",
				"source_text": "Item",
				"translated_text": value,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		plan = install.prepare(package)
		run_id = plan["run_id"]
		root = legacy_migration._private_root(site)
		mo = install._mo_path()
		mo_before = hashlib.sha256(mo.read_bytes()).hexdigest()
		assert mo_before == plan["mo_sha256"]

		def pending(*, installed, marker, migration=None):
			state = install.status()
			assert state["installed"] is installed, state
			assert state["maintenance_mode"] is True, state
			assert legacy_migration._marker() == marker
			if migration is not None:
				assert state["migration"] == migration, state
			assert hashlib.sha256(mo.read_bytes()).hexdigest() == mo_before

		def fails(call, code):
			try:
				call()
			except install.InstallError as error:
				assert str(error) == code, (code, str(error))
			else:
				raise AssertionError(f"expected {code}")

		# In-app preflight fails before Frappe records the app or #13 changes SQL.
		with patch.object(install, "_target_keys", side_effect=OSError("private value")):
			fails(lambda: installer.install_app("frappe_lt"), "TARGET_EXTRACTION_FAILED")
		pending(installed=False, marker=[], migration="pending")

		# Frappe may register the app before the #13 SQL transaction fails. Resume
		# must use the original run ID and actual committed marker, not a new plan.
		real_snapshot = legacy_migration._snapshot

		def fail_locked(*args, **kwargs):
			if kwargs.get("locked"):
				raise OSError("private value")
			return real_snapshot(*args, **kwargs)

		with patch.object(legacy_migration, "_snapshot", side_effect=fail_locked):
			fails(lambda: installer.install_app("frappe_lt"), "MIGRATION_INCOMPLETE")
		pending(installed=True, marker=[], migration="pending")
		assert frappe.db.exists("Translation", {"language": "lt", "source_text": "Item"})

		real_publish = legacy_migration._publish

		def fail_report(path, *args, **kwargs):
			if str(path).endswith(".final.json"):
				raise OSError("private value")
			return real_publish(path, *args, **kwargs)

		with patch.object(legacy_migration, "_publish", side_effect=fail_report):
			fails(install.resume, "MIGRATION_INCOMPLETE")
		pending(installed=True, marker=[run_id], migration="pending")
		assert not frappe.db.exists("Translation", {"language": "lt", "source_text": "Item"})
		assert not (root / (run_id + ".done")).exists()

		with patch.object(legacy_migration, "_clear_cache", side_effect=OSError("private value")):
			fails(install.resume, "MIGRATION_INCOMPLETE")
		pending(installed=True, marker=[run_id], migration="pending")
		assert not (root / (run_id + ".done")).exists()

		with patch.object(install, "_ensure_mo", side_effect=OSError("private value")):
			fails(install.resume, "INSTALL_PHASE_FAILED")
		pending(installed=True, marker=[run_id], migration="committed")
		assert profile.status()["state_after"] != "APPLIED"

		with patch.object(profile, "after_install", side_effect=OSError("private value")):
			fails(install.resume, "INSTALL_PHASE_FAILED")
		pending(installed=True, marker=[run_id], migration="committed")
		assert profile.status()["state_after"] != "APPLIED"

		real_release = install._release
		checks = {"count": 0}

		def fail_final_release():
			checks["count"] += 1
			if checks["count"] == 2:
				raise OSError("private value")
			return real_release()

		with patch.object(install, "_release", side_effect=fail_final_release):
			fails(install.resume, "INSTALL_PHASE_FAILED")
		assert checks["count"] == 2
		pending(installed=True, marker=[run_id], migration="committed")
		assert profile.status()["state_after"] == "APPLIED"

		with patch("frappe.translate.clear_cache", side_effect=OSError("private value")):
			fails(install.resume, "INSTALL_PHASE_FAILED")
		pending(installed=True, marker=[run_id], migration="committed")
		assert install.resume() == {
			"site": site,
			"state": "verified",
			"run_id": run_id,
			"maintenance_mode": 1,
		}
		pending(installed=True, marker=[run_id], migration="committed")
		with install._lock(frappe):
			parallel = subprocess.run(
				["bench", "--site", site, "resume-lithuanian-install"],
				cwd=Path.cwd().parent,
				capture_output=True,
				text=True,
				check=False,
			)
		output = parallel.stdout + parallel.stderr
		assert parallel.returncode != 0 and "INSTALL_ALREADY_RUNNING" in output, (
			parallel.returncode,
			re.findall(r"INSTALL_[A-Z_]+|[A-Za-z]+Error|No such command|Connection refused", output),
		)
		pending(installed=True, marker=[run_id], migration="committed")
		print("Pinned install fault recovery verified:", site, run_id)
	finally:
		frappe.db.rollback()
		frappe.destroy()


if __name__ == "__main__":
	if len(sys.argv) != 3:
		raise SystemExit("usage: python -m frappe_lt.tests.pinned_install_faults SITE ORIGINAL_CSV")
	run(sys.argv[1], sys.argv[2])
