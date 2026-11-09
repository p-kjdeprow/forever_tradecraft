# Launch target

Crowley keeps strict target contracts at
`src/crowley/data/targets/<targetId>.json`, with the active target named by
`src/crowley/data/targets/selected`. The selected contract is now the measured
WoW Forever target `forever-us-camwow7`. The historical `verified-beta` target
remains packaged for retained fixtures and evidence; its beta facts are not
relabeled as Forever facts. Forever route provenance is unmeasured, so no route
is inferred from the beta evidence. The contract supplies the
full client build, native build/interface, executable/window class,
product/flavor, locale, realm, faction/house, route provenance, and scan
cadence/cap/price-level settings. It contains no installation, account,
character, or other machine-specific paths.

The target is packaged with the Python wheel. Runtime consumers load it through
`crowley.core.game_target.load_target` and `selected_target`; unknown fields, missing fields, booleans or floats
in integer positions, invalid House values, inconsistent houses, malformed
builds, unsupported cadence values, and multi-component product directories
fail closed. The catalog loader checks the target-bound catalog metadata.
The generator reads raw catalog JSON independently, binds its build metadata to
the selected target, and then renders the canonical catalog, target contract,
addon target, and TOC interfaces. It never rewrites TOML. Operational Python
qualification and default plan emitters consume that same packaged plan count;
historical payload fixtures remain wire-admission tests rather than launch
facts.

The client-data exporter currently supports only the target's `enUS` locale and
passes the target full build into its pinned offline exporter. A target edit
does not claim that a new locale, build, route, realm, or economy is available;
those require separately measured and reviewed inputs. Session, companion, and
ops resolve their paths from the selected target, `$XDG_CONFIG_HOME/crowley/host.toml`,
and their strict tunables files. The canonical plan is cached by
`canonical_plan(target)`.

The native region ID and realm are also target facts. The addon requires both
native values to match the selected target before auto-attributing a house; a
deliberate `/crowley set` override remains the explicit escape hatch for that
field. Native faction and neutral-zone detection remain live client facts.

Use the following commands from the repository root:

```text
uv run python tools/generate_launch_target.py --check
uv run python tools/generate_launch_target.py --write
```

`--check` never writes. `--write` updates only generated repository artifacts;
it does not modify live configuration, installed game files, Data.lua,
SavedVariables, routes, or evidence. The historical beta route is accepted only
with its exact measured provenance and verified beta full build; selecting the
Forever target rejects that route until a new route is measured.

The scan cadence is deliberately a fixed 900-second policy because the current
wire contract admits only that cadence. A future target change must first add a
measured, compatible wire contract; changing this field in the target alone is
rejected. The beta sweep cap and all live launch qualification still require
fresh evidence after any target change.
