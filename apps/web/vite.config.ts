import path from "node:path";
import { createRequire } from "node:module";
import { defineConfig, normalizePath } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { viteStaticCopy } from "vite-plugin-static-copy";

const require = createRequire(import.meta.url);
const pdfjsDirectory = path.dirname(require.resolve("pdfjs-dist/package.json"));

export default defineConfig({
  plugins: [react(), tailwindcss(), viteStaticCopy({
    targets: ["cmaps", "standard_fonts", "wasm"].map((directory) => ({
      src: normalizePath(path.join(pdfjsDirectory, directory, "*")),
      dest: `pdfjs/${directory}`,
      rename: { stripBase: true },
    })),
  })],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
