import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the API under /app. In dev, API calls are proxied to the compose API port.
export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: { proxy: { "/v1": "http://127.0.0.1:18000" } },
});
