import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// La SPA se sirve SAME-ORIGIN desde FastAPI (v1/api) en producción: build a
// `dist/` (que FastAPI monta). En desarrollo, proxy de `/api` (incluido el WS)
// al Uvicorn local para conservar el modelo same-origin sin CORS.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
