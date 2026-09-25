"""Fail-closed, site-scoped install orchestration for the authenticated release."""

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from frappe_lt.inventory import verify_owned_artifacts

APP = "frappe_lt"
STATE_FILE = "install.json"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class InstallError(ValueError):
	"""An operator-safe failure (never contains Translation values)."""


def _fail(code):
	raise InstallError(code) from None


def _site(frappe):
	site = getattr(frappe.local, "site", None)
	if not isinstance(site, str) or not site or "/" in site or ".." in site:
		_fail("INVALID_SITE")
	return site


def _release():
	"""Authenticate the whole catalog without compiling or modifying active assets."""
	from frappe_lt.inventory import load_compatibility
	from frappe_lt.release_catalog import verify_release

	manifest = verify_owned_artifacts()
	result = verify_release()
	if not isinstance(result, dict) or not all(
		isinstance(result.get(key), str) and SHA256.fullmatch(result[key])
		for key in ("inventory_digest", "mo_sha256", "release_digest")
	):
		_fail("RELEASE_INVALID")
	# compatibility.mo_sha256 describes the old single-message baseline, not the
	# independently pinned full release. Only the inventory digest is shared.
	if result["inventory_digest"] != manifest["inventory_digest"]:
		_fail("RELEASE_MISMATCH")
	return result, load_compatibility()


def _target_keys(frappe, manifest):
	"""Same real source + site metadata extraction before and during install."""
	from frappe_lt.runtime_extraction import extract_runtime
	from frappe_lt.source_extraction import extract_sources

	apps = frappe.get_installed_apps()
	if apps not in (["frappe", "erpnext"], ["frappe", "erpnext", APP]):
		_fail("UNSUPPORTED_APP_ORDER")

	class ExtractionSite:
		def __getattr__(self, name):
			return getattr(frappe, name)

		def get_installed_apps(self):
			return ["frappe", "erpnext"]

		def get_all(self, doctype, **kwargs):
			# Translation rows are evaluated by #13, never part of the upstream
			# source/standard-metadata inventory. The clean fixture's single
			# presence guard would otherwise reject every legacy site.
			if doctype == "Translation" and kwargs == {"filters": None, "fields": ["name"], "limit": 1}:
				return []
			return frappe.get_all(doctype, **kwargs)

	# The extractor requires a pristine two-app site. The install app contributes
	# no upstream metadata and is hidden only from its installed-app guard.
	runtime = extract_runtime(
		ExtractionSite(), manifest["runtime_metadata_sha256"], allow_deployment_site=True
	)
	events = [*runtime.events, *extract_sources(frappe)]
	keys = set()
	for event in events:
		key = (frappe.as_unicode(event.source).strip(), event.context)
		if key[0]:
			keys.add(key)
	return keys


def _inventory_keys():
	from frappe_lt.catalog_quality import _validate_inventory
	from frappe_lt.inventory import COMPATIBILITY_PATH

	path = COMPATIBILITY_PATH.with_name("release_inventory.json")
	return set(_validate_inventory(json.loads(path.read_bytes())))


def _upstream_environment(frappe, manifest):
	"""Check the bench independently of pinned commits; key equality gates drift."""
	import erpnext

	from frappe_lt.inventory import _git_value, validate_tool_versions

	if frappe.get_installed_apps() not in (["frappe", "erpnext"], ["frappe", "erpnext", APP]):
		_fail("UNSUPPORTED_APP_ORDER")
	versions = {"frappe": frappe.__version__, "erpnext": erpnext.__version__}
	if any(not re.fullmatch(r"16(?:\..*)?", version) for version in versions.values()):
		_fail("UNSUPPORTED_MAJOR")
	validate_tool_versions(manifest)
	benches = set()
	changed = False
	for app in ("frappe", "erpnext"):
		pin = manifest["upstream"][app]
		path = Path(
			_git_value(Path(frappe.get_app_source_path(app)).resolve(), "rev-parse", "--show-toplevel")
		).resolve()
		benches.add(path.parent.parent)
		if _git_value(path, "status", "--porcelain"):
			_fail("UPSTREAM_WORKTREE_DIRTY")
		changed |= _git_value(path, "rev-parse", "HEAD") != pin["commit"] or versions[app] != pin["version"]
	if len(benches) != 1 or Path.cwd().resolve() != next(iter(benches)) / "sites":
		_fail("BENCH_MISMATCH")
	return versions, changed


def preflight(package, exceptions=None):
	"""Bench-level read-only gate. Does not create a plan or write any files."""
	return _preflight(package, exceptions, classify_site=True)


def _preflight(package, exceptions, *, classify_site):
	import frappe

	from frappe_lt import legacy_migration
	from frappe_lt.catalog_quality import _load_trusted, _validate_inventory
	from frappe_lt.inventory import COMPATIBILITY_PATH

	site = _site(frappe)
	if "erpnext" not in frappe.get_installed_apps():
		_fail("ERPNEXT_NOT_INSTALLED")
	release, manifest = _release()
	try:
		versions, changed = _upstream_environment(frappe, manifest)
	except InstallError:
		raise
	except Exception:
		_fail("UPSTREAM_VERIFICATION_FAILED")
	try:
		actual = _target_keys(frappe, manifest)
		if actual != _inventory_keys():
			_fail("TARGET_INVENTORY_MISMATCH")
	except InstallError:
		raise
	except Exception:
		_fail("TARGET_EXTRACTION_FAILED")
	warning = []
	if changed:
		warning.append("UNKNOWN_V16_PATCH_EXACT_INVENTORY_MATCH")
	classification = None
	if classify_site:
		try:
			fingerprints = legacy_migration.authenticate_package(Path(package), frappe.utils.sanitize_html)
			allowed, _policy_digest = legacy_migration.trusted_site_policy(exceptions)
			inventory, _, _, _ = _load_trusted(None, COMPATIBILITY_PATH)
			active = _validate_inventory(inventory)
			classification = legacy_migration.classify(
				legacy_migration._snapshot(), fingerprints, active, allowed
			)
		except Exception:
			_fail("MIGRATION_PREFLIGHT_FAILED")
		if classification["blocked"]:
			_fail("SITE_OVERRIDES_BLOCKED")
	return {
		"site": site,
		"inventory_digest": release["inventory_digest"],
		"release_digest": release["release_digest"],
		"mo_sha256": release["mo_sha256"],
		"versions": versions,
		"warnings": warning,
		"migration": (
			{key: len(classification[key]) for key in ("delete", "overrides", "extras")}
			if classification is not None
			else None
		),
		"state": "ready",
	}


def _root(frappe, *, create=False):
	root = Path(frappe.get_site_path("private")) / "frappe_lt_install"
	if root.is_symlink():
		_fail("UNSAFE_INSTALL_STORAGE")
	if create:
		root.mkdir(mode=0o700, exist_ok=True)
	if not root.is_dir() or stat.S_IMODE(root.stat().st_mode) != 0o700:
		_fail("UNSAFE_INSTALL_STORAGE")
	return root


def _saved(frappe):
	from frappe_lt.legacy_migration import _read_private

	try:
		data = _read_private(_root(frappe) / STATE_FILE)
	except (OSError, ValueError):
		_fail("PREPARED_INPUTS_MISSING")
	if not _valid_saved(data, _site(frappe)):
		_fail("PREPARED_INPUTS_INVALID")
	return data


def _valid_saved(data, site):
	"""Accept legacy state off the release site and bound state everywhere."""
	base_fields = {
		"schema_version",
		"site",
		"release_digest",
		"inventory_digest",
		"mo_sha256",
		"versions",
		"package",
		"package_sha256",
		"exceptions",
		"policy_sha256",
		"run_id",
	}
	if not isinstance(data, dict) or data.get("schema_version") not in {1, 2}:
		return False
	fields = base_fields | ({"release_candidate"} if data["schema_version"] == 2 else set())
	if set(data) != fields or (site == "development.localhost" and data["schema_version"] != 2):
		return False
	if data.get("schema_version", 1) == 2:
		binding = data["release_candidate"]
		if (
			not isinstance(binding, dict)
			or set(binding) != {"candidate", "capture_sha256", "schema_version"}
			or binding["schema_version"] != 1
			or not isinstance(binding["candidate"], dict)
			or set(binding["candidate"]) != {"clean", "commit"}
			or binding["candidate"].get("clean") is not True
			or not isinstance(binding["candidate"].get("commit"), str)
			or re.fullmatch(r"[0-9a-f]{40}", binding["candidate"]["commit"]) is None
			or not isinstance(binding["capture_sha256"], str)
			or SHA256.fullmatch(binding["capture_sha256"]) is None
		):
			return False
	return not (
		data["site"] != site
		or not all(
			isinstance(data.get(key), str) and SHA256.fullmatch(data[key])
			for key in ("release_digest", "inventory_digest", "mo_sha256", "package_sha256", "policy_sha256")
		)
		or not isinstance(data["versions"], dict)
		or not isinstance(data["run_id"], str)
		or not re.fullmatch("[0-9a-f]{32}", data["run_id"])
		or not isinstance(data["package"], str)
		or not (data["exceptions"] is None or isinstance(data["exceptions"], str))
	)


@contextmanager
def _lock(frappe):
	"""Persistent site inode; never unlink it while other processes may wait."""
	path = Path(frappe.get_site_path("locks", "frappe_lt_install.lock"))
	fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
	try:
		if (
			not stat.S_ISREG(os.fstat(fd).st_mode)
			or stat.S_IMODE(os.fstat(fd).st_mode) != 0o600
			or os.fstat(fd).st_ino != path.stat().st_ino
		):
			_fail("UNSAFE_INSTALL_LOCK")
		try:
			fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			_fail("INSTALL_ALREADY_RUNNING")
		try:
			yield
		finally:
			fcntl.flock(fd, fcntl.LOCK_UN)
	finally:
		os.close(fd)


def _digest(path):
	path = Path(path)
	if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
		_fail("INPUT_CHANGED")
	return hashlib.sha256(path.read_bytes()).hexdigest()


def _release_candidate_binding(frappe):
	"""Authenticate the fixed pre-install capture without following its path components."""
	from frappe_lt.inventory import canonical_json, clean_candidate_identity
	from frappe_lt.release_candidate import _candidate_root, _read_input, validate_capture

	try:
		candidate = clean_candidate_identity(Path(__file__).parent.parent)
		root = Path(
			frappe.get_site_path("private", "frappe_lt_release_candidate", candidate["commit"])
		).absolute()
		_candidate_root(frappe, root, candidate["commit"])
		descriptor = os.open(
			root,
			os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
		)
		try:
			value, content = _read_input(descriptor, "candidate.json")
		finally:
			os.close(descriptor)
		capture = validate_capture(value)
		if capture["candidate"] != candidate or content != canonical_json(capture):
			raise ValueError("candidate capture does not match the clean commit")
	except (OSError, ValueError):
		_fail("RELEASE_CANDIDATE_INVALID")
	return {
		"candidate": candidate,
		"capture_sha256": hashlib.sha256(content).hexdigest(),
		"schema_version": 1,
	}


def _maintenance(frappe):
	"""Persist maintenance before invoking install-app; never clear it automatically."""
	path = Path(frappe.get_site_path("site_config.json"))
	if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o022:
		_fail("SITE_CONFIG_UNSAFE")
	config = json.loads(path.read_bytes())
	if not isinstance(config, dict):
		_fail("SITE_CONFIG_UNSAFE")
	if config.get("maintenance_mode") == 1:
		frappe.conf.maintenance_mode = 1
		return
	config["maintenance_mode"] = 1
	# Do not require legacy_migration's private-report mode on Frappe's ordinary
	# site_config.json (often 0644). Replace it atomically with a private file.
	content = (json.dumps(config, ensure_ascii=False, sort_keys=True) + "\n").encode()
	fd, name = tempfile.mkstemp(prefix=".site_config.", dir=path.parent)
	temporary = Path(name)
	try:
		with os.fdopen(fd, "wb") as handle:
			handle.write(content)
			handle.flush()
			os.fsync(handle.fileno())
		if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o022:
			_fail("SITE_CONFIG_UNSAFE")
		os.replace(temporary, path)
		dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
		try:
			os.fsync(dir_fd)
		finally:
			os.close(dir_fd)
	finally:
		temporary.unlink(missing_ok=True)
	frappe.conf.maintenance_mode = 1


def _assert_maintenance(frappe):
	if not _in_maintenance(frappe):
		_fail("MAINTENANCE_REQUIRED")


def _in_maintenance(frappe):
	path = Path(frappe.get_site_path("site_config.json"))
	if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o022:
		_fail("SITE_CONFIG_UNSAFE")
	config = json.loads(path.read_bytes())
	if not isinstance(config, dict):
		_fail("SITE_CONFIG_UNSAFE")
	return config.get("maintenance_mode") == 1


def prepare(package, exceptions=None):
	"""Authenticate first, then publish #13 plan, save identifiers and enter maintenance."""
	import frappe

	from frappe_lt import legacy_migration

	with _lock(frappe):
		# Refuse unsafe/unwritable config before publishing a #13 plan; an orphan
		# plan would leave install-app with no durable maintenance protection.
		_in_maintenance(frappe)
		# A committed migration must never be displaced by a fresh plan. Use
		# status/resume on an existing attempt, even if its report is incomplete.
		if (Path(frappe.get_site_path("private")) / "frappe_lt_install" / STATE_FILE).exists():
			previous = _checked(frappe)
			if previous["package"] != str(Path(package).resolve()) or previous["exceptions"] != (
				str(Path(exceptions).resolve()) if exceptions else None
			):
				_fail("EXISTING_PLAN_USE_RESUME")
			return {"site": previous["site"], "state": "prepared", "run_id": previous["run_id"]}
		binding = _release_candidate_binding(frappe) if _site(frappe) == "development.localhost" else None
		ready = preflight(package, exceptions)
		# #13 authenticates CSV and policy before publishing its private report.
		planned = legacy_migration.preflight(
			_site(frappe), package, exceptions, allow_exact_inventory_patch=True
		)
		if planned["exit_code"] or planned["state"] != "planned":
			_fail("LEGACY_PREFLIGHT_BLOCKED")
		data = {
			"schema_version": 2 if binding is not None else 1,
			"site": ready["site"],
			"release_digest": ready["release_digest"],
			"inventory_digest": ready["inventory_digest"],
			"mo_sha256": ready["mo_sha256"],
			"versions": ready["versions"],
			"package": str(Path(package).resolve()),
			"package_sha256": _digest(package),
			"exceptions": str(Path(exceptions).resolve()) if exceptions else None,
			"policy_sha256": _digest(exceptions) if exceptions else hashlib.sha256(b"").hexdigest(),
			"run_id": planned["run_id"],
		}
		if binding is not None:
			data["release_candidate"] = binding
		from frappe_lt.legacy_migration import _publish

		_maintenance(frappe)
		_publish(_root(frappe, create=True) / STATE_FILE, data, replace=True)
		return {**ready, "state": "prepared", "run_id": planned["run_id"]}


def _checked(frappe):
	data = _saved(frappe)
	_assert_maintenance(frappe)
	if data.get("schema_version", 1) == 2:
		if _release_candidate_binding(frappe) != data["release_candidate"]:
			_fail("RELEASE_CANDIDATE_CHANGED")
	elif _site(frappe) == "development.localhost":
		_fail("RELEASE_CANDIDATE_BINDING_REQUIRED")
	if (
		_digest(data["package"]) != data["package_sha256"]
		or (_digest(data["exceptions"]) if data["exceptions"] else hashlib.sha256(b"").hexdigest())
		!= data["policy_sha256"]
	):
		_fail("INPUT_CHANGED")
	# #13 owns reclassification on an uncommitted attempt and the durable SQL
	# marker on recovery. Reclassifying after its commit sees the deleted rows
	# and can incorrectly block an otherwise recoverable install.
	ready = _preflight(data["package"], data["exceptions"], classify_site=False)
	if any(
		ready[key] != data[key] for key in ("release_digest", "inventory_digest", "mo_sha256", "versions")
	):
		_fail("PLAN_STALE")
	return data


def before_install():
	import frappe

	from frappe_lt import profile

	with _lock(frappe):
		try:
			_checked(frappe)
			profile.before_install()
		except InstallError:
			raise
		except Exception:
			_fail("INSTALL_PREFLIGHT_FAILED")


def _mo_path():
	from frappe.gettext.translate import get_mo_path

	return Path(get_mo_path(APP, "lt"))


def _verify_mo(expected):
	from frappe_lt.release_catalog import verify_mo

	if verify_mo(_mo_path()) != expected:
		_fail("MO_MISMATCH")


def _all_sites(frappe):
	"""Count every site using this bench's shared MO, including site symlinks."""
	sites_dir = Path(frappe.get_site_path("site_config.json")).parent.parent
	return sorted(
		path.name for path in sites_dir.iterdir() if path.is_dir() and (path / "site_config.json").exists()
	)


def _shared_ready(frappe, expected):
	"""An absent MO can be published only for one release under bench-wide maintenance."""
	from frappe_lt.legacy_migration import _read_private

	sites_dir = Path(frappe.get_site_path("site_config.json")).parent.parent
	try:
		current = _saved(frappe)
	except InstallError:
		_fail("SHARED_MO_INCOMPATIBLE")
	for name in _all_sites(frappe):
		if name == _site(frappe):
			if not _in_maintenance(frappe) or current["mo_sha256"] != expected:
				_fail("SHARED_MO_INCOMPATIBLE")
			continue
		root = sites_dir / name
		config_path = root / "site_config.json"
		private = root / "private" / "frappe_lt_install"
		try:
			if config_path.is_symlink() or config_path.stat().st_mode & 0o022:
				_fail("SHARED_MO_INCOMPATIBLE")
			if private.is_symlink() or stat.S_IMODE(private.stat().st_mode) != 0o700:
				_fail("SHARED_MO_INCOMPATIBLE")
			config = json.loads(config_path.read_bytes())
			plan = _read_private(private / STATE_FILE)
		except (OSError, ValueError):
			_fail("SHARED_MO_INCOMPATIBLE")
		if (
			not isinstance(config, dict)
			or config.get("maintenance_mode") != 1
			or not _valid_saved(plan, name)
			or any(
				plan.get(key) != current[key] for key in ("release_digest", "inventory_digest", "mo_sha256")
			)
			or plan.get("mo_sha256") != expected
		):
			_fail("SHARED_MO_INCOMPATIBLE")


@contextmanager
def _mo_lock(frappe):
	"""Serialize the shared asset publication across different site install locks."""
	sites_dir = Path(frappe.get_site_path("site_config.json")).parent.parent
	path = sites_dir / ".frappe_lt_mo.lock"
	fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
	try:
		if (
			not stat.S_ISREG(os.fstat(fd).st_mode)
			or stat.S_IMODE(os.fstat(fd).st_mode) != 0o600
			or os.fstat(fd).st_ino != path.stat().st_ino
		):
			_fail("SHARED_MO_INCOMPATIBLE")
		fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		try:
			yield
		finally:
			fcntl.flock(fd, fcntl.LOCK_UN)
	except BlockingIOError:
		_fail("SHARED_MO_BUSY")
	finally:
		os.close(fd)


def _ensure_mo(frappe, expected):
	with _mo_lock(frappe):
		return _ensure_mo_locked(frappe, expected)


def _ensure_mo_locked(frappe, expected):
	path = _mo_path()
	if path.is_symlink():
		_fail("SHARED_MO_INCOMPATIBLE")
	if path.is_file() and _digest(path) == expected:
		_verify_mo(expected)
		return
	# A shared MO may already be used by another site: never replace it without
	# being able to prove every other site's compatibility.
	if path.exists():
		_fail("SHARED_MO_INCOMPATIBLE")
	_shared_ready(frappe, expected)
	from frappe_lt.po import compile_po

	with tempfile.TemporaryDirectory(prefix="frappe-lt-install-") as directory:
		workspace = Path(directory)
		candidate = workspace / "frappe_lt.mo"
		compile_po(Path(frappe.get_app_path(APP, "locale", "lt.po")), workspace, mo_path=candidate)
		if _digest(candidate) != expected:
			_fail("MO_MISMATCH")
		# A fresh --skip-assets bench has no locale directory yet. Build the
		# destination only after isolated compilation succeeds, without following
		# symlinked parents or changing an existing shared MO.
		if any(parent.is_symlink() for parent in (path.parent, *path.parent.parents)):
			_fail("SHARED_MO_INCOMPATIBLE")
		path.parent.mkdir(parents=True, exist_ok=True)
		_shared_ready(frappe, expected)
		if path.exists() or path.is_symlink() or not path.parent.is_dir() or path.parent.is_symlink():
			_fail("SHARED_MO_INCOMPATIBLE")
		# The isolated build may be on another filesystem. Stage on the target
		# filesystem and publish without replacing a concurrently-created MO.
		fd, name = tempfile.mkstemp(prefix=".frappe_lt.mo.", dir=path.parent)
		staged = Path(name)
		try:
			with os.fdopen(fd, "wb") as handle:
				handle.write(candidate.read_bytes())
				handle.flush()
				os.fsync(handle.fileno())
			os.chmod(staged, 0o644)
			os.link(staged, path, follow_symlinks=False)
			dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
			try:
				os.fsync(dir_fd)
			finally:
				os.close(dir_fd)
		finally:
			staged.unlink(missing_ok=True)
	_verify_mo(expected)


def _migration_state(frappe, data):
	"""Require #13's durable evidence, including for its SQL-free no-op path."""
	from frappe_lt import legacy_migration

	site, run_id = _site(frappe), data["run_id"]
	root = legacy_migration._private_root(site)
	plan = legacy_migration._trusted_plan(root, site, run_id)
	if (
		plan["inventory_digest"] != data["inventory_digest"]
		or plan["policy_sha256"] != data["policy_sha256"]
		or plan["classification"]["blocked"]
	):
		return "pending"
	marker = legacy_migration._marker()
	if marker == [run_id]:
		completed = legacy_migration._read_private(root / (run_id + ".done"))
		final = legacy_migration._read_private(root / (run_id + ".final.json"))
		if (
			isinstance(completed, dict)
			and isinstance(final, dict)
			and completed.get("run_id") == run_id
			and completed.get("state") == "committed"
			and completed.get("postcommit_drift") is False
			and final.get("run_id") == run_id
			and final.get("site") == site
			and final.get("state") == "committed"
			and final.get("package_sha256") == plan["package_sha256"]
			and final.get("deleted") == len(plan["classification"]["delete"])
			and final.get("postcommit_drift") is False
			and legacy_migration._snapshot() == legacy_migration._expected_rows(plan)
		):
			return "committed"
	elif not marker and not plan["classification"]["delete"]:
		final = legacy_migration._read_private(root / (run_id + ".final.json"))
		if (
			final
			== {
				"deleted": 0,
				"package_sha256": plan["package_sha256"],
				"postcommit_drift": False,
				"run_id": run_id,
				"site": site,
				"state": "committed",
			}
			and legacy_migration._snapshot() == plan["rows"]
		):
			return "no_op"
	return "pending"


def _finish():
	import frappe

	from frappe_lt import legacy_migration, profile

	data = _checked(frappe)
	# Frappe's install hook may still own uncommitted installed-app rows. #13 is
	# the sole owner of its SQL transaction and starts with rollback; make the
	# already-performed install durable before handing control to it.
	frappe.db.commit()
	# apply() consults its committed SQL marker before any attempted retry;
	# report and cache failures remain blocking even after SQL commit.
	migration = legacy_migration.apply(
		_site(frappe),
		data["package"],
		data["run_id"],
		data["exceptions"],
		allow_exact_inventory_patch=True,
	)
	if migration["exit_code"] or migration["state"] not in ("committed", "no_op"):
		_fail("MIGRATION_INCOMPLETE")
	try:
		migration_state = _migration_state(frappe, data)
	except (OSError, ValueError, KeyError, TypeError):
		migration_state = "pending"
	if migration_state == "pending":
		_fail("MIGRATION_INCOMPLETE")
	_ensure_mo(frappe, data["mo_sha256"])
	if profile.status()["state_after"] != "APPLIED":
		profile.after_install()
	profile_state = profile.status()
	if profile_state["state_after"] != "APPLIED" or profile_state.get("skipped_manual_change"):
		_fail("PROFILE_INCOMPLETE")
	# Authenticate the entire shipped PO and expected MO again after all phases.
	_release()
	_verify_mo(data["mo_sha256"])
	from frappe.translate import clear_cache, get_translations_from_apps

	from frappe_lt.po import parse_po

	clear_cache()  # translation and boot caches, after the #13 postcommit clear
	frappe.clear_cache()  # clear this site's local document/boot caches as well
	catalog = parse_po(Path(frappe.get_app_path(APP, "locale", "lt.po")))
	app_translations = get_translations_from_apps("lt", apps=["frappe", "erpnext", APP])
	if set(catalog.messages) != _inventory_keys() or any(
		app_translations.get(f"{source}:{context}" if context else source) != value
		for (source, context), value in catalog.messages.items()
	):
		_fail("CATALOG_PRECEDENCE_FAILED")
	if app_translations.get("Item") != "Prekė":
		_fail("ITEM_SMOKE_FAILED")
	if frappe._("Item", lang="lt") != "Prekė":
		_fail("ITEM_SMOKE_FAILED")
	return {"site": data["site"], "state": "verified", "run_id": data["run_id"], "maintenance_mode": 1}


def after_install():
	import frappe

	with _lock(frappe):
		try:
			return _finish()
		except InstallError:
			raise
		except Exception:
			_fail("INSTALL_PHASE_FAILED")


def resume():
	import frappe

	if APP not in frappe.get_installed_apps():
		_fail("INSTALL_APP_REQUIRED")
	with _lock(frappe):
		try:
			return _finish()
		except InstallError:
			raise
		except Exception:
			_fail("INSTALL_PHASE_FAILED")


def status():
	"""Safe status from real migration, MO and profile state, without private values."""
	import frappe

	from frappe_lt import profile

	with _lock(frappe):
		if not (Path(frappe.get_site_path("private")) / "frappe_lt_install").exists():
			return {
				"site": _site(frappe),
				"installed": APP in frappe.get_installed_apps(),
				"maintenance_mode": _in_maintenance(frappe),
				"state": "unprepared",
			}
		data = _saved(frappe)
		run_id = data["run_id"]
		try:
			migration = _migration_state(frappe, data)
		except (OSError, ValueError, KeyError, TypeError):
			migration = "pending"
		mo = _mo_path()
		try:
			mo_state = "matched" if _digest(mo) == data["mo_sha256"] else "pending"
		except (OSError, InstallError):
			mo_state = "pending"
		try:
			profile_state = profile.status()["state_after"]
		except (OSError, ValueError, KeyError):
			profile_state = "pending"
		return {
			"site": _site(frappe),
			"installed": APP in frappe.get_installed_apps(),
			"maintenance_mode": _in_maintenance(frappe),
			"run_id": run_id,
			"migration": migration,
			"profile": profile_state,
			"mo": mo_state,
		}
