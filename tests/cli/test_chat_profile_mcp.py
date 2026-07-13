"""Tests for per-profile MCP config resolution in the dashboard chat."""

import os

from mangaba_cli.web_server import _chat_profile_mcp_servers, _parse_profile_dotenv


def _write_profile(tmp_path, config_text, dotenv_text=""):
    (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")
    if dotenv_text:
        (tmp_path / ".env").write_text(dotenv_text, encoding="utf-8")
    return tmp_path


def test_reads_servers_and_interpolates_profile_dotenv(tmp_path):
    prof = _write_profile(
        tmp_path,
        "mcp_servers:\n"
        "  tempo:\n"
        "    command: uvx\n"
        "    args: ['mcp-server-time']\n"
        "    env:\n"
        "      TZ: ${TZ_DO_PROFILE}\n",
        "TZ_DO_PROFILE=America/Maceio\n",
    )
    servers = _chat_profile_mcp_servers(prof)
    assert servers["tempo"]["env"]["TZ"] == "America/Maceio"
    # Interpolação não exporta o .env do profile para o processo.
    assert "TZ_DO_PROFILE" not in os.environ


def test_disabled_servers_are_filtered(tmp_path):
    prof = _write_profile(
        tmp_path,
        "mcp_servers:\n"
        "  ligado:\n    command: x\n"
        "  desligado:\n    command: y\n    enabled: false\n",
    )
    assert sorted(_chat_profile_mcp_servers(prof)) == ["ligado"]


def test_unknown_var_kept_verbatim_and_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("SO_NO_PROCESSO", "valor-do-processo")
    prof = _write_profile(
        tmp_path,
        "mcp_servers:\n"
        "  s:\n"
        "    command: x\n"
        "    env:\n"
        "      A: ${SO_NO_PROCESSO}\n"
        "      B: ${NAO_EXISTE}\n",
    )
    env = _chat_profile_mcp_servers(prof)["s"]["env"]
    assert env["A"] == "valor-do-processo"
    assert env["B"] == "${NAO_EXISTE}"


def test_missing_or_invalid_config_returns_empty(tmp_path):
    assert _chat_profile_mcp_servers(tmp_path) == {}
    (tmp_path / "config.yaml").write_text("mcp_servers: 'não é dict'\n")
    assert _chat_profile_mcp_servers(tmp_path) == {}


def test_parse_profile_dotenv_ignores_comments_and_quotes(tmp_path):
    (tmp_path / ".env").write_text('# comentário\nCHAVE="com aspas"\nVAZIO\n')
    parsed = _parse_profile_dotenv(tmp_path)
    assert parsed == {"CHAVE": "com aspas"}
