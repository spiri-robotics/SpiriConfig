#!/bin/sh
# Build ./test_data, so a fresh checkout of this repo can run SpiriConfig against
# something real without touching /srv/compose or /var/lib on the developer's box.
#
# It creates two things:
#
#   test_data/example-store/  a git repo, cloned from examples/store/
#   test_data/compose/        an empty compose directory, for apps to land in
#
# The store has to be a *git repo* -- that is the whole format -- and a
# subdirectory of this repository is not one, so it gets copied out and
# `git init`ed. It is disposable: delete test_data/ and run this again.
#
# SpiriConfig's defaults are the real install paths (/srv/compose as root,
# ~/spiri-apps otherwise -- see spiriconfig.paths), so this also points the
# checkout's .env (gitignored, read from the CWD) at test_data/. It only appends,
# and only if the .env does not already set them. After running this:
#
#   uv run spiriconfig appstore check
#   uv run spiriconfig appstore install whoami
#   uv run spiriconfig docker up whoami
#   uv run spiriconfig serve &
#   curl localhost:8337/plugin/whoami/
set -eu

root=$(git rev-parse --show-toplevel)
cd "$root"

if ! grep -qs '^SPIRICONFIG_DOCKER_COMPOSE_DIR=' .env; then
    cat >> .env <<EOF
# Added by scripts/test-data.sh: keep this checkout on test_data/, away from
# the real install paths.
SPIRICONFIG_DOCKER_COMPOSE_DIR=$root/test_data/compose
SPIRICONFIG_APPSTORE_STORE_DIR=$root/test_data/stores
SPIRICONFIG_APPSTORE_STORES=["$root/test_data/example-store"]
EOF
    echo "Pointed .env at test_data/."
fi

if [ -e test_data ] && [ "${1:-}" != "--force" ]; then
    echo "test_data/ already exists. Re-run with --force to rebuild it from scratch." >&2
    echo "(That deletes any apps you installed into it, and any edits to the store.)" >&2
    exit 1
fi

rm -rf test_data
mkdir -p test_data/compose test_data/stores

cp -r examples/store test_data/example-store

# An identity on the command line, not in the repo's config: the machine running
# this may have no global git identity, and a commit needs one.
git -C test_data/example-store init -q -b main
git -C test_data/example-store add -A
git -C test_data/example-store \
    -c user.name="SpiriConfig" \
    -c user.email="spiriconfig@localhost" \
    commit -qm "Example apps"

echo "Built test_data/:"
echo "  test_data/example-store   the app store ($(git -C test_data/example-store rev-parse --short HEAD))"
echo "  test_data/compose         empty; installed apps are symlinked in here"
echo "  test_data/stores          empty; 'appstore check' clones the store into it"
echo
echo "Next:"
echo "  uv run spiriconfig appstore check"
echo "  uv run spiriconfig appstore install whoami"
echo "  uv run spiriconfig docker up whoami"
echo "  uv run spiriconfig serve & curl localhost:8337/plugin/whoami/"
