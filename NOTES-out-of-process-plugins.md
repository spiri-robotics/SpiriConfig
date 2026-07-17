# Notes: out-of-process plugins

Scratch. Not documentation. Nothing here is decided.

Working assumption: **a plugin is a container, and may be written in any language.**
Third pass; the first two chased the wrong motivation and are worth not repeating.

## This is not a sandbox

Stating the premise first, because I derived a whole security architecture from its
absence and had to throw it away, and future-me will be tempted to do it again.

**You are assumed to have root on machines you own.** A plugin is trusted code that
the operator chose to install. We are not defending against a hostile plugin, and we
should not build anything that implies we are — a security boundary we half-mean is
worse than none, because people will lean on it.

Consequences, all of them freeing:

- A plugin bind-mounts whatever it needs. `/var/run/docker.sock`, `/srv/compose`,
  `/etc`. That's not a hole in the design, it's the design.
- No capability API, no `Command`-over-RPC, no confirmation dialog as a security
  mechanism. (Drafted all three. Deleted all three. They only existed to defend a
  boundary we don't want.)
- Same-origin frontend "isolation" is not a goal. We couldn't have it anyway — CSRF
  is unpreventable same-origin, and an iframe `sandbox` that made the child a foreign
  origin would cost us URL sync to buy us protection from code we already trust.

So the same-origin question I flagged last pass as "the most important open question"
is **closed**: same origin, take the deep links, the plugin's JS can touch the shell
and that's fine.

## The actual motivation: version isolation

The thing that hurts today, and the reason the original phrasing was *"a plugin means
tightly coupling third-party code to my project"*:

An entry-point plugin shares our interpreter, so it shares **our resolved dependency
set**. There is exactly one environment, and everything in it must agree.

- A plugin cannot depend on a nicegui, pydantic, or typer we didn't pick.
- Two plugins with conflicting pins cannot coexist. Neither can be installed second.
- **We cannot bump nicegui without potentially breaking every third-party plugin**,
  and no plugin can upgrade independently of us. The whole ecosystem is welded to one
  `uv lock`.
- A plugin cannot be written in Go. Obviously. But that's the same problem wearing a
  bigger hat: we are dictating the plugin's entire runtime.

Containers dissolve all of it. Each plugin ships its own image with its own closure —
own language, own runtime, own deps — and the only thing shared is a wire contract.
We bump nicegui whenever we like. A plugin ships when it likes. Nobody's release
schedule is anybody else's problem.

That's the whole pitch. The rest of this doc is mechanism.

### Bonus, not the reason

Falls out for free, worth having, but don't lead with it:

- A plugin cannot **block the event loop** — the thing we genuinely cannot defend
  against in-process. One synchronous `subprocess.run` in a plugin's `page()` today
  freezes every user's UI, websockets included, so it doesn't even fail visibly. No
  `try`/`except` saves us. A container can't do it.
- Crashes, OOM, fd exhaustion, `sys.exit()`, a segfault in some C extension. cgroups
  and the process boundary cover what our `except` blocks can't.

## Shape

- plugin container serves HTTP on some port
- SpiriConfig reverse-proxies `/plugin/<name>/{path:path}` → the container,
  injecting `X-Forwarded-Prefix: /plugin/<name>`
- shell renders `<iframe src="/plugin/<name>/">` filling the main area

Same origin (a path on us, not a port on localhost) keeps cookies working and lets
parent and child talk. That matters more than it looks — see "rejected: one port per
plugin".

An iframe is the only way to get a container's UI into our page at all. The
alternative is a data protocol where the plugin describes widgets and we render them,
which means inventing a cross-language UI toolkit. No.

## A plugin is an app with labels on it

If a plugin is a container, a plugin is **a compose app with some labels**, and we
already built an entire subsystem for installing compose apps from a git repo.

```yaml
services:
  ui:
    image: ghcr.io/someone/spiriconfig-netplan
    labels:
      spiriconfig.plugin.name: netplan
      spiriconfig.plugin.title: Network
      spiriconfig.plugin.icon: lan
      spiriconfig.plugin.port: "8080"
    volumes:
      - /etc/netplan:/etc/netplan     # trusted code; mounts what it needs
```

Follow it through — almost everything falls out:

| question | answer | who does the work |
| --- | --- | --- |
| what plugins exist? | `docker ps --filter label=spiriconfig.plugin.name` | docker |
| install a plugin | install an app from a store | the app store, unchanged |
| uninstall | `rm` a symlink | the app store, unchanged |
| is it running? | is the container up? | docker |
| restart when it dies | `restart: unless-stopped` | docker |
| where did it come from? | `readlink` + `git` | the app store, unchanged |
| update it | `git merge` | the app store, unchanged |

**There is no state that is ours.** No plugin registry, no enabled/disabled, no
install manifest. Same argument design.md already makes three times, pointed at
plugins. The plugin system doesn't need building so much as *noticing* — it's the app
store plus a label convention.

Install instructions for a plugin author become "add it to your app store", which
users already know how to do.

## The contract

Three things, and they're all boring on purpose. Boring is what makes them
implementable in Go.

1. **Declare yourself** with labels (above).
2. **Work behind a reverse proxy at a subpath**, honouring `X-Forwarded-Prefix`.
3. **Include one script tag**, if you want deep links and our theme.

### On (2): the adoption tax

This is the real cost of the design and it's worth being clear-eyed. "Work correctly
under a path prefix" is a standard ask, and plenty of apps get it wrong — Flask needs
`ProxyFix`, anything that hardcodes `/static/…` breaks. A plugin author's first bug
will be this bug.

NiceGUI happens to be *excellent* at it. Verified against the 3.14.0 in `.venv`
rather than from memory, since it decides viability:

| what | where |
| --- | --- |
| reads `X-Forwarded-Prefix`, threads it into the page | `client.py:198` |
| assets, importmap, components | `dependencies.py:235-274` |
| favicon | `favicon.py:26` |
| socket.io connection path | `static/nicegui.js:422` |
| `ui.navigate.to` / `ui.download` | `static/nicegui.js:542-546` |
| redirect `Location` headers | `middlewares.py:12` |

A NiceGUI plugin with *no idea* it's behind a prefix emits correct URLs anyway,
purely from the header. Our bundled two would port with no URL work.

Its one gap — `link.py:29` writes `href` straight to the DOM, bypassing the JS that
prepends the prefix:

```python
ui.navigate.to('/routes')          # -> /plugin/netplan/routes  ✓
ui.link('Routes', target='/routes')  # -> /routes               ✗ escapes the iframe
```

### On (3): `shell.js`

The polyglot answer to the deep-link problem. We serve one small script at a
well-known URL; the plugin adds one tag:

```html
<script src="/plugin-sdk/shell.js"></script>
```

It does the history sync (below) and picks up the shell's theme, so a Go plugin
author gets both without writing either. Optional — a plugin that skips it still
works, it just sits at a URL that doesn't move and looks like itself.

Alternative was rewriting HTML responses in the proxy to inject it. That's a tarpit.
One script tag is a fair ask.

## iframe URL mechanics — the real work

Unchanged by any of the above, and still the highest-risk piece.

An iframe is a separate browsing context: its own document, its own URL. Not a widget
that renders someone's HTML into our page — a whole browser tab that happens to be
rectangle-shaped. **Two URLs at all times**, moving independently.

Shell at `robot.local/netplan`, iframe src `/plugin/netplan/`, so the iframe document
is at `robot.local/plugin/netplan/`. Inside it:

| href | resolves to | |
| --- | --- | --- |
| `routes` | `/plugin/netplan/routes` | stays in |
| `./routes` | `/plugin/netplan/routes` | stays in |
| `/routes` | `/routes` | **escapes** |

Two consequences, complementary — which hints at fixing both at once:

**The address bar never moves.** User goes three levels deep, hits F5, lands back at
the iframe's original `src`. Position gone, and it could never have been bookmarked or
shared. As far as the browser is concerned they never left `/netplan`.

**But the back button does move.** Iframe navigations land on the tab's joint session
history — and for NiceGUI they're *real* navigations (`nicegui.js:542` does
`window.open(url, "_self")`, a full document load, not client-side routing). So the
user hits Back to leave the plugin and the *iframe* steps back a page while the
address bar sits there unchanged. We'd hit this on day one.

So: the URL doesn't track state that history *does* track. Make it track.

### The fix (unproven)

Same origin, so the child reaches the parent's `history` directly — no `postMessage`
handshake, no cooperation needed beyond the script tag.

- shell route becomes `/netplan/{sub:path}`, rendering `<iframe src="/plugin/netplan/{sub}">`
- `shell.js` mirrors the child's path up on load, via `parent.history.replaceState`

Address bar tracks the plugin; deep links and reload work because the shell rebuilds
the iframe src from its own path; `replaceState` not `pushState` so we don't
double-stack history.

**Highest-uncertainty thing in the document.** Well-trodden pattern, does work, but
"sync two browsing contexts' histories" has more edge cases than that bullet list
admits — chiefly what Back *should* do once the parent URL tracks properly. Build it
and click around before believing it.

## The CLI face

design.md: *"a plugin that offers a web page and no CLI is a bug in spirit."*

A container plugin can't add a Typer subcommand, and doesn't need to — the container's
entrypoint *is* its CLI:

```console
$ docker exec spiriconfig-netplan netplan-cli show
```

A command a human could have run, which is the whole test. `spiriconfig netplan ...`
can forward to it, and `--show` prints the `docker exec` line. The principle survives
unamended, which is a good sign we're not fighting the design.

## Open questions

- **Asset cache is keyed per prefix.** Each plugin's framework assets live under its
  own prefix, so a browser caches Vue/Quasar once *per plugin*. Over loopback the
  bandwidth is free — it's memory and parse time, N runtimes for N plugins. Measure
  before worrying; on a robot it might matter.
- **Do the bundled two become containers?** They'd need the docker socket, which is
  now fine. But it means shipping SpiriConfig means shipping images, and the dev loop
  gets a build step. Keeping them in-process means two plugin systems, which is a
  smell. Suspect the honest answer is that first-party plugins are privileged and we
  *say so* — but it's unresolved and it affects the dev experience most.
- **The websocket proxy is the only real code.** HTTP proxying is trivial; the upgrade
  needs an ASGI relay pumping frames both ways. ~100 lines. Needed for any live UI,
  not just NiceGUI ones.
- **Dead plugin card.** `docker ps` says it's down → shell renders a card instead of
  an iframe. Cheap, because docker already knows.
- **Version-skew the contract.** The thing we just decoupled will re-couple here if
  we're careless: labels + prefix + `shell.js` is now a public API. It should be tiny
  and it should be versioned (`spiriconfig.plugin.api: "1"`), precisely so we never
  end up where we are today.

## Rejected

**One port per plugin.** Iframe `http://robot.local:9001/` directly; no prefix
contract, so the single biggest adoption tax disappears. Killed by the origin: a
different port is a different origin, so no shared cookies, no `window.parent` (history
sync needs `postMessage` and cooperation), a port to allocate and firewall per plugin,
and the plugin is reachable from the network directly, bypassing whatever auth the
shell grows. Isolation used to be the counter-argument *for* this; now that we don't
want isolation, its last advantage is gone. Stays rejected, more firmly than before.

**Entry points alongside containers.** Two contracts, two docs, and every author's
first question is "which kind do I write?". The container contract is a superset — a
Python plugin can be a container. If entry points survive it's as an explicitly
first-party, privileged mechanism, not a peer.

**A capability API (plugin POSTs us a `Command`, we run it).** Elegant — it reuses the
`Command`/`str(Command)` seam design.md already built — but its entire justification
was a security boundary. Without sandboxing it's a worse way for a plugin to run
`docker compose` than mounting the socket and running `docker compose`. Noted because
it's seductive and I want the reason it died to survive.

## Phasing

1. **Spike the iframe + history sync.** One hardcoded container serving anything,
   proxied under a prefix, wired into the shell. Click around. Riskiest bit, cheapest
   to test.
2. Proxy for real: HTTP + websocket, prefix injection.
3. Label discovery via `docker ps`. Should be small — the app store already installs.
4. `shell.js`.
5. Port a bundled plugin, or write a throwaway Go one to prove the polyglot claim
   isn't theoretical. **This is the acceptance test for the whole idea** — if writing a
   Go plugin isn't pleasant, none of the above was worth it.

Nothing below 2 matters if 1 is unpleasant.
