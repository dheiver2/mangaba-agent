import { useCallback, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Radio,
  RefreshCw,
  RotateCw,
  FileText,
  Megaphone,
  X,
  ChevronDown,
  ChevronRight,
  Settings,
} from "lucide-react";
import { Button } from "@dheiver2/ui/ui/components/button";
import { Badge } from "@dheiver2/ui/ui/components/badge";
import { Spinner } from "@dheiver2/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Toast } from "@/components/Toast";
import { StatusDot } from "@/components/StatusDot";
import { EmptyState } from "@/components/EmptyState";
import { useToast } from "@/hooks/useToast";
import { api } from "@/lib/api";
import type { FleetMember } from "@/lib/api";

interface PlatformInfo {
  platform: string;
  handle?: string;
  enabled: boolean;
  home_channel?: { chat_id: string; name?: string };
  has_token: boolean;
}

type FleetMemberWithPlatforms = FleetMember & { platforms?: PlatformInfo[] };

const PLATFORM_EMOJI: Record<string, string> = {
  telegram: "✈",
  discord: "🎮",
  slack: "💬",
  whatsapp: "📱",
  email: "📧",
  signal: "🔒",
};

function platformEmoji(platform: string): string {
  return PLATFORM_EMOJI[platform.toLowerCase()] ?? "🔌";
}

function ChannelsPanel({
  member,
  onClose,
}: {
  member: FleetMemberWithPlatforms;
  onClose: () => void;
}) {
  const platforms = member.platforms ?? [];
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[80vh] w-full max-w-lg flex-col rounded-lg bg-background shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b p-3">
          <span className="font-medium">
            Canais · {member.name}
          </span>
          <Button ghost size="sm" onClick={onClose}>
            <X className="h-4 w-4" />
          </Button>
        </div>
        <div className="overflow-auto p-4">
          {platforms.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Nenhum canal configurado para este profile.
            </p>
          ) : (
            <div className="flex flex-col gap-3">
              {platforms.map((p) => (
                <div
                  key={p.platform}
                  className="flex items-start gap-3 border border-border p-3"
                >
                  <span className="text-lg shrink-0">
                    {platformEmoji(p.platform)}
                  </span>
                  <div className="flex min-w-0 flex-1 flex-col gap-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium capitalize">
                        {p.platform}
                      </span>
                      {p.handle && (
                        <a
                          href={`https://t.me/${p.handle.replace(/^@/, "")}`}
                          target="_blank"
                          rel="noreferrer"
                          className="font-mono text-xs text-primary hover:underline"
                          title="Abrir o bot no Telegram"
                        >
                          {p.handle}
                        </a>
                      )}
                      {p.has_token ? (
                        <Badge tone="success" className="text-xs">
                          token ✓
                        </Badge>
                      ) : (
                        <Badge tone="warning" className="text-xs">
                          sem token ⚠
                        </Badge>
                      )}
                      <Badge
                        tone={p.enabled ? "success" : "secondary"}
                        className="text-xs"
                      >
                        {p.enabled ? "ativo" : "inativo"}
                      </Badge>
                    </div>
                    {p.home_channel && (
                      <p className="text-xs text-muted-foreground font-mono-ui">
                        {p.home_channel.name
                          ? `${p.home_channel.name} (${p.home_channel.chat_id})`
                          : p.home_channel.chat_id}
                      </p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="mt-4 pt-3 border-t border-border">
            <p className="text-xs text-muted-foreground">
              Para configurar canais, acesse a aba{" "}
              <strong>Configuração</strong> do profile{" "}
              <code className="font-mono-ui">{member.name}</code>.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function FleetPage() {
  const [busy, setBusy] = useState<string | null>(null);
  const [logsFor, setLogsFor] = useState<string | null>(null);
  const [logText, setLogText] = useState("");
  const [broadcast, setBroadcast] = useState("");
  const [channelsFor, setChannelsFor] = useState<FleetMemberWithPlatforms | null>(null);
  const [expandedChannels, setExpandedChannels] = useState<Set<string>>(new Set());
  const { toast, showToast } = useToast();

  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["fleet"],
    queryFn: () =>
      api.getFleet() as Promise<{ members: FleetMemberWithPlatforms[] }>,
    refetchInterval: 15_000,
  });
  const members = data?.members ?? [];
  const loading = isLoading;
  const load = useCallback(() => {
    refetch();
  }, [refetch]);

  useEffect(() => {
    if (error) showToast(`Erro ao carregar a frota: ${(error as Error).message}`, "error");
  }, [error, showToast]);

  const act = async (name: string, action: "restart" | "start" | "stop") => {
    setBusy(`${name}:${action}`);
    try {
      const res = await api.fleetAction(name, action);
      showToast(res.message, res.ok ? "success" : "error");
      setTimeout(load, 1500);
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  const openLogs = async (name: string) => {
    setLogsFor(name);
    setLogText("Carregando…");
    try {
      const res = await api.getFleetLogs(name, 80);
      setLogText(res.log || "(vazio)");
    } catch (e) {
      setLogText(`Erro: ${(e as Error).message}`);
    }
  };

  const sendBroadcast = async () => {
    const msg = broadcast.trim();
    if (!msg) return;
    setBusy("broadcast");
    try {
      const res = await api.fleetBroadcast(msg);
      showToast(
        `Aviso enfileirado para ${res.reached} agente(s) / ${res.channels} canal(is).` +
          (res.skipped.length ? ` Pulados: ${res.skipped.join(", ")}` : ""),
        "success",
      );
      setBroadcast("");
    } catch (e) {
      showToast((e as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };

  const up = members.filter((m) => m.running).length;

  return (
    <div className="space-y-6 p-1">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Radio className="h-5 w-5" />
          <H2>Frota de agentes</H2>
        </div>
        <Button ghost size="sm" onClick={load} disabled={loading}>
          <RefreshCw className="h-4 w-4" /> Atualizar
        </Button>
      </div>

      {!loading && (
        <p className="text-sm text-muted-foreground">
          {members.length} agente(s) · {up} no ar · {members.length - up} parado(s)
        </p>
      )}

      {/* Broadcast */}
      <Card>
        <CardContent className="flex flex-col gap-2 p-4 sm:flex-row sm:items-center">
          <Megaphone className="h-4 w-4 shrink-0" />
          <Input
            placeholder="Aviso para o canal-operador de todos os agentes…"
            value={broadcast}
            onChange={(e) => setBroadcast(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && sendBroadcast()}
          />
          <Button onClick={sendBroadcast} disabled={busy === "broadcast" || !broadcast.trim()}>
            {busy === "broadcast" ? <Spinner className="h-4 w-4" /> : "Enviar"}
          </Button>
        </CardContent>
      </Card>

      {loading ? (
        <div className="flex justify-center py-12">
          <Spinner className="h-6 w-6" />
        </div>
      ) : members.length === 0 ? (
        <EmptyState
          icon={<Radio className="h-8 w-8" />}
          title="Nenhum agente ainda"
          description="Crie seu primeiro perfil de agente para começar. Cada perfil é um agente independente com personalidade e modelo próprios."
          actionLabel="Criar um agente"
          actionPath="/profiles"
        />
      ) : (
        <div className="space-y-3">
          {members.map((m) => (
            <Card key={m.name}>
              <CardContent className="flex flex-wrap items-center gap-3 p-4">
                <StatusDot active={m.running} title={m.running ? "no ar" : "parado"} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{m.name}</span>
                    {m.is_default && (
                      <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
                        controle
                      </span>
                    )}
                  </div>
                  <div className="truncate text-xs text-muted-foreground">
                    {m.model || "?"}
                    {m.pid ? ` · pid ${m.pid}` : ""} · {m.skills} skills
                    {m.description ? ` · ${m.description}` : ""}
                  </div>
                </div>
                <div className="flex shrink-0 flex-wrap gap-1">
                  <Button
                    outlined
                    size="sm"
                    onClick={() => {
                      setExpandedChannels((prev) => {
                        const next = new Set(prev);
                        if (next.has(m.name)) next.delete(m.name);
                        else next.add(m.name);
                        return next;
                      });
                    }}
                  >
                    {expandedChannels.has(m.name) ? (
                      <ChevronDown className="h-4 w-4" />
                    ) : (
                      <ChevronRight className="h-4 w-4" />
                    )}{" "}
                    Canais
                  </Button>
                  <Button
                    outlined
                    size="sm"
                    onClick={() => setChannelsFor(m)}
                  >
                    <Settings className="h-4 w-4" /> Configurar canais
                  </Button>
                  <Button
                    outlined
                    size="sm"
                    onClick={() => openLogs(m.name)}
                  >
                    <FileText className="h-4 w-4" /> Logs
                  </Button>
                  <Button
                    outlined
                    size="sm"
                    onClick={() => act(m.name, "restart")}
                    disabled={busy === `${m.name}:restart`}
                  >
                    {busy === `${m.name}:restart` ? (
                      <Spinner className="h-4 w-4" />
                    ) : (
                      <RotateCw className="h-4 w-4" />
                    )}{" "}
                    Reiniciar
                  </Button>
                  {m.running ? (
                    <Button
                      outlined
                      size="sm"
                      onClick={() => act(m.name, "stop")}
                      disabled={m.is_default || busy === `${m.name}:stop`}
                      title={m.is_default ? "O profile de controle não pode ser parado aqui" : ""}
                    >
                      Parar
                    </Button>
                  ) : (
                    <Button
                      outlined
                      size="sm"
                      onClick={() => act(m.name, "start")}
                      disabled={busy === `${m.name}:start`}
                    >
                      Subir
                    </Button>
                  )}
                </div>
              </CardContent>

              {/* Expandable channels inline */}
              {expandedChannels.has(m.name) && (
                <div className="border-t border-border bg-background/50 px-4 py-3">
                  {(m.platforms ?? []).length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      Nenhum canal configurado para este profile.
                    </p>
                  ) : (
                    <div className="flex flex-wrap gap-2">
                      {(m.platforms ?? []).map((p) => (
                        <div
                          key={p.platform}
                          className="flex items-center gap-2 border border-border px-3 py-1.5 text-xs"
                        >
                          <span>{platformEmoji(p.platform)}</span>
                          <span className="font-medium capitalize">
                            {p.platform}
                          </span>
                          {p.handle && (
                            <a
                              href={`https://t.me/${p.handle.replace(/^@/, "")}`}
                              target="_blank"
                              rel="noreferrer"
                              className="font-mono text-primary hover:underline"
                              title="Abrir o bot no Telegram"
                            >
                              {p.handle}
                            </a>
                          )}
                          {p.has_token ? (
                            <Badge tone="success" className="text-xs">
                              token ✓
                            </Badge>
                          ) : (
                            <Badge tone="warning" className="text-xs">
                              sem token ⚠
                            </Badge>
                          )}
                          {p.home_channel && (
                            <span className="font-mono-ui text-muted-foreground">
                              {p.home_channel.name ?? p.home_channel.chat_id}
                            </span>
                          )}
                          <Badge
                            tone={p.enabled ? "success" : "secondary"}
                            className="text-xs"
                          >
                            {p.enabled ? "ativo" : "inativo"}
                          </Badge>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </Card>
          ))}
        </div>
      )}

      {/* Channels modal */}
      {channelsFor && (
        <ChannelsPanel
          member={channelsFor}
          onClose={() => setChannelsFor(null)}
        />
      )}

      {/* Logs panel */}
      {logsFor && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={() => setLogsFor(null)}
        >
          <div
            className="flex max-h-[80vh] w-full max-w-3xl flex-col rounded-lg bg-background shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b p-3">
              <span className="font-medium">Logs · {logsFor}</span>
              <Button ghost size="sm" onClick={() => setLogsFor(null)}>
                <X className="h-4 w-4" />
              </Button>
            </div>
            <pre className="overflow-auto whitespace-pre-wrap p-3 text-xs leading-relaxed">
              {logText}
            </pre>
          </div>
        </div>
      )}

      <Toast toast={toast} />
    </div>
  );
}
