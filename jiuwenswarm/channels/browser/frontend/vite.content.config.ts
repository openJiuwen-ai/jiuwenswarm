import { defineConfig } from "vite"
import { resolve } from "path"

/**
 * Content-script build.
 *
 * Content scripts are injected as classic scripts (no `type: "module"` in the
 * manifest), so they MUST NOT emit top-level ES `import` statements or shared
 * chunks. This config bundles the content script into a single self-contained
 * IIFE and inlines shared code.
 */
export default defineConfig({
  resolve: {
    alias: {
      "@shared": resolve(import.meta.dirname, "src/shared"),
    },
  },
  build: {
    rollupOptions: {
      input: {
        content: resolve(import.meta.dirname, "src/content/index.ts"),
      },
      output: {
        entryFileNames: "content/index.js",
        inlineDynamicImports: true,
        format: "iife",
      },
    },
    outDir: "dist",
    emptyOutDir: false,
    target: "chrome114",
    sourcemap: process.env.NODE_ENV !== "production",
    minify: process.env.NODE_ENV === "production",
  },
})
