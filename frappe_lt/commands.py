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


commands = [
	build_translation_inventory,
	setup_lithuanian_profile,
	show_lithuanian_profile_status,
	restore_lithuanian_profile,
	leave_lithuanian_profile,
]
