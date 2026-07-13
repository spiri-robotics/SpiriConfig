# App store

An app store is a git repository with one directory per app, each containing a
compose file. That is the entire format. There is no manifest, no index, and
nothing to register: if you can push a directory with a `compose.yaml` in it, you
have published an app.

```
spiri-apps/
├── whoami/
│   └── compose.yaml
├── nextcloud/
│   ├── compose.yaml
│   └── nginx.conf
└── traefik/
    └── compose.yaml
```

Which means you can use one without SpiriConfig at all:

```console
$ git clone https://github.com/spiri/spiri-apps
$ cp -r spiri-apps/whoami /srv/compose/
$ cd /srv/compose/whoami && docker compose up -d
```

Everything below is a convenience on top of that.

## Pointing at a store

```console
$ export SPIRICONFIG_APPSTORE_STORES='["https://github.com/spiri/spiri-apps"]'
$ spiriconfig appstore sync
```

`sync` clones stores you have not got yet, and fetches the ones you have. It is
safe to run at any time: fetching only updates git's idea of what the remote has,
and changes no installed app.

You can configure several stores. If two of them have an app with the same name,
say which you mean: `spiriconfig appstore install spiri-apps/whoami`.

## Installing

```console
$ spiriconfig appstore list
spiri-apps
  nextcloud  2026-03-14
  traefik    2026-01-08
  whoami     1.10.1

$ spiriconfig appstore install whoami
Installed spiri-apps/whoami as whoami.
Start it with: spiriconfig docker up whoami
```

Installing makes a symlink, and nothing else:

```console
$ spiriconfig appstore install whoami --show
ln -s /var/lib/spiriconfig/stores/spiri-apps/whoami /srv/compose/whoami
```

The app is now an ordinary stack. The docker plugin has no idea it came from a
store, so everything works on it exactly as it would on a directory you made
yourself:

```console
$ spiriconfig docker up whoami
$ cd /srv/compose/whoami && docker compose up -d   # the same containers
```

Installing does not start anything. Two apps that both want port 8080 will not
fight until you tell them to, and `--as` gives one of them another name:

```console
$ spiriconfig appstore install whoami --as whoami-test
```

## Versions

An app's version is a **label**. Nothing depends on it, and you never have to
answer the unanswerable question of what "version 2" of a set of containers means.

If the store's author set one, that is what you see:

```yaml
x-spiriconfig-version: "1.10.1"
services:
  whoami:
    image: traefik/whoami:v1.10.1
```

If they did not, you get the date of the last commit that touched the app, which
is always available and always moves when the app does.

Updates are not decided by either of these. An app has an update when git says
its files differ from the store's, which is exact.

## Updating

The interesting case is the one where you have edited an app. You are *supposed*
to edit apps -- a store cannot know your ports, your paths, or your hardware.

Suppose you changed whoami's port, and the store bumped its image:

```console
$ spiriconfig appstore diff whoami
--- Your changes ---
-      - "8080:80"
+      - "9080:80"

--- Store changes since your version ---
-    image: traefik/whoami:v1.10.1
+    image: traefik/whoami:v1.11.0
```

```console
$ spiriconfig appstore update
```

You keep your port, and you get their image. That is a three-way merge, done by
git, because an installed app lives in a git working tree and your edits to it are
commits. Nothing clever is happening here, which is the point.

`update` rewrites files on disk. **It does not restart anything.** Your containers
keep running the old definition until you say otherwise:

```console
$ spiriconfig docker pull whoami && spiriconfig docker up whoami
```

This also means updates are per-store, not per-app: pulling the store updates
every app's *files*, and you choose which apps to actually restart. That is the
decision you cared about anyway.

### When it conflicts

If you and the store changed the same lines, git cannot decide for you, and it
does not pretend to:

```console
$ spiriconfig appstore update
CONFLICT (content): Merge conflict in whoami/compose.yaml

Update stopped: your edits conflict with the store's.
  /var/lib/spiriconfig/stores/spiri-apps/whoami/compose.yaml
```

The file now has conflict markers in it:

```yaml
services:
  whoami:
<<<<<<< HEAD
    image: traefik/whoami:v1.10.1-custom
=======
    image: traefik/whoami:v1.11.0
>>>>>>> @{upstream}
```

Nothing can start in this state, and that is a feature: conflict markers are not
valid YAML, so `docker compose` refuses the file, and so does SpiriConfig's
editor. You cannot accidentally run a half-merged app.

Edit the file, keep what you want, delete the `<<<<<<<`, `=======` and `>>>>>>>`
lines, then:

```console
$ spiriconfig appstore resolve
```

If any markers are left, this refuses and tells you which files. Running it too
early cannot commit a broken app.

Or change your mind entirely:

```console
$ spiriconfig appstore resolve --abort
```

That puts everything back exactly as it was before the update. **Your own edits
survive**, because they were committed before the merge began.

If you would rather just take the store's version and throw your changes away:

```console
$ spiriconfig appstore update --discard-local
```

## Uninstalling

```console
$ spiriconfig appstore uninstall whoami --show
rm /srv/compose/whoami
```

That removes a symlink. It deletes no app files, and no data. It also does not
stop anything -- run `spiriconfig docker down whoami` first, or its containers
will outlive the thing that defined them.

SpiriConfig will refuse to uninstall a directory it did not create. If
`/srv/compose/whoami` is a real directory, it is yours, and removing it is your
call to make with `rm`.

## Adopting

Adopting an app takes it out of the store and makes it yours.

```console
$ spiriconfig appstore adopt whoami
Adopt whoami from spiri-apps?

    rm /srv/compose/whoami
    cp -r /var/lib/spiriconfig/stores/spiri-apps/whoami /srv/compose/whoami

whoami stops tracking the store for good: no more updates, and nothing here
will touch it again. There is no undo -- afterwards `spiriconfig appstore uninstall`
will refuse it too, because the directory will be a real one we did not create.

Adopt it? [y/N]:
```

The symlink is replaced with a real copy, and the app becomes an ordinary compose
project that you own completely. **It will never update again.** Nothing in the
app store will touch it, and nothing in the app store will clean it up either --
removing it afterwards is a `rm -rf` you do yourself.

It is the one irreversible thing here, which is why it is the one thing that asks.
`--yes` skips the question, for scripts.

Use it when an app has diverged so far that merging is a chore, or when you want
to take something the store is not going to follow.

## A note for store authors

Prefer **named volumes** to relative bind mounts.

An installed app is a symlink into the store's git checkout, so a compose file
that says `./config:/config` makes docker create `config/` *inside that
checkout*. It works, and SpiriConfig ignores those directories when deciding
whether you have edited an app -- but it puts user data inside a git repository,
which is not where anyone expects to find it.

```yaml
services:
  nextcloud:
    image: nextcloud:29
    volumes:
      - nextcloud-data:/var/www/html   # good
      # - ./data:/var/www/html         # works, but lands in the git checkout

volumes:
  nextcloud-data:
```

Set `x-spiriconfig-version` if you have a version worth naming. If you do not,
leave it out -- the commit date is a perfectly good label, and a stale version
number is worse than none.

## Settings

| Setting | Default | What it is |
| --- | --- | --- |
| `SPIRICONFIG_APPSTORE_STORES` | `["test_data/example-store"]` | JSON list of git URLs (or local paths). |
| `SPIRICONFIG_APPSTORE_STORE_DIR` | `test_data/stores` | Where clones live. |
| `SPIRICONFIG_APPSTORE_GIT_BIN` | `git` | The git executable. |
| `SPIRICONFIG_APPSTORE_COMMAND_TIMEOUT` | `300.0` | Seconds before git is considered hung. |

The store directory is not a cache you can blow away casually: your edits to
installed apps live there, as commits. It is a git repository, and you can `cd`
into it and use it as one.

The defaults are **relative**, and point into `test_data/`. That is so a checkout
of this repository can be run without it reaching for `/var/lib` and `/srv` and
managing the containers on your actual machine. A deployment sets absolute paths
-- see [configuration](configuration.md).

## Trying it, from a checkout

```console
$ ./scripts/test-data.sh
$ uv run spiriconfig appstore sync
$ uv run spiriconfig appstore install whoami
$ uv run spiriconfig docker up whoami
$ curl localhost:8080
Hostname: 2d5bcd6f2629
IP: 172.18.0.2
GET / HTTP/1.1
```

`scripts/test-data.sh` copies `examples/store/` into `test_data/example-store` and
`git init`s it, because a store has to be a git repo before it can be cloned. The
whole tree is gitignored and disposable: delete `test_data/` and run the script
again.

The example store carries `whoami` (which answers with a description of your
request -- the fastest way to tell whether any of this works), `traefik`, and
`nextcloud` (which has state, and shows what named volumes are for).
