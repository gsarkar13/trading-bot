import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built files go straight into the Python package; FastAPI serves them (D8, no Node on the server).
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../app/api/static", emptyOutDir: true, sourcemap: false },
  server: { proxy: { "/api": "http://127.0.0.1:8600", "/ws": { target: "ws://127.0.0.1:8600", ws: true } } },
});
