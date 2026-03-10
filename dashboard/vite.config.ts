import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:3456",
      "/events": {
        target: "http://localhost:3456",
        headers: { Connection: "keep-alive" },
      },
    },
  },
  build: {
    outDir: "dist/client",
  },
});
