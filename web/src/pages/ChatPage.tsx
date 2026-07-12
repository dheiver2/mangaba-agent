import { useCallback, useEffect, useRef, useState } from "react";
import { Send, Square, RotateCw } from "lucide-react";
import { MANGABA_BASE_PATH, api, type FleetMember } from "@/lib/api";
import { cn } from "@/lib/utils";
import { StatusDot } from "@/components/StatusDot";

// ChatGPT-style chat for the dashboard. Talks to the agent over the
// /api/chat WebSocket (one turn per message, streamed token-by-token).
// Independent of the embedded terminal / --tui.

interface Msg {
  role: "user" | "assistant";
  content: string;
  pending?: boolean;
}

function buildChatWsUrl(token: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${MANGABA_BASE_PATH}/api/chat?token=${encodeURIComponent(token)}`;
}

function Avatar({ who }: { who: "user" | "assistant" }) {
  if (who === "assistant") {
    return (
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-foreground text-background text-sm font-bold">
        M
      </div>
    );
  }
  return (
    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-secondary text-foreground text-xs font-medium">
      Você
    </div>
  );
}

export default function ChatPage() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Agentes criados (profiles da frota). "" = agente padrão (config ativa).
  // O modelo de cada agente já foi definido na página Modelos — aqui só se
  // escolhe COM QUEM falar, nunca o modelo.
  const [agents, setAgents] = useState<FleetMember[]>([]);
  const [selected, setSelected] = useState<string>("");

  const wsRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  const connect = useCallback(() => {
    const token =
      typeof window !== "undefined" ? window.__MANGABA_SESSION_TOKEN__ : undefined;
    if (!token) {
      setError(
        "Token de sessão indisponível. Abra o dashboard via `mangaba dashboard`.",
      );
      return;
    }
    try {
      const ws = new WebSocket(buildChatWsUrl(token));
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        setError(null);
      };
      ws.onclose = () => {
        setConnected(false);
        setBusy(false);
      };
      ws.onerror = () => {
        setError("Erro de conexão com o agente.");
      };

      ws.onmessage = (ev) => {
        let data: { type: string; text?: string };
        try {
          data = JSON.parse(ev.data) as { type: string; text?: string };
        } catch {
          return;
        }
        if (data.type === "status") {
          return;
        }
        if (data.type === "delta") {
          setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last && last.role === "assistant") {
              next[next.length - 1] = {
                ...last,
                content: last.content + (data.text ?? ""),
                pending: true,
              };
            }
            return next;
          });
          scrollToBottom();
        } else if (data.type === "done") {
          setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last && last.role === "assistant") {
              next[next.length - 1] = {
                role: "assistant",
                content: data.text ?? last.content,
                pending: false,
              };
            }
            return next;
          });
          setBusy(false);
          scrollToBottom();
        } else if (data.type === "error") {
          setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last && last.role === "assistant" && last.pending) {
              next[next.length - 1] = {
                role: "assistant",
                content: `⚠️ ${data.text ?? "erro"}`,
                pending: false,
              };
            }
            return next;
          });
          setBusy(false);
        }
      };
    } catch {
      setError("Não foi possível abrir a conexão.");
    }
  }, [scrollToBottom]);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect]);

  // Carrega os agentes criados (profiles) para o seletor.
  useEffect(() => {
    api
      .getFleet()
      .then((res) => {
        setAgents((res.members ?? []).filter((m) => !m.is_default));
      })
      .catch(() => {
        /* sem frota — o chat usa o agente padrão */
      });
  }, []);

  const onAgentChange = (value: string) => {
    setSelected(value);
    const label = value || "Agente padrão";
    // Trocar de agente zera o histórico no backend — reflete aqui também.
    setMessages([
      { role: "assistant", content: `🔄 Falando agora com: ${label}`, pending: false },
    ]);
  };

  const send = () => {
    const text = input.trim();
    if (!text || busy) return;
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setError("Sem conexão. Tente reconectar.");
      return;
    }
    setMessages((prev) => [
      ...prev,
      { role: "user", content: text },
      { role: "assistant", content: "", pending: true },
    ]);
    setInput("");
    setBusy(true);
    ws.send(JSON.stringify({ message: text, agent: selected }));
    setTimeout(scrollToBottom, 0);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="flex min-h-0 w-full min-w-0 flex-1 flex-col">
      {/* Cabeçalho com marca Mangaba */}
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-3">
        <div className="flex h-7 w-7 items-center justify-center rounded-full bg-foreground text-background text-sm font-bold">
          M
        </div>
        <div className="flex min-w-0 flex-col">
          <span className="text-sm font-semibold leading-tight">
            {selected || "Mangaba Agent"}
          </span>
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <StatusDot active={connected} className="h-1.5 w-1.5" />
            {connected ? "Conectado" : "Desconectado"}
          </span>
        </div>

        <div className="ml-auto flex items-center gap-2">
          {agents.length > 0 && (
            <select
              value={selected}
              onChange={(e) => onAgentChange(e.target.value)}
              title="Escolher com qual agente conversar"
              aria-label="Selecionar agente"
              className="max-w-[180px] rounded-md border border-input bg-background px-2 py-1 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="">Agente padrão</option>
              {agents.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                  {a.description ? ` — ${a.description.slice(0, 40)}` : ""}
                </option>
              ))}
            </select>
          )}
          {!connected && (
            <button
              onClick={() => connect()}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:text-foreground hover:bg-secondary/40"
            >
              <RotateCw className="h-3.5 w-3.5" /> Reconectar
            </button>
          )}
        </div>
      </div>

      {/* Mensagens */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-3xl flex-col gap-5 px-4 py-6">
          {messages.length === 0 && !error && (
            <div className="flex flex-col items-center justify-center gap-3 py-20 text-center">
              <div className="flex h-14 w-14 items-center justify-center rounded-full bg-foreground text-background text-2xl font-bold">
                M
              </div>
              <h2 className="text-xl font-bold">Mangaba Agent</h2>
              <p className="max-w-sm text-sm text-muted-foreground">
                Comece uma conversa. Pergunte qualquer coisa, peça resumos,
                pesquisas, código — o agente responde aqui mesmo.
              </p>
            </div>
          )}

          {error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
              {error}
            </div>
          )}

          {messages.map((m, i) => (
            <div
              key={i}
              className={cn(
                "flex gap-3",
                m.role === "user" ? "flex-row-reverse" : "flex-row",
              )}
            >
              <Avatar who={m.role} />
              <div
                className={cn(
                  "max-w-[80%] rounded-lg px-3.5 py-2.5 text-sm leading-relaxed",
                  m.role === "user"
                    ? "bg-foreground text-background"
                    : "bg-card border border-border text-foreground",
                )}
              >
                {m.content ? (
                  <pre className="whitespace-pre-wrap break-words font-sans">
                    {m.content}
                  </pre>
                ) : m.pending ? (
                  <span className="inline-flex gap-1">
                    <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current opacity-60" />
                    <span
                      className="h-1.5 w-1.5 animate-pulse rounded-full bg-current opacity-60"
                      style={{ animationDelay: "0.2s" }}
                    />
                    <span
                      className="h-1.5 w-1.5 animate-pulse rounded-full bg-current opacity-60"
                      style={{ animationDelay: "0.4s" }}
                    />
                  </span>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Input */}
      <div className="shrink-0 border-t border-border px-4 py-3">
        <div className="mx-auto flex max-w-3xl items-end gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Envie uma mensagem para o Mangaba Agent…"
            rows={1}
            disabled={!connected}
            className="max-h-40 min-h-[44px] flex-1 resize-none rounded-lg border border-input bg-background px-3.5 py-2.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
          />
          <button
            onClick={send}
            disabled={!connected || busy || !input.trim()}
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-foreground text-background transition-opacity hover:opacity-90 disabled:opacity-40"
            aria-label={busy ? "Aguardando resposta" : "Enviar"}
          >
            {busy ? <Square className="h-4 w-4" /> : <Send className="h-4 w-4" />}
          </button>
        </div>
        <p className="mx-auto mt-1.5 max-w-3xl text-center text-xs text-muted-foreground">
          Mangaba Agent · Enter envia, Shift+Enter quebra linha
        </p>
      </div>
    </div>
  );
}
