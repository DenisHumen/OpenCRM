import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// dev: SPA на 5173, API и медиа проксируются в FastAPI на 8000
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/media": "http://localhost:8000",
      "/branding": "http://localhost:8000",
      "/b": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
