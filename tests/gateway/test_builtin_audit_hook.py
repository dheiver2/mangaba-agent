"""Tests for the builtin audit hook (gateway/builtin_hooks/audit.py)."""

import json
import os
from pathlib import Path

import pytest

from gateway.builtin_hooks import audit
from gateway.hooks import HookRegistry


@pytest.fixture
def audit_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANGABA_HOME", str(tmp_path))
    # Config vazio → defaults (enabled=True, include_content=False).
    monkeypatch.setattr(audit, "_audit_cfg", lambda: {})
    return tmp_path


def _read_records(home: Path):
    files = list((home / "audit").glob("*.jsonl"))
    assert len(files) == 1
    return [json.loads(line) for line in files[0].read_text().splitlines()], files[0]


def test_writes_jsonl_with_metadata_only(audit_home):
    audit.handle("agent:start", {
        "platform": "telegram", "user_id": "u1", "chat_id": "c1",
        "session_id": "s1", "message": "texto sensível do usuário",
    })
    records, path = _read_records(audit_home)
    rec = records[0]
    assert rec["event"] == "agent:start"
    assert rec["platform"] == "telegram"
    assert rec["user_id"] == "u1"
    # Conteúdo não vai por padrão — só o tamanho.
    assert "message" not in rec
    assert rec["message_len"] == len("texto sensível do usuário")
    assert "ts" in rec
    # Arquivo novo nasce 0600.
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_include_content_opt_in(audit_home, monkeypatch):
    monkeypatch.setattr(audit, "_audit_cfg", lambda: {"include_content": True})
    audit.handle("agent:end", {"platform": "cli", "response": "x" * 500})
    records, _ = _read_records(audit_home)
    assert records[0]["response"] == "x" * audit._MAX_FIELD


def test_disabled_writes_nothing(audit_home, monkeypatch):
    monkeypatch.setattr(audit, "_audit_cfg", lambda: {"enabled": False})
    audit.handle("agent:start", {"platform": "cli"})
    assert not (audit_home / "audit").exists()


def test_never_raises_on_bad_context(audit_home):
    audit.handle("agent:start", {"message": object()})  # não-serializável no len? str() cobre
    audit.handle("command:reset", {})


def test_registry_registers_builtin_and_fires_on_wildcard(audit_home):
    import asyncio

    registry = HookRegistry()
    registry._register_builtin_hooks()
    names = [h["name"] for h in registry.loaded_hooks]
    assert "audit" in names
    # command:* deve casar qualquer slash command.
    asyncio.run(registry.emit("command:reset", {"platform": "discord", "user_id": "u9",
                                                "command": "reset"}))
    records, _ = _read_records(audit_home)
    assert records[-1]["event"] == "command:reset"
    assert records[-1]["command"] == "reset"
