import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";


function keepEmbedPlaceholder(): Plugin {
  return {
    name: "nonoka-keep-embed-placeholder",
    closeBundle() {
      writeFileSync(resolve(import.meta.dirname, "dist/.gitkeep"), "");
    },
  };
}

export default defineConfig({
  base: "./",
  plugins: [react(), keepEmbedPlaceholder()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: {
        index: resolve(import.meta.dirname, "index.html"),
        editor: resolve(import.meta.dirname, "editor.html"),
        export: resolve(import.meta.dirname, "export.html"),
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 9245,
    strictPort: true,
  },
});
