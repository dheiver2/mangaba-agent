"""Trilha de auditoria embutida do gateway (governança multi-agente).

Grava um registro JSONL por evento de ciclo de vida em
``$MANGABA_HOME/audit/YYYY-MM.jsonl`` — quem falou com o agente, em qual
canal, quais comandos rodou e quando cada turno começou/terminou. Como o
caminho é resolvido por ``get_mangaba_home()``, cada profile (agente) tem
sua própria trilha, e a frota inteira pode ser auditada varrendo
``~/.mangaba/profiles/*/audit/``.

Privacidade: por padrão grava apenas *metadados* (ids, comando, tamanhos);
o conteúdo de mensagens/respostas só entra com ``audit.include_content: true``.

Config (config.yaml):
  audit:
    enabled: true           # false desliga a trilha
    include_content: false  # true grava message/response truncados

Best-effort: nunca levanta exceção nem bloqueia o pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# Eventos auditados (command:* casa qualquer slash command via wildcard).
EVENTS = [
    "gateway:startup",
    "session:start",
    "session:end",
    "session:reset",
    "agent:start",
    "agent:end",
    "command:*",
]

# Campos de identificação copiados verbatim (truncados) do contexto do evento.
_ID_FIELDS = (
    "platform", "user_id", "chat_id", "session_id", "session_key",
    "command", "args",
)
# Campos de conteúdo: por padrão só o tamanho vai para a trilha.
_CONTENT_FIELDS = ("message", "response")
_MAX_FIELD = 300


def _audit_cfg() -> Dict[str, Any]:
    try:
        from mangaba_cli.config import load_config

        return load_config().get("audit") or {}
    except Exception:
        return {}


def _audit_path() -> Path:
    from mangaba_agent.mangaba_constants import get_mangaba_home

    d = get_mangaba_home() / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{datetime.now(timezone.utc).strftime('%Y-%m')}.jsonl"


def handle(event_type: str, context: Dict[str, Any]) -> None:
    try:
        cfg = _audit_cfg()
        if not cfg.get("enabled", True):
            return
        rec: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event_type,
        }
        for key in _ID_FIELDS:
            value = context.get(key)
            if value not in (None, ""):
                rec[key] = str(value)[:_MAX_FIELD]
        include_content = bool(cfg.get("include_content", False))
        for key in _CONTENT_FIELDS:
            value = context.get(key)
            if not value:
                continue
            if include_content:
                rec[key] = str(value)[:_MAX_FIELD]
            else:
                rec[f"{key}_len"] = len(str(value))
        line = json.dumps(rec, ensure_ascii=False)
        path = _audit_path()
        with _LOCK:
            is_new = not path.exists()
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if is_new:
                os.chmod(path, 0o600)
    except Exception as exc:  # noqa: BLE001 — auditoria nunca derruba o pipeline
        logger.debug("audit hook: gravação falhou (não-fatal): %s", exc)
