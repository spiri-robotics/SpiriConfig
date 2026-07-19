# ESC Config

Configure a drone's ESCs.

A Spiri-ecosystem app: it ships as a container image and its `compose.yaml`
declares its settings so [SpiriConfig](https://github.com/spiri/SpiriConfig) can
install and configure it from an app store.

## Examples

Scaffolded with one example, each a self-contained module you can keep, edit, or delete:

- **nicegui** (`esc_config/nicegui_app.py`) -- a NiceGUI web app on port
  8080. Its `spiriconfig.plugin.*` labels let SpiriConfig discover it and frame it
  in its shell; standalone it is an ordinary NiceGUI app.

## Run it

```console
$ uv sync
$ uv run esc-config
```

Or as the store runs it, in a container with live reload:

```console
$ docker compose -f compose.yaml -f compose.dev.yaml up --build
```

## Configure it

Each example's settings are environment variables, listed under `x-spiri-settings`
in `compose.yaml` and mirrored in that example's `Settings` class:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ESC_CONFIG_GREETING` | `Hello from ESC Config` | The message the app shows. |

See [docs/configuration.md](docs/configuration.md) for the full field reference.

## Release it

```console
$ uv run scripts/release.py patch     # bumps pyproject + compose together, tags
$ git push && git push --tags         # the tag publishes the image to GHCR
```

The image is published to `ghcr.io/spiri/esc-config`.

## Install it on a machine

Add this directory to a Spiri app store (a git repo with one directory per app),
then from the target machine:

```console
$ spiriconfig appstore sync
$ spiriconfig appstore install esc-config
$ spiriconfig docker up esc-config
```

---

Generated from the [spiri-app-template](https://github.com/spiri/spiri-app-template)
Copier template. Run `copier update` to pull in later improvements.
