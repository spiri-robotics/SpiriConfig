---
name: screenshot
description: Launch SpiriConfig's web UI against test_data and capture screenshots of it with headless Chromium, for README/docs images. Use whenever asked to screenshot, capture, or show the SpiriConfig web UI.
---

# Screenshotting the SpiriConfig web UI

SpiriConfig's UI is a server-rendered NiceGUI app -- there is no static
markup to inspect, so "screenshot it" means: launch the real server against
`test_data/`, drive a headless browser at it, and save what renders.

This machine is NixOS: there is no `node`/`npm`, so the `chromium-cli` tool
the general `run` skill points to isn't available, and no `chromium-cli`
package exists in `nixpkgs` either. This skill is the verified fallback --
Python's `playwright` package against the system `chromium` derivation.

## 1. One-time-per-session setup

Playwright's PyPI wheel for `greenlet` is a precompiled binary that expects
`libstdc++.so.6` on the loader path. NixOS has no global `/usr/lib`, so the
import fails with `ImportError: libstdc++.so.6: cannot open shared object
file` unless `LD_LIBRARY_PATH` points at one. Resolve both the browser and
the missing library through `nix-build` (not `nix-shell -p ... --run`,
which does not reliably export `LD_LIBRARY_PATH` for library-only
derivations) so the paths are exact and don't drift with nixpkgs updates:

```bash
CHROMIUM=$(nix-build --no-out-link '<nixpkgs>' -A chromium)/bin/chromium
LIBSTDCXX=$(nix-build --no-out-link '<nixpkgs>' -A stdenv.cc.cc.lib)/lib
export LD_LIBRARY_PATH="$LIBSTDCXX"
```

Then create a scratch venv with just the `playwright` Python package --
**do not** run `playwright install`, it tries to download its own Chromium
build; the system one above is what `executable_path` will point at:

```bash
SCRATCH=/tmp/claude-1000/-home-traverseda-Code-spiri-SpiriConfig/*/scratchpad  # use this session's actual scratchpad path
uv venv "$SCRATCH/shot-venv" --python 3.13
uv pip install --python "$SCRATCH/shot-venv/bin/python" playwright
```

## 2. Launch the app

Follow the README's dev flow. `test_data/` is gitignored and disposable; if
it's already there from a previous run, leave it -- rebuilding wipes any
installed apps.

**Check for an already-running dev server first** -- the user may have
their own `spiriconfig serve` up on 8337 for their own work. If port 8337
already answers, use that instance (skip launching, skip killing it in
cleanup). Only start and later stop a server whose PID you captured
yourself:

```bash
if curl -sf http://localhost:8337 >/dev/null; then
    echo "already running -- reuse it, do not kill it in cleanup"
else
    [ -e test_data ] || ./scripts/test-data.sh
    nohup uv run spiriconfig serve > "$SCRATCH/serve.log" 2>&1 &
    SERVE_PID=$!
    disown
    timeout 30 bash -c 'until curl -sf http://localhost:8337 >/dev/null; do sleep 1; done'
fi
```

**The dev server listens on port 8337**, not the 8080 the README's
production `install` flow uses -- that's NiceGUI's own default port, and
`spiriconfig serve` doesn't override it. Check `$SCRATCH/serve.log` if the
`curl` poll times out.

Known routes worth screenshotting: `/` (plugin dashboard), `/docker` (the
Apps page -- stack list with Up/Down/Restart/Pull/Logs), and whatever other
plugins are enabled in `test_data/compose` (`/appstore`, `/users`, ...).

## 3. Drive it and screenshot

`LD_LIBRARY_PATH` and `CHROMIUM` from step 1 must still be exported in the
shell that runs this script:

```python
from playwright.sync_api import sync_playwright
import os

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=os.environ["CHROMIUM"],
        headless=True,
        args=["--no-sandbox"],
    )
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto("http://localhost:8337/docker", wait_until="networkidle")
    page.wait_for_timeout(1000)  # let NiceGUI's websocket push finish rendering
    page.screenshot(path="apps.png", full_page=True)
    browser.close()
```

Run it with the scratch venv's interpreter: `"$SCRATCH/shot-venv/bin/python" shoot.py`.

To capture a dialog (e.g. the "Up" command-preview modal), turn on the
**Advanced** toggle in the left drawer first --
`page.get_by_text("Advanced", exact=True).first.click()` -- advanced-only
UI (the copyable command line) is hidden otherwise. Clicking **Up** on an
already-running stack is safe: `docker compose up -d` on a running stack is
a no-op that just confirms it's up.

## 4. Clean up

Only stop the server if you started it yourself this session (you have
`$SERVE_PID` from step 2). If port 8337 was already answering before you
touched it, it's the user's -- leave it running, full stop.

```bash
[ -n "${SERVE_PID:-}" ] && kill "$SERVE_PID"
rm -rf "$SCRATCH/shot-venv"
```

Don't `pkill -f spiriconfig` and don't `lsof -ti:8337 | xargs kill` --
both kill by port/pattern rather than by the PID you actually launched,
and will just as happily kill the user's own long-running dev server.

## Where screenshots live in this repo

`docs/images/`, referenced from `README.md` with relative Markdown image
links, e.g. `![...](docs/images/apps.png)`. Keep filenames descriptive
(`apps.png`, `up-command.png`, `dashboard.png`) rather than numbered.
