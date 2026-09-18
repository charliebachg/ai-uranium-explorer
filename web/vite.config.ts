/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { maplibreVendor } from "./plugins/maplibreVendor.ts";

export default defineConfig({
  plugins: [react(), tailwindcss(), maplibreVendor()],
  resolve: { alias: { "@": "/src" } },
  optimizeDeps: { exclude: ["maplibre-gl"] },
  server: { port: 5173, strictPort: true },
  preview: { port: 4173, strictPort: true },
  build: { target: "es2022", chunkSizeWarningLimit: 900 },
  test: {
    environment: "happy-dom",
    include: ["tests/unit/**/*.test.ts", "tests/unit/**/*.test.tsx"],
    setupFiles: ["src/test/setup.ts"],
  },
});
