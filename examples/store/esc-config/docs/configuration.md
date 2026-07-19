# Configuration

Every knob an example has is an environment variable, read once at startup by that
example's `Settings` class (e.g.
`esc_config.nicegui_app.Settings`).
There are two views of the same list.

## The settings form (`x-spiri-settings`)

`compose.yaml` declares the variables under `x-spiri-settings`. SpiriConfig turns
that block into a form; each entry names a variable and the widget to edit it:

```yaml
x-spiri-settings:
  - env: ESC_CONFIG_GREETING
    widget: input
    label: Greeting
    help: The message the app shows.
    default: "Hello from ESC Config"
```

Common fields:

`env`
: The environment variable this widget sets. Required.

`widget`
: The NiceGUI widget to edit the value, named not inferred. One of `input`,
  `textarea`, `number`, `password`, `checkbox`, `switch`, `toggle`, `select`,
  `radio`, `slider`, `color`. A plain text field is `input` (the default).

`label`, `help`
: What the person filling in the form sees.

`default`
: What the form pre-fills. Note this is the *form's* default; the app's own
  fallback is the `${VAR:-...}` in the `services:` block, so the container still
  starts before anyone has opened the form.

`advanced: true`
: Hides the field unless SpiriConfig's Advanced switch is on. The variable is
  still read and still settable from the CLI -- it is only off the default form.

## Keeping the two in step

Add a variable to an example's `Settings`, and add the matching entry to
`x-spiri-settings` (and a `${VAR:-default}` in that service if the app should run
without the form). If they disagree, the app reads a variable nobody can set, or
the form offers one the app ignores.

## Defaults

| Variable | Default | Meaning |
| --- | --- | --- |
| `ESC_CONFIG_GREETING` | `Hello from ESC Config` | The message the app shows. |
