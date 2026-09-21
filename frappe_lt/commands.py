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


@click.command("validate-lithuanian-runtime")
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=str))
@pass_context
def validate_lithuanian_runtime(context, output_dir=None):
	"""Validate the reviewed running-interface denominator."""
	import frappe

	from frappe_lt.runtime_validation import run

	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		result = run(site, output_dir)
		click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
		if result["exit_code"]:
			raise click.exceptions.Exit(result["exit_code"])
	finally:
		frappe.destroy()


commands = [
	build_translation_inventory,
	catalog_quality_gate,
	setup_lithuanian_profile,
	show_lithuanian_profile_status,
	restore_lithuanian_profile,
	leave_lithuanian_profile,
	validate_lithuanian_runtime,
]
