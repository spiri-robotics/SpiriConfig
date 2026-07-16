"""Tests for app-store host logins.

Two things are worth pinning down. One is that we read a host out of every shape
a git remote comes in, because that host is the single key that makes one token
cover both git and the registry. The other is that a login round-trips through
real ``git``: what ``store_credentials`` writes, git can fill and ``logins`` can
list, and ``forget_credentials`` takes back out -- a credential that stored but
did not fill would look configured and still fail the clone.

The docker half is stubbed, not run: ``docker login``/``logout`` need a registry
to talk to, which a unit test has no business standing up. We assert we *called*
it with the right host; the secret-on-stdin channel it shares with git is covered
in test_commands.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spiriconfig.commands import Command, run
from spiriconfig_appstore import credentials
from spiriconfig_appstore.config import AppStoreSettings
from spiriconfig_appstore.credentials import (
    CredentialError,
    Endpoint,
    endpoint_for,
    forget_credentials,
    logins,
    store_credentials,
)


class TestEndpointFor:
    def test_https_url(self) -> None:
        assert endpoint_for("https://gitea.example.com/org/store.git") == Endpoint(
            scheme="https", host="gitea.example.com"
        )

    def test_https_url_with_port_keeps_the_bare_host(self) -> None:
        # git matches a credential on the host, not the authority, so the port
        # does not belong in the key.
        assert endpoint_for("https://gitea.example.com:3000/org/store.git") == Endpoint(
            scheme="https", host="gitea.example.com"
        )

    def test_scp_like_remote(self) -> None:
        assert endpoint_for("git@gitea.example.com:org/store.git") == Endpoint(
            scheme="ssh", host="gitea.example.com"
        )

    def test_ssh_url(self) -> None:
        assert endpoint_for("ssh://git@gitea.example.com/org/store.git") == Endpoint(
            scheme="ssh", host="gitea.example.com"
        )

    def test_a_local_path_has_no_endpoint(self) -> None:
        assert endpoint_for("/srv/stores/example") is None
        assert endpoint_for("./test_data/example-store") is None

    def test_nonsense_has_no_endpoint(self) -> None:
        assert endpoint_for("") is None
        assert endpoint_for("not a url") is None


@pytest.fixture
def settings(tmp_path: Path) -> AppStoreSettings:
    return AppStoreSettings(stores=[], store_dir=tmp_path / "stores")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point git's credential store at a throwaway HOME.

    The ``store`` helper writes ``$HOME/.git-credentials``; XDG is cleared so it
    cannot fall through to the developer's real one. Both together mean these
    tests read and write nothing outside tmp_path.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.fixture
def docker_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Stub docker login/logout, recording what they were asked to do.

    Neither can run without a registry, and neither is what these tests are
    about -- but *that* they are invoked, with the host, is part of the contract
    (one login covers git and the registry), so we record rather than skip.
    """
    calls: dict[str, list] = {"login": [], "logout": []}
    monkeypatch.setattr(
        credentials, "_docker_login",
        lambda host, user, token: calls["login"].append((host, user, token)),
    )
    monkeypatch.setattr(
        credentials, "_docker_logout", lambda host: calls["logout"].append(host)
    )
    return calls


def _fill(host: str) -> dict[str, str]:
    """Ask git for the stored credential the way a clone would, via the helper."""
    result = run(
        Command(argv=["git", "-c", "credential.helper=store", "credential", "fill"]),
        input=f"protocol=https\nhost={host}\n\n",
    )
    fields = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            fields[key] = value
    return fields


class TestStoreCredentials:
    def test_a_stored_credential_fills_back(
        self, settings: AppStoreSettings, isolated_home: Path, docker_calls: dict
    ) -> None:
        """The round trip a clone depends on: what we store, git can fill."""
        store_credentials(settings, "gitea.example.com", "alex", "tok-123")

        filled = _fill("gitea.example.com")
        assert filled["username"] == "alex"
        assert filled["password"] == "tok-123"

    def test_it_also_logs_docker_in(
        self, settings: AppStoreSettings, docker_calls: dict
    ) -> None:
        """One login, both stores: the registry is logged in under the same host."""
        store_credentials(settings, "gitea.example.com", "alex", "tok-123")
        assert docker_calls["login"] == [("gitea.example.com", "alex", "tok-123")]

    def test_ssh_skips_git_but_still_logs_docker_in(
        self, settings: AppStoreSettings, isolated_home: Path, docker_calls: dict
    ) -> None:
        """An ssh remote authenticates with a key, so there is no git secret to
        keep -- but its registry is still https, so docker is logged in."""
        store_credentials(settings, "gitea.example.com", "alex", "tok", scheme="ssh")
        assert not (isolated_home / ".git-credentials").exists()
        assert docker_calls["login"] == [("gitea.example.com", "alex", "tok")]

    def test_an_empty_host_is_refused(
        self, settings: AppStoreSettings, docker_calls: dict
    ) -> None:
        with pytest.raises(CredentialError, match="host is required"):
            store_credentials(settings, "  ", "alex", "tok")

    def test_a_missing_username_or_token_is_refused(
        self, settings: AppStoreSettings, docker_calls: dict
    ) -> None:
        with pytest.raises(CredentialError, match="username and a token"):
            store_credentials(settings, "gitea.example.com", "", "tok")
        with pytest.raises(CredentialError, match="username and a token"):
            store_credentials(settings, "gitea.example.com", "alex", "")


class TestLogins:
    def test_lists_what_was_stored(
        self, settings: AppStoreSettings, docker_calls: dict
    ) -> None:
        store_credentials(settings, "gitea.example.com", "alex", "tok")
        store_credentials(settings, "registry.acme.io", "svc", "tok2")

        found = {(login.host, login.username) for login in logins()}
        assert ("gitea.example.com", "alex") in found
        assert ("registry.acme.io", "svc") in found

    def test_the_token_is_not_surfaced(
        self, settings: AppStoreSettings, docker_calls: dict
    ) -> None:
        """The list is for managing logins, not reading secrets back out."""
        store_credentials(settings, "gitea.example.com", "alex", "sup3rsecret")
        assert all("sup3rsecret" not in str(login) for login in logins())

    def test_no_logins_is_an_empty_list_not_an_error(self) -> None:
        assert logins() == []


class TestForgetCredentials:
    def test_it_removes_the_login(
        self, settings: AppStoreSettings, docker_calls: dict
    ) -> None:
        store_credentials(settings, "gitea.example.com", "alex", "tok")
        assert any(login.host == "gitea.example.com" for login in logins())

        forget_credentials(settings, "gitea.example.com")
        assert all(login.host != "gitea.example.com" for login in logins())
        assert docker_calls["logout"] == ["gitea.example.com"]

    def test_forgetting_leaves_other_hosts_alone(
        self, settings: AppStoreSettings, docker_calls: dict
    ) -> None:
        store_credentials(settings, "gitea.example.com", "alex", "tok")
        store_credentials(settings, "registry.acme.io", "svc", "tok2")

        forget_credentials(settings, "gitea.example.com")
        remaining = {login.host for login in logins()}
        assert remaining == {"registry.acme.io"}

    def test_forgetting_an_unknown_host_is_not_an_error(
        self, settings: AppStoreSettings, docker_calls: dict
    ) -> None:
        forget_credentials(settings, "never-logged-in.example.com")
        assert docker_calls["logout"] == ["never-logged-in.example.com"]
