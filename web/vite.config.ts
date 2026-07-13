import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

const BACKEND = process.env.MANGABA_DASHBOARD_URL ?? "http://127.0.0.1:9119";

/**
 * In production the Python `mangaba dashboard` server injects a one-shot
 * session token into `index.html` (see `mangaba_cli/web_server.py`). The
 * Vite dev server serves its own `index.html`, so unless we forward that
 * token, every protected `/api/*` call 401s.
 *
 * This plugin fetches the running dashboard's `index.html` on each dev page
 * load, scrapes the `window.__MANGABA_SESSION_TOKEN__` assignment, and
 * re-injects it into the dev HTML. No-op in production builds.
 */
function mangabaDevToken(): Plugin {
  const TOKEN_RE = /window\.__MANGABA_SESSION_TOKEN__\s*=\s*"([^"]+)"/;
  const EMBEDDED_RE =
    /window\.__MANGABA_DASHBOARD_EMBEDDED_CHAT__\s*=\s*(true|false)/;
  const LEGACY_TUI_RE =
    /window\.__MANGABA_DASHBOARD_TUI__\s*=\s*(true|false)/;

  return {
    name: "mangaba:dev-session-token",
    apply: "serve",
    async transformIndexHtml() {
      try {
        const res = await fetch(BACKEND, { headers: { accept: "text/html" } });
        const html = await res.text();
        const match = html.match(TOKEN_RE);
        if (!match) {
          console.warn(
            `[mangaba] Could not find session token in ${BACKEND} — ` +
              `is \`mangaba dashboard\` running? /api calls will 401.`,
          );
          return;
        }
        const embeddedMatch = html.match(EMBEDDED_RE);
        const legacyMatch = html.match(LEGACY_TUI_RE);
        const embeddedJs = embeddedMatch
          ? embeddedMatch[1]
          : legacyMatch
            ? legacyMatch[1]
            : "false";
        return [
          {
            tag: "script",
            injectTo: "head",
            children:
              `window.__MANGABA_SESSION_TOKEN__="${match[1]}";` +
              `window.__MANGABA_DASHBOARD_EMBEDDED_CHAT__=${embeddedJs};`,
          },
        ];
      } catch (err) {
        console.warn(
          `[mangaba] Dashboard at ${BACKEND} unreachable — ` +
            `start it with \`mangaba dashboard\` or set MANGABA_DASHBOARD_URL. ` +
            `(${(err as Error).message})`,
        );
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), mangabaDevToken()],
  resolve: {
    alias: {
      // Vendored local UI shim (replaces the unpublished @dheiver2/ui npm
      // package). Keep this BEFORE the "@" entry so it matches first.
      "@dheiver2/ui": path.resolve(__dirname, "./src/vendor/dheiver2-ui"),
      "@": path.resolve(__dirname, "./src"),
    },
    // When @dheiver2/ui is symlinked via `file:../../design-language`,
    // Node's module resolution would pick up shared deps from
    // design-language/node_modules/*, giving us two copies + breaking
    // hooks (useRef-of-null), webgl contexts, etc. Force everything that
    // exists in BOTH places to use the dashboard's copy.
    //
    // Don't list packages here that only exist in the DS (nanostores,
    // @nanostores/react) — Vite dedupe errors out when it can't find
    // them at the project root.
    dedupe: [
      "react",
      "react-dom",
    ],
  },
  build: {
    outDir: "../mangaba_cli/web_dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // Libs grandes em chunks próprios: cacheáveis entre deploys (mudam
        // raramente) e fora do bundle de entrada.
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          motion: ["motion"],
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": {
        target: BACKEND,
        ws: true,
      },
      // Same host as `mangaba dashboard` must serve these; Vite has no
      // dashboard-plugins/* files, so without this, plugin scripts 404
      // or receive index.html in dev.
      "/dashboard-plugins": BACKEND,
    },
  },
});
