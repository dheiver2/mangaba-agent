// The dashboard can be served either at the root of its host (e.g.
// https://kanban.tilos.com/) or under a URL prefix when reverse-proxied
// (e.g. https://mission-control.tilos.com/mangaba/). The Python backend
// injects ``window.__MANGABA_BASE_PATH__`` into index.html based on the
// incoming ``X-Forwarded-Prefix`` header so the SPA can address its own
// ``/api/...`` and ``/dashboard-plugins/...`` URLs correctly without a
// rebuild. Empty string means "served at root".
function readBasePath(): string {
  if (typeof window === "undefined") return "";
  const raw = window.__MANGABA_BASE_PATH__ ?? "";
  if (!raw) return "";
  // Normalise: ensure leading slash, strip trailing slash.
  const withLead = raw.startsWith("/") ? raw : `/${raw}`;
  return withLead.replace(/\/+$/, "");
}

export const MANGABA_BASE_PATH = readBasePath();
const BASE = MANGABA_BASE_PATH;

import type { DashboardTheme } from "@/themes/types";

// Ephemeral session token for protected endpoints.
// Injected into index.html by the server — never fetched via API.
declare global {
  interface Window {
    __MANGABA_SESSION_TOKEN__?: string;
    __MANGABA_BASE_PATH__?: string;
  }
}
let _sessionToken: string | null = null;
const SESSION_HEADER = "X-Mangaba-Session-Token";

function setSessionHeader(headers: Headers, token: string): void {
  if (!headers.has(SESSION_HEADER)) {
    headers.set(SESSION_HEADER, token);
  }
}

export async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  // Inject the session token into all /api/ requests.
  const headers = new Headers(init?.headers);
  const token = window.__MANGABA_SESSION_TOKEN__;
  if (token) {
    setSessionHeader(headers, token);
  }
  const res = await fetch(`${BASE}${url}`, { ...init, headers });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

/** Encode a plugin registry key for URL paths (preserves `/` segment separators). */
function pluginPath(name: string): string {
  return name.split("/").map(encodeURIComponent).join("/");
}

async function getSessionToken(): Promise<string> {
  if (_sessionToken) return _sessionToken;
  const injected = window.__MANGABA_SESSION_TOKEN__;
  if (injected) {
    _sessionToken = injected;
    return _sessionToken;
  }
  throw new Error("Session token not available — page must be served by the Mangaba dashboard server");
}

export const api = {
  getStatus: () => fetchJSON<StatusResponse>("/api/status"),
  getSessions: (limit = 20, offset = 0) =>
    fetchJSON<PaginatedSessions>(`/api/sessions?limit=${limit}&offset=${offset}`),
  getSessionMessages: (id: string) =>
    fetchJSON<SessionMessagesResponse>(`/api/sessions/${encodeURIComponent(id)}/messages`),
  getSessionLatestDescendant: (id: string) =>
    fetchJSON<SessionLatestDescendantResponse>(
      `/api/sessions/${encodeURIComponent(id)}/latest-descendant`,
    ),
  deleteSession: (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  getLogs: (params: { file?: string; lines?: number; level?: string; component?: string; search?: string }) => {
    const qs = new URLSearchParams();
    if (params.file) qs.set("file", params.file);
    if (params.lines) qs.set("lines", String(params.lines));
    if (params.level && params.level !== "ALL") qs.set("level", params.level);
    if (params.component && params.component !== "all") qs.set("component", params.component);
    if (params.search && params.search.trim()) qs.set("search", params.search.trim());
    return fetchJSON<LogsResponse>(`/api/logs?${qs.toString()}`);
  },
  getAnalytics: (days: number) =>
    fetchJSON<AnalyticsResponse>(`/api/analytics/usage?days=${days}`),
  getModelsAnalytics: (days: number) =>
    fetchJSON<ModelsAnalyticsResponse>(`/api/analytics/models?days=${days}`),
  getConfig: () => fetchJSON<Record<string, unknown>>("/api/config"),
  getDefaults: () => fetchJSON<Record<string, unknown>>("/api/config/defaults"),
  getSchema: () => fetchJSON<{ fields: Record<string, unknown>; category_order: string[] }>("/api/config/schema"),
  getModelOptions: () => fetchJSON<ModelOptionsResponse>("/api/model/options"),

  testModelConnection: (provider: string, model: string) =>
    fetchJSON<ModelTestResponse>("/api/model/test-connection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, model }),
    }),
  getChatModels: () => fetchJSON<ChatModelsResponse>("/api/chat/models"),

  // ── Memória ────────────────────────────────────────────────────────────
  getMemory: () => fetchJSON<MemoryResponse>("/api/memory"),
  saveMemory: (target: "memory" | "user", content: string) =>
    fetchJSON<{ ok: boolean; chars: number }>("/api/memory", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, content }),
    }),
  resetMemory: (target: "all" | "memory" | "user") =>
    fetchJSON<{ ok: boolean }>("/api/memory/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target }),
    }),

  // ── Clientes & API (white-label / multi-tenant) ──────────────────────────
  getApiInfo: () => fetchJSON<{ base_url: string; endpoint: string }>("/api/clients/api-info"),
  listClients: () => fetchJSON<{ clients: ApiClient[] }>("/api/clients"),
  createClient: (body: Partial<ApiClient> & { name: string }) =>
    fetchJSON<ApiClient>("/api/clients", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  updateClient: (id: string, body: Partial<ApiClient>) =>
    fetchJSON<ApiClient>(`/api/clients/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteClient: (id: string) =>
    fetchJSON<{ ok: boolean }>(`/api/clients/${id}`, { method: "DELETE" }),
  listClientKeys: (id: string) =>
    fetchJSON<{ keys: ApiKey[] }>(`/api/clients/${id}/keys`),
  createClientKey: (id: string) =>
    fetchJSON<{ id: string; key: string; last4: string }>(`/api/clients/${id}/keys`, {
      method: "POST",
    }),
  revokeClientKey: (keyId: string) =>
    fetchJSON<{ ok: boolean }>(`/api/clients/keys/${keyId}`, { method: "DELETE" }),
  clientProfileStatus: (id: string) =>
    fetchJSON<ClientProfileStatus>(`/api/clients/${id}/profile/status`),
  clientProfileStart: (id: string) =>
    fetchJSON<{ running: boolean; api_port: number; error?: string }>(
      `/api/clients/${id}/profile/start`,
      { method: "POST" },
    ),
  clientProfileStop: (id: string) =>
    fetchJSON<{ running: boolean }>(`/api/clients/${id}/profile/stop`, { method: "POST" }),

  // ── RAG (base de conhecimento mangaba.ia.br) ─────────────────────────────
  getRagStatus: () => fetchJSON<RagStatus>("/api/rag/status"),
  reindexRag: () =>
    fetchJSON<{ ok: boolean; pages: number; chunks: number; path: string }>(
      "/api/rag/reindex",
      { method: "POST" },
    ),
  enableRag: (enable: boolean) =>
    fetchJSON<{ ok: boolean; enabled: boolean }>(
      `/api/rag/enable?enable=${enable ? "true" : "false"}`,
      { method: "POST" },
    ),

  // ── Observabilidade (traces + eval) ──────────────────────────────────────
  getTraces: (limit = 100, hours = 24) =>
    fetchJSON<TracesResponse>(`/api/traces?limit=${limit}&hours=${hours}`),
  setObservabilityEval: (enabled: boolean) =>
    fetchJSON<{ ok: boolean; eval: boolean }>(
      `/api/observability?eval=${enabled ? "true" : "false"}`,
      { method: "PUT" },
    ),
  getGuardrails: () => fetchJSON<GuardrailsConfig>("/api/guardrails"),
  setGuardrails: (body: GuardrailsConfig) =>
    fetchJSON<{ ok: boolean }>("/api/guardrails", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // ── Uso & custo ──────────────────────────────────────────────────────────
  getUsage: (days = 14) => fetchJSON<UsageResponse>(`/api/usage?days=${days}`),
  setUsageBudget: (daily_token_limit: number, budget_mode: "warn" | "block") =>
    fetchJSON<{ ok: boolean; budget: UsageBudgetStatus }>("/api/usage/budget", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ daily_token_limit, budget_mode }),
    }),
  getAuxiliaryModels: () => fetchJSON<AuxiliaryModelsResponse>("/api/model/auxiliary"),
  setModelAssignment: (body: ModelAssignmentRequest) =>
    fetchJSON<ModelAssignmentResponse>("/api/model/set", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  saveConfig: (config: Record<string, unknown>) =>
    fetchJSON<{ ok: boolean }>("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    }),
  getConfigRaw: () => fetchJSON<{ yaml: string }>("/api/config/raw"),
  saveConfigRaw: (yaml_text: string) =>
    fetchJSON<{ ok: boolean }>("/api/config/raw", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml_text }),
    }),
  getEnvVars: () => fetchJSON<Record<string, EnvVarInfo>>("/api/env"),
  setEnvVar: (key: string, value: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }),
    }),
  deleteEnvVar: (key: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    }),
  revealEnvVar: async (key: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ key: string; value: string }>("/api/env/reveal", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [SESSION_HEADER]: token,
      },
      body: JSON.stringify({ key }),
    });
  },

  // Cron jobs
  getCronJobs: (profile = "all") =>
    fetchJSON<CronJob[]>(`/api/cron/jobs?profile=${encodeURIComponent(profile)}`),
  createCronJob: (job: { prompt: string; schedule: string; name?: string; deliver?: string }, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs?profile=${encodeURIComponent(profile)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(job),
    }),
  pauseCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/pause?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  resumeCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/resume?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  triggerCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/trigger?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  deleteCronJob: (id: string, profile = "default") =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${encodeURIComponent(id)}?profile=${encodeURIComponent(profile)}`, { method: "DELETE" }),

  // Profiles (minimal)
  getProfiles: () =>
    fetchJSON<{ profiles: ProfileInfo[] }>("/api/profiles"),
  createProfile: (body: { name: string; clone_from_default: boolean }) =>
    fetchJSON<{ ok: boolean; name: string; path: string }>("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  renameProfile: (name: string, newName: string) =>
    fetchJSON<{ ok: boolean; name: string; path: string }>(
      `/api/profiles/${encodeURIComponent(name)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_name: newName }),
      },
    ),
  deleteProfile: (name: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  getProfileSetupCommand: (name: string) =>
    fetchJSON<{ command: string }>(
      `/api/profiles/${encodeURIComponent(name)}/setup-command`,
    ),
  getProfileSoul: (name: string) =>
    fetchJSON<{ content: string; exists: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}/soul`,
    ),
  updateProfileSoul: (name: string, content: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}/soul`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      },
    ),
  getAgentTemplates: () =>
    fetchJSON<{ templates: AgentTemplate[] }>("/api/agent-templates"),
  getAgentTemplate: (id: string) =>
    fetchJSON<AgentTemplate & { persona: string }>(
      `/api/agent-templates/${encodeURIComponent(id)}`,
    ),
  validateChannel: (platform: string, token: string) =>
    fetchJSON<{ ok: boolean; name?: string; username?: string; error?: string }>(
      `/api/channels/${platform}/validate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      },
    ),
  connectChannel: (platform: string, token: string) =>
    fetchJSON<{ ok: boolean; name?: string; username?: string }>(
      `/api/channels/${platform}/connect`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      },
    ),
  getHealth: () =>
    fetchJSON<{ ok: boolean; gateway: boolean; gateway_state: string | null; last_activity: string | null }>(
      "/api/health",
    ),
  validateWhatsAppCloud: (token: string, phone_number_id: string) =>
    fetchJSON<{ ok: boolean; name?: string; number?: string; error?: string }>(
      "/api/whatsapp-cloud/validate",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, phone_number_id }),
      },
    ),
  connectWhatsAppCloud: (token: string, phone_number_id: string) =>
    fetchJSON<{ ok: boolean; name?: string; number?: string; webhook_url: string; verify_token: string }>(
      "/api/whatsapp-cloud/connect",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, phone_number_id }),
      },
    ),
  validateTeams: (client_id: string, client_secret: string, tenant_id: string) =>
    fetchJSON<{ ok: boolean; name?: string; error?: string }>("/api/teams/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id, client_secret, tenant_id }),
    }),
  connectTeams: (client_id: string, client_secret: string, tenant_id: string) =>
    fetchJSON<{ ok: boolean; name?: string; messaging_endpoint: string; internal_port: string }>(
      "/api/teams/connect",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id, client_secret, tenant_id }),
      },
    ),
  getChannelsStatus: () =>
    fetchJSON<{ channels: { platform: string; connected: boolean; valid?: boolean; name?: string; username?: string }[] }>(
      "/api/channels/status",
    ),
  installAgentTemplate: (id: string, name?: string) =>
    fetchJSON<{ ok: boolean; name: string }>(
      `/api/agent-templates/${encodeURIComponent(id)}/install`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name ?? "" }),
      },
    ),
  getProfileTeams: (name: string) =>
    fetchJSON<{ configured: boolean; client_id: string; has_secret: boolean; tenant_id: string; port: number | null }>(
      `/api/profiles/${encodeURIComponent(name)}/teams`,
    ),
  connectProfileTeams: (name: string, client_id: string, client_secret: string, tenant_id: string) =>
    fetchJSON<{ ok: boolean; port: number; messaging_endpoint: string; note: string }>(
      `/api/profiles/${encodeURIComponent(name)}/teams/connect`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id, client_secret, tenant_id }),
      },
    ),
  disconnectProfileTeams: (name: string) =>
    fetchJSON<{ ok: boolean }>(`/api/profiles/${encodeURIComponent(name)}/teams`, {
      method: "DELETE",
    }),
  setProfileModel: (name: string, model: string, provider?: string) =>
    fetchJSON<{ ok: boolean; model: string; provider?: string | null }>(
      `/api/profiles/${encodeURIComponent(name)}/model`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, provider }),
      },
    ),

  // Skills & Toolsets
  getSkills: () => fetchJSON<SkillInfo[]>("/api/skills"),
  toggleSkill: (name: string, enabled: boolean) =>
    fetchJSON<{ ok: boolean }>("/api/skills/toggle", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, enabled }),
    }),
  getToolsets: () => fetchJSON<ToolsetInfo[]>("/api/tools/toolsets"),

  // Session search (FTS5)
  searchSessions: (q: string) =>
    fetchJSON<SessionSearchResponse>(`/api/sessions/search?q=${encodeURIComponent(q)}`),

  // OAuth provider management
  getOAuthProviders: () =>
    fetchJSON<OAuthProvidersResponse>("/api/providers/oauth"),
  disconnectOAuthProvider: async (providerId: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ ok: boolean; provider: string }>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}`,
      {
        method: "DELETE",
        headers: { [SESSION_HEADER]: token },
      },
    );
  },
  startOAuthLogin: async (providerId: string) => {
    const token = await getSessionToken();
    return fetchJSON<OAuthStartResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/start`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          [SESSION_HEADER]: token,
        },
        body: "{}",
      },
    );
  },
  submitOAuthCode: async (providerId: string, sessionId: string, code: string) => {
    const token = await getSessionToken();
    return fetchJSON<OAuthSubmitResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/submit`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          [SESSION_HEADER]: token,
        },
        body: JSON.stringify({ session_id: sessionId, code }),
      },
    );
  },
  pollOAuthSession: (providerId: string, sessionId: string) =>
    fetchJSON<OAuthPollResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/poll/${encodeURIComponent(sessionId)}`,
    ),
  cancelOAuthSession: async (sessionId: string) => {
    const token = await getSessionToken();
    return fetchJSON<{ ok: boolean }>(
      `/api/providers/oauth/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "DELETE",
        headers: { [SESSION_HEADER]: token },
      },
    );
  },

  // Gateway / update actions
  restartGateway: () =>
    fetchJSON<ActionResponse>("/api/gateway/restart", { method: "POST" }),
  updateMangaba: () =>
    fetchJSON<ActionResponse>("/api/mangaba/update", { method: "POST" }),
  getActionStatus: (name: string, lines = 200) =>
    fetchJSON<ActionStatusResponse>(
      `/api/actions/${encodeURIComponent(name)}/status?lines=${lines}`,
    ),

  // Dashboard plugins
  getPlugins: () =>
    fetchJSON<PluginManifestResponse[]>("/api/dashboard/plugins"),
  rescanPlugins: () =>
    fetchJSON<{ ok: boolean; count: number }>("/api/dashboard/plugins/rescan"),

  getPluginsHub: () => fetchJSON<PluginsHubResponse>("/api/dashboard/plugins/hub"),

  installAgentPlugin: (body: AgentPluginInstallRequest) =>
    fetchJSON<AgentPluginInstallResponse>("/api/dashboard/agent-plugins/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body }),
    }),

  enableAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string; unchanged?: boolean }>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}/enable`,
      { method: "POST" },
    ),

  disableAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string; unchanged?: boolean }>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}/disable`,
      { method: "POST" },
    ),

  updateAgentPlugin: (name: string) =>
    fetchJSON<AgentPluginUpdateResponse>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}/update`,
      { method: "POST" },
    ),

  removeAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string }>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}`,
      { method: "DELETE" },
    ),

  savePluginProviders: (body: PluginProvidersPutRequest) =>
    fetchJSON<{ ok: boolean }>("/api/dashboard/plugin-providers", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  setPluginVisibility: (name: string, hidden: boolean) =>
    fetchJSON<{ ok: boolean; name: string; hidden: boolean }>(
      `/api/dashboard/plugins/${pluginPath(name)}/visibility`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hidden }),
      },
    ),

  // Dashboard themes
  getThemes: () =>
    fetchJSON<DashboardThemesResponse>("/api/dashboard/themes"),
  setTheme: (name: string) =>
    fetchJSON<{ ok: boolean; theme: string }>("/api/dashboard/theme", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),

  // Fleet — manage N agents (profiles) at once
  getFleet: () => fetchJSON<{ members: FleetMember[] }>("/api/fleet"),
  getFleetLogs: (name: string, lines = 60) =>
    fetchJSON<{ name: string; log: string }>(
      `/api/fleet/${encodeURIComponent(name)}/logs?lines=${lines}`,
    ),
  fleetAction: (name: string, action: "start" | "stop" | "restart") =>
    fetchJSON<{ ok: boolean; message: string }>(
      `/api/fleet/${encodeURIComponent(name)}/${action}`,
      { method: "POST" },
    ),
  getFleetBudget: (name: string) =>
    fetchJSON<FleetBudget>(`/api/fleet/${encodeURIComponent(name)}/budget`),
  setFleetBudget: (name: string, daily_token_limit: number, budget_mode: "warn" | "block") =>
    fetchJSON<{ ok: boolean; daily_token_limit: number; budget_mode: string }>(
      `/api/fleet/${encodeURIComponent(name)}/budget`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ daily_token_limit, budget_mode }),
      },
    ),
  fleetBroadcast: (message: string) =>
    fetchJSON<{ ok: boolean; reached: number; channels: number; skipped: string[] }>(
      "/api/fleet/broadcast",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      },
    ),

  // ── Kanban ─────────────────────────────────────────────────────────────
  getKanbanBoards: () =>
    fetchJSON<KanbanBoardsResponse>("/api/kanban/boards"),
  createKanbanBoard: (body: { slug: string; name?: string; description?: string }) =>
    fetchJSON<{ ok: boolean; board: { slug: string; name: string } }>("/api/kanban/boards", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  selectKanbanBoard: (slug: string) =>
    fetchJSON<{ ok: boolean; current: string }>("/api/kanban/boards/current", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug }),
    }),
  deleteKanbanBoard: (slug: string) =>
    fetchJSON<{ ok: boolean }>(`/api/kanban/boards/${encodeURIComponent(slug)}`, {
      method: "DELETE",
    }),
  getKanbanTasks: (board: string) =>
    fetchJSON<KanbanTasksResponse>(`/api/kanban/tasks?board=${encodeURIComponent(board)}`),
  createKanbanTask: (
    board: string,
    body: { title: string; body?: string; assignee?: string; priority?: number; triage?: boolean },
  ) =>
    fetchJSON<{ ok: boolean; task: KanbanTask }>(
      `/api/kanban/tasks?board=${encodeURIComponent(board)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  getKanbanTask: (board: string, id: string) =>
    fetchJSON<KanbanTaskDetail>(
      `/api/kanban/tasks/${encodeURIComponent(id)}?board=${encodeURIComponent(board)}`,
    ),
  kanbanTaskAction: (
    board: string,
    id: string,
    action: "complete" | "unblock" | "reclaim",
  ) =>
    fetchJSON<{ ok: boolean }>(
      `/api/kanban/tasks/${encodeURIComponent(id)}/${action}?board=${encodeURIComponent(board)}`,
      { method: "POST" },
    ),
  kanbanBlockTask: (board: string, id: string, reason: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/kanban/tasks/${encodeURIComponent(id)}/block?board=${encodeURIComponent(board)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason }),
      },
    ),
  kanbanCommentTask: (board: string, id: string, commentBody: string) =>
    fetchJSON<{ ok: boolean; comment_id: number }>(
      `/api/kanban/tasks/${encodeURIComponent(id)}/comment?board=${encodeURIComponent(board)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body: commentBody }),
      },
    ),
  kanbanLlmAction: (id: string, action: "specify" | "decompose") =>
    fetchJSON<{ ok: boolean; started: boolean }>(
      `/api/kanban/tasks/${encodeURIComponent(id)}/${action}`,
      { method: "POST" },
    ),
};

export interface FleetBudget {
  daily_token_limit: number;
  budget_mode: "warn" | "block";
}

export interface FleetMember {
  name: string;
  path: string;
  running: boolean;
  pid: number | null;
  model: string | null;
  provider: string | null;
  skills: number;
  description: string;
  is_default: boolean;
}

export interface ActionResponse {
  name: string;
  ok: boolean;
  pid: number;
}

export interface ActionStatusResponse {
  exit_code: number | null;
  lines: string[];
  name: string;
  pid: number | null;
  running: boolean;
}

export interface PlatformStatus {
  error_code?: string;
  error_message?: string;
  state: string;
  updated_at: string;
}

export interface StatusResponse {
  active_sessions: number;
  config_path: string;
  config_version: number;
  env_path: string;
  gateway_exit_reason: string | null;
  gateway_health_url: string | null;
  gateway_pid: number | null;
  gateway_platforms: Record<string, PlatformStatus>;
  gateway_running: boolean;
  gateway_state: string | null;
  gateway_updated_at: string | null;
  mangaba_home: string;
  latest_config_version: number;
  release_date: string;
  version: string;
  rate_limit?: RateLimitSummary;
}

export interface RateLimitSummary {
  active: boolean;
  count: number;
  by_source: Record<string, number>;
  last: { ts: number; source: string; detail: string } | null;
  window_minutes?: number;
}

export interface SessionInfo {
  id: string;
  source: string | null;
  model: string | null;
  title: string | null;
  started_at: number;
  ended_at: number | null;
  last_active: number;
  is_active: boolean;
  message_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  preview: string | null;
  parent_session_id?: string | null;
}

export interface SessionLatestDescendantResponse {
  requested_session_id: string;
  session_id: string;
  path: string[];
  changed: boolean;
}

export interface PaginatedSessions {
  sessions: SessionInfo[];
  total: number;
  limit: number;
  offset: number;
}

export interface EnvVarInfo {
  is_set: boolean;
  redacted_value: string | null;
  description: string;
  url: string | null;
  category: string;
  is_password: boolean;
  tools: string[];
  advanced: boolean;
}

export interface SessionMessage {
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  tool_calls?: Array<{
    id: string;
    function: { name: string; arguments: string };
  }>;
  tool_name?: string;
  tool_call_id?: string;
  timestamp?: number;
}

export interface SessionMessagesResponse {
  session_id: string;
  messages: SessionMessage[];
}

export interface LogsResponse {
  file: string;
  lines: string[];
}

export interface AnalyticsDailyEntry {
  day: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
  api_calls: number;
}

export interface AnalyticsModelEntry {
  model: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost: number;
  sessions: number;
  api_calls: number;
}

export interface AnalyticsSkillEntry {
  skill: string;
  view_count: number;
  manage_count: number;
  total_count: number;
  percentage: number;
  last_used_at: number | null;
}

export interface AnalyticsSkillsSummary {
  total_skill_loads: number;
  total_skill_edits: number;
  total_skill_actions: number;
  distinct_skills_used: number;
}

export interface AnalyticsResponse {
  daily: AnalyticsDailyEntry[];
  by_model: AnalyticsModelEntry[];
  totals: {
    total_input: number;
    total_output: number;
    total_cache_read: number;
    total_reasoning: number;
    total_estimated_cost: number;
    total_actual_cost: number;
    total_sessions: number;
    total_api_calls: number;
  };
  skills: {
    summary: AnalyticsSkillsSummary;
    top_skills: AnalyticsSkillEntry[];
  };
}

export interface ProfileInfo {
  name: string;
  path: string;
  is_default: boolean;
  model: string | null;
  provider: string | null;
  has_env: boolean;
  skill_count: number;
}

export interface ModelsAnalyticsModelEntry {
  model: string;
  provider: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
  api_calls: number;
  tool_calls: number;
  last_used_at: number;
  avg_tokens_per_session: number;
  capabilities: {
    supports_tools?: boolean;
    supports_vision?: boolean;
    supports_reasoning?: boolean;
    context_window?: number;
    max_output_tokens?: number;
    model_family?: string;
  };
}

export interface ModelsAnalyticsResponse {
  models: ModelsAnalyticsModelEntry[];
  totals: {
    distinct_models: number;
    total_input: number;
    total_output: number;
    total_cache_read: number;
    total_reasoning: number;
    total_estimated_cost: number;
    total_actual_cost: number;
    total_sessions: number;
    total_api_calls: number;
  };
  period_days: number;
}

export interface CronJob {
  id: string;
  profile?: string | null;
  profile_name?: string | null;
  mangaba_home?: string | null;
  is_default_profile?: boolean;
  name?: string | null;
  prompt?: string | null;
  script?: string | null;
  schedule?: { kind?: string; expr?: string; display?: string };
  schedule_display?: string | null;
  enabled: boolean;
  state?: string | null;
  deliver?: string | null;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_status?: string | null;
  last_error?: string | null;
}

export interface SkillInfo {
  name: string;
  description: string;
  category: string;
  enabled: boolean;
}

export interface ToolsetInfo {
  name: string;
  label: string;
  description: string;
  enabled: boolean;
  configured: boolean;
  tools: string[];
}

export interface SessionSearchResult {
  session_id: string;
  snippet: string;
  role: string | null;
  source: string | null;
  model: string | null;
  session_started: number | null;
  session?: SessionInfo;
}

export interface SessionSearchResponse {
  results: SessionSearchResult[];
}

// ── Model info types ──────────────────────────────────────────────────

// ── Model options / assignment types ──────────────────────────────────

export interface ModelOptionProvider {
  name: string;
  slug: string;
  models?: string[];
  total_models?: number;
  is_current?: boolean;
  is_user_defined?: boolean;
  source?: string;
  warning?: string;
}

export interface ModelOptionsResponse {
  model?: string;
  provider?: string;
  providers?: ModelOptionProvider[];
}

export interface ModelTestResponse {
  ok: boolean;
  reachable: boolean;
  valid?: boolean | null;
  provider?: string;
  base_url?: string;
  model?: string;
  available_sample?: string[];
  available_count?: number;
  context_length?: number | null;
  error?: string | null;
}

export interface ChatModelsResponse {
  models: { provider: string; model: string }[];
  current: string;
}

export interface MemoryBlock {
  content: string;
  chars: number;
  limit: number;
}

export interface MemoryResponse {
  memory: MemoryBlock;
  user: MemoryBlock;
  provider: string;
  memory_enabled: boolean;
  user_profile_enabled: boolean;
}

export interface GuardrailsConfig {
  enabled: boolean;
  redact_pii: boolean;
  mode: "redact" | "block";
  llm_check: boolean;
}

export interface TraceRow {
  id: number;
  ts: number;
  platform: string;
  model: string;
  provider: string;
  tenant: string;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  tool_calls: number;
  status: "ok" | "partial" | "failed";
  user_preview: string;
  reply_preview: string;
  score: number | null;
  score_reason: string | null;
}

export interface TracesResponse {
  traces: TraceRow[];
  stats: {
    turns: number;
    success_rate: number;
    avg_latency_ms: number;
    total_tokens: number;
    avg_score: number | null;
    scored: number;
    hours: number;
  };
  eval_enabled: boolean;
}

export interface AgentTemplate {
  id: string;
  label: string;
  emoji: string;
  sector: string;
  description: string;
  rag: boolean;
  model: string;
}

export interface ApiClient {
  id: string;
  name: string;
  email: string;
  status: "active" | "suspended";
  model: string;
  persona: string;
  rag_enabled: boolean;
  daily_token_limit: number;
  plan: string;
  rpm: number;
  created_at: number;
  active_keys?: number;
  used_today?: number;
  turns_today?: number;
  limits?: { rpm: number; daily_token_limit: number };
}

export interface ClientProfileStatus {
  profile: string;
  api_port: number;
  pid: number | null;
  healthy: boolean;
  provisioned: boolean;
  autostart?: boolean;
}

export interface ApiKey {
  id: string;
  client_id: string;
  last4: string;
  status: "active" | "revoked";
  created_at: number;
  last_used_at: number | null;
}

export interface UsageBudgetStatus {
  daily_token_limit: number;
  budget_mode: "warn" | "block";
  used: number;
  over_budget: boolean;
  percent: number;
  enabled: boolean;
}

export interface UsageDay {
  date: string;
  input: number;
  output: number;
  total: number;
  turns: number;
}

export interface UsageResponse {
  today: UsageDay & { by_model?: Record<string, { input: number; output: number; turns: number }> };
  recent: { series: UsageDay[] };
  budget: UsageBudgetStatus;
}

export interface RagStatus {
  enabled: boolean;
  source: string;
  indexed: boolean;
  pages: number;
  chunks: number;
  built_at: number | null;
}

export interface AuxiliaryTaskAssignment {
  task: string;
  provider: string;
  model: string;
  base_url: string;
}

export interface AuxiliaryModelsResponse {
  tasks: AuxiliaryTaskAssignment[];
  main: { provider: string; model: string };
}

export interface ModelAssignmentRequest {
  scope: "main" | "auxiliary";
  provider: string;
  model: string;
  /** For auxiliary: task slot name, "" for all, "__reset__" to reset all. */
  task?: string;
}

export interface ModelAssignmentResponse {
  ok: boolean;
  scope?: string;
  provider?: string;
  model?: string;
  tasks?: string[];
  reset?: boolean;
}

// ── OAuth provider types ────────────────────────────────────────────────

export interface OAuthProviderStatus {
  logged_in: boolean;
  source?: string | null;
  source_label?: string | null;
  token_preview?: string | null;
  expires_at?: string | null;
  has_refresh_token?: boolean;
  last_refresh?: string | null;
  error?: string;
}

export interface OAuthProvider {
  id: string;
  name: string;
  /** "pkce" (browser redirect + paste code), "device_code" (show code + URL),
   *  or "external" (delegated to a separate CLI like Claude Code or Qwen). */
  flow: "pkce" | "device_code" | "external";
  cli_command: string;
  docs_url: string;
  status: OAuthProviderStatus;
}

export interface OAuthProvidersResponse {
  providers: OAuthProvider[];
}

/** Discriminated union — the shape of /start depends on the flow. */
export type OAuthStartResponse =
  | {
      session_id: string;
      flow: "pkce";
      auth_url: string;
      expires_in: number;
    }
  | {
      session_id: string;
      flow: "device_code";
      user_code: string;
      verification_url: string;
      expires_in: number;
      poll_interval: number;
    };

export interface OAuthSubmitResponse {
  ok: boolean;
  status: "approved" | "error";
  message?: string;
}

export interface OAuthPollResponse {
  session_id: string;
  status: "pending" | "approved" | "denied" | "expired" | "error";
  error_message?: string | null;
  expires_at?: number | null;
}

// ── Dashboard theme types ──────────────────────────────────────────────

export interface DashboardThemeSummary {
  description: string;
  label: string;
  name: string;
  /** Full theme definition for user themes; undefined for built-ins
   *  (which the frontend already has locally). */
  definition?: DashboardTheme;
}

export interface DashboardThemesResponse {
  active: string;
  themes: DashboardThemeSummary[];
}

// ── Dashboard plugin types ─────────────────────────────────────────────

export interface PluginManifestResponse {
  name: string;
  label: string;
  description: string;
  icon: string;
  version: string;
  tab: {
    path: string;
    position?: string;
    override?: string;
    hidden?: boolean;
  };
  slots?: string[];
  entry: string;
  css?: string | null;
  has_api: boolean;
  source: string;
}

export interface HubAgentPluginRow {
  name: string;
  version: string;
  description: string;
  source: string;
  runtime_status: "disabled" | "enabled" | "inactive";
  has_dashboard_manifest: boolean;
  dashboard_manifest: PluginManifestResponse | null;
  path: string;
  can_remove: boolean;
  can_update_git: boolean;
  auth_required: boolean;
  auth_command: string;
  user_hidden: boolean;
}

export interface PluginsHubProviders {
  memory_provider: string;
  memory_options: Array<{ name: string; description: string }>;
  context_engine: string;
  context_options: Array<{ name: string; description: string }>;
}

export interface PluginsHubResponse {
  plugins: HubAgentPluginRow[];
  orphan_dashboard_plugins: PluginManifestResponse[];
  providers: PluginsHubProviders;
}

export interface AgentPluginInstallRequest {
  identifier: string;
  force?: boolean;
  enable?: boolean;
}

export interface AgentPluginInstallResponse {
  ok: boolean;
  plugin_name?: string;
  warnings?: string[];
  missing_env?: string[];
  after_install_path?: string | null;
  enabled?: boolean;
  error?: string;
}

export interface AgentPluginUpdateResponse {
  ok: boolean;
  name?: string;
  output?: string;
  unchanged?: boolean;
  error?: string;
}

export interface PluginProvidersPutRequest {
  memory_provider?: string;
  context_engine?: string;
}

// ── Kanban ────────────────────────────────────────────────────────────────

export interface KanbanBoardSummary {
  slug: string;
  name: string;
  description: string;
  archived: boolean;
  by_status: Record<string, number>;
  is_current: boolean;
}

export interface KanbanBoardsResponse {
  boards: KanbanBoardSummary[];
  current: string;
}

export interface KanbanTask {
  id: string;
  title: string;
  body: string | null;
  assignee: string | null;
  status: string;
  priority: number;
  created_by: string | null;
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
  result: string | null;
  skills: string[];
  session_id: string | null;
}

export interface KanbanTasksResponse {
  board: string;
  tasks: KanbanTask[];
}

export interface KanbanTaskDetail {
  task: KanbanTask;
  parents: string[];
  children: string[];
  latest_summary: string | null;
  comments: { author: string; body: string; created_at: number }[];
  events: { kind: string; created_at: number }[];
}
