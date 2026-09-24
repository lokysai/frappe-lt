import json

import click
from frappe.commands import get_site, pass_context


@click.command("build-translation-inventory")
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=str))
@click.option("--previous-inventory", type=click.Path(dir_okay=False, path_type=str))
@click.option("--previous-compatibility", type=click.Path(dir_okay=False, path_type=str))
@pass_context
def build_translation_inventory(
	context,
	output_dir=None,
	previous_inventory=None,
	previous_compatibility=None,
):
	"""Build the pinned v16 translation inventory from a clean site."""
	import frappe

	from frappe_lt.inventory import run

	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		result = run(
			site=site,
			output_dir=output_dir,
			previous_inventory=previous_inventory,
			previous_compatibility=previous_compatibility,
		)
		click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
	finally:
		frappe.destroy()


@click.command("catalog-quality-gate")
@click.option("--candidate", "candidate_name", required=True)
@click.option("--output", "output_path", required=True, type=click.Path(dir_okay=False, path_type=str))
@click.option("--report", "report_path", required=True, type=click.Path(dir_okay=False, path_type=str))
@click.option("--fail-fast", is_flag=True)
def catalog_quality_gate(candidate_name, output_path, report_path, fail_fast):
	"""Validate, compile, and atomically publish a registered catalog candidate."""
	from frappe_lt.catalog_quality import run

	result = run(candidate_name, output_path, report_path, fail_fast=fail_fast)
	click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
	if result["exit_code"]:
		raise click.exceptions.Exit(result["exit_code"])


def _run_profile_command(context, method, **kwargs):
	import frappe

	from frappe_lt import profile

	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		result = getattr(profile, method)(**kwargs)
		click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
	finally:
		frappe.destroy()


@click.command("setup-lithuanian-profile")
@pass_context
def setup_lithuanian_profile(context):
	"""Apply or resume the Lithuanian regional profile."""
	_run_profile_command(context, "apply")


@click.command("show-lithuanian-profile-status")
@pass_context
def show_lithuanian_profile_status(context):
	"""Show the Lithuanian regional profile state."""
	_run_profile_command(context, "status")


@click.command("restore-lithuanian-profile")
@pass_context
def restore_lithuanian_profile(context):
	"""Guardedly restore values saved before profile setup."""
	_run_profile_command(context, "restore")


@click.command("leave-lithuanian-profile")
@click.option("--confirm-leave-profile", is_flag=True)
@pass_context
def leave_lithuanian_profile(context, confirm_leave_profile):
	"""Irreversibly leave applied profile values in place."""
	_run_profile_command(context, "abandon", confirmed=confirm_leave_profile)


@click.command("export-lithuanian-runtime-candidates")
@click.option(
	"--output",
	"output_path",
	required=True,
	type=click.Path(dir_okay=False, path_type=str),
)
@pass_context
def export_lithuanian_runtime_candidates(context, output_path):
	"""Export pinned runtime discovery without mutating the site."""
	import frappe

	from frappe_lt.runtime_discovery import export_candidate_snapshot

	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		result = export_candidate_snapshot(site, output_path)
		click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
	finally:
		frappe.destroy()


@click.command("validate-lithuanian-runtime")
@click.option("--diagnostic-sampling", is_flag=True)
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=str))
@click.option("--site-exceptions", "site_exception_path", type=click.Path(dir_okay=False))
@pass_context
def validate_lithuanian_runtime(
	context, diagnostic_sampling=False, output_dir=None, site_exception_path=None
):
	"""Validate the reviewed running-interface denominator."""
	import frappe

	from frappe_lt.runtime_validation import run

	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		result = run(
			site, output_dir, diagnostic_sampling=diagnostic_sampling, site_exception_path=site_exception_path
		)
		click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
		if result["exit_code"]:
			raise click.exceptions.Exit(result["exit_code"])
	finally:
		frappe.destroy()


def _legacy_command(context, operation, package_path, exception_path=None, run_id=None):
	import frappe

	from frappe_lt import legacy_migration

	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		try:
			if operation == "preflight":
				result = legacy_migration.preflight(site, package_path, exception_path)
			else:
				result = legacy_migration.apply(site, package_path, run_id, exception_path)
		except Exception as error:
			from filelock import Timeout

			if isinstance(error, Timeout | ValueError):
				result = {"exit_code": 1, "state": "blocked"}
			elif isinstance(error, OSError):
				result = {"exit_code": 3, "state": "report_failure"}
			else:
				result = {"exit_code": 2, "state": "db_failure"}
		click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
		if result["exit_code"]:
			raise click.exceptions.Exit(result["exit_code"])
	finally:
		frappe.db.rollback()
		frappe.destroy()


@click.command("preflight-legacy-translations")
@click.option("--package", "package_path", required=True, type=click.Path(dir_okay=False))
@click.option("--site-exceptions", "exception_path", type=click.Path(dir_okay=False))
@pass_context
def preflight_legacy_translations(context, package_path, exception_path):
	"""Authenticate original package and publish private read-only removal plan."""
	_legacy_command(context, "preflight", package_path, exception_path)


@click.command("apply-legacy-translations")
@click.option("--package", "package_path", required=True, type=click.Path(dir_okay=False))
@click.option("--run-id", required=True)
@click.option("--site-exceptions", "exception_path", type=click.Path(dir_okay=False))
@pass_context
def apply_legacy_translations(context, package_path, run_id, exception_path):
	"""Apply a published plan or recover its postcommit finalization."""
	_legacy_command(context, "apply", package_path, exception_path, run_id)


def _install_command(context, operation, **kwargs):
	import frappe

	from frappe_lt import install

	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		try:
			result = getattr(install, operation)(**kwargs)
		except Exception as error:
			frappe.db.rollback()
			# Never expose private Translation values, file contents or upstream tracebacks.
			code = str(error) if isinstance(error, install.InstallError) else "INSTALL_FAILED"
			click.echo(json.dumps({"state": "blocked", "code": code}, sort_keys=True))
			raise click.exceptions.Exit(1) from None
		click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
	finally:
		frappe.db.rollback()
		frappe.destroy()


@click.command("preflight-lithuanian-install")
@click.option("--package", required=True, type=click.Path(dir_okay=False))
@click.option("--site-exceptions", "exceptions", type=click.Path(dir_okay=False))
@pass_context
def preflight_lithuanian_install(context, package, exceptions):
	"""Read-only bench preflight; run after get-app, before install-app."""
	_install_command(context, "preflight", package=package, exceptions=exceptions)


@click.command("prepare-lithuanian-install")
@click.option("--package", required=True, type=click.Path(dir_okay=False))
@click.option("--site-exceptions", "exceptions", type=click.Path(dir_okay=False))
@pass_context
def prepare_lithuanian_install(context, package, exceptions):
	"""Publish the private migration plan, save inputs and enter maintenance."""
	_install_command(context, "prepare", package=package, exceptions=exceptions)


@click.command("resume-lithuanian-install")
@pass_context
def resume_lithuanian_install(context):
	"""Resume committed migration recovery, MO, profile and final verification."""
	_install_command(context, "resume")


@click.command("show-lithuanian-install-status")
@pass_context
def show_lithuanian_install_status(context):
	"""Show safe site install state without private Translation contents."""
	_install_command(context, "status")


def _release_candidate_command(context, operation, **kwargs):
	import frappe

	from frappe_lt import release_candidate

	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		try:
			result = getattr(release_candidate, operation)(site=site, frappe_module=frappe, **kwargs)
		except Exception:
			frappe.db.rollback()
			click.echo(json.dumps({"state": "blocked", "code": "RELEASE_CANDIDATE_FAILED"}, sort_keys=True))
			raise click.exceptions.Exit(1) from None
		click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
	finally:
		frappe.db.rollback()
		frappe.destroy()


@click.command("capture-lithuanian-release-candidate")
@click.option("--output-dir", required=True, type=click.Path(file_okay=False, path_type=str))
@pass_context
def capture_lithuanian_release_candidate(context, output_dir):
	"""Bind one clean candidate and its authenticated contracts before mutation."""
	_release_candidate_command(context, "capture", output_dir=output_dir)


@click.command("finalize-lithuanian-release-candidate")
@click.option("--evidence-dir", required=True, type=click.Path(file_okay=False, path_type=str))
@click.option("--site-exceptions", "site_exception_path", type=click.Path(dir_okay=False))
@pass_context
def finalize_lithuanian_release_candidate(context, evidence_dir, site_exception_path):
	"""Validate existing release evidence and publish its immutable public index."""
	_release_candidate_command(
		context,
		"finalize",
		root=evidence_dir,
		site_exception_path=site_exception_path,
	)


commands = [
	build_translation_inventory,
	catalog_quality_gate,
	setup_lithuanian_profile,
	show_lithuanian_profile_status,
	restore_lithuanian_profile,
	leave_lithuanian_profile,
	export_lithuanian_runtime_candidates,
	validate_lithuanian_runtime,
	preflight_legacy_translations,
	apply_legacy_translations,
	preflight_lithuanian_install,
	prepare_lithuanian_install,
	resume_lithuanian_install,
	show_lithuanian_install_status,
	capture_lithuanian_release_candidate,
	finalize_lithuanian_release_candidate,
]
