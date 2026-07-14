# Advanced mode

One machine has to serve two audiences: someone who wants to restart a service
without thinking about it, and someone who wants the exact `docker compose` line
so they can go and do something cleverer with it. Advanced mode is the switch
between those two views.

It is a toggle at the bottom of the sidebar. Off by default; the choice sticks.

## What it is not

:::{danger}
**Advanced mode is not a permission system. It is not a security boundary. It
must never be used as either.**

**The toggle is self-service.** It sits in the sidebar of every page, and anybody
looking at the web UI can turn it on. One click, and every advanced feature is
right there. Nothing is withheld from anyone.

So a person is never *prevented* from editing a compose file by having advanced
mode off. They flip the switch and edit it in the browser, like anyone else. No
shell, no CLI, no special knowledge -- the switch is labelled and visible.

And underneath even that: hiding an element is a *presentation* fact. The page
talks to the server over a websocket, and what a browser chooses to display has
no bearing on what a client can send down it. Anyone who wants the capability has
it, whatever the page is currently rendering.

Advanced mode gives people who want a simpler page a simpler page. That is the
entire feature. To actually stop someone doing something, see
[below](#if-you-need-real-permissions) -- it looks nothing like this.
:::

Everything stays reachable from the CLI too, of course -- that is what makes
[progressive enhancement](design.md) true. But the CLI is not the loophole here,
and neither is the browser. Treat advanced mode as a decluttering preference, on
the same footing as a dark-mode switch.

(if-you-need-real-permissions)=

## If you need real permissions

Not this. Permissions are not planned, but if they land, the intended design is:

**Log the user in with PAM, then fork to that unix user.**

The OS then enforces authorisation -- file modes, group membership, whether they
are in the `docker` group at all. There is no application-level role model, no ACL
table, and nothing for us to get wrong.

That is the same principle as the rest of SpiriConfig. We drive the machine by
running the commands a human would run, so a person should be able to do exactly
what their shell account could do, and nothing more. An app-level permission model
would be a second, weaker source of truth sitting in front of a capability the
kernel already governs -- and the first time the two disagreed, the kernel would
win and our UI would be lying.

So: capability is decided by *who you are on the machine*. Advanced mode only
decides *how much the page shows you*. Keeping those apart is what stops the UI
from merely looking safe.

## What is behind it

| Feature | Why |
| --- | --- |
| Editing a compose file | The most dangerous thing on the page. Still available from a shell: `$EDITOR "$(spiriconfig docker config whoami)"`. |
| Editing an app's `.env` | The settings form is the app author's list of knobs, which is a good default and a poor cage. The panel under the form shows the exact bytes about to be written, and lets you type in them -- variables the author never declared, comments, anything. Still available from a shell: `$EDITOR "$(spiriconfig docker env whoami)"`. |
| The raw command display | The `cd … && docker compose …` line and its copy button, in every action dialog. Developers want it; for everyone else it is noise. |

Note what is *not* hidden: logs, and the settings form itself. A regular user is
exactly the person who needs to see why a service failed, and the output of a
command is not the same thing as the invocation that produced it. And an app
author decided which knobs are safe to turn, so turning one is an ordinary act --
what is advanced is the file they land in.

## Setting the default

`SPIRICONFIG_ADVANCED` sets the starting position for someone who has not chosen
yet -- so a developer image can ship with it on, and a customer image with it off,
from the same code:

```console
$ SPIRICONFIG_ADVANCED=true spiriconfig serve
```

A person's own choice always beats this default.

To make that choice survive a restart, set a stable secret -- the cookie
identifying a browser is signed with it:

```console
$ export SPIRICONFIG_STORAGE_SECRET="$(openssl rand -base64 32)"
```

Without one, SpiriConfig generates a temporary secret, warns, and everybody's
setting resets when the process restarts.

## Using it in a plugin

```python
from spiriconfig import advanced

ui.button("Up", on_click=...)          # everyone

with advanced.only():
    ui.button("Edit", on_click=...)    # developers only
```

Elements inside {func}`~spiriconfig.advanced.only` are *bound* to the setting
rather than conditionally created, so the toggle takes effect instantly, without
rebuilding the page or losing whatever the person was in the middle of.

For a single element, {func}`~spiriconfig.advanced.mark` does the same thing and
chains:

```python
advanced.mark(ui.label(str(command))).classes("font-mono")
```

And to branch on it, {func}`~spiriconfig.advanced.enabled`:

```python
if advanced.enabled():
    ...
```

Prefer `only()` and `mark()` where you can: because they bind rather than branch,
they respond to the toggle without a re-render, and `enabled()` does not.

## When users arrive

Today SpiriConfig has no users, so "this person" means "this browser", identified
by a session cookie. That is the finest-grained identity available, and it is the
limitation user support will lift.

The code is built for that day. Nothing reads browser storage directly; every
preference goes through one seam, {mod}`spiriconfig.preferences`. Adding user
support means registering a different store, once:

```python
from spiriconfig import preferences

class DatabasePreferences:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    def get(self, key, default):
        return db.fetch_preference(self.user_id, key, default)

    def set(self, key, value) -> None:
        db.store_preference(self.user_id, key, value)

preferences.use(lambda: DatabasePreferences(current_user().id))
```

The factory is called per access, not once, so it can resolve *whoever is being
served right now* -- which is exactly what a per-user setting needs.

No plugin changes. `advanced.only()` keeps working, and the setting silently
becomes a property of the person rather than of the browser they happen to be
sitting at. There is a test (`test_advanced.py`) that registers a store keyed on
something other than a browser and asserts advanced mode follows it, precisely so
this promise does not quietly rot before anyone tries to use it.
