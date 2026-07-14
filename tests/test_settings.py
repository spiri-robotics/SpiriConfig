"""Tests for ``x-spiri-settings`` and the ``.env`` it writes.

The centre of gravity here is :class:`TestComposeAgreesWithOurQuoting`, which is
the only test in the file that proves anything about the real world. Everything
else asserts that our encoder does what we think it does; that one asserts that
what we think it does is what *docker compose* thinks it does, which is the only
opinion that counts once a password with a ``$`` in it is on the line.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spiriconfig_docker import env, settings, widgets
from spiriconfig_docker.config import DockerSettings
from spiriconfig_docker.settings import Field, SettingsError
from spiriconfig_docker.stacks import Stack

from tests.conftest import docker_required


class TestEncoding:
    """Turning a value into a line a ``.env`` can hold.

    The rules were established by asking docker compose (see
    :class:`TestComposeAgreesWithOurQuoting`); these tests pin the encoder to them
    without paying for a subprocess every time.
    """

    def test_boring_values_are_left_bare(self) -> None:
        """Ports, tags, and booleans are most of what a form writes, and a .env
        full of needless quotes is a .env nobody wants to hand-edit."""
        assert env.encode("3000") == "3000"
        assert env.encode("true") == "true"
        assert env.encode("grafana/grafana:11.1.0") == "grafana/grafana:11.1.0"

    def test_spaces_are_quoted(self) -> None:
        assert env.encode("two words") == "'two words'"

    def test_a_dollar_is_quoted_so_compose_does_not_expand_it(self) -> None:
        """Unquoted, compose reads `a$bc` as `a` -- it expands `$bc` and finds
        nothing. That is a password silently truncated to one character."""
        assert env.encode("a$bc") == "'a$bc'"

    def test_a_hash_is_quoted_so_it_is_not_read_as_a_comment(self) -> None:
        assert env.encode("pass # word") == "'pass # word'"

    def test_an_apostrophe_falls_back_to_double_quotes(self) -> None:
        """The one case single quotes cannot express: compose has no escape for a
        single quote inside a single-quoted value, and errors on the whole file."""
        assert env.encode("it's") == '"it\'s"'

    def test_double_quoted_values_escape_what_compose_would_eat(self) -> None:
        assert env.encode("it's $HOME") == '"it\'s \\$HOME"'
        assert env.encode('it\'s "quoted"') == '"it\'s \\"quoted\\""'

    def test_empty_is_empty(self) -> None:
        assert env.encode("") == ""


class TestScanning:
    def test_reads_the_obvious_things(self) -> None:
        text = "A=1\nB=two words\nC='quoted'\n"
        assert env.values(text) == {"A": "1", "B": "two words", "C": "quoted"}

    def test_ignores_comments_and_blank_lines(self) -> None:
        assert env.values("# a comment\n\nA=1\n") == {"A": "1"}

    def test_strips_an_inline_comment_from_an_unquoted_value(self) -> None:
        """A `#` only starts a comment when whitespace precedes it, which is why a
        password of `pa#ss` survives and a trailing note does not."""
        assert env.values("A=3000 # the port\n") == {"A": "3000"}
        assert env.values("A=pa#ss\n") == {"A": "pa#ss"}

    def test_single_quotes_are_literal(self) -> None:
        assert env.values("A='$HOME # not a comment'\n") == {"A": "$HOME # not a comment"}

    def test_double_quotes_honour_escapes(self) -> None:
        assert env.values('A="a \\$b \\"c\\""\n') == {"A": 'a $b "c"'}

    def test_the_last_assignment_wins_as_it_does_for_compose(self) -> None:
        assert env.values("A=1\nA=2\n") == {"A": "2"}

    def test_a_quoted_value_may_span_lines(self) -> None:
        """A hand-written .env can hold a certificate. If we mistook this for a
        one-line value, patch() would replace the first line and leave the rest as
        loose garbage."""
        text = 'CERT="line1\nline2"\nAFTER=1\n'
        assert env.values(text) == {"CERT": "line1\nline2", "AFTER": "1"}

    def test_an_unterminated_quote_does_not_swallow_the_rest_of_the_file(self) -> None:
        """Compose refuses a file like this outright. We must not 'helpfully' read
        it as one enormous value and then rewrite it that way."""
        assert env.values("A='unterminated\nB=2\n") == {"B": "2"}


class TestPatching:
    def test_replaces_a_value_in_place(self) -> None:
        assert env.patch("A=1\nB=2\n", {"A": "9"}) == "A=9\nB=2\n"

    def test_keeps_comments_and_variables_that_are_not_ours(self) -> None:
        """The .env is the user's file. We are a guest in it."""
        text = "# my note\nMINE=keep\nA=1\n"
        assert env.patch(text, {"A": "9"}) == "# my note\nMINE=keep\nA=9\n"

    def test_appends_a_key_that_is_not_there_yet(self) -> None:
        assert env.patch("A=1\n", {"B": "2"}) == "A=1\nB=2\n"

    def test_an_empty_value_removes_the_line(self) -> None:
        """Clearing a box means "I do not want to set this", so the app falls back
        to whatever its compose file says to do without it."""
        assert env.patch("A=1\nB=2\n", {"A": ""}) == "B=2\n"

    def test_replaces_the_whole_span_of_a_multi_line_value(self) -> None:
        text = 'CERT="line1\nline2"\nAFTER=1\n'
        assert env.patch(text, {"CERT": "short"}) == "CERT=short\nAFTER=1\n"

    def test_a_rewritten_duplicate_leaves_no_dead_earlier_line(self) -> None:
        """The last assignment is the one in effect, so an earlier one left behind
        would show the user a value that is not being used."""
        assert env.patch("A=1\nB=2\nA=3\n", {"A": "9"}) == "B=2\nA=9\n"

    def test_leaves_everything_alone_when_asked_for_nothing(self) -> None:
        text = "# note\nA=1\n"
        assert env.patch(text, {}) == text

    def test_repeated_saves_do_not_grow_a_gap_at_the_bottom(self) -> None:
        """A separator before appended keys looks tidier on the first save and is a
        bug on the second: last save's additions are ordinary lines now, so the file
        would gain a fresh hole every time it was written."""
        once = env.patch("A=1\n", {"B": "2"})
        twice = env.patch(once, {"C": "3"})
        assert twice == "A=1\nB=2\nC=3\n"


@docker_required
class TestComposeAgreesWithOurQuoting:
    """The test that makes the rest of the file mean something.

    Everything above asserts our encoder produces a particular string. This one
    hands that string to the actual `docker compose` and asks what it read back --
    which is the only way to know that a password containing ``$``, ``#``, a space,
    and an apostrophe survives the trip into a container.

    It is the same instinct as the test that pipes ``str(Command)`` through a real
    shell: the value of a serialiser is entirely in whether the thing on the far
    end agrees with it.
    """

    #: Values chosen to break a naive encoder, one way each.
    NASTY = [
        "plain",
        "two words",
        "dollar$sign",
        "hash#mark",
        "spaced # hash",
        "it's",
        "it's $a #mess \"really\"",
        "trailing ",
        "$",
        "100%",
        "back\\slash",
    ]

    @pytest.mark.parametrize("value", NASTY)
    def test_compose_reads_back_exactly_what_we_wrote(
        self, value: str, tmp_path: Path
    ) -> None:
        """Written by us, parsed by compose, and compared to what we started with.

        The oracle is `config --environment`, which prints the project environment
        compose resolved from the .env. Not `config` itself: its output is *itself a
        compose file*, so it re-escapes a literal `$` as `$$` on the way out, and a
        test asserting on that would be measuring compose's serialiser rather than
        its parser -- and would fail on a correctly-encoded value.
        """
        (tmp_path / "compose.yaml").write_text(
            "services:\n  app:\n    image: alpine:latest\n"
        )
        (tmp_path / ".env").write_text(f"VALUE={env.encode(value)}\n")

        result = subprocess.run(
            ["docker", "compose", "config", "--environment"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr

        # Read the line raw: no strip(), or a value whose whole point is a trailing
        # space would pass a test that had quietly removed it.
        prefix = "VALUE="
        read_back = [
            line[len(prefix) :]
            for line in result.stdout.split("\n")
            if line.startswith(prefix)
        ]
        assert read_back == [value]


class TestDeclaring:
    def _fields(self, tmp_path: Path, body: str) -> list[Field]:
        compose = tmp_path / "compose.yaml"
        compose.write_text(body)
        return settings.declared(compose)

    def test_an_app_with_no_settings_is_not_an_error(self, tmp_path: Path) -> None:
        """Which is most apps. `whoami` has nothing to configure and should not
        have to say so."""
        assert self._fields(tmp_path, "services:\n  a:\n    image: alpine\n") == []

    def test_reads_a_field(self, tmp_path: Path) -> None:
        fields = self._fields(
            tmp_path,
            "x-spiri-settings:\n"
            "  - env: PORT\n"
            "    widget: number\n"
            "    label: HTTP port\n"
            "    help: The port it listens on.\n"
            "    default: 3000\n"
            "    min: 1\n"
            "    max: 65535\n"
            "services:\n  a:\n    image: alpine\n",
        )
        assert len(fields) == 1
        field = fields[0]
        assert field.env == "PORT"
        assert field.widget == "number"
        assert field.title == "HTTP port"
        assert field.help == "The port it listens on."
        assert field.min == 1
        assert field.max == 65535

    def test_a_yaml_number_default_becomes_the_string_a_dotenv_holds(
        self, tmp_path: Path
    ) -> None:
        """`default: 3000` is the author writing what they meant. It should not
        become a type error three modules away."""
        fields = self._fields(
            tmp_path,
            "x-spiri-settings:\n  - env: PORT\n    default: 3000\nservices:\n  a:\n    image: alpine\n",
        )
        assert fields[0].default == "3000"

    def test_a_yaml_boolean_default_is_spelled_the_way_containers_spell_it(
        self, tmp_path: Path
    ) -> None:
        """`str(True)` is `"True"`, which is not how anything in a container writes
        a boolean."""
        fields = self._fields(
            tmp_path,
            "x-spiri-settings:\n"
            "  - env: ANON\n"
            "    widget: switch\n"
            "    default: true\n"
            "services:\n  a:\n    image: alpine\n",
        )
        assert fields[0].default == "true"

    def test_a_missing_label_falls_back_to_something_readable(
        self, tmp_path: Path
    ) -> None:
        fields = self._fields(
            tmp_path,
            "x-spiri-settings:\n  - env: ADMIN_PASSWORD\nservices:\n  a:\n    image: alpine\n",
        )
        assert fields[0].title == "Admin password"

    def test_a_field_with_no_env_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SettingsError, match="needs an `env:`"):
            self._fields(
                tmp_path,
                "x-spiri-settings:\n  - label: Nameless\nservices:\n  a:\n    image: alpine\n",
            )

    def test_an_unknown_widget_says_which_ones_exist(self, tmp_path: Path) -> None:
        """The error is read by whoever wrote the compose file, and `widgit:` is a
        typo they can fix in ten seconds if we tell them the alternatives."""
        with pytest.raises(SettingsError, match="does not exist"):
            self._fields(
                tmp_path,
                "x-spiri-settings:\n"
                "  - env: A\n    widget: dropdown\n"
                "services:\n  a:\n    image: alpine\n",
            )

    def test_a_choice_widget_without_options_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SettingsError, match="needs `options:`"):
            self._fields(
                tmp_path,
                "x-spiri-settings:\n"
                "  - env: LEVEL\n    widget: select\n"
                "services:\n  a:\n    image: alpine\n",
            )

    def test_an_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        """A silently ignored `helptext:` is an author wondering why their help
        text never shows up."""
        with pytest.raises(SettingsError, match="unknown keys"):
            self._fields(
                tmp_path,
                "x-spiri-settings:\n"
                "  - env: A\n    helptext: oops\n"
                "services:\n  a:\n    image: alpine\n",
            )

    def test_two_widgets_writing_one_variable_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SettingsError, match="twice"):
            self._fields(
                tmp_path,
                "x-spiri-settings:\n"
                "  - env: PORT\n"
                "  - env: PORT\n"
                "services:\n  a:\n    image: alpine\n",
            )


class TestTheSidecar:
    """A settings form can live in its own file, beside the compose file.

    The reason is the app store. An app is usually somebody else's compose file,
    copied in as published, and every line a maintainer edits is a line they get to
    re-merge by hand each time upstream moves. A sidecar adds a form while touching
    nothing.
    """

    FIELDS = "- env: PORT\n  widget: number\n  default: 3000\n"
    PLAIN_COMPOSE = "services:\n  a:\n    image: alpine\n"

    def _app(
        self,
        tmp_path: Path,
        compose: str,
        sidecar: str | None = None,
        sidecar_name: str = "spiri-settings.yaml",
    ) -> Path:
        """An app directory, and the path to its compose file."""
        compose_file = tmp_path / "compose.yaml"
        compose_file.write_text(compose)
        if sidecar is not None:
            (tmp_path / sidecar_name).write_text(sidecar)
        return compose_file

    def test_a_sidecar_is_found_with_no_compose_key_at_all(self, tmp_path: Path) -> None:
        """The whole point: the compose file is untouched, byte for byte."""
        compose_file = self._app(tmp_path, self.PLAIN_COMPOSE, sidecar=self.FIELDS)

        fields = settings.declared(compose_file)
        assert [f.env for f in fields] == ["PORT"]
        assert fields[0].default == "3000"
        assert compose_file.read_text() == self.PLAIN_COMPOSE

    def test_a_sidecar_may_be_a_pasted_compose_block(self, tmp_path: Path) -> None:
        """So a maintainer whose settings block has outgrown the compose file can cut
        it out and paste it into a file, unchanged, and have it work."""
        compose_file = self._app(
            tmp_path,
            self.PLAIN_COMPOSE,
            sidecar="x-spiri-settings:\n  - env: PORT\n    widget: number\n",
        )
        assert [f.env for f in settings.declared(compose_file)] == ["PORT"]

    def test_the_compose_file_can_name_the_sidecar(self, tmp_path: Path) -> None:
        compose_file = self._app(
            tmp_path,
            f"x-spiri-settings: knobs.yaml\n{self.PLAIN_COMPOSE}",
            sidecar=self.FIELDS,
            sidecar_name="knobs.yaml",
        )
        assert [f.env for f in settings.declared(compose_file)] == ["PORT"]

    def test_declaring_settings_twice_is_an_error(self, tmp_path: Path) -> None:
        """Two sources of truth that can disagree is the thing this project exists
        not to do. A stale sidecar next to a new inline block should be reported,
        not silently resolved in favour of one of them."""
        compose_file = self._app(
            tmp_path,
            f"x-spiri-settings:\n  - env: PORT\n{self.PLAIN_COMPOSE}",
            sidecar=self.FIELDS,
        )
        with pytest.raises(SettingsError, match="one place or the other"):
            settings.declared(compose_file)

    def test_a_sidecar_cannot_reach_outside_the_app(self, tmp_path: Path) -> None:
        """A compose file can arrive from a store, over the network. So this is a
        line somebody could write, and it must not be one that makes us read a file
        and put it on a web page."""
        compose_file = self._app(
            tmp_path, f"x-spiri-settings: ../../../etc/passwd\n{self.PLAIN_COMPOSE}"
        )
        with pytest.raises(SettingsError, match="outside the app's directory"):
            settings.declared(compose_file)

    def test_a_named_sidecar_that_does_not_exist_says_so(self, tmp_path: Path) -> None:
        compose_file = self._app(
            tmp_path, f"x-spiri-settings: missing.yaml\n{self.PLAIN_COMPOSE}"
        )
        with pytest.raises(SettingsError, match="does not exist"):
            settings.declared(compose_file)

    def test_a_broken_field_in_a_sidecar_names_the_sidecar(self, tmp_path: Path) -> None:
        """With three places a form can come from, "which file is this in?" has
        become a real question, and the error should not make the author guess."""
        compose_file = self._app(
            tmp_path, self.PLAIN_COMPOSE, sidecar="- env: A\n  widget: nonsense\n"
        )
        with pytest.raises(SettingsError, match=r"spiri-settings\.yaml\[0\]"):
            settings.declared(compose_file)


class TestValues:
    def test_defaults_show_when_the_env_file_is_missing(self, configurable: Stack) -> None:
        """The state of an app nobody has configured yet, which is every app the
        moment it is installed."""
        current = settings.for_stack(configurable).values()
        assert current["GREETING"] == "hello"
        assert current["PORT"] == "8080"

    def test_the_env_file_wins_over_the_default(self, configurable: Stack) -> None:
        (configurable.path / ".env").write_text("GREETING=hi\n")
        current = settings.for_stack(configurable).values()
        assert current["GREETING"] == "hi"
        assert current["PORT"] == "8080"

    def test_only_declared_fields_are_reported(self, configurable: Stack) -> None:
        """Whatever else the user keeps in their .env is theirs, and none of the
        form's business."""
        (configurable.path / ".env").write_text("GREETING=hi\nMINE=private\n")
        assert "MINE" not in settings.for_stack(configurable).values()


class TestValidation:
    """The rules are enforced on the way to the file, not only in the widgets, so
    that the CLI gets the same guarantees the web UI does."""

    def _save(self, stack: Stack, values: dict[str, str]) -> None:
        settings.for_stack(stack).save(values)

    def test_a_required_field_may_not_be_emptied(self, configurable: Stack) -> None:
        with pytest.raises(SettingsError, match="required"):
            self._save(configurable, {"SECRET": ""})

    def test_a_choice_must_be_one_of_the_options(self, configurable: Stack) -> None:
        with pytest.raises(SettingsError, match="must be one of"):
            self._save(configurable, {"LEVEL": "shouting"})

    def test_an_undeclared_key_is_dropped_rather_than_written(
        self, configurable: Stack
    ) -> None:
        """The caller is a form we generated from the schema, so a key that is not
        in it is our bug -- and putting it in the user's .env is the worst possible
        way to find out about it."""
        checked = settings.for_stack(configurable)._checked({"NOPE": "x"})
        assert checked == {}


@docker_required
class TestSaving:
    """Saving asks docker compose to read the file back before it commits to it --
    the same bargain `Stack.write` makes for the compose file, and for the same
    reason: a .env compose cannot read makes the app unstartable, including by hand
    from a shell, which is the escape hatch that must never close.
    """

    def test_writes_the_declared_values(self, configurable: Stack) -> None:
        stack_settings = settings.for_stack(configurable)
        stack_settings.save({"GREETING": "hi", "PORT": "9000"})

        written = env.read(stack_settings.env_file)
        assert written["GREETING"] == "hi"
        assert written["PORT"] == "9000"

    def test_a_new_env_file_says_what_it_is(self, configurable: Stack) -> None:
        stack_settings = settings.for_stack(configurable)
        stack_settings.save({"GREETING": "hi"})
        assert "editable by hand" in stack_settings.env_file.read_text()

    def test_the_header_is_not_written_again_over_a_file_that_exists(
        self, configurable: Stack
    ) -> None:
        """Rewriting a header into a file the user already owns would be exactly
        the sort of helpful vandalism this project is against."""
        stack_settings = settings.for_stack(configurable)
        stack_settings.save({"GREETING": "hi"})
        stack_settings.save({"GREETING": "hello again"})
        assert stack_settings.env_file.read_text().count("editable by hand") == 1

    def test_a_users_own_variables_and_comments_survive(self, configurable: Stack) -> None:
        stack_settings = settings.for_stack(configurable)
        stack_settings.env_file.write_text("# mine\nMINE=keep\nGREETING=old\n")

        stack_settings.save({"GREETING": "new"})

        text = stack_settings.env_file.read_text()
        assert "# mine" in text
        assert "MINE=keep" in text
        assert "GREETING=new" in text

    def test_a_value_with_a_dollar_in_it_survives_the_round_trip(
        self, configurable: Stack
    ) -> None:
        """The bug this whole encoder exists to prevent: written bare, compose
        expands `$ecret` to nothing and the password becomes `p`."""
        stack_settings = settings.for_stack(configurable)
        stack_settings.save({"SECRET": "p$ecret w0rd#!"})
        assert env.read(stack_settings.env_file)["SECRET"] == "p$ecret w0rd#!"

    #: A value that encodes to a perfectly good .env line and still makes compose
    #: refuse the project: it lands in a `ports:` mapping, and `abc` is not a port.
    #:
    #: Finding this took a moment, and the reason is worth keeping. Almost nothing a
    #: user can type can break a compose file, because compose interpolates *after*
    #: parsing the YAML -- so a quote or a colon in a value lands harmlessly inside
    #: an already-parsed string. It is only where a value has to *mean* something to
    #: compose, like a port, that it can be wrong. The web form's `number` widget
    #: will not offer this, but `spiriconfig docker settings x PORT=abc` will, which
    #: is exactly the hole the guard exists to plug.
    NOT_A_PORT = "abc"

    def test_a_rejected_file_is_put_back_exactly_as_it_was(
        self, configurable: Stack
    ) -> None:
        """A save compose will not read must leave the previous, working file --
        not a broken one, and not a half-written one."""
        stack_settings = settings.for_stack(configurable)
        stack_settings.env_file.write_text("PORT=9000\n")

        with pytest.raises(SettingsError, match="rejected"):
            stack_settings.save({"PORT": self.NOT_A_PORT})

        assert stack_settings.env_file.read_text() == "PORT=9000\n"

    def test_a_rejected_first_save_leaves_no_env_file_behind(
        self, configurable: Stack
    ) -> None:
        """Restoring "how it was" has to include "there was not one"."""
        stack_settings = settings.for_stack(configurable)
        assert not stack_settings.env_file.exists()

        with pytest.raises(SettingsError, match="rejected"):
            stack_settings.save({"PORT": self.NOT_A_PORT})

        assert not stack_settings.env_file.exists()


class TestReadingAndPreviewingTheFile:
    """What the advanced editor is filled with. No docker needed for either -- these
    are the two questions asked before anything is written."""

    def test_reading_an_app_nobody_has_configured_gives_an_empty_buffer(
        self, configurable: Stack
    ) -> None:
        assert settings.for_stack(configurable).read() == ""

    def test_reading_gives_the_file_byte_for_byte(self, configurable: Stack) -> None:
        stack_settings = settings.for_stack(configurable)
        stack_settings.env_file.write_text("# mine\nMINE=keep\n")
        assert stack_settings.read() == "# mine\nMINE=keep\n"

    def test_the_preview_is_what_would_actually_be_written(
        self, configurable: Stack
    ) -> None:
        """Including the header on a file that does not exist yet. The editor is
        seeded with the preview and writes back whatever it then holds, so a preview
        that differed from the write by so much as a header would be a preview that
        lied the moment somebody edited it."""
        stack_settings = settings.for_stack(configurable)
        preview = stack_settings.preview({"GREETING": "hi"})

        assert "editable by hand" in preview
        assert "GREETING=hi" in preview


@docker_required
class TestWritingTheFileDirectly:
    """`write` is the advanced editor's door into the same file: bytes in, bytes on
    disk, checked by the same `docker compose config` a form save is checked by.

    The form's schema does not apply -- that is the feature. A hand-edited file is
    the user's, and the app author's idea of which knobs exist is a default, not a
    cage.
    """

    def test_the_bytes_go_down_as_they_were_typed(self, configurable: Stack) -> None:
        stack_settings = settings.for_stack(configurable)
        text = "# hand written\nGREETING=typed\nPORT=9001\n"

        stack_settings.write(text)

        assert stack_settings.env_file.read_text() == text

    def test_a_variable_the_form_never_declared_is_written_anyway(
        self, configurable: Stack
    ) -> None:
        """The reason to offer a text editor at all: wanting a variable the app
        author did not think to declare is not a mistake to be corrected."""
        stack_settings = settings.for_stack(configurable)
        stack_settings.write("UNDECLARED=mine\n")

        assert env.read(stack_settings.env_file)["UNDECLARED"] == "mine"

    def test_the_schema_is_not_enforced_over_a_hand_edited_file(
        self, configurable: Stack
    ) -> None:
        """`SECRET` is `required:` in the form, and emptying it through the form is
        refused. Deleting the line from the file is not the form, and compose is
        perfectly happy with it -- so it is allowed, and the app falls back to the
        `:-` default it was written with."""
        stack_settings = settings.for_stack(configurable)
        stack_settings.write("GREETING=alone\n")

        assert "SECRET" not in stack_settings.env_file.read_text()

    def test_no_header_is_added_to_a_file_the_user_typed(
        self, configurable: Stack
    ) -> None:
        stack_settings = settings.for_stack(configurable)
        stack_settings.write("GREETING=typed\n")

        assert "editable by hand" not in stack_settings.env_file.read_text()

    def test_a_rejected_file_is_put_back_exactly_as_it_was(
        self, configurable: Stack
    ) -> None:
        """The same guarantee a form save makes, because it is the same guarantee:
        an editor that could leave an unstartable app behind would be a worse tool
        than the vim it is standing in for."""
        stack_settings = settings.for_stack(configurable)
        stack_settings.env_file.write_text("PORT=9000\n")

        with pytest.raises(SettingsError, match="rejected"):
            stack_settings.write("PORT=abc\n")

        assert stack_settings.env_file.read_text() == "PORT=9000\n"

    def test_a_rejected_first_write_leaves_no_env_file_behind(
        self, configurable: Stack
    ) -> None:
        stack_settings = settings.for_stack(configurable)
        assert not stack_settings.env_file.exists()

        with pytest.raises(SettingsError, match="rejected"):
            stack_settings.write("PORT=abc\n")

        assert not stack_settings.env_file.exists()


class TestTheWidgetRegistry:
    def test_every_declarable_widget_can_actually_be_built(self) -> None:
        """The schema validates `widget:` against one list and the page builds it
        from another. If they drift, an app declares a widget that passes
        validation and then explodes at render time, in front of the user."""
        assert set(widgets.REGISTRY) == set(settings.WIDGETS)

    def test_a_number_is_written_without_the_float_tail(self) -> None:
        """ui.number always yields a float, so a port comes back as 3000.0. Written
        out verbatim that gives `ports: "3000.0:3000"`, which docker rejects."""
        assert widgets._from_number(3000.0) == "3000"
        assert widgets._from_number(1.5) == "1.5"
        assert widgets._from_number(None) == ""

    def test_booleans_are_read_generously_and_written_strictly(self) -> None:
        """A user who hand-edited their .env to say `yes` meant yes, and a switch
        that showed it as off would be lying to them."""
        field = Field(env="X", widget="switch")
        assert widgets._to_bool("yes", field) is True
        assert widgets._to_bool("TRUE", field) is True
        assert widgets._to_bool("false", field) is False
        assert widgets._to_bool("", field) is False

        assert widgets._from_bool(True) == "true"
        assert widgets._from_bool(False) == "false"


class TestBrokenSettingsDoNotTakeThePageDown:
    def test_has_settings_is_false_for_a_broken_declaration(
        self, compose_dir: Path
    ) -> None:
        """`has_settings` is called while drawing a list of every stack. One app
        with a typo in it must not blank the whole page -- the error is raised
        properly when someone opens *that* app, which is when they can act on it."""
        project = compose_dir / "broken"
        project.mkdir()
        (project / "compose.yaml").write_text(
            "x-spiri-settings:\n"
            "  - env: A\n    widget: nonsense\n"
            "services:\n  a:\n    image: alpine\n"
        )
        from spiriconfig_docker.stacks import get

        stack = get(DockerSettings(compose_dir=compose_dir), "broken")
        assert settings.has_settings(stack) is False

        with pytest.raises(SettingsError):
            settings.for_stack(stack)
