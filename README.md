# frappe_lt

`frappe_lt` is a GPL-3.0 Frappe v16 app for Lithuanian translations. This first vertical slice ships one native gettext override: contextless `Item` -> `Prekė`.

[`CONTEXT.md`](CONTEXT.md) is the authoritative translation context. It was bootstrapped from `lokysai/Fab` commit `a40dc5f554df751a4227a192d55590fdd1eda4ed`; subsequent context maintenance happens only in this repository.

## Compatibility

The first smoke target is pinned exactly:

| Component | Version | Commit |
| --- | --- | --- |
| Frappe | 16.34.0 | `c1f1e8ec3708750d7254f7f99d869ffb9886f19f` |
| ERPNext | 16.35.0 | `12cd563fb9a79731f75ae2a45b1446a0a2dd9e74` |
| Python | 3.14.x | n/a |
| Babel | 2.16.0 | n/a |

ERPNext is required and must already be installed on the site before `frappe_lt`.

## Installation

From a compatible bench:

```bash
bench get-app erpnext https://github.com/frappe/erpnext --branch version-16
git -C apps/erpnext fetch origin 12cd563fb9a79731f75ae2a45b1446a0a2dd9e74
git -C apps/erpnext checkout --detach 12cd563fb9a79731f75ae2a45b1446a0a2dd9e74
bench setup requirements
bench --site development.localhost install-app erpnext
bench get-app frappe_lt https://github.com/lokysai/frappe-lt
bench --site development.localhost install-app frappe_lt
```

The bench's Frappe app must likewise be at the pinned commit shown above. Production installation should pin released commits or tags rather than moving branches. The verification command rejects upstream commits other than the two listed above.

## Verification

Run the same command used by CI from the bench root:

```bash
bench --site development.localhost execute frappe_lt.verify.run \
  --kwargs '{"site":"development.localhost","mode":"local"}'
```

CI uses `mode: "ci"`, which also requires an otherwise empty app list and no contextless Lithuanian `Translation` row for `Item`. Local mode allows unrelated apps but warns if such a database override exists; that warning means the effective runtime result cannot prove app origin.

The command validates the pinned Frappe and ERPNext commits, Python 3.14, Babel 2.16.0, app order, and strict PO structure. It removes the old MO, compiles twice from clean output with fixed `SOURCE_DATE_EPOCH`, compares both SHA-256 values with the repository constant, proves catalog precedence, checks database masking, clears site cache, and requires `frappe._("Item", lang="lt") == "Prekė"`.

The generated `sites/assets/locale/lt/LC_MESSAGES/frappe_lt.mo` is intentionally untracked.

## Release inventory

The machine-readable compatibility contract is [`frappe_lt/compatibility.json`](frappe_lt/compatibility.json). It is the authority for upstream commits and versions, artifact schema versions, Python/Babel versions, `SOURCE_DATE_EPOCH`, clean-install runtime metadata signatures, and the expected inventory and MO digests. Its `artifact_sha256` map authenticates every versioned release artifact other than the manifest itself.

Generate all release artifacts from a clean site after installing ERPNext but before installing `frappe_lt`:

```bash
bench --site inventory.localhost build-translation-inventory
```

The one public orchestration function is `frappe_lt.inventory.run`; the Bench command is only its pre-install transport. It requires exactly `frappe` and `erpnext`, rejects metadata that differs from the authenticated clean-install signatures (while accepting the exact system-generated Custom Fields and Property Setters), rejects dirty or unpinned upstream worktrees, checks the pinned Python and Babel versions, and validates every record in [`frappe_lt/provenance.json`](frappe_lt/provenance.json). Missing, unknown, duplicate, or contradictory provenance blocks generation before any artifacts are written. A successful run atomically writes these owned artifacts:

- `frappe_lt/release_inventory.json`
- `frappe_lt/inventory_report.json`
- `frappe_lt/inventory_report.md`
- `frappe_lt/provenance.json`
- `frappe_lt/compatibility.json`, replaced last as the commit marker

For release comparison, pass both `--previous-inventory` and `--previous-compatibility`. The prior manifest digest must match the exact prior inventory bytes.

See the [one-time smoke report](docs/smoke-report.md) for runtime and browser evidence status.

## Tests

Inside the bench environment:

```bash
env/bin/python -m unittest frappe_lt.tests.test_verify
env/bin/python -m unittest frappe_lt.tests.test_inventory
bench --site development.localhost run-tests --app frappe_lt
```

## License

GPL-3.0-only. See [LICENSE](LICENSE).
