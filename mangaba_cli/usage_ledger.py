"""Ledger local de uso de tokens e custo estimado.

Registra, por dia, os tokens de entrada/saída e o número de turnos de cada
conversa — em todos os canais (chat do dashboard, Telegram, Discord, CLI),
porque o registro acontece no forwarder único ``AIAgent.run_conversation``.

Objetivo: dar visibilidade de gasto e um **teto diário** configurável. Por
segurança, o teto é por padrão apenas um aviso (``budget_mode: warn``) — nunca
derruba o bot silenciosamente. Pode ser mudado para ``block`` no config.

Armazenamento: ``$MANGABA_HOME/usage/YYYY-MM.json`` (agregados diários).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def _usage_dir() -> Path:
    from mangaba_agent.mangaba_constants import get_mangaba_home

    d = get_mangaba_home() / "usage"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _FileLock:
    """Trava entre processos (fcntl) — o ledger é escrito pelo roteador e pelos
    gateways dedicados (processos separados). No-op em plataformas sem fcntl."""

    def __init__(self) -> None:
        self._fh = None

    def __enter__(self):
        try:
            import fcntl

            self._fh = open(_usage_dir() / ".lock", "w", encoding="utf-8")
            fcntl.flock(self._fh, fcntl.LOCK_EX)
        except Exception:
            self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                import fcntl

                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def _atomic_write(path: Path, text: str) -> None:
    """Escrita atômica: tmp + os.replace (evita JSON truncado em crash)."""
    import os

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _month_path(day: Optional[str] = None) -> Path:
    day = day or _today()
    return _usage_dir() / f"{day[:7]}.json"


def _read_month(day: Optional[str] = None) -> Dict[str, Any]:
    p = _month_path(day)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def record_usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model: str = "",
    provider: str = "",
    platform: str = "",
    tenant_id: str = "",
) -> None:
    """Acumula o uso de um turno no ledger do dia. Best-effort, não levanta.

    ``tenant_id`` (id do cliente da API) habilita a medição por cliente, base
    da cobrança/quota no modelo provedor-de-IA.
    """
    try:
        inp = int(input_tokens or 0)
        out = int(output_tokens or 0)
        if inp <= 0 and out <= 0:
            return
        day = _today()
        with _LOCK, _FileLock():
            data = _read_month(day)
            d = data.setdefault(
                day,
                {"input": 0, "output": 0, "turns": 0,
                 "by_model": {}, "by_platform": {}, "by_tenant": {}},
            )
            d["input"] += inp
            d["output"] += out
            d["turns"] += 1
            if model:
                m = d["by_model"].setdefault(model, {"input": 0, "output": 0, "turns": 0})
                m["input"] += inp
                m["output"] += out
                m["turns"] += 1
            if platform:
                d["by_platform"][platform] = d["by_platform"].get(platform, 0) + 1
            if tenant_id:
                bt = d.setdefault("by_tenant", {})
                tt = bt.setdefault(tenant_id, {"input": 0, "output": 0, "turns": 0})
                tt["input"] += inp
                tt["output"] += out
                tt["turns"] += 1
            _atomic_write(
                _month_path(day), json.dumps(data, ensure_ascii=False)
            )
    except Exception as e:  # pragma: no cover
        logger.debug("usage_ledger.record_usage falhou: %s", e)


def tenant_used_today(tenant_id: str) -> int:
    """Tokens (entrada+saída) consumidos hoje por um cliente da API."""
    if not tenant_id:
        return 0
    bt = (get_today().get("by_tenant") or {}).get(tenant_id) or {}
    return int(bt.get("input", 0)) + int(bt.get("output", 0))


def tenant_over_limit(tenant_id: str, daily_token_limit: int) -> bool:
    """True se o cliente estourou seu teto diário (limite > 0)."""
    lim = int(daily_token_limit or 0)
    return bool(lim and tenant_used_today(tenant_id) >= lim)


def get_today() -> Dict[str, Any]:
    """Agregado do dia atual (com totais derivados)."""
    day = _today()
    d = _read_month(day).get(day) or {"input": 0, "output": 0, "turns": 0, "by_model": {}}
    d = dict(d)
    d["total"] = int(d.get("input", 0)) + int(d.get("output", 0))
    d["date"] = day
    return d


def get_recent(days: int = 14) -> Dict[str, Any]:
    """Série diária dos últimos *days* dias (lê mês atual e anterior)."""
    from datetime import timedelta

    today = datetime.now()
    months = {today.strftime("%Y-%m")}
    months.add((today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))
    merged: Dict[str, Any] = {}
    for m in months:
        p = _usage_dir() / f"{m}.json"
        if p.exists():
            try:
                merged.update(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
    series = []
    for i in range(days - 1, -1, -1):
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        d = merged.get(day) or {}
        series.append(
            {
                "date": day,
                "input": int(d.get("input", 0)),
                "output": int(d.get("output", 0)),
                "total": int(d.get("input", 0)) + int(d.get("output", 0)),
                "turns": int(d.get("turns", 0)),
            }
        )
    return {"series": series}


def _budget_cfg() -> Dict[str, Any]:
    try:
        from mangaba_cli.config import load_config

        return (load_config().get("usage") or {})
    except Exception:
        return {}


def budget_status() -> Dict[str, Any]:
    """Estado do teto diário de tokens configurado."""
    cfg = _budget_cfg()
    limit = int(cfg.get("daily_token_limit", 0) or 0)
    mode = str(cfg.get("budget_mode", "warn") or "warn")
    used = get_today()["total"]
    over = bool(limit and used >= limit)
    pct = (used / limit * 100.0) if limit else 0.0
    return {
        "daily_token_limit": limit,
        "budget_mode": mode,
        "used": used,
        "over_budget": over,
        "percent": round(pct, 1),
        "enabled": bool(limit),
    }


def is_over_budget_block() -> bool:
    """True somente quando há teto, ele foi estourado e o modo é 'block'."""
    s = budget_status()
    return s["over_budget"] and s["budget_mode"] == "block"
