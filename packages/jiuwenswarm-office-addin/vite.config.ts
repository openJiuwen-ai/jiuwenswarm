import { fileURLToPath } from 'node:url';

import { defineConfig } from 'vitest/config';

/** The PowerPoint app's package directory: its tools.json and profile.json are the contract the backend loads. */
export const contractDir = fileURLToPath(
  new URL('../../jiuwenswarm/office/powerpoint/', import.meta.url),
);

export default defineConfig({
  resolve: { alias: { '@contract': contractDir } },
  test: { include: ['src/**/*.test.ts'] },
});
