"""Tests for the TLS policy: which cert we serve, and when.

As with :mod:`test_auth`, the interesting part is the decision, not the tool. We
test :func:`spiriconfig.tls.resolve` -- self-signed when exposed, provided cert when
given, plain HTTP on loopback, and HSTS on exactly one of those -- and the shape of
the ``openssl`` command, without ever asking openssl to make a real cert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spiriconfig import tls


class TestResolve:
    def test_off_is_plain_http_even_when_exposed(self) -> None:
        plan = tls.resolve(mode="off", cert=None, key=None, is_loopback=False)
        assert not plan.enabled
        assert plan.scheme == "http"
        assert not plan.hsts

    def test_provided_cert_is_used_with_hsts(self) -> None:
        plan = tls.resolve(mode="auto", cert="/c.pem", key="/k.pem", is_loopback=False)
        assert plan.enabled
        assert plan.certfile == Path("/c.pem")
        assert plan.keyfile == Path("/k.pem")
        assert plan.hsts is True
        assert plan.generate is False

    def test_provided_cert_wins_on_loopback_too(self) -> None:
        """A cert you handed us is used wherever we bind -- it is not tied to exposure."""
        plan = tls.resolve(mode="auto", cert="/c.pem", key="/k.pem", is_loopback=True)
        assert plan.enabled
        assert plan.hsts is True

    def test_exposed_without_cert_generates_selfsigned_no_hsts(self) -> None:
        plan = tls.resolve(mode="auto", cert=None, key=None, is_loopback=False)
        assert plan.enabled
        assert plan.generate is True
        # HSTS on a self-signed origin forbids the click-through the operator needs.
        assert plan.hsts is False
        assert plan.certfile is not None and plan.certfile.name == "cert.pem"

    def test_loopback_without_cert_stays_plain_http(self) -> None:
        plan = tls.resolve(mode="auto", cert=None, key=None, is_loopback=True)
        assert not plan.enabled
        assert not plan.generate

    def test_half_a_pair_is_ignored_and_falls_through(self) -> None:
        """A cert without its key is a misconfiguration, not half a setup: we ignore
        it and fall back to the automatic behaviour for the binding."""
        exposed = tls.resolve(mode="auto", cert="/c.pem", key=None, is_loopback=False)
        assert exposed.generate is True  # fell through to self-signed
        assert exposed.hsts is False

        loop = tls.resolve(mode="auto", cert=None, key="/k.pem", is_loopback=True)
        assert not loop.enabled  # fell through to plain HTTP


class TestSans:
    def test_always_covers_localhost_and_loopback(self) -> None:
        sans = tls.default_sans("0.0.0.0")
        assert "DNS:localhost" in sans
        assert "IP:127.0.0.1" in sans

    def test_a_bound_ip_is_added_as_an_ip_san(self) -> None:
        sans = tls.default_sans("192.168.1.50")
        assert "IP:192.168.1.50" in sans

    def test_a_bound_name_is_added_as_a_dns_san(self) -> None:
        sans = tls.default_sans("device.local")
        assert "DNS:device.local" in sans

    def test_a_wildcard_bind_adds_no_host_san(self) -> None:
        """0.0.0.0 is not an address a cert should claim; only the real names do."""
        assert "IP:0.0.0.0" not in tls.default_sans("0.0.0.0")


class TestSelfsignedCommand:
    def test_command_writes_the_named_paths_with_a_san_ext(self) -> None:
        cmd = tls.selfsigned_command(
            Path("/tls/cert.pem"), Path("/tls/key.pem"), ["DNS:localhost"]
        )
        argv = list(cmd.argv)
        assert argv[0] == "openssl"
        assert "-x509" in argv
        assert "/tls/cert.pem" in argv
        assert "/tls/key.pem" in argv
        # The key is unencrypted (-nodes) because a service starts unattended.
        assert "-nodes" in argv
        assert "subjectAltName=DNS:localhost" in argv


class TestEnsureSelfsigned:
    def test_existing_pair_is_not_regenerated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        cert.write_text("cert")
        key.write_text("key")
        plan = tls.TlsPlan(cert, key, generate=True)

        called = False

        def fake_run(*args: object, **kwargs: object) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(tls, "run", fake_run)
        tls.ensure_selfsigned(plan, "0.0.0.0")
        assert called is False

    def test_a_non_generate_plan_does_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tls, "run", lambda *a, **k: pytest.fail("should not run openssl")
        )
        # provided-cert plan: generate is False even though a cert is named.
        tls.ensure_selfsigned(tls.TlsPlan(Path("/c"), Path("/k")), "0.0.0.0")
