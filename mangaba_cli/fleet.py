"""Gerência de frota — ver e controlar N agentes (profiles) de um lugar só.

No Mangaba, cada **profile** é um agente independente (SOUL/modelo/skills/memória
próprios) e roda como **um gateway por profile** (1 processo, N canais). Rodar N
bots já é suportado; o que faltava era **gerenciar a frota** de um ponto: ver
quem está no ar, subir/derrubar/reiniciar, de um comando — e pelos canais.

Design: a camada de *status* é pura e testável (agrega `list_profiles()` +
PID por profile). As *ações* (start/stop/restart) delegam à máquina já provada
do gateway (``terminate_pid``, ``_gateway_run_args_for_profile``), best-effort.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class FleetMember:
    name: str
    path: Path
    running: bool
    pid: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    skills: int = 0
    description: str = ""
    is_default: bool = False
    platforms: List[dict] = field(default_factory=list)
    tokens_today: int = 0
    turns_today: int = 0


def _pid_for(profile_path: Path) -> Optional[int]:
    try:
        from gateway.status import get_running_pid
        return get_running_pid(profile_path / "gateway.pid", cleanup_stale=False)
    except Exception:
        return None


def _usage_today_for(profile_path: Path) -> Tuple[int, int]:
    """(tokens in+out, turnos) de hoje, lidos do usage ledger do profile.

    Lê o arquivo direto (``<profile>/usage/YYYY-MM.json``) em vez do módulo
    ``usage_ledger``, que é ancorado no MANGABA_HOME *ativo* — a frota precisa
    enxergar o ledger de cada profile, não só o do profile atual.
    """
    try:
        day = time.strftime("%Y-%m-%d")
        data = json.loads(
            (profile_path / "usage" / f"{day[:7]}.json").read_text(encoding="utf-8")
        )
        d = data.get(day) or {}
        return int(d.get("input", 0)) + int(d.get("output", 0)), int(d.get("turns", 0))
    except Exception:
        return 0, 0


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def collect_fleet() -> List[FleetMember]:
    """Snapshot de todos os agentes (profiles) e o estado do gateway de cada um."""
    try:
        from mangaba_cli.profiles import list_profiles
    except Exception as exc:  # noqa: BLE001
        logger.debug("fleet: list_profiles indisponível: %s", exc)
        return []
    members: List[FleetMember] = []
    for p in list_profiles():
        pid = _pid_for(p.path) if p.gateway_running else None
        # Nunca deixar um profile com config/.env malformado derrubar a frota
        # inteira — degrada para uma lista de plataformas vazia.
        try:
            platforms = _platforms_for_profile(p.path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fleet: plataformas indisponíveis para %s: %s", p.name, exc)
            platforms = []
        tokens_today, turns_today = _usage_today_for(p.path)
        members.append(FleetMember(
            name=p.name, path=p.path,
            running=bool(p.gateway_running), pid=pid,
            model=p.model, provider=p.provider,
            skills=getattr(p, "skill_count", 0) or 0,
            description=getattr(p, "description", "") or "",
            is_default=getattr(p, "is_default", False),
            platforms=platforms,
            tokens_today=tokens_today, turns_today=turns_today,
        ))
    members.sort(key=lambda m: (not m.running, m.name))  # no ar primeiro
    return members


def find_member(name: str) -> Optional[FleetMember]:
    name = (name or "").strip()
    for m in collect_fleet():
        if m.name == name:
            return m
    return None


def render_fleet(members: List[FleetMember], *, markdown: bool = False) -> str:
    """Texto amigável da frota (serve CLI e canal)."""
    if not members:
        return "Nenhum agente (profile) encontrado."
    up = sum(1 for m in members if m.running)
    head = f"🛰️ Frota: {len(members)} agente(s) · {up} no ar · {len(members) - up} parado(s)"
    lines = [f"*{head}*" if markdown else head, ""]
    for m in members:
        dot = "🟢" if m.running else "⚪"
        model = m.model or "?"
        pid = f" · pid {m.pid}" if m.pid else ""
        usage = (
            f" · hoje: {_fmt_tokens(m.tokens_today)} tok / {m.turns_today} turno(s)"
            if m.turns_today else ""
        )
        name = f"`{m.name}`" if markdown else m.name
        lines.append(f"{dot} {name} — {model}{pid}{usage}")
        if m.description:
            lines.append(f"    ↳ {m.description[:80]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ações (delegam à máquina existente do gateway). Retornam (ok, mensagem).
# ---------------------------------------------------------------------------
def stop_profile(name: str) -> Tuple[bool, str]:
    m = find_member(name)
    if m is None:
        return False, f"Agente `{name}` não encontrado."
    if not m.running or not m.pid:
        return True, f"Agente `{name}` já estava parado."
    try:
        from gateway.status import terminate_pid
        terminate_pid(m.pid)
        return True, f"🛑 Agente `{name}` (pid {m.pid}) parado."
    except Exception as exc:  # noqa: BLE001
        return False, f"Falha ao parar `{name}`: {exc}"


def start_profile(name: str) -> Tuple[bool, str]:
    m = find_member(name)
    if m is None:
        return False, f"Agente `{name}` não encontrado."
    if m.running:
        return True, f"Agente `{name}` já está no ar (pid {m.pid})."
    try:
        from mangaba_cli.gateway import _gateway_run_args_for_profile
        from mangaba_cli._subprocess_compat import windows_detach_popen_kwargs
        args = _gateway_run_args_for_profile(name)
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        kwargs.update(windows_detach_popen_kwargs())
        subprocess.Popen(args, **kwargs)  # noqa: S603 — args são internos, sem shell
        return True, f"🚀 Subindo o agente `{name}`… (gateway iniciando)"
    except Exception as exc:  # noqa: BLE001
        return False, f"Falha ao subir `{name}`: {exc}"


def restart_profile(name: str) -> Tuple[bool, str]:
    m = find_member(name)
    if m is None:
        return False, f"Agente `{name}` não encontrado."
    stop_ok, _ = stop_profile(name)
    start_ok, start_msg = start_profile(name)
    if start_ok:
        return True, f"🔄 Agente `{name}` reiniciado."
    return False, start_msg


# ---------------------------------------------------------------------------
# Ciclo de vida do agente (profile): criar e excluir. Reutiliza profiles.py.
# ---------------------------------------------------------------------------
def create_agent(name: str, description: Optional[str] = None) -> Tuple[bool, str]:
    """Cria um novo agente (profile), clonando config/skills do default."""
    name = (name or "").strip()
    if not name:
        return False, "Informe um nome: `create <nome>`."
    try:
        from mangaba_cli import profiles as _p
        canon = _p.normalize_profile_name(name)
        _p.validate_profile_name(canon)
        if _p.profile_exists(canon):
            return False, f"Já existe um agente `{canon}`."
        _p.create_profile(canon, clone_config=True, description=description or None)
    except ValueError as exc:
        return False, f"Nome inválido: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Falha ao criar `{name}`: {exc}"
    extra = f" — {description}" if description else ""
    return True, (f"✅ Agente `{canon}` criado{extra}.\n"
                  f"Configure-o com `mangaba -p {canon} setup` e suba com "
                  f"`mangaba fleet start {canon}`.")


def delete_agent(name: str, confirm: bool = False) -> Tuple[bool, str]:
    """Exclui um agente (profile). Exige confirmação; nunca apaga o default."""
    name = (name or "").strip()
    if not name:
        return False, "Informe um nome: `delete <nome>`."
    try:
        from mangaba_cli import profiles as _p
        canon = _p.normalize_profile_name(name)
    except Exception:
        canon = name
    if canon == "default":
        return False, "O agente `default` (controle) não pode ser excluído."
    try:
        from mangaba_cli import profiles as _p
        if not _p.profile_exists(canon):
            return False, f"Agente `{canon}` não encontrado."
        if not confirm:
            return False, (f"⚠️ Isso apaga *todo* o agente `{canon}` (config, skills, "
                           f"memória, sessões) — irreversível.\n"
                           f"Para confirmar: `/agent delete {canon} confirmar`")
        _p.delete_profile(canon, yes=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"Falha ao excluir `{canon}`: {exc}"
    return True, f"🗑️ Agente `{canon}` excluído."


# ---------------------------------------------------------------------------
# Logs agregados
# ---------------------------------------------------------------------------
def read_gateway_log(profile_path: Path, lines: int = 40) -> str:
    """Últimas ``lines`` linhas do gateway.log de um profile (ou aviso)."""
    log_path = profile_path / "logs" / "gateway.log"
    if not log_path.exists():
        return "(sem gateway.log — o agente ainda não rodou)"
    try:
        content = log_path.read_text(errors="ignore").splitlines()
    except OSError as exc:
        return f"(erro ao ler log: {exc})"
    tail = content[-max(1, lines):]
    return "\n".join(tail) if tail else "(log vazio)"


def fleet_logs(name: Optional[str] = None, lines: int = 40) -> str:
    """Logs de um agente específico, ou um resumo curto de todos."""
    members = collect_fleet()
    if name:
        m = find_member(name)
        if m is None:
            return f"Agente `{name}` não encontrado."
        return f"📜 Log de `{m.name}` (últimas {lines}):\n\n{read_gateway_log(m.path, lines)}"
    # Visão de todos: poucas linhas por agente para não estourar.
    per = max(1, min(lines, 10))
    out = []
    for m in members:
        dot = "🟢" if m.running else "⚪"
        out.append(f"{dot} {m.name} — últimas {per} linhas:")
        out.append(read_gateway_log(m.path, per))
        out.append("")
    return "\n".join(out).strip() or "Nenhum agente."


# ---------------------------------------------------------------------------
# Broadcast — aviso de operador para o home_channel de cada agente.
# Seguro: só atinge o canal-operador (home_channel) configurado, nunca os
# chats de clientes. Entregue pelo heartbeat de follow-ups de cada gateway.
# ---------------------------------------------------------------------------
_PLATFORM_TOKEN_VARS: dict = {
    "telegram": "TELEGRAM_BOT_TOKEN",
    "discord": "DISCORD_BOT_TOKEN",
    "slack": "SLACK_BOT_TOKEN",
    "whatsapp": "WHATSAPP_TOKEN",
    "email": "EMAIL_PASSWORD",
    "signal": "SIGNAL_NUMBER",
}


def _telegram_bot_handle(profile_path: Path, token: str) -> Optional[str]:
    """@username do bot do Telegram — getMe com cache em disco por token.

    O cache evita uma chamada de rede a cada render da frota; trocar o token
    invalida (chave = fingerprint do token). Best-effort: sem rede, sem handle.
    """
    import hashlib
    import urllib.request

    cache_path = profile_path / ".telegram_bot_handle.json"
    fingerprint = hashlib.sha256(token.encode()).hexdigest()[:16]
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("token_fp") == fingerprint and cached.get("handle"):
            return cached["handle"]
    except Exception:  # noqa: BLE001
        pass
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getMe", timeout=4
        ) as resp:
            username = (json.loads(resp.read()).get("result") or {}).get("username")
        if not username:
            return None
        handle = f"@{username}"
        try:
            cache_path.write_text(
                json.dumps({"token_fp": fingerprint, "handle": handle}),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass
        return handle
    except Exception:  # noqa: BLE001
        return None


def _platforms_for_profile(profile_path: Path) -> List[dict]:
    """Lê config.yaml do profile e retorna lista de plataformas com metadados."""
    cfg_path = profile_path / "config.yaml"
    if not cfg_path.exists():
        return []
    try:
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:  # noqa: BLE001
        return []

    # Lê variáveis do .env do profile para checar tokens
    env_vars: dict = {}
    env_path = profile_path / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env_vars[k.strip()] = v.strip()
        except Exception:  # noqa: BLE001
            pass

    platforms = cfg.get("platforms") or {}
    if not isinstance(platforms, dict):
        return []

    out: List[dict] = []
    for plat_name, pcfg in platforms.items():
        if not isinstance(pcfg, dict):
            continue
        enabled = bool(pcfg.get("enabled", True))
        hc = pcfg.get("home_channel")
        home_channel = hc if isinstance(hc, dict) else None
        token_var = _PLATFORM_TOKEN_VARS.get(plat_name)
        token_value = (env_vars.get(token_var) or os.environ.get(token_var, "")) if token_var else ""
        has_token = bool(token_value)
        entry = {
            "platform": plat_name,
            "enabled": enabled,
            "home_channel": home_channel,
            "has_token": has_token,
        }
        if plat_name == "telegram" and token_value:
            handle = _telegram_bot_handle(profile_path, token_value)
            if handle:
                entry["handle"] = handle
        out.append(entry)

    # Profiles sem bloco ``platforms.telegram`` mas COM token no .env: o bot
    # existe e é a que o agente responde — surfaça-o para o dashboard mostrar
    # a qual @ o profile está associado.
    if not any(e["platform"] == "telegram" for e in out):
        tg_token = env_vars.get("TELEGRAM_BOT_TOKEN", "")  # só o .env do profile — não vaza o token ambiente
        if tg_token:
            entry = {
                "platform": "telegram",
                "enabled": True,
                "home_channel": None,
                "has_token": True,
            }
            handle = _telegram_bot_handle(profile_path, tg_token)
            if handle:
                entry["handle"] = handle
            out.append(entry)

    return out


def _home_channels_for_profile(profile_path: Path) -> List[dict]:
    """Lê o config.yaml do profile e retorna os home_channel configurados."""
    cfg_path = profile_path / "config.yaml"
    if not cfg_path.exists():
        return []
    try:
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:  # noqa: BLE001
        return []
    out: List[dict] = []
    platforms = cfg.get("platforms") or {}
    if isinstance(platforms, dict):
        for plat_name, pcfg in platforms.items():
            if not isinstance(pcfg, dict):
                continue
            hc = pcfg.get("home_channel")
            if isinstance(hc, dict) and hc.get("chat_id"):
                out.append({
                    "platform": hc.get("platform") or plat_name,
                    "chat_id": str(hc["chat_id"]),
                    "thread_id": hc.get("thread_id"),
                })
    return out


def _enqueue_followup(profile_path: Path, *, platform: str, chat_id: str,
                      message: str, thread_id: Optional[str] = None) -> None:
    """Anexa um follow-up imediato ao store do profile (entregue pelo heartbeat)."""
    store = profile_path / "followups.jsonl"
    now = time.time()
    fid = f"f{int(now * 1000) % 1_000_000:06d}"
    record = {
        "id": fid, "platform": platform, "chat_id": chat_id,
        "message": message, "due_at": now, "status": "pending",
        "thread_id": thread_id, "created_at": now, "context": "fleet-broadcast",
    }
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def broadcast(message: str, *, running_only: bool = True) -> Tuple[int, int, List[str]]:
    """Envia um aviso ao home_channel de cada agente.

    Retorna (nº de agentes atingidos, nº de canais, lista de pulados).
    """
    message = (message or "").strip()
    if not message:
        raise ValueError("mensagem vazia")
    note = f"📢 *Aviso da operação:*\n{message}"
    reached = channels = 0
    skipped: List[str] = []
    for m in collect_fleet():
        if running_only and not m.running:
            skipped.append(f"{m.name} (parado)")
            continue
        homes = _home_channels_for_profile(m.path)
        if not homes:
            skipped.append(f"{m.name} (sem home_channel)")
            continue
        for hc in homes:
            _enqueue_followup(m.path, platform=hc["platform"], chat_id=hc["chat_id"],
                              message=note, thread_id=hc.get("thread_id"))
            channels += 1
        reached += 1
    return reached, channels, skipped
