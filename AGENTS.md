# AGENTS.md

Plugin-based configuration and container management (docker stacks + app stores), served as a Typer CLI and a NiceGUI web UI. Python 3.13, managed with uv.

## Commands

```sh
uv sync                          # install everything, incl. dev group
uv run pytest                    # 525 tests, no lint/typecheck configured
uv run pytest tests/test_x.py    # one file
uv run sphinx-build -b html docs docs/_build
./scripts/test-data.sh           # build gitignored test_data/ (compose dir + example app store)
```

Dev loop from a checkout: run `./scripts/test-data.sh`, then `uv run spiriconfig appstore check` / `appstore install whoami` / `docker up whoami`. Path defaults are the real install paths (`spiriconfig/paths.py`: `/srv/compose` as root, `~/spiri-apps` otherwise); `test-data.sh` appends overrides to the checkout's `.env` pointing at `test_data/`, so a checkout never touches them. The `.env` file in the CWD is read but real env vars win.

**Docs are stale**: README and docs/ say the web UI default port is 8080; the code default is `8337` (`SPIRICONFIG_PORT`, `src/spiriconfig/service.py:50`). Trust `src/spiriconfig/config.py` and `service.py` over prose. README's "92 tests" is outdated too.

## Layout

- `src/spiriconfig/` — core: CLI, `Command`/`run`/`stream`/`stream_pty`, NiceGUI shell, config, PAM auth, TLS, reverse proxy, plugin discovery.
- `src/spiriconfig_{docker,appstore,terminal,users,system}/` — one package per plugin, all first-class citizens. A plugin = subclass of `spiriconfig.plugins.Plugin` registered under the `spiriconfig.plugins` entry point group (`pyproject.toml`). Installing the package is what registers it; nothing special about the bundled ones.
- `docs/` — Sphinx/MyST. `docs/design.md` explains why the code looks like this; read it before changing the core.
- `NOTES-out-of-process-plugins.md` — scratch notes (explicitly undecided) on proxying container plugins; not documentation.

## Rules that differ from defaults

- **Everything shells out to the command a human would run.** No docker-py, no API calls. Build a `Command` (which renders a copy-pasteable shell line) separately from running it — that is what enables `--show` and no-daemon tests.
- **No state of ours.** No DB, no registry, no enable flags. A stack exists because a directory with a compose file exists; running state comes from `docker ps`. Never add a source of truth we have to keep in sync.
- **Anything SpiriConfig can do, the user must be able to do without it** (CLI ↔ web parity). A web-only button is a bug.
- **Advanced mode hides, never forbids** (`spiriconfig.advanced.only()`); PAM auth is a login gate, not permissions. Hidden ≠ removed capability.
- **Compose files are text, never round-tripped through a YAML parser.** Validate with `docker compose config`.
- **Plugins**: log via `logger.bind(plugin=...)` (don't configure sinks — the core does), settings from env via pydantic-settings with a `SPIRICONFIG_<PLUGIN>_` prefix, per-plugin `cli.py`/`config.py`/`web.py`.

## Testing quirks

- Tests mostly assert on the *built command line* and never need a docker daemon. Tests that do need one are marked `docker_required` (see `tests/conftest.py`) and skip when docker isn't available.
- `tests/conftest.py` loads `nicegui.testing.user_plugin` (in-process page rendering; the selenium `screen` fixture is deliberately excluded).
- Docker tests must use `unique_stack`'s UUID-named projects — "hello" would collide with a developer's own containers on the same daemon.
- New web pages registered with `web.build(plugins)` in each test; there is no `main.py` (see `main_file = ""` in pyproject).

## Gotchas

- `stream_pty` must keep using `subprocess.Popen` + `preexec_fn`, NOT `asyncio.create_subprocess_exec`: NiceGUI serves on uvloop, which refuses `preexec_fn`. See `commands.py:221`.
- pty output is raw bytes (escape sequences intact), sent to xterm.js — never decode them on our side.
- Never create a `ui.timer` that lands inside the container a refresh is about to clear (see design.md "Refresh must not schedule itself inside the thing it clears").
- Plugin page functions have FastAPI route signatures: capture the plugin by closure, never as a default argument (`web.py:182`).
- With PAM auth, the websocket must be gated too (`auth.install_websocket_guard()`), not just HTTP pages — page-render-only auth is skin-deep.
- Out-of-process plugin discovery reads docker labels `spiriconfig.plugin.*` via `docker ps`/`inspect`; a machine with no docker logs and returns nothing rather than failing.
