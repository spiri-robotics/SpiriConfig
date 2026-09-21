# Notes: out-of-process plugins

Scratch. Not documentation. What's below is genuinely undecided.

The settled shape -- labels, discovery via `docker ps`, the `/plugin` +
`/app` proxy, `shell.js`, the "this is not a sandbox" stance -- is
implemented and documented at [docs/container-plugins.md](docs/container-plugins.md).
This file only keeps what that page doesn't claim as settled.

## Open questions

- **Asset cache is keyed per prefix.** Each plugin's framework assets live under its
  own prefix, so a browser caches Vue/Quasar once *per plugin*. Over loopback the
  bandwidth is free -- it's memory and parse time, N runtimes for N plugins. Measure
  before worrying; on a robot it might matter.
- **Do the bundled two become containers?** They'd need the docker socket, which is
  now fine. But it means shipping SpiriConfig means shipping images, and the dev loop
  gets a build step. Keeping them in-process means two plugin systems, which is a
  smell. Suspect the honest answer is that first-party plugins are privileged and we
  *say so* -- but it's unresolved and it affects the dev experience most.
- **Dead plugin card.** `docker ps` says it's down → shell renders a card instead of
  an iframe. `_proxy_page` already does this when a name has no live target; whether
  it should say more (last-seen time, a restart button) is open.
- **Version-skew the contract.** Labels + prefix + `shell.js` is now a public API. It
  should be tiny and it should be versioned (`spiriconfig.plugin.api: "1"`), precisely
  so we never end up where the in-process plugin/nicegui coupling put us.

## Rejected

**One port per plugin.** Iframe `http://robot.local:9001/` directly; no prefix
contract, so the single biggest adoption tax disappears. Killed by the origin: a
different port is a different origin, so no shared cookies, no `window.parent` (history
sync needs `postMessage` and cooperation), a port to allocate and firewall per plugin,
and the plugin is reachable from the network directly, bypassing whatever auth the
shell grows. Isolation used to be the counter-argument *for* this; now that we don't
want isolation, its last advantage is gone. Stays rejected, more firmly than before.

**Entry points alongside containers.** Two contracts, two docs, and every author's
first question is "which kind do I write?". The container contract is a superset -- a
Python plugin can be a container. If entry points survive it's as an explicitly
first-party, privileged mechanism, not a peer.

**A capability API (plugin POSTs us a `Command`, we run it).** Elegant -- it reuses the
`Command`/`str(Command)` seam design.md already built -- but its entire justification
was a security boundary. Without sandboxing it's a worse way for a plugin to run
`docker compose` than mounting the socket and running `docker compose`. Noted because
it's seductive and I want the reason it died to survive.

## Phasing

Shipped: the iframe + history sync spike, the HTTP + websocket proxy, label discovery,
`shell.js`. What's left, if any of the open questions above get resolved:

5. Port a bundled plugin, or write a throwaway Go one to prove the polyglot claim
   isn't theoretical. **This is the acceptance test for the whole idea** -- if writing a
   Go plugin isn't pleasant, none of the above was worth it.
