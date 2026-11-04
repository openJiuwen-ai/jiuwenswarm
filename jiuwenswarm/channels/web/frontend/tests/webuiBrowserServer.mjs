import { createServer } from 'vite';

const server = await createServer({
  server: { host: '127.0.0.1', port: Number(process.env.WEBUI_BROWSER_PORT ?? 5189), strictPort: true },
  plugins: [{ name: 'webui-browser-protocol-fixture', configureServer(vite) {
    vite.middlewares.use('/__webui_test_echo', (request, response) => {
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify({ headers: request.headers }));
    });
  } }],
});
await server.listen();
server.printUrls();
