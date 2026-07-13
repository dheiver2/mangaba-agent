import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  ChevronDown,
  Pencil,
  Terminal,
  Trash2,
  Users,
  X,
} from "lucide-react";
import spinners from "unicode-animations";
import { H2 } from "@/components/NouiTypography";
import { api } from "@/lib/api";
import type { ProfileInfo, AgentTemplate } from "@/lib/api";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { useToast } from "@/hooks/useToast";
import { useConfirmDelete } from "@/hooks/useConfirmDelete";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { Toast } from "@/components/Toast";
import { Card, CardContent } from "@/components/ui/card";
import { ModelPickerDialog } from "@/components/ModelPickerDialog";
import { Badge } from "@dheiver2/ui/ui/components/badge";
import { Button } from "@dheiver2/ui/ui/components/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@dheiver2/ui/ui/components/checkbox";
import { useI18n } from "@/i18n";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn, themedBody } from "@/lib/utils";

// Mirrors mangaba_cli/profiles.py::_PROFILE_ID_RE so we can reject obviously
// invalid names (uppercase, spaces, …) before round-tripping a doomed POST.
const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

/** Braille unicode spinner (`unicode-animations`); static first frame when reduced motion is preferred. */
function ProfilesLoadingSpinner() {
  const { frames, interval } = spinners.braille;
  const [frameIndex, setFrameIndex] = useState(0);

  useEffect(() => {
    if (
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      return;
    }
    const id = window.setInterval(
      () => setFrameIndex((i) => (i + 1) % frames.length),
      interval,
    );
    return () => window.clearInterval(id);
  }, [frames.length, interval]);

  return (
    <span
      aria-hidden
      className="inline-block select-none font-mono text-xl leading-none text-muted-foreground"
    >
      {frames[frameIndex]}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Organograma: infere a hierarquia pelos nomes dos profiles.
//   default            → orquestrador (Diretor) no topo
//   <area>             → gerente da área (nó nível 2)
//   <area>-<especial.> → especialista (nível 3, filho de <area>)
// ---------------------------------------------------------------------------
interface OrgProfile {
  name: string;
  is_default?: boolean;
  model?: string | null;
  provider?: string | null;
}

function OrgNode({ p, label, kind }: { p?: OrgProfile; label?: string; kind: "root" | "area" | "leaf" }) {
  const ring =
    kind === "root"
      ? "border-primary/50 bg-primary/5"
      : kind === "area"
        ? "border-primary/25"
        : "border-border";
  return (
    <div className={`inline-flex flex-col gap-0.5 rounded-lg border ${ring} bg-card px-3 py-1.5`}>
      <span className="text-sm font-semibold text-foreground">
        {p?.name ?? label}
      </span>
      <span className="text-[11px] text-muted-foreground">
        {kind === "root"
          ? "Orquestrador"
          : p?.model
            ? p.model.split("/").slice(-1)[0]
            : "modelo padrão"}
      </span>
    </div>
  );
}

function OrgChart({ profiles }: { profiles: OrgProfile[] }) {
  const root = profiles.find((p) => p.is_default);
  const groups = new Map<string, { manager?: OrgProfile; children: OrgProfile[] }>();
  const leaves: OrgProfile[] = [];

  for (const p of profiles) {
    if (p.is_default) continue;
    const i = p.name.indexOf("-");
    if (i > 0) {
      const prefix = p.name.slice(0, i);
      const g = groups.get(prefix) ?? { children: [] };
      g.children.push(p);
      groups.set(prefix, g);
    } else {
      const g = groups.get(p.name) ?? { children: [] };
      g.manager = p;
      groups.set(p.name, g);
    }
  }
  // Áreas sem nenhum filho e sem nome composto viram folhas diretas do topo.
  for (const [key, g] of [...groups.entries()]) {
    if (g.manager && g.children.length === 0) {
      leaves.push(g.manager);
      groups.delete(key);
    }
  }

  const areas = [...groups.entries()];

  return (
    <Card>
      <CardContent className="overflow-x-auto p-4">
        <div className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Organograma dos agentes
        </div>
        <div className="flex flex-col items-start gap-3">
          <OrgNode p={root} label="Diretor" kind="root" />
          {(areas.length > 0 || leaves.length > 0) && (
            <div className="ml-4 flex flex-col gap-3 border-l border-primary/20 pl-5">
              {areas.map(([prefix, g]) => (
                <div key={prefix} className="flex flex-col gap-2">
                  <OrgNode p={g.manager} label={prefix} kind="area" />
                  {g.children.length > 0 && (
                    <div className="ml-3 flex flex-wrap gap-2 border-l border-border pl-4">
                      {g.children.map((c) => (
                        <OrgNode key={c.name} p={c} kind="leaf" />
                      ))}
                    </div>
                  )}
                </div>
              ))}
              {leaves.map((l) => (
                <OrgNode key={l.name} p={l} kind="leaf" />
              ))}
            </div>
          )}
        </div>
        <p className="mt-3 text-[11px] text-muted-foreground">
          Hierarquia inferida pelos nomes: <code>area</code> = gerente,{" "}
          <code>area-especialista</code> = especialista. <code>default</code> é o orquestrador.
        </p>
      </CardContent>
    </Card>
  );
}

export default function ProfilesPage() {
  const [profiles, setProfiles] = useState<ProfileInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const { toast, showToast } = useToast();
  const { t } = useI18n();
  const { setEnd } = usePageHeader();

  // Create modal
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [cloneFromDefault, setCloneFromDefault] = useState(true);
  const [creating, setCreating] = useState(false);
  const closeCreateModal = useCallback(() => setCreateModalOpen(false), []);
  const createModalRef = useModalBehavior({
    open: createModalOpen,
    onClose: closeCreateModal,
  });

  // Inline rename state
  const [renamingFrom, setRenamingFrom] = useState<string | null>(null);
  const [renameTo, setRenameTo] = useState("");

  // Inline SOUL editor state
  const [editingSoulFor, setEditingSoulFor] = useState<string | null>(null);
  const [soulText, setSoulText] = useState("");
  const [soulSaving, setSoulSaving] = useState(false);

  // Picker de provedor+modelo por profile (sem trocar o profile ativo).
  const [pickingModelFor, setPickingModelFor] = useState<string | null>(null);

  // Catálogo de agentes verticais prontos (templates).
  const [templates, setTemplates] = useState<AgentTemplate[]>([]);
  const [installing, setInstalling] = useState<string | null>(null);
  // Tracks the latest SOUL request so out-of-order responses don't overwrite
  // newer state when the user switches profiles or closes the editor.
  const activeSoulRequest = useRef<string | null>(null);

  const load = useCallback(() => {
    api
      .getProfiles()
      .then((res) => setProfiles(res.profiles))
      .catch((e) => showToast(`${t.status.error}: ${e}`, "error"))
      .finally(() => setLoading(false));
  }, [showToast, t.status.error]);

  const applyProfileModel = async (
    name: string,
    args: { provider: string; model: string },
  ) => {
    await api.setProfileModel(name, args.model, args.provider);
    showToast(
      `Modelo de "${name}" salvo. Reinicie o gateway dele na Frota para aplicar.`,
      "success",
    );
    load();
  };

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    api.getAgentTemplates().then((r) => setTemplates(r.templates)).catch(() => {});
  }, []);

  const installTemplate = async (tpl: AgentTemplate) => {
    setInstalling(tpl.id);
    try {
      const r = await api.installAgentTemplate(tpl.id);
      showToast(`Agente "${r.name}" instalado.`, "success");
      load();
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setInstalling(null);
    }
  };

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) {
      showToast(t.profiles.nameRequired, "error");
      return;
    }
    if (!PROFILE_NAME_RE.test(name)) {
      showToast(`${t.profiles.invalidName}: ${t.profiles.nameRule}`, "error");
      return;
    }
    setCreating(true);
    try {
      await api.createProfile({ name, clone_from_default: cloneFromDefault });
      showToast(`${t.profiles.created}: ${name}`, "success");
      setNewName("");
      setCreateModalOpen(false);
      load();
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setCreating(false);
    }
  };

  const handleRenameSubmit = async () => {
    if (!renamingFrom) return;
    const target = renameTo.trim();
    if (!target || target === renamingFrom) {
      setRenamingFrom(null);
      setRenameTo("");
      return;
    }
    if (!PROFILE_NAME_RE.test(target)) {
      showToast(`${t.profiles.invalidName}: ${t.profiles.nameRule}`, "error");
      return;
    }
    try {
      await api.renameProfile(renamingFrom, target);
      showToast(
        `${t.profiles.renamed}: ${renamingFrom} → ${target}`,
        "success",
      );
      setRenamingFrom(null);
      setRenameTo("");
      load();
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    }
  };

  const openSoulEditor = useCallback(
    async (name: string) => {
      if (editingSoulFor === name) {
        activeSoulRequest.current = null;
        setEditingSoulFor(null);
        return;
      }
      setEditingSoulFor(name);
      setSoulText("");
      activeSoulRequest.current = name;
      try {
        const soul = await api.getProfileSoul(name);
        if (activeSoulRequest.current === name) {
          setSoulText(soul.content);
        }
      } catch (e) {
        if (activeSoulRequest.current === name) {
          showToast(`${t.status.error}: ${e}`, "error");
        }
      }
    },
    [editingSoulFor, showToast, t.status.error],
  );

  const handleSaveSoul = async (name: string) => {
    setSoulSaving(true);
    try {
      await api.updateProfileSoul(name, soulText);
      showToast(`${t.profiles.soulSaved}: ${name}`, "success");
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
    } finally {
      setSoulSaving(false);
    }
  };

  const handleCopyTerminalCommand = async (name: string) => {
    let cmd: string;
    try {
      const res = await api.getProfileSetupCommand(name);
      cmd = res.command;
    } catch (e) {
      showToast(`${t.status.error}: ${e}`, "error");
      return;
    }
    try {
      await navigator.clipboard.writeText(cmd);
      showToast(`${t.profiles.commandCopied}: ${cmd}`, "success");
    } catch {
      showToast(`${t.profiles.copyFailed}: ${cmd}`, "error");
    }
  };

  const profileDelete = useConfirmDelete<string>({
    onDelete: useCallback(
      async (name: string) => {
        try {
          await api.deleteProfile(name);
          showToast(`${t.profiles.deleted}: ${name}`, "success");
          load();
        } catch (e) {
          showToast(`${t.status.error}: ${e}`, "error");
          throw e;
        }
      },
      [load, showToast, t.profiles.deleted, t.status.error],
    ),
  });

  const pendingName = profileDelete.pendingId;

  // Put "Create" button in page header
  useLayoutEffect(() => {
    setEnd(
      <Button
        className="uppercase"
        size="sm"
        onClick={() => setCreateModalOpen(true)}
      >
        {t.common.create}
      </Button>,
    );
    return () => {
      setEnd(null);
    };
  }, [setEnd, t.common.create, loading]);

  if (loading) {
    return (
      <div
        aria-busy="true"
        aria-live="polite"
        className="flex items-center justify-center py-24"
      >
        <span className="sr-only">{t.common.loading}</span>

        <ProfilesLoadingSpinner />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      {templates.length > 0 && (
        <Card>
          <CardContent className="p-4">
            <div className="mb-1 text-sm font-semibold text-foreground">
              Comece com um agente pronto
            </div>
            <p className="mb-3 text-xs text-muted-foreground">
              Agentes verticais já configurados (persona + base de conhecimento). Instale em 1 clique — vira um perfil que você pode ajustar.
            </p>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {templates.map((tpl) => (
                <div
                  key={tpl.id}
                  className="flex flex-col gap-2 rounded-lg border border-border bg-background p-3"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-xl" aria-hidden>
                      {tpl.emoji}
                    </span>
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium text-foreground">
                        {tpl.label}
                      </div>
                      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                        {tpl.sector}
                      </div>
                    </div>
                  </div>
                  <p className="line-clamp-2 text-xs text-muted-foreground">
                    {tpl.description}
                  </p>
                  <Button
                    size="sm"
                    className="mt-auto"
                    disabled={installing === tpl.id}
                    onClick={() => installTemplate(tpl)}
                  >
                    {installing === tpl.id ? "Instalando…" : "Instalar"}
                  </Button>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      <DeleteConfirmDialog
        open={profileDelete.isOpen}
        onCancel={profileDelete.cancel}
        onConfirm={profileDelete.confirm}
        title={t.profiles.confirmDeleteTitle}
        description={
          pendingName
            ? t.profiles.confirmDeleteMessage.replace("{name}", pendingName)
            : t.profiles.confirmDeleteMessage
        }
        loading={profileDelete.isDeleting}
      />

      {/* Create profile modal */}
      {createModalOpen && (
        <div
          ref={createModalRef}
          className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 backdrop-blur-sm p-4"
          onClick={(e) =>
            e.target === e.currentTarget && setCreateModalOpen(false)
          }
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-profile-title"
        >
          <div className={cn(themedBody, "relative w-full max-w-md border border-border bg-card shadow-2xl flex flex-col")}>
            <Button
              ghost
              size="icon"
              onClick={() => setCreateModalOpen(false)}
              className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
              aria-label="Close"
            >
              <X />
            </Button>

            <header className="p-5 pb-3 border-b border-border">
              <h2
                id="create-profile-title"
                className="font-mondwest text-display text-base tracking-wider"
              >
                {t.profiles.newProfile}
              </h2>
            </header>

            <div className="p-5 grid gap-4">
              <div className="grid gap-2">
                <Label htmlFor="profile-name">{t.profiles.name}</Label>
                <Input
                  id="profile-name"
                  autoFocus
                  placeholder={t.profiles.namePlaceholder}
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleCreate();
                  }}
                  aria-invalid={
                    newName.trim() !== "" &&
                    !PROFILE_NAME_RE.test(newName.trim())
                  }
                />
                <p className="text-xs text-muted-foreground">
                  {t.profiles.nameRule}
                </p>
              </div>

              <div className="flex items-center gap-2.5">
                <Checkbox
                  checked={cloneFromDefault}
                  id="clone-from-default"
                  onCheckedChange={(checked) =>
                    setCloneFromDefault(checked === true)
                  }
                />

                <Label
                  className="font-mondwest normal-case tracking-normal text-sm cursor-pointer"
                  htmlFor="clone-from-default"
                >
                  {t.profiles.cloneFromDefault}
                </Label>
              </div>

              <div className="flex justify-end">
                <Button
                  className="uppercase"
                  size="sm"
                  onClick={handleCreate}
                  disabled={creating}
                >
                  {creating ? t.common.creating : t.common.create}
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* List */}
      <div className="flex flex-col gap-3">
        <H2
          variant="sm"
          className="flex items-center gap-2 text-muted-foreground"
        >
          <Users className="h-4 w-4" />
          {t.profiles.allProfiles} ({profiles.length})
        </H2>

        {profiles.length > 1 && <OrgChart profiles={profiles} />}

        {profiles.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              {t.profiles.noProfiles}
            </CardContent>
          </Card>
        )}

        {profiles.map((p) => {
          const isRenaming = renamingFrom === p.name;
          const isEditingSoul = editingSoulFor === p.name;
          return (
            <Card key={p.name}>
              <CardContent className="flex items-start gap-4 py-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1 flex-wrap">
                    {isRenaming ? (
                      <Input
                        autoFocus
                        value={renameTo}
                        onChange={(e) => setRenameTo(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleRenameSubmit();
                          if (e.key === "Escape") setRenamingFrom(null);
                        }}
                        aria-invalid={
                          renameTo.trim() !== "" &&
                          renameTo.trim() !== p.name &&
                          !PROFILE_NAME_RE.test(renameTo.trim())
                        }
                        className="max-w-xs"
                      />
                    ) : (
                      <span className="font-medium text-sm truncate">
                        {p.name}
                      </span>
                    )}
                    {p.is_default && (
                      <Badge tone="secondary">{t.profiles.defaultBadge}</Badge>
                    )}
                    {p.has_env && (
                      <Badge tone="outline">{t.profiles.hasEnv}</Badge>
                    )}
                  </div>
                  {isRenaming &&
                    (() => {
                      const trimmed = renameTo.trim();
                      const invalid =
                        trimmed !== "" &&
                        trimmed !== p.name &&
                        !PROFILE_NAME_RE.test(trimmed);
                      return (
                        <p
                          className={
                            "text-xs mb-1 " +
                            (invalid
                              ? "text-destructive"
                              : "text-muted-foreground")
                          }
                        >
                          {invalid
                            ? `${t.profiles.invalidName}: ${t.profiles.nameRule}`
                            : t.profiles.nameRule}
                        </p>
                      );
                    })()}
                  <div className="flex items-center gap-4 text-xs text-muted-foreground flex-wrap">
                    <button
                      type="button"
                      onClick={() => setPickingModelFor(p.name)}
                      className="inline-flex items-center gap-1 rounded px-1 -mx-1 hover:bg-muted/50 hover:text-foreground"
                      title="Editar modelo deste agente"
                    >
                      {t.profiles.model}: {p.model || "padrão"}
                      {p.provider ? ` (${p.provider})` : ""}
                      <Pencil className="h-3 w-3 opacity-60" />
                    </button>
                    <span>
                      {t.profiles.skills}: {p.skill_count}
                    </span>
                    <span className="font-mono truncate max-w-[28rem]">
                      {p.path}
                    </span>
                  </div>

                  {pickingModelFor === p.name && (
                    <ModelPickerDialog
                      title={`Modelo de ${p.name}`}
                      alwaysGlobal
                      loader={api.getModelOptions}
                      onApply={(args) => applyProfileModel(p.name, args)}
                      onClose={() => setPickingModelFor(null)}
                    />
                  )}
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  {isRenaming ? (
                    <>
                      <Button size="sm" onClick={handleRenameSubmit}>
                        {t.common.save}
                      </Button>
                      <Button
                        size="sm"
                        ghost
                        onClick={() => setRenamingFrom(null)}
                      >
                        {t.common.cancel}
                      </Button>
                    </>
                  ) : (
                    <>
                      <Button
                        ghost
                        size="icon"
                        title={t.profiles.editSoul}
                        aria-label={t.profiles.editSoul}
                        onClick={() => openSoulEditor(p.name)}
                      >
                        {isEditingSoul ? (
                          <ChevronDown className="h-4 w-4" />
                        ) : (
                          <span aria-hidden className="text-xs font-bold">
                            S
                          </span>
                        )}
                      </Button>
                      <Button
                        ghost
                        size="icon"
                        title={t.profiles.openInTerminal}
                        aria-label={t.profiles.openInTerminal}
                        onClick={() => handleCopyTerminalCommand(p.name)}
                      >
                        <Terminal className="h-4 w-4" />
                      </Button>
                      {!p.is_default && (
                        <Button
                          ghost
                          size="icon"
                          title={t.profiles.rename}
                          aria-label={t.profiles.rename}
                          onClick={() => {
                            setRenamingFrom(p.name);
                            setRenameTo(p.name);
                          }}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                      )}
                      {!p.is_default && (
                        <Button
                          ghost
                          size="icon"
                          title={t.common.delete}
                          aria-label={t.common.delete}
                          onClick={() => profileDelete.requestDelete(p.name)}
                        >
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      )}
                    </>
                  )}
                </div>
              </CardContent>

              {isEditingSoul && (
                <div className="border-t border-border px-4 pb-4 pt-3 flex flex-col gap-2">
                  <Label
                    htmlFor={`soul-editor-${p.name}`}
                    className="flex items-center gap-2 font-mondwest text-display text-xs tracking-wider text-muted-foreground"
                  >
                    {t.profiles.soulSection}
                  </Label>
                  <textarea
                    id={`soul-editor-${p.name}`}
                    className="flex min-h-[180px] w-full border border-input bg-transparent px-3 py-2 text-sm font-mono shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    placeholder={t.profiles.soulPlaceholder}
                    value={soulText}
                    onChange={(e) => setSoulText(e.target.value)}
                  />
                  <div>
                    <Button
                      size="sm"
                      className="uppercase"
                      onClick={() => handleSaveSoul(p.name)}
                      disabled={soulSaving}
                    >
                      {soulSaving ? t.common.saving : t.common.save}
                    </Button>
                  </div>
                </div>
              )}
            </Card>
          );
        })}
      </div>
    </div>
  );
}
