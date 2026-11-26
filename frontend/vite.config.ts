import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// Built into the Flask app's static folder, which streamrip serves as it is:
// nobody needs Node to run streamrip, only to change this. `npm run dev` serves
// it with live reload and passes the API to a running `streamrip web`.
export default defineConfig({
  plugins: [svelte(), tailwindcss()],
  base: "/static/app/",
  build: { outDir: "../streamrip/web/static/app", emptyOutDir: true },
  server: { proxy: { "/settings/api": "http://127.0.0.1:8888" } },
});
