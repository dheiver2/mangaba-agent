import { ArrowRight, BookOpen, Cpu, MessageSquare, Radio, Users, Zap } from "lucide-react";
import { Button } from "@dheiver2/ui/ui/components/button";
import { useNavigate } from "react-router-dom";
import { Stagger, StaggerItem } from "@/components/motion";
import { AgentStatus } from "@/components/AgentStatus";
import { HomeMetrics } from "@/components/HomeMetrics";

const STEPS = [
  {
    title: "Defina o cérebro",
    description: "Escolha o modelo de IA que responderá às suas mensagens.",
    path: "/models",
    icon: Cpu,
    action: "Ir para modelos",
  },
  {
    title: "Conecte um canal",
    description: "Adicione Telegram, Discord ou outro canal para enviar e receber mensagens.",
    path: "/routing",
    icon: Radio,
    action: "Conectar canal",
  },
  {
    title: "Crie um agente",
    description: "Monte um perfil com personalidade e comportamento próprio.",
    path: "/profiles",
    icon: Users,
    action: "Criar agente",
  },
  {
    title: "Converse",
    description: "Use o chat para testar e ajustar o agente imediatamente.",
    path: "/chat",
    icon: MessageSquare,
    action: "Abrir chat",
  },
];

const QUICK_ACTIONS = [
  {
    title: "Comece pelo chat",
    description: "Teste o agente imediatamente e veja a IA em ação.",
    icon: Zap,
    path: "/chat",
    action: "Abrir chat",
  },
  {
    title: "Automatize com Kanban",
    description: "Delegue tarefas e acompanhe o progresso em um só lugar.",
    icon: BookOpen,
    path: "/kanban",
    action: "Abrir Kanban",
  },
  {
    title: "Verifique sua configuração",
    description: "Confira chaves, plugins, memória e perfis ativos.",
    icon: Users,
    path: "/config",
    action: "Abrir configurações",
  },
];

export default function HomePage() {
  const navigate = useNavigate();

  return (
    <Stagger className="space-y-6 pb-6" gap={0.09}>
      <StaggerItem>
        <AgentStatus />
      </StaggerItem>

      <StaggerItem>
        <HomeMetrics />
      </StaggerItem>

      <StaggerItem>
      <section className="rounded-[2rem] border border-border bg-card p-7 shadow-sm">
        <div className="grid gap-6 lg:grid-cols-[1.2fr_max(320px,0.8fr)] lg:items-center">
          <div className="space-y-5">
            <span className="inline-flex rounded-full bg-midground/10 px-4 py-2 text-sm font-semibold text-midground">
              Comece aqui
            </span>
            <h1 className="max-w-3xl text-4xl font-semibold tracking-tight text-foreground sm:text-5xl">
              Um painel simples para gerenciar agentes, canais e automações.
            </h1>
            <p className="max-w-2xl text-base leading-7 text-muted-foreground">
              Use o dashboard como sua central de trabalho: configure modelos, crie perfis e inicie conversas sem precisar voltar ao terminal.
            </p>
            <div className="flex flex-col gap-3 sm:flex-row">
              <Button onClick={() => navigate("/chat")}>Testar um agente</Button>
              <Button ghost onClick={() => navigate("/docs")}>Guia rápido</Button>
            </div>
          </div>

          <div className="grid gap-3">
            {STEPS.slice(0, 3).map((step) => {
              const Icon = step.icon;
              return (
                <div
                  key={step.title}
                  className="rounded-3xl border border-border bg-background p-4"
                >
                  <div className="flex items-start gap-3">
                    <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-midground/10 text-midground">
                      <Icon className="h-5 w-5" />
                    </div>
                    <div>
                      <p className="text-sm font-semibold">{step.title}</p>
                      <p className="mt-1 text-sm text-muted-foreground">{step.description}</p>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </section>
      </StaggerItem>

      <StaggerItem>
      <section className="grid gap-4 xl:grid-cols-[1.5fr_minmax(280px,1fr)]">
        <div className="rounded-[2rem] border border-border bg-card p-6 shadow-sm">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <span className="text-xs font-semibold uppercase tracking-[0.24em] text-muted-foreground">
                Jornada inicial
              </span>
              <h2 className="mt-3 text-2xl font-semibold">Configure seu primeiro fluxo em minutos</h2>
            </div>
            <span className="inline-flex rounded-2xl bg-midground/10 px-3 py-2 text-sm font-semibold text-midground">
              Rápido e direto
            </span>
          </div>

          <div className="mt-6 grid gap-3 sm:grid-cols-2">
            {STEPS.map((step) => {
              const Icon = step.icon;
              return (
                <button
                  key={step.path}
                  onClick={() => navigate(step.path)}
                  className="group flex w-full flex-col gap-3 rounded-3xl border border-border p-4 text-left transition hover:border-midground/60 hover:bg-midground/5"
                >
                  <div className="flex items-center gap-3">
                    <span className="inline-flex h-10 w-10 items-center justify-center rounded-2xl bg-foreground text-background">
                      <Icon className="h-5 w-5" />
                    </span>
                    <div>
                      <p className="text-sm font-semibold">{step.title}</p>
                      <p className="text-sm text-muted-foreground">{step.description}</p>
                    </div>
                  </div>
                  <span className="text-sm font-semibold text-midground group-hover:text-midground">
                    {step.action} <ArrowRight className="inline-block h-4 w-4" />
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        <div className="space-y-4">
          {QUICK_ACTIONS.map((card) => {
            const Icon = card.icon;
            return (
              <div key={card.title} className="rounded-[2rem] border border-border bg-card p-5 shadow-sm">
                <div className="flex items-center gap-3">
                  <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-muted text-foreground">
                    <Icon className="h-5 w-5" />
                  </div>
                  <div>
                    <p className="text-sm font-semibold">{card.title}</p>
                    <p className="text-sm text-muted-foreground">{card.description}</p>
                  </div>
                </div>
                <div className="mt-5">
                  <Button onClick={() => navigate(card.path)} className="w-full">
                    {card.action}
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      </section>
      </StaggerItem>

      <StaggerItem>
      <section className="rounded-[2rem] border border-border bg-card p-6 shadow-sm">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-xl font-semibold">Por que usar o Mangaba?</h2>
            <p className="mt-2 text-sm text-muted-foreground max-w-2xl">
              Um painel pensado para quem quer gerenciar agentes, criar automações e continuar produtivo sem complexidade.
            </p>
          </div>
          <div className="rounded-2xl bg-midground/10 px-4 py-2 text-sm font-semibold text-midground">
            Plug & play
          </div>
        </div>

        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <div className="rounded-3xl border border-border bg-background p-4">
            <p className="font-semibold">Tudo visual</p>
            <p className="mt-2 text-sm text-muted-foreground">
              Configure modelos, agentes, canais e automações com cliques.
            </p>
          </div>
          <div className="rounded-3xl border border-border bg-background p-4">
            <p className="font-semibold">Fácil de testar</p>
            <p className="mt-2 text-sm text-muted-foreground">
              Converse instantaneamente e ajuste o comportamento do agente.
            </p>
          </div>
          <div className="rounded-3xl border border-border bg-background p-4">
            <p className="font-semibold">Automação segura</p>
            <p className="mt-2 text-sm text-muted-foreground">
              Use Kanban e cron para delegar tarefas e manter o controle.
            </p>
          </div>
        </div>
      </section>
      </StaggerItem>
    </Stagger>
  );
}
