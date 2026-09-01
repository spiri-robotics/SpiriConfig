# Notes: drive-based provisioning

Scratch. Not documentation. Nothing here is built.

Answers the "First-run is a conundrum" item in [todo.md](todo.md): how do you apply
or reset a robot's state from a drive, with no network and possibly no working
login, the way the Pi picks up `userconf.txt`/`custom.toml` off the boot partition.
Reset passwords, push network config, load or update apps, certify a device
matches a declared state.

## Premise

Physical drive access is a trust *input*, not a thing we defend against.
[design.md](docs/design.md#a-plugin-can-also-be-a-container-and-is-not-sandboxed)
states this for plugins ("you are assumed to have root on machines you own"); the same premise
applies here one level removed: **whoever controls a trusted signing key is
authorized to fully reconfigure the device**, including deleting accounts and
running arbitrary scripts. Signing exists to raise the bar for a fielded,
compliance-sensitive robot above "anyone who can plug in a USB stick," not to
defend against a drive from someone we've already decided to trust.

Three tiers, chosen per deployment, not per drive:

| Tier | Behaviour | Who it's for |
| --- | --- | --- |
| `off` | Nothing is ever applied from a drive. Access loss means re-image. | current baseline; highest-security clients |
| `signed` | Only commits signed by a key in `trust.d/`, applied fast-forward-only, are accepted | fielded / compliance clients |
| `trusted` | Anything on the drive is applied, no signature required | dev kits only |

One setting, root-owned, not reachable from the web UI or from a drive itself —
same reasoning as `SPIRICONFIG_AUTH`: a network- or drive-reachable toggle for
"should this device trust unsigned drives" would itself be the privilege
escalation.

## Trigger: checked at boot, not on hot-plug

A drive is only ever read once per boot, by a systemd unit that runs early
(before or alongside network bring-up, doesn't need one) and looks for a known
label or marker file. No udev watch for runtime insertion. Two reasons this is
the better default, not just the simpler one: it narrows "when can a drive
change this device" to a window an operator already controls (they chose to
power-cycle it with a drive in), and it sidesteps the hot-plug edge cases
entirely — no "what if the drive is yanked mid-`rsync`", no watching for a
filesystem that might reappear under a different device node.

## Mechanism: a git remote, not a filesystem we read directly

The device keeps a persistent local clone per remote it's configured with
(`/var/lib/spiriconfig/provision/<remote-name>/repo`), and one piece of state
per remote that has to exist outside "no state of ours": `last-applied`, a
commit hash. (Why more than one remote — see "Two remotes," below.)

Applying one remote:

1. `git fetch <url> <ref>` into that remote's local clone —
   `file:///mnt/usb` for a drive, `https://`/`ssh://` for a network remote.
   We never trust a working tree directly, drive or network checkout alike —
   reading through git's object store means the history we're about to
   verify is exactly the history git says it is, not whatever a hand-edited
   FAT32 volume, or a compromised server, happens to contain.
2. Fast-forward check: `last-applied` must be an ancestor of the fetched head.
   Reject outright if not — this is the whole rollback/replay defense, and it's
   free. A captured, legitimately-signed drive from six months ago cannot be
   replayed once the device has moved past that commit, because it's no longer
   an ancestor of the new head.
3. (`signed` tier only) Walk the new commits **in order**. For each one:
   verify its signature against the trust set *as materialized after the
   previous commit* — not the incoming one. A commit touching `trust.d/`
   needs a signer in `trust.d/admins/` specifically; a commit touching
   anything else needs a signer anywhere in `trust.d/` (`admins/` or
   `signers/` — see "Two-tier trust," below). Then apply that commit's
   changes, including any `trust.d/` update, before checking the next. A
   commit can't grant itself the authority to exist.
4. Apply the longest valid prefix. A later commit failing signature verification
   doesn't discard the earlier ones that passed — they were legitimately signed,
   applying them is correct, and the operator gets told exactly which commit
   broke the chain and why.
5. Reconciliation happens against the **local, verified clone**, never the raw
   drive or a raw remote checkout.
6. Advance that remote's `last-applied` to the last commit actually applied.

**The transport in step 1 is not special.** Verification never asks how the
bytes arrived, only what the commit DAG and its signatures say — so a
networked device runs the identical logic against a USB drive or a git
server, and a compromised server gains nothing over a compromised drive:
neither can forge a signature, only replay or withhold history the ff-only
check and the signer walk already defend against. USB is the
zero-infrastructure default that still works with no network and no prior
configuration, not a constraint the rest of the mechanism assumes.

Consequences worth being explicit about:

- **Authoring gets real git remotes, not just an export step.** A local repo
  an operator edits can have an `origin` like any other repo — `git
  push`/`git pull`, ahead/behind counts, the works. "Write to a USB drive" is
  one sync target among several, not the only way a commit leaves the
  operator's machine.
- **A networked device's trigger is a separate question from the boot-only
  one above**, and isn't answered by it. "Checked once at boot" was reasoned
  specifically around USB hot-plug risk (see Trigger, above) — a device that
  polls a remote periodically, or accepts a push notification to fetch, is a
  standing network listener with a different threat model (continuously
  reachable vs. a window an operator controls by choosing when to power-cycle
  with a drive in). Needs its own decision, not inherited from USB's.
- **Multiple transports coexist for free.** A device that takes both a USB
  drive at boot and a periodic network pull needs no coordination between
  them: whichever delivers a newer, valid, fast-forward commit chain first
  just becomes the new `last-applied` for that remote. The ff-only rule is
  transport-agnostic by construction, so this was already true before it was
  needed.

## Two remotes: baseline and patch, composed in order

One repo mixing `add`/`state` looked convenient and wasn't. A compliance
certification and a Tuesday-afternoon password reset ended up in the same
history — "when was this device last certified" became a needle in a
haystack of routine resets. Worse, it was never actually answered whether an
`add` entry survived a later `state` reconciliation. Splitting into two
remotes fixes both, and neither the fetch/verify/reconcile mechanism above
nor the on-device apply logic needed to change to allow it — nothing in it
assumed exactly one remote.

**`baseline`** — `state/` only, nothing else. Rare, deliberate, usually a
different role's signing key than day-to-day ops. Its history reads as a
clean certification log: "certified against baseline vN on this date,"
nothing else mixed in.

**`patch`** — `add/` and `remove/` only (see "The add/remove/state contract,"
below), no `state/` ever. High frequency, ops-owned. Because it can never
declare an exact set, it can never prune something by omission — the worst
it can do is exactly what it explicitly says it does.

Each is an ordinary git remote with its own `last-applied`, applied through
the identical mechanism above — there is no merged history and no shared DAG
between them. A `trusted`-tier dev-kit has no reason to bother with the
split at all; one ad-hoc repo doing `add`/`remove`/`scripts` is simplest and
the baseline concept buys it nothing. The split earns its cost specifically
where compliance certification is a real, recurring event.

**Composition: baseline reconciles first, then patch reconciles on top,
unlocked.** Deliberately chosen over the alternative — a baseline "locking"
the resource types it governs against later patches. The people this is
built for are already trusted with root; refusing their own patches on top
of a baseline they can see and reason about would be a paternalism nothing
else in this project practices. The real consequence, stated plainly because
it's easy to miss: **a "certified exact state" is a floor at the moment
baseline was applied, not a continuously enforced invariant** — a patch
landing five minutes later can and will diverge from it, on purpose.
Re-applying or re-checking baseline is how you find out whether it still
holds; nothing enforces it standing.

## Two-tier trust: admins and signers

Motivating scenario: testing a third party's baseline repo — a client's
compliance team, a vendor — shouldn't hand them the power to revoke your own
device access. Under a flat `trust.d/`, any trusted key could touch
`trust.d/`, including removing another key, because "trusted to sign
content" and "trusted to decide who else is trusted" were the same bit.

Splitting them costs one more directory level: `trust.d/admins/` and
`trust.d/signers/`, both `add/`+`remove/` (no `state/` — see below). A key in
`signers/` can sign ordinary content commits (`users/`, `apps/`, `network/`,
and a baseline's `state/`) but a commit touching `trust.d/` that it signs is
rejected outright — verified the same parent-commit-materialization way as
any other `trust.d/` change (step 3, above), just checked against
`trust.d/admins/` specifically rather than all of `trust.d/`. Give a third
party's key `signers/` only, and it is *structurally* incapable of touching
`trust.d/`, not merely discouraged the way a hidden UI button would be — it
cannot revoke anything there no matter what its own signed commits ask for.

`trust.d/`'s old `state/` (an exact trusted set, the original revocation
mechanism from an earlier pass) is retired now that `remove/` exists as a
general primitive: revoking one compromised key is
`trust.d/admins/remove/<fingerprint>`, the same scoped, auditable,
doesn't-require-re-declaring-everyone-else shape as removing anything else.
"Replace the entire trust root" is rare and severe enough to deserve its own
re-commissioning flow if it's ever built, not a directory here.

`trust.d/` isn't tied to either remote's content-governance style — it can
appear in a `patch` remote or a `baseline` remote alike, governed by key
capability (admin vs. signer) rather than by which remote carries it. Where
two remotes both touch `trust.d/` in the same reconciliation pass, `baseline`
is verified and applied first, then `patch` — the same order as content,
kept consistent rather than inventing a second rule just for trust.

## Manual override: applying a repo that isn't in `trust.d/`

Resolves the break-glass question above outright, rather than leaving
re-imaging as the only way back. Someone with **local** access to a device —
console, SSH, the web UI while logged into that device directly — can apply
any repo, drive or remote, regardless of whether anything in it is signed by
a key the device trusts. Never automatic: a `signed`-tier device does not
start trusting a random drive at boot because of this. It's a deliberate,
person-in-the-loop action, gated the same way this project has always gated
things — by whether you're already logged into the box, not by a permission
bit. Someone with console access already has root; this gives them a safe,
audited way to use it instead of hand-editing files.

Two distinct flows, not one, because they answer different situations:

- **Web UI, browsing a remote.** The common case, and not really a
  "recovery" at all — a device with ordinary network access points at a
  remote it doesn't trust (a vendor's public config repo, say
  `spiri-default-configs`) and browses what it offers: branches, tags,
  whatever the remote's own refs are, each showing that remote's own
  self-declared label (`Spiri-mu v1.8.7`) rather than a raw commit hash,
  since a stranger's repo has no name in *this* device's `trust.d/` to
  borrow. Clicking one applies it, after confirmation.
- **USB key, for when the web UI isn't the point.** The actual break-glass
  case — a device that's locked out or unreachable over the network. Still
  requires *some* local access to trigger — a console session, SSH, a
  logged-in local web UI — CLI-first, matching how everything else here
  phases (see Phasing).

**Neither flow requires bypassing "no local access at all," and that's
correct, not a gap.** It was tempting to make the USB flow work off drive
insertion alone, with no prior access needed — that's how the original
`userconf.txt`-style boot-partition trick works, and it's exactly what
`trusted` tier already allows. But allowing it unconditionally on a
`signed`-tier device would mean the tier defends nothing: its entire stated
purpose is raising the bar *above* "anyone who can plug in a USB stick," and
a recovery path reachable by insertion alone, regardless of tier, quietly
turns every signed-tier robot back into a trusted-tier one for anyone
holding a drive. So: **if a `signed`-tier device is locked out with no
reachable `trust.d/` key and no local access of any kind (no console, no
SSH, nobody ever logged into its web UI), the correct outcome is that there
is no path back except re-imaging.** That is `off`-tier's fallback becoming
true for a `signed`-tier device that has locked itself out, and it is
supposed to be true — decided on direct instruction, not left as an
oversight. A lower bar than that would need a deliberate, out-of-band
physical signal distinct from mere drive presence (a recovery button held
for some seconds, a boot-mode jumper, something a stray drive insertion
can't trigger by itself) — genuinely possible on hardware that has such a
control, but nothing here assumes one exists, and none is designed.

Mechanically, both flows bypass the same two checks, and for the same reason:

- **No signature check.** The point is it might not be signed by anything
  this device trusts.
- **No fast-forward check either.** An unmanaged repo shares no ancestry
  with either remote's `last-applied` — "is it an ancestor" doesn't apply —
  so this is a **replace**, not a reconcile-on-top. It updates neither
  remote's `last-applied`; it isn't either of them.
- Whatever the repo declares is applied as the new floor for the resource
  types it touches, the same shape as a `baseline` apply, regardless of
  whether the repo's own tree is internally structured with `add`/`remove`/
  `state` or something else entirely — this path doesn't care.

**The confirmation is informative, not alarming.** Plain language, not a red
full-page danger screen: this is a sanctioned, sometimes routine action (a
technician resetting a robot to a known vendor default), and treating it as
an emergency every time trains people to click through without reading.
Naming what's true is the whole job — which repo, its own name and version,
and the one sentence that matters: *"this repository is not managed by your
organization."* A quick before/after of what actually changes (reusing the
same plain-language diff Review & Sign already renders) does more real work
than a warning icon would.

## Layout

**Superseded as the authoritative reference by `docs/provision-bundle-spec.md`**,
which tracks the real, current shape (including fields added since this was
written, like `users/`'s `authorized_keys`) and stays in sync with the code.
The tree below is kept for the rationale around it, not as the source of
truth for what's actually on disk.

Two small trees, one per remote — `trust.d/` can appear in either (see
"Two-tier trust," above); everything else is exclusive to one:

```
patch remote — add/ and remove/ only
/
├── trust.d/
│   ├── admins/{add,remove}/*.pub    ssh-signers format
│   └── signers/{add,remove}/*.pub
├── users/
│   ├── add/<name>/
│   │   ├── password.hash            crypt(3) string (yescrypt/bcrypt) -- never plaintext
│   │   ├── groups                   optional, newline-delimited
│   │   └── shell                    optional
│   └── remove/<name>                empty marker file; presence = "delete this account"
├── network/
│   ├── add/fallback-ap.nmconnection NetworkManager keyfile, dropped in verbatim
│   └── remove/old-guest-wifi
├── apps/
│   ├── stores.toml                  app stores to register: [{url, ref}]
│   ├── add/whoami/env               optional -- an app's declared settings, see below
│   ├── remove/old-dashboard
│   └── images/myapp/
│       ├── source.toml              {from: "docker://registry/myapp:1.2"} -- a skopeo copy
│       └── myapp.tar                optional pre-fetched `skopeo copy ... docker-archive:`
└── scripts/                         trusted-tier only. no add/remove/state -- imperative
    ├── 00-preflight.sh
    └── 10-site-specific.sh

baseline remote — state/ only
/
├── trust.d/
│   ├── admins/{add,remove}/*.pub
│   └── signers/{add,remove}/*.pub
├── users/state/<name>/{password.hash,groups,shell}   exact set of accounts
├── network/state/shop-floor.nmconnection              exact set of connection profiles
└── apps/state/nextcloud/env                           exact set of installed apps
```

## The add/remove/state contract

Three verbs, not two — `remove/` was missing for a while and its absence was
the actual bug, not a detail. Concretely, from real field stories: an
employee leaves, and their account needs to come off every warehouse robot.
That's not `add` (which never removes anything), and forcing it through
`state` means re-declaring *every account that should still exist* on every
affected robot, or whichever one gets forgotten is silently deleted next time
the repo is applied. Same failure mode one level up: revoking one compromised
signing key used to mean re-declaring the entire trusted-key list. `remove/`
is the fix in both places — a scoped, single-purpose, auditable deletion that
doesn't require reconstructing everything around it.

- **`add/`** ensures its entries exist and are current. Touches nothing else.
  Lives only in a `patch` remote.
- **`remove/`** ensures its entries are gone. Touches nothing else — the
  opposite polarity of `add/`, not a weaker version of `state/`. Also lives
  only in a `patch` remote, and requires the `signed` tier: it deletes, so
  every deletion traces back to a key, same as `state/`.
- **`state/`** makes the resource type's whole managed set match exactly
  what's declared, removing anything that isn't. Lives only in a `baseline`
  remote, requires `signed`, and is deliberately the heaviest of the three —
  a compliance certification is *supposed* to force enumerating everything,
  not offer a shortcut around it.

One interface method behind the contract, `provision_apply` — see
`docs/provisioning.md`, which is now the authoritative spec for this, not
here. Given a checked-out remote, a plugin returns the `Command`s that
reconcile the live system to what its resource type's directory declares,
covering `add`/`remove` or `state` alike depending on what's actually on
disk. Two shapes that method's *body* ends up taking, worth keeping in mind
as implementation guidance even though the interface above it doesn't care
which:

- **File-shaped types** (`trust.d/`, `network/`) map directly onto a real
  destination directory of drop-in files, so the whole method is one
  `Command`: `rsync -a` for `add`, `rsync -a --delete` for `state`, `rm` of
  the named files for `remove`.
- **Everything else** (`users/`, `apps/`) doesn't have a flat-file
  destination to point rsync at — accounts live in `/etc/passwd`, installs
  are symlinks that depend on a store checkout existing first — so the
  method computes its own diff (list the current set, decide what to add and
  what to drop) and returns one `Command` per entry: `useradd`/`chpasswd -e`
  or `userdel`, clone-store-then-`ln -s` or `rm` the symlink.

`state/`'s delete diff, and `remove/`'s target, are filename-based, matching
this repo's existing "nothing of ours is authoritative, the filesystem is"
stance: a renamed key or connection profile is a delete-then-recreate, not a
rename, because the literal name on disk *is* the declaration.

## Resource type notes

- **`trust.d/`**: first-key-wins, decided, and unaffected by the admin/signer
  split — whoever places the first key in an entirely empty `trust.d/`
  (admins and signers both) becomes the device's root of trust, same as
  before, with no local-confirmation gate on that transition. The
  pending-directory staging idea from an earlier pass (see Rejected) is
  explicitly not being built — simplicity over defense-in-depth here. This
  makes the open-to-locked window the one moment worth calling out in a
  deployment runbook: do it somewhere physical access is actually controlled,
  not in general transit or storage. See "Two-tier trust," above, for the
  admins/signers split itself.
- **`users/`**: ~~`spiriconfig_users.set_password` currently pipes
  *plaintext* to `chpasswd`. Applying a pre-hashed value needs a
  `chpasswd -e` path~~ **built** — `users.set_password_hashed` (`chpasswd
  -e`) and `users.hash_password` (`mkpasswd --method yescrypt`, so a
  staged password never touches `add/<name>/password.hash` as plaintext)
  now sit alongside the live, plaintext `set_password`. See
  `docs/provisioning.md`'s worked example.
- **`state/`'s "managed set" is scoped per resource type, and the scope isn't
  symmetric.** For `users/`, "managed" already means exactly what
  `spiriconfig_users.User.is_login()` means today — accounts inside the
  configured `[uid_min, uid_max]` band with a real login shell, the same
  filter the Users page already applies to decide what counts as a "user" at
  all. `root`, daemons, `nobody`, and service accounts like NixOS's
  `nixbld*` sit outside that band or behind a `nologin` shell, so they're
  structurally invisible to `users/state/` — a baseline can never delete
  them, declared or not, with no new carve-out of ours needed. For `apps/`,
  there's no equivalent protected subset: `spiriconfig_appstore.installed()`
  already excludes anything that isn't a symlink into a configured store (a
  directory someone made by hand isn't "an app" at all), but everything that
  *is* one of those symlinks is fully in scope. So `apps/state/` really is a
  full overwrite of everything installed, while `users/state/` is a full
  overwrite only within the login-account band — same contract, a
  different-sized "everything" per type, because what counts as "one of
  these" already differs before provisioning enters the picture, and
  provisioning doesn't get to redefine it.
- **`apps/images/`**: two flavours behind one shape. `source.toml` alone means
  "fetch at apply time" (needs network, small drive). A bundled `.tar` means
  "fully offline" (no network needed, bigger drive). Both are just a
  `skopeo copy` a human could type, with a different source and destination.
- **`apps/add/<name>/env` and `apps/state/<name>/env`**: an app's settings,
  reusing `spiriconfig_docker.settings`/`widgets` wholesale rather than
  inventing a provisioning-specific form. An app already declares its knobs
  as `x-spiri-settings` (in the compose file or a `spiri-settings.yaml`
  sidecar) and gets them rendered by the same `Field`/`WIDGETS`/`form()`
  machinery the Docker page's own Settings dialog already uses. The only
  thing that changes between "live" and "staged" is the write target: instead
  of the running stack's `.env`, the exact same rendered bytes land in the
  repo at `apps/<mode>/<name>/env`, applied on the device alongside the
  compose symlink. No new form code, no second schema — just a different
  destination for `StackSettings.preview()`'s output.
- **The staging UI is a list, not a one-at-a-time form** — first draft got
  this wrong for both `users/` and `apps/`. A single "type a name, click
  stage" form only ever serves `add`, and now that `remove/` exists too, a
  list with a per-row add/remove control is the natural shape regardless:
  apps get this closest to free (the app store already has a full catalog to
  list — borrow the App Store page's own card shape, swap Install/Uninstall
  for add/remove), users don't have an equivalent catalog so their list is
  the accumulating set of entries staged this session instead. **This list
  is `patch`-remote only.** Editing a `baseline`'s `state/` is a genuinely
  different, rarer surface — reached deliberately, not a mode toggle sitting
  next to add/remove in the everyday list. Conflating the two in one control
  was part of what felt off about the first pass.
- **Baseline editing is the same list-builder as `add/` — but `state/` itself
  is not relative to anything.** First mockup pass drew it as a checkbox
  grid, every currently-certified entry pre-checked, uncheck one to mark it
  removed — which quietly implies the declaration is a diff against its own
  past. It isn't. `state/` is an absolute list, evaluated fresh, on whichever
  device applies it, against *that device's* actual current contents at
  apply time — the same way `rsync -a --delete` never consults history, only
  what's declared versus what's literally there right now. (Contrast
  `add/`/`remove/`, which are themselves relative verbs — "ensure present,"
  "ensure absent" — but still evaluated against a device's real state, never
  against a remembered prior commit; nothing in this design ever diffs
  against "last applied" except the fast-forward check itself, which guards
  replay, not content.) So the editor loading the current `state/` files to
  show them is no different from opening any text file to edit it —
  convenience, not something the declaration's meaning depends on. It's
  still the same add-a-row / remove-a-row list component the `patch`
  remote's Add card already uses, with no Add/Remove toggle (there's only
  one polarity — being in the list) and writing to `state/<name>/…` on the
  `baseline` remote instead of `add/<name>/…` on `patch`. What Review & Sign
  can honestly show is a diff of the *declaration* against its own previous
  git revision — useful context for whoever's signing it — not a promise
  about what any specific device will do: patched devices may have already
  drifted from the last baseline in ways this authoring surface, possibly
  running on a laptop with no device in reach at all, has no way to see.
- **"Certify baseline" must not read as "certify this device."** The plugin
  runs standalone on an operator's laptop with no robot attached at all (see
  "Runs standalone for free" under Authoring) — there may be no local device
  to read a state from even if that were desirable. A baseline editor that
  looks like it's introspecting `/etc/passwd` or `docker ps` on the machine
  SpiriConfig happens to be running on is wrong regardless of convenience:
  it edits a repo — a declaration for whichever *target* profile is open —
  exactly like every other surface here, never the host it's running on.
- **`scripts/`**: `trusted`-tier-only — the reverse of what you'd guess from
  "biggest blast radius gets the strongest tier." Deliberate: `signed` exists
  to give a fielded/compliance device an auditable, declarative action set
  where every change traces to a resource type with defined semantics.
  Arbitrary code doesn't fit that story no matter whose key signed it, so it's
  excluded from `signed` entirely rather than merely gated by it, and stays a
  dev-kit convenience where "anything on the drive runs" was already true.
  Ordering is filename-prefix (`00-`, `10-`), no manifest needed. Idempotency
  falls out of the ff-only rule for free: re-fetching the same commit is
  already excluded, so a script only ever runs once per commit that introduces
  or changes it.

## Authoring: where the editing UI lives

Not a standalone app. First pass at this designed one — a separate
out-of-process plugin an operator would run alone on a laptop to build and
sign drives — and it broke on contact with its own mockup: it needed a
"Network" screen, and there is no network plugin in this codebase to back one.
Building a UI whose scope is "everything the drive format could theoretically
hold" rather than "everything SpiriConfig can actually do" is exactly the
mistake `design.md`'s core rule exists to catch.

So instead:

- **Its own plugin, for the parts no *other* plugin owns.** `trust.d/` isn't a
  system resource a plugin manages, it's a property of the tool itself —
  closer to Advanced mode than to `users/` or `apps/`. But "cross-cutting" does
  not mean "belongs in core `spiriconfig/`": `spiriconfig_system` is already
  exactly this shape (host info nothing else owns) and is an ordinary bundled
  plugin, sidebar entry and all, not special-cased into `web.py`. So
  `trust.d/`, the review → commit-and-sign → export-to-drive spine, and the
  commit history log are `spiriconfig_provision` — a first-party plugin
  package sibling to `spiriconfig_system`, not a change to the shell.
- **Staging UI is contributed, not hosted per-plugin.** First mockup put a
  "stage for a provisioning drive" card on the Users and App Store pages
  themselves, each behind `advanced.only()`. Wrong call once there was an
  actual screen to look at: a drive combining a password reset, a wifi
  profile, and an app install meant visiting three plugin pages and mentally
  tracking what had been staged where. Instead each plugin exposes an
  optional render hook — the same shape as `Plugin.on_startup()`, a no-op
  default that costs nothing for a plugin that skips it — and
  `spiriconfig_provision`'s page calls it, so `spiriconfig_users` and
  `spiriconfig_appstore`'s staging forms render *on the Provisioning page*,
  attributed by icon and name, with a link back to the owning plugin's real
  page. The plugin still owns the block's contents and what it writes; only
  where it renders moved. **The exact shape of that hook — the patch/baseline
  split, how a plugin describes a changed path for Review & Sign and the
  commit message, how it applies a repo back to the live system, why it's a
  separate contract rather than new `Plugin` methods — is specified in
  `docs/provisioning.md`, not repeated here.** There is deliberately no
  network staging block
  yet, because there is no network plugin yet — the authoring UI's scope is
  bounded by what SpiriConfig can already do, the same discipline the rest of
  the app already holds itself to.
- **The write path stays decoupled — only the render call is shared.** A
  staging block still just writes a file into the repo
  (`users/add/<name>/password.hash`), the same as it would anywhere else;
  `spiriconfig_provision` never sees the account name or the password, only
  the rendered block and, separately, whatever `git status` reports as
  pending — the same way the appstore page reads symlinks and the users page
  reads `/etc/passwd`. The one real coupling is the render hook's existence,
  not its content, and a network plugin arriving later needs to *implement*
  it, not for `spiriconfig_provision` to be taught anything new.
- **Runs standalone for free.** The "operator on a laptop with no robot"
  scenario that motivated a separate app in the first place doesn't need one:
  `Scope` already models a per-user, non-root SpiriConfig install, so `uv run
  spiriconfig serve` on a bare laptop already gives an operator the whole
  thing, robot or not.
- **The repo format doesn't wait on any of this.** `add/`/`remove/`/`state/`
  directories of files and signed commits are the actual interface — a
  network plugin arriving later only has to add the staging *convenience*.
  Someone can hand-write an `.nmconnection` file into `network/add/` today
  with a text editor and `git commit -S`, exactly the same "you must be able
  to do it without us" property everything else in this codebase has. So the
  on-device phasing (trust.d + network via rsync, phase 2) is not blocked on
  any authoring UI existing at all.
- **The Provisioning page manages two independent namespaces of profiles,
  patch and baseline. Built**, minus the status-pill polish. An operator
  working more than one fleet/profile (`shop-floor-fleet`, `warehouse-b`,
  `dev-kits`) needs to see which ones are stale at a glance, not open
  each to find out — and a patch profile and a baseline profile are
  unrelated data even when they share a name (`Profile` in
  `spiriconfig_provision/repo.py`, keyed by `(name, remote)`, not one
  `RepoProfile` bundling both — that shape was tried and split back
  apart, see :doc:`/provisioning`'s "Profiles" section for why. **But
  see "Mockup: the actual designed screens" below — the actual design
  mockup shows the bundled shape, and this conflict is unresolved.**)
  Each
  remote gets its own picker (`_profile_section` in
  `spiriconfig_provision/web.py`) showing that profile's sync status
  against `origin` — ahead, behind, up to date, or no origin — the same
  status `Export`/`Pull` act on. Read off git (`git rev-list
  --left-right --count`), not tracked separately — same "no state of
  ours" stance as everything else. Not yet built: a status *badge per
  profile in each picker's closed state*, so a fleet with several
  profiles shows which ones need attention without opening the dropdown
  at all — right now that only shows once a profile is selected.
- **Export has two destinations behind one action, not two features.
  Built.** A drive and a `git push` to `origin` are the same operation —
  send HEAD somewhere — with different transports, so `Export` is one
  dialog with a destination choice (`spiriconfig_provision.sync.export_to`),
  not a drive-only button plus an unrelated push button bolted on
  elsewhere. `Pull` stays separate (it brings changes *in*, a
  different direction entirely), surfaced next to the repo picker since it's
  frequent and low-stakes enough not to need a dialog of its own.

## Mockup: the actual designed screens

There is a real design mockup for this page — a Claude Design artifact
(`https://claude.ai/code/artifact/cb2e89e7-bef1-4664-9664-675450145aae`,
titled "Provisioning UX", eight anchored sections: `s-main`, `s-users`,
`s-apps`, `s-settings`, `s-baseline`, `s-unmanaged`, `s-review`,
`s-export`). It had already been shared once earlier in this project and
was asked for again three times before its content made it into this
file — write down what it actually shows here, not just in chat, so that
does not happen a fourth time. It is a *static* mockup (its own footnote:
"buttons, dropdowns and toggles are not wired up"), styled from
SpiriConfig's real Quasar/NiceGUI theme, not a new visual identity.

**This directly contradicts a build already shipped in this repo** on at
least one load-bearing point — see "Profiles, bundled vs. split" below.
Resolve that before trusting anything here as an implementation spec
outright; the concrete details are otherwise faithful to what the mockup
shows.

### Profiles, bundled vs. split — resolved, kept split

**Decided: kept the split** (independent `patch`/`baseline` namespaces,
two pickers/tabs) over reverting to the mockup's bundled shape. The
mockup is superseded on this one point; everything else in this section
still describes the real design to build toward.

### Profiles, bundled vs. split — the open conflict (background, resolved above)

The mockup's "Local repo" picker (`s-main`) shows **one profile with both
a patch remote and a baseline remote bundled together**: a dropdown lists
whole profiles (`shop-floor-fleet`, `warehouse-b`, `dev-kits`), and
`shop-floor-fleet`'s entry shows *both* `patch: 2 ahead` and `baseline: up
to date` under the one name; `dev-kits` is annotated "no baseline — one
ad-hoc remote", i.e. a profile can have just one remote, but patch and
baseline that *do* both exist are still one profile, one entry in the
list, one Tier badge, sync-status shown side by side under it.

That is the *first* profile shape this project tried (`RepoProfile` with
an optional `patch`/`baseline` pair) — and this session later split it
into two fully independent namespaces instead (`Profile` keyed by
`(name, remote)`, two separate pickers, now two tabs — see "Authoring:
where the editing UI lives" above and :doc:`/provisioning`'s "Profiles"
section). That split was done on explicit instruction *without* this
mockup in view. Whether to revert to the bundled shape to match the
mockup, or keep the split and treat the mockup as superseded on this one
point, is unresolved — ask before rebuilding either the profile picker or
the two-tab layout again.

### Page shape: one long scroll with anchors, not tabs

`s-main` is one continuous page, sections stacked top to bottom, no tab
control anywhere: repo picker → "Stage a change — patch remote" (Users
card, App Store card) → "Certify baseline" (a link-out card, not the
editor inline) → one "N uncommitted changes / Review & sign" card →
"Trust & signing" (`advanced`-only) → "Recent signed commits" → "Apply an
unmanaged repo". `Export` is a single button in the page header, not
per-section. The baseline editor (`s-baseline`) is a genuinely separate
screen reached via "Open baseline editor →", matching what's already
built; nothing else here is tabbed.

### Repo picker (`s-main`)

Card: repo name + chevron, a vertical divider, a `Tier` badge (`Signed`,
purple). Below a divider, a two-column grid: **Patch · day-to-day**
(short hash, an `N ahead`/`N behind` badge, a `Pull` button) and
**Baseline · compliance** (hash or `up to date`, "certified N days ago",
its own `Pull` button). Clicking the name opens a dropdown: one row per
profile, each showing both remotes' status (badges/text, not just one
line), plus a `+ New repo` action at the bottom. Caption below the card:
"Ordinary git, two remotes — anyone with access can `git clone` either
and work without this page. Export, above, sends either one to a drive or
straight to its own remote."

### Users staging card (`s-main`, mirrored full-size at `s-users`)

Segmented pill toggle **Add / Remove** (not a dense `ui.toggle`, visually
a two-tab pill with the active side white-on-shadow). Below it a
3-column grid: *Account name* field, *New password* field with an inline
**Generate** link-button on its right edge, and an **Add** button.
Helper line: "Hashed locally with yescrypt · writes into users/add,
ensures present, never removes anyone." Then a divider and **Staged this
session**: each entry is `+`/`−` sign, `name — users/add` or `.../remove`
text, a dismiss (×) icon on the right.

**Deviates from the mock on the toggle, deliberately, to match App Store's
own already-recorded reversal (see its staging card section below).**
`render_patch_block` first built this exactly as mocked — a pill toggle
switching an Add/Remove mode plus one shared "Stage" button — and that's
the same shape the App Store card's own first pass used before direct
feedback called it out as an unlabelled pair of tabs rather than two
distinct actions. Once the App Store card moved to a plain Add button and
a plain Remove button per row, leaving the Users card on a toggle was the
one inconsistency left on the page — same interaction, argued about once,
answered twice differently for no reason tied to `users/` itself. Now
built the same way: a plain **Add** button and a plain **Remove** button,
name and password fields shared between them (Remove ignores the
password), no toggle. The account name and password fields, and the
**Staged this session** list with its `+`/`−` rows, are otherwise as
mocked; rows are now tinted (primary for `add`, negative for `remove`),
the same `color-mix`-over-`--q-primary`/`--q-negative` treatment
`_app_row` in `spiriconfig_appstore/provision.py` uses, for the same
"reads correctly in both light and dark" reason. The inline **Generate**
password action from the mock is still not implemented.

### App Store staging card (`s-main`, mirrored full-size at `s-apps`)

**Not** a name-entry form — a bordered list of *every app in every
configured store* (not just staged ones), one row each: app name, then
`store · version` in faint text, then a right-aligned status control that
is one of:
- `Not staged` — outline pill with a chevron (default state)
- `Add` — filled primary pill (row background tinted `primary-bg-tint`)
- `Remove` — filled negative/red pill (row background tinted
  `negative-tint`)

An app that declares `x-spiri-settings` also gets a small gear icon to
the left of its status pill, linking to the settings dialog (`s-settings`,
below) — shown for `nextcloud` in the mock, not for `whoami`/`traefik`.
Caption under the list: "Every app in a configured store — mark each one
add or remove. A settings form an app declares for itself works here too,
unchanged." Footer: "Add — ensures installed, writes apps/add. Remove —
ensures uninstalled, writes apps/remove. Neither touches any app not
named here." **Built**, matching this shape: `render_patch_block` in
`spiriconfig_appstore/provision.py` lists every app across configured
stores, and a gear icon (shown once a row is staged `Add` and the app
declares settings) opens the settings dialog below. One simplification
from the mock, since superseded by a second one: the status control was
first a single cycling button (Not staged → Add → Remove → Not staged),
one fewer click target than the mock's two-way-choice pill, but direct
feedback was that a click landing on the already-active state read as
doing nothing rather than as "already done." It's now a plain **Add**
button and a plain **Remove** button per row, each relabelled to a
past-tense state (`Added`/`Removed`) once it's the one in effect; a click
on the already-true one opens `_already_staged_dialog` instead of
silently no-opping, offering the one thing actually left to do —
unstage. See `_app_row`'s and `_already_staged_dialog`'s own docstrings.
`spiriconfig_users/provision.py`'s own patch card now follows the same
plain-Add/plain-Remove-button reasoning (see the Users staging card
section above), though without the dialog: unlike an app row, a `users/`
Add always does something new even when restaged (rewrites the password
hash), so there is no "already-true, nothing left to do" state to guard
against there.

### App settings dialog (`s-settings`)

Title `<name> — settings`. Ordinary fields in the *same widget set the
Docker Settings page already renders* for `x-spiri-settings` (masked
password field, plain text field, number field, a toggle switch for a
boolean) — no new form-building code, per the mockup's own eyebrow note:
"identical fields, identical widgets, only the write target changed:
instead of the live stack's `.env`, this writes `apps/state/nextcloud/env`
in the repo." Below the form, a collapsible "advanced" block: the target
path shown as a small chip (`apps/state/nextcloud/env`) with a chevron,
expanding to the literal rendered env-file bytes in a dark code block
(masked password value shown as bullets), captioned "The bytes that will
be written — edit them directly and yours are used instead, exactly as in
the Docker page's own settings editor." Buttons: `Cancel` / `Save to
repo`. **Built, minus the raw-bytes advanced editor.** The form itself
(`_settings_dialog` in `spiriconfig_appstore/provision.py`) reuses
`spiriconfig_docker.settings.declared`/`check_values` and
`spiriconfig_docker.widgets.form`/`values` directly -- no new
form-building code, exactly as designed -- and writes into
`apps/<add|state>/<name>/env` via `spiriconfig_docker.env.patch`, the
same patch-in-place logic `StackSettings.save` uses, minus the live
`docker compose config` validation roundtrip (this repo may be authored
with no device, and no docker, in reach at all). Not built: the
collapsible raw-env-bytes advanced panel the live Docker settings dialog
has; editing is form-only for now.

### Baseline editor (`s-baseline`)

Title "Declare the exact set", subtitle `<profile> · last certified N
days ago · applied before patch, which layers on top afterward` (a
reminder that baseline applies before patch in the compose order — see
"Two remotes" above). Two cards, same list-builder shape as the patch
side's Users card but one polarity (no Add/Remove toggle, just a bare
list + `+ Add an account to this set` / `+ Add an app to this set`
link, dismiss × per row): **Accounts — exact set** (caption: scoped to
login accounts only, root/daemons never touched) and **Apps — exact set**
(each entry shows `name · store`; caption: no protected subset, this
really is everything). Bottom-right: `Review & sign this baseline →`.
Matches what's already built reasonably closely, modulo the app list
being new.

### Trust & signing (`s-main`, `advanced`-only card — this is `trust.d/`)

Not built at all yet, but fully designed: purple-outlined card, header
"Trust & signing" + `Add signer` button, caption "Who can sign a commit
this device will accept. Determines who controls it — treat changes here
with more care than anything else on this page." A bordered list of
current signers, each row: green checkmark, name (`ops-laptop`), key type
+ fingerprint in mono (`ed25519 SHA256:kR9x…3Lm2`), "added N ago", a red
flat `Revoke` button. Below the list, a highlighted callout: "**First key
wins.** An empty trust list lets the next signer added become this
device's permanent root of trust — there is no separate confirmation
step. This device already has keys, so further changes only take effect
through a commit one of the keys above signs." This is the operator-facing
half of NOTES' phasing step 2 (`trust.d/` admins/signers add+remove) and
lives on the *same* Provisioning page as everything else, not a separate
screen.

### Recent signed commits (`s-main`, below Trust & signing)

Not built yet. A plain card list: hash (mono), commit subject, signer
name, relative time — one row per recent signed commit. A commit that
touched `trust.d/` is visually distinguished (hash in purple, and a
purple `trust.d change` label in place of the usual columns) rather than
looking like an ordinary content change. This is the "commit history log"
NOTES already lists as not-yet-built.

### Apply an unmanaged repo (`s-main` card + `s-unmanaged` dialog)

Not built yet — this is the "manual override" flow, decided in "Manual
override" above but not designed until this mockup. Lives inline on the
main page, not a separate screen: caption "Browse a remote this device
doesn't trust — a vendor's public defaults, a colleague's test config —
and apply one of its branches directly. Requires being logged in here;
nothing about this happens on its own." A `Remote` field (implicitly a
picker, showing name + URL). Below it, a bordered list of that remote's
branches — name/version, `stable`/`beta` + "updated N ago", a
"not managed by your org" chip, chevron. Clicking one opens a
confirmation dialog (`s-unmanaged`): title `Reset apps to <version>?`, an
*informational* callout (blue/primary tint, explicitly **not** a red
danger screen — the mockup's own eyebrow: "This is a sanctioned,
sometimes-routine action; treating it as an emergency every time trains
people to stop reading it") explaining what "not managed" means and that
applying replaces the device's apps with exactly what's declared
regardless of current state; a "What changes" +/- list (green adds,
red-tinted removes); a copy-pasteable command block showing the literal
`git fetch <url> <branch> && git reset --hard FETCH_HEAD` line; buttons
`Cancel` / `Reset apps`.

### Review & Sign (`s-review`)

Title "Review changes", subtitle `<profile> · N files changed`. When a
removal is part of the set, a prominent red/negative-tint banner explains
*why*, concretely — not just "this removes something" but e.g. "users/state
is an exact set — alice is not listed, so any device that currently has
that account loses it the moment it applies this baseline. A signed
commit is the only thing preventing this from being replayed or undone by
accident." Changes are grouped by resource type (`trust.d`, `users`,
`apps`, each its own labeled block), not one flat list — each entry is a
`+`/`−` row, description, and the path suffix (`— users/add`); a removal
row gets the same red tint as the banner. The commit message field shows
*two* pieces of text, not one flat editable string: the auto-suggested
phrase in muted gray, an em-dash, then a free-text "why" in normal color
(e.g. "new shift lead starts Monday, replacing alice") — with a `↺ Reset
to suggested` action to discard the "why" and go back to just the
suggestion. A `Sign with` field names the key and says where it lives
("ops-laptop · ed25519, loaded in ssh-agent"), captioned "The private key
never leaves your agent — Provisioning only asks it to sign." A
copy-pasteable command block shows the literal command that would run —
`git commit -S --gpg-sign=ssh -m "<message>"` — matching the streaming
copy-pasteable-command treatment every other action-running dialog in
SpiriConfig already uses, and explicitly deferred for this dialog when it
was first built (plain `notify()` instead). Buttons: `Cancel` /
`Commit & sign`.

### Export (`s-export`)

Title "Export", subtitle `<profile> · HEAD <hash>`. A segmented toggle —
**USB drive** / **Central location** (not "URL" — friendlier wording for
the same git-push destination). A `Remote` field (name + URL, a picker),
captioned with live status ("origin is 2 commits behind HEAD — pushing
will fast-forward it. Anyone who clones or pulls origin picks this up, no
drive required."). A copy-pasteable command block (`git push origin
main`), then a streaming dark terminal-style output block showing the
push happening live, then a success banner ("Pushed. origin is up to
date — any device polling it, or the next USB drive stamped, picks this
up."). `Close` button. Same gap as Review & Sign: this dialog currently
uses a plain `notify()`, not the streaming command/output treatment shown
here.

## Open questions

- ~~Full break-glass~~ **decided** — see "Manual override," below.
- ~~Operator feedback with no network~~ **decided, not designed** — a
  hostname-keyed log written back onto the drive itself: how someone in the
  field finds out whether an applied drive actually succeeded without any
  network to phone home over. Entries include things like a rejected commit
  ("trust disallowed" — a signature that didn't verify, or a key not in
  `trust.d/`), not just successes. `docs/provisioning.md` now specifies how a
  plugin *reconciles* its own resource type (`provision_apply`), but not who
  drives that — fetch, verify, fast-forward, the boot trigger, and writing
  this log are all one level up, still not designed. Recorded here so the
  decision isn't lost before that phase starts.

## Rejected

**Quorum, or self-revoke-only, for admin-on-admin revocation.** Any admin
can `trust.d/admins/remove/<anyone>`, including another admin — decided, on
direct instruction, not a gap. Two mutually-untrusting parties who both hold
admin keys on one device can lock each other out, and that is simply what
holding admin together means, the same as it would for two people who both
have root. Building quorum bookkeeping or a self-revoke-only rule to prevent
it is exactly the kind of properly-scoped-authority-domain machinery this
project has consistently chosen not to build — real complexity, permanent
maintenance cost, for a scenario resolved in the field by not handing admin
to someone you don't trust with it.

**Locking a resource type against `patch` once `baseline` governs it.** The
alternative to "state reconciles first, then patch layers on top, unlocked"
(see "Two remotes"): once a baseline declares an exact `users/` set, refuse
any `patch` commit touching `users/` until a new baseline supersedes it.
Rejected on direct instruction — the people this is built for are trusted
with root already, and refusing their own patches on top of a baseline they
can see and reason about is a paternalism nothing else here practices.
Accepted cost: a "certified exact state" is a floor at the moment it was
applied, not a standing guarantee — worth restating next to `Export` or
wherever a certification's currency gets shown, so nobody mistakes "was
certified" for "is still certified."

**A standalone out-of-process app for authoring drives.** Own container,
own reverse-proxied UI, installable independent of any robot. The
out-of-process plugin contract (`proxy.py`/`discovery.py`) makes this
genuinely buildable, which is exactly what made it tempting — but its scope
would have been "whatever the drive format can hold" rather than "whatever
SpiriConfig can actually do," and it surfaced immediately: it needed a
network-config screen with no network plugin behind it. It also duplicated
two things that already exist — `advanced.only()` as the hide-don't-forbid
mechanism, and per-user `Scope` as the "runs standalone on an operator's
laptop" story — for no benefit over using them directly. See "Authoring:
where the editing UI lives" above for the shape that replaced it.

**Implicit trust-on-first-use.** The tempting version is "the first signed
commit you ever see becomes trusted," with no distinct `trust.d/` step at all.
Wrong specifically because the open window is when a device is *least*
physically controlled — a warehouse, a shipping crate, a staging area — not
most. Explicit key placement is the mitigation that's actually built:
first-key-wins, but it requires a deliberate write to `trust.d/`, not an
inference from an ordinary commit.

**Staging trust changes in a `trust.d.pending/`, requiring local (non-drive)
promotion.** Considered as a way to close the first-key-wins window further.
Declined: extra moving part, extra directory semantics, for a scenario
(hostile use of the open window) that's already mitigated by controlling
*where* that window happens rather than adding a second gate inside it.

**A `request:`/verb-shaped manifest.** First draft of this had explicit actions
("pin-trust-key", etc.). Dropped in favour of pure directory/file declarations
— consistent with this codebase's existing refusal to have an "enabled" state
anywhere else (a stack exists because a directory exists; an app is installed
because a symlink exists). `scripts/` is the one deliberate exception, and it's
scoped and named as an escape hatch rather than folded into the declarative
surface.

**Reading the USB filesystem directly instead of through git.** Simpler on
paper, but throws away git's ancestry-based replay/rollback protection and
means re-deriving signature-chain verification by hand against a mutable FAT32
tree with no integrity guarantees of its own.

## Phasing

**Superseded in practice.** Written before the decision to build the
*editor* first and leave the applier for later (see `docs/provisioning.md`'s
scope note) — this list still starts with an apply-only CLI loop, which is
the opposite order. Left as-is rather than rewritten, since it's still the
right shape once applying is actually being built; what's true today is
that steps 3 and 4's authoring halves are done ahead of steps 1–2, patch
*and* baseline both: `users/` `add`/`remove` staging, exact-set declaring,
description, and `provision_apply` all exist and are tested
(`src/spiriconfig_users/provision.py`); so does the equivalent for
`apps/` -- a full app-catalog list (not a name-entry form, see the
mockup section below) with Add/Remove staging, exact-set declaring,
`apps/stores.toml` (app store registration, cloned at apply time), and
per-app settings reusing `spiriconfig_docker.settings`/`widgets`
wholesale, written to `apps/<mode>/<name>/env` -- minus `apps/images/` +
skopeo, still future work (`src/spiriconfig_appstore/provision.py`).
Authoring is organised into named
*profiles* (`shop-floor-fleet`, `dev-kits`, ...), one per remote — a patch
profile and a baseline profile are separate namespaces, never one bundled
repo-level choice (`spiriconfig_provision/repo.py`'s
`Profile`/`list_profiles`/`create_profile`, each taking `remote`) — with
Review & Sign
(commits, signed with whatever `git config user.signingkey` already
names), Pull, and Export (drive path or URL, same operation either way)
all built and reachable at `/provision` behind advanced mode. Step 1
itself is also done, minus the part that needs step 2 first: `spiriconfig
provision apply <remote> <source>` (`src/spiriconfig_provision/apply.py`)
does fetch → ff-check → tier-gate → reconcile for the `off` and `trusted`
tiers; `signed` refuses cleanly rather than pretending to verify anything,
since there is still no `trust.d/` (or `network/`) at all to verify
against. Still missing from the page: `trust.d/` authoring, a commit
history log, and the manual-override ("apply an unmanaged repo") flow.

1. Core loop only, CLI, no USB/UI, one remote at a time: `spiriconfig
   provision apply <remote> <path>` doing fetch → ff-check → (signed tier)
   per-commit verification → reconcile. Provable and testable with zero new
   hardware concerns, and "one remote" is deliberate — running it twice by
   hand (once per remote) is enough to prove step 3's baseline-then-patch
   ordering before any code exists to automate calling it twice. **Done for
   `off`/`trusted`; `signed`'s per-commit verification is blocked on step 2.**
2. `trust.d/` (admins/signers, `add`+`remove`) + `network/` (`add` only) via
   rsync — smallest useful slice, both file-shaped, proves the signed tier
   and the two-tier trust check end to end.
3. `users/` `add`+`remove` via shadow-utils, in a `patch` remote. **Done,**
   `state/` too (see the note above step 1).
4. `apps/` `add`+`remove`, then `images/` + skopeo, then the `x-spiri-settings`
   reuse for `apps/<mode>/<name>/env`. **Done except `images/`+skopeo:**
   `add`+`remove`, `state/`, store registration, and per-app settings
   (`apps/<mode>/<name>/env`) are all built, ahead of this phasing's own
   order -- the settings reuse was meant to come last but the mockup
   showed it as core to the design, not a deferred extra.
5. `state/` for whichever of the above matter most for a real compliance
   case, in a `baseline` remote — deliberately after `add`/`remove` are
   solid, since `state/`'s reconciliation (list current, diff, prune) is
   built on the same three primitives those already need.
6. `scripts/`.
7. The boot-time systemd unit itself, calling step 1's logic once per
   configured remote in baseline-then-patch order. Deliberately last despite
   being simple — it's a thin wrapper, not new machinery, so there's little
   reason to build it before the thing it wraps is trustworthy.
