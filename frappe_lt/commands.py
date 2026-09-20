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


commands = [build_translation_inventory]
