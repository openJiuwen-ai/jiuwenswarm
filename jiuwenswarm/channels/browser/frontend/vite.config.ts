import { defineConfig } from "vite"
import { resolve } from "path"
import { cpSync, mkdirSync } from "fs"

export default defineConfig({
  resolve: {
    alias: {
      "@shared": resolve(import.meta.dirname, "src/shared"),
    },
  },
  build: {
    rollupOptions: {
      input: {
        background: resolve(import.meta.dirname, "src/background/index.ts"),
        sidepanel:  resolve(import.meta.dirname, "src/sidepanel/index.ts"),
        popup:      resolve(import.meta.dirname, "src/popup/index.ts"),
        options:    resolve(import.meta.dirname, "src/options/index.ts"),
      },
      output: {
        entryFileNames: "[name]/index.js",
        chunkFileNames: "shared/[name]-[hash].js",
        assetFileNames: "assets/[name].[ext]",
        format: "es",
      },
    },
    outDir: "dist",
    emptyOutDir: true,
    target: "chrome114",
    sourcemap: process.env.NODE_ENV !== "production",
    minify: process.env.NODE_ENV === "production",
  },
  plugins: [
    // Copy static assets into dist after build
    {
      name: "copy-static",
      closeBundle() {
        // HTML files
        mkdirSync("dist/sidepanel", { recursive: true })
        mkdirSync("dist/popup", { recursive: true })
        mkdirSync("dist/options", { recursive: true })
        mkdirSync("dist/webview", { recursive: true })
        cpSync("src/sidepanel/sidepanel.html", "dist/sidepanel/sidepanel.html")
        cpSync("src/popup/popup.html", "dist/popup/popup.html")
        cpSync("src/options/options.html", "dist/options/options.html")
        cpSync("src/webview/chat.html", "dist/webview/chat.html")
        // Icons
        mkdirSync("dist/icons", { recursive: true })
        cpSync("icons", "dist/icons", { recursive: true })
        // Manifest
        cpSync("manifest.json", "dist/manifest.json")
      },
    },
  ],
})
