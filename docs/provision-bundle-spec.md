# Provisioning: the bundle format

A provisioning repo is an ordinary git repository whose working tree is
read directly by {doc}`provisioning`'s `provision_apply`/`provision_describe`
methods. This page is a pure reference for that tree: what directories
exist, which plugin owns each one, and what the files inside them look
like. It intentionally has no design rationale -- that's
`NOTES-usb-provisioning.md` at the repo root -- and no interface docs --
that's {doc}`provisioning`.

Each entry below is tagged **Built** (reconciled by a real `provision_apply`
today) or **Design only** (the format is decided, nothing reads or writes
it yet).

## Two remotes, two trees

A repo is either a **patch** remote or a **baseline** remote, never both --
see {doc}`provisioning`'s "Profiles" section. The two use disjoint verbs
under each resource directory:

| Remote     | Verbs           | Meaning                                            |
| ---------- | --------------- | --------------------------------------------------- |
| `patch`    | `add/`, `remove/` | ensure these entries exist / ensure these are gone |
| `baseline` | `state/`        | the managed set is exactly this, nothing else       |

`remove/<name>` is always a bare empty *file*, never a directory -- git
tracks nothing about an empty directory, so a directory-only marker
would vanish the moment it's the only thing in a commit.

## Directory ownership

| Path            | Owning plugin          | Status      |
| ---------------- | ----------------------- | ----------- |
| `trust.d/`        | `spiriconfig_provision` (self) | Design only |
| `users/`          | `spiriconfig_users`      | Built       |
| `apps/`           | `spiriconfig_appstore`   | Built (`apps/images/` bundling built; consuming a bundle at apply time is not) |
| `network/`        | *(no plugin yet)*        | Design only |
| `scripts/`        | *(no owning plugin -- imperative escape hatch, `trusted` tier only)* | Design only |

A directory's owner is whichever plugin's `provision_resource` class
attribute names it (`spiriconfig_provision.repo.contributors()` finds it
by that, not a registry) -- see {doc}`provisioning`'s "Provisioning is a
plugin" section for how a new resource type gets added.

## The full tree

```
patch remote
/
├── trust.d/                                  design only
│   ├── admins/{add,remove}/<fingerprint>.pub  ssh-signers format
│   └── signers/{add,remove}/<fingerprint>.pub
├── users/                                     spiriconfig_users
│   ├── add/<name>/
│   │   ├── password.hash                      crypt(3) string, e.g. yescrypt
│   │   ├── groups                             optional, one name per line
│   │   └── authorized_keys                    optional, OpenSSH authorized_keys format
│   └── remove/<name>                          empty marker file
├── network/                                   design only, no plugin yet
│   ├── add/<file>.nmconnection                 NetworkManager keyfile, dropped in verbatim
│   └── remove/<file>.nmconnection
├── apps/                                      spiriconfig_appstore
│   ├── stores.toml                             [[store]] url, ref -- whole-file rewrite, no add/remove
│   ├── add/<name>/env                          optional; app's declared settings as KEY=value lines
│   ├── remove/<name>                           empty marker file
│   └── images/oci/                             one shared OCI layout for every bundled image
│       ├── index.json
│       └── blobs/sha256/<digest>               routed through git-lfs
└── scripts/                                    design only, trusted tier only
    ├── 00-preflight.sh
    └── 10-site-specific.sh

baseline remote
/
├── trust.d/                                   design only
│   ├── admins/{add,remove}/<fingerprint>.pub
│   └── signers/{add,remove}/<fingerprint>.pub
├── users/state/<name>/
│   ├── password.hash
│   ├── groups                                 optional
│   └── authorized_keys                        optional
├── network/state/<file>.nmconnection           design only
└── apps/
    ├── stores.toml                             same file as the patch side, own copy per remote
    ├── state/<name>/env                        optional
    └── images/oci/                             same layout as the patch side
```

## `users/` (`spiriconfig_users/provision.py`)

- **`add/<name>/`** and **`state/<name>/`** are directories holding:
  - `password.hash` -- always present. A `/etc/shadow`-compatible hash
    line (`crypt(3)`; `$y$...` for the default `yescrypt` method), produced
    by `mkpasswd --method=<method> --stdin`. Never plaintext.
  - `groups` -- optional. One group name per line, written only if the
    staged form's Groups field was non-empty; the file is deleted (not
    left empty) on a restage that clears it. **Staged, not yet applied**:
    `provision_apply` reads `password.hash` only today, so a declared
    `groups` file has no effect on a device yet.
  - `authorized_keys` -- optional. One OpenSSH public-key line per
    non-blank line of the staged textarea, i.e. already in
    `~/.ssh/authorized_keys` format. Same "written only if non-empty,
    deleted on a restage that clears it" rule as `groups`, and the same
    **staged, not yet applied** caveat.
- **`remove/<name>`** -- an empty marker file. Presence means "delete
  this account."
- Managed scope is `User.is_login()`'s band (`[uid_min, uid_max]`, a real
  login shell) -- `state/` can never touch `root`, daemons, or
  `nologin` service accounts, declared or not.

## `apps/` (`spiriconfig_appstore/provision.py`)

- **`stores.toml`** -- app stores this profile's devices should have
  cloned, as repeated `[[store]]` TOML tables:
  ```toml
  [[store]]
  url = "https://example.com/spiri-apps.git"
  ref = "main"          # optional
  ```
  Whole-file rewrite on every change; there is no per-entry add/remove
  marker. One copy per remote (patch and baseline each carry their own),
  since each is applied on its own and neither falls back to the other's.
- **`add/<name>/`** and **`state/<name>/`** are directories holding one
  optional file:
  - `env` -- the app's declared `x-spiri-settings`, rendered the same
    `KEY=value` bytes `spiriconfig_docker.env` writes for a live stack's
    `.env`. Missing or empty means "install with defaults."
- **`remove/<name>`** -- an empty marker file, same shape as `users/remove/`.
- **`images/oci/`** -- one shared [OCI image
  layout](https://github.com/opencontainers/image-spec) per repo (not one
  per app), populated by the page's "Bundle images" action via `skopeo
  copy`. Content-addressed (`blobs/sha256/<digest>`), so a layer shared
  across apps or versions is only ever stored once. Every path under it is
  routed through git-lfs (`apps/images/oci/blobs/** filter=lfs ...` in
  `.gitattributes`) so ordinary clones/fetches of the repo's text stay
  small. **Bundling is built; nothing on the applying end consumes this
  directory yet** -- installs still pull over the network at apply time.
- Managed scope is everything `spiriconfig_appstore.installed()` reports
  (a symlink into a configured store) -- unlike `users/`, there is no
  protected subset, so `apps/state/` really is a full overwrite of
  everything installed.

## `trust.d/` (design only, owned by `spiriconfig_provision` itself)

Not implemented -- no code reads or writes this directory yet, and the
`signed` tier refuses outright rather than pretend to check anything
against it (`spiriconfig_provision/apply.py`). The decided shape, once
built:

- `admins/{add,remove}/<fingerprint>.pub` and
  `signers/{add,remove}/<fingerprint>.pub` -- `ssh-keygen`-format public
  keys, one file per key, named by its fingerprint. `add`/`remove` only,
  no `state/` -- revoking one key is a scoped `remove/`, never a
  re-declaration of everyone else.
- A key in `admins/` may sign a commit that itself changes `trust.d/`; a
  key in `signers/` may sign ordinary content only.
- Can appear inside either a `patch` or a `baseline` remote -- governed
  by which list a key sits in, not by which remote carries it.

## `network/` (design only, no owning plugin yet)

Not implemented -- there is no network-management plugin in this codebase
today, so nothing stages, describes, or applies this directory. Decided
shape once one exists: `add/<file>.nmconnection` / `remove/<file>.nmconnection`
for `patch`, `state/<file>.nmconnection` for `baseline` -- NetworkManager
keyfiles, dropped in verbatim, filename is the declaration (a renamed
profile is a delete-then-recreate, not a rename).

## `scripts/` (design only, no owning plugin)

Not implemented. Decided shape: arbitrary shell scripts, ordered by
filename prefix (`00-`, `10-`, ...), no manifest. `trusted` tier only --
excluded from `signed` entirely, since arbitrary code doesn't fit
`signed`'s "every change traces to a resource type with defined
semantics" story no matter whose key signed it.

## See also

- {doc}`provisioning` -- the `ProvisioningContributor` interface a plugin
  implements to read and write these directories, and how
  `spiriconfig_provision` discovers, calls, and orchestrates contributors.
- `NOTES-usb-provisioning.md` (repo root) -- why the format looks like
  this: the trust model, the two-remote split, the add/remove/state
  contract, and everything still undecided.
