"""Tests for the dashboard chat → profile-gateway delegation (item 5)."""

from types import SimpleNamespace
import mangaba_cli.web_server as ws


def _member(path, running):
    return SimpleNamespace(name="x", path=path, running=running)


def test_endpoint_none_when_gateway_stopped(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("platforms:\n  api_server:\n    port: 8643\n")
    monkeypatch.setattr(__import__("mangaba_cli.fleet", fromlist=["x"]), "find_member", lambda n: _member(tmp_path, False))
    assert ws._profile_api_server_endpoint("x") is None


def test_endpoint_none_without_api_server(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("platforms:\n  telegram:\n    enabled: true\n")
    monkeypatch.setattr(__import__("mangaba_cli.fleet", fromlist=["x"]), "find_member", lambda n: _member(tmp_path, True))
    assert ws._profile_api_server_endpoint("x") is None


def test_endpoint_resolves_when_running_and_configured(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("platforms:\n  api_server:\n    port: 8643\n")
    monkeypatch.setattr(__import__("mangaba_cli.fleet", fromlist=["x"]), "find_member", lambda n: _member(tmp_path, True))
    assert ws._profile_api_server_endpoint("x") == "http://127.0.0.1:8643"


def test_endpoint_none_when_api_server_disabled(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("platforms:\n  api_server:\n    port: 8643\n    enabled: false\n")
    monkeypatch.setattr(__import__("mangaba_cli.fleet", fromlist=["x"]), "find_member", lambda n: _member(tmp_path, True))
    assert ws._profile_api_server_endpoint("x") is None


def test_delegate_returns_none_on_unreachable():
    # Porta improvável de estar escutando → fallback (None), nunca levanta.
    assert ws._delegate_chat_to_gateway("http://127.0.0.1:1", "oi", []) is None
