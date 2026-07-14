"""The NiceGUI face of the docker plugin.

Nothing here may do anything the CLI cannot. Each action opens a dialog that
shows the exact ``docker compose`` command being run, with a copy button, and
then streams its output -- so the UI is a demonstration of the command line
rather than a replacement for it.
"""

from __future__ import annotations

import asyncio
import shlex
import uuid

from loguru import logger
from nicegui import context, ui

from spiriconfig import advanced, terminal, theme
from spiriconfig.commands import Command, PtySession, run, stream_pty

from spiriconfig_docker import settings as app_settings
from spiriconfig_docker import widgets
from spiriconfig_docker.config import DockerSettings, docker_settings
from spiriconfig_docker.settings import SettingsError
from spiriconfig_docker.stacks import (
    DEFAULT_EXEC_COMMAND,
    Stack,
    StackError,
    discover,
)

log = logger.bind(plugin="docker")

#: What CodeMirror should make of a ``.env``. There is no ``.env`` mode, and this is
#: the one it already has for ``KEY=value`` with ``#`` comments -- which is exactly
#: what the file is. Shell is the tempting alternative and the wrong one: a ``.env``
#: is not shell, it runs nothing, and highlighting it as though it did would be the
#: editor telling a lie about the file it is editing.
_ENV_LANGUAGE = "Properties files"

#: Status word -> Quasar colour, for the badge on each stack.
STATUS_COLOURS = {
    "running": "positive",
    "partial": "warning",
    "stopped": "grey",
    "down": "grey-7",
}


async def _run_in_dialog(title: str, command: Command) -> None:
    """Show ``command``, stream it, and leave the output on screen to read.

    Returns only when the *user* closes the dialog, not when the command
    finishes. That is load-bearing, and the reason is worth writing down.

    Callers do ``await _run_in_dialog(...)`` and then ``refresh()``, and refresh
    clears the container this dialog was created inside -- which deletes the
    dialog. So a version of this that returned as soon as the command exited
    tore its own output off the screen a frame later. Nobody noticed for `up` or
    `pull`, which stream for long enough to read; `logs` finishes instantly, and
    the modal appeared and vanished.

    Waiting for the dismissal fixes it at the source: the output cannot be
    cleared away while the user is still looking at it, because we have not
    handed control back to the code that clears things.
    """
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(title).classes("text-lg font-bold")

        # Advanced only: the exact command line, to copy and run yourself. The
        # output below is shown to everyone -- a regular user still needs to see
        # what went wrong, they just do not need the invocation that caused it.
        with advanced.only():
            _copyable(command)

        output = terminal.terminal()
        close = ui.button("Close", on_click=dialog.close).props("flat")
        close.disable()

    dialog.open()
    try:
        async for chunk in stream_pty(
            command,
            log=log,
            rows=terminal.TERMINAL_ROWS,
            columns=terminal.TERMINAL_COLUMNS,
        ):
            output.write(chunk)
    except Exception as exc:  # noqa: BLE001 - surface any failure in the dialog
        log.exception("command failed: {}", command)
        output.write(f"\r\n[error] {exc}\r\n")
    finally:
        close.enable()

    # Block here until Close (or escape, or a click outside) dismisses it.
    await dialog

    # Then take it away. A closed dialog is still an element on the page, so
    # without this every button press leaves one behind, and a session spent
    # starting and stopping things accretes a pile of invisible modals holding
    # on to their output.
    dialog.delete()


def _copyable(command: Command) -> None:
    """The exact command line, with a button to take it away with you."""
    with ui.row().classes(f"w-full items-center gap-2 {theme.COMMAND_CLASS} p-2"):
        ui.label(str(command)).classes("font-mono text-xs grow break-all")
        ui.button(
            icon="content_copy",
            on_click=lambda: ui.clipboard.write(str(command)),
        ).props("flat dense round").tooltip("Copy command")


async def _terminal_dialog(
    title: str,
    command: Command,
    *,
    what: str,
    shown: Command | None = None,
    reap: Command | None = None,
) -> None:
    """Run ``command`` on a pty, in a dialog you can type into.

    The sibling of :func:`_run_in_dialog`, and the distinction is the one drawn in
    :mod:`spiriconfig.terminal`: that one shows a *transcript* of a command somebody
    already decided on, this one hands you the keyboard. `up` and `pull` are things
    we run for you; `exec` and `attach` are things you run.

    ``shown`` is the command line we *print*, when that is not the one we run. Only
    `exec` needs it, and the split is not a fib -- it is the opposite. The line on
    screen is the line that gets you this shell, and the one the CLI runs, and the
    one worth copying; what we actually spawn is that same command wearing a
    babysitter (see :meth:`Stack.hangup`) which exists solely so the browser tab can
    hang up on it. Printing the babysitter would be showing the user a command line
    that is *about our implementation* rather than about their container.

    ``reap`` is that babysitter's other half, and it runs down both paths out of
    here -- the Close button, and the browser vanishing -- because those are two
    different ways for a session to end and only one of them tells us so.
    """
    session = PtySession(
        command,
        log=log,
        rows=terminal.TERMINAL_ROWS,
        columns=terminal.TERMINAL_COLUMNS,
    )
    client = context.client
    reaped = False

    async def hangup() -> None:
        """End the session, and kill what it left inside the container.

        Idempotent, because both endings can happen -- a user who closes the dialog
        and *then* the tab would otherwise reap twice. The kill is harmless the
        second time (the pid is already gone) but the log line is a lie, and a lie
        in a log is worth more than the two lines it costs to prevent.
        """
        nonlocal reaped
        session.close()
        if reap is None or reaped:
            return
        reaped = True
        await asyncio.to_thread(run, reap, log=log)

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(title).classes("text-lg font-bold")

        # Not behind `advanced.only()`, unlike everywhere else this block appears.
        # Getting here at all meant turning advanced mode on -- the buttons that
        # open this dialog are advanced-only -- so hiding it would only serve to
        # make it vanish if someone flipped the switch off mid-session, which is
        # a strange thing to arrange on purpose.
        _copyable(shown or command)

        view = terminal.interactive(
            on_data=lambda event: session.write(event.data),
            on_resize=lambda event: session.resize(event.rows, event.cols),
        ).classes("h-[60vh]")

        ui.button("Close", on_click=dialog.close).props("flat")

    dialog.open()

    # The closed tab. `pump` already hangs up the *session* on disconnect, but the
    # shell inside the container is not ours to hang up on -- it outlives the client
    # that started it, and nothing but this will kill it. Registered before the
    # session starts, unlike pump's, because the thing it cleans up is not the
    # process we spawned: it is the one docker spawned on our behalf, which exists
    # from the moment the exec lands and would survive us failing halfway through.
    client.on_disconnect(hangup)

    # Started as a task, not awaited: this runs for as long as the user keeps the
    # session open, and we have a dialog to sit and wait on in the meantime.
    running = asyncio.create_task(
        terminal.pump(session, view, client, what=what, log=log)
    )

    await dialog

    # The user is done. Hang up -- which ends `pump`'s loop over the output, so the
    # task finishes on its own and the process gets reaped rather than being left
    # for the garbage collector to notice.
    await hangup()
    await running
    dialog.delete()


async def _pick_service(title: str, services: list[str]) -> str | None:
    """Ask which container, when there is more than one it could be."""
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
        ui.label(title).classes("text-lg font-bold")
        service = ui.select(
            services, value=services[0], label="Service"
        ).classes("w-full").props("outlined")

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button(
                "Open", on_click=lambda: dialog.submit(service.value)
            ).props("color=primary").mark("pick-open")

    dialog.open()
    chosen = await dialog
    dialog.delete()
    return chosen


async def _exec_dialog(
    stack: Stack, services: list[str]
) -> tuple[str, list[str]] | None:
    """Ask what to run and where. Returns the service and the argv, or None.

    The parts rather than a finished :class:`Command`, because the caller builds two
    of them out of this -- the one it runs, which is supervised, and the one it shows,
    which is the one you would type. See :meth:`Stack.hangup`.

    The command is a text box rather than a shell button, because the box is the
    honest widget: this is ``docker compose exec``, which runs *anything* the image
    has, and a button labelled "Shell" would be a smaller tool wearing a costume.
    It is filled in with a shell, which is what almost everyone wants almost always.

    The line is shown as it is typed. That is the whole idea of the project rendered
    in one widget -- you can watch the thing you are about to run assemble itself,
    and copy it, and run it yourself instead.
    """
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label(f"{stack.name} — exec").classes("text-lg font-bold")

        service = ui.select(
            services, value=services[0], label="Service"
        ).classes("w-full").props("outlined")
        command = ui.input(
            "Command", value=DEFAULT_EXEC_COMMAND
        ).classes("w-full").props("outlined")

        with ui.row().classes(f"w-full items-center gap-2 {theme.COMMAND_CLASS} p-2"):
            preview = ui.label().classes("font-mono text-xs grow break-all")

        def argv() -> list[str] | None:
            """What was typed, as a command, or None if it is not one yet.

            ``shlex``, so that `sh -c 'echo hi'` means what it means in a shell --
            three arguments, not five. It is also the only thing here that can be
            *wrong*: an unbalanced quote is the one input this form can be given
            that has no command in it at all.
            """
            try:
                words = shlex.split(command.value)
            except ValueError:
                return None
            return words or None

        def show() -> None:
            words = argv()
            preview.set_text(str(stack.exec(service.value, words)) if words else "…")
            go.set_enabled(words is not None)

        def submit() -> None:
            if (words := argv()) is not None:
                dialog.submit((service.value, words))

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            go = (
                ui.button("Run", icon="play_arrow", on_click=submit)
                .props("color=primary")
                .mark("exec-run")
            )

        service.on_value_change(show)
        command.on_value_change(show)
        show()

    dialog.open()
    chosen = await dialog
    dialog.delete()
    return chosen


async def _edit_dialog(stack: Stack, on_saved) -> None:
    """Edit a stack's compose file, refusing to save something compose rejects."""
    try:
        text = await asyncio.to_thread(stack.read)
    except OSError as exc:
        ui.notify(f"Could not read {stack.compose_file}: {exc}", type="negative")
        return

    # Asked for before the dialog is built rather than inside it: both of these are
    # round trips, and doing them up front keeps the awaits out of the slot the
    # elements are being created in.
    editor_theme = await theme.codemirror_theme()

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-4xl"):
        ui.label(f"{stack.name} — {stack.compose_file}").classes("text-lg font-bold")
        editor = ui.codemirror(
            text, language="YAML", theme=editor_theme
        ).classes("w-full h-96")

        async def save() -> None:
            # Validation lives in Stack.write, which restores the old file if
            # docker compose rejects the new one -- so a bad save cannot leave a
            # stack unstartable.
            try:
                await asyncio.to_thread(stack.write, editor.value)
            except (StackError, OSError) as exc:
                ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
                return
            ui.notify(f"Saved {stack.compose_file}", type="positive")
            dialog.close()
            on_saved()

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=save).props("color=primary")

    dialog.open()


async def _settings_dialog(stack: Stack) -> str | None:
    """Edit a stack's declared settings. Returns what the user chose to do.

    ``"applied"`` if they asked for the stack to be brought up with the new
    settings, ``"saved"`` if they only saved, and ``None`` if they dismissed it
    without saving.

    Returning the choice rather than acting on it is what keeps the "apply" path
    out of a nested dialog. The command has to be streamed into an output dialog,
    and building that one inside *this* one -- which is mid-teardown by then --
    is a good way to reinvent the disappearing-modal bug. The caller runs it in
    the card's slot instead, exactly as every other action on the card does.
    """
    try:
        config = await asyncio.to_thread(app_settings.for_stack, stack)
        current = await asyncio.to_thread(config.values)
    except (SettingsError, OSError) as exc:
        ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
        return None

    # Asked for before the dialog is built, like _edit_dialog does, and for the same
    # reason: it is a round trip, and it does not belong in the slot the elements are
    # being created in.
    editor_theme = await theme.codemirror_theme()

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label(f"{stack.name} — settings").classes("text-lg font-bold")

        with ui.column().classes("w-full gap-4"):
            bound = widgets.form(config.fields, current)

        # Advanced only: the file itself, editable. The settings page is a nicer way
        # to edit a .env, and the form is the app author's idea of which knobs there
        # are -- which is a fine default and a poor cage. A developer wanting a
        # variable the author never declared, or a comment, or a key the form does
        # not know about, should not have to leave the page to get one.
        #
        # What it shows is not the file on disk but the bytes we would write *right
        # now*, form answers included. A preview is only worth anything before the
        # write, and an editor seeded with anything else would silently undo whatever
        # the user had just typed into the form above.
        with advanced.only(), ui.expansion(
            f"{config.env_file}", icon="description"
        ).classes(f"w-full {theme.COMMAND_CLASS}") as expansion:
            editor = ui.codemirror(
                "", language=_ENV_LANGUAGE, theme=editor_theme
            ).classes("w-full h-64")
            ui.label(
                "The bytes that will be written. Edit them and yours are written "
                "instead, exactly as typed — the form above stops being consulted."
            ).classes("text-xs text-gray-500")
            problem = ui.label().classes("text-xs text-warning")

            # The text we last put in the editor. Anything else in it is the user's,
            # and is what gets saved. Comparing against this is the whole of "has this
            # been hand-edited?" -- no dirty flag, no change handler, and no way for
            # the two to disagree.
            seeded = ""

            def show_file() -> None:
                """Fill the editor with the .env as it would be written.

                Done when the panel is opened rather than on every keystroke: a
                settings form is a handful of boxes, and re-rendering the file as a
                password is typed buys nothing anyone can see.

                Hand edits are never overwritten. A user who typed in here and then
                collapsed the panel by accident would not thank us for tidying their
                work away when they opened it again.
                """
                nonlocal seeded
                if not expansion.value or editor.value != seeded:
                    return
                try:
                    seeded = config.preview(widgets.values(bound))
                    problem.set_text("")
                except SettingsError as exc:
                    # The form has an answer we would refuse to write, so there are no
                    # bytes to show for it. The file as it stands is still worth
                    # showing -- and is still worth editing, which is how someone digs
                    # themselves out of a form they cannot satisfy.
                    seeded = config.read()
                    problem.set_text(f"Showing the file as it is on disk: {exc}")
                editor.set_value(seeded)

            expansion.on_value_change(show_file)

        async def save(apply: bool) -> None:
            # Two doors into one file, and the user's own bytes win. Both writes put
            # the old .env back if docker compose will not read the new one -- so a
            # bad save cannot leave a stack that will not start, whichever way it was
            # made.
            edited = editor.value != seeded
            try:
                if edited:
                    await asyncio.to_thread(config.write, editor.value)
                else:
                    await asyncio.to_thread(config.save, widgets.values(bound))
            except (SettingsError, OSError) as exc:
                ui.notify(str(exc), type="negative", multi_line=True, timeout=0)
                return
            ui.notify(f"Saved {config.env_file}", type="positive")
            dialog.submit("applied" if apply else "saved")

        with ui.row().classes("w-full justify-end items-center"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save", on_click=lambda: save(apply=False)).props("flat")
            # The one that does what the user actually came to do. A saved .env
            # changes nothing until compose reads it again, so a settings page
            # without this button is one that appears not to work: you change the
            # port, you save, and the app is still on the old one.
            ui.button(
                "Save & apply", icon="play_arrow", on_click=lambda: save(apply=True)
            ).props("color=primary").tooltip(
                "Save, then run docker compose up -d to restart with the new settings"
            )

    dialog.open()
    result = await dialog
    dialog.delete()
    return result


def _stack_card(stack: Stack, status: str, has_settings: bool, refresh) -> None:
    """One stack: its status, and the things you can do to it."""
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label(stack.name).classes("text-lg font-bold")
                ui.label(str(stack.compose_file)).classes("text-xs text-gray-500")
            ui.badge(status, color=STATUS_COLOURS.get(status, "grey"))

        async def act(command: Command, title: str) -> None:
            await _run_in_dialog(f"{stack.name} — {title}", command)
            refresh()

        async def configure() -> None:
            """Open the settings form, and do whatever the user asked for next.

            The `up` runs out here rather than inside the dialog, in the card's own
            slot -- the same slot every other command on this card is streamed into.
            """
            result = await _settings_dialog(stack)
            if result == "applied":
                await _run_in_dialog(f"{stack.name} — up", stack.up())
            if result is not None:
                refresh()

        async def running() -> list[str] | None:
            """The services you could get into, or None with the reason said out loud.

            Both exec and attach need a *running process*, so a stopped stack has
            nothing to offer either of them. Saying so is better than a menu with
            nothing in it, and much better than handing docker a service name it
            will only reject.
            """
            services = await asyncio.to_thread(stack.running_services)
            if not services:
                ui.notify(
                    f"{stack.name} has no running containers. Start it first.",
                    type="warning",
                )
                return None
            return services

        async def exec_() -> None:
            """Exec, supervised -- the one command here that cannot clean up after
            itself.

            `docker compose exec` orphans its process inside the container when the
            client goes away, and a closed browser tab is exactly that. So the
            session we *run* wears a pidfile, and the session we *show* does not:
            see :meth:`Stack.hangup`, which is also where the evidence lives.
            """
            if (services := await running()) is None:
                return
            chosen = await _exec_dialog(stack, services)
            if chosen is None:
                return
            service, argv = chosen

            pidfile = f"/tmp/.spiriconfig-exec-{uuid.uuid4().hex}"
            await _terminal_dialog(
                f"{stack.name} — exec {service}",
                stack.exec(service, argv, pidfile=pidfile),
                what="the command",
                shown=stack.exec(service, argv),
                reap=stack.hangup(service, pidfile),
            )
            refresh()

        async def attach() -> None:
            """Attach. Needs no supervision: it starts nothing, so it orphans nothing.

            The picker is shown even when there is only one service to pick. It costs
            a click on the common case, and it buys the two buttons behaving the same
            way as each other -- a dropdown that appears or does not depending on how
            many containers happen to be up is a UI you have to *learn*, and the first
            thing anybody does is go looking for the menu that is not there.
            """
            if (services := await running()) is None:
                return
            service = await _pick_service(f"{stack.name} — attach", services)
            if service is None:
                return
            await _terminal_dialog(
                f"{stack.name} — attach {service}",
                stack.attach(service),
                what="the attachment",
            )
            refresh()

        with ui.row().classes("gap-2"):
            ui.button("Up", icon="play_arrow", on_click=lambda: act(stack.up(), "up"))
            ui.button("Down", icon="stop", on_click=lambda: act(stack.down(), "down"))
            ui.button(
                "Restart", icon="restart_alt",
                on_click=lambda: act(stack.restart(), "restart"),
            ).props("flat")

            # Not advanced-only, and that is the point of the feature: an app
            # author decided which knobs are safe to turn, so turning them is an
            # ordinary thing for an ordinary user to do. Only shown for an app that
            # declares some -- a Settings button leading to an empty form would be
            # worse than no button at all.
            if has_settings:
                ui.button(
                    "Settings", icon="tune", on_click=configure
                ).props("flat")
            ui.button(
                "Pull", icon="download",
                on_click=lambda: act(stack.pull(), "pull"),
            ).props("flat")
            ui.button(
                "Logs", icon="article",
                on_click=lambda: act(stack.logs(), "logs"),
            ).props("flat")

            # Advanced only, all three. Hand-editing the compose file is the most
            # dangerous thing on this page; a prompt inside a container is the most
            # *bewildering* one, and neither is anything an ordinary user came here
            # for. All of it stays fully available from the shell --
            # `$EDITOR "$(spiriconfig docker config <stack>)"`,
            # `spiriconfig docker exec <stack> <service>` -- because hiding a button
            # is decluttering, not a permission.
            with advanced.only():
                ui.button("Exec", icon="terminal", on_click=exec_).props(
                    "flat"
                ).tooltip("Run a command in one of this app's containers")
                ui.button("Attach", icon="cable", on_click=attach).props(
                    "flat"
                ).tooltip("Attach to a container's main process, stdin and all")
                ui.button(
                    "Edit", icon="edit",
                    on_click=lambda: _edit_dialog(stack, refresh),
                ).props("flat")


async def _statuses(stacks: list[Stack]) -> dict[str, str]:
    """Fetch every stack's status concurrently, off the event loop.

    Each status is a `docker compose ps`, which is slow enough that doing them
    one after another is noticeable on a machine with a handful of stacks.
    """
    results = await asyncio.gather(
        *(asyncio.to_thread(s.status) for s in stacks),
        return_exceptions=True,
    )
    statuses = {}
    for stack, result in zip(stacks, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("could not get status for {!r}: {}", stack.name, result)
            statuses[stack.name] = "down"
        else:
            statuses[stack.name] = result
    return statuses


def page(settings: DockerSettings | None = None) -> None:
    """Render the docker plugin's page."""
    config = settings or docker_settings()

    ui.label("Apps").classes("text-2xl font-bold")
    ui.label(f"Compose projects in {config.compose_dir}").classes(
        "text-sm text-gray-500"
    )

    container = ui.column().classes("w-full gap-2")

    async def render() -> None:
        container.clear()
        stacks = await asyncio.to_thread(discover, config)
        statuses = await _statuses(stacks)

        # Which stacks have a settings form, worked out in one hop off the event
        # loop. It is a small YAML parse per stack, but it is a disk read per
        # stack, and the list is drawn on every refresh.
        settings_flags = await asyncio.to_thread(
            lambda: {s.name: app_settings.has_settings(s) for s in stacks}
        )

        with container:
            if not stacks:
                with ui.card().classes("w-full"):
                    ui.label("No compose projects found.").classes("text-lg")
                    ui.label(
                        f"Create a directory with a compose file in "
                        f"{config.compose_dir} and it will show up here."
                    ).classes("text-sm text-gray-500")
                return
            for stack in stacks:
                _stack_card(
                    stack,
                    statuses[stack.name],
                    settings_flags[stack.name],
                    refresh,
                )

    def refresh() -> None:
        """Re-render the stack list, shortly.

        The timer is pinned to the slot `container` lives in, and that is not a
        detail -- it is the whole bug.

        A NiceGUI event handler runs with the *clicked element's* slot active. So
        a timer created here during a button press becomes a child of `container`,
        because the button is inside a card inside `container`. Then `render()`
        runs, calls `container.clear()`, and deletes the very timer whose callback
        is executing -- which cancels it, half-done, immediately after the clear
        and before anything is put back. The page goes blank.

        The first render works only by accident: `page()` calls this from the page
        slot, outside the container, so that one timer survives.
        """
        with container.parent_slot:
            ui.timer(0.1, render, once=True)

    with ui.row().classes("items-center gap-2"):
        ui.button("Refresh", icon="refresh", on_click=refresh).props("flat")

    refresh()
