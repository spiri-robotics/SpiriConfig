# ESC Config

Configure a drone's ESCs.

This app is packaged for the Spiri ecosystem: it ships as a container image on
GHCR, and its `compose.yaml` declares the settings SpiriConfig presents when
someone installs it from an app store.

```{toctree}
:maxdepth: 2

configuration
development
```

## Quick start

```console
$ uv sync
$ uv run esc-config
```

This project was scaffolded with one example:

- **nicegui** -- `esc_config.nicegui_app`

Each is a self-contained starting point in its own module. `uv run esc-config`
runs the first; name another to run it, e.g. `uv run esc-config nicegui`.
Keep the one you want to build on and delete the rest.

The `nicegui` example serves a web UI on <http://localhost:8080>. When its
container runs on a machine with SpiriConfig, SpiriConfig discovers it (from the
`spiriconfig.plugin.*` labels in `compose.yaml`) and frames it in its own shell.

## Settings reference

Each example reads its configuration from environment variables, mirrored in that
example's `Settings` class and in `compose.yaml`'s `x-spiri-settings`. See
[configuration.md](configuration.md) for the field-by-field reference.
