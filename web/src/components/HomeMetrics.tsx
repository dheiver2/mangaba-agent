import { useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, Coins, Radio, Users } from "lucide-react";
import { Link } from "react-router-dom";
import { api, type FleetMember, type UsageResponse } from "@/lib/api";

// Painel de métricas da home — hero "Total Balance": número grande do dia,
// variação vs ontem, área dos últimos 14 dias e tiles de estado da frota.
// Série única (tokens/dia) → sem legenda; o título nomeia a série.

function fmtTokens(n: number): string {
  const compact = (v: number, suffix: string) =>
    `${v.toFixed(1).replace(/\.0$/, "")}${suffix}`;
  if (n >= 1_000_000) return compact(n / 1_000_000, "M");
  if (n >= 1_000) return compact(n / 1_000, "k");
  return String(n);
}

function StatTile({
  icon: Icon,
  label,
  value,
  hint,
  to,
}: {
  icon: typeof Coins;
  label: string;
  value: string;
  hint?: string;
  to: string;
}) {
  return (
    <Link
      to={to}
      className="flex flex-col gap-2 rounded-2xl border border-border bg-card p-5 transition-colors hover:border-ring/40"
    >
      <span className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
        <Icon className="h-3.5 w-3.5" aria-hidden />
        {label}
      </span>
      <span className="text-2xl font-bold tracking-tight text-foreground">{value}</span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </Link>
  );
}

export function HomeMetrics() {
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [fleet, setFleet] = useState<FleetMember[]>([]);

  useEffect(() => {
    api.getUsage(14).then(setUsage).catch(() => {});
    api
      .getFleet()
      .then((r) => setFleet(r.members ?? []))
      .catch(() => {});
  }, []);

  const series = useMemo(() => {
    const days = usage?.recent?.series ?? [];
    return days.map((d) => ({
      date: d.date,
      label: d.date.slice(5).split("-").reverse().join("/"),
      total: d.total ?? (d.input ?? 0) + (d.output ?? 0),
      turns: d.turns ?? 0,
    }));
  }, [usage]);

  if (!usage && fleet.length === 0) return null;

  const today = usage?.today;
  const yesterday = series.length >= 2 ? series[series.length - 2] : null;
  const delta =
    today && yesterday && yesterday.total > 0
      ? ((today.total - yesterday.total) / yesterday.total) * 100
      : null;

  const up = fleet.filter((m) => m.running).length;
  const budget = usage?.budget;

  return (
    <section className="grid gap-4 lg:grid-cols-[1.6fr_1fr]">
      {/* Hero: tokens hoje + área de 14 dias */}
      <div className="rounded-2xl border border-border bg-card p-6">
        <div className="flex items-baseline justify-between gap-3 flex-wrap">
          <div>
            <p className="text-xs font-medium text-muted-foreground">
              Tokens consumidos hoje
            </p>
            <p className="mt-1 text-4xl font-bold tracking-tight text-foreground">
              {fmtTokens(today?.total ?? 0)}
            </p>
          </div>
          {delta !== null && (
            <span
              className={`rounded-full px-2.5 py-1 text-xs font-semibold ${
                delta >= 0 ? "bg-primary/15 text-primary" : "bg-muted text-muted-foreground"
              }`}
            >
              {delta >= 0 ? "+" : ""}
              {delta.toFixed(1)}% vs ontem
            </span>
          )}
        </div>

        {series.length >= 2 && (
          <div className="mt-4 h-40" role="img" aria-label="Tokens por dia, últimos 14 dias">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={series} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="home-tokens-fill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--color-primary)" stopOpacity={0.28} />
                    <stop offset="100%" stopColor="var(--color-primary)" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid
                  vertical={false}
                  stroke="var(--color-border)"
                  strokeOpacity={0.5}
                />
                <XAxis
                  dataKey="label"
                  tickLine={false}
                  axisLine={false}
                  interval="preserveStartEnd"
                  tick={{ fill: "var(--color-muted-foreground)", fontSize: 11 }}
                />
                <YAxis
                  width={48}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v: number) => fmtTokens(v)}
                  tick={{ fill: "var(--color-muted-foreground)", fontSize: 11 }}
                />
                <Tooltip
                  cursor={{ stroke: "var(--color-ring)", strokeOpacity: 0.4 }}
                  contentStyle={{
                    background: "var(--color-popover)",
                    border: "1px solid var(--color-border)",
                    borderRadius: "0.75rem",
                    color: "var(--color-foreground)",
                    fontSize: 12,
                  }}
                  labelStyle={{ color: "var(--color-muted-foreground)" }}
                  formatter={(value) => [`${fmtTokens(Number(value ?? 0))} tokens`, ""]}
                />
                <Area
                  type="monotone"
                  dataKey="total"
                  stroke="var(--color-primary)"
                  strokeWidth={2}
                  fill="url(#home-tokens-fill)"
                  dot={false}
                  activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--color-card)" }}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>

      {/* Tiles de estado */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-1 xl:grid-cols-2">
        <StatTile
          icon={Radio}
          label="Agentes no ar"
          value={`${up}/${fleet.length}`}
          hint={up > 0 ? "frota respondendo" : "nenhum gateway ativo"}
          to="/fleet"
        />
        <StatTile
          icon={Activity}
          label="Turnos hoje"
          value={String(today?.turns ?? 0)}
          hint="conversas em todos os canais"
          to="/sessions"
        />
        <StatTile
          icon={Coins}
          label="Teto diário"
          value={
            budget?.enabled && budget.daily_token_limit > 0
              ? `${Math.min(100, Math.round(budget.percent))}%`
              : "sem teto"
          }
          hint={
            budget?.enabled && budget.daily_token_limit > 0
              ? `${fmtTokens(budget.used)} de ${fmtTokens(budget.daily_token_limit)} (${budget.budget_mode})`
              : "defina em Configuração → usage"
          }
          to="/config"
        />
        <StatTile
          icon={Users}
          label="Agentes criados"
          value={String(Math.max(0, fleet.length - 1))}
          hint="perfis além do padrão"
          to="/profiles"
        />
      </div>
    </section>
  );
}
