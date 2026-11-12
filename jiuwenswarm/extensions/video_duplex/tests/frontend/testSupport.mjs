import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { readFile } from 'node:fs/promises';

const require = createRequire(new URL('../../../../channels/web/frontend/package.json', import.meta.url));
const { build } = require('esbuild');
const frontend = new URL('../../frontend/', import.meta.url);
const webFrontend = new URL('../../../../channels/web/frontend/', import.meta.url);

export const webSource = path => new URL(`src/${path}`, webFrontend);

/** Compile actual sources together so consumers share their stores and module identity. */
export async function loadModules(paths, { plugins = [], alias = { react: require.resolve('react') } } = {}) {
  const contents = paths.map(path => `export * from ${JSON.stringify(fileURLToPath(
    path instanceof URL ? path : new URL(path, frontend),
  ))};`).join('\n');
  const compiled = await build({
    stdin: { contents, resolveDir: fileURLToPath(webFrontend) },
    bundle: true, platform: 'node', format: 'esm', write: false,
    define: { 'import.meta.env.DEV': 'false' }, alias,
    plugins: [...plugins, { name: 'source-module-urls', setup(builder) {
      builder.onLoad({ filter: /\.tsx?$/ }, async ({ path }) => {
        const source = await readFile(path, 'utf8');
        if (!source.includes('import.meta.url')) return;
        return { contents: source.replaceAll('import.meta.url', JSON.stringify(pathToFileURL(path).href)),
          loader: path.endsWith('.tsx') ? 'tsx' : 'ts' };
      });
    } }],
  });
  return import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`);
}
