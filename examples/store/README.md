# Example app store

An app store is a git repository with one top-level directory per app, each
containing a compose file. That is the whole format -- this directory *is* the
documentation for it.

`scripts/test-data.sh` copies this into `test_data/example-store` and runs
`git init` on it, because a store has to be a git repo before it can be cloned.
SpiriConfig's default settings point at the result, so a fresh checkout can do:

```console
$ ./scripts/test-data.sh
$ uv run spiriconfig appstore sync
$ uv run spiriconfig appstore install whoami
$ uv run spiriconfig docker up whoami
$ curl localhost:8080
```

`docs/` below is here on purpose: a top-level directory with no compose file in
it is not an app, and must be ignored rather than crashed on.
