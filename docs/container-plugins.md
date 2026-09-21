# Container plugins

A plugin does not have to be Python that shares our interpreter. It can be a
container, in any language, that serves a web page -- SpiriConfig finds it,
proxies it into the shell, and frames it in an iframe next to every other
plugin's page. There is nothing special about this compared to an
[entry-point plugin](plugins.md): it is a second, independent way to plug in,
not a workaround.

The whole point is that the two do not share a dependency set. An entry-point
plugin shares our interpreter, so it shares our resolved `nicegui`/`pydantic`
pins, and we cannot bump either without a chance of breaking every third-party
one. A container plugin ships its own image with its own runtime -- its own
language, even -- and the only thing it shares with us is a wire contract.

## A plugin is an app with labels on it

If a plugin is a container, it is a compose app with some labels, and the
[app store](appstore.md) already knows how to install compose apps from a git
repo. Nothing new is needed on that side -- installing a container plugin
*is* installing an app:

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
      - /etc/netplan:/etc/netplan   # trusted code; mounts what it needs
```

```console
$ spiriconfig appstore install netplan
$ spiriconfig docker up netplan
```

That's it. There is no separate plugin registry, no enable flag, and no
install step beyond what installing any app already does:

| question | answered by | who does the work |
| --- | --- | --- |
| what plugins exist? | `docker ps --filter label=spiriconfig.plugin.name` | docker |
| install one | install an app from a store | the app store, unchanged |
| uninstall | remove the symlink | the app store, unchanged |
| is it running? | is the container up? | docker |
| restart when it dies | `restart: unless-stopped` in its compose file | docker |
| where did it come from? | `readlink` + `git` | the app store, unchanged |
| update it | `git merge` | the app store, unchanged |

## Discovery

{mod}`spiriconfig.discovery` polls `docker ps` for containers carrying
`spiriconfig.plugin.name`, then `docker inspect` to read the rest of the
labels and the container's own IP on its docker network:

```console
$ docker ps -q --filter label=spiriconfig.plugin.name
```

A container missing `spiriconfig.plugin.port`, or without a network IP yet,
is logged and skipped -- one misconfigured plugin does not blank the sidebar
for the rest. This rescans on an interval (`SPIRICONFIG_PLUGIN_DISCOVERY_INTERVAL`,
default 5 seconds; see [Settings](#settings) below), so an app installed or
stopped while SpiriConfig is running appears or disappears in the sidebar on
its own -- no restart needed. A machine with no docker simply reports no
container plugins; discovery logs and returns nothing rather than failing.

## The contract

A plugin author owes three things:

**1. Declare yourself with labels** -- `name`, `port` (required); `title`,
`icon` (optional, default to the name and a generic web icon).

**2. Work correctly behind a reverse proxy at a path prefix.** SpiriConfig
proxies the container at `/plugin/<name>/...`, on the same origin as the
shell -- not a port of its own. Same origin is why cookies, the PAM login
gate, and (below) the address bar sync all just work; a different port would
be a different origin and lose all three. The proxy sets
`X-Forwarded-Prefix: /plugin/<name>` on every request, so a framework that
honours that header emits correct URLs with no idea it is behind us.
NiceGUI does this thoroughly, so a NiceGUI plugin needs no URL-handling code
at all to port over.

**3. Include one script tag, if you want deep links.** An iframe is a
separate browsing context with its own URL, so without help the shell's
address bar never moves and the back button steps through the *iframe's*
history instead of the shell's. `/plugin-sdk/shell.js` fixes both: it mirrors
the plugin's own path up to the parent's address bar with
`history.replaceState`, same-origin, no `postMessage` handshake needed.

```html
<script src="/plugin-sdk/shell.js"></script>
```

Optional -- a plugin that skips it still works, it just sits at a URL that
never changes as you navigate inside it.

## What SpiriConfig proxies

{mod}`spiriconfig.proxy` forwards two kinds of traffic between `/plugin/<name>/...`
and the container's own root:

- **HTTP**, streamed both ways with `httpx` so a large download or an SSE
  stream is never buffered whole.
- **WebSocket**, relayed frame-for-frame -- the part that makes a live UI
  (NiceGUI's socket.io, or anything else riding a websocket) work at all.

The shell page a user actually navigates to is `/app/<name>/...` -- distinct
from `/plugin/<name>/...`, which is the iframe's `src` and returns the
container's raw bytes. `/app/<name>` renders the sidebar and frames the
proxied content in an iframe that fills the rest of the page; a name with no
live target (container stopped, or a stale link) gets an honest "not
available" card instead of a blank frame.

## This is not a sandbox

A plugin is trusted code the operator chose to install, same as an
entry-point one -- SpiriConfig is not defending against a hostile plugin. The
proxy forwards the plugin's cookies and inherits whatever auth sits in front
of the shell; it is plumbing, not a security boundary. A container plugin
bind-mounting `/var/run/docker.sock` or `/etc` is not a hole in the design,
it is the design: the same freedom an entry-point plugin already has by
sharing our process.

## The CLI face

A container plugin cannot add a Typer subcommand, and does not need to --
its entrypoint is its CLI:

```console
$ docker exec spiriconfig-netplan netplan-cli show
```

A command a human could have run, which is the same test every other plugin
is held to (see [Writing a plugin](plugins.md)).

## Settings

| Setting | Default | What it is |
| --- | --- | --- |
| `SPIRICONFIG_PLUGIN_DISCOVERY_INTERVAL` | `5.0` | Seconds between rescans for container plugins. `0` scans once at startup and never again. |

## Still open

The labels, discovery, and proxy described above are implemented and in use.
A few things around the edges are not yet decided -- an asset-cache cost per
plugin prefix, a dedicated "container is down" card, and whether the label
contract itself should carry a version (`spiriconfig.plugin.api`) so it can
change without breaking every plugin at once. See
`NOTES-out-of-process-plugins.md` for the reasoning still in flux; it is
scratch, not documentation, and should not be relied on for anything this
page already states as settled.
