import { defineConfig, Plugin } from "vite";
import react from "@vitejs/plugin-react";
import legacy from "@vitejs/plugin-legacy";

function removeCrossorigin(): Plugin {
  return {
    name: "remove-crossorigin",
    transformIndexHtml(html) {
      return html.replaceAll(" crossorigin", "");
    },
  };
}

export default defineConfig({
  plugins: [
    react(),
    legacy({
      targets: ["safari >= 14", "ios >= 14"],
    }),
    removeCrossorigin(),
  ],
  build: {
    target: "safari14",
  },
});
