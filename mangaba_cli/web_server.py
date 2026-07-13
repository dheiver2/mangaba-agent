"""
Mangaba Agent — Web UI server.

Provides a FastAPI backend serving the Vite/React frontend and REST API
endpoints for managing configuration, environment variables, and sessions.

Usage:
    python -m mangaba_cli.main web          # Start on http://127.0.0.1:9119
    python -m mangaba_cli.main web --port 8080
"""

import asyncio
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mangaba_cli import __version__, __release_date__
from mangaba_cli.config import (
    cfg_get,
    DEFAULT_CONFIG,
    OPTIONAL_ENV_VARS,
    get_config_path,
    get_env_path,
    get_mangaba_home,
    load_config,
    load_env,
    save_config,
    save_env_value,
    remove_env_value,
    check_config_version,
    redact_key,
)
from gateway.status import get_running_pid, read_runtime_status
from mangaba_agent.utils import env_var_enabled

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError:
    # First try lazy-installing the dashboard extras. Only the user actually
    # running `mangaba dashboard` needs fastapi+uvicorn; lazy install keeps
    # them out of every other install path. After install, re-import.
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("tool.dashboard", prompt=False)
        from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
        from fastapi.staticfiles import StaticFiles
        from pydantic import BaseModel
    except Exception:
        raise SystemExit(
            "Web UI requires fastapi and uvicorn.\n"
            f"Install with: {sys.executable} -m pip install 'fastapi' 'uvicorn[standard]'"
        )

WEB_DIST = Path(os.environ["MANGABA_WEB_DIST"]) if "MANGABA_WEB_DIST" in os.environ else Path(__file__).parent / "web_dist"
_log = logging.getLogger(__name__)

app = FastAPI(title="Mangaba Agent", version=__version__)

# ---------------------------------------------------------------------------
# Session token for protecting sensitive endpoints (reveal).
# Persistido em $MANGABA_HOME/.dashboard_session_token para sobreviver a
# reinícios do dashboard — evita o 401 que obrigava a recarregar o navegador
# a cada restart. Injetado na SPA para que só a UI legítima possa usá-lo.
# ---------------------------------------------------------------------------
def _load_or_create_session_token() -> str:
    try:
        from mangaba_agent.mangaba_constants import get_mangaba_home

        p = get_mangaba_home() / ".dashboard_session_token"
        if p.exists():
            tok = p.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        tok = secrets.token_urlsafe(32)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(tok, encoding="utf-8")
        try:
            p.chmod(0o600)
        except Exception:
            pass
        return tok
    except Exception:
        # Falha ao persistir → cai no token efêmero (comportamento antigo).
        return secrets.token_urlsafe(32)


_SESSION_TOKEN = _load_or_create_session_token()
_SESSION_HEADER_NAME = "X-Mangaba-Session-Token"

# In-browser Chat tab (/chat, /api/pty, …).  Off unless ``mangaba dashboard --tui``
# or MANGABA_DASHBOARD_TUI=1.  Set from :func:`start_server`.
_DASHBOARD_EMBEDDED_CHAT_ENABLED = False

# Simple rate limiter for the reveal endpoint
_reveal_timestamps: List[float] = []
_REVEAL_MAX_PER_WINDOW = 5
_REVEAL_WINDOW_SECONDS = 30

# CORS: restrict to localhost origins only.  The web UI is intended to run
# locally; binding to 0.0.0.0 with allow_origins=["*"] would let any website
# read/modify config and secrets.

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Endpoints that do NOT require the session token.  Everything else under
# /api/ is gated by the auth middleware below.  Keep this list minimal —
# only truly non-sensitive, read-only endpoints belong here.
# ---------------------------------------------------------------------------
_PUBLIC_API_PATHS: frozenset = frozenset({
    "/api/health",
    "/api/whatsapp/webhook",  # chamado pela Meta (verificado por verify_token/assinatura)
    "/api/status",
    "/api/config/defaults",
    "/api/config/schema",
    "/api/model/info",
    "/api/dashboard/themes",
    "/api/dashboard/plugins",
    "/api/dashboard/plugins/rescan",
})


def _has_valid_session_token(request: Request) -> bool:
    """True if the request carries a valid dashboard session token.

    The dedicated session header avoids collisions with reverse proxies that
    already use ``Authorization`` (for example Caddy ``basic_auth``). We still
    accept the legacy Bearer path for backward compatibility with older
    dashboard bundles.
    """
    session_header = request.headers.get(_SESSION_HEADER_NAME, "")
    if session_header and hmac.compare_digest(
        session_header.encode(),
        _SESSION_TOKEN.encode(),
    ):
        return True

    auth = request.headers.get("authorization", "")
    expected = f"Bearer {_SESSION_TOKEN}"
    return hmac.compare_digest(auth.encode(), expected.encode())


def _require_token(request: Request) -> None:
    """Validate the ephemeral session token.  Raises 401 on mismatch."""
    if not _has_valid_session_token(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


# Accepted Host header values for loopback binds. DNS rebinding attacks
# point a victim browser at an attacker-controlled hostname (evil.test)
# which resolves to 127.0.0.1 after a TTL flip — bypassing same-origin
# checks because the browser now considers evil.test and our dashboard
# "same origin". Validating the Host header at the app layer rejects any
# request whose Host isn't one we bound for. See GHSA-ppp5-vxwm-4cf7.
_LOOPBACK_HOST_VALUES: frozenset = frozenset({
    "localhost", "127.0.0.1", "::1",
})


def _is_accepted_host(host_header: str, bound_host: str) -> bool:
    """True if the Host header targets the interface we bound to.

    Accepts:
    - Exact bound host (with or without port suffix)
    - Loopback aliases when bound to loopback
    - Any host when bound to 0.0.0.0 (explicit opt-in to non-loopback,
      no protection possible at this layer)
    """
    if not host_header:
        return False
    # Strip port suffix. IPv6 addresses use bracket notation:
    #   [::1]         — no port
    #   [::1]:9119    — with port
    # Plain hosts/v4:
    #   localhost:9119
    #   127.0.0.1:9119
    h = host_header.strip()
    if h.startswith("["):
        # IPv6 bracketed — port (if any) follows "]:"
        close = h.find("]")
        if close != -1:
            host_only = h[1:close]  # strip brackets
        else:
            host_only = h.strip("[]")
    else:
        host_only = h.rsplit(":", 1)[0] if ":" in h else h
    host_only = host_only.lower()

    # 0.0.0.0 bind means operator explicitly opted into all-interfaces
    # (requires --insecure per web_server.start_server). No Host-layer
    # defence can protect that mode; rely on operator network controls.
    if bound_host in {"0.0.0.0", "::"}:
        return True

    # Loopback bind: accept the loopback names
    bound_lc = bound_host.lower()
    if bound_lc in _LOOPBACK_HOST_VALUES:
        return host_only in _LOOPBACK_HOST_VALUES

    # Explicit non-loopback bind: require exact host match
    return host_only == bound_lc


@app.middleware("http")
async def host_header_middleware(request: Request, call_next):
    """Reject requests whose Host header doesn't match the bound interface.

    Defends against DNS rebinding: a victim browser on a localhost
    dashboard is tricked into fetching from an attacker hostname that
    TTL-flips to 127.0.0.1. CORS and same-origin checks don't help —
    the browser now treats the attacker origin as same-origin with the
    dashboard. Host-header validation at the app layer catches it.

    See GHSA-ppp5-vxwm-4cf7.
    """
    # Store the bound host on app.state so this middleware can read it —
    # set by start_server() at listen time.
    bound_host = getattr(app.state, "bound_host", None)
    if bound_host:
        host_header = request.headers.get("host", "")
        if not _is_accepted_host(host_header, bound_host):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        "Invalid Host header. Dashboard requests must use "
                        "the hostname the server was bound to."
                    ),
                },
            )
    return await call_next(request)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Require the session token on all /api/ routes except the public list."""
    path = request.url.path
    if path.startswith("/api/") and path not in _PUBLIC_API_PATHS:
        if not _has_valid_session_token(request):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Config schema — auto-generated from DEFAULT_CONFIG
# ---------------------------------------------------------------------------

# Manual overrides for fields that need select options or custom types
_SCHEMA_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "model": {
        "type": "string",
        "description": "Default model (e.g. anthropic/claude-sonnet-4.6)",
        "category": "general",
    },
    "model_context_length": {
        "type": "number",
        "description": "Context window override (0 = auto-detect from model metadata)",
        "category": "general",
    },
    "terminal.backend": {
        "type": "select",
        "description": "Terminal execution backend",
        "options": ["local", "docker", "ssh", "modal", "daytona", "vercel_sandbox", "singularity"],
    },
    "terminal.vercel_runtime": {
        "type": "select",
        "description": "Vercel Sandbox runtime",
        "options": ["node24", "node22", "python3.13"],  # sync with _SUPPORTED_VERCEL_RUNTIMES in terminal_tool.py
    },
    "terminal.modal_mode": {
        "type": "select",
        "description": "Modal sandbox mode",
        "options": ["sandbox", "function"],
    },
    "tts.provider": {
        "type": "select",
        "description": "Text-to-speech provider",
        "options": ["edge", "elevenlabs", "openai", "neutts"],
    },
    "stt.provider": {
        "type": "select",
        "description": "Speech-to-text provider",
        # "mistral" temporarily removed — mistralai PyPI package quarantined
        # (malicious 2.4.6 release on 2026-05-12). Restore once available.
        "options": ["local", "openai"],
    },
    "display.skin": {
        "type": "select",
        "description": "CLI visual theme",
        "options": ["default", "ares", "mono", "slate"],
    },
    "dashboard.theme": {
        "type": "select",
        "description": "Web dashboard visual theme",
        "options": ["default", "midnight", "ember", "mono", "cyberpunk", "rose"],
    },
    "display.resume_display": {
        "type": "select",
        "description": "How resumed sessions display history",
        "options": ["minimal", "full", "off"],
    },
    "display.busy_input_mode": {
        "type": "select",
        "description": "Input behavior while agent is running",
        "options": ["interrupt", "queue", "steer"],
    },
    "memory.provider": {
        "type": "select",
        "description": "Memory provider plugin",
        "options": ["builtin", "honcho"],
    },
    "approvals.mode": {
        "type": "select",
        "description": "Dangerous command approval mode",
        "options": ["ask", "yolo", "deny"],
    },
    "context.engine": {
        "type": "select",
        "description": "Context management engine",
        "options": ["default", "custom"],
    },
    "human_delay.mode": {
        "type": "select",
        "description": "Simulated typing delay mode",
        "options": ["off", "typing", "fixed"],
    },
    "logging.level": {
        "type": "select",
        "description": "Log level for agent.log",
        "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
    },
    "agent.service_tier": {
        "type": "select",
        "description": "API service tier (OpenAI/Anthropic)",
        "options": ["", "auto", "default", "flex"],
    },
    "delegation.reasoning_effort": {
        "type": "select",
        "description": "Reasoning effort for delegated subagents",
        "options": ["", "low", "medium", "high"],
    },
}

# Categories with fewer fields get merged into "general" to avoid tab sprawl.
_CATEGORY_MERGE: Dict[str, str] = {
    "privacy": "security",
    "context": "agent",
    "skills": "agent",
    "cron": "agent",
    "network": "agent",
    "checkpoints": "agent",
    "approvals": "security",
    "human_delay": "display",
    "dashboard": "display",
    "code_execution": "agent",
    "prompt_caching": "agent",
    "goals": "agent",
    # Only `telegram.reactions` currently lives under telegram — fold it in
    # with the other messaging-platform config (discord) so it isn't an
    # orphan tab of one field.
    "telegram": "discord",
}

# Display order for tabs — unlisted categories sort alphabetically after these.
_CATEGORY_ORDER = [
    "general", "agent", "terminal", "display", "delegation",
    "memory", "compression", "security", "browser", "voice",
    "tts", "stt", "logging", "discord", "auxiliary",
]


def _infer_type(value: Any) -> str:
    """Infer a UI field type from a Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "string"


def _build_schema_from_config(
    config: Dict[str, Any],
    prefix: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Walk DEFAULT_CONFIG and produce a flat dot-path → field schema dict."""
    schema: Dict[str, Dict[str, Any]] = {}
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key

        # Skip internal / version keys
        if full_key in {"_config_version",}:
            continue

        # Category is the first path component for nested keys, or "general"
        # for top-level scalar fields (model, toolsets, timezone, etc.).
        if prefix:
            category = prefix.split(".")[0]
        elif isinstance(value, dict):
            category = key
        else:
            category = "general"

        if isinstance(value, dict):
            # Recurse into nested dicts
            schema.update(_build_schema_from_config(value, full_key))
        else:
            entry: Dict[str, Any] = {
                "type": _infer_type(value),
                "description": full_key.replace(".", " → ").replace("_", " ").title(),
                "category": category,
            }
            # Apply manual overrides
            if full_key in _SCHEMA_OVERRIDES:
                entry.update(_SCHEMA_OVERRIDES[full_key])
            # Merge small categories
            entry["category"] = _CATEGORY_MERGE.get(entry["category"], entry["category"])
            schema[full_key] = entry
    return schema


CONFIG_SCHEMA = _build_schema_from_config(DEFAULT_CONFIG)

# Inject virtual fields that don't live in DEFAULT_CONFIG but are surfaced
# by the normalize/denormalize cycle.  Insert model_context_length right after
# the "model" key so it renders adjacent in the frontend.
_mcl_entry = _SCHEMA_OVERRIDES["model_context_length"]
_ordered_schema: Dict[str, Dict[str, Any]] = {}
for _k, _v in CONFIG_SCHEMA.items():
    _ordered_schema[_k] = _v
    if _k == "model":
        _ordered_schema["model_context_length"] = _mcl_entry
CONFIG_SCHEMA = _ordered_schema


class ConfigUpdate(BaseModel):
    config: dict


class EnvVarUpdate(BaseModel):
    key: str
    value: str


class EnvVarDelete(BaseModel):
    key: str


class EnvVarReveal(BaseModel):
    key: str


class ModelAssignment(BaseModel):
    """Payload for POST /api/model/set — assign a provider/model to a slot.

    scope="main"        → writes model.provider + model.default
    scope="auxiliary"   → writes auxiliary.<task>.provider + auxiliary.<task>.model
    scope="auxiliary" with task=""  → applied to every auxiliary.* slot
    scope="auxiliary" with task="__reset__"  → resets every slot to provider="auto"
    """
    scope: str
    provider: str
    model: str
    task: str = ""


_GATEWAY_HEALTH_URL = os.getenv("GATEWAY_HEALTH_URL")
try:
    _GATEWAY_HEALTH_TIMEOUT = float(os.getenv("GATEWAY_HEALTH_TIMEOUT", "3"))
except (ValueError, TypeError):
    _log.warning(
        "Invalid GATEWAY_HEALTH_TIMEOUT value %r — using default 3.0s",
        os.getenv("GATEWAY_HEALTH_TIMEOUT"),
    )
    _GATEWAY_HEALTH_TIMEOUT = 3.0

# DEPRECATED (scheduled for removal): GATEWAY_HEALTH_URL / GATEWAY_HEALTH_TIMEOUT.
# Cross-container / cross-host gateway liveness detection will be folded into a
# first-class dashboard config key so it's no longer Docker-adjacent lore buried
# in env vars.  The env vars still work for now so existing Compose deployments
# don't break.  Do not add new callers — wire new uses through the planned
# config surface.


def _probe_gateway_health() -> tuple[bool, dict | None]:
    """Probe the gateway via its HTTP health endpoint (cross-container).

    .. deprecated::
        Driven by the deprecated ``GATEWAY_HEALTH_URL`` /
        ``GATEWAY_HEALTH_TIMEOUT`` env vars.  Scheduled for removal alongside
        a move to a first-class dashboard config key.  See
        :data:`_GATEWAY_HEALTH_URL` for context.

    Uses ``/health/detailed`` first (returns full state), falling back to
    the simpler ``/health`` endpoint.  Returns ``(is_alive, body_dict)``.

    Accepts any of these as ``GATEWAY_HEALTH_URL``:
    - ``http://gateway:8642``                (base URL — recommended)
    - ``http://gateway:8642/health``         (explicit health path)
    - ``http://gateway:8642/health/detailed`` (explicit detailed path)

    This is a **blocking** call — run via ``run_in_executor`` from async code.
    """
    if not _GATEWAY_HEALTH_URL:
        return False, None

    # Normalise to base URL so we always probe the right paths regardless of
    # whether the user included /health or /health/detailed in the env var.
    base = _GATEWAY_HEALTH_URL.rstrip("/")
    if base.endswith("/health/detailed"):
        base = base[: -len("/health/detailed")]
    elif base.endswith("/health"):
        base = base[: -len("/health")]

    for path in (f"{base}/health/detailed", f"{base}/health"):
        try:
            req = urllib.request.Request(path, method="GET")
            with urllib.request.urlopen(req, timeout=_GATEWAY_HEALTH_TIMEOUT) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read())
                    return True, body
        except Exception:
            continue
    return False, None


@app.get("/api/health")
async def get_health():
    """Healthcheck leve e sem autenticação para monitoramento externo.

    Retorna 200 com ``{"ok": true}`` quando o dashboard está no ar e o gateway
    está rodando; ``ok: false`` (ainda HTTP 200) quando o gateway caiu — assim
    um supervisor consegue distinguir "processo vivo" de "serviço saudável".
    """
    try:
        pid = get_running_pid()
    except Exception:
        pid = None
    gateway_up = pid is not None

    last_activity = None
    gateway_state = None
    try:
        runtime = read_runtime_status()
        if runtime:
            gateway_state = runtime.get("gateway_state")
            last_activity = runtime.get("updated_at") or runtime.get("last_activity")
    except Exception:
        pass

    return {
        "ok": gateway_up,
        "dashboard": True,
        "gateway": gateway_up,
        "gateway_pid": pid,
        "gateway_state": gateway_state,
        "last_activity": last_activity,
    }


@app.get("/api/status")
async def get_status():
    current_ver, latest_ver = check_config_version()

    # --- Gateway liveness detection ---
    # Try local PID check first (same-host).  If that fails and a remote
    # GATEWAY_HEALTH_URL is configured, probe the gateway over HTTP so the
    # dashboard works when the gateway runs in a separate container.
    gateway_pid = get_running_pid()
    gateway_running = gateway_pid is not None
    remote_health_body: dict | None = None

    if not gateway_running and _GATEWAY_HEALTH_URL:
        loop = asyncio.get_running_loop()
        alive, remote_health_body = await loop.run_in_executor(
            None, _probe_gateway_health
        )
        if alive:
            gateway_running = True
            # PID from the remote container (display only — not locally valid)
            if remote_health_body:
                gateway_pid = remote_health_body.get("pid")

    gateway_state = None
    gateway_platforms: dict = {}
    gateway_exit_reason = None
    gateway_updated_at = None
    configured_gateway_platforms: set[str] | None = None
    try:
        from gateway.config import load_gateway_config

        gateway_config = load_gateway_config()
        configured_gateway_platforms = {
            platform.value for platform in gateway_config.get_connected_platforms()
        }
    except Exception:
        configured_gateway_platforms = None

    # Prefer the detailed health endpoint response (has full state) when the
    # local runtime status file is absent or stale (cross-container).
    runtime = read_runtime_status()
    if runtime is None and remote_health_body and remote_health_body.get("gateway_state"):
        runtime = remote_health_body

    if runtime:
        gateway_state = runtime.get("gateway_state")
        gateway_platforms = runtime.get("platforms") or {}
        if configured_gateway_platforms is not None:
            gateway_platforms = {
                key: value
                for key, value in gateway_platforms.items()
                if key in configured_gateway_platforms
            }
        gateway_exit_reason = runtime.get("exit_reason")
        gateway_updated_at = runtime.get("updated_at")
        if not gateway_running:
            gateway_state = gateway_state if gateway_state in {"stopped", "startup_failed"} else "stopped"
            gateway_platforms = {}
        elif gateway_running and remote_health_body is not None:
            # The health probe confirmed the gateway is alive, but the local
            # runtime status file may be stale (cross-container).  Override
            # stopped/None state so the dashboard shows the correct badge.
            if gateway_state in {None, "stopped"}:
                gateway_state = "running"

    # If there was no runtime info at all but the health probe confirmed alive,
    # ensure we still report the gateway as running (no shared volume scenario).
    if gateway_running and gateway_state is None and remote_health_body is not None:
        gateway_state = "running"

    active_sessions = 0
    try:
        from mangaba_agent.mangaba_state import SessionDB
        db = SessionDB()
        try:
            sessions = db.list_sessions_rich(limit=50)
            now = time.time()
            active_sessions = sum(
                1 for s in sessions
                if s.get("ended_at") is None
                and (now - s.get("last_active", s.get("started_at", 0))) < 300
            )
        finally:
            db.close()
    except Exception:
        pass

    return {
        "version": __version__,
        "release_date": __release_date__,
        "mangaba_home": str(get_mangaba_home()),
        "config_path": str(get_config_path()),
        "env_path": str(get_env_path()),
        "config_version": current_ver,
        "latest_config_version": latest_ver,
        "gateway_running": gateway_running,
        "gateway_pid": gateway_pid,
        "gateway_health_url": _GATEWAY_HEALTH_URL,
        "gateway_state": gateway_state,
        "gateway_platforms": gateway_platforms,
        "gateway_exit_reason": gateway_exit_reason,
        "gateway_updated_at": gateway_updated_at,
        "active_sessions": active_sessions,
        "rate_limit": _rate_limit_summary(),
    }


def _rate_limit_summary() -> dict:
    """Resumo de eventos de rate-limit (modelo/PNCP/etc.) p/ o banner do dashboard."""
    try:
        from mangaba_cli import rate_limit_log

        return rate_limit_log.summary(minutes=15)
    except Exception:
        return {"active": False, "count": 0, "by_source": {}, "last": None}


# ---------------------------------------------------------------------------
# Gateway + update actions (invoked from the Status page).
#
# Both commands are spawned as detached subprocesses so the HTTP request
# returns immediately.  stdin is closed (``DEVNULL``) so any stray ``input()``
# calls fail fast with EOF rather than hanging forever.  stdout/stderr are
# streamed to a per-action log file under ``~/.mangaba/logs/<action>.log`` so
# the dashboard can tail them back to the user.
# ---------------------------------------------------------------------------

_ACTION_LOG_DIR: Path = get_mangaba_home() / "logs"

# Short ``name`` (from the URL) → absolute log file path.
_ACTION_LOG_FILES: Dict[str, str] = {
    "gateway-restart": "gateway-restart.log",
    "mangaba-update": "mangaba-update.log",
}

# ``name`` → most recently spawned Popen handle.  Used so ``status`` can
# report liveness and exit code without shelling out to ``ps``.
_ACTION_PROCS: Dict[str, subprocess.Popen] = {}


def _spawn_mangaba_action(subcommand: List[str], name: str) -> subprocess.Popen:
    """Spawn ``mangaba <subcommand>`` detached and record the Popen handle.

    Uses the running interpreter's ``mangaba_cli.main`` module so the action
    inherits the same venv/PYTHONPATH the web server is using.
    """
    log_file_name = _ACTION_LOG_FILES[name]
    _ACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _ACTION_LOG_DIR / log_file_name
    log_file = open(log_path, "ab", buffering=0)
    log_file.write(
        f"\n=== {name} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
    )

    cmd = [sys.executable, "-m", "mangaba_cli.main", *subcommand]

    popen_kwargs: Dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "env": {**os.environ, "MANGABA_NONINTERACTIVE": "1"},
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    _ACTION_PROCS[name] = proc
    return proc


def _tail_lines(path: Path, n: int) -> List[str]:
    """Return the last ``n`` lines of ``path``.  Reads the whole file — fine
    for our small per-action logs.  Binary-decoded with ``errors='replace'``
    so log corruption doesn't 500 the endpoint."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n:] if n > 0 else lines


@app.post("/api/gateway/restart")
async def restart_gateway():
    """Kick off a ``mangaba gateway restart`` in the background."""
    try:
        proc = _spawn_mangaba_action(["gateway", "restart"], "gateway-restart")
    except Exception as exc:
        _log.exception("Failed to spawn gateway restart")
        raise HTTPException(status_code=500, detail=f"Failed to restart gateway: {exc}")
    return {
        "ok": True,
        "pid": proc.pid,
        "name": "gateway-restart",
    }


def _fleet_member_to_dict(m) -> Dict[str, Any]:
    return {
        "name": m.name,
        "path": str(m.path),
        "running": bool(m.running),
        "pid": m.pid,
        "model": m.model,
        "provider": m.provider,
        "skills": m.skills,
        "description": m.description,
        "is_default": m.is_default,
        "platforms": m.platforms,
    }


@app.get("/api/fleet")
async def get_fleet():
    """Snapshot of every agent (profile) and its gateway status."""
    try:
        from mangaba_cli import fleet as _fleet
        return {"members": [_fleet_member_to_dict(m) for m in _fleet.collect_fleet()]}
    except Exception as exc:
        _log.exception("GET /api/fleet failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/fleet/{name}/logs")
async def get_fleet_logs(name: str, lines: int = 60):
    """Tail a profile's gateway.log."""
    try:
        from mangaba_cli import fleet as _fleet
        m = _fleet.find_member(name)
        if m is None:
            raise HTTPException(status_code=404, detail=f"Agente '{name}' não encontrado")
        return {"name": name, "log": _fleet.read_gateway_log(m.path, lines=lines)}
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("GET /api/fleet/%s/logs failed", name)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/fleet/{name}/{action}")
async def fleet_action(name: str, action: str):
    """start | stop | restart a profile's gateway."""
    try:
        from mangaba_cli import fleet as _fleet
        fn = {"start": _fleet.start_profile, "stop": _fleet.stop_profile,
              "restart": _fleet.restart_profile}.get(action)
        if fn is None:
            raise HTTPException(status_code=400, detail=f"Ação inválida: {action}")
        ok, message = fn(name)
        return {"ok": ok, "message": message}
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("POST /api/fleet/%s/%s failed", name, action)
        raise HTTPException(status_code=500, detail=str(exc))


class FleetBroadcast(BaseModel):
    message: str


@app.post("/api/fleet/broadcast")
async def fleet_broadcast(body: FleetBroadcast):
    """Send an operator notice to every agent's home_channel (never customer chats)."""
    try:
        from mangaba_cli import fleet as _fleet
        reached, channels, skipped = _fleet.broadcast(body.message)
        return {"ok": True, "reached": reached, "channels": channels, "skipped": skipped}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _log.exception("POST /api/fleet/broadcast failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/fleet/{name}/platforms")
async def get_fleet_member_platforms(name: str):
    """Retorna a configuração de plataformas de um profile."""
    try:
        from mangaba_cli import fleet as _fleet
        m = _fleet.find_member(name)
        if m is None:
            raise HTTPException(status_code=404, detail=f"Agente '{name}' não encontrado")
        cfg_path = m.path / "config.yaml"
        if not cfg_path.exists():
            return {"platforms": {}}
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
        return {"platforms": cfg.get("platforms") or {}}
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("GET /api/fleet/%s/platforms failed", name)
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/api/fleet/{name}/platforms")
async def update_fleet_member_platforms(name: str, body: Dict[str, Any]):
    """Atualiza a seção platforms do config.yaml de um profile."""
    try:
        from mangaba_cli import fleet as _fleet
        m = _fleet.find_member(name)
        if m is None:
            raise HTTPException(status_code=404, detail=f"Agente '{name}' não encontrado")
        cfg_path = m.path / "config.yaml"
        cfg: dict = {}
        if cfg_path.exists():
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        platforms_data = body.get("platforms")
        if platforms_data is None:
            raise HTTPException(status_code=400, detail="Campo 'platforms' é obrigatório no body")
        cfg["platforms"] = platforms_data
        cfg_path.write_text(yaml.dump(cfg, allow_unicode=True, default_flow_style=False))
        return {"ok": True, "platforms": platforms_data}
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("PUT /api/fleet/%s/platforms failed", name)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/mangaba/update")
async def update_mangaba():
    """Kick off ``mangaba update`` in the background."""
    try:
        proc = _spawn_mangaba_action(["update"], "mangaba-update")
    except Exception as exc:
        _log.exception("Failed to spawn mangaba update")
        raise HTTPException(status_code=500, detail=f"Failed to start update: {exc}")
    return {
        "ok": True,
        "pid": proc.pid,
        "name": "mangaba-update",
    }


@app.get("/api/actions/{name}/status")
async def get_action_status(name: str, lines: int = 200):
    """Tail an action log and report whether the process is still running."""
    log_file_name = _ACTION_LOG_FILES.get(name)
    if log_file_name is None:
        raise HTTPException(status_code=404, detail=f"Unknown action: {name}")

    log_path = _ACTION_LOG_DIR / log_file_name
    tail = _tail_lines(log_path, min(max(lines, 1), 2000))

    proc = _ACTION_PROCS.get(name)
    if proc is None:
        running = False
        exit_code: Optional[int] = None
        pid: Optional[int] = None
    else:
        exit_code = proc.poll()
        running = exit_code is None
        pid = proc.pid

    return {
        "name": name,
        "running": running,
        "exit_code": exit_code,
        "pid": pid,
        "lines": tail,
    }


@app.get("/api/sessions")
async def get_sessions(limit: int = 20, offset: int = 0):
    try:
        from mangaba_agent.mangaba_state import SessionDB
        db = SessionDB()
        try:
            sessions = db.list_sessions_rich(limit=limit, offset=offset)
            total = db.session_count()
            now = time.time()
            for s in sessions:
                s["is_active"] = (
                    s.get("ended_at") is None
                    and (now - s.get("last_active", s.get("started_at", 0))) < 300
                )
            return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}
        finally:
            db.close()
    except Exception:
        _log.exception("GET /api/sessions failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/sessions/fleet")
async def get_fleet_sessions(limit: int = 10, offset: int = 0):
    """Retorna sessões recentes de TODOS os profiles da frota com paginação."""
    try:
        from mangaba_cli import fleet as _fleet
        from mangaba_agent.mangaba_state import SessionDB

        all_sessions: List[Dict[str, Any]] = []
        for member in _fleet.collect_fleet():
            db_path = member.path / "state.db"
            if not db_path.exists():
                continue
            try:
                db = SessionDB(db_path=db_path)
                try:
                    # busca mais que limit para ter total correto após merge
                    sessions = db.list_sessions_rich(limit=limit + offset)
                    for s in sessions:
                        s_dict = dict(s) if not isinstance(s, dict) else s
                        s_dict["_profile"] = member.name
                        s_dict["_profile_running"] = member.running
                        all_sessions.append(s_dict)
                finally:
                    db.close()
            except Exception:
                pass

        # Ordena por last_active desc
        all_sessions.sort(
            key=lambda s: s.get("last_active") or s.get("started_at") or 0,
            reverse=True,
        )
        total = len(all_sessions)
        page = all_sessions[offset: offset + limit]
        return {"sessions": page, "total": total}
    except Exception as exc:
        _log.exception("GET /api/sessions/fleet failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/sessions/search")
async def search_sessions(q: str = "", limit: int = 20):
    """Full-text search across session message content using FTS5."""
    if not q or not q.strip():
        return {"results": []}
    try:
        from mangaba_agent.mangaba_state import SessionDB
        db = SessionDB()
        try:
            # Auto-add prefix wildcards so partial words match
            # e.g. "nimb" → "nimb*" matches "nimby"
            # Preserve quoted phrases and existing wildcards as-is
            import re
            terms = []
            for token in re.findall(r'"[^"]*"|\S+', q.strip()):
                if token.startswith('"') or token.endswith("*"):
                    terms.append(token)
                else:
                    terms.append(token + "*")
            prefix_query = " ".join(terms)
            matches = db.search_messages(query=prefix_query, limit=limit)
            # Group by session_id — return unique sessions with their best snippet
            seen: dict = {}
            for m in matches:
                sid = m["session_id"]
                if sid not in seen:
                    seen[sid] = {
                        "session_id": sid,
                        "snippet": m.get("snippet", ""),
                        "role": m.get("role"),
                        "source": m.get("source"),
                        "model": m.get("model"),
                        "session_started": m.get("session_started"),
                    }

            # Enriquece cada match com o SessionInfo completo (título, contadores,
            # last_active…) para a tela renderizar a linha direto dos resultados,
            # sem depender da página atual. Indexa a lista rica por id.
            now = time.time()
            rich_by_id: dict = {}
            try:
                total = db.session_count()
                rich = db.list_sessions_rich(limit=min(total, 5000), offset=0)
                for s in rich:
                    s["is_active"] = (
                        s.get("ended_at") is None
                        and (now - s.get("last_active", s.get("started_at", 0))) < 300
                    )
                    rich_by_id[s["id"]] = s
            except Exception:
                _log.debug("search: list_sessions_rich enrichment failed", exc_info=True)

            for sid, r in seen.items():
                info = rich_by_id.get(sid)
                if info is None:
                    # Fallback mínimo quando a sessão não está na lista rica
                    # (ex.: sessão-filha/compressão projetada para outro id).
                    info = {
                        "id": sid,
                        "source": r.get("source"),
                        "model": r.get("model"),
                        "title": None,
                        "started_at": r.get("session_started") or 0,
                        "ended_at": None,
                        "last_active": r.get("session_started") or 0,
                        "is_active": False,
                        "message_count": 0,
                        "tool_call_count": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    }
                r["session"] = info
            return {"results": list(seen.values())}
        finally:
            db.close()
    except Exception:
        _log.exception("GET /api/sessions/search failed")
        raise HTTPException(status_code=500, detail="Search failed")


def _normalize_config_for_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize config for the web UI.

    Mangaba supports ``model`` as either a bare string (``"anthropic/claude-sonnet-4"``)
    or a dict (``{default: ..., provider: ..., base_url: ...}``).  The schema is built
    from DEFAULT_CONFIG where ``model`` is a string, but user configs often have the
    dict form.  Normalize to the string form so the frontend schema matches.

    Also surfaces ``model_context_length`` as a top-level field so the web UI can
    display and edit it.  A value of 0 means "auto-detect".
    """
    config = dict(config)  # shallow copy
    model_val = config.get("model")
    if isinstance(model_val, dict):
        # Extract context_length before flattening the dict
        ctx_len = model_val.get("context_length", 0)
        config["model"] = model_val.get("default", model_val.get("name", ""))
        config["model_context_length"] = ctx_len if isinstance(ctx_len, int) else 0
    else:
        config["model_context_length"] = 0
    return config


@app.get("/api/config")
async def get_config():
    config = _normalize_config_for_web(load_config())
    # Strip internal keys that the frontend shouldn't see or send back
    return {k: v for k, v in config.items() if not k.startswith("_")}


@app.get("/api/config/defaults")
async def get_defaults():
    return DEFAULT_CONFIG


@app.get("/api/config/schema")
async def get_schema():
    return {"fields": CONFIG_SCHEMA, "category_order": _CATEGORY_ORDER}


_EMPTY_MODEL_INFO: dict = {
    "model": "",
    "provider": "",
    "auto_context_length": 0,
    "config_context_length": 0,
    "effective_context_length": 0,
    "capabilities": {},
}


@app.get("/api/model/info")
def get_model_info():
    """Return resolved model metadata for the currently configured model.

    Calls the same context-length resolution chain the agent uses, so the
    frontend can display "Auto-detected: 200K" alongside the override field.
    Also returns model capabilities (vision, reasoning, tools) when available.
    """
    try:
        cfg = load_config()
        model_cfg = cfg.get("model", "")

        # Extract model name and provider from the config
        if isinstance(model_cfg, dict):
            model_name = model_cfg.get("default", model_cfg.get("name", ""))
            provider = model_cfg.get("provider", "")
            base_url = model_cfg.get("base_url", "")
            config_ctx = model_cfg.get("context_length")
        else:
            model_name = str(model_cfg) if model_cfg else ""
            provider = ""
            base_url = ""
            config_ctx = None

        if not model_name:
            return dict(_EMPTY_MODEL_INFO, provider=provider)

        # Resolve auto-detected context length (pass config_ctx=None to get
        # purely auto-detected value, then separately report the override)
        try:
            from agent.model_metadata import get_model_context_length
            auto_ctx = get_model_context_length(
                model=model_name,
                base_url=base_url,
                provider=provider,
                config_context_length=None,  # ignore override — we want auto value
            )
        except Exception:
            auto_ctx = 0

        config_ctx_int = 0
        if isinstance(config_ctx, int) and config_ctx > 0:
            config_ctx_int = config_ctx

        # Effective is what the agent actually uses
        effective_ctx = config_ctx_int if config_ctx_int > 0 else auto_ctx

        # Try to get model capabilities from models.dev
        caps = {}
        try:
            from agent.models_dev import get_model_capabilities
            mc = get_model_capabilities(provider=provider, model=model_name)
            if mc is not None:
                caps = {
                    "supports_tools": mc.supports_tools,
                    "supports_vision": mc.supports_vision,
                    "supports_reasoning": mc.supports_reasoning,
                    "context_window": mc.context_window,
                    "max_output_tokens": mc.max_output_tokens,
                    "model_family": mc.model_family,
                }
        except Exception:
            pass

        return {
            "model": model_name,
            "provider": provider,
            "auto_context_length": auto_ctx,
            "config_context_length": config_ctx_int,
            "effective_context_length": effective_ctx,
            "capabilities": caps,
        }
    except Exception:
        _log.exception("GET /api/model/info failed")
        return dict(_EMPTY_MODEL_INFO)


# ---------------------------------------------------------------------------
# Model assignment — pick provider+model for main slot or auxiliary slots.
# Mirrors the model.options JSON-RPC from tui_gateway but uses REST so the
# Models page (which has no chat PTY open) can drive it.
# ---------------------------------------------------------------------------

# Canonical auxiliary task slots. Keep in sync with DEFAULT_CONFIG["auxiliary"]
# in mangaba_cli/config.py — listed here for deterministic ordering in the UI.
_AUX_TASK_SLOTS: Tuple[str, ...] = (
    "vision",
    "web_extract",
    "compression",
    "skills_hub",
    "approval",
    "mcp",
    "title_generation",
    "triage_specifier",
    "kanban_decomposer",
    "profile_describer",
    "curator",
)


@app.get("/api/model/options")
def get_model_options():
    """Return authenticated providers + their curated model lists.

    REST equivalent of the ``model.options`` JSON-RPC on tui_gateway, so the
    dashboard Models page can render the picker without a live chat session.
    The response shape matches ``model.options`` 1:1 so ``ModelPickerDialog``
    can share the same types.
    """
    try:
        from mangaba_cli.inventory import build_models_payload, load_picker_context

        return build_models_payload(
            load_picker_context(),
            max_models=50,
            include_unconfigured=True,
            picker_hints=True,
            canonical_order=True,
        )
    except Exception:
        _log.exception("GET /api/model/options failed")
        raise HTTPException(status_code=500, detail="Failed to list model options")


@app.get("/api/model/auxiliary")
def get_auxiliary_models():
    """Return current auxiliary task assignments.

    Shape:
      {
        "tasks": [
          {"task": "vision", "provider": "auto", "model": "", "base_url": ""},
          ...
        ],
        "main": {"provider": "openrouter", "model": "anthropic/claude-opus-4.7"},
      }
    """
    try:
        cfg = load_config()
        aux_cfg = cfg.get("auxiliary", {})
        if not isinstance(aux_cfg, dict):
            aux_cfg = {}

        tasks = []
        for slot in _AUX_TASK_SLOTS:
            slot_cfg = aux_cfg.get(slot, {}) if isinstance(aux_cfg.get(slot), dict) else {}
            tasks.append({
                "task": slot,
                "provider": str(slot_cfg.get("provider", "auto") or "auto"),
                "model": str(slot_cfg.get("model", "") or ""),
                "base_url": str(slot_cfg.get("base_url", "") or ""),
            })

        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            main = {
                "provider": str(model_cfg.get("provider", "") or ""),
                "model": str(model_cfg.get("default", model_cfg.get("name", "")) or ""),
            }
        else:
            main = {"provider": "", "model": str(model_cfg) if model_cfg else ""}

        return {"tasks": tasks, "main": main}
    except Exception:
        _log.exception("GET /api/model/auxiliary failed")
        raise HTTPException(status_code=500, detail="Failed to read auxiliary config")


@app.post("/api/model/set")
async def set_model_assignment(body: ModelAssignment):
    """Assign a model to the main slot or an auxiliary task slot.

    Writes to ``~/.mangaba/config.yaml`` — applies to **new** sessions only.
    The currently running chat PTY (if any) is not affected; use the
    ``/model`` slash command inside a chat to hot-swap that specific session.
    """
    scope = (body.scope or "").strip().lower()
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    task = (body.task or "").strip().lower()

    if scope not in {"main", "auxiliary"}:
        raise HTTPException(status_code=400, detail="scope must be 'main' or 'auxiliary'")

    try:
        cfg = load_config()

        if scope == "main":
            if not provider or not model:
                raise HTTPException(status_code=400, detail="provider and model required for main")
            model_cfg = cfg.get("model", {})
            if not isinstance(model_cfg, dict):
                model_cfg = {}
            model_cfg["provider"] = provider
            model_cfg["default"] = model
            # Clear stale base_url so the resolver picks the provider's own default.
            if "base_url" in model_cfg and model_cfg.get("base_url"):
                model_cfg["base_url"] = ""
            # Also clear hardcoded context_length override — new model may have
            # a different context window.
            if "context_length" in model_cfg:
                model_cfg.pop("context_length", None)
            cfg["model"] = model_cfg
            save_config(cfg)
            return {"ok": True, "scope": "main", "provider": provider, "model": model}

        # scope == "auxiliary"
        aux = cfg.get("auxiliary")
        if not isinstance(aux, dict):
            aux = {}

        if task == "__reset__":
            # Reset every slot to provider="auto", model="" — keeps other fields intact.
            for slot in _AUX_TASK_SLOTS:
                slot_cfg = aux.get(slot)
                if not isinstance(slot_cfg, dict):
                    slot_cfg = {}
                slot_cfg["provider"] = "auto"
                slot_cfg["model"] = ""
                aux[slot] = slot_cfg
            cfg["auxiliary"] = aux
            save_config(cfg)
            return {"ok": True, "scope": "auxiliary", "reset": True}

        if not provider:
            raise HTTPException(status_code=400, detail="provider required for auxiliary")

        targets = [task] if task else list(_AUX_TASK_SLOTS)
        for slot in targets:
            if slot not in _AUX_TASK_SLOTS:
                raise HTTPException(status_code=400, detail=f"unknown auxiliary task: {slot}")
            slot_cfg = aux.get(slot)
            if not isinstance(slot_cfg, dict):
                slot_cfg = {}
            slot_cfg["provider"] = provider
            slot_cfg["model"] = model
            aux[slot] = slot_cfg

        cfg["auxiliary"] = aux
        save_config(cfg)
        return {
            "ok": True,
            "scope": "auxiliary",
            "tasks": targets,
            "provider": provider,
            "model": model,
        }
    except HTTPException:
        raise
    except Exception:
        _log.exception("POST /api/model/set failed")
        raise HTTPException(status_code=500, detail="Failed to save model assignment")




def _denormalize_config_from_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse _normalize_config_for_web before saving.

    Reconstructs ``model`` as a dict by reading the current on-disk config
    to recover model subkeys (provider, base_url, api_mode, etc.) that were
    stripped from the GET response.  The frontend only sees model as a flat
    string; the rest is preserved transparently.

    Also handles ``model_context_length`` — writes it back into the model dict
    as ``context_length``.  A value of 0 or absent means "auto-detect" (omitted
    from the dict so get_model_context_length() uses its normal resolution).
    """
    config = dict(config)
    # Remove any _model_meta that might have leaked in (shouldn't happen
    # with the stripped GET response, but be defensive)
    config.pop("_model_meta", None)

    # Extract and remove model_context_length before processing model
    ctx_override = config.pop("model_context_length", 0)
    if not isinstance(ctx_override, int):
        try:
            ctx_override = int(ctx_override)
        except (TypeError, ValueError):
            ctx_override = 0

    model_val = config.get("model")
    if isinstance(model_val, str) and model_val:
        # Read the current disk config to recover model subkeys
        try:
            disk_config = load_config()
            disk_model = disk_config.get("model")
            if isinstance(disk_model, dict):
                # Preserve all subkeys, update default with the new value
                disk_model["default"] = model_val
                # Write context_length into the model dict (0 = remove/auto)
                if ctx_override > 0:
                    disk_model["context_length"] = ctx_override
                else:
                    disk_model.pop("context_length", None)
                config["model"] = disk_model
            # Model was previously a bare string — upgrade to dict if
            # user is setting a context_length override
            elif ctx_override > 0:
                config["model"] = {
                    "default": model_val,
                    "context_length": ctx_override,
                }
        except Exception:
            pass  # can't read disk config — just use the string form
    return config


@app.put("/api/config")
async def update_config(body: ConfigUpdate):
    try:
        save_config(_denormalize_config_from_web(body.config))
        return {"ok": True}
    except Exception:
        _log.exception("PUT /api/config failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/env")
async def get_env_vars():
    env_on_disk = load_env()
    result = {}
    for var_name, info in OPTIONAL_ENV_VARS.items():
        value = env_on_disk.get(var_name)
        result[var_name] = {
            "is_set": bool(value),
            "redacted_value": redact_key(value) if value else None,
            "description": info.get("description", ""),
            "url": info.get("url"),
            "category": info.get("category", ""),
            "is_password": info.get("password", False),
            "tools": info.get("tools", []),
            "advanced": info.get("advanced", False),
        }
    return result


@app.put("/api/env")
async def set_env_var(body: EnvVarUpdate):
    try:
        save_env_value(body.key, body.value)
        return {"ok": True, "key": body.key}
    except Exception:
        _log.exception("PUT /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.delete("/api/env")
async def remove_env_var(body: EnvVarDelete):
    try:
        removed = remove_env_value(body.key)
        if not removed:
            raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")
        return {"ok": True, "key": body.key}
    except HTTPException:
        raise
    except Exception:
        _log.exception("DELETE /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/env/reveal")
async def reveal_env_var(body: EnvVarReveal, request: Request):
    """Return the real (unredacted) value of a single env var.

    Protected by:
    - Ephemeral session token (generated per server start, injected into SPA)
    - Rate limiting (max 5 reveals per 30s window)
    - Audit logging
    """
    # --- Token check ---
    _require_token(request)

    # --- Rate limit ---
    now = time.time()
    cutoff = now - _REVEAL_WINDOW_SECONDS
    _reveal_timestamps[:] = [t for t in _reveal_timestamps if t > cutoff]
    if len(_reveal_timestamps) >= _REVEAL_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many reveal requests. Try again shortly.")
    _reveal_timestamps.append(now)

    # --- Reveal ---
    env_on_disk = load_env()
    value = env_on_disk.get(body.key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")

    _log.info("env/reveal: %s", body.key)
    return {"key": body.key, "value": value}


# ---------------------------------------------------------------------------
# Memory API — ver/editar a memória do agente (MEMORY.md) e o perfil do
# usuário (USER.md) direto no dashboard, como blocos editáveis.
# ---------------------------------------------------------------------------

_MEMORY_FILES = {"memory": "MEMORY.md", "user": "USER.md"}


def _memory_dir():
    try:
        from tools.memory_tool import get_memory_dir
        return get_memory_dir()
    except Exception:  # noqa: BLE001
        from mangaba_cli.config import get_mangaba_home
        return get_mangaba_home() / "memories"


class MemoryWrite(BaseModel):
    target: str  # "memory" | "user"
    content: str


class MemoryReset(BaseModel):
    target: str = "all"  # "all" | "memory" | "user"


@app.get("/api/memory")
def get_memory():
    """Conteúdo + uso dos blocos de memória e o provedor configurado."""
    try:
        from mangaba_cli.config import load_config

        mem_dir = _memory_dir()
        cfg = load_config()
        mcfg = cfg.get("memory") or {}

        def _read(name: str) -> str:
            p = mem_dir / name
            try:
                return p.read_text(encoding="utf-8") if p.exists() else ""
            except Exception:  # noqa: BLE001
                return ""

        mem = _read("MEMORY.md")
        usr = _read("USER.md")
        return {
            "memory": {
                "content": mem,
                "chars": len(mem),
                "limit": int(mcfg.get("memory_char_limit", 2200)),
            },
            "user": {
                "content": usr,
                "chars": len(usr),
                "limit": int(mcfg.get("user_char_limit", 1375)),
            },
            "provider": str(mcfg.get("provider") or ""),
            "memory_enabled": bool(mcfg.get("memory_enabled", True)),
            "user_profile_enabled": bool(mcfg.get("user_profile_enabled", True)),
        }
    except Exception as exc:  # noqa: BLE001
        _log.exception("GET /api/memory failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/api/memory")
def put_memory(body: MemoryWrite):
    """Salva o conteúdo de um bloco de memória (MEMORY.md ou USER.md)."""
    try:
        fname = _MEMORY_FILES.get(body.target)
        if not fname:
            raise HTTPException(status_code=400, detail="target deve ser 'memory' ou 'user'")
        mem_dir = _memory_dir()
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / fname).write_text(body.content, encoding="utf-8")
        return {"ok": True, "chars": len(body.content)}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.exception("PUT /api/memory failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/memory/reset")
def reset_memory(body: MemoryReset):
    """Limpa a memória do agente, o perfil do usuário, ou ambos."""
    try:
        mem_dir = _memory_dir()
        targets = (
            ["memory", "user"] if body.target == "all" else [body.target]
        )
        for t in targets:
            fname = _MEMORY_FILES.get(t)
            if fname and (mem_dir / fname).exists():
                (mem_dir / fname).write_text("", encoding="utf-8")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        _log.exception("POST /api/memory/reset failed")
        raise HTTPException(status_code=500, detail=str(exc))


def _load_rag_module():
    """Carrega o módulo do provider de RAG (mangaba_rag) de forma isolada."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    init = root / "plugins" / "memory" / "mangaba_rag" / "__init__.py"
    spec = importlib.util.spec_from_file_location("mangaba_rag_dash", str(init))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@app.get("/api/rag/status")
def rag_status():
    """Estado da base de conhecimento RAG (mangaba.ia.br)."""
    try:
        import json as _json

        from mangaba_cli.config import load_config

        mod = _load_rag_module()
        path = mod._index_path()
        enabled = str((load_config().get("memory") or {}).get("provider") or "") == "mangaba_rag"
        info = {
            "enabled": enabled,
            "source": mod.SOURCE_BASE,
            "indexed": path.exists(),
            "pages": 0,
            "chunks": 0,
            "built_at": None,
        }
        if path.exists():
            data = _json.loads(path.read_text(encoding="utf-8"))
            info["pages"] = int(data.get("pages", 0))
            info["chunks"] = len(data.get("chunks") or [])
            info["built_at"] = data.get("built_at")
        return info
    except Exception as exc:  # noqa: BLE001
        _log.exception("GET /api/rag/status failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/rag/reindex")
def rag_reindex():
    """(Re)constrói a base de conhecimento a partir de mangaba.ia.br."""
    try:
        mod = _load_rag_module()
        stats = mod.reindex()
        return {"ok": True, **stats}
    except Exception as exc:  # noqa: BLE001
        _log.exception("POST /api/rag/reindex failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/rag/enable")
def rag_enable(enable: bool = True):
    """Liga/desliga o provider de RAG (memory.provider)."""
    try:
        from mangaba_cli.config import load_config, save_config

        cfg = load_config()
        mem = cfg.setdefault("memory", {})
        mem["provider"] = "mangaba_rag" if enable else ""
        save_config(cfg)
        return {"ok": True, "enabled": bool(enable)}
    except Exception as exc:  # noqa: BLE001
        _log.exception("POST /api/rag/enable failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/traces")
def get_traces(limit: int = 100, hours: int = 24):
    """Observabilidade: traces recentes + resumo (latência, sucesso, tokens, nota)."""
    try:
        from mangaba_cli import trace_ledger
        from mangaba_cli.config import load_config

        obs = (load_config().get("observability") or {})
        return {
            "traces": trace_ledger.recent(limit),
            "stats": trace_ledger.stats(hours),
            "eval_enabled": bool(obs.get("eval")),
        }
    except Exception as exc:  # noqa: BLE001
        _log.exception("GET /api/traces failed")
        raise HTTPException(status_code=500, detail=str(exc))


class GuardrailsConfig(BaseModel):
    enabled: bool = False
    redact_pii: bool = True
    mode: str = "redact"  # redact | block
    llm_check: bool = False


@app.get("/api/guardrails")
def get_guardrails():
    from mangaba_cli.config import load_config

    g = (load_config().get("guardrails") or {})
    return {
        "enabled": bool(g.get("enabled", False)),
        "redact_pii": bool(g.get("redact_pii", True)),
        "mode": g.get("mode", "redact"),
        "llm_check": bool(g.get("llm_check", False)),
    }


@app.put("/api/guardrails")
def set_guardrails(body: GuardrailsConfig):
    try:
        from mangaba_cli.config import load_config, save_config

        cfg = load_config()
        cfg["guardrails"] = {
            "enabled": bool(body.enabled),
            "redact_pii": bool(body.redact_pii),
            "mode": body.mode if body.mode in ("redact", "block") else "redact",
            "llm_check": bool(body.llm_check),
        }
        save_config(cfg)
        return {"ok": True, **cfg["guardrails"]}
    except Exception as exc:  # noqa: BLE001
        _log.exception("PUT /api/guardrails failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/api/observability")
def set_observability(eval: bool = False):
    """Liga/desliga a avaliação de qualidade (LLM-juiz) por turno."""
    try:
        from mangaba_cli.config import load_config, save_config

        cfg = load_config()
        cfg.setdefault("observability", {})["eval"] = bool(eval)
        save_config(cfg)
        return {"ok": True, "eval": bool(eval)}
    except Exception as exc:  # noqa: BLE001
        _log.exception("PUT /api/observability failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/usage")
def get_usage(days: int = 14):
    """Uso de tokens (hoje + série recente) e estado do teto diário."""
    try:
        from mangaba_cli import usage_ledger

        return {
            "today": usage_ledger.get_today(),
            "recent": usage_ledger.get_recent(days),
            "budget": usage_ledger.budget_status(),
        }
    except Exception as exc:  # noqa: BLE001
        _log.exception("GET /api/usage failed")
        raise HTTPException(status_code=500, detail=str(exc))


class UsageBudget(BaseModel):
    daily_token_limit: int = 0
    budget_mode: str = "warn"  # "warn" | "block"


@app.put("/api/usage/budget")
def set_usage_budget(body: UsageBudget):
    """Configura o teto diário de tokens e o modo (avisar ou bloquear)."""
    try:
        from mangaba_cli.config import load_config, save_config
        from mangaba_cli import usage_ledger

        mode = body.budget_mode if body.budget_mode in ("warn", "block") else "warn"
        cfg = load_config()
        usage = cfg.setdefault("usage", {})
        usage["daily_token_limit"] = max(0, int(body.daily_token_limit or 0))
        usage["budget_mode"] = mode
        save_config(cfg)
        return {"ok": True, "budget": usage_ledger.budget_status()}
    except Exception as exc:  # noqa: BLE001
        _log.exception("PUT /api/usage/budget failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Clientes & API (multi-tenant / white-label) ─────────────────────────────
class ClientCreate(BaseModel):
    name: str
    email: str = ""
    model: str = ""
    persona: str = ""
    rag_enabled: bool = True
    daily_token_limit: int = 0
    plan: str = "free"
    rpm: int = 0


class ClientUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    status: Optional[str] = None
    model: Optional[str] = None
    persona: Optional[str] = None
    rag_enabled: Optional[bool] = None
    daily_token_limit: Optional[int] = None
    plan: Optional[str] = None
    rpm: Optional[int] = None


def _api_base_url() -> str:
    """URL pública sugerida para os clientes baterem na API."""
    import os as _os

    host = _os.getenv("API_SERVER_HOST", "127.0.0.1")
    port = _os.getenv("API_SERVER_PORT", "8642")
    if host in ("0.0.0.0", "::"):
        host = "SEU_IP_OU_DOMINIO"
    return f"http://{host}:{port}/v1"


@app.get("/api/clients/api-info")
def clients_api_info():
    """Info para montar o snippet de uso (base URL da API OpenAI-compatível)."""
    return {"base_url": _api_base_url(), "endpoint": "/chat/completions"}


@app.get("/api/clients")
def clients_list():
    try:
        from mangaba_cli import api_clients, usage_ledger

        today = usage_ledger.get_today().get("by_tenant") or {}
        out = []
        for c in api_clients.list_clients():
            t = today.get(c["id"]) or {}
            c["used_today"] = int(t.get("input", 0)) + int(t.get("output", 0))
            c["turns_today"] = int(t.get("turns", 0))
            c["limits"] = api_clients.effective_limits(c)
            out.append(c)
        return {"clients": out, "plans": api_clients.PLANS}
    except Exception as exc:  # noqa: BLE001
        _log.exception("GET /api/clients failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/clients")
def clients_create(body: ClientCreate):
    try:
        from mangaba_cli import api_clients

        if not body.name.strip():
            raise HTTPException(status_code=400, detail="nome obrigatório")
        return api_clients.create_client(
            body.name, email=body.email, model=body.model, persona=body.persona,
            rag_enabled=body.rag_enabled, daily_token_limit=body.daily_token_limit,
            plan=body.plan, rpm=body.rpm,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.exception("POST /api/clients failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/api/clients/{client_id}")
def clients_update(client_id: str, body: ClientUpdate):
    try:
        from mangaba_cli import api_clients

        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        c = api_clients.update_client(client_id, **fields)
        if not c:
            raise HTTPException(status_code=404, detail="cliente não encontrado")
        return c
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.exception("PUT /api/clients failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/clients/{client_id}")
def clients_delete(client_id: str):
    try:
        from mangaba_cli import api_clients, client_profiles

        c = api_clients.get_client(client_id)
        if c and c.get("profile"):
            try:
                client_profiles.teardown(c, delete_files=True)
            except Exception:
                _log.warning("teardown do profile do cliente %s falhou", client_id)
        return {"ok": api_clients.delete_client(client_id)}
    except Exception as exc:  # noqa: BLE001
        _log.exception("DELETE /api/clients failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Isolamento por processo dedicado (Fase 3) ───────────────────────────────
def _client_or_404(client_id: str):
    from mangaba_cli import api_clients

    c = api_clients.get_client(client_id)
    if not c:
        raise HTTPException(status_code=404, detail="cliente não encontrado")
    return c


@app.get("/api/clients/{client_id}/profile/status")
def client_profile_status(client_id: str):
    try:
        from mangaba_cli import client_profiles

        return client_profiles.status(_client_or_404(client_id))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.exception("GET profile/status failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/clients/{client_id}/profile/start")
def client_profile_start(client_id: str):
    """Provisiona (se preciso) e sobe o agente dedicado do cliente."""
    try:
        from mangaba_cli import client_profiles

        from mangaba_cli import api_clients

        c = _client_or_404(client_id)
        client_profiles.provision(c)
        c = _client_or_404(client_id)  # recarrega com profile/api_port
        r = client_profiles.start(c)
        if r.get("running"):
            api_clients.update_client(client_id, autostart=True)  # volta no boot
        return r
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.exception("POST profile/start failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/clients/{client_id}/profile/stop")
def client_profile_stop(client_id: str):
    try:
        from mangaba_cli import api_clients, client_profiles

        r = client_profiles.stop(_client_or_404(client_id))
        api_clients.update_client(client_id, autostart=False)  # não volta no boot
        return r
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.exception("POST profile/stop failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/clients/{client_id}/keys")
def clients_keys_list(client_id: str):
    try:
        from mangaba_cli import api_clients

        return {"keys": api_clients.list_keys(client_id)}
    except Exception as exc:  # noqa: BLE001
        _log.exception("GET /api/clients/keys failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/clients/{client_id}/keys")
def clients_keys_create(client_id: str):
    """Cria uma chave. Retorna o token em texto puro UMA vez."""
    try:
        from mangaba_cli import api_clients

        k = api_clients.create_key(client_id)
        if not k:
            raise HTTPException(status_code=404, detail="cliente não encontrado")
        return k
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.exception("POST /api/clients/keys failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/clients/keys/{key_id}")
def clients_keys_revoke(key_id: str):
    try:
        from mangaba_cli import api_clients

        return {"ok": api_clients.revoke_key(key_id)}
    except Exception as exc:  # noqa: BLE001
        _log.exception("DELETE /api/clients/keys failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Kanban board API — multi-worker task orchestration (boards + tasks +
# lifecycle). LLM-heavy ops (specify/decompose) run in a background thread
# so the request returns immediately. Each board is a separate SQLite DB,
# opened via kanban_db.connect(board=<slug>).
# ---------------------------------------------------------------------------


class KanbanBoardCreate(BaseModel):
    """Payload to create a kanban board."""
    slug: str
    name: str = ""
    description: str = ""


class KanbanBoardSelect(BaseModel):
    """Payload to switch the active board."""
    slug: str


class KanbanTaskCreate(BaseModel):
    """Payload to create a kanban task."""
    title: str
    body: str = ""
    assignee: str = ""
    priority: int = 0
    triage: bool = False
    parents: List[str] = []


class KanbanComment(BaseModel):
    body: str
    author: str = "dashboard"


class KanbanAssign(BaseModel):
    assignee: str = ""


class KanbanBlock(BaseModel):
    reason: str = ""


def _kanban_task_to_dict(t: Any) -> Dict[str, Any]:
    """Serialize a kanban_db.Task dataclass to a JSON-safe dict."""
    return {
        "id": t.id,
        "title": t.title,
        "body": t.body,
        "assignee": t.assignee,
        "status": t.status,
        "priority": t.priority,
        "tenant": t.tenant,
        "workspace_kind": t.workspace_kind,
        "workspace_path": t.workspace_path,
        "branch_name": t.branch_name,
        "created_by": t.created_by,
        "created_at": t.created_at,
        "started_at": t.started_at,
        "completed_at": t.completed_at,
        "result": t.result,
        "skills": list(t.skills) if t.skills else [],
        "session_id": t.session_id,
    }


@app.get("/api/kanban/boards")
async def list_kanban_boards():
    """Lista todos os quadros com contagem de tarefas por status."""
    try:
        from mangaba_cli import kanban_db as _kb

        current = _kb.get_current_board()
        boards = []
        for meta in _kb.list_boards(include_archived=False):
            slug = meta.get("slug")
            stats: Dict[str, Any] = {}
            try:
                conn = _kb.connect(board=slug)
                try:
                    stats = _kb.board_stats(conn)
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                stats = {}
            boards.append({
                "slug": slug,
                "name": meta.get("name") or slug,
                "description": meta.get("description") or "",
                "archived": bool(meta.get("archived")),
                "by_status": stats.get("by_status", {}),
                "is_current": slug == current,
            })
        return {"boards": boards, "current": current}
    except Exception as exc:
        _log.exception("GET /api/kanban/boards failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/kanban/boards")
async def create_kanban_board(body: KanbanBoardCreate):
    """Cria um novo quadro."""
    try:
        from mangaba_cli import kanban_db as _kb

        meta = _kb.create_board(
            body.slug,
            name=body.name or None,
            description=body.description or None,
        )
        return {"ok": True, "board": {"slug": meta.get("slug"), "name": meta.get("name")}}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _log.exception("POST /api/kanban/boards failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/kanban/boards/current")
async def select_kanban_board(body: KanbanBoardSelect):
    """Define o quadro ativo."""
    try:
        from mangaba_cli import kanban_db as _kb

        if not _kb.board_exists(body.slug):
            raise HTTPException(status_code=404, detail=f"Quadro '{body.slug}' não existe")
        _kb.set_current_board(body.slug)
        return {"ok": True, "current": body.slug}
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("POST /api/kanban/boards/current failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/kanban/boards/{slug}")
async def delete_kanban_board(slug: str):
    """Arquiva um quadro (não pode arquivar o 'default')."""
    try:
        from mangaba_cli import kanban_db as _kb

        result = _kb.remove_board(slug, archive=True)
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _log.exception("DELETE /api/kanban/boards/%s failed", slug)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/kanban/tasks")
async def list_kanban_tasks(board: str = "", status: str = "", assignee: str = ""):
    """Lista tarefas de um quadro (todas as colunas). Recalcula 'ready' antes."""
    try:
        from mangaba_cli import kanban_db as _kb

        board_slug = board or _kb.get_current_board()
        conn = _kb.connect(board=board_slug)
        try:
            _kb.recompute_ready(conn)
            tasks = _kb.list_tasks(
                conn,
                status=status or None,
                assignee=assignee or None,
            )
            return {
                "board": board_slug,
                "tasks": [_kanban_task_to_dict(t) for t in tasks],
            }
        finally:
            conn.close()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _log.exception("GET /api/kanban/tasks failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/kanban/tasks")
async def create_kanban_task(body: KanbanTaskCreate, board: str = ""):
    """Cria uma tarefa no quadro."""
    try:
        from mangaba_cli import kanban_db as _kb

        board_slug = board or _kb.get_current_board()
        conn = _kb.connect(board=board_slug)
        try:
            task_id = _kb.create_task(
                conn,
                title=body.title,
                body=body.body or None,
                assignee=body.assignee or None,
                created_by="dashboard",
                priority=body.priority,
                parents=tuple(body.parents or ()),
                triage=bool(body.triage),
            )
            task = _kb.get_task(conn, task_id)
            return {"ok": True, "task": _kanban_task_to_dict(task)}
        finally:
            conn.close()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _log.exception("POST /api/kanban/tasks failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/kanban/tasks/{task_id}")
async def get_kanban_task(task_id: str, board: str = ""):
    """Detalhe de uma tarefa: corpo, dependências, comentários, eventos, runs."""
    try:
        from mangaba_cli import kanban_db as _kb

        board_slug = board or _kb.get_current_board()
        conn = _kb.connect(board=board_slug)
        try:
            task = _kb.get_task(conn, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail=f"Tarefa '{task_id}' não encontrada")
            comments = _kb.list_comments(conn, task_id)
            events = _kb.list_events(conn, task_id)
            parents = _kb.parent_ids(conn, task_id)
            children = _kb.child_ids(conn, task_id)
            latest_summary = _kb.latest_summary(conn, task_id)
            return {
                "task": _kanban_task_to_dict(task),
                "parents": parents,
                "children": children,
                "latest_summary": latest_summary,
                "comments": [
                    {"author": c.author, "body": c.body, "created_at": c.created_at}
                    for c in comments
                ],
                "events": [
                    {"kind": e.kind, "created_at": e.created_at}
                    for e in events
                ],
            }
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("GET /api/kanban/tasks/%s failed", task_id)
        raise HTTPException(status_code=500, detail=str(exc))


def _kanban_simple_action(task_id: str, board: str, fn_name: str, **kwargs) -> Dict[str, Any]:
    """Helper: open the board DB, call a kanban_db mutator, return {ok}."""
    from mangaba_cli import kanban_db as _kb

    board_slug = board or _kb.get_current_board()
    conn = _kb.connect(board=board_slug)
    try:
        fn = getattr(_kb, fn_name)
        ok = fn(conn, task_id, **kwargs)
        return {"ok": bool(ok)}
    finally:
        conn.close()


@app.post("/api/kanban/tasks/{task_id}/complete")
async def complete_kanban_task(task_id: str, board: str = ""):
    """Marca a tarefa como concluída."""
    try:
        return _kanban_simple_action(task_id, board, "complete_task")
    except Exception as exc:
        _log.exception("POST /api/kanban/tasks/%s/complete failed", task_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/kanban/tasks/{task_id}/block")
async def block_kanban_task(task_id: str, body: KanbanBlock, board: str = ""):
    """Bloqueia a tarefa (aguardando intervenção)."""
    try:
        return _kanban_simple_action(task_id, board, "block_task", reason=body.reason or None)
    except Exception as exc:
        _log.exception("POST /api/kanban/tasks/%s/block failed", task_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/kanban/tasks/{task_id}/unblock")
async def unblock_kanban_task(task_id: str, board: str = ""):
    """Desbloqueia a tarefa (volta para a fila)."""
    try:
        return _kanban_simple_action(task_id, board, "unblock_task")
    except Exception as exc:
        _log.exception("POST /api/kanban/tasks/%s/unblock failed", task_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/kanban/tasks/{task_id}/reclaim")
async def reclaim_kanban_task(task_id: str, board: str = ""):
    """Libera o claim de um worker travado (recuperação)."""
    try:
        return _kanban_simple_action(task_id, board, "reclaim_task")
    except Exception as exc:
        _log.exception("POST /api/kanban/tasks/%s/reclaim failed", task_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/kanban/tasks/{task_id}/assign")
async def assign_kanban_task(task_id: str, body: KanbanAssign, board: str = ""):
    """Atribui/reatribui a tarefa a um profile (worker)."""
    try:
        from mangaba_cli import kanban_db as _kb

        board_slug = board or _kb.get_current_board()
        conn = _kb.connect(board=board_slug)
        try:
            ok = _kb.assign_task(conn, task_id, body.assignee or None)
            return {"ok": bool(ok)}
        finally:
            conn.close()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        _log.exception("POST /api/kanban/tasks/%s/assign failed", task_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/kanban/tasks/{task_id}/comment")
async def comment_kanban_task(task_id: str, body: KanbanComment, board: str = ""):
    """Adiciona um comentário à tarefa."""
    try:
        from mangaba_cli import kanban_db as _kb

        board_slug = board or _kb.get_current_board()
        conn = _kb.connect(board=board_slug)
        try:
            comment_id = _kb.add_comment(conn, task_id, body.author or "dashboard", body.body)
            return {"ok": True, "comment_id": comment_id}
        finally:
            conn.close()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _log.exception("POST /api/kanban/tasks/%s/comment failed", task_id)
        raise HTTPException(status_code=500, detail=str(exc))


def _kanban_run_llm_async(kind: str, task_id: str) -> None:
    """Run specify/decompose in a background thread (blocking LLM call).

    These functions open their own DB connection and never raise — they
    return an outcome object. We log the result and swallow exceptions so a
    failure here can never crash the server thread.
    """
    try:
        if kind == "specify":
            from mangaba_cli.kanban_specify import specify_task
            outcome = specify_task(task_id)
        else:
            from mangaba_cli.kanban_decompose import decompose_task
            outcome = decompose_task(task_id)
        _log.info("kanban %s(%s) -> ok=%s reason=%s",
                  kind, task_id, getattr(outcome, "ok", None), getattr(outcome, "reason", None))
    except Exception:  # noqa: BLE001
        _log.exception("kanban %s(%s) background thread failed", kind, task_id)


@app.post("/api/kanban/tasks/{task_id}/specify")
async def specify_kanban_task(task_id: str):
    """Dispara a especificação automática (LLM) da tarefa em background."""
    try:
        import threading
        threading.Thread(
            target=_kanban_run_llm_async, args=("specify", task_id), daemon=True
        ).start()
        return {"ok": True, "started": True}
    except Exception as exc:
        _log.exception("POST /api/kanban/tasks/%s/specify failed", task_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/kanban/tasks/{task_id}/decompose")
async def decompose_kanban_task(task_id: str):
    """Dispara a decomposição automática (LLM) da tarefa em subtarefas, em background."""
    try:
        import threading
        threading.Thread(
            target=_kanban_run_llm_async, args=("decompose", task_id), daemon=True
        ).start()
        return {"ok": True, "started": True}
    except Exception as exc:
        _log.exception("POST /api/kanban/tasks/%s/decompose failed", task_id)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# OAuth provider endpoints — status + disconnect (Phase 1)
# ---------------------------------------------------------------------------
#
# Phase 1 surfaces *which OAuth providers exist* and whether each is
# connected, plus a disconnect button. The actual login flow (PKCE for
# Anthropic, device-code for Nous/Codex) still runs in the CLI for now;
# Phase 2 will add in-browser flows. For unconnected providers we return
# the canonical ``mangaba auth add <provider>`` command so the dashboard
# can surface a one-click copy.


def _truncate_token(value: Optional[str], visible: int = 6) -> str:
    """Return ``...XXXXXX`` (last N chars) for safe display in the UI.

    We never expose more than the trailing ``visible`` characters of an
    OAuth access token. JWT prefixes (the part before the first dot) are
    stripped first when present so the visible suffix is always part of
    the signing region rather than a meaningless header chunk.

    Returns the Entra-ID placeholder when handed a callable (Azure Foundry
    bearer provider) — the callable is NEVER invoked here.
    """
    if not value:
        return ""
    if callable(value) and not isinstance(value, str):
        # Entra ID bearer provider — never reveal a minted token in the UI.
        return "<entra-id-bearer>"
    s = str(value)
    if "." in s and s.count(".") >= 2:
        # Looks like a JWT — show the trailing piece of the signature only.
        s = s.rsplit(".", 1)[-1]
    if len(s) <= visible:
        return s
    return f"…{s[-visible:]}"


def _anthropic_oauth_status() -> Dict[str, Any]:
    """Combined status across the three Anthropic credential sources we read.

    Mangaba resolves Anthropic creds in this order at runtime:
    1. ``~/.mangaba/.anthropic_oauth.json`` — Mangaba-managed PKCE flow
    2. ``~/.claude/.credentials.json`` — Claude Code CLI credentials (auto)
    3. ``ANTHROPIC_TOKEN`` / ``ANTHROPIC_API_KEY`` env vars
    The dashboard reports the highest-priority source that's actually present.
    """
    try:
        from agent.anthropic_adapter import (
            read_mangaba_oauth_credentials,
            read_claude_code_credentials,
            _MANGABA_OAUTH_FILE,
        )
    except ImportError:
        read_claude_code_credentials = None  # type: ignore
        read_mangaba_oauth_credentials = None  # type: ignore
        _MANGABA_OAUTH_FILE = None  # type: ignore

    mangaba_creds = None
    if read_mangaba_oauth_credentials:
        try:
            mangaba_creds = read_mangaba_oauth_credentials()
        except Exception:
            mangaba_creds = None
    if mangaba_creds and mangaba_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "mangaba_pkce",
            "source_label": f"Mangaba PKCE ({_MANGABA_OAUTH_FILE})",
            "token_preview": _truncate_token(mangaba_creds.get("accessToken")),
            "expires_at": mangaba_creds.get("expiresAt"),
            "has_refresh_token": bool(mangaba_creds.get("refreshToken")),
        }

    cc_creds = None
    if read_claude_code_credentials:
        try:
            cc_creds = read_claude_code_credentials()
        except Exception:
            cc_creds = None
    if cc_creds and cc_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code",
            "source_label": "Claude Code (~/.claude/.credentials.json)",
            "token_preview": _truncate_token(cc_creds.get("accessToken")),
            "expires_at": cc_creds.get("expiresAt"),
            "has_refresh_token": bool(cc_creds.get("refreshToken")),
        }

    env_token = os.getenv("ANTHROPIC_TOKEN") or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        return {
            "logged_in": True,
            "source": "env_var",
            "source_label": "ANTHROPIC_TOKEN environment variable",
            "token_preview": _truncate_token(env_token),
            "expires_at": None,
            "has_refresh_token": False,
        }
    return {"logged_in": False, "source": None}


def _claude_code_only_status() -> Dict[str, Any]:
    """Surface Claude Code CLI credentials as their own provider entry.

    Independent of the Anthropic entry above so users can see whether their
    Claude Code subscription tokens are actively flowing into Mangaba even
    when they also have a separate Mangaba-managed PKCE login.
    """
    try:
        from agent.anthropic_adapter import read_claude_code_credentials
        creds = read_claude_code_credentials()
    except Exception:
        creds = None
    if creds and creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code_cli",
            "source_label": "~/.claude/.credentials.json",
            "token_preview": _truncate_token(creds.get("accessToken")),
            "expires_at": creds.get("expiresAt"),
            "has_refresh_token": bool(creds.get("refreshToken")),
        }
    return {"logged_in": False, "source": None}


# Provider catalog. The order matters — it's how we render the UI list.
# ``cli_command`` is what the dashboard surfaces as the copy-to-clipboard
# fallback while Phase 2 (in-browser flows) isn't built yet.
# ``flow`` describes the OAuth shape so the future modal can pick the
# right UI: ``pkce`` = open URL + paste callback code, ``device_code`` =
# show code + verification URL + poll, ``external`` = read-only (delegated
# to a third-party CLI like Claude Code or Qwen).
_OAUTH_PROVIDER_CATALOG: tuple[Dict[str, Any], ...] = (
    {
        "id": "anthropic",
        "name": "Anthropic (Claude API)",
        "flow": "pkce",
        "cli_command": "mangaba auth add anthropic",
        "docs_url": "https://docs.claude.com/en/api/getting-started",
        "status_fn": _anthropic_oauth_status,
    },
    {
        "id": "claude-code",
        "name": "Claude Code (subscription)",
        "flow": "external",
        "cli_command": "claude setup-token",
        "docs_url": "https://docs.claude.com/en/docs/claude-code",
        "status_fn": _claude_code_only_status,
    },
    {
        "id": "nous",
        "name": "Nous Portal",
        "flow": "device_code",
        "cli_command": "mangaba auth add nous",
        "docs_url": "https://portal.dheiver2.com",
        "status_fn": None,  # dispatched via auth.get_nous_auth_status
    },
    {
        "id": "openai-codex",
        "name": "OpenAI Codex (ChatGPT)",
        "flow": "device_code",
        "cli_command": "mangaba auth add openai-codex",
        "docs_url": "https://platform.openai.com/docs",
        "status_fn": None,  # dispatched via auth.get_codex_auth_status
    },
    {
        "id": "qwen-oauth",
        "name": "Qwen (via Qwen CLI)",
        "flow": "external",
        "cli_command": "mangaba auth add qwen-oauth",
        "docs_url": "https://github.com/QwenLM/qwen-code",
        "status_fn": None,  # dispatched via auth.get_qwen_auth_status
    },
    {
        "id": "minimax-oauth",
        "name": "MiniMax (OAuth)",
        # MiniMax's flow is structurally device-code (verification URI +
        # user code, backend polls the token endpoint) with a PKCE
        # extension for code-binding. The dashboard renders the same UX
        # as Nous's device-code flow; the PKCE bit is a security
        # extension that doesn't change the operator experience.
        "flow": "device_code",
        "cli_command": "mangaba auth add minimax-oauth",
        "docs_url": "https://www.minimax.io",
        "status_fn": None,  # dispatched via auth.get_minimax_oauth_auth_status
    },
)


def _resolve_provider_status(provider_id: str, status_fn) -> Dict[str, Any]:
    """Dispatch to the right status helper for an OAuth provider entry."""
    if status_fn is not None:
        try:
            return status_fn()
        except Exception as e:
            return {"logged_in": False, "error": str(e)}
    try:
        from mangaba_cli import auth as hauth
        if provider_id == "nous":
            raw = hauth.get_nous_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "nous_portal",
                "source_label": raw.get("portal_base_url") or "Nous Portal",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("access_expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "openai-codex":
            raw = hauth.get_codex_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or "openai_codex",
                "source_label": raw.get("auth_mode") or "OpenAI Codex",
                "token_preview": _truncate_token(raw.get("api_key")),
                "expires_at": None,
                "has_refresh_token": False,
                "last_refresh": raw.get("last_refresh"),
            }
        if provider_id == "qwen-oauth":
            raw = hauth.get_qwen_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "qwen_cli",
                "source_label": raw.get("auth_store_path") or "Qwen CLI",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "minimax-oauth":
            raw = hauth.get_minimax_oauth_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "minimax_oauth",
                "source_label": f"MiniMax ({raw.get('region', 'global')})",
                "token_preview": None,
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": True,
            }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}
    return {"logged_in": False}


@app.get("/api/providers/oauth")
async def list_oauth_providers():
    """Enumerate every OAuth-capable LLM provider with current status.

    Response shape (per provider):
        id              stable identifier (used in DELETE path)
        name            human label
        flow            "pkce" | "device_code" | "external"
        cli_command     fallback CLI command for users to run manually
        docs_url        external docs/portal link for the "Learn more" link
        status:
          logged_in        bool — currently has usable creds
          source           short slug ("mangaba_pkce", "claude_code", ...)
          source_label     human-readable origin (file path, env var name)
          token_preview    last N chars of the token, never the full token
          expires_at       ISO timestamp string or null
          has_refresh_token bool
    """
    providers = []
    for p in _OAUTH_PROVIDER_CATALOG:
        status = _resolve_provider_status(p["id"], p.get("status_fn"))
        providers.append({
            "id": p["id"],
            "name": p["name"],
            "flow": p["flow"],
            "cli_command": p["cli_command"],
            "docs_url": p["docs_url"],
            "status": status,
        })
    return {"providers": providers}


@app.delete("/api/providers/oauth/{provider_id}")
async def disconnect_oauth_provider(provider_id: str, request: Request):
    """Disconnect an OAuth provider. Token-protected (matches /env/reveal)."""
    _require_token(request)

    valid_ids = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider: {provider_id}. "
                   f"Available: {', '.join(sorted(valid_ids))}",
        )

    # Anthropic and claude-code clear the same Mangaba-managed PKCE file
    # AND forget the Claude Code import. We don't touch ~/.claude/* directly
    # — that's owned by the Claude Code CLI; users can re-auth there if they
    # want to undo a disconnect.
    if provider_id in {"anthropic", "claude-code"}:
        try:
            from agent.anthropic_adapter import _MANGABA_OAUTH_FILE
            if _MANGABA_OAUTH_FILE.exists():
                _MANGABA_OAUTH_FILE.unlink()
        except Exception:
            pass
        # Also clear the credential pool entry if present.
        try:
            from mangaba_cli.auth import clear_provider_auth
            clear_provider_auth("anthropic")
        except Exception:
            pass
        _log.info("oauth/disconnect: %s", provider_id)
        return {"ok": True, "provider": provider_id}

    try:
        from mangaba_cli.auth import clear_provider_auth
        cleared = clear_provider_auth(provider_id)
        _log.info("oauth/disconnect: %s (cleared=%s)", provider_id, cleared)
        return {"ok": bool(cleared), "provider": provider_id}
    except Exception as e:
        _log.exception("disconnect %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# OAuth Phase 2 — in-browser PKCE & device-code flows
# ---------------------------------------------------------------------------
#
# Two flow shapes are supported:
#
#   PKCE (Anthropic):
#     1. POST /api/providers/oauth/anthropic/start
#          → server generates code_verifier + challenge, builds claude.ai
#            authorize URL, stashes verifier in _oauth_sessions[session_id]
#          → returns { session_id, flow: "pkce", auth_url }
#     2. UI opens auth_url in a new tab. User authorizes, copies code.
#     3. POST /api/providers/oauth/anthropic/submit { session_id, code }
#          → server exchanges (code + verifier) → tokens at console.anthropic.com
#          → persists to ~/.mangaba/.anthropic_oauth.json AND credential pool
#          → returns { ok: true, status: "approved" }
#
#   Device code (Nous, OpenAI Codex):
#     1. POST /api/providers/oauth/{nous|openai-codex}/start
#          → server hits provider's device-auth endpoint
#          → gets { user_code, verification_url, device_code, interval, expires_in }
#          → spawns background poller thread that polls the token endpoint
#            every `interval` seconds until approved/expired
#          → stores poll status in _oauth_sessions[session_id]
#          → returns { session_id, flow: "device_code", user_code,
#                      verification_url, expires_in, poll_interval }
#     2. UI opens verification_url in a new tab and shows user_code.
#     3. UI polls GET /api/providers/oauth/{provider}/poll/{session_id}
#          every 2s until status != "pending".
#     4. On "approved" the background thread has already saved creds; UI
#        refreshes the providers list.
#
# Sessions are kept in-memory only (single-process FastAPI) and time out
# after 15 minutes. A periodic cleanup runs on each /start call to GC
# expired sessions so the dict doesn't grow without bound.

_OAUTH_SESSION_TTL_SECONDS = 15 * 60
_oauth_sessions: Dict[str, Dict[str, Any]] = {}
_oauth_sessions_lock = threading.Lock()

# Import OAuth constants from canonical source instead of duplicating.
# Guarded so mangaba web still starts if anthropic_adapter is unavailable;
# Phase 2 endpoints will return 501 in that case.
try:
    from agent.anthropic_adapter import (
        _OAUTH_CLIENT_ID as _ANTHROPIC_OAUTH_CLIENT_ID,
        _OAUTH_TOKEN_URL as _ANTHROPIC_OAUTH_TOKEN_URL,
        _OAUTH_REDIRECT_URI as _ANTHROPIC_OAUTH_REDIRECT_URI,
        _OAUTH_SCOPES as _ANTHROPIC_OAUTH_SCOPES,
        _generate_pkce as _generate_pkce_pair,
    )
    _ANTHROPIC_OAUTH_AVAILABLE = True
except ImportError:
    _ANTHROPIC_OAUTH_AVAILABLE = False
_ANTHROPIC_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"


def _gc_oauth_sessions() -> None:
    """Drop expired sessions. Called opportunistically on /start."""
    cutoff = time.time() - _OAUTH_SESSION_TTL_SECONDS
    with _oauth_sessions_lock:
        stale = [sid for sid, sess in _oauth_sessions.items() if sess["created_at"] < cutoff]
        for sid in stale:
            _oauth_sessions.pop(sid, None)


def _new_oauth_session(provider_id: str, flow: str) -> tuple[str, Dict[str, Any]]:
    """Create + register a new OAuth session, return (session_id, session_dict)."""
    sid = secrets.token_urlsafe(16)
    sess = {
        "session_id": sid,
        "provider": provider_id,
        "flow": flow,
        "created_at": time.time(),
        "status": "pending",  # pending | approved | denied | expired | error
        "error_message": None,
    }
    with _oauth_sessions_lock:
        _oauth_sessions[sid] = sess
    return sid, sess


def _save_anthropic_oauth_creds(access_token: str, refresh_token: str, expires_at_ms: int) -> None:
    """Persist Anthropic PKCE creds to both Mangaba file AND credential pool.

    Mirrors what auth_commands.add_command does so the dashboard flow leaves
    the system in the same state as ``mangaba auth add anthropic``.
    """
    from agent.anthropic_adapter import _MANGABA_OAUTH_FILE
    payload = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at_ms,
    }
    _MANGABA_OAUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MANGABA_OAUTH_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Best-effort credential-pool insert. Failure here doesn't invalidate
    # the file write — pool registration only matters for the rotation
    # strategy, not for runtime credential resolution.
    try:
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        import uuid
        pool = load_pool("anthropic")
        # Avoid duplicate entries: delete any prior dashboard-issued OAuth entry
        existing = [e for e in pool.entries() if getattr(e, "source", "").startswith(f"{SOURCE_MANUAL}:dashboard_pkce")]
        for e in existing:
            try:
                pool.remove_entry(getattr(e, "id", ""))
            except Exception:
                pass
        entry = PooledCredential(
            provider="anthropic",
            id=uuid.uuid4().hex[:6],
            label="dashboard PKCE",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_pkce",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at_ms=expires_at_ms,
        )
        pool.add_entry(entry)
    except Exception as e:
        _log.warning("anthropic pool add (dashboard) failed: %s", e)


def _start_anthropic_pkce() -> Dict[str, Any]:
    """Begin PKCE flow. Returns the auth URL the UI should open."""
    if not _ANTHROPIC_OAUTH_AVAILABLE:
        raise HTTPException(status_code=501, detail="Anthropic OAuth not available (missing adapter)")
    verifier, challenge = _generate_pkce_pair()
    sid, sess = _new_oauth_session("anthropic", "pkce")
    sess["verifier"] = verifier
    sess["state"] = verifier  # Anthropic round-trips verifier as state
    params = {
        "code": "true",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "scope": _ANTHROPIC_OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    auth_url = f"{_ANTHROPIC_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    return {
        "session_id": sid,
        "flow": "pkce",
        "auth_url": auth_url,
        "expires_in": _OAUTH_SESSION_TTL_SECONDS,
    }


def _submit_anthropic_pkce(session_id: str, code_input: str) -> Dict[str, Any]:
    """Exchange authorization code for tokens. Persists on success."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess or sess["provider"] != "anthropic" or sess["flow"] != "pkce":
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    if sess["status"] != "pending":
        return {"ok": False, "status": sess["status"], "message": sess.get("error_message")}

    # Anthropic's redirect callback page formats the code as `<code>#<state>`.
    # Strip the state suffix if present (we already have the verifier server-side).
    parts = code_input.strip().split("#", 1)
    code = parts[0].strip()
    if not code:
        return {"ok": False, "status": "error", "message": "No code provided"}
    state_from_callback = parts[1] if len(parts) > 1 else ""

    exchange_data = json.dumps({
        "grant_type": "authorization_code",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "code": code,
        "state": state_from_callback or sess["state"],
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "code_verifier": sess["verifier"],
    }).encode()
    req = urllib.request.Request(
        _ANTHROPIC_OAUTH_TOKEN_URL,
        data=exchange_data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "mangaba-dashboard/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = f"Token exchange failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")
    expires_in = int(result.get("expires_in") or 3600)
    if not access_token:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = "No access token returned"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    expires_at_ms = int(time.time() * 1000) + (expires_in * 1000)
    try:
        _save_anthropic_oauth_creds(access_token, refresh_token, expires_at_ms)
    except Exception as e:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = f"Save failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}
    with _oauth_sessions_lock:
        sess["status"] = "approved"
    _log.info("oauth/pkce: anthropic login completed (session=%s)", session_id)
    return {"ok": True, "status": "approved"}


async def _start_device_code_flow(provider_id: str) -> Dict[str, Any]:
    """Initiate a device-code flow (Nous, OpenAI Codex, or MiniMax).

    Calls the provider's device-auth endpoint via the existing CLI helpers,
    then spawns a background poller. Returns the user-facing display fields
    so the UI can render the verification page link + user code.
    """
    if provider_id == "nous":
        from mangaba_cli.auth import (
            _nous_device_scope_with_env_override,
            _request_nous_device_code_with_scope_fallback,
            PROVIDER_REGISTRY,
        )
        import httpx
        pconfig = PROVIDER_REGISTRY["nous"]
        portal_base_url = (
            os.getenv("MANGABA_PORTAL_BASE_URL")
            or os.getenv("NOUS_PORTAL_BASE_URL")
            or pconfig.portal_base_url
        ).rstrip("/")
        client_id = pconfig.client_id
        scope, explicit_scope = _nous_device_scope_with_env_override(
            None,
            default_scope=pconfig.scope,
        )

        def _do_nous_device_request():
            with httpx.Client(
                timeout=httpx.Timeout(15.0),
                headers={"Accept": "application/json"},
            ) as client:
                return _request_nous_device_code_with_scope_fallback(
                    client=client,
                    portal_base_url=portal_base_url,
                    client_id=client_id,
                    scope=scope,
                    allow_legacy_fallback=not explicit_scope,
                )

        device_data, effective_scope = await asyncio.get_running_loop().run_in_executor(
            None, _do_nous_device_request
        )
        sid, sess = _new_oauth_session("nous", "device_code")
        sess["device_code"] = str(device_data["device_code"])
        sess["interval"] = int(device_data["interval"])
        sess["expires_at"] = time.time() + int(device_data["expires_in"])
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = client_id
        sess["scope"] = effective_scope
        threading.Thread(
            target=_nous_poller, args=(sid,), daemon=True, name=f"oauth-poll-{sid[:6]}"
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri_complete"]),
            "expires_in": int(device_data["expires_in"]),
            "poll_interval": int(device_data["interval"]),
        }

    if provider_id == "openai-codex":
        # Codex uses fixed OpenAI device-auth endpoints; reuse the helper.
        sid, _ = _new_oauth_session("openai-codex", "device_code")
        # Use the helper but in a thread because it polls inline.
        # We can't extract just the start step without refactoring auth.py,
        # so we run the full helper in a worker and proxy the user_code +
        # verification_url back via the session dict. The helper prints
        # to stdout — we capture nothing here, just status.
        threading.Thread(
            target=_codex_full_login_worker, args=(sid,), daemon=True,
            name=f"oauth-codex-{sid[:6]}",
        ).start()
        # Block briefly until the worker has populated the user_code, OR error.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with _oauth_sessions_lock:
                s = _oauth_sessions.get(sid)
            if s and (s.get("user_code") or s["status"] != "pending"):
                break
            await asyncio.sleep(0.1)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(sid, {})
        if s.get("status") == "error":
            raise HTTPException(status_code=500, detail=s.get("error_message") or "device-auth failed")
        if not s.get("user_code"):
            raise HTTPException(status_code=504, detail="device-auth timed out before returning a user code")
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": s["user_code"],
            "verification_url": s["verification_url"],
            "expires_in": int(s.get("expires_in") or 900),
            "poll_interval": int(s.get("interval") or 5),
        }

    if provider_id == "minimax-oauth":
        # MiniMax uses a device-code-style flow (verification URI + user
        # code + background poll) with a PKCE extension on top. From the
        # operator's perspective it's identical to Nous's device-code
        # flow; the PKCE bit (verifier + challenge from
        # _minimax_pkce_pair) is a security extension that binds the
        # token exchange to the original session.
        from mangaba_cli.auth import (
            _minimax_pkce_pair,
            _minimax_request_user_code,
            MINIMAX_OAUTH_CLIENT_ID,
            MINIMAX_OAUTH_GLOBAL_BASE,
        )
        import httpx
        verifier, challenge, state = _minimax_pkce_pair()
        portal_base_url = (
            os.getenv("MINIMAX_PORTAL_BASE_URL") or MINIMAX_OAUTH_GLOBAL_BASE
        ).rstrip("/")
        def _do_minimax_request():
            with httpx.Client(
                timeout=httpx.Timeout(15.0),
                headers={"Accept": "application/json"},
                follow_redirects=True,
            ) as client:
                return _minimax_request_user_code(
                    client=client,
                    portal_base_url=portal_base_url,
                    client_id=MINIMAX_OAUTH_CLIENT_ID,
                    code_challenge=challenge,
                    state=state,
                )
        device_data = await asyncio.get_event_loop().run_in_executor(
            None, _do_minimax_request
        )
        sid, sess = _new_oauth_session("minimax-oauth", "device_code")
        # The CLI flow names this `interval_ms` because MiniMax's
        # `interval` field is in milliseconds (defensive default 2000ms
        # in _minimax_poll_token).
        interval_raw = device_data.get("interval")
        sess["interval_ms"] = (
            int(interval_raw) if interval_raw is not None else None
        )
        sess["user_code"] = str(device_data["user_code"])
        sess["code_verifier"] = verifier
        sess["state"] = state
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = MINIMAX_OAUTH_CLIENT_ID
        sess["region"] = "global"
        # `expired_in` from MiniMax is overloaded — could be a unix-ms
        # timestamp OR a seconds-from-now duration. Mirror the heuristic
        # in _minimax_poll_token. Stash the raw value for the poller;
        # compute a derived expires_at + UI-friendly expires_in seconds.
        expired_in_raw = int(device_data["expired_in"])
        sess["expired_in_raw"] = expired_in_raw
        if expired_in_raw > 1_000_000_000_000:  # likely unix-ms
            expires_at_ts = expired_in_raw / 1000.0
            expires_in_seconds = max(0, int(expires_at_ts - time.time()))
        else:
            expires_at_ts = time.time() + expired_in_raw
            expires_in_seconds = expired_in_raw
        sess["expires_at"] = expires_at_ts
        threading.Thread(
            target=_minimax_poller,
            args=(sid,),
            daemon=True,
            name=f"oauth-poll-{sid[:6]}",
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri"]),
            "expires_in": expires_in_seconds,
            "poll_interval": max(2, (sess["interval_ms"] or 2000) // 1000),
        }

    raise HTTPException(status_code=400, detail=f"Provider {provider_id} does not support device-code flow")


def _nous_poller(session_id: str) -> None:
    """Background poller that drives a Nous device-code flow to completion."""
    from mangaba_cli.auth import (
        NOUS_INFERENCE_AUTH_MODE_FRESH,
        _poll_for_token,
        refresh_nous_oauth_from_state,
    )
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    device_code = sess["device_code"]
    interval = sess["interval"]
    scope = sess.get("scope")
    expires_in = max(60, int(sess["expires_at"] - time.time()))
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0), headers={"Accept": "application/json"}) as client:
            token_data = _poll_for_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        # Same post-processing as _nous_device_code_login (mint agent key)
        now = datetime.now(timezone.utc)
        token_ttl = int(token_data.get("expires_in") or 0)
        auth_state = {
            "portal_base_url": portal_base_url,
            "inference_base_url": token_data.get("inference_base_url"),
            "client_id": client_id,
            "scope": token_data.get("scope") or scope,
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "obtained_at": now.isoformat(),
            "expires_at": (
                datetime.fromtimestamp(now.timestamp() + token_ttl, tz=timezone.utc).isoformat()
                if token_ttl else None
            ),
            "expires_in": token_ttl,
        }
        full_state = refresh_nous_oauth_from_state(
            auth_state,
            min_key_ttl_seconds=300,
            timeout_seconds=15.0,
            force_refresh=False,
            inference_auth_mode=NOUS_INFERENCE_AUTH_MODE_FRESH,
        )
        from mangaba_cli.auth import persist_nous_credentials
        persist_nous_credentials(full_state)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: nous login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("nous device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _minimax_poller(session_id: str) -> None:
    """Background poller that drives a MiniMax OAuth flow to completion.

    Mirrors `_nous_poller` but calls the MiniMax-specific token endpoint,
    which uses a PKCE-style ``code_verifier`` + ``user_code`` rather than
    the ``device_code`` field used by Nous. On success, builds the same
    auth_state dict that ``_minimax_oauth_login`` (the CLI flow) builds
    and persists via ``_minimax_save_auth_state`` — so the dashboard
    path leaves the system in the same state as
    ``mangaba auth add minimax-oauth``.
    """
    from mangaba_cli.auth import (
        _minimax_poll_token,
        _minimax_resolve_token_expiry_unix,
        _minimax_save_auth_state,
        MINIMAX_OAUTH_GLOBAL_INFERENCE,
        MINIMAX_OAUTH_SCOPE,
    )
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    user_code = sess["user_code"]
    code_verifier = sess["code_verifier"]
    interval_ms = sess.get("interval_ms")
    expired_in_raw = sess["expired_in_raw"]
    try:
        with httpx.Client(
            timeout=httpx.Timeout(15.0),
            headers={"Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            token_data = _minimax_poll_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                user_code=user_code,
                code_verifier=code_verifier,
                expired_in=expired_in_raw,
                interval_ms=interval_ms,
            )
        # Build the auth_state dict in the same shape as the CLI flow's
        # `_minimax_oauth_login` so `_minimax_save_auth_state` writes
        # the canonical record. Region is fixed to "global" for the
        # dashboard path; cn-region operators can still use the CLI
        # flow which supports `--region cn`.
        now = datetime.now(timezone.utc)
        expires_at_ts = _minimax_resolve_token_expiry_unix(
            int(token_data["expired_in"]), now=now,
        )
        expires_in_s = max(0, int(expires_at_ts - now.timestamp()))
        auth_state = {
            "provider": "minimax-oauth",
            "region": sess.get("region", "global"),
            "portal_base_url": portal_base_url,
            "inference_base_url": MINIMAX_OAUTH_GLOBAL_INFERENCE,
            "client_id": client_id,
            "scope": MINIMAX_OAUTH_SCOPE,
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "resource_url": token_data.get("resource_url"),
            "obtained_at": now.isoformat(),
            "expires_at": datetime.fromtimestamp(
                expires_at_ts, tz=timezone.utc
            ).isoformat(),
            "expires_in": expires_in_s,
        }
        _minimax_save_auth_state(auth_state)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: minimax login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("minimax device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _codex_full_login_worker(session_id: str) -> None:
    """Run the complete OpenAI Codex device-code flow.

    Codex doesn't use the standard OAuth device-code endpoints; it has its
    own ``/api/accounts/deviceauth/usercode`` (JSON body, returns
    ``device_auth_id``) and ``/api/accounts/deviceauth/token`` (JSON body
    polled until 200). On success the response carries an
    ``authorization_code`` + ``code_verifier`` that get exchanged at
    CODEX_OAUTH_TOKEN_URL with grant_type=authorization_code.

    The flow is replicated inline (rather than calling
    _codex_device_code_login) because that helper prints/blocks/polls in a
    single function — we need to surface the user_code to the dashboard the
    moment we receive it, well before polling completes.
    """
    try:
        import httpx
        from mangaba_cli.auth import (
            CODEX_OAUTH_CLIENT_ID,
            CODEX_OAUTH_TOKEN_URL,
            DEFAULT_CODEX_BASE_URL,
        )
        issuer = "https://auth.openai.com"

        # Step 1: request device code
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(
                f"{issuer}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"deviceauth/usercode returned {resp.status_code}")
        device_data = resp.json()
        user_code = device_data.get("user_code", "")
        device_auth_id = device_data.get("device_auth_id", "")
        poll_interval = max(3, int(device_data.get("interval", "5")))
        if not user_code or not device_auth_id:
            raise RuntimeError("device-code response missing user_code or device_auth_id")
        verification_url = f"{issuer}/codex/device"
        with _oauth_sessions_lock:
            sess = _oauth_sessions.get(session_id)
            if not sess:
                return
            sess["user_code"] = user_code
            sess["verification_url"] = verification_url
            sess["device_auth_id"] = device_auth_id
            sess["interval"] = poll_interval
            sess["expires_in"] = 15 * 60  # OpenAI's effective limit
            sess["expires_at"] = time.time() + sess["expires_in"]

        # Step 2: poll until authorized
        deadline = time.monotonic() + sess["expires_in"]
        code_resp = None
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            while time.monotonic() < deadline:
                time.sleep(poll_interval)
                poll = client.post(
                    f"{issuer}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    code_resp = poll.json()
                    break
                if poll.status_code in {403, 404}:
                    continue  # user hasn't authorized yet
                raise RuntimeError(f"deviceauth/token poll returned {poll.status_code}")

        if code_resp is None:
            with _oauth_sessions_lock:
                sess["status"] = "expired"
                sess["error_message"] = "Device code expired before approval"
            return

        # Step 3: exchange authorization_code for tokens
        authorization_code = code_resp.get("authorization_code", "")
        code_verifier = code_resp.get("code_verifier", "")
        if not authorization_code or not code_verifier:
            raise RuntimeError("device-auth response missing authorization_code/code_verifier")
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            token_resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": f"{issuer}/deviceauth/callback",
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if token_resp.status_code != 200:
            raise RuntimeError(f"token exchange returned {token_resp.status_code}")
        tokens = token_resp.json()
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        if not access_token:
            raise RuntimeError("token exchange did not return access_token")

        # Persist via credential pool — same shape as auth_commands.add_command
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        import uuid as _uuid
        pool = load_pool("openai-codex")
        base_url = (
            os.getenv("MANGABA_CODEX_BASE_URL", "").strip().rstrip("/")
            or DEFAULT_CODEX_BASE_URL
        )
        entry = PooledCredential(
            provider="openai-codex",
            id=_uuid.uuid4().hex[:6],
            label="dashboard device_code",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_device_code",
            access_token=access_token,
            refresh_token=refresh_token,
            base_url=base_url,
        )
        pool.add_entry(entry)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: openai-codex login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("codex device-code worker failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(session_id)
            if s:
                s["status"] = "error"
                s["error_message"] = str(e)


@app.post("/api/providers/oauth/{provider_id}/start")
async def start_oauth_login(provider_id: str, request: Request):
    """Initiate an OAuth login flow. Token-protected."""
    _require_token(request)
    _gc_oauth_sessions()
    valid = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider_id}")
    catalog_entry = next(p for p in _OAUTH_PROVIDER_CATALOG if p["id"] == provider_id)
    if catalog_entry["flow"] == "external":
        raise HTTPException(
            status_code=400,
            detail=f"{provider_id} uses an external CLI; run `{catalog_entry['cli_command']}` manually",
        )
    try:
        # The pkce branch is gated on provider_id == "anthropic" because
        # `_start_anthropic_pkce()` is hardcoded to the Anthropic flow.
        # Routing any other future pkce-flagged provider through it would
        # silently launch the Anthropic OAuth flow (the bug fixed in this
        # change for MiniMax). New PKCE providers must add their own
        # start function and an explicit branch here.
        if catalog_entry["flow"] == "pkce" and provider_id == "anthropic":
            return _start_anthropic_pkce()
        if catalog_entry["flow"] == "device_code":
            return await _start_device_code_flow(provider_id)
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("oauth/start %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))
    raise HTTPException(status_code=400, detail="Unsupported flow")


class OAuthSubmitBody(BaseModel):
    session_id: str
    code: str


@app.post("/api/providers/oauth/{provider_id}/submit")
async def submit_oauth_code(provider_id: str, body: OAuthSubmitBody, request: Request):
    """Submit the auth code for PKCE flows. Token-protected."""
    _require_token(request)
    if provider_id == "anthropic":
        return await asyncio.get_running_loop().run_in_executor(
            None, _submit_anthropic_pkce, body.session_id, body.code,
        )
    raise HTTPException(status_code=400, detail=f"submit not supported for {provider_id}")


@app.get("/api/providers/oauth/{provider_id}/poll/{session_id}")
async def poll_oauth_session(provider_id: str, session_id: str):
    """Poll a device-code session's status (no auth — read-only state)."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if sess["provider"] != provider_id:
        raise HTTPException(status_code=400, detail="Provider mismatch for session")
    return {
        "session_id": session_id,
        "status": sess["status"],
        "error_message": sess.get("error_message"),
        "expires_at": sess.get("expires_at"),
    }


@app.delete("/api/providers/oauth/sessions/{session_id}")
async def cancel_oauth_session(session_id: str, request: Request):
    """Cancel a pending OAuth session. Token-protected."""
    _require_token(request)
    with _oauth_sessions_lock:
        sess = _oauth_sessions.pop(session_id, None)
    if sess is None:
        return {"ok": False, "message": "session not found"}
    return {"ok": True, "session_id": session_id}


# ---------------------------------------------------------------------------
# Session detail endpoints
# ---------------------------------------------------------------------------



def _session_latest_descendant(session_id: str):
    """Resolve a session id to the newest child leaf session.

    /model may create child sessions. Dashboard refresh should continue the
    newest child instead of reopening the old parent.
    """
    from mangaba_agent.mangaba_state import SessionDB

    def row_get(row, key, index):
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row[key]
        except Exception:
            try:
                return row[index]
            except Exception:
                return None

    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid or not db.get_session(sid):
            return None, []

        conn = (
            getattr(db, "conn", None)
            or getattr(db, "_conn", None)
            or getattr(db, "connection", None)
            or getattr(db, "_connection", None)
        )

        rows = []
        if conn is not None:
            raw_rows = conn.execute(
                "SELECT id, parent_session_id, started_at FROM sessions"
            ).fetchall()
            for row in raw_rows:
                rows.append({
                    "id": row_get(row, "id", 0),
                    "parent_session_id": row_get(row, "parent_session_id", 1),
                    "started_at": row_get(row, "started_at", 2),
                })
        else:
            rows = db.list_sessions_rich(limit=10000, offset=0)

        children = {}
        for row in rows:
            rid = row.get("id")
            parent = row.get("parent_session_id")
            if rid and parent:
                children.setdefault(parent, []).append(row)

        def started(row):
            try:
                return float(row.get("started_at") or 0)
            except Exception:
                return 0.0

        current = sid
        path = [sid]
        seen = {sid}

        while children.get(current):
            candidates = [r for r in children[current] if r.get("id") not in seen]
            if not candidates:
                break
            candidates.sort(key=started, reverse=True)
            current = candidates[0]["id"]
            path.append(current)
            seen.add(current)

        return current, path
    finally:
        db.close()

@app.get("/api/sessions/{session_id}")
async def get_session_detail(session_id: str):
    from mangaba_agent.mangaba_state import SessionDB
    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        session = db.get_session(sid) if sid else None
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session
    finally:
        db.close()



@app.get("/api/sessions/{session_id}/latest-descendant")
async def get_session_latest_descendant(session_id: str):
    latest, path = _session_latest_descendant(session_id)
    if not latest:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "requested_session_id": path[0] if path else session_id,
        "session_id": latest,
        "path": path,
        "changed": bool(path and latest != path[0]),
    }

@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    from mangaba_agent.mangaba_state import SessionDB
    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        messages = db.get_messages(sid)
        return {"session_id": sid, "messages": messages}
    finally:
        db.close()


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    from mangaba_agent.mangaba_state import SessionDB
    db = SessionDB()
    try:
        if not db.delete_session(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"ok": True}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Log viewer endpoint
# ---------------------------------------------------------------------------


@app.get("/api/logs")
async def get_logs(
    file: str = "agent",
    lines: int = 100,
    level: Optional[str] = None,
    component: Optional[str] = None,
    search: Optional[str] = None,
):
    from mangaba_cli.logs import _read_tail, LOG_FILES

    log_name = LOG_FILES.get(file)
    if not log_name:
        raise HTTPException(status_code=400, detail=f"Unknown log file: {file}")
    log_path = get_mangaba_home() / "logs" / log_name
    if not log_path.exists():
        return {"file": file, "lines": []}

    try:
        from mangaba_agent.mangaba_logging import COMPONENT_PREFIXES
    except ImportError:
        COMPONENT_PREFIXES = {}

    # Normalize "ALL" / "all" / empty → no filter. _matches_filters treats an
    # empty tuple as "must match a prefix" (startswith(()) is always False),
    # so passing () instead of None silently drops every line.
    min_level = level if level and level.upper() != "ALL" else None
    if component and component.lower() != "all":
        comp_prefixes = COMPONENT_PREFIXES.get(component)
        if comp_prefixes is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown component: {component}. "
                       f"Available: {', '.join(sorted(COMPONENT_PREFIXES))}",
            )
    else:
        comp_prefixes = None

    has_filters = bool(min_level or comp_prefixes or search)
    result = _read_tail(
        log_path, min(lines, 500) if not search else 2000,
        has_filters=has_filters,
        min_level=min_level,
        component_prefixes=comp_prefixes,
    )
    # Post-filter by search term (case-insensitive substring match).
    # _read_tail doesn't support free-text search, so we filter here and
    # trim to the requested line count afterward.
    if search:
        needle = search.lower()
        result = [l for l in result if needle in l.lower()][-min(lines, 500):]
    return {"file": file, "lines": result}


# ---------------------------------------------------------------------------
# Cron job management endpoints
# ---------------------------------------------------------------------------


class CronJobCreate(BaseModel):
    prompt: str
    schedule: str
    name: str = ""
    deliver: str = "local"


class CronJobUpdate(BaseModel):
    updates: dict


_CRON_PROFILE_LOCK = threading.RLock()


def _cron_profile_dicts() -> List[Dict[str, Any]]:
    """Return dashboard profile records, falling back to a directory scan."""
    from mangaba_cli import profiles as profiles_mod
    try:
        return [_profile_to_dict(p) for p in profiles_mod.list_profiles()]
    except Exception:
        _log.exception("Failed to list profiles for cron dashboard; falling back to directory scan")
        return _fallback_profile_dicts(profiles_mod)


def _cron_profile_home(profile: Optional[str]) -> Tuple[str, Path]:
    """Resolve a profile query value to (profile_name, MANGABA_HOME)."""
    from mangaba_cli import profiles as profiles_mod

    raw = (profile or "default").strip() or "default"
    try:
        canon = profiles_mod.normalize_profile_name(raw)
        profiles_mod.validate_profile_name(canon)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(canon):
        raise HTTPException(status_code=404, detail=f"Profile '{canon}' does not exist.")
    return canon, profiles_mod.get_profile_dir(canon)


def _annotate_cron_job(job: Dict[str, Any], profile: str, home: Path) -> Dict[str, Any]:
    annotated = dict(job)
    annotated["profile"] = profile
    annotated["profile_name"] = profile
    annotated["mangaba_home"] = str(home)
    annotated["is_default_profile"] = profile == "default"
    return annotated


def _call_cron_for_profile(profile: Optional[str], func_name: str, *args, **kwargs):
    """Run cron.jobs helpers against the selected profile's cron directory.

    cron.jobs keeps CRON_DIR/JOBS_FILE/OUTPUT_DIR as module globals resolved
    from the process MANGABA_HOME at import time. The dashboard is a single
    process that can inspect many profiles, so temporarily retarget those
    globals while holding a lock and restore them immediately after the call.
    """
    profile_name, home = _cron_profile_home(profile)
    with _CRON_PROFILE_LOCK:
        from cron import jobs as cron_jobs

        old_cron_dir = cron_jobs.CRON_DIR
        old_jobs_file = cron_jobs.JOBS_FILE
        old_output_dir = cron_jobs.OUTPUT_DIR
        cron_jobs.CRON_DIR = home / "cron"
        cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
        cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
        try:
            result = getattr(cron_jobs, func_name)(*args, **kwargs)
        finally:
            cron_jobs.CRON_DIR = old_cron_dir
            cron_jobs.JOBS_FILE = old_jobs_file
            cron_jobs.OUTPUT_DIR = old_output_dir

    if isinstance(result, list):
        return [_annotate_cron_job(j, profile_name, home) for j in result]
    if isinstance(result, dict):
        return _annotate_cron_job(result, profile_name, home)
    return result


def _find_cron_job_profile(job_id: str) -> Optional[str]:
    for profile in _cron_profile_dicts():
        name = str(profile.get("name") or "")
        if not name:
            continue
        jobs = _call_cron_for_profile(name, "list_jobs", True)
        if any(j.get("id") == job_id or j.get("name") == job_id for j in jobs):
            return name
    return None


@app.get("/api/cron/jobs")
async def list_cron_jobs(profile: str = "all"):
    requested = (profile or "all").strip()
    if requested.lower() != "all":
        return _call_cron_for_profile(requested, "list_jobs", True)

    jobs: List[Dict[str, Any]] = []
    for item in _cron_profile_dicts():
        name = str(item.get("name") or "")
        if not name:
            continue
        try:
            jobs.extend(_call_cron_for_profile(name, "list_jobs", True))
        except Exception:
            _log.exception("Failed to list cron jobs for profile %s", name)
    return jobs


@app.get("/api/cron/jobs/{job_id}")
async def get_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "get_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs")
async def create_cron_job(body: CronJobCreate, profile: str = "default"):
    try:
        return _call_cron_for_profile(
            profile,
            "create_job",
            prompt=body.prompt,
            schedule=body.schedule,
            name=body.name,
            deliver=body.deliver,
        )
    except Exception as e:
        _log.exception("POST /api/cron/jobs failed")
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, body: CronJobUpdate, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "update_job", job_id, body.updates)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/pause")
async def pause_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "pause_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/resume")
async def resume_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "resume_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "trigger_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _call_cron_for_profile(selected, "remove_job", job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Profile management endpoints (minimal — list/create/rename/delete + SOUL.md)
# ---------------------------------------------------------------------------


class ProfileCreate(BaseModel):
    name: str
    clone_from_default: bool = False
    no_skills: bool = False


class ProfileRename(BaseModel):
    new_name: str


class ProfileSoulUpdate(BaseModel):
    content: str


def _profile_attr(info, name: str, default: Any = None) -> Any:
    try:
        return getattr(info, name)
    except Exception:
        return default


def _profile_to_dict(info) -> Dict[str, Any]:
    return {
        "name": _profile_attr(info, "name", ""),
        "path": str(_profile_attr(info, "path", "")),
        "is_default": bool(_profile_attr(info, "is_default", False)),
        "model": _profile_attr(info, "model"),
        "provider": _profile_attr(info, "provider"),
        "has_env": bool(_profile_attr(info, "has_env", False)),
        "skill_count": int(_profile_attr(info, "skill_count", 0) or 0),
    }


def _fallback_profile_dicts(profiles_mod) -> List[Dict[str, Any]]:
    def _safe(callable_, default):
        try:
            return callable_()
        except Exception:
            return default

    profiles: List[Dict[str, Any]] = []
    default_home = profiles_mod._get_default_mangaba_home()
    if default_home.is_dir():
        model, provider = _safe(lambda: profiles_mod._read_config_model(default_home), (None, None))
        profiles.append({
            "name": "default",
            "path": str(default_home),
            "is_default": True,
            "model": model,
            "provider": provider,
            "has_env": (default_home / ".env").exists(),
            "skill_count": _safe(lambda: profiles_mod._count_skills(default_home), 0),
        })

    profiles_root = profiles_mod._get_profiles_root()
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir() or not profiles_mod._PROFILE_ID_RE.match(entry.name):
                continue
            model, provider = _safe(lambda entry=entry: profiles_mod._read_config_model(entry), (None, None))
            profiles.append({
                "name": entry.name,
                "path": str(entry),
                "is_default": False,
                "model": model,
                "provider": provider,
                "has_env": (entry / ".env").exists(),
                "skill_count": _safe(lambda entry=entry: profiles_mod._count_skills(entry), 0),
            })

    return profiles


def _resolve_profile_dir(name: str) -> Path:
    """Validate ``name`` and resolve to its directory or raise an HTTPException."""
    from mangaba_cli import profiles as profiles_mod
    try:
        profiles_mod.validate_profile_name(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(name):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' does not exist.")
    return profiles_mod.get_profile_dir(name)


def _profile_setup_command(name: str) -> str:
    """Return the shell command used to configure a profile in the CLI."""
    _resolve_profile_dir(name)
    return "mangaba setup" if name == "default" else f"{name} setup"


@app.get("/api/profiles")
async def list_profiles_endpoint():
    from mangaba_cli import profiles as profiles_mod
    try:
        return {"profiles": [_profile_to_dict(p) for p in profiles_mod.list_profiles()]}
    except Exception:
        _log.exception("GET /api/profiles failed; falling back to profile directory scan")
        return {"profiles": _fallback_profile_dicts(profiles_mod)}


@app.post("/api/profiles")
async def create_profile_endpoint(body: ProfileCreate):
    from mangaba_cli import profiles as profiles_mod
    try:
        path = profiles_mod.create_profile(
            name=body.name,
            clone_from="default" if body.clone_from_default else None,
            clone_config=body.clone_from_default,
            no_skills=body.no_skills,
        )
        # Match the CLI's profile-create flow: fresh named profiles get the
        # bundled skills installed. When cloning from default, create_profile()
        # has already copied the source profile's skills, including any
        # user-installed skills. When no_skills=True, create_profile() wrote
        # the opt-out marker and seed_profile_skills() will no-op.
        if not body.clone_from_default:
            profiles_mod.seed_profile_skills(path, quiet=True)

        # Match the CLI's profile-create flow: named profiles should get a
        # wrapper in ~/.local/bin when the alias is safe to create.
        collision = profiles_mod.check_alias_collision(body.name)
        if not collision:
            profiles_mod.create_wrapper_script(body.name)
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.exception("POST /api/profiles failed")
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "name": body.name, "path": str(path)}


@app.get("/api/profiles/{name}/setup-command")
async def get_profile_setup_command(name: str):
    return {"command": _profile_setup_command(name)}


@app.post("/api/profiles/{name}/open-terminal")
async def open_profile_terminal_endpoint(name: str):
    try:
        command = _profile_setup_command(name)

        if sys.platform.startswith("win"):
            subprocess.Popen(["cmd.exe", "/c", "start", "", command])
        elif sys.platform == "darwin":
            escaped = command.replace("\\", "\\\\").replace('"', '\\"')
            applescript = (
                'tell application "Terminal"\n'
                "activate\n"
                f'do script "{escaped}"\n'
                "end tell"
            )
            subprocess.Popen(["osascript", "-e", applescript])
        else:
            terminal_commands = [
                ("x-terminal-emulator", ["x-terminal-emulator", "-e", "sh", "-lc", command]),
                ("gnome-terminal", ["gnome-terminal", "--", "sh", "-lc", command]),
                ("konsole", ["konsole", "-e", "sh", "-lc", command]),
                ("xfce4-terminal", ["xfce4-terminal", "-e", f"sh -lc '{command}'"]),
                ("mate-terminal", ["mate-terminal", "-e", f"sh -lc '{command}'"]),
                ("lxterminal", ["lxterminal", "-e", f"sh -lc '{command}'"]),
                ("tilix", ["tilix", "-e", "sh", "-lc", command]),
                ("alacritty", ["alacritty", "-e", "sh", "-lc", command]),
                ("kitty", ["kitty", "sh", "-lc", command]),
                ("xterm", ["xterm", "-e", "sh", "-lc", command]),
            ]
            for executable, popen_args in terminal_commands:
                if subprocess.call(
                    ["which", executable],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ) == 0:
                    subprocess.Popen(popen_args)
                    break
            else:
                raise HTTPException(
                    status_code=400,
                    detail="No supported terminal emulator found",
                )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("POST /api/profiles/%s/open-terminal failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "command": command}


@app.patch("/api/profiles/{name}")
async def rename_profile_endpoint(name: str, body: ProfileRename):
    from mangaba_cli import profiles as profiles_mod
    try:
        path = profiles_mod.rename_profile(name, body.new_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.exception("PATCH /api/profiles/%s failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "name": body.new_name, "path": str(path)}


@app.delete("/api/profiles/{name}")
async def delete_profile_endpoint(name: str):
    """Delete a profile. The dashboard collects the user's confirmation in
    its own dialog before this request, so we always pass ``yes=True`` to
    skip the CLI's interactive prompt."""
    from mangaba_cli import profiles as profiles_mod
    try:
        path = profiles_mod.delete_profile(name, yes=True)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.exception("DELETE /api/profiles/%s failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "path": str(path)}


@app.get("/api/profiles/{name}/soul")
async def get_profile_soul(name: str):
    soul_path = _resolve_profile_dir(name) / "SOUL.md"
    if soul_path.exists():
        try:
            return {"content": soul_path.read_text(encoding="utf-8"), "exists": True}
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Could not read SOUL.md: {e}")
    return {"content": "", "exists": False}


@app.put("/api/profiles/{name}/soul")
async def update_profile_soul(name: str, body: ProfileSoulUpdate):
    soul_path = _resolve_profile_dir(name) / "SOUL.md"
    try:
        soul_path.write_text(body.content, encoding="utf-8")
    except OSError as e:
        _log.exception("PUT /api/profiles/%s/soul failed", name)
        raise HTTPException(status_code=500, detail=f"Could not write SOUL.md: {e}")
    return {"ok": True}


class ProfileModelUpdate(BaseModel):
    model: str  # ex.: "Qwen/Qwen2.5-7B-Instruct" — default+name


@app.get("/api/profiles/{name}/model")
async def get_profile_model(name: str):
    """Lê o modelo configurado no config.yaml de um profile específico,
    sem precisar trocar o profile ativo."""
    import yaml

    cfg_path = _resolve_profile_dir(name) / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    except Exception:
        cfg = {}
    m = (cfg or {}).get("model")
    if isinstance(m, dict):
        return {"model": m.get("default") or m.get("name") or "", "provider": m.get("provider", "")}
    return {"model": m or "", "provider": ""}


@app.put("/api/profiles/{name}/model")
async def set_profile_model(name: str, body: ProfileModelUpdate):
    """Grava model.default/name no config.yaml do profile (preserva o resto)."""
    import yaml

    cfg_path = _resolve_profile_dir(name) / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        cfg = cfg or {}
        m = cfg.get("model")
        if not isinstance(m, dict):
            m = {} if not m else {"default": str(m), "name": str(m)}
        m["default"] = body.model.strip()
        m["name"] = body.model.strip()
        cfg["model"] = m
        cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _log.exception("PUT /api/profiles/%s/model failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "model": body.model.strip()}


# ── Teams por agente (1 bot por agente) ─────────────────────────────────────
class TeamsCreds(BaseModel):
    client_id: str
    client_secret: str
    tenant_id: str


def _read_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def _alloc_teams_port() -> int:
    """Aloca uma porta TEAMS livre (base 3978) varrendo os .env dos profiles."""
    from mangaba_cli import profiles as profiles_mod

    used = set()
    try:
        for p in profiles_mod.list_profiles():
            pdir = profiles_mod.get_profile_dir(_profile_attr(p, "name", ""))
            env = _read_env_file(pdir / ".env")
            if env.get("TEAMS_PORT"):
                try:
                    used.add(int(env["TEAMS_PORT"]))
                except Exception:
                    pass
    except Exception:
        pass
    port = 3978
    while port in used:
        port += 1
    return port


@app.get("/api/profiles/{name}/teams")
async def get_profile_teams(name: str):
    env = _read_env_file(_resolve_profile_dir(name) / ".env")
    cid = env.get("TEAMS_CLIENT_ID", "")
    return {
        "configured": bool(cid),
        "client_id": (cid[:8] + "…") if cid else "",
        "has_secret": bool(env.get("TEAMS_CLIENT_SECRET")),
        "tenant_id": env.get("TEAMS_TENANT_ID", ""),
        "port": int(env["TEAMS_PORT"]) if env.get("TEAMS_PORT") else None,
    }


@app.post("/api/profiles/{name}/teams/connect")
async def connect_profile_teams(name: str, body: TeamsCreds):
    """Valida e grava credenciais Teams no .env do profile (1 bot por agente)."""
    from mangaba_cli.client_profiles import _set_env_lines

    pdir = _resolve_profile_dir(name)
    res = _validate_teams(body.client_id, body.client_secret, body.tenant_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Credenciais inválidas."))
    env = _read_env_file(pdir / ".env")
    port = int(env["TEAMS_PORT"]) if env.get("TEAMS_PORT") else _alloc_teams_port()
    try:
        _set_env_lines(
            pdir / ".env",
            updates={
                "TEAMS_CLIENT_ID": body.client_id.strip(),
                "TEAMS_CLIENT_SECRET": body.client_secret.strip(),
                "TEAMS_TENANT_ID": body.tenant_id.strip(),
                "TEAMS_PORT": str(port),
            },
            remove_keys=set(),
        )
    except Exception as exc:  # noqa: BLE001
        _log.exception("connect_profile_teams failed")
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "ok": True,
        "port": port,
        "messaging_endpoint": "https://SEU_DOMINIO/api/messages",
        "note": "Aponte (proxy HTTPS) para 127.0.0.1:%d/api/messages e inicie o agente." % port,
    }


@app.delete("/api/profiles/{name}/teams")
async def disconnect_profile_teams(name: str):
    from mangaba_cli.client_profiles import _set_env_lines

    pdir = _resolve_profile_dir(name)
    _set_env_lines(pdir / ".env", updates={}, remove_keys={
        "TEAMS_CLIENT_ID", "TEAMS_CLIENT_SECRET", "TEAMS_TENANT_ID", "TEAMS_PORT",
    })
    return {"ok": True}


# ── Agentes verticais prontos (templates) ───────────────────────────────────
class TemplateInstall(BaseModel):
    name: str = ""  # nome do profile (vazio = usa o id do template)


@app.get("/api/agent-templates")
async def agent_templates_list():
    from mangaba_cli import agent_templates

    return {"templates": agent_templates.list_templates()}


@app.get("/api/agent-templates/{template_id}")
async def agent_template_get(template_id: str):
    from mangaba_cli import agent_templates

    tpl = agent_templates.get_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="template não encontrado")
    return tpl


# ── Conexão de canais com validação ao vivo (usabilidade) ───────────────────
class ChannelToken(BaseModel):
    token: str


_CHANNEL_ENV = {
    "telegram": "TELEGRAM_BOT_TOKEN",
    "discord": "DISCORD_BOT_TOKEN",
}


def _validate_channel_token(platform: str, token: str) -> Dict[str, Any]:
    """Confere o token chamando a API do provedor. Retorna {ok, name, ...}."""
    import httpx

    token = (token or "").strip()
    if not token:
        return {"ok": False, "error": "Token vazio."}
    try:
        if platform == "telegram":
            r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=12)
            d = r.json()
            if r.status_code == 200 and d.get("ok"):
                b = d["result"]
                return {"ok": True, "name": b.get("first_name", ""),
                        "username": b.get("username", "")}
            return {"ok": False, "error": "Token do Telegram inválido."}
        if platform == "discord":
            r = httpx.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token}"},
                timeout=12,
            )
            if r.status_code == 200:
                b = r.json()
                uname = b.get("username", "")
                disc = b.get("discriminator", "0")
                return {"ok": True, "name": uname,
                        "username": uname if disc in ("0", "", None) else f"{uname}#{disc}"}
            return {"ok": False, "error": "Token do Discord inválido."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Falha ao validar: {e}"}
    return {"ok": False, "error": "Canal não suportado."}


@app.post("/api/channels/{platform}/validate")
async def channel_validate(platform: str, body: ChannelToken):
    if platform not in _CHANNEL_ENV:
        raise HTTPException(status_code=400, detail="canal não suportado")
    return _validate_channel_token(platform, body.token)


@app.post("/api/channels/{platform}/connect")
async def channel_connect(platform: str, body: ChannelToken):
    """Valida, salva o token no .env e reinicia o gateway."""
    if platform not in _CHANNEL_ENV:
        raise HTTPException(status_code=400, detail="canal não suportado")
    res = _validate_channel_token(platform, body.token)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Token inválido."))
    try:
        save_env_value(_CHANNEL_ENV[platform], body.token.strip())
        _spawn_mangaba_action(["gateway", "restart"], "gateway-restart")
    except Exception as exc:  # noqa: BLE001
        _log.exception("channel connect failed")
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, **res}


@app.get("/api/channels/status")
async def channels_status():
    """Para cada canal: se há token e (se houver) o nome do bot."""
    import os as _os

    out = []
    for plat, env_key in _CHANNEL_ENV.items():
        tok = _os.getenv(env_key, "")
        info: Dict[str, Any] = {"platform": plat, "connected": bool(tok)}
        if tok:
            v = _validate_channel_token(plat, tok)
            info["valid"] = bool(v.get("ok"))
            info["name"] = v.get("name", "")
            info["username"] = v.get("username", "")
        out.append(info)
    return {"channels": out}


# ── WhatsApp Cloud API (oficial, Meta) ──────────────────────────────────────
class WhatsAppCloudCreds(BaseModel):
    token: str
    phone_number_id: str


@app.post("/api/whatsapp-cloud/validate")
async def whatsapp_cloud_validate(body: WhatsAppCloudCreds):
    from mangaba_cli import whatsapp_cloud

    return whatsapp_cloud.validate(body.token, body.phone_number_id)


@app.post("/api/whatsapp-cloud/connect")
async def whatsapp_cloud_connect(body: WhatsAppCloudCreds):
    """Valida, salva credenciais e gera o verify_token do webhook.

    Retorna a URL do webhook e o verify_token para o operador colar no painel
    da Meta (App → WhatsApp → Configuration → Webhook)."""
    import os as _os

    from mangaba_cli import whatsapp_cloud

    res = whatsapp_cloud.validate(body.token, body.phone_number_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Credenciais inválidas."))
    try:
        save_env_value("WHATSAPP_CLOUD_TOKEN", body.token.strip())
        save_env_value("WHATSAPP_PHONE_NUMBER_ID", body.phone_number_id.strip())
        verify = _os.getenv("WHATSAPP_VERIFY_TOKEN", "")
        if not verify:
            verify = secrets.token_urlsafe(18)
            save_env_value("WHATSAPP_VERIFY_TOKEN", verify)
        _os.environ["WHATSAPP_CLOUD_TOKEN"] = body.token.strip()
        _os.environ["WHATSAPP_PHONE_NUMBER_ID"] = body.phone_number_id.strip()
        _os.environ["WHATSAPP_VERIFY_TOKEN"] = verify
    except Exception as exc:  # noqa: BLE001
        _log.exception("whatsapp cloud connect failed")
        raise HTTPException(status_code=500, detail=str(exc))

    base = _api_base_url().replace("/v1", "")  # reaproveita host:porta do operador
    return {
        "ok": True,
        **res,
        "webhook_url": f"{base}/api/whatsapp/webhook",
        "verify_token": verify,
    }


# ── Microsoft Teams (Bot Framework / Azure) ─────────────────────────────────
def _validate_teams(client_id: str, client_secret: str, tenant_id: str) -> Dict[str, Any]:
    """Confere as credenciais pegando um token OAuth do Azure AD (Bot Framework)."""
    import httpx

    cid, sec, tid = client_id.strip(), client_secret.strip(), tenant_id.strip()
    if not (cid and sec and tid):
        return {"ok": False, "error": "Informe client_id, client_secret e tenant_id."}
    try:
        r = httpx.post(
            f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": sec,
                "scope": "https://api.botframework.com/.default",
            },
            timeout=15,
        )
        if r.status_code == 200 and r.json().get("access_token"):
            return {"ok": True, "name": "Bot do Teams"}
        try:
            msg = r.json().get("error_description", "").split("\n")[0]
        except Exception:
            msg = ""
        return {"ok": False, "error": msg or f"Credenciais inválidas (HTTP {r.status_code})."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Falha ao validar: {e}"}


@app.post("/api/teams/validate")
async def teams_validate(body: TeamsCreds):
    return _validate_teams(body.client_id, body.client_secret, body.tenant_id)


@app.post("/api/teams/connect")
async def teams_connect(body: TeamsCreds):
    """Valida, salva as credenciais e reinicia o gateway (auto-habilita o Teams).

    Retorna o messaging endpoint para registrar no Azure Bot."""
    res = _validate_teams(body.client_id, body.client_secret, body.tenant_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Credenciais inválidas."))
    try:
        save_env_value("TEAMS_CLIENT_ID", body.client_id.strip())
        save_env_value("TEAMS_CLIENT_SECRET", body.client_secret.strip())
        save_env_value("TEAMS_TENANT_ID", body.tenant_id.strip())
        _spawn_mangaba_action(["gateway", "restart"], "gateway-restart")
    except Exception as exc:  # noqa: BLE001
        _log.exception("teams connect failed")
        raise HTTPException(status_code=500, detail=str(exc))
    # O adapter do Teams escuta em TEAMS_PORT (padrão 3978) no caminho
    # /api/messages. O endpoint público (a registrar no Azure) deve apontar,
    # via proxy HTTPS, para essa porta.
    import os as _os

    port = _os.getenv("TEAMS_PORT", "3978")
    return {
        "ok": True,
        **res,
        "messaging_endpoint": "https://SEU_DOMINIO/api/messages",
        "internal_port": port,
    }


# Histórico curto em memória por número (continuidade de conversa).
_WA_HISTORY: Dict[str, List[Dict[str, str]]] = {}
_WA_HISTORY_MAX = 12


@app.get("/api/whatsapp/webhook")
async def whatsapp_webhook_verify(request: Request):
    """Verificação do webhook (Meta envia hub.* num GET)."""
    import os as _os

    qp = request.query_params
    mode = qp.get("hub.mode")
    token = qp.get("hub.verify_token")
    challenge = qp.get("hub.challenge", "")
    expected = _os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if mode == "subscribe" and expected and token == expected:
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="verify_token inválido")


@app.post("/api/whatsapp/webhook")
async def whatsapp_webhook_receive(request: Request):
    """Recebe mensagens da Meta, roda o agente e responde via Graph API."""
    import os as _os

    from mangaba_cli import whatsapp_cloud

    token = _os.getenv("WHATSAPP_CLOUD_TOKEN", "")
    pnid = _os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    if not token or not pnid:
        return {"ok": True}  # não configurado — ignora silenciosamente

    raw = await request.body()

    # Verificação de assinatura (defesa contra POSTs forjados na rota pública).
    # A Meta assina o corpo com HMAC-SHA256 usando o App Secret. Só é
    # ENFORÇADA se WHATSAPP_APP_SECRET estiver configurado.
    app_secret = _os.getenv("WHATSAPP_APP_SECRET", "")
    if app_secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            app_secret.encode(), raw, hashlib.sha256
        ).hexdigest()
        if not (sig and hmac.compare_digest(sig, expected)):
            raise HTTPException(status_code=403, detail="assinatura inválida")

    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        return {"ok": True}

    messages = whatsapp_cloud.parse_inbound(payload)
    if not messages:
        return {"ok": True}  # status/evento sem texto

    loop = asyncio.get_running_loop()

    def _handle(frm: str, text: str) -> None:
        try:
            agent = _build_chat_agent()
            hist = _WA_HISTORY.get(frm, [])
            result = agent.run_conversation(text, conversation_history=list(hist))
            reply = result.get("final_response", "") if isinstance(result, dict) else str(result)
            if reply:
                whatsapp_cloud.send_text(token, pnid, frm, reply)
                hist = (hist + [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": reply},
                ])[-_WA_HISTORY_MAX:]
                _WA_HISTORY[frm] = hist
        except Exception:  # noqa: BLE001
            _log.exception("whatsapp inbound handling failed")

    # Processa em background para responder o webhook na hora (a Meta exige 200 rápido).
    for m in messages:
        loop.run_in_executor(None, _handle, m["from"], m["text"])
    return {"ok": True}


@app.post("/api/agent-templates/{template_id}/install")
async def agent_template_install(template_id: str, body: TemplateInstall):
    """Cria um profile pré-configurado a partir de um template de setor."""
    import yaml

    from mangaba_cli import agent_templates
    from mangaba_cli import profiles as profiles_mod
    from mangaba_agent.mangaba_constants import get_mangaba_home

    tpl = agent_templates.get_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="template não encontrado")

    name = (body.name or template_id).strip()
    try:
        profiles_mod.validate_profile_name(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if profiles_mod.profile_exists(name):
        raise HTTPException(status_code=400, detail=f"Já existe um perfil '{name}'.")

    try:
        # Clona config/.env do profile ativo → herda credenciais e provider.
        path = profiles_mod.create_profile(
            name=name, clone_from="default", clone_config=True, no_alias=True
        )
        pdir = profiles_mod.get_profile_dir(name)

        # Persona → SOUL.md
        (pdir / "SOUL.md").write_text((tpl.get("persona") or "") + "\n", encoding="utf-8")

        # config.yaml: modelo (se o template definir) + RAG
        cfg_path = pdir / "config.yaml"
        cfg = {}
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}
        if tpl.get("model"):
            m = cfg.get("model")
            m = m if isinstance(m, dict) else {}
            m["default"] = tpl["model"]
            m["name"] = tpl["model"]
            cfg["model"] = m
        cfg.setdefault("memory", {})["provider"] = "mangaba_rag" if tpl.get("rag") else ""
        cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

        # Copia o índice RAG do profile ativo, se houver.
        if tpl.get("rag"):
            src_rag = get_mangaba_home() / "rag"
            dst_rag = pdir / "rag"
            if src_rag.is_dir() and not dst_rag.exists():
                import shutil

                try:
                    shutil.copytree(src_rag, dst_rag)
                except Exception:
                    pass

        if not profiles_mod.check_alias_collision(name):
            profiles_mod.create_wrapper_script(name)
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        _log.exception("install template %s failed", template_id)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "name": name, "path": str(path)}


# ---------------------------------------------------------------------------
# Skills & Tools endpoints
# ---------------------------------------------------------------------------


class SkillToggle(BaseModel):
    name: str
    enabled: bool


@app.get("/api/skills")
async def get_skills():
    from tools.skills_tool import _find_all_skills
    from mangaba_cli.skills_config import get_disabled_skills
    config = load_config()
    disabled = get_disabled_skills(config)
    skills = _find_all_skills(skip_disabled=True)
    for s in skills:
        s["enabled"] = s["name"] not in disabled
    return skills


@app.put("/api/skills/toggle")
async def toggle_skill(body: SkillToggle):
    from mangaba_cli.skills_config import get_disabled_skills, save_disabled_skills
    config = load_config()
    disabled = get_disabled_skills(config)
    if body.enabled:
        disabled.discard(body.name)
    else:
        disabled.add(body.name)
    save_disabled_skills(config, disabled)
    return {"ok": True, "name": body.name, "enabled": body.enabled}


@app.get("/api/tools/toolsets")
async def get_toolsets():
    from mangaba_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _toolset_has_keys,
    )
    from mangaba_agent.toolsets import resolve_toolset

    config = load_config()
    enabled_toolsets = _get_platform_tools(
        config,
        "cli",
        include_default_mcp_servers=False,
    )
    result = []
    for name, label, desc in _get_effective_configurable_toolsets():
        try:
            tools = sorted(set(resolve_toolset(name)))
        except Exception:
            tools = []
        is_enabled = name in enabled_toolsets
        result.append({
            "name": name, "label": label, "description": desc,
            "enabled": is_enabled,
            "available": is_enabled,
            "configured": _toolset_has_keys(name, config),
            "tools": tools,
        })
    return result


# ---------------------------------------------------------------------------
# Raw YAML config endpoint
# ---------------------------------------------------------------------------


class RawConfigUpdate(BaseModel):
    yaml_text: str


@app.get("/api/config/raw")
async def get_config_raw():
    path = get_config_path()
    if not path.exists():
        return {"yaml": ""}
    return {"yaml": path.read_text(encoding="utf-8")}


@app.put("/api/config/raw")
async def update_config_raw(body: RawConfigUpdate):
    try:
        parsed = yaml.safe_load(body.yaml_text)
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping")
        save_config(parsed)
        return {"ok": True}
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")


# ---------------------------------------------------------------------------
# Token / cost analytics endpoint
# ---------------------------------------------------------------------------


@app.get("/api/analytics/usage")
async def get_usage_analytics(days: int = 30):
    from mangaba_agent.mangaba_state import SessionDB
    from agent.insights import InsightsEngine

    db = SessionDB()
    try:
        cutoff = time.time() - (days * 86400)
        cur = db._conn.execute("""
            SELECT date(started_at, 'unixepoch') as day,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls
            FROM sessions WHERE started_at > ?
            GROUP BY day ORDER BY day
        """, (cutoff,))
        daily = [dict(r) for r in cur.fetchall()]

        cur2 = db._conn.execute("""
            SELECT model,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls
            FROM sessions WHERE started_at > ? AND model IS NOT NULL
            GROUP BY model ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
        """, (cutoff,))
        by_model = [dict(r) for r in cur2.fetchall()]

        cur3 = db._conn.execute("""
            SELECT SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions,
                   SUM(COALESCE(api_call_count, 0)) as total_api_calls
            FROM sessions WHERE started_at > ?
        """, (cutoff,))
        totals = dict(cur3.fetchone())
        insights_report = InsightsEngine(db).generate(days=days)
        skills = insights_report.get("skills", {
            "summary": {
                "total_skill_loads": 0,
                "total_skill_edits": 0,
                "total_skill_actions": 0,
                "distinct_skills_used": 0,
            },
            "top_skills": [],
        })

        return {
            "daily": daily,
            "by_model": by_model,
            "totals": totals,
            "period_days": days,
            "skills": skills,
        }
    finally:
        db.close()


@app.get("/api/analytics/models")
async def get_models_analytics(days: int = 30):
    """Rich per-model analytics for the Models dashboard page.

    Returns token/cost/session breakdown per model plus capability metadata
    from models.dev (context window, vision, tools, reasoning, etc.).
    """
    from mangaba_agent.mangaba_state import SessionDB

    db = SessionDB()
    try:
        cutoff = time.time() - (days * 86400)

        cur = db._conn.execute("""
            SELECT model,
                   billing_provider,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls,
                   SUM(tool_call_count) as tool_calls,
                   MAX(started_at) as last_used_at,
                   AVG(input_tokens + output_tokens) as avg_tokens_per_session
            FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
            GROUP BY model, billing_provider
            ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
        """, (cutoff,))
        rows = [dict(r) for r in cur.fetchall()]

        models = []
        for row in rows:
            provider = row.get("billing_provider") or ""
            model_name = row["model"]
            caps = {}
            try:
                from agent.models_dev import get_model_capabilities
                mc = get_model_capabilities(provider=provider, model=model_name)
                if mc is not None:
                    caps = {
                        "supports_tools": mc.supports_tools,
                        "supports_vision": mc.supports_vision,
                        "supports_reasoning": mc.supports_reasoning,
                        "context_window": mc.context_window,
                        "max_output_tokens": mc.max_output_tokens,
                        "model_family": mc.model_family,
                    }
            except Exception:
                pass

            models.append({
                "model": model_name,
                "provider": provider,
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "reasoning_tokens": row["reasoning_tokens"],
                "estimated_cost": row["estimated_cost"],
                "actual_cost": row["actual_cost"],
                "sessions": row["sessions"],
                "api_calls": row["api_calls"],
                "tool_calls": row["tool_calls"],
                "last_used_at": row["last_used_at"],
                "avg_tokens_per_session": row["avg_tokens_per_session"],
                "capabilities": caps,
            })

        totals_cur = db._conn.execute("""
            SELECT COUNT(DISTINCT model) as distinct_models,
                   SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions,
                   SUM(COALESCE(api_call_count, 0)) as total_api_calls
            FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
        """, (cutoff,))
        totals = dict(totals_cur.fetchone())

        return {
            "models": models,
            "totals": totals,
            "period_days": days,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# /api/pty — PTY-over-WebSocket bridge for the dashboard "Chat" tab.
#
# The endpoint spawns the same ``mangaba --tui`` binary the CLI uses, behind
# a POSIX pseudo-terminal, and forwards bytes + resize escapes across a
# WebSocket.  The browser renders the ANSI through xterm.js (see
# web/src/pages/ChatPage.tsx).
#
# Auth: ``?token=<session_token>`` query param (browsers can't set
# Authorization on the WS upgrade).  Same ephemeral ``_SESSION_TOKEN`` as
# REST.  Localhost-only — we defensively reject non-loopback clients even
# though uvicorn binds to 127.0.0.1.
# ---------------------------------------------------------------------------

import re
import asyncio

# PTY bridge is POSIX-only (depends on fcntl/termios/ptyprocess).  On native
# Windows the import raises; catch and leave PtyBridge=None so the rest of
# the dashboard (sessions, jobs, metrics, config editor) still loads and the
# /api/pty endpoint cleanly refuses with a WSL-suggested message.
try:
    from mangaba_cli.pty_bridge import PtyBridge, PtyUnavailableError
    _PTY_BRIDGE_AVAILABLE = True
except ImportError as _pty_import_err:  # pragma: no cover - Windows-only path
    PtyBridge = None  # type: ignore[assignment]
    _PTY_BRIDGE_AVAILABLE = False

    class PtyUnavailableError(RuntimeError):  # type: ignore[no-redef]
        """Stub on platforms where pty_bridge can't be imported."""
        pass

_RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")
_PTY_READ_CHUNK_TIMEOUT = 0.2
_VALID_CHANNEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# Starlette's TestClient reports the peer as "testclient"; treat it as
# loopback so tests don't need to rewrite request scope.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _is_public_bind() -> bool:
    """True when bound to all-interfaces (operator used --insecure)."""
    return getattr(app.state, "bound_host", "") in {"0.0.0.0", "::"}


def _ws_client_is_allowed(ws: "WebSocket") -> bool:
    """Check if the WebSocket client IP is acceptable.

    Allows loopback always; allows any IP when bound to all-interfaces
    (--insecure mode, guarded by session token auth).
    """
    if _is_public_bind():
        return True
    client_host = ws.client.host if ws.client else ""
    if not client_host:
        return True
    return client_host in _LOOPBACK_HOSTS

# Per-channel subscriber registry used by /api/pub (PTY-side gateway → dashboard)
# and /api/events (dashboard → browser sidebar).  Keyed by an opaque channel id
# the chat tab generates on mount; entries auto-evict when the last subscriber
# drops AND the publisher has disconnected.
_event_channels: dict[str, set] = {}
_event_lock = asyncio.Lock()


def _resolve_chat_argv(
    resume: Optional[str] = None,
    sidecar_url: Optional[str] = None,
) -> tuple[list[str], Optional[str], Optional[dict]]:
    """Resolve the argv + cwd + env for the chat PTY.

    Default: whatever ``mangaba --tui`` would run.  Tests monkeypatch this
    function to inject a tiny fake command (``cat``, ``sh -c 'printf …'``)
    so nothing has to build Node or the TUI bundle.

    Session resume is propagated via the ``MANGABA_TUI_RESUME`` env var —
    matching what ``mangaba_cli.main._launch_tui`` does for the CLI path.
    Appending ``--resume <id>`` to argv doesn't work because ``ui-tui`` does
    not parse its argv.

    `sidecar_url` (when set) is forwarded as ``MANGABA_TUI_SIDECAR_URL`` so
    the spawned ``tui_gateway.entry`` can mirror dispatcher emits to the
    dashboard's ``/api/pub`` endpoint (see :func:`pub_ws`).
    """
    from mangaba_cli.main import PROJECT_ROOT, _make_tui_argv

    argv, cwd = _make_tui_argv(PROJECT_ROOT / "ui-tui", tui_dev=False)
    env = os.environ.copy()
    env.setdefault("NODE_ENV", "production")
    # Browser-embedded chat should prefer stable wheel-based scrollback over
    # native terminal mouse tracking. When mouse tracking is enabled, wheel
    # events are consumed by the TUI and forwarded as terminal input, which
    # makes browser-side transcript scrolling feel broken. Keep the terminal
    # build unchanged for native CLI usage; only disable mouse tracking for
    # the dashboard PTY path.
    env.setdefault("MANGABA_TUI_DISABLE_MOUSE", "1")
    env.setdefault("MANGABA_TUI_INLINE", "1")

    if resume:
        latest_resume, _latest_path = _session_latest_descendant(resume)
        if latest_resume:
            resume = latest_resume
        env["MANGABA_TUI_RESUME"] = resume

    if sidecar_url:
        env["MANGABA_TUI_SIDECAR_URL"] = sidecar_url

    return list(argv), str(cwd) if cwd else None, env


def _build_sidecar_url(channel: str) -> Optional[str]:
    """ws:// URL the PTY child should publish events to, or None when unbound."""
    host = getattr(app.state, "bound_host", None)
    port = getattr(app.state, "bound_port", None)

    if not host or not port:
        return None

    netloc = f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"
    qs = urllib.parse.urlencode({"token": _SESSION_TOKEN, "channel": channel})

    return f"ws://{netloc}/api/pub?{qs}"


async def _broadcast_event(channel: str, payload: str) -> None:
    """Fan out one publisher frame to every subscriber on `channel`."""
    async with _event_lock:
        subs = list(_event_channels.get(channel, ()))

    for sub in subs:
        try:
            await sub.send_text(payload)
        except Exception:
            # Subscriber went away mid-send; the /api/events finally clause
            # will remove it from the registry on its next iteration.
            _log.warning("broadcast send failed for subscriber on %s", channel, exc_info=True)


def _channel_or_close_code(ws: WebSocket) -> Optional[str]:
    """Return the channel id from the query string or None if invalid."""
    channel = ws.query_params.get("channel", "")

    return channel if _VALID_CHANNEL_RE.match(channel) else None


_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _chat_profile_home(agent_profile: str) -> Optional[Path]:
    """Resolve the MANGABA_HOME directory of a fleet profile by name.

    Names are validated against a strict charset (no separators) so a
    WebSocket payload can't path-traverse out of the profiles root.
    """
    name = (agent_profile or "").strip()
    if not name or name.lower() == "default" or not _PROFILE_NAME_RE.match(name):
        return None
    prof = Path.home() / ".mangaba" / "profiles" / name
    return prof if prof.is_dir() else None


def _parse_profile_dotenv(prof_home: Path) -> Dict[str, str]:
    """Parse ``<profile>/.env`` into a dict (best-effort, sem exportar)."""
    out: Dict[str, str] = {}
    try:
        for line in (prof_home / ".env").read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return out


def _chat_profile_mcp_servers(prof_home: Path) -> Dict[str, dict]:
    """``mcp_servers`` do config.yaml do profile, com ``${VAR}`` interpolado.

    A interpolação usa o ambiente do processo + o ``.env`` do próprio profile
    (sem exportá-lo — chaves de um agente não vazam para os outros).
    """
    try:
        cfg = yaml.safe_load((prof_home / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    servers = cfg.get("mcp_servers")
    if not servers or not isinstance(servers, dict):
        return {}

    env = dict(os.environ)
    env.update(_parse_profile_dotenv(prof_home))
    var_re = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def _interp(value):
        if isinstance(value, str):
            return var_re.sub(lambda m: env.get(m.group(1), m.group(0)), value)
        if isinstance(value, dict):
            return {k: _interp(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_interp(v) for v in value]
        return value

    return {
        str(name): _interp(server_cfg)
        for name, server_cfg in servers.items()
        if isinstance(server_cfg, dict)
        and str(server_cfg.get("enabled", "true")).lower() not in ("false", "0", "no")
    }


def _build_chat_agent(
    model_override: str = None,
    provider_override: str = None,
    agent_profile: str = None,
):
    """Build an AIAgent for the dashboard chat, mirroring the oneshot/CLI path.

    ``model_override`` / ``provider_override`` let the Chat tab switch models on
    the fly for testing; when unset, the profile's configured default is used.

    ``agent_profile`` embodies one of the created agents (fleet profiles): its
    SOUL.md becomes the persona and its configured model/endpoint is used when
    resolvable, so the Chat tab talks to the agent the user built — the model
    itself stays whatever was configured on the Models page for that agent.
    """
    from mangaba_cli.config import load_config
    from mangaba_cli.runtime_provider import resolve_runtime_provider
    from mangaba_agent.run_agent import AIAgent

    cfg = load_config()
    model_cfg = cfg.get("model") or {}

    soul_prompt = None
    use_profile_endpoint = False
    prof_home = _chat_profile_home(agent_profile) if agent_profile else None
    if prof_home is not None:
        soul_path = prof_home / "SOUL.md"
        if soul_path.exists():
            soul_prompt = soul_path.read_text(encoding="utf-8").strip() or None
        pcfg_path = prof_home / "config.yaml"
        if pcfg_path.exists():
            try:
                pmodel = (yaml.safe_load(pcfg_path.read_text(encoding="utf-8")) or {}).get("model") or {}
                # Only adopt the profile's model block when it's locally
                # resolvable (explicit endpoint); provider-keyed setups still
                # resolve through the active profile's credentials below.
                if pmodel.get("base_url") and (pmodel.get("default") or pmodel.get("name")):
                    model_cfg = pmodel
                    use_profile_endpoint = True
            except Exception:  # noqa: BLE001
                _log.warning("chat: config.yaml ilegível para o profile %s", agent_profile)

    effective_model = str(
        model_override or model_cfg.get("default") or model_cfg.get("name") or ""
    ).strip()
    if use_profile_endpoint:
        runtime = {
            "api_key": model_cfg.get("api_key") or "x",
            "base_url": model_cfg.get("base_url"),
            "provider": model_cfg.get("provider") or "custom",
            "api_mode": None,
            "credential_pool": None,
        }
    else:
        runtime = resolve_runtime_provider(
            requested=(provider_override or None), target_model=effective_model or None
        )
    try:
        from mangaba_cli.tools_config import _get_platform_tools
        toolsets = sorted(_get_platform_tools(cfg, "cli"))
    except Exception:  # noqa: BLE001
        toolsets = None

    # MCPs do agente encarnado: conecta os servidores do profile (o registry
    # do processo é idempotente por nome — colisão de nome entre profiles usa
    # a primeira conexão) e isola por toolset: o agente ganha os seus
    # ``mcp-<servidor>`` e perde os do profile ativo que não sejam dele.
    disabled_toolsets = None
    if prof_home is not None:
        try:
            from tools.mcp_tool import _load_mcp_config, register_mcp_servers

            profile_servers = _chat_profile_mcp_servers(prof_home)
            if profile_servers:
                register_mcp_servers(profile_servers)
                toolsets = (toolsets or []) + [f"mcp-{n}" for n in profile_servers]
            active_servers = set(_load_mcp_config() or {})
            foreign = active_servers - set(profile_servers)
            if foreign:
                disabled_toolsets = [f"mcp-{n}" for n in sorted(foreign)]
        except Exception:  # noqa: BLE001
            _log.warning("chat: MCPs do profile %s indisponíveis", agent_profile, exc_info=True)

    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        model=effective_model or None,
        enabled_toolsets=toolsets,
        disabled_toolsets=disabled_toolsets,
        quiet_mode=True,
        platform="cli",
        credential_pool=runtime.get("credential_pool"),
        ephemeral_system_prompt=soul_prompt,
    )
    agent.suppress_status_output = True
    # Skills do agente encarnado: cada turno ativa este override context-local
    # (ver chat_ws.run_turn) para que índice, skills_list e skill_view resolvam
    # no diretório do profile selecionado, não no do profile ativo.
    skills_dir = (prof_home / "skills") if prof_home is not None else None
    agent._mangaba_skills_override = (
        skills_dir if (skills_dir is not None and skills_dir.is_dir()) else None
    )
    return agent


@app.get("/api/chat/models")
def chat_models() -> Dict[str, Any]:
    """Modelos disponíveis para o seletor da aba Chat.

    Enumera os modelos locais do Ollama (que ``/api/model/options`` não lista)
    via ``/api/tags``, inclui o modelo configurado, e cai para os providers do
    picker quando não há Ollama local.
    """
    import json as _json
    import urllib.request

    from mangaba_cli.config import load_config

    cfg = load_config()
    model_cfg = cfg.get("model") or {}
    current = str(model_cfg.get("default") or model_cfg.get("name") or "").strip()
    provider = str(model_cfg.get("provider") or "").strip().lower()
    base = str(model_cfg.get("base_url") or "").strip()

    out: List[Dict[str, str]] = []
    seen: set = set()

    def _add(prov: str, name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            out.append({"provider": prov, "model": name})

    # 0) Hugging Face — lista curada de modelos verificados na Inference API.
    #    (O branding Mangaba é aplicado no frontend via brandModel.)
    if provider == "huggingface" or "huggingface" in base:
        _HF_MODELS = [
            "Qwen/Qwen2.5-7B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
            "meta-llama/Llama-3.3-70B-Instruct",
            "meta-llama/Llama-3.1-8B-Instruct",
            "deepseek-ai/DeepSeek-V3-0324",
        ]
        if current:
            _add("huggingface", current)
        for m in _HF_MODELS:
            _add("huggingface", m)
        return {"models": out, "current": current}

    # 1) Ollama local — deriva o host de base_url (…/v1 → …/api/tags).
    if provider == "ollama" or "11434" in base or not base:
        host = base.rstrip("/")
        if host.endswith("/v1"):
            host = host[: -len("/v1")]
        if not host:
            host = "http://localhost:11434"
        try:
            req = urllib.request.Request(f"{host}/api/tags")
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            for m in data.get("models", []):
                name = m.get("name") or m.get("model")
                if name:
                    _add("ollama", str(name))
        except Exception:  # noqa: BLE001
            pass

    # 2) Garante que o modelo atual está na lista (mesmo se o Ollama não listar).
    if current:
        _add(provider or "ollama", current)

    # 3) Fallback: providers do picker (setups de nuvem sem Ollama).
    if not out:
        try:
            from mangaba_cli.inventory import build_models_payload, load_picker_context
            payload = build_models_payload(load_picker_context(), max_models=50)
            for p in payload.get("providers", []) or []:
                for name in p.get("models", []) or []:
                    _add(str(p.get("slug") or ""), str(name))
        except Exception:  # noqa: BLE001
            pass

    return {"models": out, "current": current}


@app.websocket("/api/chat")
async def chat_ws(ws: WebSocket) -> None:
    """ChatGPT-style chat over WebSocket. Each message runs one agent turn and
    streams the reply token-by-token. Independent of the embedded TUI/PTY."""
    import asyncio
    import threading
    from starlette.websockets import WebSocketDisconnect

    token = ws.query_params.get("token", "")
    if not hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode()):
        await ws.close(code=4401)
        return
    if not _ws_client_is_allowed(ws):
        await ws.close(code=4403)
        return
    await ws.accept()

    loop = asyncio.get_event_loop()
    agent = None
    built_model = None  # qual modelo o agente atual foi construído
    built_profile = None  # qual agente (profile) o chat está encarnando
    history: List[Dict[str, Any]] = []

    try:
        while True:
            data = await ws.receive_json()
            message = (data.get("message") or "").strip()
            if not message:
                continue

            model = (data.get("model") or "").strip() or None
            provider = (data.get("provider") or "").strip() or None
            agent_profile = (data.get("agent") or "").strip() or None

            # (Re)constrói o agente na primeira mensagem ou quando o modelo
            # ou o agente (profile) selecionado muda. Trocar de agente zera o
            # histórico — cada persona conversa do seu próprio contexto.
            if agent is None or model != built_model or agent_profile != built_profile:
                await ws.send_json({"type": "status", "text": "Carregando agente…"})
                try:
                    agent = await loop.run_in_executor(
                        None, lambda: _build_chat_agent(model, provider, agent_profile)
                    )
                    if agent_profile != built_profile:
                        history = []
                    built_model = model
                    built_profile = agent_profile
                except Exception as exc:  # noqa: BLE001
                    _log.exception("chat_ws: build agent failed")
                    await ws.send_json({"type": "error", "text": f"Falha ao iniciar o agente: {exc}"})
                    continue

            queue: "asyncio.Queue[dict]" = asyncio.Queue()

            def on_delta(delta) -> None:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, {"delta": str(delta)})
                except Exception:  # noqa: BLE001
                    pass

            def run_turn() -> None:
                from mangaba_agent.mangaba_constants import (
                    reset_skills_dir_override,
                    set_skills_dir_override,
                )

                # Skills do profile encarnado — override context-local só
                # nesta thread de turno; não vaza para outros chats/threads.
                skills_override = getattr(agent, "_mangaba_skills_override", None)
                token = (
                    set_skills_dir_override(skills_override)
                    if skills_override is not None
                    else None
                )
                try:
                    result = agent.run_conversation(
                        message,
                        conversation_history=list(history),
                        stream_callback=on_delta,
                    )
                    final = (
                        result.get("final_response", "")
                        if isinstance(result, dict)
                        else str(result)
                    )
                    loop.call_soon_threadsafe(queue.put_nowait, {"done": final})
                except Exception as exc:  # noqa: BLE001
                    loop.call_soon_threadsafe(queue.put_nowait, {"error": str(exc)})
                finally:
                    if token is not None:
                        reset_skills_dir_override(token)

            threading.Thread(target=run_turn, daemon=True).start()

            while True:
                item = await queue.get()
                if "delta" in item:
                    await ws.send_json({"type": "delta", "text": item["delta"]})
                elif "done" in item:
                    final = item["done"]
                    history.append({"role": "user", "content": message})
                    history.append({"role": "assistant", "content": final})
                    await ws.send_json({"type": "done", "text": final})
                    break
                else:  # error
                    await ws.send_json({"type": "error", "text": item.get("error", "erro desconhecido")})
                    break
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        _log.exception("chat_ws failed")
        try:
            await ws.close(code=1011)
        except Exception:  # noqa: BLE001
            pass


@app.websocket("/api/pty")
async def pty_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    # --- auth + loopback check (before accept so we can close cleanly) ---
    token = ws.query_params.get("token", "")
    expected = _SESSION_TOKEN
    if not hmac.compare_digest(token.encode(), expected.encode()):
        await ws.close(code=4401)
        return

    if not _ws_client_is_allowed(ws):
        await ws.close(code=4403)
        return

    await ws.accept()

    # On native Windows, the POSIX PTY bridge can't be imported.  Tell the
    # client and close cleanly rather than pretending the feature works.
    if not _PTY_BRIDGE_AVAILABLE:
        await ws.send_text(
            "\r\n\x1b[31mChat unavailable: the embedded terminal requires a "
            "POSIX PTY, which native Windows Python doesn't provide.\x1b[0m\r\n"
            "\x1b[33mInstall Mangaba inside WSL2 to use the dashboard's /chat "
            "tab — the rest of the dashboard works here.\x1b[0m\r\n"
        )
        await ws.close(code=1011)
        return

    # --- spawn PTY ------------------------------------------------------
    resume = ws.query_params.get("resume") or None
    channel = _channel_or_close_code(ws)
    sidecar_url = _build_sidecar_url(channel) if channel else None

    try:
        argv, cwd, env = _resolve_chat_argv(resume=resume, sidecar_url=sidecar_url)
    except SystemExit as exc:
        # _make_tui_argv calls sys.exit(1) when node/npm is missing.
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return


    try:
        bridge = PtyBridge.spawn(argv, cwd=cwd, env=env)
    except PtyUnavailableError as exc:
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return
    except (FileNotFoundError, OSError) as exc:
        await ws.send_text(f"\r\n\x1b[31mChat failed to start: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return

    loop = asyncio.get_running_loop()

    # --- reader task: PTY master → WebSocket ----------------------------
    async def pump_pty_to_ws() -> None:
        while True:
            chunk = await loop.run_in_executor(
                None, bridge.read, _PTY_READ_CHUNK_TIMEOUT
            )
            if chunk is None:  # EOF
                return
            if not chunk:  # no data this tick; yield control and retry
                await asyncio.sleep(0)
                continue
            try:
                await ws.send_bytes(chunk)
            except Exception:
                return

    reader_task = asyncio.create_task(pump_pty_to_ws())

    # --- writer loop: WebSocket → PTY master ----------------------------
    try:
        while True:
            msg = await ws.receive()
            msg_type = msg.get("type")
            if msg_type == "websocket.disconnect":
                break
            raw = msg.get("bytes")
            if raw is None:
                text = msg.get("text")
                raw = text.encode("utf-8") if isinstance(text, str) else b""
            if not raw:
                continue

            # Resize escape is consumed locally, never written to the PTY.
            match = _RESIZE_RE.match(raw)
            if match and match.end() == len(raw):
                cols = int(match.group(1))
                rows = int(match.group(2))
                bridge.resize(cols=cols, rows=rows)
                continue

            bridge.write(raw)
    except WebSocketDisconnect:
        pass
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass
        bridge.close()


# ---------------------------------------------------------------------------
# /api/ws — JSON-RPC WebSocket sidecar for the dashboard "Chat" tab.
#
# Drives the same `tui_gateway.dispatch` surface Ink uses over stdio, so the
# dashboard can render structured metadata (model badge, tool-call sidebar,
# slash launcher, session info) alongside the xterm.js terminal that PTY
# already paints. Both transports bind to the same session id when one is
# active, so a tool.start emitted by the agent fans out to both sinks.
# ---------------------------------------------------------------------------


@app.websocket("/api/ws")
async def gateway_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    token = ws.query_params.get("token", "")
    if not hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode()):
        await ws.close(code=4401)
        return

    if not _ws_client_is_allowed(ws):
        await ws.close(code=4403)
        return

    from tui_gateway.ws import handle_ws

    await handle_ws(ws)


# ---------------------------------------------------------------------------
# /api/pub + /api/events — chat-tab event broadcast.
#
# The PTY-side ``tui_gateway.entry`` opens /api/pub at startup (driven by
# MANGABA_TUI_SIDECAR_URL set in /api/pty's PTY env) and writes every
# dispatcher emit through it.  The dashboard fans those frames out to any
# subscriber that opened /api/events on the same channel id.  This is what
# gives the React sidebar its tool-call feed without breaking the PTY
# child's stdio handshake with Ink.
# ---------------------------------------------------------------------------


@app.websocket("/api/pub")
async def pub_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    token = ws.query_params.get("token", "")
    if not hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode()):
        await ws.close(code=4401)
        return

    if not _ws_client_is_allowed(ws):
        await ws.close(code=4403)
        return

    channel = _channel_or_close_code(ws)
    if not channel:
        await ws.close(code=4400)
        return

    await ws.accept()

    try:
        while True:
            await _broadcast_event(channel, await ws.receive_text())
    except WebSocketDisconnect:
        pass


@app.websocket("/api/events")
async def events_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    token = ws.query_params.get("token", "")
    if not hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode()):
        await ws.close(code=4401)
        return

    if not _ws_client_is_allowed(ws):
        await ws.close(code=4403)
        return

    channel = _channel_or_close_code(ws)
    if not channel:
        await ws.close(code=4400)
        return

    await ws.accept()

    async with _event_lock:
        _event_channels.setdefault(channel, set()).add(ws)

    try:
        while True:
            # Subscribers don't speak — the receive() just blocks until
            # disconnect so the connection stays open as long as the
            # browser holds it.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with _event_lock:
            subs = _event_channels.get(channel)

            if subs is not None:
                subs.discard(ws)

                if not subs:
                    _event_channels.pop(channel, None)


def _normalise_prefix(raw: Optional[str]) -> str:
    """Normalise an X-Forwarded-Prefix header value.

    Returns a string like ``"/mangaba"`` (no trailing slash) or ``""`` when
    no prefix is set / the header is malformed. We deliberately reject
    anything containing ``..`` or non-printable bytes so a hostile proxy
    can't inject HTML via the prefix.
    """
    if not raw:
        return ""
    p = raw.strip()
    if not p:
        return ""
    if not p.startswith("/"):
        p = "/" + p
    p = p.rstrip("/")
    if "//" in p or ".." in p or any(c in p for c in ('"', "'", "<", ">", " ", "\n", "\r", "\t")):
        return ""
    if len(p) > 64:
        return ""
    return p


def mount_spa(application: FastAPI):
    """Mount the built SPA. Falls back to index.html for client-side routing.

    The session token is injected into index.html via a ``<script>`` tag so
    the SPA can authenticate against protected API endpoints without a
    separate (unauthenticated) token-dispensing endpoint.

    When served behind a path-prefix reverse proxy (e.g.
    ``mission-control.tilos.com/mangaba/*`` -> local Caddy -> :9119), the
    proxy injects ``X-Forwarded-Prefix: /mangaba`` on every request. We
    rewrite the served ``index.html`` so absolute asset URLs (``/assets/...``)
    and the SPA's runtime ``__MANGABA_BASE_PATH__`` honour that prefix
    without rebuilding the bundle.
    """
    if not WEB_DIST.exists():
        @application.get("/{full_path:path}")
        async def no_frontend(full_path: str):
            return JSONResponse(
                {"error": "Frontend not built. Run: cd web && npm run build"},
                status_code=404,
            )
        return

    _index_path = WEB_DIST / "index.html"

    def _serve_index(prefix: str = ""):
        """Return index.html with the session token + base-path injected.

        ``prefix`` is the normalised ``X-Forwarded-Prefix`` (e.g. ``/mangaba``)
        or empty string when served at root.
        """
        html = _index_path.read_text()
        chat_js = "true" if _DASHBOARD_EMBEDDED_CHAT_ENABLED else "false"
        token_script = (
            f'<script>window.__MANGABA_SESSION_TOKEN__="{_SESSION_TOKEN}";'
            f"window.__MANGABA_DASHBOARD_EMBEDDED_CHAT__={chat_js};"
            f'window.__MANGABA_BASE_PATH__="{prefix}";</script>'
        )
        if prefix:
            # Rewrite absolute asset URLs baked into the Vite build so the
            # browser fetches them through the same proxy prefix.
            html = html.replace('href="/assets/', f'href="{prefix}/assets/')
            html = html.replace('src="/assets/', f'src="{prefix}/assets/')
            html = html.replace('href="/favicon.ico"', f'href="{prefix}/favicon.ico"')
            html = html.replace('href="/fonts/', f'href="{prefix}/fonts/')
            html = html.replace('href="/ds-assets/', f'href="{prefix}/ds-assets/')
            html = html.replace('src="/ds-assets/', f'src="{prefix}/ds-assets/')
        html = html.replace("</head>", f"{token_script}</head>", 1)
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    # When served behind a path-prefix proxy, the built CSS contains
    # absolute ``url(/fonts/...)`` and ``url(/ds-assets/...)`` references.
    # Browsers resolve those against the document origin, which means
    # under ``/mangaba`` they'd hit ``mission-control.tilos.com/fonts/...``
    # (the MC Pages app), not the Mangaba backend. Intercept CSS asset
    # requests BEFORE the StaticFiles mount and rewrite the absolute paths
    # when a prefix is in play.
    @application.get("/assets/{filename}.css")
    async def serve_css(filename: str, request: Request):
        css_path = WEB_DIST / "assets" / f"{filename}.css"
        if not css_path.is_file() or not css_path.resolve().is_relative_to(
            WEB_DIST.resolve()
        ):
            return JSONResponse({"error": "not found"}, status_code=404)
        prefix = _normalise_prefix(request.headers.get("x-forwarded-prefix"))
        css = css_path.read_text()
        if prefix:
            for asset_dir in ("/fonts/", "/fonts-terminal/", "/ds-assets/", "/assets/"):
                css = css.replace(f"url({asset_dir}", f"url({prefix}{asset_dir}")
                css = css.replace(f"url(\"{asset_dir}", f"url(\"{prefix}{asset_dir}")
                css = css.replace(f"url('{asset_dir}", f"url('{prefix}{asset_dir}")
        return Response(content=css, media_type="text/css")

    application.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @application.get("/{full_path:path}")
    async def serve_spa(full_path: str, request: Request):
        prefix = _normalise_prefix(request.headers.get("x-forwarded-prefix"))
        file_path = WEB_DIST / full_path
        # Prevent path traversal via url-encoded sequences (%2e%2e/)
        if (
            full_path
            and file_path.resolve().is_relative_to(WEB_DIST.resolve())
            and file_path.exists()
            and file_path.is_file()
        ):
            return FileResponse(file_path)
        return _serve_index(prefix)


# ---------------------------------------------------------------------------
# Dashboard theme endpoints
# ---------------------------------------------------------------------------

# Built-in dashboard themes — label + description only.  The actual color
# definitions live in the frontend (web/src/themes/presets.ts).
_BUILTIN_DASHBOARD_THEMES = [
    {"name": "default",       "label": "Mangaba Noite",          "description": "Modo escuro — grafite quente com laranja da marca"},
    {"name": "default-large", "label": "Mangaba Noite (Grande)", "description": "Mangaba Noite com fontes maiores e espaçamento confortável"},
    {"name": "claude",    "label": "Claude AI",      "description": "Anthropic Claude — warm coral & cream on deep brown"},
    {"name": "midnight",      "label": "Midnight",            "description": "Deep blue-violet with cool accents"},
    {"name": "ember",     "label": "Ember",          "description": "Warm crimson and bronze — forge vibes"},
    {"name": "mono",      "label": "Mono",           "description": "Clean grayscale — minimal and focused"},
    {"name": "cyberpunk", "label": "Cyberpunk",      "description": "Neon green on black — matrix terminal"},
    {"name": "rose",      "label": "Rosé",           "description": "Soft pink and warm ivory — easy on the eyes"},
]


def _parse_theme_layer(value: Any, default_hex: str, default_alpha: float = 1.0) -> Optional[Dict[str, Any]]:
    """Normalise a theme layer spec from YAML into `{hex, alpha}` form.

    Accepts shorthand (a bare hex string) or full dict form.  Returns
    ``None`` on garbage input so the caller can fall back to a built-in
    default rather than blowing up.
    """
    if value is None:
        return {"hex": default_hex, "alpha": default_alpha}
    if isinstance(value, str):
        return {"hex": value, "alpha": default_alpha}
    if isinstance(value, dict):
        hex_val = value.get("hex", default_hex)
        alpha_val = value.get("alpha", default_alpha)
        if not isinstance(hex_val, str):
            return None
        try:
            alpha_f = float(alpha_val)
        except (TypeError, ValueError):
            alpha_f = default_alpha
        return {"hex": hex_val, "alpha": max(0.0, min(1.0, alpha_f))}
    return None


_THEME_DEFAULT_TYPOGRAPHY: Dict[str, str] = {
    "fontSans": 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    "fontMono": 'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace',
    "baseSize": "15px",
    "lineHeight": "1.55",
    "letterSpacing": "0",
}

_THEME_DEFAULT_LAYOUT: Dict[str, str] = {
    "radius": "0.5rem",
    "density": "comfortable",
}

_THEME_OVERRIDE_KEYS = {
    "card", "cardForeground", "popover", "popoverForeground",
    "primary", "primaryForeground", "secondary", "secondaryForeground",
    "muted", "mutedForeground", "accent", "accentForeground",
    "destructive", "destructiveForeground", "success", "warning",
    "border", "input", "ring",
}

# Well-known named asset slots themes can populate.  Any other keys under
# ``assets.custom`` are exposed as ``--theme-asset-custom-<key>`` CSS vars
# for plugin/shell use.
_THEME_NAMED_ASSET_KEYS = {"bg", "hero", "logo", "crest", "sidebar", "header"}

# Component-style buckets themes can override.  The value under each bucket
# is a mapping from camelCase property name to CSS string; each pair emits
# ``--component-<bucket>-<kebab-property>`` on :root.  The frontend's shell
# components (Card, App header, Backdrop, etc.) consume these vars so themes
# can restyle chrome (clip-path, border-image, segmented progress, etc.)
# without shipping their own CSS.
_THEME_COMPONENT_BUCKETS = {
    "card", "header", "footer", "sidebar", "tab",
    "progress", "badge", "backdrop", "page",
}

_THEME_LAYOUT_VARIANTS = {"standard", "cockpit", "tiled"}

# Cap on customCSS length so a malformed/oversized theme YAML can't blow up
# the response payload or the <style> tag.  32 KiB is plenty for every
# practical reskin (the Strike Freedom demo is ~2 KiB).
_THEME_CUSTOM_CSS_MAX = 32 * 1024


def _normalise_theme_definition(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalise a user theme YAML into the wire format `ThemeProvider`
    expects.  Returns ``None`` if the theme is unusable.

    Accepts both the full schema (palette/typography/layout) and a loose
    form with bare hex strings, so hand-written YAMLs stay friendly.
    """
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    # Palette
    palette_src = data.get("palette", {}) if isinstance(data.get("palette"), dict) else {}
    # Allow top-level `colors.background` as a shorthand too.
    colors_src = data.get("colors", {}) if isinstance(data.get("colors"), dict) else {}

    def _layer(key: str, default_hex: str, default_alpha: float = 1.0) -> Dict[str, Any]:
        spec = palette_src.get(key, colors_src.get(key))
        parsed = _parse_theme_layer(spec, default_hex, default_alpha)
        return parsed if parsed is not None else {"hex": default_hex, "alpha": default_alpha}

    palette = {
        "background": _layer("background", "#041c1c", 1.0),
        "midground": _layer("midground", "#ffe6cb", 1.0),
        "foreground": _layer("foreground", "#ffffff", 0.0),
        "warmGlow": palette_src.get("warmGlow") or data.get("warmGlow") or "rgba(255, 189, 56, 0.35)",
        "noiseOpacity": 1.0,
    }
    raw_noise = palette_src.get("noiseOpacity", data.get("noiseOpacity"))
    try:
        palette["noiseOpacity"] = float(raw_noise) if raw_noise is not None else 1.0
    except (TypeError, ValueError):
        palette["noiseOpacity"] = 1.0

    # Typography
    typo_src = data.get("typography", {}) if isinstance(data.get("typography"), dict) else {}
    typography = dict(_THEME_DEFAULT_TYPOGRAPHY)
    for key in ("fontSans", "fontMono", "fontDisplay", "fontUrl", "baseSize", "lineHeight", "letterSpacing"):
        val = typo_src.get(key)
        if isinstance(val, str) and val.strip():
            typography[key] = val

    # Layout
    layout_src = data.get("layout", {}) if isinstance(data.get("layout"), dict) else {}
    layout = dict(_THEME_DEFAULT_LAYOUT)
    radius = layout_src.get("radius")
    if isinstance(radius, str) and radius.strip():
        layout["radius"] = radius
    density = layout_src.get("density")
    if isinstance(density, str) and density in {"compact", "comfortable", "spacious"}:
        layout["density"] = density

    # Color overrides — keep only valid keys with string values.
    overrides_src = data.get("colorOverrides", {})
    color_overrides: Dict[str, str] = {}
    if isinstance(overrides_src, dict):
        for key, val in overrides_src.items():
            if key in _THEME_OVERRIDE_KEYS and isinstance(val, str) and val.strip():
                color_overrides[key] = val

    # Assets — named slots + arbitrary user-defined keys.  Values must be
    # strings (URLs or CSS ``url(...)``/``linear-gradient(...)`` expressions).
    # We don't fetch remote assets here; the frontend just injects them as
    # CSS vars.  Empty values are dropped so a theme can explicitly clear a
    # slot by setting ``hero: ""``.
    assets_out: Dict[str, Any] = {}
    assets_src = data.get("assets", {}) if isinstance(data.get("assets"), dict) else {}
    for key in _THEME_NAMED_ASSET_KEYS:
        val = assets_src.get(key)
        if isinstance(val, str) and val.strip():
            assets_out[key] = val
    custom_assets_src = assets_src.get("custom")
    if isinstance(custom_assets_src, dict):
        custom_assets: Dict[str, str] = {}
        for key, val in custom_assets_src.items():
            if (
                isinstance(key, str)
                and key.replace("-", "").replace("_", "").isalnum()
                and isinstance(val, str)
                and val.strip()
            ):
                custom_assets[key] = val
        if custom_assets:
            assets_out["custom"] = custom_assets

    # Custom CSS — raw CSS text the frontend injects as a scoped <style>
    # tag on theme apply.  Clipped to _THEME_CUSTOM_CSS_MAX to keep the
    # payload bounded.  We intentionally do NOT parse/sanitise the CSS
    # here — the dashboard is localhost-only and themes are user-authored
    # YAML in ~/.mangaba/, same trust level as the config file itself.
    custom_css_val = data.get("customCSS")
    custom_css: Optional[str] = None
    if isinstance(custom_css_val, str) and custom_css_val.strip():
        custom_css = custom_css_val[:_THEME_CUSTOM_CSS_MAX]

    # Component style overrides — per-bucket dicts of camelCase CSS
    # property -> CSS string.  The frontend converts these into CSS vars
    # that shell components (Card, App header, Backdrop) consume.
    component_styles_src = data.get("componentStyles", {})
    component_styles: Dict[str, Dict[str, str]] = {}
    if isinstance(component_styles_src, dict):
        for bucket, props in component_styles_src.items():
            if bucket not in _THEME_COMPONENT_BUCKETS or not isinstance(props, dict):
                continue
            clean: Dict[str, str] = {}
            for prop, value in props.items():
                if (
                    isinstance(prop, str)
                    and prop.replace("-", "").replace("_", "").isalnum()
                    and isinstance(value, (str, int, float))
                    and str(value).strip()
                ):
                    clean[prop] = str(value)
            if clean:
                component_styles[bucket] = clean

    layout_variant_src = data.get("layoutVariant")
    layout_variant = (
        layout_variant_src
        if isinstance(layout_variant_src, str) and layout_variant_src in _THEME_LAYOUT_VARIANTS
        else "standard"
    )

    result: Dict[str, Any] = {
        "name": name,
        "label": data.get("label") or name,
        "description": data.get("description", ""),
        "palette": palette,
        "typography": typography,
        "layout": layout,
        "layoutVariant": layout_variant,
    }
    if color_overrides:
        result["colorOverrides"] = color_overrides
    if assets_out:
        result["assets"] = assets_out
    if custom_css is not None:
        result["customCSS"] = custom_css
    if component_styles:
        result["componentStyles"] = component_styles
    return result


def _discover_user_themes() -> list:
    """Scan ~/.mangaba/dashboard-themes/*.yaml for user-created themes.

    Returns a list of fully-normalised theme definitions ready to ship
    to the frontend, so the client can apply them without a secondary
    round-trip or a built-in stub.
    """
    themes_dir = get_mangaba_home() / "dashboard-themes"
    if not themes_dir.is_dir():
        return []
    result = []
    for f in sorted(themes_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        normalised = _normalise_theme_definition(data)
        if normalised is not None:
            result.append(normalised)
    return result


@app.get("/api/dashboard/themes")
async def get_dashboard_themes():
    """Return available themes and the currently active one.

    Built-in entries ship name/label/description only (the frontend owns
    their full definitions in `web/src/themes/presets.ts`).  User themes
    from `~/.mangaba/dashboard-themes/*.yaml` ship with their full
    normalised definition under `definition`, so the client can apply
    them without a stub.
    """
    config = load_config()
    active = cfg_get(config, "dashboard", "theme", default="claude")
    user_themes = _discover_user_themes()
    seen = set()
    themes = []
    for t in _BUILTIN_DASHBOARD_THEMES:
        seen.add(t["name"])
        themes.append(t)
    for t in user_themes:
        if t["name"] in seen:
            continue
        themes.append({
            "name": t["name"],
            "label": t["label"],
            "description": t["description"],
            "definition": t,
        })
        seen.add(t["name"])
    return {"themes": themes, "active": active}


class ThemeSetBody(BaseModel):
    name: str


@app.put("/api/dashboard/theme")
async def set_dashboard_theme(body: ThemeSetBody):
    """Set the active dashboard theme (persists to config.yaml)."""
    config = load_config()
    if "dashboard" not in config:
        config["dashboard"] = {}
    config["dashboard"]["theme"] = body.name
    save_config(config)
    return {"ok": True, "theme": body.name}


# ---------------------------------------------------------------------------
# Dashboard plugin system
# ---------------------------------------------------------------------------

def _safe_plugin_api_relpath(api_field: Any, *, dashboard_dir: Path) -> Optional[str]:
    """Validate the manifest's ``api`` field for the plugin loader.

    The web server later imports this file as a Python module via
    ``importlib.util.spec_from_file_location`` (arbitrary code
    execution by design — that's how plugins extend the backend).
    Pre-#29156 the field was used as-is, which meant:

    * An absolute path swallowed the plugin's dashboard directory
      entirely — ``Path('safe/dashboard') / '/tmp/evil.py'`` resolves
      to ``/tmp/evil.py``, so any attacker-controlled manifest could
      point the import at any Python file on disk (GHSA-5qr3-c538-wm9j).
    * A ``../..`` traversal could climb out of the plugin into
      neighbouring directories on the search path.

    Return the original string when the resolved path stays under
    ``dashboard_dir``; return ``None`` (with a warning logged at the
    call site) otherwise so the plugin still loads its static JS/CSS
    but its backend ``api`` is rejected.
    """
    if not isinstance(api_field, str) or not api_field.strip():
        return None
    candidate = Path(api_field)
    if candidate.is_absolute():
        return None
    try:
        resolved = (dashboard_dir / candidate).resolve()
        base = dashboard_dir.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(base)
    except ValueError:
        return None
    return api_field


def _discover_dashboard_plugins() -> list:
    """Scan plugins/*/dashboard/manifest.json for dashboard extensions.

    Checks three plugin sources (same as mangaba_cli.plugins):
    1. User plugins:    ~/.mangaba/plugins/<name>/dashboard/manifest.json
    2. Bundled plugins: <repo>/plugins/<name>/dashboard/manifest.json  (memory/, etc.)
    3. Project plugins: ./.mangaba/plugins/  (only if MANGABA_ENABLE_PROJECT_PLUGINS)
    """
    plugins = []
    seen_names: set = set()

    from mangaba_cli.plugins import get_bundled_plugins_dir
    bundled_root = get_bundled_plugins_dir()
    search_dirs = [
        (get_mangaba_home() / "plugins", "user"),
        (bundled_root / "memory", "bundled"),
        (bundled_root, "bundled"),
    ]
    # GHSA-5qr3-c538-wm9j (#29156): the previous ``os.environ.get(...)``
    # check treated *any* non-empty string as truthy, so ``=0``, ``=false``,
    # and ``=no`` — all of which the agent loader and operators correctly
    # read as "disabled" — silently *enabled* the untrusted project source
    # in the web server.  Combined with the absolute-path RCE primitive on
    # the manifest's ``api`` field (now patched below), this turned the
    # opt-in into a sticky always-on switch.  Use the shared truthy
    # semantics (``1`` / ``true`` / ``yes`` / ``on``) so the gate matches
    # ``mangaba_cli/plugins.py`` and the documented user contract.
    if env_var_enabled("MANGABA_ENABLE_PROJECT_PLUGINS"):
        search_dirs.append((Path.cwd() / ".mangaba" / "plugins", "project"))

    for plugins_root, source in search_dirs:
        if not plugins_root.is_dir():
            continue
        for child in sorted(plugins_root.iterdir()):
            if not child.is_dir():
                continue
            manifest_file = child / "dashboard" / "manifest.json"
            if not manifest_file.exists():
                continue
            try:
                data = json.loads(manifest_file.read_text(encoding="utf-8"))
                name = data.get("name", child.name)
                if name in seen_names:
                    continue
                seen_names.add(name)
                # Tab options: ``path`` + ``position`` for a new tab, optional
                # ``override`` to replace a built-in route, and ``hidden`` to
                # register the plugin component/slots without adding a tab
                # (useful for slot-only plugins like a header-crest injector).
                raw_tab = data.get("tab", {}) if isinstance(data.get("tab"), dict) else {}
                tab_info = {
                    "path": raw_tab.get("path", f"/{name}"),
                    "position": raw_tab.get("position", "end"),
                }
                override_path = raw_tab.get("override")
                if isinstance(override_path, str) and override_path.startswith("/"):
                    tab_info["override"] = override_path
                if bool(raw_tab.get("hidden")):
                    tab_info["hidden"] = True
                # Slots: list of named slot locations this plugin populates.
                # The frontend exposes ``registerSlot(pluginName, slotName, Component)``
                # on window; plugins with non-empty slots call it from their JS bundle.
                slots_src = data.get("slots")
                slots: List[str] = []
                if isinstance(slots_src, list):
                    slots = [s for s in slots_src if isinstance(s, str) and s]
                # Validate ``api`` at discovery time so the value cached
                # on the plugin entry is already safe to feed into the
                # importer.  An attacker-controlled manifest can name
                # any absolute path or ``..`` traversal here — the
                # web server then imports that file as a Python module
                # (RCE, GHSA-5qr3-c538-wm9j).
                raw_api = data.get("api")
                dashboard_dir = child / "dashboard"
                safe_api = _safe_plugin_api_relpath(raw_api, dashboard_dir=dashboard_dir)
                if raw_api and safe_api is None:
                    _log.warning(
                        "Plugin %s: refusing unsafe api path %r (must be a "
                        "relative file inside the plugin's dashboard/ "
                        "directory); backend routes from this plugin will "
                        "not be mounted",
                        name, raw_api,
                    )
                plugins.append({
                    "name": name,
                    "label": data.get("label", name),
                    "description": data.get("description", ""),
                    "icon": data.get("icon", "Puzzle"),
                    "version": data.get("version", "0.0.0"),
                    "tab": tab_info,
                    "slots": slots,
                    "entry": data.get("entry", "dist/index.js"),
                    "css": data.get("css"),
                    "has_api": bool(safe_api),
                    "source": source,
                    "_dir": str(dashboard_dir),
                    "_api_file": safe_api,
                })
            except Exception as exc:
                _log.warning("Bad dashboard plugin manifest %s: %s", manifest_file, exc)
                continue
    return plugins


# Cache discovered plugins per-process (refresh on explicit re-scan).
_dashboard_plugins_cache: Optional[list] = None


def _get_dashboard_plugins(force_rescan: bool = False) -> list:
    global _dashboard_plugins_cache
    if _dashboard_plugins_cache is None or force_rescan:
        _dashboard_plugins_cache = _discover_dashboard_plugins()
    elif _dashboard_plugins_cache:
        if any(not Path(p["_dir"]).is_dir() for p in _dashboard_plugins_cache):
            _dashboard_plugins_cache = _discover_dashboard_plugins()
    return _dashboard_plugins_cache


@app.get("/api/dashboard/plugins")
async def get_dashboard_plugins():
    """Return discovered dashboard plugins (excludes user-hidden ones)."""
    plugins = _get_dashboard_plugins()
    # Read user's hidden plugins list from config.
    config = load_config()
    hidden: list = cfg_get(config, "dashboard", "hidden_plugins", default=[]) or []
    # Strip internal fields before sending to frontend and filter out hidden.
    return [
        {k: v for k, v in p.items() if not k.startswith("_")}
        for p in plugins
        if p["name"] not in hidden
    ]


@app.get("/api/dashboard/plugins/rescan")
async def rescan_dashboard_plugins():
    """Force re-scan of dashboard plugins."""
    plugins = _get_dashboard_plugins(force_rescan=True)
    return {"ok": True, "count": len(plugins)}


class _AgentPluginInstallBody(BaseModel):
    identifier: str
    force: bool = False
    enable: bool = True


def _strip_dashboard_manifest(p: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in p.items() if not k.startswith("_")}


def _merged_plugins_hub() -> Dict[str, Any]:
    """Agent discovery + dashboard manifests + optional provider picker metadata."""
    from mangaba_cli.plugins_cmd import (
        _discover_all_plugins,
        _get_current_context_engine,
        _get_current_memory_provider,
        _discover_context_engines,
        _discover_memory_providers,
        _get_disabled_set,
        _get_enabled_set,
        _read_manifest as _read_plugin_manifest_at,
    )

    dashboard_list = _get_dashboard_plugins()
    dash_by_name = {str(p["name"]): p for p in dashboard_list}

    disabled_set = _get_disabled_set()
    enabled_set = _get_enabled_set()

    # Read user-hidden plugins from config for the user_hidden field.
    config = load_config()
    hidden_plugins: list = cfg_get(config, "dashboard", "hidden_plugins", default=[]) or []

    plugins_root_resolved = (get_mangaba_home() / "plugins").resolve()
    rows: List[Dict[str, Any]] = []

    for name, version, description, source, dir_str in _discover_all_plugins():
        if name in disabled_set:
            runtime_status = "disabled"
        elif name in enabled_set:
            runtime_status = "enabled"
        else:
            runtime_status = "inactive"

        dir_path = Path(dir_str)
        dm = dash_by_name.get(name)
        has_dash_manifest = dm is not None or (dir_path / "dashboard" / "manifest.json").exists()

        under_user_tree = False
        try:
            dir_path.resolve().relative_to(plugins_root_resolved)
            under_user_tree = True
        except ValueError:
            pass

        can_remove_update = (
            source in {"user", "git"} and under_user_tree and Path(dir_str).is_dir()
        )

        # Check if this plugin provides tools that require auth
        auth_required = False
        auth_command = ""
        manifest_data = _read_plugin_manifest_at(dir_path)
        provides_tools = manifest_data.get("provides_tools") or []
        if provides_tools:
            try:
                from tools.registry import registry
                for tname in provides_tools:
                    entry = registry.get_entry(tname)
                    if entry and entry.check_fn and not entry.check_fn():
                        auth_required = True
                        auth_command = f"mangaba auth {name}"
                        break
            except Exception:
                pass

        rows.append({
            "name": name,
            "version": version or "",
            "description": description or "",
            "source": source,
            "runtime_status": runtime_status,
            "has_dashboard_manifest": has_dash_manifest,
            "dashboard_manifest": _strip_dashboard_manifest(dm) if dm else None,
            "path": dir_str,
            "can_remove": can_remove_update,
            "can_update_git": can_remove_update and (Path(dir_str) / ".git").exists(),
            "auth_required": auth_required,
            "auth_command": auth_command,
            "user_hidden": name in hidden_plugins,
        })

    agent_names = {r["name"] for r in rows}
    orphan_dashboard = [
        _strip_dashboard_manifest(p)
        for p in dashboard_list
        if str(p["name"]) not in agent_names
    ]

    memory_providers: List[Dict[str, str]] = []
    try:
        for n, desc in _discover_memory_providers():
            memory_providers.append({"name": n, "description": desc})
    except Exception:
        memory_providers = []

    context_engines: List[Dict[str, str]] = []
    try:
        for n, desc in _discover_context_engines():
            context_engines.append({"name": n, "description": desc})
    except Exception:
        context_engines = []

    return {
        "plugins": rows,
        "orphan_dashboard_plugins": orphan_dashboard,
        "providers": {
            "memory_provider": _get_current_memory_provider() or "",
            "memory_options": memory_providers,
            "context_engine": _get_current_context_engine(),
            "context_options": context_engines,
        },
    }


@app.get("/api/dashboard/plugins/hub")
async def get_plugins_hub(request: Request):
    """Unified agent plugins + dashboard extension metadata (session protected)."""
    _require_token(request)
    try:
        return _merged_plugins_hub()
    except Exception as exc:
        _log.warning("plugins/hub failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to build plugins hub.") from exc


@app.post("/api/dashboard/agent-plugins/install")
async def post_agent_plugin_install(request: Request, body: _AgentPluginInstallBody):
    _require_token(request)
    from mangaba_cli.plugins_cmd import dashboard_install_plugin

    result = dashboard_install_plugin(
        body.identifier.strip(),
        force=body.force,
        enable=body.enable,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Install failed.",
        )
    _get_dashboard_plugins(force_rescan=True)
    # Strip internal paths from the response
    result.pop("after_install_path", None)
    return result


def _validate_plugin_name(name: str) -> str:
    """Reject path-traversal attempts in plugin name URL parameters."""
    name = name.strip("/")
    if not name or ".." in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Invalid plugin name.")
    return name


@app.post("/api/dashboard/agent-plugins/{name:path}/enable")
async def post_agent_plugin_enable(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from mangaba_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

    result = dashboard_set_agent_plugin_enabled(name, enabled=True)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Enable failed.")
    return result


@app.post("/api/dashboard/agent-plugins/{name:path}/disable")
async def post_agent_plugin_disable(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from mangaba_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

    result = dashboard_set_agent_plugin_enabled(name, enabled=False)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Disable failed.")
    return result


@app.post("/api/dashboard/agent-plugins/{name:path}/update")
async def post_agent_plugin_update(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from mangaba_cli.plugins_cmd import dashboard_update_user_plugin

    result = dashboard_update_user_plugin(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Update failed.")
    _get_dashboard_plugins(force_rescan=True)
    return result


@app.delete("/api/dashboard/agent-plugins/{name:path}")
async def delete_agent_plugin(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from mangaba_cli.plugins_cmd import dashboard_remove_user_plugin

    result = dashboard_remove_user_plugin(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Remove failed.")
    _get_dashboard_plugins(force_rescan=True)
    return result


class _PluginProvidersPutBody(BaseModel):
    memory_provider: Optional[str] = None
    context_engine: Optional[str] = None


@app.put("/api/dashboard/plugin-providers")
async def put_plugin_providers(request: Request, body: _PluginProvidersPutBody):
    """Persist memory provider / context engine selection (writes config.yaml)."""
    _require_token(request)
    from mangaba_cli.plugins_cmd import (
        _save_context_engine,
        _save_memory_provider,
    )

    if body.memory_provider is not None:
        _save_memory_provider(body.memory_provider)
    if body.context_engine is not None:
        _save_context_engine(body.context_engine)
    return {"ok": True}


class _PluginVisibilityBody(BaseModel):
    hidden: bool


@app.post("/api/dashboard/plugins/{name:path}/visibility")
async def post_plugin_visibility(request: Request, name: str, body: _PluginVisibilityBody):
    """Toggle a plugin's sidebar visibility (persists to config.yaml dashboard.hidden_plugins)."""
    _require_token(request)
    name = _validate_plugin_name(name)

    config = load_config()
    if "dashboard" not in config or not isinstance(config.get("dashboard"), dict):
        config["dashboard"] = {}
    hidden_list: list = config["dashboard"].get("hidden_plugins") or []
    if not isinstance(hidden_list, list):
        hidden_list = []

    if body.hidden and name not in hidden_list:
        hidden_list.append(name)
    elif not body.hidden and name in hidden_list:
        hidden_list.remove(name)

    config["dashboard"]["hidden_plugins"] = hidden_list
    save_config(config)
    return {"ok": True, "name": name, "hidden": body.hidden}


@app.get("/dashboard-plugins/{plugin_name}/{file_path:path}")
async def serve_plugin_asset(plugin_name: str, file_path: str):
    """Serve static assets from a dashboard plugin directory.

    Only serves files from the plugin's ``dashboard/`` subdirectory.
    Path traversal is blocked by checking ``resolve().is_relative_to()``.
    """
    plugins = _get_dashboard_plugins()
    plugin = next((p for p in plugins if p["name"] == plugin_name), None)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    base = Path(plugin["_dir"])
    target = (base / file_path).resolve()

    if not target.is_relative_to(base.resolve()):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Guess content type
    suffix = target.suffix.lower()
    content_types = {
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".html": "text/html",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
    }
    media_type = content_types.get(suffix, "application/octet-stream")
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


def _mount_plugin_api_routes():
    """Import and mount backend API routes from plugins that declare them.

    Each plugin's ``api`` field points to a Python file that must expose
    a ``router`` (FastAPI APIRouter).  Routes are mounted under
    ``/api/plugins/<name>/``.

    Backend import is restricted to ``bundled`` and ``user`` sources.
    Project plugins (``./.mangaba/plugins/``) ship with the CWD and are
    therefore attacker-controlled in any threat model where the user
    opens a malicious repo; they can extend the dashboard UI via
    static JS/CSS but their Python ``api`` file is never auto-imported
    by the web server.  See GHSA-5qr3-c538-wm9j (#29156).
    """
    for plugin in _get_dashboard_plugins():
        api_file_name = plugin.get("_api_file")
        if not api_file_name:
            continue
        if plugin.get("source") == "project":
            _log.warning(
                "Plugin %s: ignoring backend api=%s (project plugins may "
                "not auto-import Python code; move the plugin to "
                "~/.mangaba/plugins/ if you trust it)",
                plugin["name"], api_file_name,
            )
            continue
        dashboard_dir = Path(plugin["_dir"])
        api_path = dashboard_dir / api_file_name
        try:
            resolved_api = api_path.resolve()
            resolved_base = dashboard_dir.resolve()
            resolved_api.relative_to(resolved_base)
        except (OSError, RuntimeError, ValueError):
            # Discovery already filters this, but re-check here in case
            # ``_dir`` was tampered with after caching or a future caller
            # bypasses the validator.  Defence in depth keeps the import
            # primitive contained even if the upstream check regresses.
            _log.warning(
                "Plugin %s: refusing to import api file outside its "
                "dashboard directory (%s)", plugin["name"], api_path,
            )
            continue
        if not api_path.exists():
            _log.warning("Plugin %s declares api=%s but file not found", plugin["name"], api_file_name)
            continue
        try:
            module_name = f"mangaba_dashboard_plugin_{plugin['name']}"
            spec = importlib.util.spec_from_file_location(module_name, api_path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            # Register in sys.modules BEFORE exec_module so pydantic/FastAPI
            # can resolve forward references (e.g. models defined in a file
            # that uses `from __future__ import annotations`). Without this,
            # TypeAdapter lazy-build fails at first request with
            # "is not fully defined" because the module namespace isn't
            # reachable by name for string-annotation resolution.
            sys.modules[module_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            router = getattr(mod, "router", None)
            if router is None:
                _log.warning("Plugin %s api file has no 'router' attribute", plugin["name"])
                continue
            app.include_router(router, prefix=f"/api/plugins/{plugin['name']}")
            _log.info("Mounted plugin API routes: /api/plugins/%s/", plugin["name"])
        except Exception as exc:
            _log.warning("Failed to load plugin %s API routes: %s", plugin["name"], exc)


# Mount plugin API routes before the SPA catch-all.
_mount_plugin_api_routes()

mount_spa(app)


def start_server(
    host: str = "127.0.0.1",
    port: int = 9119,
    open_browser: bool = True,
    allow_public: bool = False,
    *,
    embedded_chat: bool = False,
):
    """Start the web UI server."""
    import uvicorn

    global _DASHBOARD_EMBEDDED_CHAT_ENABLED
    _DASHBOARD_EMBEDDED_CHAT_ENABLED = embedded_chat

    _LOCALHOST = ("127.0.0.1", "localhost", "::1")
    if host not in _LOCALHOST and not allow_public:
        raise SystemExit(
            f"Refusing to bind to {host} — the dashboard exposes API keys "
            f"and config without robust authentication.\n"
            f"Use --insecure to override (NOT recommended on untrusted networks)."
        )
    if host not in _LOCALHOST:
        _log.warning(
            "Binding to %s with --insecure — the dashboard has no robust "
            "authentication. Only use on trusted networks.", host,
        )

    # Record the bound host so host_header_middleware can validate incoming
    # Host headers against it. Defends against DNS rebinding (GHSA-ppp5-vxwm-4cf7).
    # bound_port is also stashed so /api/pty can build the back-WS URL the
    # PTY child uses to publish events to the dashboard sidebar.
    app.state.bound_host = host
    app.state.bound_port = port

    if open_browser:
        import webbrowser

        # On headless Linux (no DISPLAY or WAYLAND_DISPLAY) some registered
        # browsers are TUI programs (links, lynx, www-browser) that try to
        # take over the terminal.  That can send SIGHUP to the server process
        # and cause an immediate exit even though uvicorn bound successfully.
        # Skip the auto-open attempt on headless systems and let the user
        # open the URL manually.  macOS and Windows are always considered
        # display-capable.
        _has_display = (
            sys.platform != "linux"
            or bool(os.environ.get("DISPLAY"))
            or bool(os.environ.get("WAYLAND_DISPLAY"))
        )

        if _has_display:
            def _open():
                try:
                    time.sleep(1.0)
                    webbrowser.open(f"http://{host}:{port}")
                except Exception:
                    pass

            threading.Thread(target=_open, daemon=True).start()
        else:
            _log.debug(
                "Skipping browser-open: no DISPLAY or WAYLAND_DISPLAY detected "
                "(headless Linux). Pass --no-open to suppress this detection."
            )

    print(f"  Mangaba Web UI → http://{host}:{port}")
    # proxy_headers=False so _ws_client_is_allowed sees the real connection peer
    # rather than X-Forwarded-For's rewritten value (which would defeat the
    # loopback gate when behind a reverse proxy).
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=False)
