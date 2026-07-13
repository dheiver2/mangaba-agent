import type { DashboardTheme, ThemeTypography, ThemeLayout } from "./types";

/**
 * Built-in dashboard themes.
 *
 * Each theme defines its own palette, typography, and layout so switching
 * themes produces visible changes beyond just color — fonts, density, and
 * corner-radius all shift to match the theme's personality.
 *
 * Theme names must stay in sync with the backend's
 * `_BUILTIN_DASHBOARD_THEMES` list in `mangaba_cli/web_server.py`.
 */

// ---------------------------------------------------------------------------
// Shared typography / layout presets
// ---------------------------------------------------------------------------

/** Default system stack — neutral, safe fallback for every platform. */
const SYSTEM_SANS =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const SYSTEM_MONO =
  'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace';

/* Cascadia Code — fonte do editor do VS Code, usada também na UI.
   Fallback para Consolas (Windows) e SF Mono / Menlo (macOS). */
const VSCODE_SANS =
  '"Cascadia Code", Consolas, "SF Mono", Menlo, "Ubuntu Mono", monospace';
const VSCODE_MONO = VSCODE_SANS;

/* CDN fontsource — carrega Cascadia Code sem instalar nada localmente. */
const CASCADIA_URL =
  "https://cdn.jsdelivr.net/npm/@fontsource/cascadia-code@5.2.1/index.css";

const DEFAULT_TYPOGRAPHY: ThemeTypography = {
  fontSans: VSCODE_SANS,
  fontMono: VSCODE_MONO,
  fontUrl: CASCADIA_URL,
  baseSize: "13px",
  lineHeight: "1.5",
  letterSpacing: "0",
};

const DEFAULT_LAYOUT: ThemeLayout = {
  radius: "0.5rem",
  density: "comfortable",
};

// ---------------------------------------------------------------------------
// Themes
// ---------------------------------------------------------------------------

/**
 * Mangaba Brand Dark (modo noite) — paleta fiel ao logo vertical.
 *
 * Cores extraídas pixel-perfect do SVG oficial "Logo Vertical com bg.svg":
 *   background  #1A0C04  — espresso profundo (tom escuro de #403731)
 *   midground   #FFDFCC  — pêssego do gradiente de fundo do logo (#FFDFCC stop@1)
 *   accent      #FF7A1A  — laranja primário do gradiente da fruta (stop@0.623)
 *   yellow      #FFD83D  — amarelo dourado do logo (stop@0.199)
 *   green       #7BBF26  — verde Mangaba (stop@1 do gradiente da fruta)
 *   deep-green  #689924  — verde folha escuro (stop@0 do gradiente da folha)
 *
 * Contraste verificado (WCAG AA):
 *   #FFDFCC on #1A0C04 → 14.8:1 ✓  (texto primário)
 *   #C8956E on #1A0C04 →  5.1:1 ✓  (texto secundário/mutedForeground)
 *   #FF7A1A on #1A0C04 →  6.9:1 ✓  (accent/primary)
 */
export const defaultTheme: DashboardTheme = {
  name: "default",
  label: "Mangaba Noite",
  description: "Modo escuro — grafite quente com laranja da marca",
  palette: {
    background: { hex: "#1C1916", alpha: 1 },
    midground:  { hex: "#F3ECE0", alpha: 1 },
    foreground: { hex: "#F3ECE0", alpha: 0 },
    warmGlow:   "rgba(245, 132, 31, 0.16)",
    noiseOpacity: 0.5,
  },
  typography: DEFAULT_TYPOGRAPHY,
  layout: { radius: "0.5rem", density: "comfortable" },
  colorOverrides: {
    primary:           "#F5841F", // laranja manga
    primaryForeground: "#241A12",

    card:              "#262220",
    cardForeground:    "#F3ECE0",
    popover:           "#211D1A",
    popoverForeground: "#F3ECE0",

    secondary:          "#2C2825",
    secondaryForeground:"#F3ECE0",
    muted:              "#2C2825",
    mutedForeground:    "#A89F92",

    accent:            "#332E29",
    accentForeground:  "#F5A24B",

    border: "#3A332E",
    input:  "#3A332E",
    ring:   "#F5841F",

    success:              "#5BB535", // verde folha (mais claro p/ fundo escuro)
    warning:              "#E0962B",
    destructive:          "#E5544A",
    destructiveForeground:"#ffffff",
  },
};

/**
 * Mangaba Brand Light (modo dia) — versão clara fiel ao logo.
 *
 * Cores extraídas do SVG oficial:
 *   background  #FFFCF0  — creme claro (stop@0 do gradiente de fundo)
 *   midground   #3A2E28  — marrom escuro derivado de #403731
 *   accent      #D45E00  — laranja escurecido para contraste em fundo claro
 *
 * Contraste verificado (WCAG AA):
 *   #3A2E28 on #FFFCF0 → 13.2:1 ✓  (texto primário)
 *   #6B4A3A on #FFFCF0 →  5.4:1 ✓  (texto secundário)
 *   #D45E00 on #FFFCF0 →  4.7:1 ✓  (accent/primary)
 */
export const mangabaLightTheme: DashboardTheme = {
  name: "mangaba-light",
  label: "Mangaba Dia",
  description: "Modo claro — creme quente com laranja da marca",
  palette: {
    // Fundo creme + texto grafite quente (cores da logo mangaba.ai).
    background: { hex: "#FBF4E6", alpha: 1 },
    midground:  { hex: "#2E2C2A", alpha: 1 },
    foreground: { hex: "#2E2C2A", alpha: 0 },
    warmGlow:   "rgba(245, 132, 31, 0.10)",
    noiseOpacity: 0.18,
  },
  typography: DEFAULT_TYPOGRAPHY,
  layout: { radius: "0.5rem", density: "comfortable" },
  colorOverrides: {
    primary:           "#F5841F", // laranja manga / ".ai"
    primaryForeground: "#2A2018", // grafite sobre laranja (legível)

    card:              "#FFFFFF",
    cardForeground:    "#2E2C2A",
    popover:           "#FFFDF7",
    popoverForeground: "#2E2C2A",

    secondary:          "#F3EBDB",
    secondaryForeground:"#2E2C2A",
    muted:              "#F1E9D8",
    mutedForeground:    "#6E655A",

    accent:            "#F6EEDF",
    accentForeground:  "#8A4A12",

    border: "#E8DEC9",
    input:  "#E8DEC9",
    ring:   "#F5841F",

    success:              "#4CA32E", // verde folha
    warning:              "#D98A1F",
    destructive:          "#CC3A2E",
    destructiveForeground:"#ffffff",
  },
};

/**
 * Mangaba Pro — dark admin no padrão shadcn/ui (zinc-950, cards elevados,
 * bordas sutis, cantos bem arredondados), com o laranja da marca como acento.
 *
 * Contraste verificado (WCAG AA):
 *   #FAFAFA on #09090B → 19.2:1 ✓  (texto primário)
 *   #9B9BA4 on #09090B →  7.1:1 ✓  (texto secundário/mutedForeground)
 *   #F5841F on #09090B →  8.0:1 ✓  (accent/primary)
 */
export const mangabaProTheme: DashboardTheme = {
  name: "mangaba-pro",
  label: "Mangaba Pro",
  description: "Dark admin — zinc profundo, cards elevados e laranja da marca",
  palette: {
    background: { hex: "#09090B", alpha: 1 },
    midground:  { hex: "#FAFAFA", alpha: 1 },
    foreground: { hex: "#FFFFFF", alpha: 0 },
    warmGlow:   "rgba(245, 132, 31, 0.10)",
    noiseOpacity: 0.2,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Inter", ${SYSTEM_SANS}`,
    fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
    fontDisplay: `"Inter", ${SYSTEM_SANS}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap",
    letterSpacing: "-0.01em",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.85rem",
  },
  colorOverrides: {
    primary:           "#F5841F",
    primaryForeground: "#160D06",

    card:              "#121215",
    cardForeground:    "#FAFAFA",
    popover:           "#0E0E11",
    popoverForeground: "#FAFAFA",

    secondary:          "#1A1A1F",
    secondaryForeground:"#FAFAFA",
    muted:              "#1A1A1F",
    mutedForeground:    "#9B9BA4",

    accent:            "#1F1F25",
    accentForeground:  "#F5A24B",

    border: "#232329",
    input:  "#232329",
    ring:   "#F5841F",

    success:              "#4ADE80",
    warning:              "#FBBF24",
    destructive:          "#EF4444",
    destructiveForeground:"#ffffff",
  },
};

export const midnightTheme: DashboardTheme = {
  name: "midnight",
  label: "Midnight",
  description: "Deep blue-violet with cool accents",
  palette: {
    background: { hex: "#0a0a1f", alpha: 1 },
    midground: { hex: "#d4c8ff", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(167, 139, 250, 0.32)",
    noiseOpacity: 0.8,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Inter", ${SYSTEM_SANS}`,
    fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap",
    letterSpacing: "-0.005em",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.75rem",
  },
};

export const emberTheme: DashboardTheme = {
  name: "ember",
  label: "Ember",
  description: "Warm crimson and bronze — forge vibes",
  palette: {
    background: { hex: "#1a0a06", alpha: 1 },
    midground: { hex: "#ffd8b0", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 115, 22, 0.38)",
    noiseOpacity: 1,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Spectral", Georgia, "Times New Roman", serif`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Spectral:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0.25rem",
  },
  colorOverrides: {
    destructive: "#c92d0f",
    warning: "#f97316",
  },
};

export const monoTheme: DashboardTheme = {
  name: "mono",
  label: "Mono",
  description: "Clean grayscale — minimal and focused",
  palette: {
    background: { hex: "#0e0e0e", alpha: 1 },
    midground: { hex: "#eaeaea", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255, 255, 255, 0.1)",
    noiseOpacity: 0.6,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"IBM Plex Sans", ${SYSTEM_SANS}`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
};

export const cyberpunkTheme: DashboardTheme = {
  name: "cyberpunk",
  label: "Cyberpunk",
  description: "Neon green on black — matrix terminal",
  palette: {
    background: { hex: "#040608", alpha: 1 },
    midground: { hex: "#9bffcf", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(0, 255, 136, 0.22)",
    noiseOpacity: 1.2,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontMono: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=JetBrains+Mono:wght@400;700&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "0",
  },
  colorOverrides: {
    success: "#00ff88",
    warning: "#ffd700",
    destructive: "#ff0055",
  },
};

export const roseTheme: DashboardTheme = {
  name: "rose",
  label: "Rosé",
  description: "Soft pink and warm ivory — easy on the eyes",
  palette: {
    background: { hex: "#1a0f15", alpha: 1 },
    midground: { hex: "#ffd4e1", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 168, 212, 0.3)",
    noiseOpacity: 0.9,
  },
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    fontSans: `"Fraunces", Georgia, serif`,
    fontMono: `"DM Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=DM+Mono:wght@400;500&display=swap",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    radius: "1rem",
  },
};

/**
 * Same look as ``defaultTheme`` but with a larger root font size, looser
 * line-height, and ``spacious`` density so every rem-based size in the
 * dashboard scales up. For users who find the default 15px UI too dense.
 */
export const defaultLargeTheme: DashboardTheme = {
  name: "default-large",
  label: "Mangaba Noite (Grande)",
  description: "Mangaba Noite com fontes maiores e espaçamento confortável",
  palette: defaultTheme.palette,
  typography: {
    ...DEFAULT_TYPOGRAPHY,
    baseSize: "18px",
    lineHeight: "1.65",
  },
  layout: {
    ...DEFAULT_LAYOUT,
    density: "spacious",
  },
};

/**
 * Claude AI / Mangaba — Official brand palette.
 *
 * Values sourced pixel-perfect from:
 *   mangaba.chat · mangaba-voice.tech · mangabapixel.online
 *
 *   --bg          #1C1917  warm near-black canvas
 *   --accent      #D97757  coral / terracotta — the brand signature hue
 *   --surface     #2D2A28  elevated surfaces (cards)
 *   --sidebar     #252220  sidebar / secondary surface
 *   --border      #3A3430  subtle warm border
 *   --leaf        #6B9540  Mangaba green — secondary accent
 *   Font: Inter (same as mangaba.chat / claude.ai)
 */
export const claudeTheme: DashboardTheme = {
  name: "claude",
  label: "Claude AI",
  description: "Paleta oficial Mangaba/Claude — coral & cream em fundo escuro quente",
  palette: {
    /* Exact --bg value used across all three Mangaba sites. */
    background: { hex: "#1C1917", alpha: 1 },
    /* Warm cream — hsl(48 23% 95%) from mangaba.chat --foreground dark mode. */
    midground:  { hex: "#F5EEE4", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    /* Coral glow matching --accent #D97757. */
    warmGlow:   "rgba(217, 119, 87, 0.28)",
    noiseOpacity: 0.65,
  },
  typography: {
    /* Inter — exact font used on mangaba.chat & mangaba-voice.tech.
       Weight range matches their Google Fonts import (300–700). */
    fontSans:
      '"Inter", system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    fontMono:
      '"JetBrains Mono", ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace',
    fontDisplay:
      '"Inter", system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Inter:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&family=JetBrains+Mono:wght@400;500;700&display=swap",
    baseSize: "15px",
    lineHeight: "1.6",
    letterSpacing: "-0.01em",
  },
  layout: {
    /* --radius:0.5rem from mangaba.chat globals. */
    radius: "0.5rem",
    density: "comfortable",
  },
  colorOverrides: {
    /* ── Primary ─────────────────────────────────────────────────────── */
    /* --accent: #D97757 (mangabapixel & mangaba.chat --primary dark). */
    primary:            "#D97757",
    primaryForeground:  "#ffffff",

    /* ── Surfaces ────────────────────────────────────────────────────── */
    /* --surface: #2D2A28 · --sidebar: #252220 */
    card:               "#2D2A28",
    cardForeground:     "#F5EEE4",
    popover:            "#252220",
    popoverForeground:  "#F5EEE4",

    /* ── Secondary / muted ───────────────────────────────────────────── */
    secondary:          "#252220",
    secondaryForeground:"#C4B9B0",
    muted:              "#252220",
    /* hsl(30 6% 64%) from mangaba.chat --muted-foreground dark. */
    mutedForeground:    "#A49890",

    /* ── Accent tints ────────────────────────────────────────────────── */
    /* --accent-soft: #2E1F18 (mangabapixel soft coral bg). */
    accent:             "#2E1F18",
    accentForeground:   "#D97757",

    /* ── Borders & ring ──────────────────────────────────────────────── */
    /* --border: #3A3430 · --hover: #302C28 */
    border:             "#3A3430",
    input:              "#3A3430",
    ring:               "#D97757",

    /* ── Status ──────────────────────────────────────────────────────── */
    destructive:            "#E05C3A",
    destructiveForeground:  "#ffffff",
    /* --leaf: #6B9540 — Mangaba green for success states. */
    success:            "#6B9540",
    warning:            "#E6A135",
  },
};

export const BUILTIN_THEMES: Record<string, DashboardTheme> = {
  "mangaba-pro": mangabaProTheme,
  default: defaultTheme,
  "mangaba-light": mangabaLightTheme,
  "default-large": defaultLargeTheme,
  claude: claudeTheme,
  midnight: midnightTheme,
  ember: emberTheme,
  mono: monoTheme,
  cyberpunk: cyberpunkTheme,
  rose: roseTheme,
};
