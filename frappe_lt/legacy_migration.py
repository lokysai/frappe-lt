"""Guarded, one-way removal of original-package Translation records.

The original CSV is an operator-supplied input, never inferred from catalog provenance.
"""

import csv
import fcntl
import hashlib
import io
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from frappe_lt.inventory import canonical_json

PACKAGE_SHA256 = "490cf32b7d2e17406d012a993e7897659ef3fa7f71a4255721f947579e3aad0d"
PACKAGE_ROWS = 7968
PACKAGE_MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 50_000
MAX_REPORT_BYTES = 16 * 1024 * 1024
PAGE_SIZE = 500
HEADERS = ["Language", "Source Text", "Context", "Translated Text"]


class ScanLimit(ValueError):
	"""The site cannot be completely classified within safe bounds."""


def authenticate_package(path: Path, sanitize_html) -> dict[tuple[str, str], str]:
	"""Return exact stored-value fingerprints from the original import script's CSV."""
	path = Path(path)
	if path.is_symlink() or not path.is_file() or path.stat().st_size > PACKAGE_MAX_BYTES:
		raise ValueError("missing, unsafe or oversized original package")
	content = path.read_bytes()
	if hashlib.sha256(content).hexdigest() != PACKAGE_SHA256:
		raise ValueError("original package digest mismatch")
	reader = csv.DictReader(io.StringIO(content.decode("utf-8"), newline=""), strict=True)
	if reader.fieldnames != HEADERS:
		raise ValueError("original package headers mismatch")
	fingerprints = {}
	for row in reader:
		if set(row) != set(HEADERS) or any(not isinstance(v, str) for v in row.values()):
			raise ValueError("ambiguous original package entry")
		if row["Language"] != "lt" or not row["Source Text"] or not row["Translated Text"]:
			raise ValueError("invalid original package entry")
		key = row["Source Text"], row["Context"] or ""
		if key in fingerprints:
			raise ValueError("duplicate original package key")
		# The original importer called frappe.utils.sanitize_html once on raw CSV text.
		fingerprints[key] = sanitize_html(row["Translated Text"])
		if len(fingerprints) > PACKAGE_ROWS:
			raise ValueError("original package count mismatch")
	if len(fingerprints) != PACKAGE_ROWS:
		raise ValueError("original package count mismatch")
	return fingerprints


def classify(rows: list[dict], fingerprints: dict, inventory: dict, exceptions: set) -> dict:
	"""Classify one complete lt snapshot; use unchanged for preflight and locked apply."""
	from frappe_lt.catalog_quality import _html_errors, _tokens

	active = {(source, context or ""): entry for (source, context), entry in inventory.items()}
	flattened = {}
	for key in active:
		flattened.setdefault(_runtime_key(key), set()).add(key)
	result = {
		"delete": [],
		"overrides": [],
		"extras": [],
		"english_extras": [],
		"duplicates": [],
		"blocked": [],
	}
	groups = {}
	for row in rows:
		if row["language"] != "lt":
			result["blocked"].append({"name": row["name"], "code": "LANGUAGE_MISMATCH"})
			continue
		key = row["source_text"], row["context"] or ""
		groups.setdefault(key, []).append(row)
		if flattened.get(_runtime_key(key), set()) - {key}:
			result["blocked"].append({"name": row["name"], "code": "RUNTIME_KEY_COLLISION"})
		if key in fingerprints and fingerprints[key] == row["translated_text"]:
			result["delete"].append(row["name"])
		else:
			bucket = "overrides" if key in fingerprints else "extras"
			result[bucket].append(row["name"])
			if key not in active and row["translated_text"] == key[0]:
				result["english_extras"].append(row["name"])
			source_tokens, source_unknown = _tokens(key[0])
			value_tokens, value_unknown = _tokens(row["translated_text"] or "")
			if source_unknown or value_unknown:
				result["blocked"].append({"name": row["name"], "code": "UNKNOWN_TOKEN_SYNTAX"})
			elif source_tokens != value_tokens:
				result["blocked"].append({"name": row["name"], "code": "PRESERVED_TOKEN_MISMATCH"})
			for code in _html_errors(key[0], row["translated_text"] or ""):
				result["blocked"].append({"name": row["name"], "code": code})
			if key in active and (
				not row["translated_text"]
				or (
					row["translated_text"].strip() == key[0]
					and (key[0], key[1], active[key]["source_digest"]) not in exceptions
				)
			):
				result["blocked"].append({"name": row["name"], "code": "ENGLISH_FALLBACK"})
	for members in groups.values():
		if len(members) > 1:
			result["duplicates"].append([row["name"] for row in members])
			if len({row["translated_text"] for row in members}) > 1:
				result["blocked"].append(
					{"names": [row["name"] for row in members], "code": "CONFLICTING_DUPLICATE"}
				)
	for bucket in ("delete", "overrides", "extras"):
		result[bucket].sort()
	result["duplicates"].sort()
	result["english_extras"].sort()
	result["blocked"].sort(key=lambda value: (value["code"], value.get("name", ""), value.get("names", [])))
	return result


def _runtime_key(key):
	return f"{key[0]}:{key[1]}" if key[1] else key[0]


def _private_root(site, *, create=False):
	import frappe

	if frappe.local.site != site or not site or "/" in site or ".." in site:
		raise ValueError("site does not match active Frappe connection")
	root = Path(frappe.get_site_path("private")) / "frappe_lt_legacy_migration"
	if root.is_symlink():
		raise ValueError("private migration directory is a symlink")
	if create:
		root.mkdir(mode=0o700, exist_ok=True)
	if stat.S_IMODE(root.stat().st_mode) != 0o700:
		raise ValueError("private migration directory must have mode 0700")
	return root


def _read_private(path):
	if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
		raise ValueError("unsafe private migration report")
	if path.stat().st_size > MAX_REPORT_BYTES:
		raise ValueError("private migration report exceeds size limit")
	return json.loads(path.read_bytes())


def _check_lock(path):
	if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
		raise ValueError("unsafe migration lock")


@contextmanager
def _site_lock(root):
	"""Hold one persistent inode across attempts; unlinking a flock file splits waiters."""
	path = root / "site.lock"
	_check_lock(path)
	fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
	try:
		if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_ino != path.stat().st_ino:
			raise ValueError("migration lock changed")
		try:
			fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError as error:
			raise ValueError("migration already running") from error
		try:
			yield
		finally:
			fcntl.flock(fd, fcntl.LOCK_UN)
	finally:
		os.close(fd)


def _publish(path, data, *, replace=False):
	content = canonical_json(data)
	if len(content) > MAX_REPORT_BYTES:
		raise ValueError("private migration report exceeds size limit")
	temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
	fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
	try:
		with os.fdopen(fd, "wb") as handle:
			handle.write(content)
			handle.flush()
			os.fsync(handle.fileno())
		if replace:
			if path.exists():
				_read_private(path)
			os.replace(temporary, path)
		else:
			os.link(temporary, path, follow_symlinks=False)
			os.unlink(temporary)
		fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
		try:
			os.fsync(fd)
		finally:
			os.close(fd)
	finally:
		temporary.unlink(missing_ok=True)


def _unique_pairs(pairs):
	result = {}
	for key, value in pairs:
		if key in result:
			raise ValueError("duplicate site exception policy field")
		result[key] = value
	return result


def _policy_unchanged(path, digest):
	if path is None:
		return digest == hashlib.sha256(b"").hexdigest()
	try:
		path = Path(path)
		return (
			not path.is_symlink()
			and path.stat().st_size <= 1_000_000
			and hashlib.sha256(path.read_bytes()).hexdigest() == digest
		)
	except OSError:
		return False


def _site_policy(exception_path, active):
	allowed = set()
	policy_digest = hashlib.sha256(b"").hexdigest()
	if exception_path is None:
		return allowed, policy_digest
	path = Path(exception_path)
	if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
		raise ValueError("missing or unsafe site exception policy")
	content = path.read_bytes()
	policy_digest = hashlib.sha256(content).hexdigest()
	policy = json.loads(content, object_pairs_hook=_unique_pairs)
	if (
		not isinstance(policy, dict)
		or set(policy) != {"schema_version", "entries"}
		or policy["schema_version"] != 1
		or not isinstance(policy["entries"], list)
		or len(policy["entries"]) > 10_000
	):
		raise ValueError("invalid site exception policy")
	seen = set()
	for entry in policy["entries"]:
		if not isinstance(entry, dict) or set(entry) != {
			"key",
			"source_digest",
			"approver",
			"reason",
			"revoked",
		}:
			raise ValueError("invalid site exception entry")
		key = entry["key"]
		if (
			not isinstance(key, dict)
			or set(key) != {"source", "context"}
			or not isinstance(key["source"], str)
			or (key["context"] is not None and not isinstance(key["context"], str))
		):
			raise ValueError("invalid site exception key")
		identity = key["source"], key["context"] or ""
		if identity not in active or entry["source_digest"] != active[identity]["source_digest"]:
			raise ValueError("stale site exception")
		if not all(
			isinstance(entry[field], str) and entry[field].strip() for field in ("approver", "reason")
		) or not isinstance(entry["revoked"], bool):
			raise ValueError("unreviewed site exception")
		token = (*identity, entry["source_digest"])
		if identity in seen:
			raise ValueError("ambiguous site exception")
		seen.add(identity)
		if not entry["revoked"]:
			allowed.add(token)
	return allowed, policy_digest


def trusted_site_policy(exception_path):
	"""Authenticate site approvals against the pinned Release Inventory, without needing the CSV."""
	from frappe_lt.catalog_quality import _load_trusted, _validate_inventory

	compatibility_path = Path(__file__).with_name("compatibility.json")
	inventory, _, _, _ = _load_trusted(None, compatibility_path)
	active = _validate_inventory(inventory)
	return _site_policy(exception_path, {(s, c or ""): e for (s, c), e in active.items()})


def _inputs(site, package_path, exception_path, *, allow_exact_inventory_patch=False):
	import frappe

	from frappe_lt.catalog_quality import _load_trusted, _validate_inventory
	from frappe_lt.inventory import load_compatibility, verify_environment

	# No site write (including the report directory) before full authentication.
	if allow_exact_inventory_patch:
		# The install gate authenticates tools, major, clean worktrees and the
		# real target keys against the release before allowing unpinned patches.
		# Import locally: install calls #13 for classification and apply.
		from frappe_lt import install

		ready = install._preflight(package_path, exception_path, classify_site=False)
		if ready["site"] != site:
			raise ValueError("install site does not match migration site")
	else:
		verify_environment(frappe, site=site, require_clean_upstream=True)
	fingerprints = authenticate_package(Path(package_path), frappe.utils.sanitize_html)
	compatibility_path = Path(__file__).with_name("compatibility.json")
	inventory, _, _, _ = _load_trusted(None, compatibility_path)
	active = _validate_inventory(inventory)
	active = {(source, context or ""): entry for (source, context), entry in active.items()}
	manifest = load_compatibility(compatibility_path)
	if allow_exact_inventory_patch and ready["inventory_digest"] != manifest["inventory_digest"]:
		raise ValueError("install inventory does not match migration inventory")
	allowed, policy_digest = _site_policy(exception_path, active)
	return fingerprints, active, allowed, manifest["inventory_digest"], policy_digest


def _snapshot(*, locked=False):
	import frappe

	rows = []
	last = ""
	# Keyset pages: the final empty locking read also locks the end of the lt range.
	while True:
		page = frappe.db.sql(
			"SELECT name, language, source_text, context, translated_text FROM `tabTranslation` "
			"WHERE language = 'lt' AND name > %s ORDER BY name LIMIT %s" + (" FOR UPDATE" if locked else ""),
			(last, PAGE_SIZE),
			as_dict=True,
		)
		rows.extend(dict(row) for row in page)
		if len(rows) > MAX_ROWS:
			raise ScanLimit("Translation scan exceeds safe row limit")
		if len(page) < PAGE_SIZE:
			return rows
		last = page[-1]["name"]


def preflight(site, package_path, exception_path=None, *, allow_exact_inventory_patch=False):
	"""Read-only DB classification; publish the complete private planned report."""
	try:
		fingerprints, active, allowed, inventory_digest, policy_digest = _inputs(
			site,
			package_path,
			exception_path,
			allow_exact_inventory_patch=allow_exact_inventory_patch,
		)
	except (ValueError, OSError, csv.Error, UnicodeError):
		return {"exit_code": 1, "state": "blocked"}
	import frappe

	try:
		rows = _snapshot()
	except ValueError:
		frappe.db.rollback()
		return {"exit_code": 1, "state": "blocked"}
	except Exception:
		frappe.db.rollback()
		return {"exit_code": 2, "state": "db_failure"}
	classification = classify(rows, fingerprints, active, allowed)
	run_id = secrets.token_hex(16)
	plan = {
		"schema_version": 1,
		"run_id": run_id,
		"site": site,
		"state": "planned",
		"package_sha256": PACKAGE_SHA256,
		"inventory_digest": inventory_digest,
		"policy_sha256": policy_digest,
		"rows": rows,
		"classification": classification,
	}
	try:
		root = _private_root(site, create=True)
		lock_path = root / "site.lock"
		if not lock_path.exists():
			fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
			os.close(fd)
		_check_lock(lock_path)
	except (OSError, ValueError):
		frappe.db.rollback()
		return {"exit_code": 3, "state": "report_failure", "run_id": run_id}
	try:
		_publish(root / (run_id + ".planned.json"), plan)
	except (OSError, ValueError):
		return {"exit_code": 3, "state": "report_failure", "run_id": run_id}
	finally:
		# Discard read transaction; Bench must not implicitly commit this command.
		frappe.db.rollback()
	return {
		"exit_code": 1 if classification["blocked"] else 0,
		"run_id": run_id,
		"state": "planned",
		"delete": len(classification["delete"]),
		"overrides": len(classification["overrides"]),
		"extras": len(classification["extras"]),
		"english_extras": len(classification["english_extras"]),
		"duplicates": len(classification["duplicates"]),
		"blocked": len(classification["blocked"]),
	}


def _marker():
	import frappe

	return frappe.db.sql(
		"SELECT value FROM `tabSingles` WHERE doctype = %s AND field = 'committed_run'",
		("frappe_lt_legacy_migration",),
		pluck=True,
	)


def _trusted_plan(root, site, run_id):
	plan = _read_private(root / (run_id + ".planned.json"))
	if (
		not isinstance(plan, dict)
		or set(plan)
		!= {
			"schema_version",
			"run_id",
			"site",
			"state",
			"package_sha256",
			"inventory_digest",
			"policy_sha256",
			"rows",
			"classification",
		}
		or plan.get("schema_version") != 1
		or plan.get("run_id") != run_id
		or plan.get("site") != site
		or plan.get("state") != "planned"
		or plan.get("package_sha256") != PACKAGE_SHA256
		or not isinstance(plan.get("inventory_digest"), str)
		or len(plan["inventory_digest"]) != 64
		or not isinstance(plan.get("policy_sha256"), str)
		or len(plan["policy_sha256"]) != 64
		or not isinstance(plan.get("rows"), list)
		or not isinstance(plan.get("classification"), dict)
		or set(plan["classification"])
		!= {"delete", "overrides", "extras", "english_extras", "duplicates", "blocked"}
		or not isinstance(plan["classification"].get("delete"), list)
		or not isinstance(plan["classification"].get("blocked"), list)
	):
		raise ValueError("invalid saved migration plan")
	if (
		any(
			not isinstance(row, dict)
			or set(row) != {"name", "language", "source_text", "context", "translated_text"}
			or not isinstance(row["name"], str)
			for row in plan["rows"]
		)
		or len({row["name"] for row in plan["rows"]}) != len(plan["rows"])
		or any(not isinstance(name, str) for name in plan["classification"]["delete"])
		or len(set(plan["classification"]["delete"])) != len(plan["classification"]["delete"])
		or not set(plan["classification"]["delete"]) <= {row["name"] for row in plan["rows"]}
	):
		raise ValueError("invalid saved migration rows")
	return plan


def _clear_cache():
	from frappe.translate import clear_cache

	clear_cache()  # clears USER_TRANSLATION_KEY, MERGED_TRANSLATION_KEY and bootinfo


def _stale(root, run_id):
	try:
		path = root / (run_id + ".final.json")
		if path.exists() and _read_private(path).get("state") == "committed":
			return {"exit_code": 1, "state": "stale", "run_id": run_id}
		_publish(
			path,
			{"run_id": run_id, "state": "rolled_back", "reason": "stale"},
			replace=True,
		)
	except (ValueError, OSError):
		return {"exit_code": 3, "state": "report_failure", "run_id": run_id}
	return {"exit_code": 1, "state": "stale", "run_id": run_id}


def apply(site, package_path, run_id, exception_path=None, *, allow_exact_inventory_patch=False):
	"""Own the sole SQL transaction, with durable postcommit report/cache recovery."""
	import frappe

	if len(run_id) != 32 or any(char not in "0123456789abcdef" for char in run_id):
		raise ValueError("invalid run ID")
	try:
		root = _private_root(site)
		plan = _trusted_plan(root, site, run_id)
		_check_lock(root / "site.lock")
	except (ValueError, OSError):
		return {"exit_code": 3, "state": "report_failure", "run_id": run_id}
	with _site_lock(root):
		frappe.db.rollback()  # command is the transaction owner
		try:
			marker = _marker()
		except Exception:
			frappe.db.rollback()
			return {"exit_code": 2, "state": "db_failure", "run_id": run_id}
		if marker == [run_id]:
			# SQL commit already happened: never label this attempt rolled_back.
			frappe.db.rollback()
			policy_changed = not _policy_unchanged(exception_path, plan["policy_sha256"])
			if (root / (run_id + ".done")).exists():
				try:
					completed = _read_private(root / (run_id + ".done"))
					final = _read_private(root / (run_id + ".final.json"))
				except (ValueError, OSError):
					return {"exit_code": 3, "state": "report_failure", "run_id": run_id}
				if (
					not isinstance(completed, dict)
					or set(completed) != {"run_id", "state", "postcommit_drift"}
					or completed["run_id"] != run_id
					or completed["state"] != "committed"
					or not isinstance(completed["postcommit_drift"], bool)
					or not isinstance(final, dict)
					or final.get("run_id") != run_id
					or final.get("site") != site
					or final.get("state") != "committed"
					or final.get("package_sha256") != PACKAGE_SHA256
					or final.get("deleted") != len(plan["classification"]["delete"])
					or final.get("postcommit_drift") != completed["postcommit_drift"]
				):
					return {"exit_code": 3, "state": "report_failure", "run_id": run_id}
				try:
					current = _snapshot()
				except Exception:
					frappe.db.rollback()
					return {"exit_code": 2, "state": "db_failure", "run_id": run_id}
				finally:
					frappe.db.rollback()
				if completed.get("postcommit_drift") or policy_changed or current != _expected_rows(plan):
					return {"exit_code": 1, "state": "stale", "run_id": run_id}
				return {"exit_code": 0, "state": "no_op", "run_id": run_id}
			return _finish(root, plan, force_stale=policy_changed)
		if marker and not (root / (marker[0] + ".done")).exists():
			frappe.db.rollback()
			return {"exit_code": 3, "state": "pending_recovery", "run_id": marker[0]}
		# No committed marker: only freshly authenticated inputs may authorize mutation.
		try:
			fingerprints, active, allowed, inventory_digest, policy_digest = _inputs(
				site,
				package_path,
				exception_path,
				allow_exact_inventory_patch=allow_exact_inventory_patch,
			)
		except (ValueError, OSError, csv.Error, UnicodeError):
			frappe.db.rollback()
			return {"exit_code": 1, "state": "blocked", "run_id": run_id}
		if plan["inventory_digest"] != inventory_digest:
			frappe.db.rollback()
			return {"exit_code": 1, "state": "stale", "run_id": run_id}
		policy_changed = plan["policy_sha256"] != policy_digest
		if plan["classification"]["blocked"]:
			frappe.db.rollback()
			return {"exit_code": 1, "state": "blocked", "run_id": run_id}
		if policy_changed:
			frappe.db.rollback()
			return {"exit_code": 1, "state": "stale", "run_id": run_id}
		if not _policy_unchanged(exception_path, policy_digest):
			frappe.db.rollback()
			return _stale(root, run_id)
		commit_started = False
		try:
			frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			frappe.db.sql("START TRANSACTION")
			rows = _snapshot(locked=True)
			actual = classify(rows, fingerprints, active, allowed)
			if actual != plan["classification"] or rows != plan["rows"]:
				frappe.db.rollback()
				return _stale(root, run_id)
			if not _policy_unchanged(exception_path, policy_digest):
				frappe.db.rollback()
				return _stale(root, run_id)
			if not actual["delete"]:
				frappe.db.rollback()
				try:
					_publish(
						root / (run_id + ".final.json"),
						{"run_id": run_id, "site": site, "state": "committed", "deleted": 0},
						replace=True,
					)
				except Exception:
					return {"exit_code": 3, "state": "report_failure", "run_id": run_id}
				return {"exit_code": 0, "state": "no_op", "run_id": run_id}
			planned_rows = {row["name"]: row for row in plan["rows"]}
			for offset in range(0, len(actual["delete"]), PAGE_SIZE):
				batch = [planned_rows[name] for name in actual["delete"][offset : offset + PAGE_SIZE]]
				placeholders = ", ".join(["(%s, BINARY %s, %s, BINARY %s)"] * len(batch))
				values = []
				for row in batch:
					values.extend(
						(row["name"], row["source_text"], row["context"] or "", row["translated_text"])
					)
				frappe.db.sql(
					"DELETE FROM `tabTranslation` WHERE BINARY language = BINARY 'lt' AND "
					"(name, BINARY source_text, COALESCE(context, ''), BINARY translated_text) IN ("
					+ placeholders
					+ ")",
					tuple(values),
				)
				if frappe.db._cursor.rowcount != len(batch):
					raise ValueError("conditional Translation deletion failed")
			frappe.db.sql(
				"DELETE FROM `tabSingles` WHERE doctype = %s AND field = 'committed_run'",
				("frappe_lt_legacy_migration",),
			)
			frappe.db.sql(
				"INSERT INTO `tabSingles` (doctype, field, value) VALUES (%s, 'committed_run', %s)",
				("frappe_lt_legacy_migration", run_id),
			)
			commit_started = True
			frappe.db.commit()
		except ScanLimit:
			frappe.db.rollback()
			return _stale(root, run_id)
		except Exception:
			if commit_started:
				return {"exit_code": 3, "state": "commit_uncertain", "run_id": run_id}
			frappe.db.rollback()
			try:
				_publish(
					root / (run_id + ".final.json"), {"run_id": run_id, "state": "rolled_back"}, replace=True
				)
			except Exception:
				return {"exit_code": 3, "state": "report_failure", "run_id": run_id}
			return {"exit_code": 2, "state": "db_failure", "run_id": run_id}
		return _finish(root, plan)


def _finish(root, plan, *, force_stale=False):
	run_id = plan["run_id"]
	try:
		current = _snapshot()
	except Exception:
		return {"exit_code": 3, "state": "db_failure", "run_id": run_id}
	expected = _expected_rows(plan)
	# The transactional DB marker proves our commit; the current rows reveal later site edits.
	drifted = current != expected or force_stale
	result = {
		"run_id": run_id,
		"state": "committed",
		"site": plan["site"],
		"package_sha256": plan["package_sha256"],
		"deleted": len(plan["classification"]["delete"]),
		"postcommit_drift": drifted,
	}
	try:
		_publish(root / (run_id + ".final.json"), result, replace=True)
	except Exception:
		return {"exit_code": 3, "state": "report_failure", "run_id": run_id}
	try:
		_clear_cache()
	except Exception:
		return {"exit_code": 3, "state": "cache_failure", "run_id": run_id}
	try:
		_publish(
			root / (run_id + ".done"),
			{"run_id": run_id, "state": "committed", "postcommit_drift": drifted},
		)
	except Exception:
		return {"exit_code": 3, "state": "report_failure", "run_id": run_id}
	if drifted:
		return {"exit_code": 1, "state": "stale", "run_id": run_id}
	return {"exit_code": 0, "state": "committed", "run_id": run_id, "deleted": result["deleted"]}


def _expected_rows(plan):
	deleted_names = set(plan["classification"]["delete"])
	return [row for row in plan["rows"] if row["name"] not in deleted_names]
