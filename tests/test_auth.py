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

import base64
import json
import sys
import types

import itsdangerous
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


def _starlette_session_cookie(session: dict, secret: str) -> str:
    """Sign a session the way starlette.middleware.sessions does, for the header.

    The websocket guard has to read exactly what Starlette wrote, so the test
    forges a cookie the same way rather than trusting our own reader to round-trip
    with itself: a TimestampSigner over the storage secret, wrapping base64(JSON).
    """
    signer = itsdangerous.TimestampSigner(str(secret))
    data = base64.b64encode(json.dumps(session).encode())
    return f"session={signer.sign(data).decode()}"


class TestSessionCookieDecode:
    """The socket guard trusts a cookie only if it verifies under the storage
    secret; every other outcome must collapse to "no session", i.e. deny."""

    SECRET = "storage-secret-under-test"

    def test_valid_cookie_yields_its_session_id(self) -> None:
        header = _starlette_session_cookie({"id": "sess-42"}, self.SECRET)
        assert auth._session_id_from_cookie(header, self.SECRET) == "sess-42"

    def test_wrong_secret_is_rejected(self) -> None:
        """A cookie an attacker minted (or one signed under a rotated secret) does
        not verify, so it names no session -- the whole point of signing it."""
        header = _starlette_session_cookie({"id": "sess-42"}, "a-different-secret")
        assert auth._session_id_from_cookie(header, self.SECRET) is None

    def test_tampered_cookie_is_rejected(self) -> None:
        header = _starlette_session_cookie({"id": "sess-42"}, self.SECRET)
        assert auth._session_id_from_cookie(header[:-3] + "xxx", self.SECRET) is None

    def test_absent_cookie_and_missing_secret_are_rejected(self) -> None:
        good = _starlette_session_cookie({"id": "sess-42"}, self.SECRET)
        assert auth._session_id_from_cookie("othercookie=x", self.SECRET) is None
        assert auth._session_id_from_cookie(good, "") is None
        assert auth._session_id_from_cookie(good, None) is None


class TestAttachRule:
    """`_may_attach` is the socket's version of the page gate: a real session, or
    the login page reaching its own client so a logged-out visitor can log in."""

    def test_authenticated_session_may_attach_to_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(auth, "_session_is_authenticated", lambda environ: True)
        # No need to even name a client: a logged-in session is allowed on its own.
        assert auth._may_attach({}, "any-client-id") is True

    def test_unauthenticated_may_attach_only_to_an_unrestricted_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(auth, "_session_is_authenticated", lambda environ: False)
        # Stand in for nicegui's Client.instances: a login-page client and a
        # protected-page client, looked up by id.
        login = types.SimpleNamespace(page=types.SimpleNamespace(path="/login"))
        secret = types.SimpleNamespace(page=types.SimpleNamespace(path="/secret"))
        instances = {"login-cid": login, "secret-cid": secret}
        monkeypatch.setattr(
            auth, "_targets_unrestricted_page",
            lambda cid: instances.get(cid) is login,
        )
        assert auth._may_attach({}, "login-cid") is True
        assert auth._may_attach({}, "secret-cid") is False
        assert auth._may_attach({}, None) is False
