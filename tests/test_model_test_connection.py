"""Tests for _extract_context_length / test-connection probe helpers."""

from mangaba_cli.web_server import _extract_context_length


def test_extract_context_length_root_variants():
    assert _extract_context_length({"context_length": 32768}) == 32768
    assert _extract_context_length({"context_window": 8192}) == 8192
    assert _extract_context_length({"max_context_length": 128000}) == 128000


def test_extract_context_length_nested():
    assert _extract_context_length({"meta": {"context_window": 4096}}) == 4096
    assert _extract_context_length({"capabilities": {"context_size": 16384}}) == 16384


def test_extract_context_length_absent_or_invalid():
    assert _extract_context_length({}) is None
    assert _extract_context_length({"context_length": 0}) is None
    assert _extract_context_length({"context_length": "muito"}) is None


def test_test_connection_requires_model(monkeypatch):
    from mangaba_cli.web_server import test_model_connection, ModelTestRequest
    r = test_model_connection(ModelTestRequest(provider="deepseek", model=""))
    assert r["ok"] is False
    assert "modelo" in r["error"].lower()
