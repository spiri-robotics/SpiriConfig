"""Tests for the PAM login policy.

The interesting thing about :func:`spiriconfig.auth.authenticate` is not that it
calls PAM -- it is *whom it refuses to even ask PAM about*, and that is a decision
we make before libpam is loaded. So these tests fake PAM entirely and assert on
the gate in front of it: who reaches the check, who is turned away first, and
crucially that the turned-away never reach it. The real PAM call is exercised by
running the app against the host's stack, not here; there is no value in testing
that a stub we wrote returns what we told it to.
"""

from __future__ import annotations

import sys
import types

import pytest

from spiriconfig import auth
from spiriconfig.config import Settings


def _config(**overrides: object) -> Settings:
    return Settings(**overrides)


def _fake_pam(monkeypatch: pytest.MonkeyPatch, *, accepts: bool) -> list[tuple]:
    """Install a stand-in ``pamela`` so ``authenticate`` loads it instead of libpam.

    Returns the list the fake records its calls into, so a test can assert that
    PAM was -- or, more to the point, was *not* -- consulted.
    """
    calls: list[tuple] = []

    class PAMError(Exception):
        pass

    def authenticate(username: str, password: str, service: str = "login") -> None:
        calls.append((username, password, service))
        if not accepts:
            raise PAMError("Authentication failure")

    module = types.ModuleType("pamela")
    module.PAMError = PAMError
    module.authenticate = authenticate
    monkeypatch.setitem(sys.modules, "pamela", module)
    return calls


class TestNonRootMayOnlyLogInItsOwnUser:
    """Not root, so PAM can only verify our own account -- and we say so up front."""

    def test_own_username_reaches_pam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(auth, "is_root", lambda: False)
        monkeypatch.setattr(auth, "running_user", lambda: "alice")
        calls = _fake_pam(monkeypatch, accepts=True)

        result = auth.authenticate("alice", "pw", _config(auth_service="login"))

        assert result.ok
        assert result.username == "alice"
        assert calls == [("alice", "pw", "login")]

    def test_other_username_is_refused_without_asking_pam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of the whole rule: a name we cannot verify never touches PAM,
        and the error names the one account that would have worked."""
        monkeypatch.setattr(auth, "is_root", lambda: False)
        monkeypatch.setattr(auth, "running_user", lambda: "alice")
        calls = _fake_pam(monkeypatch, accepts=True)

        result = auth.authenticate("bob", "pw", _config())

        assert not result.ok
        assert "alice" in result.error
        assert calls == []

    def test_the_group_does_not_gate_a_non_root_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is only one possible account when not root; applying the wheel
        gate to it could only lock someone out of their own instance, so it is not
        applied. A member-less group must not stop the running user logging in."""
        monkeypatch.setattr(auth, "is_root", lambda: False)
        monkeypatch.setattr(auth, "running_user", lambda: "alice")
        monkeypatch.setattr(auth, "_in_group", lambda user, group: False)
        calls = _fake_pam(monkeypatch, accepts=True)

        result = auth.authenticate("alice", "pw", _config())

        assert result.ok
        assert calls == [("alice", "pw", "login")]


class TestRootGatesOnGroupMembership:
    """Root can verify anyone, so the admin group is what keeps that from meaning
    every system account is an admin login."""

    def test_non_member_is_refused_without_asking_pam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(auth, "is_root", lambda: True)
        monkeypatch.setattr(auth, "_in_group", lambda user, group: False)
        calls = _fake_pam(monkeypatch, accepts=True)

        result = auth.authenticate("nobody", "pw", _config(auth_group="wheel"))

        assert not result.ok
        assert "wheel" in result.error
        assert calls == []

    def test_member_reaches_pam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(auth, "is_root", lambda: True)
        monkeypatch.setattr(auth, "_in_group", lambda user, group: True)
        calls = _fake_pam(monkeypatch, accepts=True)

        result = auth.authenticate("admin", "pw", _config())

        assert result.ok
        assert result.username == "admin"
        assert calls == [("admin", "pw", "login")]


class TestPamOutcome:
    def test_rejection_is_a_generic_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wrong password and an unknown user get the same words: telling them
        apart is a hint to whoever is guessing."""
        monkeypatch.setattr(auth, "is_root", lambda: False)
        monkeypatch.setattr(auth, "running_user", lambda: "alice")
        _fake_pam(monkeypatch, accepts=False)

        result = auth.authenticate("alice", "wrong", _config())

        assert not result.ok
        assert result.error == "Incorrect username or password."

    def test_blank_credentials_never_reach_pam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_pam(monkeypatch, accepts=True)

        assert not auth.authenticate("", "", _config()).ok
        assert calls == []

    def test_missing_libpam_is_a_clean_failure_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host where libpam will not load fails one login, not the process --
        which is why the import is inside authenticate() and wrapped."""
        monkeypatch.setattr(auth, "is_root", lambda: False)
        monkeypatch.setattr(auth, "running_user", lambda: "alice")
        # None in sys.modules makes `import pamela` raise ImportError.
        monkeypatch.setitem(sys.modules, "pamela", None)

        result = auth.authenticate("alice", "pw", _config())

        assert not result.ok
        assert "unavailable" in result.error.lower()


class TestGroupMembership:
    """`_in_group` has to catch the primary-group case, which `gr_mem` omits."""

    def test_supplementary_membership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            auth.grp, "getgrnam", lambda group: types.SimpleNamespace(gr_mem=["alice"], gr_gid=10)
        )
        assert auth._in_group("alice", "wheel") is True

    def test_primary_gid_membership(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Someone whose *primary* group is the admin one is not listed in gr_mem,
        and checking only the member list would wrongly lock them out."""
        monkeypatch.setattr(
            auth.grp, "getgrnam", lambda group: types.SimpleNamespace(gr_mem=[], gr_gid=10)
        )
        monkeypatch.setattr(
            auth.pwd, "getpwnam", lambda user: types.SimpleNamespace(pw_gid=10)
        )
        assert auth._in_group("alice", "wheel") is True

    def test_non_member(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            auth.grp, "getgrnam", lambda group: types.SimpleNamespace(gr_mem=[], gr_gid=10)
        )
        monkeypatch.setattr(
            auth.pwd, "getpwnam", lambda user: types.SimpleNamespace(pw_gid=99)
        )
        assert auth._in_group("alice", "wheel") is False

    def test_missing_group_is_not_a_member(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_keyerror(group: str) -> None:
            raise KeyError(group)

        monkeypatch.setattr(auth.grp, "getgrnam", raise_keyerror)
        assert auth._in_group("alice", "nope") is False
